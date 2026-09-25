# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
mcp_server.py — MCP-server för norsk riksdags- och rättsdata

Exponerar följande verktyg till MCP-kompatibla AI-verktyg:

  nor_lista_sesjoner      — Listar alla Stortingssesjoner (1986-87 och framåt)
  nor_sok_stortinget      — Söker saker, spørsmål och høringer i Stortinget
  nor_lista_publikasjoner — Listar en saks publikationsreferenser utan fulltext
  nor_hamta_dokument      — Hämtar metadata + fulltext för ett Stortinget-dokument
  nor_hamta_regjeringen   — Hämtar en proposisjon/NOU/Meld.St. från regjeringen.no
  nor_sok_lovdata         — Söker norska lagar och föreskrifter (Lovdata-cache)
  nor_hamta_lovdokument   — Hämtar fulltext för ett Lovdata-dokument ur cachen
  nor_sok                 — Aggregerad sökning över alla norska källor
  nor_sok_i_dokument      — Fulltextsökning inom ett cachat dokument
  nor_hamta_vedtak        — Hämtar stortingsvedtak (parlamentariska beslut)
  nor_hamta_horinginnspill — Hämtar skriftliga innspill till en høring
  nor_lista_emner         — Hämtar Stortingets ämnesklassificering
  nor_sok_semantisk       — Semantisk sökning med pgvector (kräver PostgreSQL)

Datakällor:
  Stortinget   — data.stortinget.no (XML metadata + fulltext, 1986-87+)
  Lovdata      — gratis bulk-nedladdning (daglig synk)
  regjeringen  — proposisjoner och NOU som PDF (PDF-extraktion + OCR under minnesvakt)

Transport (MCP_TRANSPORT i .env, se mcp_transport.py):

  stdio — lokal MCP-klient, som startar processen direkt:
    python3 mcp_server.py

  http — delad drift bakom en URL (Streamable HTTP):
    MCP_TRANSPORT=http MCP_API_KEY=<NYCKEL> python3 mcp_server.py
    Servern lyssnar på MCP_HOST:MCP_PORT (standard 127.0.0.1:8003) och
    kräver MCP_API_KEY; utan nyckel startar den inte.

Konfiguration via .env (se config.example.env).
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any, NotRequired, Optional, TypedDict

from dotenv import load_dotenv

# .env läses före de egna modulerna, som läser sin konfiguration vid import.
load_dotenv(Path(__file__).parent / ".env")

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import regjeringen as rg
import stortinget as st
from db import _ar_postgres, initiera_schema
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

# Standardtak för fulltext i hämtverktygen. Utan ett tak som gäller by default
# kan ett anrop mot ett stort dokument överskrida MCP-protokollets storleksgräns
# och misslyckas helt, utan väg runt. Anroparen kan alltid höja taket, eller
# sätta 0 för hela texten som ett uttryckligt val.
NOR_MAX_TECKEN = int(os.getenv("NOR_MAX_TECKEN", "60000"))

# Övre tak för ett enskilt textutdrag, även när anroparen ber om hela texten
# (max_tecken=0). Svaret skickas två gånger — som text och som struktur — och
# en proposisjon kan vara 600 000 tecken, vilket annars ger ett svar på
# nästan 2 MB. 200 000 tecken håller svaret väl under 1 MB; resten läses med
# fran_tecken.
NOR_TAK_TECKEN = int(os.getenv("NOR_TAK_TECKEN", "200000"))

# ── Query-expansion ────────────────────────────────────────────────────────────
# Aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
# Stöder alla OpenAI-kompatibla endpoints (Claude, OpenAI, Ollama, LM Studio).
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL", "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL", "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
)

# ── Embeddingmodell ────────────────────────────────────────────────────────────
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "NbAiLab/nb-sbert-base")
_embedding_modell = None   # Laddas vid första semantiska sökning
_embedding_las = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "norge",
    version="1.1.0",
    cache_hints=CACHE_HINTAR,
    instructions=(
        "MCP-server för norsk riksdags- och rättsdata. "
        "Täcker Stortinget (1986-87–idag), norska lagar och föreskrifter (Lovdata), "
        "samt proposisjoner och NOU från regjeringen.no. "
        "Verktygen har prefixet nor_. "
        "SÖKTERMER: komma separerar termer och tolkas som OR mellan dem; flera "
        "ord inom en och samma term tolkas som AND — alla orden måste förekomma. "
        "'forbud mot konverteringsterapi, omvendelsesterapi' söker alltså efter "
        "poster som innehåller alla tre orden i första termen, eller ordet i den "
        "andra. Varje träff visar i matchade_termer vilken term som gav träffen. "
        "SVARSSTORLEK: hämtverktygen tar max_tecken och fran_tecken; ett trunkerat "
        "svar bär fälten trunkerad och fortsatt_fran_tecken. Börja med "
        "nor_hamta_dokument(bara_metadata=True) för stora saker och hämta sedan en "
        "publikation i taget — hela saken på en gång kan överskrida svarsgränsen. "
        "nor_lista_publikasjoner ger vägen från en sökträff till sakens dokument. "
        "nor_hamta_vedtak ger parlamentariska beslutstexter. "
        "nor_hamta_horinginnspill ger skriftliga remissvar till høringer. "
        "nor_lista_emner ger Stortingets ämnesklassificering. "
        "LOVTIDEND: Norsk Lovtidend avd. I (2001–idag) innehåller lagar och "
        "sentrala forskrifter i den form de kungjordes — främst ändringslagar "
        "och ändringsforskrifter. Använd den för att se vilken ändringslag som "
        "ändrade vad och när: nor_sok_lovdata(fraga='LOV-2005-06-17-62', "
        "dok_type='lovtidend') listar de kungjorda dokument som ändrar "
        "arbetsmiljölagen, nyast först, med kungörandedatum (dato), "
        "ikraftträdande (ikraft) och ändrade författningar (endrer). Läs "
        "ändringstexten med nor_hamta_lovdokument(lovdata_id='LTI/lov/...'). "
        "Den gällande, konsoliderade texten söks som förut med dok_type 'lov', "
        "'forskrift' eller 'alla'. nor_sok_semantisk söker som standard utanför "
        "Lovtidend; dok_type='lovtidend' eller 'alla_med_lovtidend' tar med den."
    ),
)

# En lag har samma beteckning (LOV-...) i gällande form och i Lovtidend. Vid
# uppslag på beteckning eller titel ska den gällande texten komma först.
_LOVTIDEND_SIST = "CASE WHEN dok_type = 'lovtidend' THEN 1 ELSE 0 END"

# Sessionslistan hämtas en gång och återanvänds. Verktygen körs på
# arbetstrådar, så hämtningen skyddas av ett lås med dubbelkontroll.
_sesjoner_cache: Optional[dict] = None
_sesjoner_las = threading.Lock()


def _sesjoner() -> dict:
    global _sesjoner_cache
    if _sesjoner_cache is None:
        with _sesjoner_las:
            if _sesjoner_cache is None:
                _sesjoner_cache = st.hamta_sesjoner()
    return _sesjoner_cache


def _verktygsfel(exc: Exception, sammanhang: str) -> ToolError:
    """
    Översätter ett undantag till ToolError med ett begripligt meddelande.

    Utan översättning får klienten bara "Error executing tool" utan orsak.
    Förväntade fel (okänd identifierare, anropstak, nätverksfel) får ett
    eget besked; oväntade loggas med spår och rapporteras med sin text.
    """
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (st.StortingetFel, st.StortingetTakFel)):
        return ToolError(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        return ToolError(
            f"{sammanhang}: källan svarade HTTP {exc.response.status_code} "
            f"för {exc.request.url}."
        )
    if isinstance(exc, httpx.HTTPError):
        return ToolError(f"{sammanhang}: källan svarade inte ({exc}). Försök igen senare.")
    log.exception("%s misslyckades", sammanhang)
    return ToolError(f"{sammanhang} misslyckades: {exc}")


def expandera_fraga(fraga: str) -> list[str]:
    """
    Expanderar söktermen med norsk parlamentarisk och juridisk terminologi via LLM.

    Returnerar kompletterande söktermer (bokmål, nynorsk, juridiska ekvivalenter)
    eller tom lista om expansion är inaktiverat eller misslyckas.

    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
    Promptfilen (prompts/expansion_prompt.txt) kan redigeras fritt.
    """
    if not QUERY_EXPANSION_ENABLED:
        return []

    prompt_path = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_path.exists():
        log.warning("Promptfil för query-expansion saknas: %s", prompt_path)
        return []

    try:
        from openai import OpenAI

        prompt_mall = prompt_path.read_text(encoding="utf-8")
        prompt = prompt_mall.replace("{query}", fraga)

        klient = OpenAI(
            base_url=QUERY_EXPANSION_BASE_URL or None,
            api_key=QUERY_EXPANSION_API_KEY or "placeholder",
        )
        svar = klient.chat.completions.create(
            model=QUERY_EXPANSION_MODEL or "claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1,
        )
        text = svar.choices[0].message.content or ""
        termer = [t.strip() for t in text.split(",") if t.strip()]
        log.debug("Query-expansion: '%s' → %s", fraga, termer)
        return termer

    except Exception as exc:
        log.warning("Query-expansion misslyckades: %s", exc)
        return []


def _begransa_text(
    text: Optional[str],
    max_tecken: int,
    fran_tecken: int = 0,
) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Returnerar ett dict-fragment som slås ihop med verktygets svar:
      text                — utdraget
      tecken_totalt       — hela textens längd
      tecken_visade       — utdragets längd
      trunkerad           — True om något kapats bort
      fortsatt_fran_tecken — värde att skicka som fran_tecken i nästa anrop,
                             eller None när texten är slut

    max_tecken <= 0 betyder så mycket som ryms under NOR_TAK_TECKEN, och
    inget värde får gå över taket. Klipper på ordgräns, aldrig mitt i ett ord.

    fortsatt_fran_tecken är utdragets faktiska slut. Kapningen på ordgräns
    gör utdraget kortare än max_tecken, så fran_tecken + max_tecken skulle
    hoppa över det avkapade ordet.
    """
    text = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]
    if max_tecken <= 0 or max_tecken > NOR_TAK_TECKEN:
        max_tecken = NOR_TAK_TECKEN

    if len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        # Ett utdrag av bara blanktecken skulle ge slut == start, och
        # fortsättningen skulle peka på samma ställe igen.
        utdrag = utdrag.rstrip() or rest[:max_tecken]
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
    }


def _hamta_embedding_modell():
    """
    Laddar embeddingmodellen vid första behov.

    Dubbelkontrollerad låsning: flera sökanrop kan komma samtidigt på olika
    trådar, och utan låset kunde två av dem ladda modellen var för sig.
    Utskrifter från modellbiblioteken kan inte störa stdio-protokollet: SDK:n
    leder om fildeskriptor 1 till stderr och skriver protokollet på en egen
    kopia.
    """
    global _embedding_modell
    if _embedding_modell is None:
        with _embedding_las:
            if _embedding_modell is None:
                from sentence_transformers import SentenceTransformer
                _embedding_modell = SentenceTransformer(EMBEDDING_MODEL)
                log.info("Embeddingmodell laddad: %s", EMBEDDING_MODEL)
    return _embedding_modell


def _forvarm_http() -> None:
    """Laddar embeddingmodellen före första anropet i http-läget (bara med Postgres)."""
    if _ar_postgres():
        try:
            _hamta_embedding_modell()
        except Exception as exc:
            log.warning("Embeddingmodellen kunde inte laddas i förväg: %s", exc)


# ── Svarstyper ─────────────────────────────────────────────────────────────────
#
# Typerna ger klienten ett utdataschema. Svaret valideras mot typen, så fält
# som kan saknas eller vara null i källdatan är NotRequired eller "| None".
# Heterogena poster (saker, träffar, publikationer) typas som dict[str, Any];
# det stabila skalet runt dem typas fullt ut.

Post = dict[str, Any]


class SesjonerSvar(TypedDict):
    innevaerende: str
    sesjoner: list[Post]
    antal: int


class StortingetSokSvar(TypedDict):
    fraga: str
    sesjonid: str
    saker: NotRequired[list[Post]]
    saker_antal: NotRequired[int]
    sporsmal: NotRequired[list[Post]]
    sporsmal_antal: NotRequired[int]
    horinger: NotRequired[list[Post]]
    horinger_antal: NotRequired[int]


class DokumentSvar(TypedDict):
    sesjonid: str
    dokument: list[Post]
    sakid: NotRequired[str]
    publikasjonid: NotRequired[str]
    sak: NotRequired[Post]
    publikasjoner: NotRequired[list[Post]]
    regjeringen_url: NotRequired[str]
    notat: NotRequired[str]


class PublikasjonerSvar(TypedDict):
    sakid: str
    tittel: str
    sesjonid: str
    dokumentgruppe: str
    sak_status: str
    antal: int
    publikasjoner: list[Post]
    regjeringen_url: str
    notat: NotRequired[str]


class TraffSvar(TypedDict):
    fraga: str
    antal: int
    treff: list[Post]


class LovdokumentSvar(TypedDict):
    lovdata_id: str | None
    beteckning: str | None
    tittel: str | None
    dok_type: str | None
    dato: str | None
    url: str | None
    fulltext_md: str | None
    tecken_totalt: int
    tecken_visade: int
    trunkerad: bool
    fortsatt_fran_tecken: int | None
    endrer: NotRequired[list[str]]
    ikraft: NotRequired[str | None]
    las_vidare: NotRequired[str]


class SamladSokSvar(TypedDict):
    fraga: str
    stortinget: Post
    lovdata: Post
    regjeringen: Post


class SokIDokumentSvar(TypedDict):
    identifierare: str
    lovdata_id: str | None
    beteckning: str | None
    tittel: str
    kilde: str | None
    dok_type: str | None
    publikasjonid: str | None
    url: str | None
    fraga: str
    antal: int
    avsnitt_totalt: int
    treff: list[Post]


class RegjeringenSvar(TypedDict):
    tittel: str | None
    beteckning: str | None
    dok_type: str | None
    url: str | None
    pdf_url: str | None
    fulltext_md: str | None
    kalla: str
    tecken_antal: int
    tecken_totalt: int
    trunkerad: bool
    fortsatt_fran_tecken: int | None
    las_vidare: NotRequired[str]
    fel: str | None


class VedtakSvar(TypedDict):
    sesjonid: str
    vedtakid: NotRequired[str]
    vedtak: NotRequired[Post]
    antal: NotRequired[int]
    vedtak_liste: NotRequired[list[Post]]
    notat: NotRequired[str]


class InnspillSvar(TypedDict):
    horingid: str
    antal: int
    innspill: list[Post]
    notat: NotRequired[str]


class EmnerSvar(TypedDict):
    antal: int
    toppnivaa: list[Post]
    undernivaa: list[Post]


class SemantiskSvar(TypedDict):
    fraga: str
    expansion: list[str]
    kilde: str
    dok_type: str
    antal: int
    treff: list[Post]
    diagnostik: NotRequired[Post]


# ── Verktyg ────────────────────────────────────────────────────────────────────


@mcp.tool(title="Lista Stortingssesjoner", annotations=LASNING_EXTERN)
def nor_lista_sesjoner() -> SesjonerSvar:
    """
    Listar alla tillgängliga Stortingssesjoner (43 st, 1986-87 och framåt).

    Returnerar en lista med sessions-ID, namn och datum.
    Använd sessions-ID (t.ex. "2024-2025") som indata till nor_sok_stortinget.
    """
    try:
        return _sesjoner()
    except Exception as exc:
        raise _verktygsfel(exc, "Hämtningen av sesjoner") from exc


@mcp.tool(title="Sök i Stortinget", annotations=LASNING_EXTERN)
def nor_sok_stortinget(
    fraga: str,
    sesjonid: str = "",
    typer: str = "saker,sporsmal,horinger",
    dokumentgruppe: str = "",
    emne: str = "",
    sak_status: str = "",
    max_treff: int = 10,
) -> StortingetSokSvar:
    """
    Söker i Stortingets data för en given session.

    Parametrar:
      fraga          — Sökfråga. Komma separerar termer och ger OR mellan dem;
                       flera ord inom en term ger AND — alla orden måste
                       förekomma. "skatt, avgift" hittar skatt ELLER avgift;
                       "forbud mot konverteringsterapi" kräver alla tre orden.
                       Varje träff visar i matchade_termer vad som gav träffen.
                       Tom sträng med filter = hämta alla som matchar filtren.
      sesjonid       — Sessions-ID, t.ex. "2024-2025". Tomt = senaste session.
      typer          — Kommaseparerad lista: saker, sporsmal, horinger.
                       Standard: alla tre.
      dokumentgruppe — Filtrera saker på dokumentgruppe (partiell matchning).
                       Vanliga värden: 'lovsak', 'stmeld', 'innst', 'dok8',
                       'grunnlovsforslag', 'budsjettforslag'.
                       Tom = ingen filter.
      emne           — Filtrera saker på ämnesord (partiell matchning).
                       Exempel: "arbeidsmiljø", "skatt", "klima".
                       Tom = ingen filter.
      sak_status     — Filtrera saker på status (partiell matchning).
                       Exempel: "behandlet", "mottatt", "trukket".
                       Tom = ingen filter.
      max_treff      — Max antal träffar per typ (standard 10).

    Returnerar:
      En dict med nycklarna "saker", "sporsmal" och/eller "horinger" beroende
      på vad som söks, samt "sesjonid" och "fraga".

    OBS: Söker i titlar, emner, stikkord och innstillingstekst (live-API).
    """
    try:
        sesjoner = _sesjoner()
        if not sesjonid:
            sesjonid = st.aktuell_sesjonid(sesjoner)

        aktiva_typer = {t.strip().lower() for t in typer.split(",")}
        resultat: dict = {"fraga": fraga, "sesjonid": sesjonid}

        if "saker" in aktiva_typer:
            saker = st.sok_saker(
                fraga,
                sesjonid,
                dokumentgruppe=dokumentgruppe,
                emne=emne,
                sak_status=sak_status,
            )[:max_treff]
            resultat["saker"] = saker
            resultat["saker_antal"] = len(saker)

        if "sporsmal" in aktiva_typer:
            sporsmal = st.sok_sporsmal(fraga, sesjonid)[:max_treff]
            resultat["sporsmal"] = sporsmal
            resultat["sporsmal_antal"] = len(sporsmal)

        if "horinger" in aktiva_typer:
            horinger = st.sok_horinger(fraga, sesjonid)[:max_treff]
            resultat["horinger"] = horinger
            resultat["horinger_antal"] = len(horinger)

        return resultat

    except Exception as exc:
        raise _verktygsfel(exc, "Sökningen i Stortinget") from exc


@mcp.tool(title="Hämta Stortinget-dokument", annotations=LASNING_EXTERN)
def nor_hamta_dokument(
    id: str,
    id_typ: str = "sakid",
    sesjonid: str = "",
    spara_i_db: bool = True,
    bara_metadata: bool = False,
    publikasjon: str = "",
    max_tecken: int = NOR_MAX_TECKEN,
    fran_tecken: int = 0,
) -> DokumentSvar:
    """
    Hämtar metadata och fulltext för ett Stortinget-dokument.

    Parametrar:
      id        — Dokumentets identifierare. Beroende på id_typ:
                    sakid:        Stortingets ärendenummer (t.ex. "80368")
                    publikasjonid: Publikations-ID (t.ex. "a-1001")
      id_typ    — "sakid" (standard) eller "publikasjonid".
      sesjonid  — Sessions-ID för XML-parsning (t.ex. "2024-2025").
                  Lämna tomt om okänt — påverkar val av XML-parser.
      spara_i_db — Spara dokumentet i lokal databas (standard: true).
      bara_metadata — Returnera sakens metadata och publikationsreferenserna
                  UTAN fulltext. Snabbt och litet svar. Använd detta först för
                  att se vilka publikationer saken har, och hämta sedan en i taget.
      publikasjon — Hämta bara EN publikation ur saken. Ange dess eksport_id
                  eller lenke_url ur publikasjon_referanse_liste. Tom = alla.
      max_tecken — Teckentak per publikations fulltext (0 = upp till serverns
                  övre tak på 200 000 tecken; resten läses med fran_tecken).
                  Varje trunkerat dokument får fälten trunkerad, tecken_totalt,
                  tecken_visade och fortsatt_fran_tecken.
      fran_tecken — Börja fulltexten vid denna teckenposition (paginering).

    Returnerar:
      sak        — Sakens metadata (tittel, status, emner, publikasjoner,
                   regjeringen_url)
      dokument   — Lista av hämtade publikasjoner med fulltext_md. En
                   publikation från regjeringen.no som inte gick att hämta
                   bär fältet fel med orsaken.

    Okänt id, okänd publikation och okänd id_typ ger ett verktygsfel.

    STORLEK: en sak kan ha många publikationer på vardera hundratusentals tecken.
    Utan begränsning kan svaret överskrida MCP:s storleksgräns och anropet
    misslyckas helt. Börja därför med bara_metadata=True, och hämta sedan
    enskilda publikationer med publikasjon=... och vid behov max_tecken.

    OBS: Proposisjoner distribueras INTE av Stortingets API utan hämtas direkt
    från regjeringen.no via nor_hamta_regjeringen. URL:en finns i sakens fält
    regjeringen_url.
    """
    try:
        dokument_lista = []

        if id_typ == "sakid":
            # Hämta sak-metadata och dess publikasjoner
            sak = st.hamta_sak(id)
            if not sesjonid:
                sesjonid = sak.get("sesjonid", "")

            # Metadata utan fulltext — det billiga första anropet
            if bara_metadata:
                return {
                    "sakid":            id,
                    "sesjonid":         sesjonid,
                    "sak":              sak,
                    "publikasjoner":    sak.get("publikasjoner", []),
                    "regjeringen_url":  sak.get("regjeringen_url", ""),
                    "dokument":         [],
                    "notat": (
                        "Endast metadata hämtad. Hämta en publikation med "
                        "nor_hamta_dokument(id=..., publikasjon=<eksport_id eller lenke_url>)."
                    ),
                }

            pub_referenser = sak.get("publikasjoner", [])
            if publikasjon:
                valda = [
                    p for p in pub_referenser
                    if publikasjon in (p.get("eksport_id", ""), p.get("lenke_url", ""))
                ]
                if not valda:
                    giltiga = ", ".join(
                        p.get("eksport_id") or p.get("lenke_url", "") for p in pub_referenser
                    )
                    raise ToolError(
                        f"Publikationen '{publikasjon}' finns inte bland sakens "
                        f"{len(pub_referenser)} publikationsreferenser ({giltiga}). "
                        f"Kör nor_lista_publikasjoner('{id}') för att se dem."
                    )
                pub_referenser = valda

            for pub_ref in pub_referenser:
                pub_id  = pub_ref.get("eksport_id", "")
                pub_url = pub_ref.get("lenke_url", "")

                # Proposisjoner och stortingsmeldinger pekar på regjeringen.no
                if "regjeringen.no" in pub_url or not pub_id:
                    if pub_url:
                        log.info("Hämtar regjeringen.no-dokument: %s", pub_url)
                        rg_data = rg.hamta_og_ekstraher(pub_url)
                        # Trunkeringen gäller bara svaret till anroparen —
                        # databasen får alltid hela texten.
                        utdrag = _begransa_text(
                            rg_data.get("fulltext_md"), max_tecken, fran_tecken
                        )
                        dokument_lista.append({
                            "typ":         rg_data.get("dok_type", "ekstern"),
                            "kilde":       "regjeringen.no",
                            "url":         rg_data.get("url", pub_url),
                            "pdf_url":     rg_data.get("pdf_url"),
                            "tittel":      rg_data.get("tittel", ""),
                            "beteckning":  rg_data.get("beteckning", ""),
                            "fulltext_md": utdrag["text"],
                            "tecken_totalt":        utdrag["tecken_totalt"],
                            "tecken_visade":        utdrag["tecken_visade"],
                            "trunkerad":            utdrag["trunkerad"],
                            "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
                            "fel":         rg_data.get("fel"),
                        })
                        if spara_i_db and rg_data.get("fulltext_md"):
                            try:
                                from db import upsert_dokument
                                upsert_dokument(
                                    kilde="regjeringen.no",
                                    dok_type=rg_data.get("dok_type", "proposisjon"),
                                    beteckning=rg_data.get("beteckning"),
                                    tittel=rg_data.get("tittel"),
                                    sesjonid=sesjonid,
                                    dato=None,
                                    url=rg_data.get("url", pub_url),
                                    publikasjonid=rg_data.get("url", pub_url),
                                    sakid=id,
                                    lovdata_id=None,
                                    fulltext_md=rg_data.get("fulltext_md"),
                                )
                            except Exception as db_exc:
                                log.warning("Databasskrivning (regjeringen) misslyckades: %s", db_exc)
                    else:
                        dokument_lista.append({
                            "typ":   "ekstern",
                            "kilde": "regjeringen.no",
                            "url":   "",
                            "notat": "Ingen URL tillgänglig",
                        })
                    continue

                # Stortinget-dokument — hämta XML
                parsed = st.hamta_og_parse_publikasjon(pub_id, sesjonid)
                utdrag = _begransa_text(
                    parsed["fulltext_md"], max_tecken, fran_tecken
                )
                dokument_lista.append({
                    "publikasjonid": pub_id,
                    "tittel":        parsed["tittel"],
                    "typ":           parsed["typ"],
                    "metadata":      parsed["metadata"],
                    "fulltext_md":   utdrag["text"],
                    "tecken_totalt":        utdrag["tecken_totalt"],
                    "tecken_visade":        utdrag["tecken_visade"],
                    "trunkerad":            utdrag["trunkerad"],
                    "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
                })

                # Spara i databas
                if spara_i_db:
                    try:
                        from db import upsert_dokument
                        upsert_dokument(
                            kilde="stortinget",
                            dok_type=parsed["typ"],
                            beteckning=None,
                            tittel=parsed["tittel"],
                            sesjonid=sesjonid,
                            dato=parsed["metadata"].get("dato"),
                            url=f"https://data.stortinget.no/eksport/publikasjon?publikasjonid={pub_id}",
                            publikasjonid=pub_id,
                            sakid=id,
                            lovdata_id=None,
                            fulltext_md=parsed["fulltext_md"],
                        )
                    except Exception as db_exc:
                        log.warning("Databasskrivning misslyckades: %s", db_exc)

            svar = {
                "sakid":           id,
                "sesjonid":        sesjonid,
                "sak":             sak,
                "regjeringen_url": sak.get("regjeringen_url", ""),
                "dokument":        dokument_lista,
            }
            if any(d.get("trunkerad") for d in dokument_lista):
                svar["notat"] = (
                    "Minst ett dokument är trunkerat. Läs vidare med "
                    "fran_tecken=<fortsatt_fran_tecken>, eller sätt max_tecken=0 "
                    "för hela texten."
                )
            return svar

        elif id_typ == "publikasjonid":
            # Direkt publikasjonhämtning utan sak-kontext
            parsed = st.hamta_og_parse_publikasjon(id, sesjonid)
            if spara_i_db:
                try:
                    from db import upsert_dokument
                    upsert_dokument(
                        kilde="stortinget",
                        dok_type=parsed["typ"],
                        beteckning=None,
                        tittel=parsed["tittel"],
                        sesjonid=sesjonid,
                        dato=parsed["metadata"].get("dato"),
                        url=f"https://data.stortinget.no/eksport/publikasjon?publikasjonid={id}",
                        publikasjonid=id,
                        sakid=None,
                        lovdata_id=None,
                        fulltext_md=parsed["fulltext_md"],
                    )
                except Exception as db_exc:
                    log.warning("Databasskrivning misslyckades: %s", db_exc)

            utdrag = _begransa_text(parsed["fulltext_md"], max_tecken, fran_tecken)
            return {
                "publikasjonid": id,
                "sesjonid":      sesjonid,
                "dokument":      [{
                    **parsed,
                    "fulltext_md":          utdrag["text"],
                    "tecken_totalt":        utdrag["tecken_totalt"],
                    "tecken_visade":        utdrag["tecken_visade"],
                    "trunkerad":            utdrag["trunkerad"],
                    "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
                }],
            }

        else:
            raise ToolError(f"Okänd id_typ: '{id_typ}'. Använd 'sakid' eller 'publikasjonid'.")

    except Exception as exc:
        raise _verktygsfel(exc, f"Hämtningen av dokument {id}") from exc


@mcp.tool(title="Lista en saks publikationer", annotations=LASNING_EXTERN)
def nor_lista_publikasjoner(sakid: str) -> PublikasjonerSvar:
    """
    Listar en saks publikationsreferenser — utan fulltext.

    Detta är vägen från en sökträff till sakens dokument. Sökträffar från
    nor_sok_stortinget och nor_sok kommer från Stortingets listendpoint, som
    inte bär publikationsreferenserna; de finns bara på den enskilda saken.
    Verktyget hämtar dem billigt, utan att dra in någon fulltext.

    Parametrar:
      sakid — Stortingets ärendenummer, t.ex. "94762"

    Returnerar:
      publikasjoner   — Lista med eksport_id, lenke_url, lenke_tekst och type
      regjeringen_url — URL till proposisjonen/meldingen på regjeringen.no om
                        saken har en sådan (indata till nor_hamta_regjeringen)
      tittel, sesjonid, dokumentgruppe, sak_status

    Kedjning:
      eksport_id → nor_hamta_dokument(id=<eksport_id>, id_typ="publikasjonid")
      regjeringen_url → nor_hamta_regjeringen(url=<regjeringen_url>)
    """
    try:
        sak = st.hamta_sak(sakid)
        pub = sak.get("publikasjoner", [])
        svar = {
            "sakid":           sakid,
            "tittel":          sak.get("tittel", ""),
            "sesjonid":        sak.get("sesjonid", ""),
            "dokumentgruppe":  sak.get("dokumentgruppe", ""),
            "sak_status":      sak.get("sak_status", ""),
            "antal":           len(pub),
            "publikasjoner":   pub,
            "regjeringen_url": sak.get("regjeringen_url", ""),
        }
        if not pub:
            svar["notat"] = (
                "Saken har inga publikationsreferenser hos Stortinget. För "
                "proposisjoner och meldinger ligger dokumentet på regjeringen.no; "
                "saknas även regjeringen_url kan saken vara under behandling och "
                "ännu inte ha publicerade dokument."
            )
        return svar
    except Exception as exc:
        raise _verktygsfel(exc, f"Hämtningen av sak {sakid}") from exc


# Gällande rätt. 'alla' i nor_sok_lovdata och Lovdata-delen av nor_sok avser
# de konsoliderade texterna; Lovtidend söks uttryckligen med dok_type.
_GJELDENDE_TYPER = ("lov", "forskrift")


@mcp.tool(title="Sök i Lovdata och Lovtidend", annotations=LASNING_DB)
def nor_sok_lovdata(
    fraga: str,
    dok_type: str = "alla",
    max_treff: int = 10,
) -> TraffSvar:
    """
    Söker i den lokala Lovdata-cachen: gällande norska lagar och forskrifter,
    och Norsk Lovtidend avd. I.

    Parametrar:
      fraga     — Sökfråga. Komma separerar termer och ger OR mellan dem;
                  flera ord inom en term ger AND — alla orden måste förekomma.
                  Exempel: "arbeidsmiljø, oppsigelse" söker endera termen.
                  Med dok_type='lovtidend' kan en term också vara en
                  författningsreferens ('LOV-2005-06-17-62',
                  'NL/lov/2005-06-17-62', 'lov/2005-06-17-62'); då listas de
                  kungjorda dokument som ändrar den författningen, nyast först.
      dok_type  — 'lov', 'forskrift', 'alla' (gällande lagar och forskrifter,
                  standard) eller 'lovtidend' (Norsk Lovtidend avd. I:
                  lagar och sentrala forskrifter i den form de kungjordes
                  2001 och framåt, mest ändringslagar och ändringsforskrifter).
      max_treff — Max antal träffar (standard 10).

    Returnerar:
      En lista med matchande dokument (lovdata_id, beteckning, tittel,
      dok_type, dato, url) sorterade efter relevans. Lovtidend-träffar bär
      också endrer (refid för de ändrade författningarna) och ikraft
      (ikraftträdandet som källan anger det); dato är kungörandedatum.

    OBS: Cachen uppdateras via lovdata_sync.py (daglig synk). Om cachen är
    tom returneras ett tomt resultat — kör synkskriptet först.
    Sökningen är fulltextsökning i tittel + fulltext_md.
    """
    try:
        from db import fts_sok, lovtidend_som_endrer, normalisera_refid

        if dok_type in ("lov", "forskrift", "lovtidend"):
            typ_filter = dok_type
        else:
            typ_filter = _GJELDENDE_TYPER

        rader: list[dict] = []
        fritext = fraga
        if dok_type == "lovtidend":
            termer = [t.strip() for t in fraga.split(",") if t.strip()]
            refider = [r for r in (normalisera_refid(t) for t in termer) if r]
            for refid in refider:
                for rad in lovtidend_som_endrer(refid, max_treff):
                    rad["matchade_termer"] = [refid]
                    rader.append(rad)
            fritext = ", ".join(t for t in termer if not normalisera_refid(t))

        if fritext.strip():
            sedda = {r["lovdata_id"] for r in rader}
            rader += [
                r for r in fts_sok(
                    fritext,
                    kilde_filter    = "lovdata",
                    dok_type_filter = typ_filter,
                    max_treff       = max_treff,
                )
                if r["lovdata_id"] not in sedda
            ]

        treff = []
        for r in rader[:max_treff]:
            post = {
                "lovdata_id": r["lovdata_id"],
                "beteckning": r["beteckning"],
                "tittel":     r["tittel"],
                "dok_type":   r["dok_type"],
                "dato":       r["dato"],
                "url":        r["url"],
                "rank":       r["rank"],
            }
            if r["dok_type"] == "lovtidend":
                post["endrer"] = r.get("endrer") or []
                post["ikraft"] = r.get("ikraft")
            if r.get("matchade_termer"):
                post["matchade_termer"] = r["matchade_termer"]
            treff.append(post)

        return {
            "fraga": fraga,
            "antal": len(treff),
            "treff": treff,
        }

    except Exception as exc:
        raise _verktygsfel(exc, "Sökningen i Lovdata-cachen") from exc


@mcp.tool(title="Hämta Lovdata-dokument", annotations=LASNING_DB)
def nor_hamta_lovdokument(
    lovdata_id: str,
    max_tecken: int = NOR_MAX_TECKEN,
    fran_tecken: int = 0,
) -> LovdokumentSvar:
    """
    Hämtar fulltext och metadata för ett Lovdata-dokument ur lokal cache.

    Parametrar:
      lovdata_id  — Lovdatas dokumentidentifierare, t.ex. 'NL/lov/2005-05-20-28'
                    eller beteckning 'LOV-2005-05-20-28'. En beteckning ger
                    den gällande texten; den kungjorda versionen i Lovtidend
                    hämtas med sitt id, t.ex. 'LTI/lov/2026-01-23-1'.
      max_tecken  — Teckentak för fulltext_md (0 = upp till serverns övre
                    tak på 200 000 tecken; resten läses med fran_tecken).
                    Rekommenderat för stora dokument — föreskrifter kan vara
                    100 000+ tecken. Exempel: max_tecken=20000 för en inledande
                    läsning.
      fran_tecken — Börja texten vid denna teckenposition. Skicka värdet ur
                    fortsatt_fran_tecken för att läsa vidare där förra anropet
                    slutade.

    Returnerar:
      Metadata + fulltext_md för dokumentet, samt tecken_totalt, tecken_visade,
      trunkerad och fortsatt_fran_tecken.
      Ett dokument som inte finns i cachen ger ett verktygsfel.

    Vill du hitta en enskild bestämmelse i stället för att läsa hela lagen —
    använd nor_sok_i_dokument.
    """
    try:
        from db import _cursor, _ph, _prefix

        # Stöd både lovdata_id och beteckning som indata
        with _cursor() as cur:
            cur.execute(
                f"""
                SELECT lovdata_id, beteckning, tittel, dok_type, dato, url, fulltext_md,
                       endrer, ikraft
                FROM   {_prefix()}dokument
                WHERE  kilde = {_ph()}
                  AND  (lovdata_id = {_ph()} OR beteckning = {_ph()})
                ORDER BY {_LOVTIDEND_SIST}
                LIMIT 1
                """,
                ("lovdata", lovdata_id, lovdata_id)
            )
            rad = cur.fetchone()

        if not rad:
            raise ToolError(
                f"Dokumentet '{lovdata_id}' finns inte i den lokala Lovdata-cachen. "
                "Kontrollera identifieraren (t.ex. 'NL/lov/2005-06-17-62', "
                "'LOV-2005-06-17-62' eller 'LTI/lov/2026-01-23-1'), sök med "
                "nor_sok_lovdata, eller kör lovdata_sync.py om cachen är tom."
            )

        utdrag = _begransa_text(rad[6], max_tecken, fran_tecken)

        return {
            "lovdata_id":  rad[0],
            "beteckning":  rad[1],
            "tittel":      rad[2],
            "dok_type":    rad[3],
            "dato":        str(rad[4]) if rad[4] else None,
            "url":         rad[5],
            "fulltext_md":          utdrag["text"] if rad[6] else None,
            "tecken_totalt":        utdrag["tecken_totalt"],
            "tecken_visade":        utdrag["tecken_visade"],
            "trunkerad":            utdrag["trunkerad"],
            "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
            **({"endrer": (rad[7] or "").split(), "ikraft": rad[8]}
               if rad[3] == "lovtidend" else {}),
            **({"las_vidare": (
                    f'nor_hamta_lovdokument(lovdata_id="{rad[0]}", '
                    f'max_tecken={max_tecken}, '
                    f'fran_tecken={utdrag["fortsatt_fran_tecken"]})')}
               if utdrag["fortsatt_fran_tecken"] is not None else {}),
        }

    except Exception as exc:
        raise _verktygsfel(exc, f"Hämtningen av {lovdata_id} ur cachen") from exc


@mcp.tool(title="Samlad sökning i norska källor", annotations=LASNING_EXTERN)
def nor_sok(
    fraga: str,
    sesjonid: str = "",
    max_treff: int = 10,
) -> SamladSokSvar:
    """
    Samlad sökning över alla norska källor: Stortinget (live API),
    Lovdata (lokal cache) och regjeringen.no (lokal cache).

    Parametrar:
      fraga     — Sökfråga på norska. Komma separerar termer och ger OR mellan
                  dem; flera ord inom en term ger AND — alla orden måste
                  förekomma. Exempel: "klimaendring, utslipp"
      sesjonid  — Begränsa Stortinget-sökning till en session (t.ex. "2024-2025").
                  Lämna tomt för senaste session.
      max_treff — Max antal träffar per källa (standard 10).

    Returnerar:
      stortinget  — Saker från Stortingets live-API
      lovdata     — Matchande gällande lagar och forskrifter från lokal cache
      regjeringen — Proposisjoner och NOU från lokal cache

    Misslyckas en källa bär dess del fältet fel, medan de andra delarna
    svarar som vanligt.

    Norsk Lovtidend ingår inte här; sök den med
    nor_sok_lovdata(dok_type='lovtidend').
    """
    try:
        from db import fts_sok

        resultat: dict = {"fraga": fraga}

        # ── Stortinget (live API) ────────────────────────────────────────────
        try:
            sesjoner = _sesjoner()
            if not sesjonid:
                sesjonid = st.aktuell_sesjonid(sesjoner)
            saker = st.sok_saker(fraga, sesjonid)[:max_treff]
            resultat["stortinget"] = {
                "sesjonid": sesjonid,
                "saker":    saker,
                "antal":    len(saker),
            }
        except Exception as st_exc:
            log.warning("Stortinget-søk misslyckades: %s", st_exc)
            resultat["stortinget"] = {"fel": str(st_exc)}

        # ── Lovdata (lokal FTS-cache) ─────────────────────────────────────────
        try:
            lovdata_treff = fts_sok(
                fraga,
                kilde_filter    = "lovdata",
                dok_type_filter = _GJELDENDE_TYPER,
                max_treff       = max_treff,
            )
            resultat["lovdata"] = {
                "treff": lovdata_treff,
                "antal": len(lovdata_treff),
            }
        except Exception as lov_exc:
            log.warning("Lovdata-søk misslyckades: %s", lov_exc)
            resultat["lovdata"] = {"fel": str(lov_exc)}

        # ── regjeringen.no (lokal cache) ──────────────────────────────────────
        try:
            reg_treff = fts_sok(
                fraga,
                kilde_filter = "regjeringen.no",
                max_treff    = max_treff,
            )
            resultat["regjeringen"] = {
                "treff": reg_treff,
                "antal": len(reg_treff),
            }
        except Exception as reg_exc:
            log.warning("Regjeringen-søk misslyckades: %s", reg_exc)
            resultat["regjeringen"] = {"fel": str(reg_exc)}

        return resultat

    except Exception as exc:
        raise _verktygsfel(exc, "Den samlade sökningen") from exc


@mcp.tool(title="Sök inom ett cachat dokument", annotations=LASNING_DB)
def nor_sok_i_dokument(
    lovdata_id: str,
    fraga: str,
    max_treff: int = 10,
    max_tecken: int = 1500,
) -> SokIDokumentSvar:
    """
    Söker inom ett specifikt cachat dokument och returnerar matchande avsnitt.

    Söker i alla cachade källor — Lovdata (gällande texter och Lovtidend),
    Stortinget och regjeringen.no. Svaret visar dokumentets dok_type. En
    beteckning eller titeldel som finns både som gällande text och i
    Lovtidend ger den gällande texten; Lovtidend-versionen nås med sitt
    LTI-id.

    Parametrar:
      lovdata_id — Dokumentets identifierare. Godtar Lovdata-id
                   ('NL/lov/2005-05-20-28'), beteckning ('LOV-2005-05-20-28',
                   'Prop. 132 L (2022-2023)'), publikasjonid eller en del av
                   dokumentets titel.
      fraga      — Vad du söker efter. Kommaseparerade termer = OR mellan dem;
                   flera ord inom en term = AND (alla orden måste förekomma).
      max_treff  — Max antal matchande avsnitt att returnera (standard 10).
      max_tecken — Teckentak per träff (standard 1500, 0 = hela avsnittet upp
                   till serverns övre tak).

    Returnerar:
      Lista med matchande avsnitt ur fulltext_md, med rubrik och text.
      Varje träff visar vilka termer som matchade och om texten är trunkerad.

    OBS: söker bara i dokument som redan finns i den lokala cachen. Ett
    Stortinget-dokument hamnar där när det hämtats med nor_hamta_dokument;
    Lovdata-dokument kommer via den dagliga synken.
    """
    import re

    try:
        from db import _cursor, _ph, _prefix

        # Slå upp brett — kilde-filtret som fanns här tidigare gjorde att
        # verktyget bara hittade Lovdata-dokument, trots att dokumentationen
        # utlovade Stortinget-beteckningar och titeldelar.
        with _cursor() as cur:
            cur.execute(
                f"""
                SELECT lovdata_id, beteckning, tittel, fulltext_md, kilde,
                       publikasjonid, url, dok_type
                FROM   {_prefix()}dokument
                WHERE  lovdata_id    = {_ph()}
                   OR  beteckning    = {_ph()}
                   OR  publikasjonid = {_ph()}
                   OR  tittel     LIKE {_ph()}
                ORDER BY (fulltext_md IS NOT NULL) DESC,
                         {_LOVTIDEND_SIST},
                         length(coalesce(fulltext_md, '')) DESC
                LIMIT 1
                """,
                (lovdata_id, lovdata_id, lovdata_id, f"%{lovdata_id}%")
            )
            rad = cur.fetchone()

        if not rad:
            raise ToolError(
                f"'{lovdata_id}' finns inte i den lokala cachen. Kontrollera "
                "identifieraren, eller hämta dokumentet först: Stortinget-"
                "dokument med nor_hamta_dokument, proposisjoner och NOU med "
                "nor_hamta_regjeringen. Lovdata-dokument kommer via den "
                "dagliga synken."
            )

        dok_id, beteckning, dok_tittel, fulltext, kilde, pub_id, url, dok_typ = rad
        fulltext   = fulltext or ""
        dok_tittel = dok_tittel or ""

        if not fulltext:
            # Skilj "okänd identifierare" från "finns men saknar text" —
            # felmeddelandet ska visa vägen framåt.
            raise ToolError(
                f"Dokumentet '{dok_tittel or lovdata_id}' ({kilde}) finns i cachen "
                "men har ingen extraherad fulltext. För regjeringen.no-dokument "
                "kan PDF-extraktionen ha misslyckats — kör "
                f"nor_hamta_regjeringen(url='{url}') för att försöka igen."
            )

        # Dela upp i avsnitt (paragrafer avgränsas av ### i Markdown)
        termer    = [t.strip().lower() for t in fraga.split(",") if t.strip()]
        sektioner = re.split(r"\n(?=###\s)", fulltext)

        def _matchar(text_lower: str) -> list:
            """OR mellan kommaseparerade termer, AND mellan ord inom en term."""
            return [
                t for t in termer
                if all(o in text_lower for o in t.split() if o)
            ]

        treff = []
        for seksjon in sektioner:
            matchade = _matchar(seksjon.lower())
            if not matchade:
                continue
            rubrik_m = re.match(r"###\s+(.+?)(?:\n|$)", seksjon)
            rubrik   = rubrik_m.group(1).strip() if rubrik_m else ""
            ren_text = re.sub(r"\*[^*]+\*", "", seksjon).strip()
            utdrag   = _begransa_text(ren_text, max_tecken)
            treff.append({
                "rubrik":               rubrik,
                "text":                 utdrag["text"],
                "matchade_termer":      matchade,
                "tecken_totalt":        utdrag["tecken_totalt"],
                "trunkerad":            utdrag["trunkerad"],
                "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
            })
            if len(treff) >= max_treff:
                break

        return {
            "identifierare": lovdata_id,
            "lovdata_id":    dok_id,
            "beteckning":    beteckning,
            "tittel":        dok_tittel,
            "kilde":         kilde,
            "dok_type":      dok_typ,
            "publikasjonid": pub_id,
            "url":           url,
            "fraga":         fraga,
            "antal":         len(treff),
            "avsnitt_totalt": len(sektioner),
            "treff":         treff,
        }

    except Exception as exc:
        raise _verktygsfel(exc, f"Sökningen i {lovdata_id}") from exc


def _hamta_regjeringen_fra_db(url: str) -> Optional[dict]:
    """
    Returnerar cachat regjeringen-dokument från norge.dokument om det finns
    med fulltext, eller None. Försöker matcha både den exakta inkommande
    URL:en och dess normaliserade form (https://www.regjeringen.no/...).

    Används av nor_hamta_regjeringen för att undvika dyr PDF-extraktion och
    OCR vid återkommande anrop.
    """
    try:
        from db import _cursor, _ph, _prefix
        norm_url = rg._normaliser_url(url)

        sql = f"""
            SELECT dok_type, beteckning, tittel, url, fulltext_md
            FROM   {_prefix()}dokument
            WHERE  kilde = {_ph()}
              AND  (publikasjonid IN ({_ph()}, {_ph()})
                    OR url        IN ({_ph()}, {_ph()}))
            LIMIT 1
        """
        with _cursor() as cur:
            cur.execute(sql, ("regjeringen.no", url, norm_url, url, norm_url))
            rad = cur.fetchone()

        if not rad or not rad[4]:
            return None
        return {
            "dok_type":    rad[0],
            "beteckning":  rad[1],
            "tittel":      rad[2],
            "url":         rad[3],
            "fulltext_md": rad[4],
        }
    except Exception as exc:
        log.debug("_hamta_regjeringen_fra_db misslyckades (%s): %s", url, exc)
        return None


def _regjeringen_svar(
    data: dict, kalla: str, url: str, max_tecken: int, fran_tecken: int
) -> "RegjeringenSvar":
    """Bygger svaret med ett begränsat textutdrag och en läs vidare-rad."""
    utdrag = _begransa_text(data.get("fulltext_md"), max_tecken, fran_tecken)
    svar: RegjeringenSvar = {
        "tittel":               data.get("tittel"),
        "beteckning":           data.get("beteckning"),
        "dok_type":             data.get("dok_type"),
        "url":                  data.get("url") or url,
        "pdf_url":              data.get("pdf_url"),
        "fulltext_md":          utdrag["text"],
        "kalla":                kalla,
        "tecken_antal":         utdrag["tecken_visade"],
        "tecken_totalt":        utdrag["tecken_totalt"],
        "trunkerad":            utdrag["trunkerad"],
        "fortsatt_fran_tecken": utdrag["fortsatt_fran_tecken"],
        "fel":                  None,
    }
    if utdrag["fortsatt_fran_tecken"] is not None:
        svar["las_vidare"] = (
            f'nor_hamta_regjeringen(url="{url}", max_tecken={max_tecken}, '
            f'fran_tecken={utdrag["fortsatt_fran_tecken"]})'
        )
    return svar


@mcp.tool(title="Hämta dokument från regjeringen.no", annotations=LASNING_EXTERN)
def nor_hamta_regjeringen(
    url: str,
    spara_i_db: bool = True,
    max_tecken: int = NOR_MAX_TECKEN,
    fran_tecken: int = 0,
) -> RegjeringenSvar:
    """
    Hämtar en proposisjon, NOU eller Meld. St. direkt från regjeringen.no.

    Strategi: kollar först om dokumentet redan finns i lokal DB-cache med
    fulltext. Om så returneras det omedelbart (millisekunder). Om inte hämtas
    det live via download → PDF-extraktion under minnes- och tidsvakt, med
    OCR (nor+eng) för sidor utan textlager, varefter resultatet cachas i DB
    för framtida anrop.

    Parametrar:
      url       — URL till dokumentet på regjeringen.no. Accepterar:
                    - Kortlänk:      regjeringen.no/id/<KORTLÄNK_ID>
                    - Fullständig:   regjeringen.no/no/dokumenter/<DOK_SLUG>/id<DOC_ID>/
                    - Protokollrelativ: //www.regjeringen.no/id/...
      spara_i_db — Spara extraherad text i lokal databas (standard: true).
                   Sätt false bara för engångsanvändning där cachning inte är önskvärt.
      max_tecken — Teckentak för fulltext_md (standard 60 000; 0 = upp till
                   serverns övre tak på 200 000 tecken). En proposisjon kan
                   vara flera hundra tusen tecken.
      fran_tecken — Börja texten vid denna teckenposition. Skicka värdet ur
                   fortsatt_fran_tecken för att läsa vidare. Databasen får
                   alltid hela texten; taket gäller bara svaret.

    KÄLLA — hur man hittar URL:en per dokumenttyp:
      Proposisjon (Prop.)
        URL:en finns i Stortingets sak-objekt. Hämta saken med nor_hamta_dokument
        och läs fältet "regjeringen_url" i metadata. Proposisjoner distribueras
        inte av Stortingets API utan pekar alltid till regjeringen.no.
      Meld. St. (Stortingsmelding)
        Samma mönster som Prop. — URL:en fås via nor_hamta_dokument.
      NOU (Norges offentlige utredninger)
        NOU:er länkas INTE från Stortingets sak-objekt (publikasjon_referanse_liste
        innehåller bara Prop.-URL:er, inte NOU). URL:en måste anges direkt.
        Format: https://www.regjeringen.no/no/dokumenter/nou-<ÅR>-<NR>/id<DOC_ID>/
        Hitta aktuella URL:er via regjeringen.no:s dokumentarkiv eller sök på webben —
        ID-talet är specifikt per dokument och kan inte gissas.

    Returnerar:
      tittel       — Dokumentets titel
      beteckning   — Formell beteckning (t.ex. "Prop. 165 L (2024-2025)")
      dok_type     — 'proposisjon', 'nou' eller 'meld_st'
      url          — Slutlig URL efter omdirigeringar
      pdf_url      — URL till PDF (None om hämtad från cache; tillgänglig vid live-hämtning)
      fulltext_md  — Extraherad text i Markdown-format (utdraget)
      kalla        — 'db_cache' om hämtad från lokal databas, 'live' om hämtad nyss
      tecken_antal — Antal tecken i fulltext_md (utdraget)
      tecken_totalt, trunkerad, fortsatt_fran_tecken — hela textens längd,
                     om utdraget är kapat och var nästa utdrag börjar (null
                     när texten är slut)
      las_vidare   — komplett anrop för nästa utdrag, när texten är kapad
      fel          — null vid framgång

    Går dokumentet inte att hämta ges ett verktygsfel med orsaken. Det
    gäller också när regjeringen.no blockerar automatiserad åtkomst med en
    Cloudflare-utmaning; meddelandet visar då vilka vägar som fungerar.

    OBS: Vid första hämtningen av ett stort eller bildbaserat dokument kan
    OCR-steget ta flera minuter och slå i MCP-timeouten. Efterföljande
    anrop med samma URL går mot cachen och tar millisekunder.
    """
    try:
        # Strategi 1: returnera från DB-cache om fulltext redan finns
        cached = _hamta_regjeringen_fra_db(url)
        if cached:
            return _regjeringen_svar(
                {**cached, "pdf_url": None}, "db_cache", url, max_tecken, fran_tecken
            )

        # Strategi 2: hämta live (PDF → markdown, OCR vid behov, under minnesvakt)
        data = rg.hamta_og_ekstraher(url)
        if not data.get("fulltext_md"):
            # hamta_og_ekstraher fångar själv sina fel, inklusive botskyddet,
            # och lägger orsaken i fel. Här blir den ett verktygsfel.
            raise ToolError(
                data.get("fel") or f"Ingen text kunde extraheras ur {url}."
            )

        if spara_i_db and data.get("fulltext_md"):
            try:
                from db import upsert_dokument
                upsert_dokument(
                    kilde="regjeringen.no",
                    dok_type=data.get("dok_type", "proposisjon"),
                    beteckning=data.get("beteckning"),
                    tittel=data.get("tittel"),
                    sesjonid=None,
                    dato=None,
                    url=data.get("url", url),
                    publikasjonid=data.get("url", url),
                    sakid=None,
                    lovdata_id=None,
                    fulltext_md=data.get("fulltext_md"),
                )
            except Exception as db_exc:
                log.warning("Databasskrivning (nor_hamta_regjeringen) misslyckades: %s", db_exc)

        return _regjeringen_svar(data, "live", url, max_tecken, fran_tecken)

    except Exception as exc:
        raise _verktygsfel(exc, f"Hämtningen från regjeringen.no ({url})") from exc


_VEDTAK_LISTFALT = ("id", "nummer", "sak_id", "dato", "tittel", "vedtakstype")


@mcp.tool(title="Hämta stortingsvedtak", annotations=LASNING_EXTERN)
def nor_hamta_vedtak(
    sesjonid: str = "",
    vedtakid: str = "",
    med_fulltext: bool = False,
) -> VedtakSvar:
    """
    Hämtar stortingsvedtak (parlamentariska beslut).

    Listar alla vedtak i en session, eller hämtar ett enskilt vedtak med
    beslutstext.

    Parametrar:
      sesjonid     — Sessions-ID (t.ex. "2024-2025"). Tomt = innevarande
                     session. Gäller både listning och uppslag av vedtakid.
      vedtakid     — ID för ett enskilt vedtak (t.ex. "40030102"). Vedtaket
                     slås upp i sessionen ovan; Stortinget har inget uppslag
                     direkt på vedtakid. Ligger vedtaket i en annan session
                     än den innevarande måste sesjonid anges.
      med_fulltext — Vid listning: ta med varje vedtaks beslutstext. Texterna
                     följer med i samma svar från källan, så det kostar inga
                     extra anrop, men en hel session kan ha tusen vedtak och
                     flera hundra tusen tecken text. Svaret tar därför med text
                     upp till ett samlat teckentak; vedtak därefter får
                     fulltext_utelamnad=True och hämtas enskilt med vedtakid.
                     Standard: False.

    Returnerar:
      vid listning: sesjonid, antal och vedtak_liste (id, nummer, sak_id,
                    dato, tittel, vedtakstype och vid med_fulltext
                    fulltext_md)
      vid vedtakid: vedtakid, sesjonid och vedtak (samma fält plus
                    vedtakstype_navn, url, sak_url och fulltext_md med hela
                    beslutstexten)
    """
    try:
        if not sesjonid:
            sesjonid = st.aktuell_sesjonid(_sesjoner())

        if vedtakid:
            vedtak = st.hamta_vedtak(vedtakid, sesjonid)
            if vedtak is None:
                raise ToolError(
                    f"Vedtak {vedtakid} finns inte bland vedtaken i session "
                    f"{sesjonid}. Stortinget har inget uppslag direkt på "
                    f"vedtakid, så vedtaket söks bara i en session åt gången. "
                    f"Ange sesjonid för den session vedtaket fattades i."
                )
            vedtak["fulltext_md"] = vedtak.pop("vedtakstekst", "")
            return {"vedtakid": vedtakid, "sesjonid": sesjonid, "vedtak": vedtak}

        # En session kan ha över tusen vedtak. Listan bär därför bara de fält
        # som behövs för att välja vedtak; länkar och typnamn finns i
        # uppslaget på vedtakid. Utan den gallringen närmar sig svaret MCP:s
        # storleksgräns redan utan beslutstexter.
        vedtak_liste = [
            {k: v[k] for k in _VEDTAK_LISTFALT} | {"vedtakstekst": v["vedtakstekst"]}
            for v in st.hamta_vedtak_liste(sesjonid)
        ]
        budget = NOR_MAX_TECKEN
        utelamnade = 0
        for v in vedtak_liste:
            tekst = v.pop("vedtakstekst", "")
            if not med_fulltext:
                continue
            if len(tekst) <= budget:
                v["fulltext_md"] = tekst
                budget -= len(tekst)
            else:
                v["fulltext_utelamnad"] = True
                utelamnade += 1

        svar = {
            "sesjonid":     sesjonid,
            "antal":        len(vedtak_liste),
            "vedtak_liste": vedtak_liste,
        }
        if utelamnade:
            svar["notat"] = (
                f"Beslutstexten är utelämnad för {utelamnade} av "
                f"{len(vedtak_liste)} vedtak (samlat teckentak "
                f"{NOR_MAX_TECKEN}). Hämta dem enskilt med "
                f"nor_hamta_vedtak(vedtakid=..., sesjonid='{sesjonid}')."
            )
        return svar

    except Exception as exc:
        raise _verktygsfel(exc, "Hämtningen av vedtak") from exc


@mcp.tool(title="Hämta høringsinnspill", annotations=LASNING_EXTERN)
def nor_hamta_horinginnspill(
    horingid: str,
    med_fulltext: bool = True,
    max_tecken: int = 4000,
) -> InnspillSvar:
    """
    Hämtar skriftliga innspill (remissvar) till en høring (utskottsutfrågning).

    Ger civila samhällets och organisationers synpunkter på lagstiftningsärenden.

    Parametrar:
      horingid     — Høringens ID-nummer (hämtas via nor_sok_stortinget med
                     typer='horinger').
      med_fulltext — Ta med varje innspills text (standard: True). Sätt False
                     för att bara få avsändare och rubriker — ett litet svar
                     när du bara vill se vilka som yttrat sig.
      max_tecken   — Teckentak per innspill (standard 4000, 0 = hela texten upp
                     till serverns övre tak).
                     En høring kan ha tjugo innspill på flera tusen tecken var.

    Returnerar:
      Lista med innspill: id, tittel, avsender, dato och (med med_fulltext)
      fulltext_md samt trunkeringsfälten.

    Fulltexten ingår i samma svar från Stortinget — inget extra anrop per
    innspill behövs, och därför kostar med_fulltext=True ingen extra tid.
    """
    try:
        try:
            innspill = st.hamta_skriftlige_innspill(horingid)
        except st.StortingetFel as exc:
            # Källan svarar likadant för en høring utan godkända innspill
            # och för ett okänt ID. Det första är vanligt (muntliga høringer),
            # så svaret blir en tom lista med förklaring, inte ett fel.
            return {"horingid": horingid, "antal": 0, "innspill": [], "notat": str(exc)}

        for item in innspill:
            hel_text = item.pop("fulltext_md", "") or ""
            if med_fulltext:
                utdrag = _begransa_text(hel_text, max_tecken)
                item["fulltext_md"]          = utdrag["text"]
                item["tecken_totalt"]        = utdrag["tecken_totalt"]
                item["trunkerad"]            = utdrag["trunkerad"]
                item["fortsatt_fran_tecken"] = utdrag["fortsatt_fran_tecken"]
            else:
                item["tecken_totalt"] = len(hel_text)

        svar = {
            "horingid": horingid,
            "antal":    len(innspill),
            "innspill": innspill,
        }
        if not innspill:
            svar["notat"] = (
                "Høringen har inga godkända skriftliga innspill registrerade "
                "hos Stortinget."
            )
        return svar

    except Exception as exc:
        raise _verktygsfel(exc, f"Hämtningen av innspill till høring {horingid}") from exc


@mcp.tool(title="Lista Stortingets ämnen", annotations=LASNING_EXTERN)
def nor_lista_emner() -> EmnerSvar:
    """
    Hämtar Stortingets ämnesklassificering (ca 250 ämnen i 2-nivåhierarki).

    Nyttigt för att förstå vilka emne-värden som kan användas som filter i
    nor_sok_stortinget (parametern emne).

    Returnerar lista med alla ämnen: id, navn, forelder_id.
    Toppnivåämnen har tomt forelder_id.

    Exempel på hierarki:
      ARBEIDSLIV → ARBEIDSMILJØ, LØNNSFORHOLD, TARIFFAVTALER
      HELSE → FOLKEHELSE, SYKEHUS, LEGEMIDLER
    """
    try:
        emner = st.hamta_emner()
        # Bygg hierarkisk struktur för bättre läsbarhet
        toppnivaa  = [e for e in emner if not e["forelder_id"]]
        undernivaa = [e for e in emner if e["forelder_id"]]
        return {
            "antal":      len(emner),
            "toppnivaa":  toppnivaa,
            "undernivaa": undernivaa,
        }
    except Exception as exc:
        raise _verktygsfel(exc, "Hämtningen av ämnen") from exc


@mcp.tool(title="Semantisk sökning", annotations=LASNING_DB)
def nor_sok_semantisk(
    fraga: str,
    kilde: str = "alla",
    max_treff: int = 10,
    dok_type: str = "alla",
) -> SemantiskSvar:
    """
    Semantisk sökning i norsk cached text med pgvector (cosinuslikhet).

    Kräver PostgreSQL + pgvector och att nor_embedding.py har körts för att
    generera vektorer. Returnerar de mest semantiskt likartade styckena ur
    cachen — användbart för konceptuella frågor där exakt nyckelordssökning
    missar relevanta stycken.

    Parametrar:
      fraga   — Fråga eller beskrivning på norska (bokmål/nynorsk) eller svenska.
                Om QUERY_EXPANSION_ENABLED=true expanderas frågan automatiskt
                till norsk juridisk terminologi via LLM.
      kilde   — Begränsa till datakälla: 'lovdata', 'stortinget',
                'regjeringen.no' eller 'alla' (standard).
      max_treff — Max antal träffar (standard 10).
      dok_type — Begränsa till dokumenttyp. 'alla' (standard) söker i allt
                utom Norsk Lovtidend, så att ändringslagar inte tränger undan
                gällande rätt och Stortingets dokument. 'lovtidend' söker
                bara i Lovtidend, 'alla_med_lovtidend' i allt. Andra värden
                matchar dok_type exakt, t.ex. 'lov', 'forskrift',
                'innstilling_ny', 'proposisjon'.

    Returnerar:
      Lista med matchande textstycken (rubrik, text, likhet 0–1,
      källdokumentets metadata inklusive dok_type).
    """
    try:
        from db import vektor_sok, _ar_postgres

        if not _ar_postgres():
            raise ToolError(
                "Semantisk sökning kräver PostgreSQL med pgvector; servern kör "
                "mot SQLite. Använd nor_sok eller nor_sok_lovdata (fulltext)."
            )

        # Query-expansion (valfritt)
        extra = expandera_fraga(fraga)
        sok_text = fraga + (", " + ", ".join(extra) if extra else "")

        modell = _hamta_embedding_modell()
        embedding = modell.encode(
            sok_text,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()

        kilde_filter = None if kilde == "alla" else kilde
        if dok_type == "alla":
            typfilter = {"utom_dok_typer": ["lovtidend"]}
        elif dok_type == "alla_med_lovtidend":
            typfilter = {}
        else:
            typfilter = {"dok_typer": [dok_type]}
        treff = vektor_sok(
            embedding, kilde_filter=kilde_filter, max_treff=max_treff, **typfilter
        )

        svar = {
            "fraga":     fraga,
            "expansion": extra,
            "kilde":     kilde,
            "dok_type":  dok_type,
            "antal":     len(treff),
            "treff":     treff,
        }

        # Ett nollresultat kan betyda två helt olika saker: att inget matchar,
        # eller att den valda källan aldrig embeddats. Utan den skillnaden
        # framstår en oindexerad källa som en tom källa.
        if not treff:
            from db import vektor_tackning
            tackning = vektor_tackning()
            svar["diagnostik"] = {"vektor_tackning_per_kalla": tackning}

            if kilde != "alla":
                kalla_stat = tackning.get(kilde)
                if kalla_stat is None:
                    svar["diagnostik"]["orsak"] = (
                        f"Källan '{kilde}' har inga dokument alls i den lokala cachen."
                    )
                elif not kalla_stat["chunks_med_vektor"]:
                    svar["diagnostik"]["orsak"] = (
                        f"Källan '{kilde}' har {kalla_stat['dokument']} dokument i "
                        f"cachen men inga embeddings — semantisk sökning kan därför "
                        f"aldrig ge träffar här. Kör nor_embedding.py --kilde "
                        f"{kilde}. Använd nor_sok eller nor_sok_lovdata under tiden "
                        f"(fulltextsökning kräver inga vektorer)."
                    )
                else:
                    svar["diagnostik"]["orsak"] = (
                        f"Källan '{kilde}' har {kalla_stat['chunks_med_vektor']} "
                        f"embeddade textstycken — nollresultatet beror på att inget "
                        f"matchade frågan, inte på att index saknas."
                    )
            elif not any(v["chunks_med_vektor"] for v in tackning.values()):
                svar["diagnostik"]["orsak"] = (
                    "Inga embeddings finns i databasen. Kör nor_embedding.py "
                    "--kilde alla."
                )

        return svar

    except Exception as exc:
        raise _verktygsfel(exc, "Den semantiska sökningen") from exc


# ── Startpunkt ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    starta(
        mcp,
        standardport=8003,
        initiera=initiera_schema,
        forvarm_http=_forvarm_http,
    )
