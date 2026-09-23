# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
lovdata_sync.py — Synkronisering av Lovdata gratis bulk-dataset

Hämtar (paketen upptäcks via /v1/publicData/list):
  gjeldende-lover.tar.bz2                (~6 MB,  gällande lagar)
  gjeldende-sentrale-forskrifter.tar.bz2 (~21 MB, gällande sentrala forskrifter)
  lovtidend-avd1-<år1>-<år2>.tar.bz2     (~70 MB, Norsk Lovtidend avd. I, historiska år)
  lovtidend-avd1-<innevarande år>.tar.bz2 (~1-2 MB, innevarande års kungöranden)

Lovtidend-paketens namn byts vid årsskiftet; de väljs ur listan efter
mönster, inte efter hårdkodat namn.

Licens: NLOD 2.0 — fri användning inklusive AI-träning, inget konto krävs.

Flöde per källa:
  1. Läs paketets lastModified ur listan — oförändrat sedan förra synken → hoppa
     utan nedladdning
  2. Ladda ner tarball till temporär fil
  3. Beräkna SHA-256 — om oförändrad jämfört med norge.sync_status → hoppa
  4. Packa upp och parsa HTML (BeautifulSoup) → Markdown
  5. Upsert i norge.dokument (UNIQUE(kilde, lovdata_id))
  6. Uppdatera norge.sync_status med checksum, paketnamn och lastModified
  7. Radera temporär tarball

Körning (manuellt eller via launchd/cron):
  python3 lovdata_sync.py                             # synka alla källor
  python3 lovdata_sync.py --kalla lover               # bara lagar
  python3 lovdata_sync.py --kalla forskrifter         # bara forskrifter
  python3 lovdata_sync.py --kalla lovtidend           # innevarande års Lovtidend
  python3 lovdata_sync.py --kalla lovtidend-historisk # Lovtidend, tidigare år
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

# Konfiguration per källa. Gjeldende-paketen har fasta namn. Lovtidend-paketen
# byter namn vid årsskiftet (lovtidend-avd1-2026 → lovtidend-avd1-2027, och
# det historiska paketet växer med ett år), så de anges som mönster och
# väljs ur Lovdatas lista vid varje synk.
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
    "lovtidend-historisk": {
        "paket_monster": r"lovtidend-avd1-\d{4}-\d{4}",
        "mapp":          "lti",
        "dok_typ":       "lovtidend",
        "kilde_id":      "lovdata_lovtidend_historisk",
    },
    "lovtidend": {
        "paket_monster": r"lovtidend-avd1-\d{4}",
        "mapp":          "lti",
        "dok_typ":       "lovtidend",
        "kilde_id":      "lovdata_lovtidend_aar",
    },
}

_USER_AGENT = (
    "mcp-for-stortinget-regjeringen-lovdata "
    "(+https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata; NLOD-2.0)"
)

_SESSION_JSON = httpx.Client(
    headers={
        "User-Agent": _USER_AGENT,
        "Accept":     "application/json",
    },
    follow_redirects=True,
    timeout=30,
)

_SESSION = httpx.Client(
    headers={
        "User-Agent": _USER_AGENT,
        "Accept":     "application/octet-stream",
    },
    follow_redirects=True,
    timeout=120,
)


# ---------------------------------------------------------------------------
# Lovdata paketupptäckt
# ---------------------------------------------------------------------------

def hamta_lovdata_paket() -> dict[str, dict]:
    """
    Hämtar Lovdatas paketlista från /v1/publicData/list.

    Källan svarar med en lista av objekt:
      {"filename": "gjeldende-lover.tar.bz2", "sizeBytes": "5810986",
       "lastModified": "2026-09-19T01:31:00Z", "description": "..."}

    Returnerar {paketnamn utan .tar.bz2: {"storlek": int, "andrad": str}}.
    Vid fel returneras tom dict; synken fortsätter då med de fasta
    paketnamnen, medan Lovtidend-källorna hoppas över eftersom deras namn
    bara kan väljas ur listan.
    """
    try:
        r = _SESSION_JSON.get(_LOVDATA_LIST_URL)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Kunde inte hämta Lovdatas paketlista (%s)", exc)
        return {}

    if not isinstance(data, list):
        log.warning("Oväntat svarsformat från /v1/publicData/list: %s", type(data).__name__)
        return {}

    paket: dict[str, dict] = {}
    for post in data:
        if isinstance(post, str):
            filnamn, storlek, andrad = post, 0, ""
        elif isinstance(post, dict):
            filnamn = post.get("filename") or ""
            try:
                storlek = int(post.get("sizeBytes") or 0)
            except (TypeError, ValueError):
                storlek = 0
            andrad = post.get("lastModified") or ""
        else:
            continue
        namn = filnamn.removesuffix(".tar.bz2")
        if namn:
            paket[namn] = {"storlek": storlek, "andrad": andrad}
    return paket


def hamta_lovdata_paket_lista() -> list[str]:
    """Paketnamnen i Lovdatas lista, t.ex. ['gjeldende-lover', 'lovtidend-avd1-2026']."""
    return list(hamta_lovdata_paket())


def valj_paket(kalla_nyckel: str, paket: dict[str, dict], idag: Optional[date] = None) -> Optional[str]:
    """
    Väljer paketnamn för en källa ur Lovdatas lista.

    Källor med fast paketnamn får det namnet. Innevarande års Lovtidend blir
    lovtidend-avd1-<år>; saknas det (t.ex. första dagarna efter årsskiftet)
    väljs det senaste årspaketet som finns. Det historiska paketet är det
    med senast slutår. Returnerar None om inget paket passar.
    """
    kalla = _KALLOR[kalla_nyckel]
    if "paket_namn" in kalla:
        return kalla["paket_namn"]

    monster = re.compile(kalla["paket_monster"] + r"$")
    kandidater = sorted(n for n in paket if monster.fullmatch(n))
    if not kandidater:
        return None
    if kalla_nyckel == "lovtidend":
        ar = (idag or date.today()).year
        onskat = f"lovtidend-avd1-{ar}"
        if onskat in kandidater:
            return onskat
        log.warning("%s saknas i Lovdatas lista — använder %s", onskat, kandidater[-1])
    return kandidater[-1]


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

    if dok_typ == "lovtidend":
        return _lovtidend_till_markdown(soup)

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


# ---------------------------------------------------------------------------
# Lovtidend avd. I → Markdown
#
# Lovtidend kungör lagar och sentrala forskrifter i den form de beslutades.
# De flesta är ändringsdokument: brödtexten består av <article class=
# "document-change"> per ändrad författning, <article class="change"> per
# ändring ("§ 21 skal lyde:") och <article class="futureLegalArticle"> med den
# nya lydelsen, blandat med listor, tabeller och fotnoter. Parsern för
# gjeldende-paketen läser bara legalArticle/legalP och tappar därför det mesta
# av ett ändringsdokument. Här går vi i stället igenom hela dokumentkroppen i
# ordning, så att ingen text faller bort oavsett struktur.
# ---------------------------------------------------------------------------

_BLOCKTAGGAR = {
    "article", "section", "ul", "ol", "li", "table", "thead", "tbody", "tfoot",
    "tr", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "caption",
}
_RUBRIKKLASSER = {"legalArticleHeader", "futureLegalArticleHeader"}


def _ren(text: str) -> str:
    """Slår ihop blanktecken; inline-element bär själva sina mellanslag."""
    return re.sub(r"\s+", " ", text).strip()


def _klasser(el) -> set[str]:
    return set(el.get("class") or [])


def _ar_block(el) -> bool:
    if el.name in _BLOCKTAGGAR:
        return True
    return el.name == "span" and bool(_klasser(el) & (_RUBRIKKLASSER | {"futuretitle"}))


def _inline_text(el) -> str:
    """Text ur ett inline-element. Fotnotsreferenser skrivs som [n]."""
    if el.name == "sup" and "footnotereference" in _klasser(el):
        return f" [{_ren(el.get_text())}]"
    if el.name == "br":
        return " "
    return el.get_text()


def _paragraf_rubrik(artikel) -> str:
    """'§ 21. Tittel' ur en legalArticle eller futureLegalArticle."""
    huvud = artikel.find(
        lambda t: bool(_klasser(t) & _RUBRIKKLASSER), recursive=False
    ) or artikel.find(lambda t: bool(_klasser(t) & _RUBRIKKLASSER))
    if huvud is None:
        return artikel.get("data-name", "")
    varde = huvud.find("span", class_="legalArticleValue")
    tittel = huvud.find("span", class_="legalArticleTitle")
    nr = _ren(varde.get_text()) if varde else artikel.get("data-name", "")
    if tittel and _ren(tittel.get_text()):
        return f"{nr}. {_ren(tittel.get_text())}"
    return nr or _ren(huvud.get_text())


def _block_till_md(el, linjer: list[str], i_endring: bool = False) -> None:
    """
    Skriver ett blockelement och dess innehåll som Markdown.

    Rubrikkonventionen följer resten av cachen: '## ' för kapitel/avsnitt
    (I, II, ...) och '### ' för paragrafer, så att nor_sok_i_dokument delar
    dokumentet på samma sätt som gjeldende-texterna. Inom en ändring blir
    ändringen själv ('§ 21 skal lyde:') avsnittsrubriken och den nya
    paragrafens rubrik skrivs i fetstil, så att varje ändring hamnar i ett
    eget avsnitt.
    """
    namn, klass = el.name, _klasser(el)

    if namn == "h1":
        return  # dokumenttiteln står redan överst
    if namn in ("h2", "h3", "h4", "h5", "h6"):
        linjer += ["", f"## {_ren(el.get_text())}", ""]
        return
    if namn == "span" and "futuretitle" in klass:
        linjer += ["", f"**{_ren(el.get_text())}**"]
        return
    if namn == "span" and klass & _RUBRIKKLASSER:
        return  # skrivs av paragrafen själv

    if namn == "article" and klass & {"legalArticle", "futureLegalArticle"}:
        rubrik = _paragraf_rubrik(el)
        if rubrik:
            linjer += (["", f"**{rubrik}**"] if i_endring else ["", f"### {rubrik}"])
        _barn_till_md(el, linjer, i_endring)
        return

    if namn == "article" and "change" in klass:
        # Första stycket ('§ 21 skal lyde:') blir rubrik för ändringen.
        forsta = el.find("article", class_="defaultP", recursive=False)
        rubrik = _ren(forsta.get_text()) if forsta else el.get("data-change-part", "")
        if rubrik:
            linjer += ["", f"### {rubrik}"]
        for barn in el.children:
            if barn is forsta:
                continue
            if getattr(barn, "name", None) and _ar_block(barn):
                _block_till_md(barn, linjer, i_endring=True)
            elif getattr(barn, "name", None):
                t = _ren(_inline_text(barn))
                if t:
                    linjer += ["", t]
            else:
                t = _ren(str(barn))
                if t:
                    linjer += ["", t]
        return

    if namn == "li":
        delar: list[str] = []
        _barn_till_md(el, delar, i_endring)
        text = " ".join(d.strip() for d in delar if d.strip())
        if text:
            etikett = el.get("data-name", "").rstrip(".")
            prefix = f"{etikett}. " if etikett and not text.startswith(etikett) else ""
            linjer.append(f"- {prefix}{text}")
        return

    if namn == "tr":
        celler = [_ren(c.get_text(" ")) for c in el.find_all(["td", "th"], recursive=False)]
        if any(celler):
            linjer.append("| " + " | ".join(celler) + " |")
        return

    if namn == "footer" and "footnotes" in klass:
        linjer += ["", "**Fotnoter**"]
        for fotnot in el.find_all("article", class_="footnote"):
            etikett = fotnot.find("span", class_="footnoteLabel")
            nr = _ren(etikett.get_text()) if etikett else ""
            if etikett:
                etikett.extract()
            linjer.append(f"[{nr}] {_ren(fotnot.get_text())}".strip())
        return

    if namn in ("ul", "ol", "table", "thead", "tbody", "tfoot"):
        linjer.append("")
    _barn_till_md(el, linjer, i_endring)


def _barn_till_md(el, linjer: list[str], i_endring: bool) -> None:
    """
    Går igenom ett elements barn i ordning. Löptext och inline-element
    samlas till ett stycke; ett blockbarn avslutar stycket och skrivs för sig.
    """
    stycke: list[str] = []

    def _avsluta_stycke() -> None:
        text = _ren("".join(stycke))
        if text:
            linjer.extend(["", text])
        stycke.clear()

    for barn in el.children:
        namn = getattr(barn, "name", None)
        if namn is None:
            stycke.append(str(barn))
        elif _ar_block(barn):
            _avsluta_stycke()
            _block_till_md(barn, linjer, i_endring)
        else:
            stycke.append(_inline_text(barn))
    _avsluta_stycke()


def _lovtidend_till_markdown(soup: BeautifulSoup) -> tuple[dict, str]:
    """
    Metadata och Markdown för ett dokument ur Lovtidend avd. I.

    Utöver de fält gjeldende-parsern ger bär metadata:
      endrer      — refid för de författningar dokumentet ändrar, t.ex.
                    ['lov/1999-07-02-64'] (changesToDocuments)
      ikraft      — ikraftträdandet som källan anger det, ofta fritext
                    ('2026-07-01', 'Kongen bestemmer', flera datum)
      kunngjort   — date-objekt för kungörandet i Lovtidend
    """
    lovdata_id  = _meta_varde(soup, "dokid")
    tittel      = _meta_varde(soup, "title")
    korttittel  = _meta_varde(soup, "titleShort")
    departement = _meta_varde(soup, "ministry")
    ikraft      = _meta_varde(soup, "dateInForce")
    kunngjort_t = _meta_varde(soup, "dateOfPublication")
    ovrigt      = _meta_varde(soup, "miscInformation")
    hjemmel     = [_text(li) for li in (soup.find("dd", class_="basedOn") or soup.new_tag("x")).find_all("li")]
    endrer      = [_text(li) for li in (soup.find("dd", class_="changesToDocuments") or soup.new_tag("x")).find_all("li")]

    metadata = {
        "lovdata_id":   lovdata_id,
        "beteckning":   _meta_varde(soup, "legacyID"),
        "tittel":       tittel,
        "korttittel":   korttittel,
        "departement":  departement,
        "dato_ikraft":  _parsera_datum(ikraft),
        "dato_vedtatt": _parsera_datum(kunngjort_t),
        "kunngjort":    _parsera_datum(kunngjort_t),
        "ikraft":       ikraft,
        "endrer":       endrer,
        "dok_type":     "lovtidend",
        # Lovtidend-dokumenten ligger under /dokument/LTI/... hos Lovdata.
        "url":          f"https://lovdata.no/dokument/{lovdata_id}" if lovdata_id else "",
        "hjemmel":      hjemmel,
    }

    linjer: list[str] = [f"# {tittel}"]
    if korttittel and korttittel != tittel:
        linjer.append(f"*{korttittel}*")
    linjer.append("")
    for etikett, varde in (
        ("Departement", departement),
        ("Kunngjort", kunngjort_t),
        ("I kraft", ikraft),
        ("Endrer", ", ".join(endrer)),
        ("Hjemmel", ", ".join(hjemmel)),
        ("Om dokumentet", ovrigt),
    ):
        if varde:
            linjer.append(f"**{etikett}:** {varde}")
    linjer += ["", "---"]

    main = soup.find("main")
    if main is not None:
        _barn_till_md(main, linjer, i_endring=False)

    # Tomrader från blockgränserna slås ihop till en.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(linjer)).strip() + "\n"
    return metadata, text


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

def _hamta_status(kilde_id: str) -> dict:
    """Lagrad synkstatus för källan (checksum, detaljer); tom dict om saknas."""
    try:
        from db import get_sync_status
        return get_sync_status(kilde_id)
    except Exception as exc:
        log.warning("Kunde inte läsa sync_status (%s): %s", kilde_id, exc)
        return {}


def _uppdatera_sync_status(kilde_id: str, checksum: str, detaljer: dict) -> None:
    """Uppdaterar eller skapar en rad i sync_status."""
    try:
        from db import set_sync_status
        set_sync_status(kilde_id, checksum=checksum, detaljer=detaljer)
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status (%s): %s", kilde_id, exc)


def upsert_lovdata_dokument(meta: dict, fulltext_md: str) -> None:
    """Spar ett Lovdata-dokument i norge.dokument via db.upsert_dokument."""
    from db import upsert_dokument
    lovtidend = meta["dok_type"] == "lovtidend"
    upsert_dokument(
        kilde      = "lovdata",
        dok_type   = meta["dok_type"],
        beteckning = meta["beteckning"],
        tittel     = meta["tittel"],
        sesjonid   = None,
        # Ett Lovtidend-dokument dateras efter kungörandet, eftersom det är
        # den tidpunkt Lovtidend dokumenterar. Ikraftträdandet står i ikraft.
        dato       = meta["kunngjort"] if lovtidend else meta["dato_ikraft"],
        url        = meta["url"],
        publikasjonid = None,
        sakid      = None,
        lovdata_id = meta["lovdata_id"],
        fulltext_md = fulltext_md,
        endrer     = " ".join(meta["endrer"]) if lovtidend else None,
        ikraft     = meta["ikraft"] if lovtidend else None,
    )


# ---------------------------------------------------------------------------
# Huvudflöde per källa
# ---------------------------------------------------------------------------

def synka_kalla(
    kalla_nyckel: str,
    tvinga: bool = False,
    tillgangliga_paket: Optional[dict] = None,
) -> int:
    """
    Synkar en Lovdata-källa. Returnerar antal behandlade dokument.

    kalla_nyckel       — nyckel i _KALLOR ('lover', 'forskrifter',
                         'lovtidend', 'lovtidend-historisk')
    tvinga             — om True laddas paketet ned och parsas om oavsett
                         lastModified och checksum
    tillgangliga_paket — svaret från hamta_lovdata_paket(); None om listan
                         inte gick att hämta
    """
    kalla    = _KALLOR[kalla_nyckel]
    mapp     = kalla["mapp"]
    dok_typ  = kalla["dok_typ"]
    kilde_id = kalla["kilde_id"]

    paket_namn = valj_paket(kalla_nyckel, tillgangliga_paket or {})
    if paket_namn is None:
        log.error(
            "%s: inget paket som matchar %s i Lovdatas lista — hoppar",
            kalla_nyckel, kalla.get("paket_monster"),
        )
        return 0
    if tillgangliga_paket and paket_namn not in tillgangliga_paket:
        log.error(
            "%s: paketet '%s' finns inte i Lovdatas lista (%s) — hoppar",
            kalla_nyckel, paket_namn, sorted(tillgangliga_paket),
        )
        return 0

    # lastModified i listan gör att ett oförändrat paket kan hoppas över
    # utan nedladdning. Det spelar roll för det historiska Lovtidend-paketet
    # (~70 MB), som sällan ändras.
    andrad = (tillgangliga_paket or {}).get(paket_namn, {}).get("andrad", "")
    status = _hamta_status(kilde_id)
    tidigare = status.get("detaljer") or {}
    if (not tvinga and andrad and tidigare.get("paket") == paket_namn
            and tidigare.get("andrad") == andrad):
        log.info("%s: %s oförändrat sedan %s — hoppar", kalla_nyckel, paket_namn, andrad)
        return 0

    url = f"{_LOVDATA_GET_BASE}/{paket_namn}.tar.bz2"

    log_dir = _SCRIPT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Ladda ner till temporär fil
    tmp = Path(tempfile.mktemp(suffix=".tar.bz2", dir=log_dir))
    try:
        ny_checksum = ladda_ned_tarball(url, tmp)
        detaljer = {"paket": paket_namn, "andrad": andrad}

        # Kontrollera om tarball ändrats
        if not tvinga and status.get("checksum") == ny_checksum:
            log.info("%s: oförändrad (checksum match) — hoppar parsning", kalla_nyckel)
            _uppdatera_sync_status(kilde_id, ny_checksum, {
                **detaljer, "antal_dokument": tidigare.get("antal_dokument"),
            })
            return 0

        log.info("%s: parsar %s...", kalla_nyckel, paket_namn)
        antal_ok = 0
        antal_fel = 0

        # Iterera arkivet i ordning i stället för via getmembers(): ett
        # bz2-arkiv måste annars packas upp två gånger, först för
        # medlemslistan och sedan för innehållet.
        with tarfile.open(tmp, "r:bz2") as tf:
            for medlem in tf:
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

                    if antal_ok % 1000 == 0:
                        log.info("  %d behandlade...", antal_ok)

                except Exception as exc:
                    antal_fel += 1
                    log.warning("Fel vid parsning av %s: %s", namn, exc)

        _uppdatera_sync_status(kilde_id, ny_checksum, {**detaljer, "antal_dokument": antal_ok})
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
        choices=[*_KALLOR, "alla"],
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

    tillgangliga_paket = hamta_lovdata_paket()
    if tillgangliga_paket:
        log.info("Tillgängliga Lovdata-paket: %s", sorted(tillgangliga_paket))
    else:
        log.warning(
            "Paketlistan saknas — gjeldende-paketen synkas med kända namn, "
            "Lovtidend hoppas över"
        )

    kallor = list(_KALLOR) if args.kalla == "alla" else [args.kalla]
    totalt = 0
    for k in kallor:
        totalt += synka_kalla(k, tvinga=args.tvinga, tillgangliga_paket=tillgangliga_paket)

    log.info("Synk klar. Totalt behandlade dokument: %d", totalt)


if __name__ == "__main__":
    main()
