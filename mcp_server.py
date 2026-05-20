# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
mcp_server.py — MCP-server för norsk riksdags- och rättsdata

Exponerar följande verktyg till MCP-kompatibla AI-verktyg:

  nor_lista_sesjoner      — Listar alla Stortingssesjoner (1986-87 och framåt)
  nor_sok_stortinget      — Söker saker, spørsmål och høringer i Stortinget
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
  regjeringen  — proposisjoner och NOU som PDF (PDF-extraktion + OCR)

Transport-lägen (styrs via MCP_TRANSPORT i .env):

  stdio (lokal användning):
    python3 mcp_server.py
    MCP-klienten startar och hanterar processen direkt.

  http (hostad driftsättning):
    MCP_TRANSPORT=http python3 mcp_server.py
    Servern lyssnar på MCP_HOST:MCP_PORT (standard 127.0.0.1:8003).
    Sätt MCP_API_KEY för Bearer-token-autentisering.

Konfiguration via .env (se config.example.env).
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import stortinget as st
import regjeringen as rg
from db import initiera_schema

load_dotenv(Path(__file__).parent / ".env")

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST      = os.getenv("MCP_HOST",      "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT",  "8003"))
MCP_API_KEY   = os.getenv("MCP_API_KEY",   "")

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
_embedding_modell = None   # Laddas vid första semantisk sökning

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "norge",
    instructions=(
        "MCP-server för norsk riksdags- och rättsdata. "
        "Täcker Stortinget (1986-87–idag), norska lagar och föreskrifter (Lovdata), "
        "samt proposisjoner och NOU från regjeringen.no. "
        "Verktygen har prefixet nor_. "
        "Söktermen kan innehålla kommaseparerade ord — de tolkas som OR-logik. "
        "nor_hamta_vedtak ger parlamentariska beslutstexter. "
        "nor_hamta_horinginnspill ger skriftliga remissvar till høringer. "
        "nor_lista_emner ger Stortingets ämnesklassificering."
    ),
)

# Cachad sessionsrespons (hämtas vid behov, återanvänds)
_sesjoner_cache: Optional[dict] = None


def _sesjoner() -> dict:
    global _sesjoner_cache
    if _sesjoner_cache is None:
        _sesjoner_cache = st.hamta_sesjoner()
    return _sesjoner_cache


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


def _hamta_embedding_modell():
    """Laddar embeddingmodellen vid behov (lat laddning, FD1-skyddad)."""
    global _embedding_modell
    if _embedding_modell is None:
        import os as _os
        log_path = _SCRIPT_DIR / "logs" / "embedding.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd   = _os.open(str(log_path), _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT)
        save_fd1 = _os.dup(1)
        try:
            _os.dup2(log_fd, 1)
            from sentence_transformers import SentenceTransformer
            _embedding_modell = SentenceTransformer(EMBEDDING_MODEL)
        finally:
            _os.dup2(save_fd1, 1)
            _os.close(save_fd1)
            _os.close(log_fd)
        log.info("Embeddingmodell laddad: %s", EMBEDDING_MODEL)
    return _embedding_modell


# ── Verktyg ────────────────────────────────────────────────────────────────────


@mcp.tool()
def nor_lista_sesjoner() -> dict:
    """
    Listar alla tillgängliga Stortingssesjoner (43 st, 1986-87 och framåt).

    Returnerar en lista med sessions-ID, namn och datum.
    Använd sessions-ID (t.ex. "2024-2025") som indata till nor_sok_stortinget.
    """
    try:
        return _sesjoner()
    except Exception as exc:
        log.error("nor_lista_sesjoner misslyckades: %s", exc)
        return {"fel": str(exc)}


@mcp.tool()
def nor_sok_stortinget(
    fraga: str,
    sesjonid: str = "",
    typer: str = "saker,sporsmal,horinger",
    dokumentgruppe: str = "",
    emne: str = "",
    sak_status: str = "",
    max_treff: int = 10,
) -> dict:
    """
    Söker i Stortingets data för en given session.

    Parametrar:
      fraga          — Sökfråga. Kommaseparerade termer = OR-logik.
                       Exempel: "skatt, avgift" hittar om skatt ELLER avgift.
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
        log.error("nor_sok_stortinget misslyckades: %s", exc)
        return {"fel": str(exc), "fraga": fraga, "sesjonid": sesjonid}


@mcp.tool()
def nor_hamta_dokument(
    id: str,
    id_typ: str = "sakid",
    sesjonid: str = "",
    spara_i_db: bool = True,
) -> dict:
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

    Returnerar:
      metadata   — Sakens metadata (tittel, status, emner, publikasjoner)
      dokument   — Lista av hämtade publikasjoner med fulltext_md
      fel        — Eventuellt felmeddelande

    OBS: Proposisjoner distribueras INTE av Stortingets API utan hämtas direkt
    från regjeringen.no via nor_hamta_regjeringen. Använd den funktionen med
    URL:en ur sak-objektets publikasjon_referanse_liste.
    """
    try:
        dokument_lista = []

        if id_typ == "sakid":
            # Hämta sak-metadata och dess publikasjoner
            sak = st.hamta_sak(id)
            if not sesjonid:
                sesjonid = sak.get("sesjonid", "")

            for pub_ref in sak.get("publikasjoner", []):
                pub_id  = pub_ref.get("eksport_id", "")
                pub_url = pub_ref.get("lenke_url", "")

                # Proposisjoner och stortingsmeldinger pekar på regjeringen.no
                if "regjeringen.no" in pub_url or not pub_id:
                    if pub_url:
                        log.info("Hämtar regjeringen.no-dokument: %s", pub_url)
                        rg_data = rg.hamta_og_ekstraher(pub_url)
                        dokument_lista.append({
                            "typ":         rg_data.get("dok_type", "ekstern"),
                            "kilde":       "regjeringen.no",
                            "url":         rg_data.get("url", pub_url),
                            "pdf_url":     rg_data.get("pdf_url"),
                            "tittel":      rg_data.get("tittel", ""),
                            "beteckning":  rg_data.get("beteckning", ""),
                            "fulltext_md": rg_data.get("fulltext_md"),
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
                dokument_lista.append({
                    "publikasjonid": pub_id,
                    "tittel":        parsed["tittel"],
                    "typ":           parsed["typ"],
                    "metadata":      parsed["metadata"],
                    "fulltext_md":   parsed["fulltext_md"],
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

            return {
                "sakid":    id,
                "sesjonid": sesjonid,
                "sak":      sak,
                "dokument": dokument_lista,
            }

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

            return {
                "publikasjonid": id,
                "sesjonid":      sesjonid,
                "dokument":      [parsed],
            }

        else:
            return {"fel": f"Okänd id_typ: '{id_typ}'. Använd 'sakid' eller 'publikasjonid'."}

    except Exception as exc:
        log.error("nor_hamta_dokument misslyckades (id=%s, typ=%s): %s", id, id_typ, exc)
        return {"fel": str(exc), "id": id, "id_typ": id_typ}


@mcp.tool()
def nor_sok_lovdata(
    fraga: str,
    dok_type: str = "alla",
    max_treff: int = 10,
) -> dict:
    """
    Söker i den lokala Lovdata-cachen (gällande norska lagar och forskrifter).

    Parametrar:
      fraga     — Sökfråga. Kommaseparerade termer tolkas som OR-logik.
                  Exempel: "arbeidsmiljø, oppsigelse" söker båda termerna.
      dok_type  — Filtrera på dokumenttyp: 'lov', 'forskrift' eller 'alla'.
                  Standard: alla.
      max_treff — Max antal träffar (standard 10).

    Returnerar:
      En lista med matchande dokument (lovdata_id, beteckning, tittel,
      korttittel, dato, url) sorterade efter relevans.

    OBS: Cachen uppdateras via lovdata_sync.py (daglig synk). Om cachen är
    tom returneras ett tomt resultat — kör synkskriptet först.
    Sökningen är fulltextsökning i tittel + fulltext_md.
    """
    try:
        from db import fts_sok

        typ_filter = dok_type if dok_type in ("lov", "forskrift") else None
        rader = fts_sok(
            fraga,
            kilde_filter    = "lovdata",
            dok_type_filter = typ_filter,
            max_treff       = max_treff,
        )

        treff = [
            {
                "lovdata_id": r["lovdata_id"],
                "beteckning": r["beteckning"],
                "tittel":     r["tittel"],
                "dok_type":   r["dok_type"],
                "dato":       r["dato"],
                "url":        r["url"],
                "rank":       r["rank"],
            }
            for r in rader
        ]

        return {
            "fraga": fraga,
            "antal": len(treff),
            "treff": treff,
        }

    except Exception as exc:
        log.error("nor_sok_lovdata misslyckades: %s", exc)
        return {"fel": str(exc), "fraga": fraga}


@mcp.tool()
def nor_hamta_lovdokument(lovdata_id: str, max_tecken: int = 0) -> dict:
    """
    Hämtar fulltext och metadata för ett Lovdata-dokument ur lokal cache.

    Parametrar:
      lovdata_id — Lovdatas dokumentidentifierare, t.ex. 'NL/lov/2005-05-20-28'
                   eller beteckning 'LOV-2005-05-20-28'.
      max_tecken — Trunkera fulltext_md till detta antal tecken (0 = ingen trunkering).
                   Rekommenderat för stora dokument (föreskrifter kan vara 100 000+
                   tecken). Exempel: max_tecken=20000 för en inledande läsning.

    Returnerar:
      Metadata + fulltext_md för dokumentet.
      fulltext_md är null om dokumentet inte finns i cachen — synka först.
      Om max_tecken är satt inkluderas fältet trunkerad=true om texten kapades.
    """
    try:
        from db import _cursor, _ph, _prefix

        # Stöd både lovdata_id och beteckning som indata
        with _cursor() as cur:
            cur.execute(
                f"""
                SELECT lovdata_id, beteckning, tittel, dok_type, dato, url, fulltext_md
                FROM   {_prefix()}dokument
                WHERE  kilde = {_ph()}
                  AND  (lovdata_id = {_ph()} OR beteckning = {_ph()})
                LIMIT 1
                """,
                ("lovdata", lovdata_id, lovdata_id)
            )
            rad = cur.fetchone()

        if not rad:
            return {
                "fel": f"Dokumentet '{lovdata_id}' finns inte i cachen. "
                       "Kör lovdata_sync.py för att synka.",
                "lovdata_id": lovdata_id,
            }

        fulltext = rad[6]
        trunkerad = False
        if max_tecken and max_tecken > 0 and fulltext and len(fulltext) > max_tecken:
            fulltext  = fulltext[:max_tecken]
            trunkerad = True

        return {
            "lovdata_id":  rad[0],
            "beteckning":  rad[1],
            "tittel":      rad[2],
            "dok_type":    rad[3],
            "dato":        str(rad[4]) if rad[4] else None,
            "url":         rad[5],
            "fulltext_md": fulltext,
            "trunkerad":   trunkerad,
        }

    except Exception as exc:
        log.error("nor_hamta_lovdokument misslyckades (id=%s): %s", lovdata_id, exc)
        return {"fel": str(exc), "lovdata_id": lovdata_id}


@mcp.tool()
def nor_sok(
    fraga: str,
    sesjonid: str = "",
    max_treff: int = 10,
) -> dict:
    """
    Samlad sökning över alla norska källor: Stortinget (live API),
    Lovdata (lokal cache) och regjeringen.no (lokal cache).

    Parametrar:
      fraga     — Sökfråga på norska. Kommaseparerade termer = OR-logik.
                  Exempel: "klimaendring, utslipp"
      sesjonid  — Begränsa Stortinget-sökning till en session (t.ex. "2024-2025").
                  Lämna tomt för senaste session.
      max_treff — Max antal träffar per källa (standard 10).

    Returnerar:
      stortinget  — Saker från Stortingets live-API
      lovdata     — Matchande lagar och forskrifter från lokal cache
      regjeringen — Proposisjoner och NOU från lokal cache
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
                kilde_filter = "lovdata",
                max_treff    = max_treff,
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
        log.error("nor_sok misslyckades: %s", exc)
        return {"fel": str(exc), "fraga": fraga}


@mcp.tool()
def nor_sok_i_dokument(
    lovdata_id: str,
    fraga: str,
    max_treff: int = 10,
) -> dict:
    """
    Söker inom ett specifikt cachat dokument och returnerar matchande paragrafer.

    Parametrar:
      lovdata_id — Dokumentets identifierare, t.ex. 'NL/lov/2005-05-20-28'
                   eller beteckning 'LOV-2005-05-20-28'. Fungerar även med
                   Stortinget-beteckning eller titeldel.
      fraga      — Vad du söker efter. Söker i varje paragrafs text.
      max_treff  — Max antal matchande paragrafer att returnera (standard 10).

    Returnerar:
      Lista med matchande paragrafer ur fulltext_md, med §-nummer och text.
      Nyttigt för att hitta specifika bestämmelser utan att läsa hela lagen.
    """
    import re

    try:
        from db import _cursor, _ph, _prefix

        with _cursor() as cur:
            cur.execute(
                f"""
                SELECT lovdata_id, beteckning, tittel, fulltext_md
                FROM   {_prefix()}dokument
                WHERE  kilde = {_ph()}
                  AND  (lovdata_id = {_ph()} OR beteckning = {_ph()}
                        OR tittel LIKE {_ph()})
                LIMIT 1
                """,
                ("lovdata", lovdata_id, lovdata_id, f"%{lovdata_id}%")
            )
            rad = cur.fetchone()

        if not rad:
            return {
                "fel":        f"'{lovdata_id}' finns inte i cachen.",
                "lovdata_id": lovdata_id,
                "treff":      [],
            }

        fulltext  = rad[3] if rad[3] else ""
        dok_tittel = rad[2] or ""
        dok_id     = rad[0] or ""

        if not fulltext:
            return {
                "lovdata_id": dok_id,
                "tittel":     dok_tittel,
                "fel":        "Dokumentet har ingen extraherad text i cachen.",
                "treff":      [],
            }

        # Dela upp i paragrafer (avgränsas av ### i Markdown)
        termer = [t.strip().lower() for t in fraga.split(",") if t.strip()]
        sektioner = re.split(r"\n(?=###\s)", fulltext)

        treff = []
        for seksjon in sektioner:
            seksjon_lower = seksjon.lower()
            if any(t in seksjon_lower for t in termer):
                # Extrahera §-nummer ur rubriken
                rubrik_m = re.match(r"###\s+(.+?)(?:\n|$)", seksjon)
                rubrik   = rubrik_m.group(1).strip() if rubrik_m else ""
                # Rensa Markdown-formatering för läsbarhet
                ren_text = re.sub(r"\*[^*]+\*", "", seksjon).strip()
                treff.append({
                    "rubrik": rubrik,
                    "text":   ren_text[:600],   # Begränsa per träff
                })
                if len(treff) >= max_treff:
                    break

        return {
            "lovdata_id": dok_id,
            "tittel":     dok_tittel,
            "fraga":      fraga,
            "antal":      len(treff),
            "treff":      treff,
        }

    except Exception as exc:
        log.error("nor_sok_i_dokument misslyckades (id=%s): %s", lovdata_id, exc)
        return {"fel": str(exc), "lovdata_id": lovdata_id, "treff": []}


def _hamta_regjeringen_fra_db(url: str) -> Optional[dict]:
    """
    Returnerar cachat regjeringen-dokument från norge.dokument om det finns
    med fulltext, eller None. Försöker matcha både den exakta inkommande
    URL:en och dess normaliserade form (https://www.regjeringen.no/...).

    Används av nor_hamta_regjeringen för att undvika dyr PDF-extraktion och
    OCR-fallback vid återkommande anrop.
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


@mcp.tool()
def nor_hamta_regjeringen(url: str, spara_i_db: bool = True) -> dict:
    """
    Hämtar en proposisjon, NOU eller Meld. St. direkt från regjeringen.no.

    Strategi: kollar först om dokumentet redan finns i lokal DB-cache med
    fulltext. Om så returneras det omedelbart (millisekunder). Om inte hämtas
    det live via download → PDF-extraktion → eventuell OCR-fallback för bild-
    baserade PDF:er, varefter resultatet cachas i DB för framtida anrop.

    Parametrar:
      url       — URL till dokumentet på regjeringen.no. Accepterar:
                    - Kortlänk:      regjeringen.no/id/<KORTLÄNK_ID>
                    - Fullständig:   regjeringen.no/no/dokumenter/<DOK_SLUG>/id<DOC_ID>/
                    - Protokollrelativ: //www.regjeringen.no/id/...
      spara_i_db — Spara extraherad text i lokal databas (standard: true).
                   Sätt false bara för engångsanvändning där cachning inte är önskvärt.

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
      fulltext_md  — Extraherad text i Markdown-format
      kalla        — 'db_cache' om hämtad från lokal databas, 'live' om hämtad nyss
      tecken_antal — Antal tecken i fulltext_md
      fel          — Eventuellt felmeddelande (null vid framgång)

    OBS: Vid första hämtningen av ett stort eller bildbaserat dokument kan
    OCR-fallbacken ta flera minuter och slå i MCP-timeouten. Efterföljande
    anrop med samma URL går mot cachen och tar millisekunder.
    """
    try:
        # Strategi 1: returnera från DB-cache om fulltext redan finns
        cached = _hamta_regjeringen_fra_db(url)
        if cached:
            return {
                **cached,
                "pdf_url":      None,
                "kalla":        "db_cache",
                "tecken_antal": len(cached["fulltext_md"]),
                "fel":          None,
            }

        # Strategi 2: hämta live (PDF → markdown → eventuell OCR)
        data = rg.hamta_og_ekstraher(url)

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

        data["kalla"]        = "live"
        data["tecken_antal"] = len(data.get("fulltext_md") or "")
        return data

    except Exception as exc:
        log.error("nor_hamta_regjeringen misslyckades (%s): %s", url, exc)
        return {
            "tittel": "", "beteckning": "", "dok_type": "proposisjon",
            "url": url, "pdf_url": None, "fulltext_md": None,
            "kalla": "fel", "tecken_antal": 0,
            "fel": str(exc),
        }


@mcp.tool()
def nor_hamta_vedtak(
    sesjonid: str = "",
    vedtakid: str = "",
    med_fulltext: bool = False,
) -> dict:
    """
    Hämtar stortingsvedtak (parlamentariska beslut).

    Kan antingen lista alla vedtak för en session, eller hämta ett
    specifikt vedtak med fulltext.

    Parametrar:
      sesjonid     — Sessions-ID (t.ex. "2024-2025"). Lämna tomt för
                     senaste session. Används för listning.
      vedtakid     — ID för ett specifikt vedtak. Om angivet hämtas
                     fulltext för just detta vedtak.
      med_fulltext — Om True och sesjonid är angivet (ej vedtakid):
                     hämta fulltext för alla vedtak i sessionen.
                     Kan ta tid! Standard: False.

    Returnerar:
      vedtak_liste — Lista med vedtak (id, sak_id, dato, tittel, vedtakstekst)
      eller
      vedtak       — Fulltext för ett specifikt vedtak (id, tittel, fulltext_md)
    """
    try:
        sesjoner = _sesjoner()
        if not sesjonid and not vedtakid:
            sesjonid = st.aktuell_sesjonid(sesjoner)

        # Specifikt vedtak med fulltext
        if vedtakid:
            fulltext = st.hamta_vedtak_fulltext(vedtakid)
            return {
                "vedtakid": vedtakid,
                "vedtak":   fulltext,
            }

        # Lista för en session
        if not sesjonid:
            sesjonid = st.aktuell_sesjonid(sesjoner)

        vedtak_liste = st.hamta_vedtak_liste(sesjonid)

        if med_fulltext:
            for v in vedtak_liste:
                if v.get("id"):
                    try:
                        ft = st.hamta_vedtak_fulltext(v["id"])
                        v["fulltext_md"] = ft.get("fulltext_md", "")
                    except Exception as exc:
                        log.warning("Fulltext för vedtak %s misslyckades: %s", v["id"], exc)
                        v["fulltext_md"] = ""

        return {
            "sesjonid":     sesjonid,
            "antal":        len(vedtak_liste),
            "vedtak_liste": vedtak_liste,
        }

    except Exception as exc:
        log.error("nor_hamta_vedtak misslyckades: %s", exc)
        return {"fel": str(exc), "sesjonid": sesjonid, "vedtakid": vedtakid}


@mcp.tool()
def nor_hamta_horinginnspill(
    horingid: str,
    med_fulltext: bool = False,
) -> dict:
    """
    Hämtar skriftliga innspill (remissvar) till en høring (utskottsutfrågning).

    Ger civila samhällets och organisationers synpunkter på lagstiftningsärenden.

    Parametrar:
      horingid     — Høringens ID-nummer (hämtas via nor_sok_stortinget med
                     typer='horinger').
      med_fulltext — Om True hämtas fulltext för varje innspill som har
                     eksport_id (kan ta tid). Standard: False.

    Returnerar:
      Lista med innspill: id, tittel, avsender, dato, ingress.
      Med med_fulltext=True: fulltext_md per innspill.
    """
    try:
        innspill = st.hamta_skriftlige_innspill(horingid)

        if med_fulltext:
            for item in innspill:
                eksport_id = item.get("eksport_id", "")
                if eksport_id:
                    try:
                        parsed = st.hamta_og_parse_publikasjon(eksport_id)
                        item["fulltext_md"] = parsed.get("fulltext_md", "")
                    except Exception as exc:
                        log.warning("Fulltext för innspill %s misslyckades: %s", eksport_id, exc)
                        item["fulltext_md"] = ""

        return {
            "horingid": horingid,
            "antal":    len(innspill),
            "innspill": innspill,
        }

    except Exception as exc:
        log.error("nor_hamta_horinginnspill misslyckades (horingid=%s): %s", horingid, exc)
        return {"fel": str(exc), "horingid": horingid}


@mcp.tool()
def nor_lista_emner() -> dict:
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
        log.error("nor_lista_emner misslyckades: %s", exc)
        return {"fel": str(exc)}


@mcp.tool()
def nor_sok_semantisk(
    fraga: str,
    kilde: str = "alla",
    max_treff: int = 10,
) -> dict:
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

    Returnerar:
      Lista med matchande textstycken (rubrik, text, likhet 0–1,
      källdokumentets metadata).
    """
    try:
        from db import vektor_sok, _ar_postgres

        if not _ar_postgres():
            return {
                "fel":   "Semantisk sökning kräver PostgreSQL + pgvector.",
                "fraga": fraga,
                "treff": [],
            }

        # Query-expansion (valfritt)
        extra = expandera_fraga(fraga)
        sok_text = fraga + (", " + ", ".join(extra) if extra else "")

        modell = _hamta_embedding_modell()

        import os as _os
        log_path = _SCRIPT_DIR / "logs" / "embedding.log"
        log_fd   = _os.open(str(log_path), _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT)
        save_fd1 = _os.dup(1)
        try:
            _os.dup2(log_fd, 1)
            embedding = modell.encode(
                sok_text,
                normalize_embeddings=True,
            ).tolist()
        finally:
            _os.dup2(save_fd1, 1)
            _os.close(save_fd1)
            _os.close(log_fd)

        kilde_filter = None if kilde == "alla" else kilde
        treff = vektor_sok(embedding, kilde_filter=kilde_filter, max_treff=max_treff)

        return {
            "fraga":     fraga,
            "expansion": extra,
            "antal":     len(treff),
            "treff":     treff,
        }

    except Exception as exc:
        log.error("nor_sok_semantisk misslyckades: %s", exc)
        return {"fel": str(exc), "fraga": fraga, "treff": []}


# ── HTTP-autentisering ────────────────────────────────────────────────────────

def _make_auth_app(asgi_app, api_key: str):
    """
    Wrappa en ASGI-app med enkel Bearer-token-autentisering.
    Alla anrop utan korrekt Authorization-header avvisas med HTTP 401.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            token = (
                request.headers.get("Authorization", "")
                .removeprefix("Bearer ")
                .strip()
            )
            if token != api_key:
                return PlainTextResponse(
                    "Obehörig: ogiltig eller saknad API-nyckel.", status_code=401
                )
            return await call_next(request)

    return Starlette(
        routes=[Mount("/", app=asgi_app)],
        middleware=[Middleware(ApiKeyMiddleware)],
    )


# ── Startpunkt ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Initiera databas vid uppstart — fortsätter även om databasen inte är nåbar
    try:
        initiera_schema()
    except Exception as _exc:
        log.warning("Schemainitiering misslyckades — startar utan databas: %s", _exc)

    if MCP_TRANSPORT == "http":
        import uvicorn

        try:
            asgi_app = mcp.streamable_http_app()
        except AttributeError:
            log.warning(
                "mcp.streamable_http_app() saknas — försöker med sse_app(). "
                "Uppgradera mcp-paketet om problem uppstår."
            )
            asgi_app = mcp.sse_app()

        if MCP_API_KEY:
            log.info("API-nyckelautentisering aktiverad")
            app = _make_auth_app(asgi_app, MCP_API_KEY)
        else:
            log.warning(
                "MCP_API_KEY är inte satt — servern körs utan autentisering. "
                "Bind enbart till loopback (MCP_HOST=127.0.0.1) eller "
                "skydda via reverse proxy."
            )
            app = asgi_app

        log.info("Startar HTTP-transport på %s:%s", MCP_HOST, MCP_PORT)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
    else:
        log.info("Startar stdio-transport (lokal användning)")
        mcp.run(transport="stdio")
