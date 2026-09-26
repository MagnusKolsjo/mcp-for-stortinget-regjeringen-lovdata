# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
regjeringen.py — Pipeline för hämtning av proposisjoner och NOU från regjeringen.no

Hanterar dokumenttyper:
  - Proposisjoner (Prop.) — länkas från Stortingets sak-objekt (typ='regjering')
  - NOU               — länkas direkt eller söks via regjeringen.no
  - Meld. St.         — stortingsmeldinger, samma URL-mönster

Strategi:
  1. Hämta HTML-sidan för dokumentet (regjeringen.no/id/... eller fullständig URL)
  2. Extrahera PDF-URL från <a href="/contentassets/...pdf">-länk
  3. Ladda ned PDF till temporär fil
  4. Extrahera text under minnes- och tidsvakt, med OCR-språk nor+eng
     för sidor utan textlager (pdftext_skydd.extrahera_pdf, se den modulen)
  5. Radera PDF omedelbart efter lyckad extraktion
  6. Returnera metadata + extraherad text

URL-format som hanteras:
  http://www.regjeringen.no/id/<KORTLÄNK_ID>
  https://www.regjeringen.no/id/<KORTLÄNK_ID>
  //www.regjeringen.no/id/...       (protokollrelativ)
  https://www.regjeringen.no/no/dokumenter/<DOK_SLUG>/id<DOC_ID>/

Extraktionen körs i en egen process (se pdftext_skydd.py), så den kan
aldrig fälla MCP-servern på minne eller tid.
"""

import html as html_lib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from pdftext_skydd import extrahera_pdf

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent
_BASE_URL   = "https://www.regjeringen.no"

_SESSION = httpx.Client(
    headers={
        # Kan ersättas via .env, t.ex. med den identifierare som regjeringen.no
        # har vitlistat för en viss installation.
        "User-Agent": os.getenv(
            "REGJERINGEN_USER_AGENT",
            "mcp-for-stortinget-regjeringen-lovdata/2.0 (+https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata)",
        ),
        "Referer":    _BASE_URL,
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nb,no;q=0.9,sv;q=0.8,en;q=0.7",
    },
    follow_redirects=True,
    timeout=60,
)


# ---------------------------------------------------------------------------
# URL-normalisering
# ---------------------------------------------------------------------------

def _normaliser_url(url: str) -> str:
    """
    Normaliserar en regjeringen.no-URL till fullt https-format.
    Hanterar: http://, https://, //www.regjeringen.no/..., /id/..., /no/...
    """
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = _BASE_URL + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]
    # Om URL redan har https:// behålls den
    if not url.startswith("https://"):
        url = _BASE_URL + "/" + url.lstrip("/")
    return url


# ---------------------------------------------------------------------------
# HTML-hämtning och PDF-URL-extraktion
# ---------------------------------------------------------------------------

class BotskyddFel(Exception):
    """
    regjeringen.no avvisade anropet med en bot-kontroll.

    Sedan 2026-08 ligger hela domänen bakom en Cloudflare JS-challenge som
    svarar HTTP 403 på varje förfrågan — även förstasidan och robots.txt.
    Kontrollen är oberoende av User-Agent och kan inte passeras av en
    HTTP-klient. Undantaget skiljer detta från vanliga hämtningsfel så att
    verktygen kan förklara läget i stället för att rapportera ett rått 403.
    """


def hamta_html(url: str) -> tuple[str, str]:
    """
    Hämtar HTML-sidan för ett dokument. Returnerar (html_text, final_url).
    Följer omdirigeringar automatiskt.

    Kastar BotskyddFel när källan svarar med en Cloudflare-utmaning.
    """
    norm_url = _normaliser_url(url)
    r = _SESSION.get(norm_url)

    if r.status_code == 403 and (
        r.headers.get("cf-mitigated") == "challenge"
        or "cloudflare" in r.headers.get("server", "").lower()
    ):
        raise BotskyddFel(
            "regjeringen.no avvisar automatiserad åtkomst med en Cloudflare-"
            "utmaning (HTTP 403, cf-mitigated: challenge). Kontrollen gäller hela "
            "domänen och är oberoende av User-Agent, så dokumentet kan inte "
            "hämtas härifrån. Vägar vidare: (1) Stortingets innstilling och "
            "referat i samma sak återger propositionens innehåll utförligt och "
            "är fritt tillgängliga — använd nor_lista_publikasjoner(sakid) och "
            "hämta innstillingen; (2) dokument som redan ligger i den lokala "
            "cachen nås oförändrat via nor_sok och nor_sok_i_dokument. "
            "Långsiktig åtgärd är en vitlistningsbegäran hos DSS."
        )

    r.raise_for_status()
    return r.text, str(r.url)


def extrahera_pdf_url(html: str, bas_url: str) -> Optional[str]:
    """
    Extraherar PDF-URL från regjeringen.no HTML-sida.

    Regjeringen.no-mönster (verifierat 2026-05-15):
      <a href="/contentassets/{hash}/no/pdfs/{id}pdfs.pdf">...</a>

    Returnerar fullständig https-URL eller None om ingen PDF hittades.
    """
    # Primär: /contentassets/.../pdfs/...pdf
    treff = re.findall(
        r'href=["\']'
        r'(/contentassets/[^"\']+\.pdf[^"\']*)'
        r'["\']',
        html, re.I
    )
    if treff:
        # Välj den som innehåller "/pdfs/" (nedladdningsbar version)
        for t in treff:
            if "/pdfs/" in t.lower():
                return _BASE_URL + t
        # Fallback: första träffen
        return _BASE_URL + treff[0]

    # Sekundär: fullständig https-URL med .pdf
    treff2 = re.findall(
        r'href=["\']'
        r'(https://[^"\']*regjeringen\.no[^"\']+\.pdf[^"\']*)'
        r'["\']',
        html, re.I
    )
    if treff2:
        return treff2[0]

    return None


def extrahera_metadata(html: str, final_url: str) -> dict:
    """Extraherar titel, dokumenttype och dato från HTML-sidan."""
    # Titel från <title> — avkoda HTML-entiteter (t.ex. &#x2013; → –)
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    tittel  = html_lib.unescape(
        title_m.group(1).replace("- regjeringen.no", "").strip()
    ) if title_m else ""

    # Beteckning ur titel eller de första 2000 tecknen av HTML
    sokvag = tittel or html_lib.unescape(html[:2000])
    bet_m  = re.search(
        r"(Prop\.\s*\d+\s*[A-Z]*\s*\(\d{4}[–\-]\d{2,4}\)|"
        r"NOU\s*\d{4}:\s*\d+|"
        r"Meld\.\s*St\.\s*\d+\s*\(\d{4}[–\-]\d{2,4}\))",
        sokvag, re.I
    )
    beteckning = bet_m.group(0).strip() if bet_m else ""

    # Dokumenttype
    dok_type = "proposisjon"
    if "NOU" in (beteckning + tittel).upper():
        dok_type = "nou"
    elif "Meld. St." in (beteckning + tittel):
        dok_type = "meld_st"

    return {
        "tittel":     tittel,
        "beteckning": beteckning,
        "dok_type":   dok_type,
        "url":        final_url,
    }


# ---------------------------------------------------------------------------
# PDF-nedladdning och textextraktion
# ---------------------------------------------------------------------------

def ladda_ned_pdf(pdf_url: str, sokvag: Path) -> tuple[bool, str]:
    """Laddar ned PDF till disk. Returnerar (True, "") eller (False, felmeddelande)."""
    try:
        r = _SESSION.get(pdf_url, timeout=120)
        r.raise_for_status()
        sokvag.parent.mkdir(parents=True, exist_ok=True)
        sokvag.write_bytes(r.content)
        return True, ""
    except Exception as exc:
        log.warning("PDF-nedladdning misslyckades (%s): %s", pdf_url, exc)
        return False, str(exc)


def extrahera_text(sokvag: Path, kalla: str = "") -> Optional[str]:
    """
    Extraherar text från PDF under minnes- och tidsvakt (pdftext_skydd),
    med nor+eng som OCR-språk för sidor utan textlager.
    """
    try:
        res = extrahera_pdf(sokvag, prefix="NOR", standardsprak="nor+eng",
                            kalla_id=kalla or sokvag.name, kalla_url=kalla or "")
        text = res.text
        return text if text and len(text.strip()) > 50 else None
    except Exception as exc:
        log.warning("Textextraktion misslyckades (%s): %s", sokvag.name, exc)
        return None


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def hamta_og_ekstraher(url: str) -> dict:
    """
    Hämtar ett regjeringen.no-dokument och returnerar metadata + fulltext.

    Parametrar:
      url — URL till dokumentet. Kan vara:
            - regjeringen.no/id/PRP... (kortlänk)
            - regjeringen.no/no/dokumenter/... (fullständig URL)
            - //www.regjeringen.no/... (protokollrelativ)

    Returnerar dict med:
      tittel      — dokumentets titel
      beteckning  — formell beteckning (t.ex. "Prop. 165 L (2024-2025)")
      dok_type    — 'proposisjon', 'nou', 'meld_st'
      url         — final URL efter omdirigeringar
      pdf_url     — PDF-URL (None om ingen hittades)
      fulltext_md — extraherad Markdown-text (None om extraktion misslyckades)
      fel         — eventuellt felmeddelande
    """
    try:
        # 1. Hämta HTML
        html, final_url = hamta_html(url)
        metadata        = extrahera_metadata(html, final_url)
        pdf_url         = extrahera_pdf_url(html, final_url)

        if not pdf_url:
            return {**metadata, "pdf_url": None, "fulltext_md": None,
                    "fel": "Ingen PDF-URL hittades på sidan"}

        metadata["pdf_url"] = pdf_url

        # 2. Ladda ned PDF till temporär fil
        suffix  = Path(pdf_url.split("?")[0]).suffix or ".pdf"
        tmp_fil = Path(tempfile.mktemp(suffix=suffix, dir=_SCRIPT_DIR / "logs"))
        tmp_fil.parent.mkdir(parents=True, exist_ok=True)

        ok, fel = ladda_ned_pdf(pdf_url, tmp_fil)
        if not ok:
            return {**metadata, "fulltext_md": None, "fel": f"PDF-nedladdning: {fel}"}

        # 3. Extrahera text (OCR för sidor utan textlager sker inbyggt, se
        #    pdftext_skydd.py — inget separat OCR-fallbacksteg behövs längre)
        fulltext = extrahera_text(tmp_fil, kalla=pdf_url)

        # 4. Radera PDF direkt
        try:
            tmp_fil.unlink()
        except Exception:
            pass

        if not fulltext:
            return {**metadata, "fulltext_md": None,
                    "fel": "Textextraktion misslyckades (även efter OCR)"}

        log.info("regjeringen.no OK: %s (%d tecken)", metadata.get("beteckning") or url, len(fulltext))
        return {**metadata, "fulltext_md": fulltext, "fel": None}

    except BotskyddFel as exc:
        # Källan blockerar all automatiserad åtkomst — inte ett fel i hämtningen.
        # Märks med egen felkod så anroparen kan skilja det från nätverksfel.
        log.warning("regjeringen.no blockerar automatiserad åtkomst (%s)", url)
        return {
            "tittel": "", "beteckning": "", "dok_type": "proposisjon",
            "url": url, "pdf_url": None, "fulltext_md": None,
            "fel_typ": "kalla_blockerar_automatiserad_atkomst",
            "fel": str(exc),
        }

    except Exception as exc:
        log.error("hamta_og_ekstraher misslyckades (%s): %s", url, exc)
        return {
            "tittel": "", "beteckning": "", "dok_type": "proposisjon",
            "url": url, "pdf_url": None, "fulltext_md": None,
            "fel_typ": "hamtningsfel",
            "fel": str(exc),
        }
