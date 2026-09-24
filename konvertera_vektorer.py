#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
konvertera_vektorer.py — Byter embeddings från vector(768) till halfvec(768)

halfvec lagrar varje komponent som 16-bitars flyttal: 1 540 byte per vektor
i stället för 3 076. Topp-10 i cosinussökningen påverkas inte mätbart.
Vektorindexet byggs om som HNSW (m=16, ef_construction=64) i stället för
IVFFlat.

Servern fungerar före, under (med väntan) och efter konverteringen: den läser
kolumntypen vid varje sökning och inskrivning. Nya och nästan tomma databaser
konverteras automatiskt vid uppstart; det här skriptet är till för befintliga
databaser med många chunks, där omskrivningen tar tid och kräver disk.

Så går det till:
  1. Vektorindexet tas bort (dess operatorklass gäller vector).
  2. ALTER TABLE … TYPE halfvec(768) skriver om hela norge.chunks. Tabellen är
     låst under tiden; semantiska sökningar och embeddingkörningar väntar.
  3. HNSW-indexet byggs.

Kör:
  python3 konvertera_vektorer.py --torrkorning   # uppskattning, ändrar inget
  python3 konvertera_vektorer.py --minne 4GB     # konvertera och bygg index
  python3 konvertera_vektorer.py --bara-index    # bygg om HNSW-indexet
"""

import argparse
import logging
import math
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv

load_dotenv(_SCRIPT_DIR / ".env")

import db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("konvertera_vektorer")

# Uppmätt på 211 000 riktiga chunks (pgvector 0.8.2, maintenance_work_mem
# 4 GB, fyra parallella arbetare): omskrivningen tog 11 s och HNSW-bygget
# 87 s. Används bara för uppskattningarna.
SPARAT_PER_VEKTOR = 3_100                   # byte mindre i tabell + TOAST per vektor
HNSW_BYTE_PER_RAD = 1_970                   # indexstorlek per vektor
HNSW_SEK_PER_100K = 39.0                    # byggtid för 100 000 vektorer
OMSKRIVNING_BYTE_PER_SEK = 100 * 1024**2    # försiktig läs- och skrivtakt


def _gb(byte: float) -> str:
    return f"{byte / 1024**3:.1f} GB"


def _tid(sek: float) -> str:
    return f"{sek / 60:.0f} min" if sek < 5400 else f"{sek / 3600:.1f} h"


def _hnsw_sek(n: int) -> float:
    """HNSW-bygget växer ungefär som n·log n."""
    if n <= 0:
        return 0.0
    return HNSW_SEK_PER_100K * (n / 100_000) * (math.log(max(n, 2)) / math.log(100_000))


def lagesbild() -> dict:
    """Läser storlekar ur katalogen och statistiken; inga tunga frågor."""
    with db._cursor() as cur:
        cur.execute("""
            SELECT c.reltuples::bigint, pg_relation_size(c.oid),
                   coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0),
                   pg_total_relation_size(c.oid)
            FROM pg_class c WHERE c.oid = 'norge.chunks'::regclass""")
        rader, heap, toast, totalt = cur.fetchone()
        if rader < 0:  # aldrig analyserad
            cur.execute("SELECT count(*) FROM norge.chunks")
            rader = cur.fetchone()[0]
        cur.execute("""SELECT null_frac FROM pg_stats
                       WHERE schemaname = 'norge' AND tablename = 'chunks'
                         AND attname = 'embedding'""")
        rad = cur.fetchone()
        null_frac = rad[0] if rad else 0.0
        cur.execute("""SELECT am.amname, pg_relation_size(i.indexrelid)
                       FROM pg_index i
                       JOIN pg_class c ON c.oid = i.indexrelid
                       JOIN pg_am am ON am.oid = c.relam
                       WHERE c.relname = %s AND c.relnamespace = 'norge'::regnamespace""",
                    (db.VEKTORINDEX,))
        idx = cur.fetchone()
        return {
            "rader": rader, "heap": heap, "toast": toast, "totalt": totalt,
            "typ": db.vektortyp(cur),
            "vektorer": int(rader * (1 - null_frac)),
            "index_typ": idx[0] if idx else None,
            "index": idx[1] if idx else 0,
        }


def torrkorning() -> None:
    info = lagesbild()
    log.info(
        "norge.chunks: %d rader, %s; totalt %s (tabell %s, TOAST %s, vektorindex %s %s)",
        info["rader"], info["typ"], _gb(info["totalt"]), _gb(info["heap"]),
        _gb(info["toast"]), info["index_typ"] or "saknas", _gb(info["index"]),
    )
    if info["typ"] == "halfvec" and info["index_typ"] == "hnsw":
        log.info("Inget att göra; kolumnen är halfvec med HNSW-index.")
        return

    n = info["vektorer"]
    konvertera = info["typ"] == "vector"
    sparat = n * SPARAT_PER_VEKTOR if konvertera else 0
    tabell_efter = max(info["heap"] + info["toast"] - sparat, 0)
    ovriga_index = info["totalt"] - info["heap"] - info["toast"] - info["index"]
    hnsw_efter = n * HNSW_BYTE_PER_RAD
    totalt_efter = tabell_efter + ovriga_index + hnsw_efter

    sek_omskrivning = (info["heap"] + info["toast"]) / OMSKRIVNING_BYTE_PER_SEK if konvertera else 0
    log.info("Uppskattning (osäkerhet ungefär ±50 procent):")
    log.info("  omskrivning av tabellen: %s; HNSW-bygge: %s (med tillräckligt minne)",
             _tid(sek_omskrivning), _tid(_hnsw_sek(n)))
    if konvertera:
        log.info("  disk under omskrivningen: ytterligare %s (ny kopia av tabellen) "
                 "efter att vektorindexet (%s) tagits bort",
                 _gb(tabell_efter + ovriga_index), _gb(info["index"]))
    log.info("  maintenance_work_mem för snabbt HNSW-bygge: minst %s (--minne)",
             _gb(hnsw_efter * 1.2))
    log.info("  slutstorlek: %s (tabell %s, HNSW %s, övriga index %s); frigör %s",
             _gb(totalt_efter), _gb(tabell_efter), _gb(hnsw_efter), _gb(ovriga_index),
             _gb(info["totalt"] - totalt_efter))


def konvertera(minne: str, parallella: int, bara_index: bool) -> None:
    with db._cursor() as cur:
        typ = db.vektortyp(cur)
    if not bara_index and typ == "vector":
        start = time.time()
        log.info("Skriver om norge.chunks till halfvec(%d); tabellen är låst under tiden...",
                 db.VEKTOR_DIM)
        with db._cursor() as cur:
            db.konvertera_till_halfvec(cur)
        log.info("Omskrivning klar på %s", _tid(time.time() - start))
    elif not bara_index:
        log.info("Kolumnen är redan halfvec; bygger bara index.")

    start = time.time()
    log.info("Bygger HNSW-index (maintenance_work_mem=%s, %d arbetare)...", minne, parallella)
    db.bygg_vektorindex(minne=minne, parallella=parallella)
    log.info("Index klart på %s", _tid(time.time() - start))

    with db._cursor() as cur:
        cur.execute("ANALYZE norge.chunks")
        cur.execute("SELECT pg_total_relation_size('norge.chunks')")
        log.info("norge.chunks efter konverteringen: %s", _gb(cur.fetchone()[0]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Byter embeddings till halfvec(768) med HNSW-index"
    )
    parser.add_argument("--torrkorning", action="store_true",
                        help="Visa uppskattad tid, diskbehov och slutstorlek; ändrar inget")
    parser.add_argument("--minne", default="4GB",
                        help="maintenance_work_mem för HNSW-bygget (standard 4GB)")
    parser.add_argument("--parallella", type=int, default=4,
                        help="Parallella arbetare för indexbygget (standard 4)")
    parser.add_argument("--bara-index", action="store_true",
                        help="Bygg bara om HNSW-indexet, ingen typkonvertering")
    args = parser.parse_args()

    if not db._ar_postgres():
        log.error("Skriptet kräver PostgreSQL med pgvector.")
        sys.exit(1)
    if args.torrkorning:
        torrkorning()
        return
    konvertera(args.minne, args.parallella, args.bara_index)
    log.info("Klar.")


if __name__ == "__main__":
    main()
