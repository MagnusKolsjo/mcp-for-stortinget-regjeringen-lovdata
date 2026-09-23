# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
db.py — Databaslager för MCP-servern för Stortinget, Lovdata och regjeringen.no

Stödjer två lagringsbackender — användaren väljer vid installation via
DATABASE_URL i .env:

  postgresql://anvandare:losenord@localhost:5432/<DATABASNAMN>
    → PostgreSQL + pgvector (schema: norge)
    → Ger samtidiga skrivningar och pgvector-baserad semantisk sökning

  sqlite:///norge_cache.db
    → SQLite (en lokal fil, ingen serverprocess)
    → Snabbt att komma igång; vektorsökning kräver Postgres

Anslutningsmönstret är per-anrop: varje funktion öppnar och stänger sin
egen anslutning. Detta gör koden tråd-säker, slipper stale-connection-
problematik och håller transaktionerna korta.
"""

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Sequence, Union
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///norge_cache.db")


# ---------------------------------------------------------------------------
# Backend-väljare och hjälpfunktioner
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """True om DATABASE_URL pekar mot Postgres, annars False."""
    return DATABASE_URL.startswith(("postgresql://", "postgres://"))


def _hamta_db():
    """
    Öppnar en ny databasanslutning. Varje anropare ansvarar för att stänga den,
    typiskt via `with _hamta_db() as conn:` eller via `_cursor()`-kontexthanteraren.
    """
    if _ar_postgres():
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL pekar mot Postgres men psycopg2 är inte installerat. "
                "Kör 'pip install psycopg2-binary' eller välj sqlite:// i DATABASE_URL."
            ) from exc
        return psycopg2.connect(DATABASE_URL)

    # SQLite: sqlite:///relativ.db ger sökvägen '/relativ.db' och
    # sqlite:////absolut/fil.db ger '//absolut/fil.db'. Bara det första
    # snedstrecket hör till URL-syntaxen; resten är en del av sökvägen.
    sokvag = urlparse(DATABASE_URL).path
    sokvag = sokvag[1:] if sokvag.startswith("/") else sokvag
    if not sokvag:
        raise RuntimeError("DATABASE_URL för SQLite saknar filsökväg")
    conn = sqlite3.connect(sokvag, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ph() -> str:
    """Platshållare för parameterbindning. Postgres: %s. SQLite: ?."""
    return "%s" if _ar_postgres() else "?"


def _prefix() -> str:
    """Schema-prefix för tabellnamn. Postgres får 'norge.', SQLite får ''."""
    return "norge." if _ar_postgres() else ""


# ---------------------------------------------------------------------------
# Schema-init
# ---------------------------------------------------------------------------

def initiera_schema():
    """
    Skapar tabeller och index om de inte finns. Kör SQL från db/schema_*.sql.
    Säker att köra många gånger — schemat är idempotent.

    Loggar varning och returnerar utan att kasta om DB är otillgänglig,
    så att MCP-servern står kvar i MCP-klienten även när Postgres-containern
    är nere. Verktygsanrop felar i så fall tills DB kommer upp igen.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    sql_filnamn = "schema_postgres.sql" if _ar_postgres() else "schema_sqlite.sql"
    sql_sokvag = Path(__file__).parent / "db" / sql_filnamn
    if not sql_sokvag.exists():
        log.error("Schemafil saknas: %s", sql_sokvag)
        return

    sql = sql_sokvag.read_text(encoding="utf-8")
    try:
        conn = _hamta_db()
        try:
            if _ar_postgres():
                with conn.cursor() as cur:
                    cur.execute(sql)
            else:
                conn.executescript(sql)
            _kor_migrationer(conn)
            conn.commit()
            log.info("Schema initierat (%s)", "postgres" if _ar_postgres() else "sqlite")
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "Databasinitiering misslyckades: %s — servern startar utan DB. "
            "Verktygsanrop felar tills databasen är tillgänglig.",
            exc,
        )


def _kolumn_finns(conn, tabell: str, kolumn: str) -> bool:
    """True om kolumnen finns (SQLite saknar ADD COLUMN IF NOT EXISTS)."""
    return any(rad[1] == kolumn for rad in conn.execute(f"PRAGMA table_info({tabell})"))


def _kor_migrationer(conn) -> None:
    """
    Schemaändringar efter första publicering, i kronologisk ordning.

    Bas-schemat i db/schema_*.sql är låst sedan 1.0.0 och ändras aldrig;
    befintliga installationer får nya kolumner och index härifrån. Varje
    steg är idempotent och körs vid varje start. Stegen är ren DDL, så de
    committas tillsammans med bas-schemat.
    """
    # Lovtidend avd. I: vilka författningar ett kungjort dokument ändrar
    # (Lovdatas refid, blankstegsseparerade, t.ex. 'lov/1999-07-02-64') och
    # ikraftträdandet som källan anger det — ofta fritext som 'Kongen
    # bestemmer', som inte ryms i en DATE-kolumn.
    nya_kolumner = (("endrer", "TEXT"), ("ikraft", "TEXT"))
    if _ar_postgres():
        with conn.cursor() as cur:
            for kolumn, typ in nya_kolumner:
                cur.execute(
                    f"ALTER TABLE {_prefix()}dokument ADD COLUMN IF NOT EXISTS {kolumn} {typ}"
                )
    else:
        for kolumn, typ in nya_kolumner:
            if not _kolumn_finns(conn, "dokument", kolumn):
                conn.execute(f"ALTER TABLE dokument ADD COLUMN {kolumn} {typ}")


# ---------------------------------------------------------------------------
# Generell cursor-kontexthanterare
# ---------------------------------------------------------------------------

@contextmanager
def _cursor():
    """
    Kontexthanterare som ger en databasmarkör och committar/rollbackar.
    Öppnar och stänger en frisk anslutning per anrop.

    Exempel:
        with _cursor() as cur:
            cur.execute("SELECT ...")
            rader = cur.fetchall()
    """
    conn = _hamta_db()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# FTS-sökning i dokument
# ---------------------------------------------------------------------------

def fts_sok(
    fraga: str,
    kilde_filter: Optional[str] = None,
    dok_type_filter: Optional[Union[str, Sequence[str]]] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Fulltextsökning i norge.dokument.

    Postgres: GIN-index med norsk stemming via to_tsvector('norwegian', ...).
    SQLite: ILIKE-fallback (ingen norsk stemming finns).

    Söktermerna följer projektets svarskontrakt: komma separerar termer och ger
    OR mellan dem, medan flera ord inom en term ger AND — alla orden måste
    förekomma. AND-delen faller ut av plainto_tsquery, som själv kombinerar
    orden i en fras med &.

    dok_type_filter är en dokumenttyp, en sekvens av typer eller None/'alla'
    för alla typer.

    Returnerar lista med dicts: {dok_id, kilde, dok_type, beteckning, tittel,
                                  dato, url, lovdata_id, endrer, ikraft, rank}
    """
    termer = [t.strip() for t in fraga.split(",") if t.strip()]
    if not termer:
        return []

    if _ar_postgres():
        return _pg_fts_sok(termer, kilde_filter, dok_type_filter, max_treff)
    return _sq_fts_sok(termer, kilde_filter, dok_type_filter, max_treff)


def _pg_fts_sok(termer, kilde_filter, dok_type_filter, max_treff) -> list[dict]:
    """FTS via to_tsquery med norsk konfiguration."""
    tsq_parts = " || ".join(["plainto_tsquery('norwegian', %s)"] * len(termer))

    villkor = []
    filter_params = []
    if kilde_filter:
        villkor.append("kilde = %s")
        filter_params.append(kilde_filter)
    typer = _dok_typer(dok_type_filter)
    if typer:
        villkor.append(f"dok_type IN ({', '.join(['%s'] * len(typer))})")
        filter_params.extend(typer)

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    params = list(termer) + filter_params + [max_treff]

    sql = f"""
        WITH q AS (SELECT {tsq_parts} AS tsq)
        SELECT
            d.id AS dok_id, d.kilde, d.dok_type, d.beteckning, d.tittel,
            d.dato, d.url, d.lovdata_id, d.endrer, d.ikraft,
            ts_rank_cd(
                to_tsvector('norwegian',
                    coalesce(d.tittel,'') || ' ' || coalesce(d.fulltext_md,'')),
                q.tsq
            ) AS rank
        FROM   {_prefix()}dokument d, q
        WHERE  to_tsvector('norwegian',
                   coalesce(d.tittel,'') || ' ' || coalesce(d.fulltext_md,''))
               @@ q.tsq
        {where_extra}
        ORDER  BY rank DESC, d.dato DESC NULLS LAST
        LIMIT  %s
    """

    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()

    return [
        {
            "dok_id":     r[0],
            "kilde":      r[1],
            "dok_type":   r[2],
            "beteckning": r[3],
            "tittel":     r[4],
            "dato":       str(r[5]) if r[5] else None,
            "url":        r[6],
            "lovdata_id": r[7],
            "endrer":     r[8].split() if r[8] else [],
            "ikraft":     r[9],
            "rank":       float(r[10]) if r[10] is not None else 0.0,
        }
        for r in rader
    ]


def _sq_fts_sok(termer, kilde_filter, dok_type_filter, max_treff) -> list[dict]:
    """ILIKE-fallback för SQLite (saknar norsk FTS-konfiguration)."""
    or_delar = " OR ".join(
        ["(tittel LIKE ? OR fulltext_md LIKE ?)"] * len(termer)
    )
    villkor = [f"({or_delar})"]
    params: list = []
    for t in termer:
        params += [f"%{t}%", f"%{t}%"]

    if kilde_filter:
        villkor.append("kilde = ?")
        params.append(kilde_filter)
    typer = _dok_typer(dok_type_filter)
    if typer:
        villkor.append(f"dok_type IN ({', '.join(['?'] * len(typer))})")
        params.extend(typer)

    params.append(max_treff)

    sql = f"""
        SELECT id AS dok_id, kilde, dok_type, beteckning, tittel,
               dato, url, lovdata_id, endrer, ikraft, 0.0 AS rank
        FROM   dokument
        WHERE  {' AND '.join(villkor)}
        ORDER  BY dato DESC
        LIMIT  ?
    """

    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()

    return [
        {
            "dok_id":     r[0],
            "kilde":      r[1],
            "dok_type":   r[2],
            "beteckning": r[3],
            "tittel":     r[4],
            "dato":       str(r[5]) if r[5] else None,
            "url":        r[6],
            "lovdata_id": r[7],
            "endrer":     r[8].split() if r[8] else [],
            "ikraft":     r[9],
            "rank":       0.0,
        }
        for r in rader
    ]


def _dok_typer(dok_type_filter) -> list[str]:
    """Normaliserar dok_type-filtret till en lista; tom lista = inget filter."""
    if not dok_type_filter or dok_type_filter == "alla":
        return []
    if isinstance(dok_type_filter, str):
        return [dok_type_filter]
    return list(dok_type_filter)


# ---------------------------------------------------------------------------
# Lovtidend — uppslag på ändrad författning
# ---------------------------------------------------------------------------

_REFID_MONSTER = re.compile(
    r"^(?:(?:NL|SF|LTI)/)?(lov|forskrift)/(\d{4}-\d{2}-\d{2}-\d+)$", re.IGNORECASE
)
_BETECKNING_MONSTER = re.compile(r"^(LOV|FOR)-(\d{4}-\d{2}-\d{2}-\d+)$", re.IGNORECASE)


def normalisera_refid(text: str) -> Optional[str]:
    """
    Gör om en författningsreferens till Lovdatas refid, t.ex.
    'NL/lov/2005-06-17-62', 'LOV-2005-06-17-62' och 'lov/2005-06-17-62'
    → 'lov/2005-06-17-62'. Returnerar None om texten inte är en sådan referens.
    """
    text = text.strip()
    m = _REFID_MONSTER.match(text)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    m = _BETECKNING_MONSTER.match(text)
    if m:
        typ = "lov" if m.group(1).upper() == "LOV" else "forskrift"
        return f"{typ}/{m.group(2)}"
    return None


def lovtidend_som_endrer(refid: str, max_treff: int = 10) -> list[dict]:
    """
    Lovtidend-dokument som enligt källans metadata ändrar författningen refid
    (t.ex. 'lov/2005-06-17-62'), nyast kungjorda först.

    Matchningen sker på hela refid, så 'lov/2005-06-17-6' träffar inte
    'lov/2005-06-17-62'. Poster med paragrafsuffix ('lov/.../§21') räknas
    som träff på författningen.
    """
    ph = _ph()
    sql = f"""
        SELECT id, kilde, dok_type, beteckning, tittel, dato, url,
               lovdata_id, endrer, ikraft
        FROM   {_prefix()}dokument
        WHERE  kilde = {ph} AND dok_type = {ph}
          AND  ((' ' || endrer || ' ') LIKE {ph} OR (' ' || endrer) LIKE {ph})
        ORDER  BY dato DESC
        LIMIT  {ph}
    """
    with _cursor() as cur:
        cur.execute(sql, ("lovdata", "lovtidend", f"% {refid} %", f"% {refid}/%", max_treff))
        rader = cur.fetchall()
    return [
        {
            "dok_id":     r[0],
            "kilde":      r[1],
            "dok_type":   r[2],
            "beteckning": r[3],
            "tittel":     r[4],
            "dato":       str(r[5]) if r[5] else None,
            "url":        r[6],
            "lovdata_id": r[7],
            "endrer":     r[8].split() if r[8] else [],
            "ikraft":     r[9],
            "rank":       1.0,
        }
        for r in rader
    ]


# ---------------------------------------------------------------------------
# Upsert — dokument
# ---------------------------------------------------------------------------

def upsert_dokument(
    kilde: str,
    dok_type: Optional[str],
    beteckning: Optional[str],
    tittel: Optional[str],
    sesjonid: Optional[str],
    dato: Optional[str],
    url: Optional[str],
    publikasjonid: Optional[str],
    sakid: Optional[str],
    lovdata_id: Optional[str],
    fulltext_md: Optional[str],
    endrer: Optional[str] = None,
    ikraft: Optional[str] = None,
) -> int:
    """
    Infogar eller uppdaterar ett dokument. Returnerar postens id.
    Konflikt avgörs på (kilde, publikasjonid) eller (kilde, lovdata_id).

    endrer och ikraft används av Lovtidend-dokument; se _kor_migrationer.
    """
    # Konfliktkolumnen följer vilken naturlig nyckel dokumentet har.
    # NULL-på-NULL är ingen konflikt, så ett dokument utan båda nycklarna
    # infogas alltid.
    konflikt = "lovdata_id" if (lovdata_id and not publikasjonid) else "publikasjonid"
    varden = (kilde, dok_type, beteckning, tittel, sesjonid, dato, url,
              publikasjonid, sakid, lovdata_id, fulltext_md, endrer, ikraft)
    if _ar_postgres():
        return _pg_upsert_dokument(varden, konflikt)
    return _sq_upsert_dokument(varden, konflikt, kilde, publikasjonid, lovdata_id)


_UPSERT_KOLUMNER = (
    "kilde, dok_type, beteckning, tittel, sesjonid, dato, url, "
    "publikasjonid, sakid, lovdata_id, fulltext_md, endrer, ikraft"
)


def _pg_upsert_dokument(varden: tuple, konflikt: str) -> int:
    sql = f"""
        INSERT INTO {_prefix()}dokument ({_UPSERT_KOLUMNER}, cachad_vid)
        VALUES ({', '.join(['%s'] * len(varden))}, NOW())
        ON CONFLICT (kilde, {konflikt}) DO UPDATE SET
            tittel       = EXCLUDED.tittel,
            fulltext_md  = EXCLUDED.fulltext_md,
            endrer       = EXCLUDED.endrer,
            ikraft       = EXCLUDED.ikraft,
            cachad_vid   = NOW()
        RETURNING id
    """
    with _cursor() as cur:
        cur.execute(sql, varden)
        row = cur.fetchone()
    return row[0]


def _sq_upsert_dokument(varden: tuple, konflikt: str, kilde, publikasjonid, lovdata_id) -> int:
    # Konfliktkolumnen måste vara samma unika nyckel som krockar. Med
    # (kilde, publikasjonid) även för Lovdata-dokument, som bara har
    # lovdata_id, avvisades varje omsynk med "UNIQUE constraint failed".
    sql_insert = f"""
        INSERT INTO dokument ({_UPSERT_KOLUMNER})
        VALUES ({', '.join(['?'] * len(varden))})
        ON CONFLICT (kilde, {konflikt}) DO UPDATE SET
            tittel      = excluded.tittel,
            fulltext_md = excluded.fulltext_md,
            endrer      = excluded.endrer,
            ikraft      = excluded.ikraft,
            cachad_vid  = datetime('now')
    """
    nyckel = lovdata_id if konflikt == "lovdata_id" else publikasjonid
    with _cursor() as cur:
        cur.execute(sql_insert, varden)
        cur.execute(
            f"SELECT id FROM dokument WHERE kilde=? AND {konflikt}=?",
            (kilde, nyckel),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        return cur.lastrowid or -1


# ---------------------------------------------------------------------------
# Sync-status
# ---------------------------------------------------------------------------

def get_sync_status(kilde: str) -> dict:
    """Returnerar synk-status för en källa, eller tomt dict om ingen finns."""
    if _ar_postgres():
        sql = f"SELECT kilde, sist_synkad, checksum, detaljer FROM {_prefix()}sync_status WHERE kilde=%s"
        with _cursor() as cur:
            cur.execute(sql, (kilde,))
            row = cur.fetchone()
        if not row:
            return {}
        return {"kilde": row[0], "sist_synkad": str(row[1]),
                "checksum": row[2], "detaljer": row[3]}

    sql = "SELECT kilde, sist_synkad, checksum, detaljer FROM sync_status WHERE kilde=?"
    with _cursor() as cur:
        cur.execute(sql, (kilde,))
        row = cur.fetchone()
    if not row:
        return {}
    status = dict(row)
    # SQLite lagrar detaljer som JSON-text; Postgres ger redan en dict (JSONB).
    try:
        status["detaljer"] = json.loads(status["detaljer"]) if status["detaljer"] else {}
    except (TypeError, ValueError):
        status["detaljer"] = {}
    return status


def set_sync_status(kilde: str, checksum: Optional[str] = None, detaljer: Optional[str] = None):
    """Uppdaterar (eller infogar) synk-status för en källa."""
    if isinstance(detaljer, dict):
        detaljer = json.dumps(detaljer)

    if _ar_postgres():
        sql = f"""
            INSERT INTO {_prefix()}sync_status (kilde, sist_synkad, checksum, detaljer)
            VALUES (%s, NOW(), %s, %s::jsonb)
            ON CONFLICT (kilde) DO UPDATE SET
                sist_synkad = NOW(),
                checksum    = EXCLUDED.checksum,
                detaljer    = EXCLUDED.detaljer
        """
    else:
        sql = """
            INSERT INTO sync_status (kilde, sist_synkad, checksum, detaljer)
            VALUES (?, datetime('now'), ?, ?)
            ON CONFLICT (kilde) DO UPDATE SET
                sist_synkad = datetime('now'),
                checksum    = excluded.checksum,
                detaljer    = excluded.detaljer
        """

    with _cursor() as cur:
        cur.execute(sql, (kilde, checksum, detaljer))


# ---------------------------------------------------------------------------
# Vektorsökning (kräver PostgreSQL + pgvector)
# ---------------------------------------------------------------------------

def vektor_sok(
    embedding: list[float],
    kilde_filter: Optional[str] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Semantisk sökning i norge.chunks med pgvector (cosinuslikhet).

    Kräver PostgreSQL — returnerar tom lista vid SQLite eller om pgvector saknas.

    Returnerar lista med dicts: {dok_id, chunk_index, text, likhet,
                                  kilde, dok_type, beteckning, tittel,
                                  dato, url, lovdata_id}
    """
    if not _ar_postgres():
        log.warning("vektor_sok: pgvector kräver PostgreSQL — returnerar tom lista.")
        return []

    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"

    villkor = []
    filter_params: list = []
    if kilde_filter:
        villkor.append("d.kilde = %s")
        filter_params.append(kilde_filter)

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    params = [vec_str] + filter_params + [vec_str, max_treff]

    sql = f"""
        SELECT
            c.dok_id,
            c.chunk_index,
            c.text,
            1 - (c.embedding <=> %s::vector)  AS likhet,
            d.kilde,
            d.dok_type,
            d.beteckning,
            d.tittel,
            d.dato,
            d.url,
            d.lovdata_id
        FROM   {_prefix()}chunks c
        JOIN   {_prefix()}dokument d ON d.id = c.dok_id
        WHERE  c.embedding IS NOT NULL
        {where_extra}
        ORDER  BY c.embedding <=> %s::vector
        LIMIT  %s
    """

    try:
        with _cursor() as cur:
            cur.execute(sql, params)
            rader = cur.fetchall()
    except Exception as exc:
        log.error("vektor_sok misslyckades: %s", exc)
        return []

    return [
        {
            "dok_id":      r[0],
            "chunk_index": r[1],
            "text":        r[2],
            "likhet":      round(float(r[3]), 4) if r[3] is not None else 0.0,
            "kilde":       r[4],
            "dok_type":    r[5],
            "beteckning":  r[6],
            "tittel":      r[7],
            "dato":        str(r[8]) if r[8] else None,
            "url":         r[9],
            "lovdata_id":  r[10],
        }
        for r in rader
    ]


def vektor_tackning() -> dict:
    """
    Redovisar hur många dokument och chunks per källa som har embeddings.

    Används för att skilja ett äkta nollresultat i den semantiska sökningen
    från att den valda källan aldrig har embeddats. Utan den skillnaden ser en
    källa utan vektorer ut som en källa utan relevant innehåll, vilket leder
    utredningsarbetet fel.

    Returnerar {kilde: {dokument, dokument_med_vektor, chunks_med_vektor}}.
    Tom dict om databasen inte är nåbar eller inte är PostgreSQL.
    """
    if not _ar_postgres():
        return {}

    sql = f"""
        SELECT d.kilde,
               COUNT(DISTINCT d.id)                                        AS dokument,
               COUNT(DISTINCT c.dok_id) FILTER (WHERE c.embedding IS NOT NULL)
                                                                           AS dok_med_vektor,
               COUNT(c.id) FILTER (WHERE c.embedding IS NOT NULL)          AS chunks
        FROM   {_prefix()}dokument d
        LEFT JOIN {_prefix()}chunks c ON c.dok_id = d.id
        GROUP  BY d.kilde
    """
    try:
        with _cursor() as cur:
            cur.execute(sql)
            rader = cur.fetchall()
    except Exception as exc:
        log.warning("vektor_tackning misslyckades: %s", exc)
        return {}

    return {
        r[0]: {
            "dokument":            r[1],
            "dokument_med_vektor": r[2],
            "chunks_med_vektor":   r[3],
        }
        for r in rader
    }
