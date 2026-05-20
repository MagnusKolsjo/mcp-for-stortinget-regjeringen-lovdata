# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
lovdata_sync.py — Synkronisering av Lovdata gratis bulk-dataset

Hämtar dagligen (paket upptäcks dynamiskt via /v1/publicData/list):
  gjeldende-lover.tar.bz2              (~5,5 MB,  738 lagar)
  gjeldende-sentrale-forskrifter.tar.bz2 (~20 MB, 3 427 forskrifter)

Licens: NLOD 2.0 — fri användning inklusive AI-träning, inget konto krävs.

Flöde per körning:
  1. HEAD-begäran mot Lovdata (ingen Last-Modified/ETag tillgänglig)
  2. Ladda ner tarball till temporär fil
  3. Beräkna SHA-256 — om oförändrad jämfört med norge.sync_status → hoppa
  4. Packa upp och parsa HTML (BeautifulSoup) → Markdown
  5. Upsert i norge.dokument (UNIQUE(kilde, lovdata_id))
  6. Uppdatera norge.sync_status med ny checksum och tidsstämpel
  7. Radera temporär tarball

Körning (manuellt eller via launchd/cron):
  python3 lovdata_sync.py                     # synka båda källorna
  python3 lovdata_sync.py --kalla lover       # bara lagar
  python3 lovdata_sync.py --kalla forskrifter # bara forskrifter
  python3 lovdata_sync.py --tvinga            # ignorera checksum, parsa om allt
  python3 lovdata_sync.py --installera-schema # installera schemalagt jobb (se .env)

Schemaläggning:
  Styrs av SCHEMALAGGARE och CRON_SCHEMA i .env.
  Standard: launchd 04:00 (macOS — kör vid uppvakning om datorn sov)

Städning av tempfiler:
  stada_temp_filer() körs automatiskt vid varje synk och rensar bort
  kvarliggande .tar.bz2- och .pdf-filer i logs/ (rester efter krascher).
"""

import argparse
import hashlib
import logging
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent

_LOVDATA_LIST_URL = "https://api.lovdata.no/v1/publicData/list"
_LOVDATA_GET_BASE = "https://api.lovdata.no/v1/publicData/get"

# Konfiguration per källa — url byggs dynamiskt via _LOVDATA_GET_BASE + paket_namn
_KALLOR = {
    "lover": {
        "paket_namn": "gjeldende-lover",
        "mapp":       "nl",
        "dok_typ":    "lov",
        "kilde_id":   "lovdata_lover",
    },
    "forskrifter": {
        "paket_namn": "gjeldende-sentrale-forskrifter",
        "mapp":       "sf",
        "dok_typ":    "forskrift",
        "kilde_id":   "lovdata_forskrifter",
    },
}

_SESSION_JSON = httpx.Client(
    headers={
        "User-Agent": "mcp-for-stortinget-regjeringen-lovdata/1.0 (+https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata; NLOD-2.0)",
        "Accept":     "application/json",
    },
    follow_redirects=True,
    timeout=30,
)

_SESSION = httpx.Client(
    headers={
        "User-Agent": "mcp-for-stortinget-regjeringen-lovdata/1.0 (+https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata; NLOD-2.0)",
        "Accept":     "application/octet-stream",
    },
    follow_redirects=True,
    timeout=120,
)


# ---------------------------------------------------------------------------
# Lovdata paketupptäckt
# ---------------------------------------------------------------------------

def hamta_lovdata_paket_lista() -> list[str]:
    """
    Hämtar lista med tillgängliga paketnamn från Lovdata /v1/publicData/list.

    Returnerar lista med paketnamn, t.ex.:
      ['gjeldende-lover', 'gjeldende-sentrale-forskrifter',
       'lovtidend-historisk-2023', 'lovtidend-innevaerende-aar']

    Vid fel returneras tom lista — anroparen kan då falla tillbaka på
    hårdkodad konfiguration i _KALLOR.
    """
    try:
        r = _SESSION_JSON.get(_LOVDATA_LIST_URL)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            if data and isinstance(data[0], str):
                return data
            elif data and isinstance(data[0], dict):
                # Extrahera nyckelnamn — Lovdata kan skicka {name:...} eller {id:...}
                return [
                    item.get("name") or item.get("id") or item.get("pakke") or ""
                    for item in data
                    if item.get("name") or item.get("id") or item.get("pakke")
                ]
        log.warning("Oväntat svarsformat från /v1/publicData/list: %s", type(data).__name__)
        return []

    except Exception as exc:
        log.warning(
            "Kunde inte hämta Lovdata paketlista (%s) — fortsätter med hårdkodad konfiguration",
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# HTML → Markdown-konvertering
# ---------------------------------------------------------------------------

def _text(element) -> str:
    """Extraherar ren text från ett BeautifulSoup-element."""
    return element.get_text(separator=" ", strip=True) if element else ""


def _meta_varde(soup: BeautifulSoup, klass: str) -> str:
    """Hämtar värdet ur <dd class="{klass}"> i dokumenthuvudet."""
    dd = soup.find("dd", class_=klass)
    return _text(dd) if dd else ""


def _meta_lankar(soup: BeautifulSoup, klass: str) -> list[str]:
    """Hämtar href-attributen ur <dd class="{klass}"><ul><li><a href=...>."""
    dd = soup.find("dd", class_=klass)
    if not dd:
        return []
    return [a["href"] for a in dd.find_all("a", href=True)]


def _parsera_datum(text: str) -> Optional[date]:
    """Konverterar 'YYYY-MM-DD' (första datumet om flera) till date-objekt."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _kapitel_rubrik(section) -> str:
    """Extraherar kapitelrubrik ur ett <section>-element."""
    for tag in ("h2", "h3", "h4"):
        h = section.find(tag)
        if h:
            return _text(h)
    return ""


def html_till_markdown(html: str, dok_typ: str) -> tuple[dict, str]:
    """
    Parserar ett Lovdata HTML-dokument och returnerar (metadata, fulltext_md).

    metadata-dict:
      lovdata_id    — t.ex. 'NL/lov/2005-05-20-28'
      beteckning    — t.ex. 'LOV-2005-05-20-28'
      tittel        — full titel
      korttittel    — korttitel / populärnamn
      departement   — ansvarigt departement
      dato_ikraft   — date-objekt (första ikraftträdandedatum)
      dato_vedtatt  — date-objekt (kunngjort/vedtatt-datum)
      dok_type      — 'lov' eller 'forskrift'
      url           — https://lovdata.no/dokument/NL/lov/2005-05-20-28
      hjemmel       — lista med href-strängar (för forskrifter)
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Metadata ────────────────────────────────────────────────────────────
    lovdata_id  = _meta_varde(soup, "dokid")
    beteckning  = _meta_varde(soup, "legacyID")
    tittel      = _meta_varde(soup, "title")
    korttittel  = _meta_varde(soup, "titleShort")
    departement = _meta_varde(soup, "ministry")
    dato_ikraft = _parsera_datum(_meta_varde(soup, "dateInForce"))
    dato_publisert = _parsera_datum(_meta_varde(soup, "dateOfPublication"))
    hjemmel     = _meta_lankar(soup, "basedOn")
    refid       = _meta_varde(soup, "refid")

    url = f"https://lovdata.no/dokument/{refid}" if refid else ""

    metadata = {
        "lovdata_id":  lovdata_id,
        "beteckning":  beteckning,
        "tittel":      tittel,
        "korttittel":  korttittel,
        "departement": departement,
        "dato_ikraft": dato_ikraft,
        "dato_vedtatt": dato_publisert,
        "dok_type":    dok_typ,
        "url":         url,
        "hjemmel":     hjemmel,
    }

    # ── Markdown-konvertering ────────────────────────────────────────────────
    linjer: list[str] = []

    # Rubrik
    linjer.append(f"# {tittel}")
    if korttittel and korttittel != tittel:
        linjer.append(f"*{korttittel}*")
    linjer.append("")

    # Kortfattad metadata
    if departement:
        linjer.append(f"**Departement:** {departement}")
    if dato_ikraft:
        linjer.append(f"**I kraft:** {dato_ikraft}")
    sist_endret = _meta_varde(soup, "lastChangeInForce")
    if sist_endret:
        linjer.append(f"**Sist endret:** {sist_endret}")
    if hjemmel:
        linjer.append(f"**Hjemmel:** {', '.join(hjemmel[:3])}")
    linjer.append("")
    linjer.append("---")
    linjer.append("")

    # Lovtekst — itererera kapitel och paragrafer
    # Lagar använder <section> för kapitel; forskrifter har bara <main>/<body>
    body = soup.find("body")
    if not body:
        return metadata, "\n".join(linjer)

    # Fjern headern (metadata-blocket) ur body
    header = body.find("header")
    if header:
        header.decompose()

    # Hitta dokumentkroppen (lagar: <body>, forskrifter: <main class="documentBody">)
    main = body.find("main") or body
    sektioner = main.find_all("section", recursive=False)
    if not sektioner:
        sektioner = main.find_all("section")

    if sektioner:
        # Lagar: kapitelstruktur via <section>
        for seksjon in sektioner:
            rubrik = _kapitel_rubrik(seksjon)
            if rubrik:
                linjer.append(f"## {rubrik}")
                linjer.append("")
            for article in seksjon.find_all("article", class_="legalArticle"):
                _artikel_till_md(article, linjer)
    else:
        # Forskrifter och kortare lagar: paragrafer direkt under main
        for article in main.find_all("article", class_="legalArticle"):
            _artikel_till_md(article, linjer)

    return metadata, "\n".join(linjer)


def _artikel_till_md(article, linjer: list[str]) -> None:
    """Lägger till en legalArticle som Markdown-paragraf i linjer-listan."""
    para_nr   = article.get("data-name", "")
    # Lagar: <h4 class="legalArticleHeader">, forskrifter: <h2 class="legalArticleHeader">
    header_el = article.find(["h2", "h3", "h4"], class_="legalArticleHeader")

    if header_el:
        nr_el    = header_el.find("span", class_="legalArticleValue")
        tittel_el = header_el.find("span", class_="legalArticleTitle")
        nr_text   = _text(nr_el) if nr_el else para_nr
        tittel_text = _text(tittel_el) if tittel_el else ""
        if tittel_text:
            linjer.append(f"### {nr_text}. {tittel_text}")
        else:
            linjer.append(f"### {nr_text}")
    elif para_nr:
        linjer.append(f"### {para_nr}")

    # Ledd (stycken)
    for ledd in article.find_all("article", class_="legalP"):
        ledd_text = _text(ledd)
        if ledd_text:
            linjer.append("")
            linjer.append(ledd_text)

    # Endringer/tillägg (enklare historiknotering)
    endring = article.find("article", class_="changesToParent")
    if endring:
        endring_text = _text(endring)
        if endring_text:
            linjer.append("")
            linjer.append(f"*{endring_text}*")

    linjer.append("")


# ---------------------------------------------------------------------------
# Nedladdning och SHA-256
# ---------------------------------------------------------------------------

def ladda_ned_tarball(url: str, sokvag: Path) -> str:
    """Laddar ner tarball och returnerar SHA-256 av innehållet."""
    log.info("Laddar ner: %s", url)
    hasher = hashlib.sha256()
    with _SESSION.stream("GET", url) as r:
        r.raise_for_status()
        with open(sokvag, "wb") as fh:
            for chunk in r.iter_bytes(chunk_size=65536):
                fh.write(chunk)
                hasher.update(chunk)
    sha = hasher.hexdigest()
    log.info("Nedladdad: %s (%s bytes, sha256=%s…)", sokvag.name,
             sokvag.stat().st_size, sha[:12])
    return sha


# ---------------------------------------------------------------------------
# Databas
# ---------------------------------------------------------------------------

def _hamta_checksum(kilde_id: str) -> Optional[str]:
    """Hämtar lagrad checksum ur sync_status (returnerar None om saknas)."""
    try:
        from db import get_sync_status
        return get_sync_status(kilde_id).get("checksum")
    except Exception as exc:
        log.warning("Kunde inte hämta checksum (%s): %s", kilde_id, exc)
        return None


def _uppdatera_sync_status(kilde_id: str, checksum: str, antal: int) -> None:
    """Uppdaterar eller skapar en rad i sync_status."""
    try:
        from db import set_sync_status
        set_sync_status(kilde_id, checksum=checksum, detaljer={"antal_dokument": antal})
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status (%s): %s", kilde_id, exc)


def upsert_lovdata_dokument(meta: dict, fulltext_md: str) -> None:
    """Spar ett Lovdata-dokument i norge.dokument via db.upsert_dokument."""
    from db import upsert_dokument
    upsert_dokument(
        kilde      = "lovdata",
        dok_type   = meta["dok_type"],
        beteckning = meta["beteckning"],
        tittel     = meta["tittel"],
        sesjonid   = None,
        dato       = meta["dato_ikraft"],
        url        = meta["url"],
        publikasjonid = None,
        sakid      = None,
        lovdata_id = meta["lovdata_id"],
        fulltext_md = fulltext_md,
    )


# ---------------------------------------------------------------------------
# Huvudflöde per källa
# ---------------------------------------------------------------------------

def synka_kalla(
    kalla_nyckel: str,
    tvinga: bool = False,
    tillgangliga_paket: Optional[list] = None,
) -> int:
    """
    Synkar en Lovdata-källa. Returnerar antal behandlade dokument.

    kalla_nyckel       — 'lover' eller 'forskrifter'
    tvinga             — om True, ignoreras lagrad checksum och allt parsas om
    tillgangliga_paket — lista från hamta_lovdata_paket_lista() för verifiering;
                         None hoppar verifieringen
    """
    kalla      = _KALLOR[kalla_nyckel]
    paket_namn = kalla["paket_namn"]
    mapp       = kalla["mapp"]
    dok_typ    = kalla["dok_typ"]
    kilde_id   = kalla["kilde_id"]

    # Verifiera att paketet finns (om lista tillhandahållits)
    if tillgangliga_paket is not None and paket_namn not in tillgangliga_paket:
        log.error(
            "%s: paketet '%s' finns inte i Lovdatas lista (%s) — hoppar",
            kalla_nyckel, paket_namn, tillgangliga_paket,
        )
        return 0

    url = f"{_LOVDATA_GET_BASE}/{paket_namn}.tar.bz2"

    log_dir = _SCRIPT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Ladda ner till temporär fil
    tmp = Path(tempfile.mktemp(suffix=".tar.bz2", dir=log_dir))
    try:
        ny_checksum = ladda_ned_tarball(url, tmp)

        # Kontrollera om tarball ändrats
        if not tvinga:
            gammal_checksum = _hamta_checksum(kilde_id)
            if gammal_checksum and gammal_checksum == ny_checksum:
                log.info("%s: oförändrad (checksum match) — hoppar parsning", kalla_nyckel)
                return 0

        # Öppna tarball och parsa
        log.info("%s: parsar tarball...", kalla_nyckel)
        antal_ok = 0
        antal_fel = 0

        with tarfile.open(tmp, "r:bz2") as tf:
            for medlem in tf.getmembers():
                namn = medlem.name
                # Hoppa över mappar och filer utanför rätt mapp
                if not namn.endswith(".xml") or not namn.startswith(mapp + "/"):
                    continue
                # Hoppa nynorsk-varianter (-nn.xml) — behåller bokmål som primär
                if namn.endswith("-nn.xml"):
                    continue

                try:
                    fh = tf.extractfile(medlem)
                    if fh is None:
                        continue
                    html = fh.read().decode("utf-8", errors="replace")
                    meta, fulltext_md = html_till_markdown(html, dok_typ)

                    if not meta.get("lovdata_id"):
                        log.warning("Ingen lovdata_id i %s — hoppar", namn)
                        continue

                    upsert_lovdata_dokument(meta, fulltext_md)
                    antal_ok += 1

                    if antal_ok % 100 == 0:
                        log.info("  %d/%d behandlade...", antal_ok,
                                 len([m for m in tf.getmembers()
                                      if m.name.endswith(".xml")]))

                except Exception as exc:
                    antal_fel += 1
                    log.warning("Fel vid parsning av %s: %s", namn, exc)

        _uppdatera_sync_status(kilde_id, ny_checksum, antal_ok)
        log.info("%s klar: %d OK, %d fel", kalla_nyckel, antal_ok, antal_fel)
        return antal_ok

    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Städning av temporära filer
# ---------------------------------------------------------------------------

def stada_temp_filer() -> int:
    """
    Rensar kvarliggande temporära filer i logs/-mappen.

    Vid krasch eller avbrott kan .tar.bz2-tarbollarna och .pdf-filer
    från regjeringen.py bli kvar i logs/. Denna funktion raderar dem
    om de är äldre än 1 timme (dvs. inte pågående nedladdning).

    Returnerar antal raderade filer.
    """
    log_dir = _SCRIPT_DIR / "logs"
    if not log_dir.exists():
        return 0

    nu = time.time()
    en_timme = 3600
    raderade = 0

    for monster in ("*.tar.bz2", "*.tar.gz", "*.pdf"):
        for fil in log_dir.glob(monster):
            alder = nu - fil.stat().st_mtime
            if alder > en_timme:
                try:
                    fil.unlink()
                    log.info("Städat kvarliggande tempfil: %s", fil.name)
                    raderade += 1
                except Exception as exc:
                    log.warning("Kunde inte radera %s: %s", fil.name, exc)

    return raderade


# ---------------------------------------------------------------------------
# Schemaläggning — launchd (macOS) och cron (Linux)
# ---------------------------------------------------------------------------

_LAUNCHD_PLIST_NAMN = "se.magnuskolsjo.mcp-norge-synk"

_LAUNCHD_PLIST_MALL = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{skript}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>{timme}</integer>
        <key>Minute</key> <integer>{minut}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{logg}</string>
    <key>StandardErrorPath</key>
    <string>{logg}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""

_CRON_RAD = "{minut} {timme} * * * /bin/bash {skript} >> {logg} 2>&1"


def installera_schema() -> None:
    """Installerar schemalagt jobb baserat på SCHEMALAGGARE och CRON_SCHEMA i .env.

    Stöder: launchd (macOS) och cron (Linux och macOS).
    """
    schemalaggare = os.getenv("SCHEMALAGGARE", "launchd").lower()
    cron_schema   = os.getenv("CRON_SCHEMA", "0 4 * * *")

    # Parsa cron-uttrycket: "minut timme * * *"
    try:
        delar = cron_schema.split()
        minut = int(delar[0])
        timme = int(delar[1])
    except (IndexError, ValueError):
        log.error("Ogiltigt CRON_SCHEMA: '%s'. Förväntat format: 'minut timme * * *'", cron_schema)
        return

    skript  = str((_SCRIPT_DIR / "synk_daglig.sh").resolve())
    log_fil = str((_SCRIPT_DIR / "logs" / "synk_daglig.log").resolve())
    (_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt till SCHEMALAGGARE=cron.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_sokvag = plist_dir / f"{_LAUNCHD_PLIST_NAMN}.plist"

        innehall = _LAUNCHD_PLIST_MALL.format(
            label=_LAUNCHD_PLIST_NAMN,
            skript=skript,
            logg=log_fil,
            timme=timme,
            minut=minut,
        )
        plist_sokvag.write_text(innehall, encoding="utf-8")

        # Avregistrera eventuell gammal plist innan omladdning
        subprocess.run(["launchctl", "unload", str(plist_sokvag)], capture_output=True)
        resultat = subprocess.run(
            ["launchctl", "load", str(plist_sokvag)],
            capture_output=True, text=True
        )
        if resultat.returncode == 0:
            log.info("launchd-jobb installerat: %s", plist_sokvag)
            log.info("Kör dagligen kl. %02d:%02d (kör vid uppvakning om datorn sov).", timme, minut)
        else:
            log.error("launchctl load misslyckades: %s", resultat.stderr)

    elif schemalaggare == "cron":
        ny_rad = _CRON_RAD.format(skript=skript, logg=log_fil, minut=minut, timme=timme)
        befintlig = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        befintliga_rader = befintlig.stdout.splitlines() if befintlig.returncode == 0 else []

        # Ta bort eventuell gammal rad för samma skript
        filtrerade = [r for r in befintliga_rader if skript not in r]
        filtrerade.append(ny_rad)

        ny_crontab = "\n".join(filtrerade) + "\n"
        proc = subprocess.run(["crontab", "-"], input=ny_crontab, text=True, capture_output=True)
        if proc.returncode == 0:
            log.info("Cron-jobb installerat: %s", ny_rad)
            log.info("Kör dagligen kl. %02d:%02d.", timme, minut)
        else:
            log.error("crontab-installation misslyckades: %s", proc.stderr)

    else:
        log.error("Okänd SCHEMALAGGARE: '%s'. Välj 'launchd' eller 'cron'.", schemalaggare)


# ---------------------------------------------------------------------------
# CLI-startpunkt
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt = "%H:%M:%S",
        handlers = [
            logging.StreamHandler(),
            logging.FileHandler(_SCRIPT_DIR / "logs" / "lovdata_sync.log"),
        ]
    )

    parser = argparse.ArgumentParser(
        description="Synkroniserar Lovdata gratis bulk-dataset till lokal databas."
    )
    parser.add_argument(
        "--kalla",
        choices=["lover", "forskrifter", "alla"],
        default="alla",
        help="Vilken källa som ska synkas (standard: alla)",
    )
    parser.add_argument(
        "--tvinga",
        action="store_true",
        help="Ignorera lagrad checksum och parsa om allt",
    )
    parser.add_argument(
        "--installera-schema",
        action="store_true",
        help="Installera schemalagt jobb via cron eller launchd (se SCHEMALAGGARE och CRON_SCHEMA i .env)",
    )
    args = parser.parse_args()

    # Schemaläggning — körs fristående, ingen DB-initiering behövs
    if args.installera_schema:
        installera_schema()
        return

    from db import initiera_schema
    initiera_schema()

    # Städa kvarliggande tempfiler från eventuella tidigare krascher
    raderade = stada_temp_filer()
    if raderade:
        log.info("Städning: %d kvarliggande tempfil(er) raderade", raderade)

    # Hämta tillgängliga paket från Lovdata för dynamisk URL-konstruktion
    tillgangliga_paket = hamta_lovdata_paket_lista()
    if tillgangliga_paket:
        log.info("Tillgängliga Lovdata-paket (%d): %s", len(tillgangliga_paket), tillgangliga_paket)
    else:
        log.warning("Kunde inte verifiera paketlistan — fortsätter med känd konfiguration")
        tillgangliga_paket = None  # None hoppar verifieringssteget i synka_kalla

    kallor = ["lover", "forskrifter"] if args.kalla == "alla" else [args.kalla]
    totalt = 0
    for k in kallor:
        totalt += synka_kalla(k, tvinga=args.tvinga, tillgangliga_paket=tillgangliga_paket)

    log.info("Synk klar. Totalt behandlade dokument: %d", totalt)


if __name__ == "__main__":
    main()
