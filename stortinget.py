# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
stortinget.py — Klient mot Stortingets öppna data-API och XML-parser

Stortingets API returnerar XML för ALLA endpoints — metadata såväl som fulltext.
Det finns inget JSON-läge.

Två XML-dialekter används:

  1. METADATA-XML (sesjoner, saker, spørsmål, høringer m.fl.)
     Namespace: http://data.stortinget.no
     Bas-URL:   https://data.stortinget.no/eksport/
     Session-ID format: "2024-2025" (fyrsiffrigt från och med 2021, "1986-87" äldre)

  2. FULLTEXT-XML (publikasjoner — innstillinger, referater, lovvedtak m.fl.)
     Inget namespace. Hämtas via: /eksport/publikasjon?publikasjonid=ID
     Två DTD-varianter beroende på dokumenttyp och ålder:
       - 2016-17+, referater:  forhandlinger.dtd  → rot: <Forhandling>
       - 2016-17+, övriga:     innstillinger.dtd  → rot: <Innstilling>, <Lovvedtak>, ...
       - Pre-2016-17:          äldre elementstruktur

Takbegränsning: 100 anrop/minut (respekteras automatiskt via token-bucket).

API-dokumentation: https://data.stortinget.no/dokumentasjon
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from lxml import etree

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE    = os.getenv("STORTINGET_API_BASE", "https://data.stortinget.no/eksport")
RATE_LIMIT  = int(os.getenv("STORTINGET_RATE_LIMIT", 100))   # anrop/minut

# Namespace för metadata-XML
NS = "http://data.stortinget.no"
NP = f"{{{NS}}}"   # prefix-sträng: {http://data.stortinget.no}

# Token-bucket för rate-limiting
_bucket_tokens  = float(RATE_LIMIT)
_bucket_last_ts = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP-primitiver
# ---------------------------------------------------------------------------

def _throttle():
    global _bucket_tokens, _bucket_last_ts
    now     = time.monotonic()
    elapsed = now - _bucket_last_ts
    _bucket_tokens   = min(RATE_LIMIT, _bucket_tokens + elapsed * (RATE_LIMIT / 60.0))
    _bucket_last_ts  = now
    if _bucket_tokens < 1:
        sleep_s = (1 - _bucket_tokens) / (RATE_LIMIT / 60.0)
        log.debug("Rate-limit nådd, väntar %.1f s", sleep_s)
        time.sleep(sleep_s)
        _bucket_tokens = 0.0
    else:
        _bucket_tokens -= 1.0


def _get_xml_root(path: str, params: Optional[dict] = None) -> etree._Element:
    """GET mot API_BASE/{path} — returnerar lxml-rot."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.get(url, params=params or {}, timeout=60)
    r.raise_for_status()
    parser = etree.XMLParser(recover=True, load_dtd=False, no_network=True)
    return etree.fromstring(r.content, parser=parser)


def _get_xml_text(path: str, params: Optional[dict] = None) -> str:
    """GET mot API_BASE/{path} — returnerar råa XML-bytes som sträng (för fulltext)."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.get(url, params=params or {}, timeout=60)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# XML-hjälpfunktioner för metadata-XML (med namespace)
# ---------------------------------------------------------------------------

def _xt(elem: etree._Element, tag: str, default: str = "") -> str:
    """Returnerar textinnehållet i ett direkt barnelement (med NS)."""
    child = elem.find(f"{NP}{tag}")
    return (child.text or "").strip() if child is not None else default


def _xta(elem: etree._Element, tag: str) -> list[etree._Element]:
    """Returnerar alla direkta barnelement med det angivna tagnamnet (med NS)."""
    return elem.findall(f"{NP}{tag}")


def _xfind(elem: etree._Element, *path: str) -> Optional[etree._Element]:
    """Traverserar en sökväg av taggar (med NS)."""
    current = elem
    for tag in path:
        if current is None:
            return None
        current = current.find(f"{NP}{tag}")
    return current


# ---------------------------------------------------------------------------
# XML-hjälpfunktioner för fulltext-XML (utan namespace)
# ---------------------------------------------------------------------------

def _ft(elem: etree._Element, tag: str, default: str = "") -> str:
    """Returnerar textinnehållet i ett direkt barnelement (utan NS)."""
    child = elem.find(tag)
    return (child.text or "").strip() if child is not None else default


def _all_text(elem: etree._Element) -> str:
    """Extraherar all text (text + tail) rekursivt ur ett element."""
    parts = []
    if elem.text:
        t = elem.text.strip()
        if t:
            parts.append(t)
    for child in elem:
        child_text = _all_text(child)
        if child_text:
            parts.append(child_text)
        if child.tail:
            t = child.tail.strip()
            if t:
                parts.append(t)
    return " ".join(parts)


def _lokal_tag(elem: etree._Element) -> str:
    tag = elem.tag if isinstance(elem.tag, str) else ""
    return tag.split("}")[-1].lower() if "}" in tag else tag.lower()


# ---------------------------------------------------------------------------
# Sesjoner
# ---------------------------------------------------------------------------

def hamta_sesjoner() -> list[dict]:
    """
    Hämtar alla tillgängliga sessioner (1986-87 och framåt).
    Returnerar lista sorterad äldst-först med nycklarna: id, fra, til.
    """
    root     = _get_xml_root("sesjoner")
    innevaer = _xfind(root, "innevaerende_sesjon")
    innevaer_id = _xt(innevaer, "id") if innevaer is not None else ""

    s_liste  = root.find(f"{NP}sesjoner_liste")
    sesjoner = []
    if s_liste is not None:
        for s in s_liste.findall(f"{NP}sesjon"):
            sesjoner.append({
                "id":  _xt(s, "id"),
                "fra": _xt(s, "fra")[:10],   # ISO-datum
                "til": _xt(s, "til")[:10],
            })

    # API returnerar nyast-först — vänd för konsistent äldst-först
    sesjoner.reverse()

    return {
        "innevaerende": innevaer_id,
        "sesjoner":     sesjoner,
        "antal":        len(sesjoner),
    }


def aktuell_sesjonid(sesjoner_resp: Optional[dict] = None) -> str:
    """Returnerar ID för innevarande session (t.ex. '2025-2026')."""
    if sesjoner_resp is None:
        sesjoner_resp = hamta_sesjoner()
    return sesjoner_resp.get("innevaerende", "") or (
        sesjoner_resp["sesjoner"][-1]["id"] if sesjoner_resp.get("sesjoner") else "2025-2026"
    )


# ---------------------------------------------------------------------------
# Saker (ärenden)
# ---------------------------------------------------------------------------

def hamta_sak(sakid: str) -> dict:
    """Hämtar metadata för en enstaka sak inkl. publikasjoner."""
    root = _get_xml_root("sak", {"sakid": sakid})
    sak  = root.find(f"{NP}sak") or root
    return _normalisera_sak(sak, sakid)


def hamta_saker(sesjonid: str) -> list[dict]:
    """Hämtar alla saker för en session (~700 st)."""
    root    = _get_xml_root("saker", {"sesjonid": sesjonid})
    s_liste = root.find(f"{NP}saker_liste")
    if s_liste is None:
        return []
    return [_normalisera_sak(s) for s in s_liste.findall(f"{NP}sak")]


def _normalisera_sak(sak: etree._Element, fallback_id: str = "") -> dict:
    sakid    = _xt(sak, "id") or fallback_id
    tittel   = _xt(sak, "tittel") or _xt(sak, "korttittel")
    sesjonid = _xt(sak, "behandlet_sesjon_id") or _xt(sak, "sesjon_id")

    # innstillingstekst — kortfattad resumé av komiteens innstilling
    innstillingstekst = _xt(sak, "innstillingstekst")

    # Stikkord
    stikkord = []
    stikkord_liste = sak.find(f"{NP}stikkord_liste")
    if stikkord_liste is not None:
        for s in stikkord_liste.findall(f"{NP}stikkord"):
            tekst = _xt(s, "navn") or (s.text or "").strip()
            if tekst:
                stikkord.append(tekst)

    # Emner (ämnesord)
    emner = []
    emne_liste = sak.find(f"{NP}emne_liste")
    if emne_liste is not None:
        for emne in emne_liste.findall(f"{NP}emne"):
            navn = _xt(emne, "navn")
            if navn:
                emner.append(navn)

    # Publikasjon-referanser
    pub_refs = []
    pub_liste = sak.find(f"{NP}publikasjon_referanse_liste")
    if pub_liste is not None:
        for pub in pub_liste.findall(f"{NP}publikasjon_referanse"):
            eksport_id  = _xt(pub, "eksport_id")
            lenke_url   = _xt(pub, "lenke_url")
            lenke_tekst = _xt(pub, "lenke_tekst")
            pub_type    = _xt(pub, "type")
            pub_refs.append({
                "eksport_id":  eksport_id,
                "lenke_url":   lenke_url,
                "lenke_tekst": lenke_tekst,
                "type":        pub_type,
            })

    return {
        "sakid":             sakid,
        "tittel":            tittel,
        "sesjonid":          sesjonid,
        "dokumentgruppe":    _xt(sak, "dokumentgruppe"),
        "sak_status":        _xt(sak, "status"),
        "sist_oppdatert":    _xt(sak, "sist_oppdatert_dato")[:10],
        "innstillingstekst": innstillingstekst,
        "stikkord":          stikkord,
        "emner":             emner,
        "publikasjoner":     pub_refs,
    }


def sok_saker(
    fraga: str,
    sesjonid: str,
    dokumentgruppe: str = "",
    emne: str = "",
    sak_status: str = "",
) -> list[dict]:
    """
    Klient-sidan sökning i saker för en session.

    Söker i tittel, emner, stikkord och innstillingstekst.

    Parametrar:
      fraga          — Söktermer (kommaseparerade = OR-logik). Tom = alla.
      sesjonid       — Stortings-session (t.ex. '2024-2025').
      dokumentgruppe — Filtrera på dokumentgruppe (t.ex. 'lovsak', 'stmeld').
                       Partiell matchning, case-insensitivt. Tom = ingen filter.
      emne           — Filtrera på ämnesord (partiell matchning). Tom = ingen filter.
      sak_status     — Filtrera på sakens status (t.ex. 'mottatt', 'behandlet').
                       Tom = ingen filter.
    """
    saker  = hamta_saker(sesjonid)
    termer = _split_termer(fraga)

    resultat = []
    for s in saker:
        # Dokumentgruppe-filter
        if dokumentgruppe:
            dg = (s.get("dokumentgruppe") or "").lower()
            if dokumentgruppe.lower() not in dg:
                continue

        # Emne-filter
        if emne:
            emner_tekst = " ".join(s.get("emner", [])).lower()
            if emne.lower() not in emner_tekst:
                continue

        # Status-filter
        if sak_status:
            status = (s.get("sak_status") or "").lower()
            if sak_status.lower() not in status:
                continue

        # Tekstsökning (hoppa om inga termer)
        if termer:
            sok_tekst = (
                (s["tittel"] or "")
                + " " + " ".join(s.get("emner", []))
                + " " + " ".join(s.get("stikkord", []))
                + " " + (s.get("innstillingstekst") or "")
            )
            if not _matcher(termer, sok_tekst):
                continue

        resultat.append(s)

    return resultat


# ---------------------------------------------------------------------------
# Spørsmål
# ---------------------------------------------------------------------------

def hamta_skriftlige_sporsmal(sesjonid: str) -> list[dict]:
    """Hämtar skriftliga frågor för en session."""
    root    = _get_xml_root("skriftligesporsmal", {"sesjonid": sesjonid})
    s_liste = root.find(f"{NP}sporsmal_liste")
    if s_liste is None:
        return []
    result = []
    for item in s_liste:
        result.append({
            "id":       _xt(item, "id"),
            "typ":      "skriftlig",
            "tittel":   _xt(item, "tittel"),
            "dato":     _xt(item, "datert_dato")[:10],
            "til":      _xt(item, "sporsmal_til_minister_tittel"),
            "sesjonid": _xt(item, "sesjon_id"),
            "status":   _xt(item, "status"),
        })
    return result


def hamta_sporretimesporsmal(sesjonid: str) -> list[dict]:
    """Hämtar spørretimespørsmål (muntliga frågor) för en session."""
    root    = _get_xml_root("sporretimesporsmal", {"sesjonid": sesjonid})
    # Prova olika listtaggar
    s_liste = (root.find(f"{NP}sporretime_sporsmal_liste")
               or root.find(f"{NP}sporsmal_liste"))
    if s_liste is None:
        return []
    result = []
    for item in s_liste:
        result.append({
            "id":       _xt(item, "id"),
            "typ":      "sporretimen",
            "tittel":   _xt(item, "tittel") or _xt(item, "tema"),
            "dato":     _xt(item, "dato")[:10] if _xt(item, "dato") else "",
            "til":      _xt(item, "til_minister_tittel") or _xt(item, "sporsmal_til_minister_tittel"),
            "sesjonid": _xt(item, "sesjon_id"),
        })
    return result


def sok_sporsmal(fraga: str, sesjonid: str) -> list[dict]:
    """Klient-sidan sökning i spørsmål för en session. Tom fraga = returnera alla."""
    alle   = hamta_skriftlige_sporsmal(sesjonid) + hamta_sporretimesporsmal(sesjonid)
    termer = _split_termer(fraga)
    if not termer:
        return alle
    return [s for s in alle if _matcher(termer, s["tittel"] or "")]


# ---------------------------------------------------------------------------
# Høringer
# ---------------------------------------------------------------------------

def hamta_horinger(sesjonid: str) -> list[dict]:
    """Hämtar høringer (utskottsutfrågningar) för en session."""
    root    = _get_xml_root("horinger", {"sesjonid": sesjonid})
    h_liste = root.find(f"{NP}horinger_liste")
    if h_liste is None:
        return []
    result = []
    for h in h_liste:
        komite_elem = h.find(f"{NP}komite")
        komite_navn = _xt(komite_elem, "navn") if komite_elem is not None else ""

        # Prova att hämta tittel från horing_sak_info_liste
        tittel = ""
        sak_liste = h.find(f"{NP}horing_sak_info_liste")
        if sak_liste is not None:
            first_sak = next(iter(sak_liste), None)
            if first_sak is not None:
                tittel = _xt(first_sak, "sak_tittel") or _xt(first_sak, "sak_korttittel")

        start = _xt(h, "start_dato")[:10] if _xt(h, "start_dato") != "0001-01-01T00:00:00Z" else ""

        result.append({
            "id":          _xt(h, "id"),
            "tittel":      tittel or f"Høring ({komite_navn})" if komite_navn else "Høring",
            "komite":      komite_navn,
            "dato":        start,
            "status":      _xt(h, "horing_status"),
            "sesjonid":    sesjonid,
        })
    return result


def sok_horinger(fraga: str, sesjonid: str) -> list[dict]:
    """Klient-sidan sökning i høringer för en session. Tom fraga = returnera alla."""
    alle   = hamta_horinger(sesjonid)
    termer = _split_termer(fraga)
    if not termer:
        return alle
    return [h for h in alle if _matcher(termer, (h["tittel"] or "") + " " + (h["komite"] or ""))]


# ---------------------------------------------------------------------------
# Sökhjiälpfunktioner
# ---------------------------------------------------------------------------

def _split_termer(fraga: str) -> list[str]:
    """Splittar frågan på komman och mellanslag → lista av lowercase-termer."""
    return [t.strip().lower() for t in re.split(r"[,\s]+", fraga) if t.strip()]


def _matcher(termer: list[str], haystack: str) -> bool:
    """Returnerar True om NÅGON term finns i haystack (OR-logik)."""
    h = haystack.lower()
    return any(t in h for t in termer)


# ---------------------------------------------------------------------------
# Publikasjoner — fulltext (XML utan namespace)
# ---------------------------------------------------------------------------

def hamta_publikasjon_xml(eksport_id: str) -> str:
    """Hämtar en publikasjon som råa XML-bytes."""
    return _get_xml_text("publikasjon", {"publikasjonid": eksport_id})


def parse_publikasjon_xml(xml_text: str, sesjonid: str = "") -> dict:
    """
    Parsar en Stortingets publikasjons-XML till strukturerad text.

    Hanterar BÅDA formaten:
      - 2016-17+, referater:  <Forhandling> / forhandlinger.dtd
      - 2016-17+, övriga:     <Innstilling>, <Lovvedtak> m.fl. / innstillinger.dtd
      - Pre-2016-17:          äldre elementstruktur

    Fulltext-XML använder INGET namespace.

    Returnerar dict med:
      tittel      — dokumentets titel
      typ         — detekterat format ('innstilling_ny', 'referat_ny', 'aldre', 'generisk')
      fulltext_md — extraherad text i läsbart Markdown-liknande format
      metadata    — övriga metadata-fält
    """
    # Ta bort DOCTYPE-deklaration (refererar externt DTD som vi inte hämtar)
    xml_clean = re.sub(r"<!DOCTYPE[^>]+?>", "", xml_text, flags=re.DOTALL)

    parser = etree.XMLParser(recover=True, load_dtd=False, no_network=True)
    try:
        root = etree.fromstring(xml_clean.encode("utf-8"), parser=parser)
    except Exception as exc:
        log.warning("XML-parsning misslyckades: %s", exc)
        return {
            "tittel":      "Parsningsfel",
            "typ":         "fel",
            "fulltext_md": xml_text[:2000],
            "metadata":    {"fel": str(exc)},
        }

    rot_tag      = _lokal_tag(root)
    sesjon_ar    = _sesjon_till_ar(sesjonid)

    # Referater (forhandlinger.dtd): rot-tagg innehåller "forhandling"
    if "forhandling" in rot_tag:
        return _parse_referat(root, sesjonid, sesjon_ar)

    # Innstillinger och övriga (innstillinger.dtd): rot-tagg är t.ex. "innstilling"
    # Äldre sesjoner: liknande struktur men med avvikande taggar
    return _parse_innstilling(root, sesjonid, sesjon_ar)


def _sesjon_till_ar(sesjonid: str) -> int:
    """Konverterar '2024-2025' eller '2016-17' → startår som int, '' → 0."""
    if not sesjonid:
        return 0
    m = re.match(r"(\d{4})", sesjonid)
    return int(m.group(1)) if m else 0


# -----------
# Parser: Innstillinger (innstillinger.dtd, ny och äldre)
# -----------

def _parse_innstilling(root: etree._Element, sesjonid: str, sesjon_ar: int) -> dict:
    """
    Parser för innstillinger och övriga ikke-referat-dokumenter.

    Ny struktur (2016-17+) observerad i verkligheten:
      <Innstilling Status="Komplett">
        <Startseksjon>
          <Navn>, <Aar>, <Doktit>, <Kildedok>, <Ingress>
        </Startseksjon>
        <Hovedseksjon>
          <Kapittel Num="Ja">
            <Tittel>
            <Seksjon2>
              <Tittel>
              <A Type="...">  ← brödtext
              <Liste>, <Tbl>
            </Seksjon2>
          </Kapittel>
        </Hovedseksjon>
      </Innstilling>
    """
    linjer = []

    # Titel från Startseksjon
    start  = root.find("Startseksjon")
    tittel = ""
    if start is not None:
        navn    = _ft(start, "Navn")
        aar     = _ft(start, "Aar")
        doktit  = _ft(start, "Doktit")
        ingress = _ft(start, "Ingress")
        kilderef = _ft(start, "Kildedok")

        tittel = f"{navn} {aar}".strip() if navn else doktit
        if tittel:
            linjer.append(f"# {tittel}")
        if doktit and doktit != tittel:
            linjer.append(f"**{doktit}**")
        if kilderef:
            linjer.append(f"*{kilderef}*")
        if ingress:
            linjer.append(f"\n{ingress}\n")
    else:
        # Äldre format — prova generiska titteltaggar
        tittel = _first_text(root, "Tittel", "tittel", "titel")
        if tittel:
            linjer.append(f"# {tittel}")

    # Hauptsektioner
    for elem in root.iter():
        tag = _lokal_tag(elem)

        if tag == "kapittel":
            kap_tittel = elem.findtext("Tittel") or elem.findtext("tittel") or ""
            if kap_tittel:
                linjer.append(f"\n## {kap_tittel.strip()}")

        elif tag == "seksjon2":
            sek_tittel = elem.findtext("Tittel") or elem.findtext("tittel") or ""
            if sek_tittel:
                linjer.append(f"\n### {sek_tittel.strip()}")

        elif tag == "a":
            # Brödtext
            tekst = _all_text(elem)
            if tekst and len(tekst) > 10:
                linjer.append(tekst)

        elif tag in ("merknad", "tilrading", "vedtak", "lovtekst", "lovdata"):
            # Rekursivt extrahera avsnitt
            for a in elem.iter("A"):
                tekst = _all_text(a)
                if tekst and len(tekst) > 10:
                    linjer.append(tekst)

    fulltext = "\n".join(linjer)
    if len(fulltext.strip()) < 200:
        # Generisk fallback
        return _parse_generisk(root, "innstilling_generisk")

    typ = "innstilling_ny" if sesjon_ar >= 2016 else "innstilling_aldre"
    return {
        "tittel":      tittel or _lokal_tag(root),
        "typ":         typ,
        "fulltext_md": fulltext,
        "metadata":    {"sesjonid": sesjonid},
    }


# -----------
# Parser: Referater (forhandlinger.dtd, ny och äldre)
# -----------

def _parse_referat(root: etree._Element, sesjonid: str, sesjon_ar: int) -> dict:
    """
    Parser för stortingsreferater (forhandlinger.dtd).

    Faktisk struktur (2016-17+, verifierat mot live-API):
      <Forhandlinger>
        <Mote>
          <Startseksjon>
            <Tittel>
            <President><A>...</A></President>
            <Dagsorden>...</Dagsorden>
            <Presinnlegg>
              <Navn>Presidenten [10:00:27]:</Navn> taltext...
              <A Type="...">...</A>
            </Presinnlegg>
          </Startseksjon>
          <Saker>
            <Sak>
              <Sakshode><Saktittel>...</Saktittel></Sakshode>
              <Presinnlegg>   ← debatt/innlegg
                <Navn>TalerNamn [hh:mm:ss]:</Navn> inledningstext...
                <A>...</A>
              </Presinnlegg>
              <Sakdel>...</Sakdel>
            </Sak>
          </Sakers>
          <Sluttseksjon>...</Sluttseksjon>
        </Mote>
      </Forhandlinger>

    Talarmarkören är <Navn> — texten innehåller namn och tidsstämpel.
    Brödtexten direkt efter <Navn> ligger i tail; efterföljande <A>-element
    är fortsättningen av samma inlägg.
    """
    linjer = []

    # Titel och mötesinledning
    mote = root.find(".//Mote")
    if mote is None:
        mote = root
    start = mote.find("Startseksjon")
    if start is None:
        start = root.find("Startseksjon")
    tittel = ""
    if start is not None:
        tittel = _first_text(start, "Tittel", "tittel")
    if not tittel:
        tittel = _first_text(root, "Tittel", "tittel")

    if tittel:
        linjer.append(f"# {tittel}\n")

    # Saker — finns under Mote/Hovedseksjon/Saker (använd .// för robusthet)
    saker_elem = root.find(".//Saker")
    if saker_elem is None:
        return _parse_generisk(root, "referat_generisk")

    for sak in saker_elem.findall("Sak"):
        sh      = sak.find("Sakshode")
        sak_tit = _first_text(sh, "Saktittel", "saktittel") if sh is not None else ""
        if sak_tit:
            linjer.append(f"\n## {sak_tit.strip()}")

        # Travers ALL text i saken via <A>-element med talarbyte-markering.
        # <Navn> finns INNE I <A>: <A><Navn>Taler [hh:mm:ss]:</Navn>tail</A>
        for a_elem in sak.iter("A"):
            navn_elem = a_elem.find("Navn")
            if navn_elem is not None:
                namn_rå = (navn_elem.text or "").strip()
                taler   = re.sub(r"\s*\[\d{2}:\d{2}:\d{2}\]", "", namn_rå).rstrip(":").strip()
                tail    = (navn_elem.tail or "").strip()
                if taler:
                    linjer.append(f"\n**{taler}:** {tail}")
            else:
                tekst = _all_text(a_elem)
                if tekst and len(tekst) > 5:
                    linjer.append(tekst)

    fulltext = "\n".join(linjer)
    if len(fulltext.strip()) < 200:
        return _parse_generisk(root, "referat_generisk")

    typ = "referat_ny" if sesjon_ar >= 2016 else "referat_aldre"
    return {
        "tittel":      tittel or "Stortingsreferat",
        "typ":         typ,
        "fulltext_md": fulltext,
        "metadata":    {"sesjonid": sesjonid},
    }


def _ekstrahera_innlegg(seksjon: etree._Element, linjer: list):
    """
    Extraherar taleinlägg från en sektionselement.

    Faktisk struktur i referater (verifierat):
      <Presinnlegg> eller <Hoofdinnlegg>
        <A><Navn>Talernavn [hh:mm:ss]:</Navn>text direkt efter Navn i tail</A>
        <A>fortsättning av samma inlägg</A>
        <A><Navn>Neste talar [hh:mm:ss]:</Navn>...</A>

    <Navn> ligger INUTI <A>, inte som direkt barn av sektionen.
    Talarbyte identifieras av att <A> innehåller ett <Navn>-element.
    """
    gjeldende_taler: str = ""
    innlegg_deler: list[str] = []

    def _flush():
        if not innlegg_deler:
            return
        tekst = " ".join(innlegg_deler).strip()
        if not tekst:
            return
        if gjeldende_taler:
            linjer.append(f"\n**{gjeldende_taler}:**")
        linjer.append(tekst)
        innlegg_deler.clear()

    for a_elem in seksjon.iter("A"):
        navn_elem = a_elem.find("Navn")

        if navn_elem is not None:
            # Nytt inlägg — spara föregående
            _flush()
            navn_tekst = (navn_elem.text or "").strip()
            # Ta bort tidsstämpel [hh:mm:ss]
            gjeldende_taler = re.sub(r"\s*\[\d{2}:\d{2}:\d{2}\]", "", navn_tekst).rstrip(":")
            # Tail av <Navn> = inledningsorden för det nya inlägget
            tail = (navn_elem.tail or "").strip()
            if tail:
                innlegg_deler.append(tail)
        else:
            # Fortsättning av pågående inlägg
            tekst = _all_text(a_elem)
            if tekst and len(tekst) > 3:
                innlegg_deler.append(tekst)

    _flush()


# -----------
# Parser: Generisk fallback
# -----------

def _parse_generisk(root: etree._Element, typ_etikett: str) -> dict:
    """Extraherar all text ur ett XML-dokument utan strukturkunskap."""
    tittel   = _first_text(root, "Tittel", "tittel", "SakTittel", "Navn")
    all_text = _all_text(root)
    return {
        "tittel":      tittel or "Stortingsdokument",
        "typ":         typ_etikett,
        "fulltext_md": all_text,
        "metadata":    {},
    }


def _first_text(elem: etree._Element, *taggar: str) -> str:
    """Söker igenom elementets descendants efter den första taggen som ger text."""
    for tag in taggar:
        for child in elem.iter(tag):
            tekst = (child.text or "").strip()
            if tekst:
                return tekst
    return ""


# ---------------------------------------------------------------------------
# Hämta och parsa ett dokument i ett steg
# ---------------------------------------------------------------------------

def hamta_og_parse_publikasjon(eksport_id: str, sesjonid: str = "") -> dict:
    """
    Hämtar XML från Stortingets API och parsar till strukturerad text.
    Returnerar samma dict-format som parse_publikasjon_xml.
    """
    xml_text = hamta_publikasjon_xml(eksport_id)
    return parse_publikasjon_xml(xml_text, sesjonid)


# ---------------------------------------------------------------------------
# Vedtak — parlamentariska beslut
# ---------------------------------------------------------------------------

def hamta_vedtak_liste(sesjonid: str) -> list[dict]:
    """
    Hämtar lista med stortingsvedtak för en session.

    Returnerar lista med dicts: id, nummer, sak_id, sesjonid, dato, tittel, vedtakstekst.
    vedtakstekst är beslutstexten (kan vara HTML-formaterad).
    För fulltext, använd hamta_vedtak_fulltext(vedtakid).
    """
    root    = _get_xml_root("stortingsvedtak", {"sesjonid": sesjonid})
    v_liste = root.find(f"{NP}stortingsvedtak_liste")
    if v_liste is None:
        log.warning("Ingen stortingsvedtak_liste i svar från /stortingsvedtak?sesjonid=%s", sesjonid)
        return []

    result = []
    for v in v_liste.findall(f"{NP}stortingsvedtak"):
        dato_raa = _xt(v, "dato_tid")
        result.append({
            "id":           _xt(v, "id"),
            "nummer":       _xt(v, "nummer"),
            "sak_id":       _xt(v, "sak_id"),
            "sesjonid":     _xt(v, "sesjon_id") or sesjonid,
            "dato":         dato_raa[:10] if dato_raa and dato_raa != "0001-01-01T00:00:00Z" else "",
            "tittel":       _xt(v, "stortingsvedtak_tittel"),
            "vedtakstekst": _xt(v, "tekst"),
        })
    return result


def hamta_vedtak_fulltext(vedtakid: str) -> dict:
    """
    Hämtar fulltext för ett enstaka stortingsvedtak.

    Returnerar dict med: id, tittel, fulltext_md, url.
    fulltext_md är extraherad beslutstext (kan vara HTML med beslutsspråk).
    """
    _throttle()
    url = f"{API_BASE}/stortingsvedtak"
    r   = httpx.get(url, params={"vedtakid": vedtakid}, timeout=60)
    r.raise_for_status()

    content_type = r.headers.get("content-type", "").lower()

    if "xml" in content_type:
        parser = etree.XMLParser(recover=True, load_dtd=False, no_network=True)
        root = etree.fromstring(r.content, parser=parser)
        vedtak_el = root.find(f"{NP}stortingsvedtak") or root
        vedtakstekst = _xt(vedtak_el, "tekst") or _all_text(vedtak_el)
        tittel       = _xt(vedtak_el, "stortingsvedtak_tittel")
        fulltext_md  = vedtakstekst
    else:
        # HTML-format (alternativt returformat)
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            # Ta bort skript och stilar
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            tittel      = (soup.find("h1") or soup.find("title") or soup.find("h2"))
            tittel      = tittel.get_text(strip=True) if tittel else ""
            fulltext_md = soup.get_text(separator="\n", strip=True)
        except Exception as exc:
            log.warning("HTML-parsning av vedtak misslyckades: %s", exc)
            tittel, fulltext_md = "", r.text[:5000]

    return {
        "id":          vedtakid,
        "tittel":      tittel,
        "fulltext_md": fulltext_md,
        "url":         f"{API_BASE}/vedtak?vedtakid={vedtakid}",
    }


# ---------------------------------------------------------------------------
# Skriftlige innspill til høringer
# ---------------------------------------------------------------------------

def hamta_skriftlige_innspill(horingid: str) -> list[dict]:
    """
    Hämtar lista med skriftliga innspill (remissvar) till en høring.

    Returnerar lista med dicts: id, tittel, avsender, dato, ingress, eksport_id.
    För fulltext, hämta via hamta_og_parse_publikasjon(eksport_id) om eksport_id finns.
    """
    root    = _get_xml_root("horingsinnspill", {"horingid": horingid})
    i_liste = root.find(f"{NP}horingsinnspill_liste")
    if i_liste is None:
        log.warning("Ingen horingsinnspill_liste i svar från /horingsinnspill?horingid=%s", horingid)
        return []

    result = []
    for item in i_liste:
        dato_raa = _xt(item, "dato") or _xt(item, "fremsatt_dato")
        result.append({
            "id":         _xt(item, "id"),
            "tittel":     _xt(item, "tittel"),
            "avsender":   (
                _xt(item, "avsender")
                or _xt(item, "organisasjonsnavn")
                or _xt(item, "navn")
            ),
            "dato":       dato_raa[:10] if dato_raa and dato_raa != "0001-01-01T00:00:00Z" else "",
            "ingress":    _xt(item, "ingress"),
            "eksport_id": _xt(item, "eksport_id"),
        })
    return result


# ---------------------------------------------------------------------------
# Emner — ämnesklassificering
# ---------------------------------------------------------------------------

def hamta_emner() -> list[dict]:
    """
    Hämtar Stortingets ämnesklassificering (hierarkisk lista, ca 250 ämnen).

    Returnerar lista med dicts: id, navn, forelder_id.
    Toppnivåämnen har tomt forelder_id. Underämnen har forelder_id = förälderns id.

    Exempel på hierarki:
      ARBEIDSLIV (toppnivå, forelder_id='')
        → ARBEIDSMILJØ (undernivå, forelder_id=<id för ARBEIDSLIV>)
        → LØNNSFORHOLD
    """
    root    = _get_xml_root("emner")
    e_liste = root.find(f"{NP}emne_liste")
    if e_liste is None:
        log.warning("Ingen emne_liste i svar från /emner")
        return []

    result = []
    for emne in e_liste.findall(f"{NP}emne"):
        # Toppnivåämne
        result.append({
            "id":          _xt(emne, "id"),
            "navn":        _xt(emne, "navn"),
            "forelder_id": "",
        })
        # Underämnen (underemne_liste → underemne med föräldrareferens i huvudemne_id)
        underemne_liste = emne.find(f"{NP}underemne_liste")
        if underemne_liste is not None:
            for underemne in underemne_liste.findall(f"{NP}underemne"):
                result.append({
                    "id":          _xt(underemne, "id"),
                    "navn":        _xt(underemne, "navn"),
                    "forelder_id": _xt(underemne, "hovedemne_id"),
                })
    return result
