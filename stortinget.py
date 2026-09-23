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

Anropstak: Stortinget tillåter 100 anrop/minut. Klienten håller sig under
taket med en tokenhink (standard 90/minut) och respekterar Retry-After när
källan ändå svarar HTTP 429.

API-dokumentation: https://data.stortinget.no/dokumentasjon-og-hjelp/
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from lxml import etree

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE    = os.getenv("STORTINGET_API_BASE", "https://data.stortinget.no/eksport")

# Källans tak är 100 anrop/minut. 90 lämnar marginal för klockdrift och för
# att taket räknas på källans sida, inte vår.
RATE_LIMIT  = int(os.getenv("STORTINGET_RATE_LIMIT", "90"))   # anrop/minut

# Namespace för metadata-XML
NS = "http://data.stortinget.no"
NP = f"{{{NS}}}"   # prefix-sträng: {http://data.stortinget.no}

_USER_AGENT = (
    "mcp-for-stortinget-regjeringen-lovdata "
    "(+https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata)"
)

# Delad klient: httpx.Client är trådsäker för anrop, och headers sätts bara här.
_KLIENT = httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=60)

# ---------------------------------------------------------------------------
# Anropstak
#
# En tokenhink släpper igenom högst (hinkens storlek + takten × 60 s) anrop
# under en godtycklig minut. Med en hink lika stor som takten blir det nära
# dubbla taket direkt efter en vilopaus, så hinken hålls liten: 10 platser
# vid 90/minut ger högst 100 anrop under varje rullande minut. Den startar
# halvfull, så att inte heller första minuten efter start kan överskrida taket.
# ---------------------------------------------------------------------------

_TAKT_PER_S   = max(RATE_LIMIT, 1) / 60.0
_HINK_STORLEK = float(min(10, max(RATE_LIMIT, 1)))

_hink_las    = threading.Lock()
_hink_tokens = _HINK_STORLEK / 2
_hink_senast = time.monotonic()
# Tidpunkt (monotonic) före vilken inga anrop får göras, satt av ett 429-svar.
_paus_till   = 0.0

# 429-hantering: antal försök totalt, och längsta väntan vi accepterar innan
# vi hellre ger upp med ett tydligt fel än låter ett verktygsanrop hänga.
_MAX_FORSOK_429   = 3
_MAX_VANTAN_429_S = 60.0
_STANDARD_VANTAN_429_S = 30.0


# ---------------------------------------------------------------------------
# HTTP-primitiver
# ---------------------------------------------------------------------------

def _throttle() -> None:
    """
    Väntar tills ett anrop får göras enligt tokenhinken.

    Verktygen körs på arbetstrådar, så hinken skyddas av ett lås. Tråden
    reserverar sin token inne i låset (saldot får bli negativt) och sover
    utanför det; då väntar samtidiga anrop i tur och ordning i stället för
    att alla vakna samtidigt och gå förbi taket.
    """
    global _hink_tokens, _hink_senast
    with _hink_las:
        nu = time.monotonic()
        _hink_tokens = min(
            _HINK_STORLEK, _hink_tokens + (nu - _hink_senast) * _TAKT_PER_S
        )
        _hink_senast = nu
        _hink_tokens -= 1.0
        vanta = -_hink_tokens / _TAKT_PER_S if _hink_tokens < 0 else 0.0
        vanta = max(vanta, _paus_till - nu)
    if vanta > 0:
        log.debug("Anropstak: väntar %.1f s", vanta)
        time.sleep(vanta)


def _tolka_retry_after(varde: Optional[str]) -> Optional[float]:
    """
    Tolkar Retry-After som sekunder. Headern får enligt HTTP vara antingen ett
    heltal sekunder eller ett HTTP-datum. Returnerar None om den saknas eller
    inte går att tolka.
    """
    if not varde:
        return None
    varde = varde.strip()
    if varde.isdigit():
        return float(varde)
    try:
        tidpunkt = parsedate_to_datetime(varde)
    except (TypeError, ValueError, IndexError):
        return None
    if tidpunkt.tzinfo is None:
        tidpunkt = tidpunkt.replace(tzinfo=timezone.utc)
    return max(0.0, (tidpunkt - datetime.now(timezone.utc)).total_seconds())


class StortingetTakFel(Exception):
    """Stortinget svarade HTTP 429 även efter att Retry-After respekterats."""


def _get(path: str, params: Optional[dict] = None) -> httpx.Response:
    """
    GET mot API_BASE/{path} inom anropstaket, med 429-hantering.

    Vid 429 pausas alla trådar till den tid Retry-After anger (eller
    _STANDARD_VANTAN_429_S om headern saknas), och anropet görs om. Efter
    _MAX_FORSOK_429 försök, eller om källan begär längre väntan än
    _MAX_VANTAN_429_S, kastas StortingetTakFel med besked om när det går att
    försöka igen.
    """
    global _paus_till
    url = f"{API_BASE}/{path}"
    for forsok in range(1, _MAX_FORSOK_429 + 1):
        _throttle()
        r = _KLIENT.get(url, params=params or {})
        if r.status_code != 429:
            return r

        vanta = _tolka_retry_after(r.headers.get("Retry-After"))
        if vanta is None:
            vanta = _STANDARD_VANTAN_429_S
        with _hink_las:
            _paus_till = max(_paus_till, time.monotonic() + vanta)

        if vanta > _MAX_VANTAN_429_S or forsok == _MAX_FORSOK_429:
            raise StortingetTakFel(
                f"Stortinget begränsar anropstakten (HTTP 429) och ber om "
                f"{vanta:.0f} sekunders paus. Försök igen om en stund; "
                f"källans tak är 100 anrop per minut."
            )
        log.warning(
            "Stortinget svarade 429 på %s (försök %d/%d) — väntar %.0f s",
            path, forsok, _MAX_FORSOK_429, vanta,
        )
    raise AssertionError("oåtkomlig")  # loopen returnerar eller kastar alltid


class StortingetFel(Exception):
    """
    Stortinget avvisade förfrågan med sitt eget <feil>-dokument.

    API:et svarar med HTTP 500 och en <feil>-kropp när en identifierare är
    okänd eller saknar innehåll — det är alltså inte ett serverhaveri utan ett
    normalt "finns inte"-svar. Undantaget skiljer det fallet från äkta
    nätverks- och serverfel så att verktygen kan ge ett begripligt besked.
    """


def _get_xml_root(path: str, params: Optional[dict] = None) -> etree._Element:
    """GET mot API_BASE/{path} — returnerar lxml-rot."""
    r = _get(path, params)

    parser = etree.XMLParser(recover=True, load_dtd=False, no_network=True)

    if r.status_code >= 400:
        # Läs kroppen innan felet kastas — <feil> betyder "okänd identifierare",
        # inte att tjänsten är trasig.
        try:
            rot = etree.fromstring(r.content, parser=parser)
        except Exception:
            rot = None
        if rot is not None and _lokal_tag(rot) == "feil":
            raise StortingetFel(
                f"Stortinget känner inte igen förfrågan {path} med {params}. "
                f"Kontrollera identifieraren."
            )
        r.raise_for_status()

    return etree.fromstring(r.content, parser=parser)


def _get_xml_text(path: str, params: Optional[dict] = None) -> str:
    """GET mot API_BASE/{path} — returnerar råa XML-bytes som sträng (för fulltext)."""
    r = _get(path, params)
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

    # Proposisjoner och stortingsmeldinger distribueras inte av Stortingets API
    # utan pekar mot regjeringen.no. URL:en ligger i publikasjonsreferenserna;
    # den lyfts fram som eget fält eftersom det är ingången till
    # nor_hamta_regjeringen.
    regjeringen_url = next(
        (p["lenke_url"] for p in pub_refs
         if p.get("lenke_url") and "regjeringen.no" in p["lenke_url"]),
        "",
    )

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
        "regjeringen_url":   regjeringen_url,
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
            traffade = _matchade_termer(termer, sok_tekst)
            if not traffade:
                continue
            # Redovisa matchningsgrunden så anroparen ser VARFÖR träffen kom med
            s = {**s, "matchade_termer": traffade}

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
    traffar = []
    for s in alle:
        matchade = _matchade_termer(termer, s["tittel"] or "")
        if matchade:
            traffar.append({**s, "matchade_termer": matchade})
    return traffar


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

    traffar = []
    for h in alle:
        matchade = _matchade_termer(
            termer, (h["tittel"] or "") + " " + (h["komite"] or "")
        )
        if matchade:
            traffar.append({**h, "matchade_termer": matchade})
    return traffar


# ---------------------------------------------------------------------------
# Sökhjiälpfunktioner
# ---------------------------------------------------------------------------

def _split_termer(fraga: str) -> list[str]:
    """
    Splittar frågan på komma → lista av lowercase-termer.

    En term kan innehålla flera ord. Mellanslag inom en term splittar INTE —
    orden hör ihop och matchas med AND (se _matcher). Den tidigare
    uppsplittringen på mellanslag gjorde att en fras som
    "forbud mot konverteringsterapi" löstes upp i tre fristående ord med
    OR-logik, vilket rankade in varje ärende som råkade innehålla ordet "mot".
    """
    return [t.strip().lower() for t in fraga.split(",") if t.strip()]


def _matcha_term(term: str, haystack_lower: str) -> bool:
    """
    Returnerar True om termen matchar. Alla ord i termen måste förekomma (AND).

    Orden behöver inte stå intill varandra — titeln och ämnesorden är
    hopslagna till en söksträng, så en flerordig term kan ha sina ord
    utspridda över fälten.
    """
    ord_i_term = [o for o in term.split() if o]
    if not ord_i_term:
        return False
    return all(o in haystack_lower for o in ord_i_term)


def _matchade_termer(termer: list[str], haystack: str) -> list[str]:
    """Returnerar de termer som matchar haystack (OR mellan termer, AND inom)."""
    h = haystack.lower()
    return [t for t in termer if _matcha_term(t, h)]


def _matcher(termer: list[str], haystack: str) -> bool:
    """
    Returnerar True om NÅGON term matchar (OR mellan kommaseparerade termer).

    Inom en term gäller AND — alla ord måste förekomma.
    """
    return bool(_matchade_termer(termer, haystack))


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
    r = _get("stortingsvedtak", {"vedtakid": vedtakid})
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

def _html_till_text(html: str) -> str:
    """
    Gör om HTML-innehåll till läsbar text med bevarade styckegränser.

    Høringsinnspillens tekst levereras som HTML i XML-elementet. Styckena är
    betydelsebärande i remissvar, så <p> och <br> blir radbrytningar i stället
    för att kollapsa till en enda textmassa.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        log.debug("HTML-parsning misslyckades, faller tillbaka på regex: %s", exc)
        text = re.sub(r"<\s*(br|/p|/div|/li)\s*/?\s*>", "\n", html, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
    # Normalisera whitespace utan att slå ihop stycken
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def hamta_skriftlige_innspill(horingid: str) -> list[dict]:
    """
    Hämtar lista med skriftliga innspill (remissvar) till en høring.

    Returnerar lista med dicts: id, tittel, avsender, dato, fulltext_md.

    Fulltexten ingår i samma svar från källan — inget separat anrop behövs.

    Fältnamnen är verifierade mot ett live-svar från
    /eksport/horingsinnspill?horingid=... (2026-08-10). Källans element per
    innspill är: dato, id, organisasjon, tekst, tittel. Notera att API:ets
    dokumentationssida visar ett äldre exempelsvar med elementnamnen
    horingsnotat_liste/horingsnotat — det stämmer inte med vad tjänsten
    faktiskt returnerar. Rotelement och lista heter horingsinnspill_oversikt
    respektive horingsinnspill_liste.
    """
    # Källan svarar HTTP 500 med ett <feil>-dokument när høringen saknar
    # godkända innspill eller ID:t är okänt. Översätt det till ett begripligt
    # besked i stället för ett rått serverfel.
    try:
        root = _get_xml_root("horingsinnspill", {"horingid": horingid})
    except StortingetFel:
        raise StortingetFel(
            f"Stortinget har inga godkända skriftliga innspill registrerade för "
            f"høring {horingid}, alternativt är høring-ID:t okänt. Kontrollera "
            f"ID:t mot nor_sok_stortinget(typer='horinger'). Observera att "
            f"muntliga høringer normalt saknar skriftliga innspill."
        ) from None

    i_liste = root.find(f"{NP}horingsinnspill_liste")
    if i_liste is None:
        log.warning(
            "Ingen horingsinnspill_liste i svar från /horingsinnspill?horingid=%s "
            "(rotelement: %s)", horingid, _lokal_tag(root)
        )
        return []

    result = []
    for item in i_liste:
        dato_raa = _xt(item, "dato")
        tekst_html = _xt(item, "tekst")
        result.append({
            "id":          _xt(item, "id"),
            "tittel":      _xt(item, "tittel"),
            "avsender":    _xt(item, "organisasjon"),
            "dato":        dato_raa[:10] if dato_raa and not dato_raa.startswith("0001-01-01") else "",
            "fulltext_md": _html_till_text(tekst_html),
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
