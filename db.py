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

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
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

    # SQLite — extrahera filsökväg från sqlite:/// eller sqlite:////absolut/sökväg
    sokvag = urlparse(DATABASE_URL).path.lstrip("/")
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
    så att MCP-servern står kvar i Claude Desktop även när Postgres-containern
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
    dok_type_filter: Optional[str] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Fulltextsökning i norge.dokument.

    Postgres: GIN-index med norsk stemming via to_tsvector('norwegian', ...).
    SQLite: ILIKE-fallback (ingen norsk stemming finns).

    Kommaseparerade termer tolkas som OR-logik.

    Returnerar lista med dicts: {dok_id, kilde, dok_type, beteckning, tittel,
                                  dato, url, lovdata_id, rank}
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
    if dok_type_filter and dok_type_filter != "alla":
        villkor.append("dok_type = %s")
        filter_params.append(dok_type_filter)

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    params = list(termer) + filter_params + [max_treff]

    sql = f"""
        WITH q AS (SELECT {tsq_parts} AS tsq)
        SELECT
            d.id AS dok_id, d.kilde, d.dok_type, d.beteckning, d.tittel,
            d.dato, d.url, d.lovdata_id,
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
            "rank":       float(r[8]) if r[8] is not None else 0.0,
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
    if dok_type_filter and dok_type_filter != "alla":
        villkor.append("dok_type = ?")
        params.append(dok_type_filter)

    params.append(max_treff)

    sql = f"""
        SELECT id AS dok_id, kilde, dok_type, beteckning, tittel,
               dato, url, lovdata_id, 0.0 AS rank
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
            "rank":       0.0,
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
) -> int:
    """
    Infogar eller uppdaterar ett dokument. Returnerar postens id.
    Konflikt avgörs på (kilde, publikasjonid) eller (kilde, lovdata_id).
    """
    if _ar_postgres():
        return _pg_upsert_dokument(
            kilde, dok_type, beteckning, tittel, sesjonid, dato,
            url, publikasjonid, sakid, lovdata_id, fulltext_md
        )
    return _sq_upsert_dokument(
        kilde, dok_type, beteckning, tittel, sesjonid, dato,
        url, publikasjonid, sakid, lovdata_id, fulltext_md
    )


def _pg_upsert_dokument(kilde, dok_type, beteckning, tittel, sesjonid,
                         dato, url, publikasjonid, sakid, lovdata_id, fulltext_md) -> int:
    # Välj konfliktkolumn beroende på källa
    if publikasjonid:
        conflict_col = "publikasjonid"
    elif lovdata_id:
        conflict_col = "lovdata_id"
    else:
        conflict_col = "publikasjonid"  # NULL-på-NULL är inte en konflikt — insert alltid

    sql = f"""
        INSERT INTO {_prefix()}dokument
            (kilde, dok_type, beteckning, tittel, sesjonid, dato,
             url, publikasjonid, sakid, lovdata_id, fulltext_md, cachad_vid)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (kilde, {conflict_col}) DO UPDATE SET
            tittel       = EXCLUDED.tittel,
            fulltext_md  = EXCLUDED.fulltext_md,
            cachad_vid   = NOW()
        RETURNING id
    """
    with _cursor() as cur:
        cur.execute(sql, (kilde, dok_type, beteckning, tittel, sesjonid, dato,
                          url, publikasjonid, sakid, lovdata_id, fulltext_md))
        row = cur.fetchone()
    return row[0]


def _sq_upsert_dokument(kilde, dok_type, beteckning, tittel, sesjonid,
                         dato, url, publikasjonid, sakid, lovdata_id, fulltext_md) -> int:
    sql_insert = """
        INSERT INTO dokument
            (kilde, dok_type, beteckning, tittel, sesjonid, dato,
             url, publikasjonid, sakid, lovdata_id, fulltext_md)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (kilde, publikasjonid) DO UPDATE SET
            tittel      = excluded.tittel,
            fulltext_md = excluded.fulltext_md,
            cachad_vid  = datetime('now')
    """
    with _cursor() as cur:
        cur.execute(sql_insert, (kilde, dok_type, beteckning, tittel, sesjonid, dato,
                                  url, publikasjonid, sakid, lovdata_id, fulltext_md))
        if cur.lastrowid:
            return cur.lastrowid
        # Hämta befintligt id vid konflikt
        cur.execute(
            "SELECT id FROM dokument WHERE kilde=? AND publikasjonid=?",
            (kilde, publikasjonid)
        )
        row = cur.fetchone()
    return row["id"] if row else -1


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
    return dict(row) if row else {}


def set_sync_status(kilde: str, checksum: Optional[str] = None, detaljer: Optional[str] = None):
    """Uppdaterar (eller infogar) synk-status för en källa."""
    import json
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
