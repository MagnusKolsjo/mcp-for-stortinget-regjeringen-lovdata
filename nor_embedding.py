# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
nor_embedding.py — Chunkning och embedding för norsk rättsinformation

Läser fulltext_md från norge.dokument, delar upp i chunk-stycken (~800 tecken)
och genererar vektorer med NbAiLab/nb-sbert-base (768 dimensioner).
Vektorerna lagras i norge.chunks.embedding (pgvector).

Kör en gång efter initial datainläsning och sedan vid behov (t.ex. efter
lovdata_sync.py har lagt in nya dokument).

Kräver PostgreSQL + pgvector — semantisk sökning stöds ej för SQLite.

Användning:
    python3 nor_embedding.py
    python3 nor_embedding.py --kilde lovdata
    python3 nor_embedding.py --kilde stortinget --tvinga
    python3 nor_embedding.py --kilde alla --batch 50
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# ── Konfiguration ──────────────────────────────────────────────────────────────

EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "NbAiLab/nb-sbert-base")
CHUNK_MAX_TECKEN   = int(os.getenv("CHUNK_MAX_TECKEN", "800"))
CHUNK_MIN_TECKEN   = int(os.getenv("CHUNK_MIN_TECKEN", "100"))   # kasta bort för korta stycken
CHUNK_OVERLAPP     = int(os.getenv("CHUNK_OVERLAPP", "200"))      # överlapp mellan angränsande chunks
EMBEDDING_BATCH    = int(os.getenv("EMBEDDING_BATCH_STORLEK", "32"))

# Delas globalt efter första laddning
_modell = None


def _hamta_modell():
    """Laddar NbAiLab/nb-sbert-base vid behov (lat laddning)."""
    global _modell
    if _modell is None:
        log.info("Laddar embeddingmodell: %s", EMBEDDING_MODEL)
        import os as _os, contextlib

        # Skydda FD 1 mot tqdm/transformers-utskrifter (MCP-protokollet)
        save_fd1 = _os.dup(1)
        log_path = Path(__file__).parent / "logs" / "embedding.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = _os.open(str(log_path), _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT)
        try:
            _os.dup2(log_fd, 1)
            from sentence_transformers import SentenceTransformer
            _modell = SentenceTransformer(EMBEDDING_MODEL)
        finally:
            _os.dup2(save_fd1, 1)
            _os.close(save_fd1)
            _os.close(log_fd)

        log.info("Embeddingmodell laddad")
    return _modell


# ── Chunkning ──────────────────────────────────────────────────────────────────

def chunka_text(text: str) -> list[dict]:
    """
    Delar upp text i stycken på ~CHUNK_MAX_TECKEN tecken med CHUNK_OVERLAPP
    tecken överlapp mellan angränsande chunks.

    Splittningsstrategi (i fallande prioritet):
      1. Paragrafrubriker (### ) — naturliga gränser i Markdown från Lovdata
      2. Dubbla radbrytningar (styckebrytning)
      3. Meningsgränser (. ? !) om stycket fortfarande är för långt
      4. Hårda snitt på CHUNK_MAX_TECKEN om inget bättre alternativ finns

    Överlappet kopierar de sista CHUNK_OVERLAPP tecknen från föregående chunk
    som prefix till nästa, så att meningar som hamnar på en chunkgräns inte
    förlorar sin kontext vid semantisk sökning.

    Returnerar lista med dicts: {chunk_index, text, tecken_start, tecken_slut}
    """
    if not text:
        return []

    # Steg 1: dela på §-/kapitelrubriker
    delar = re.split(r"(?=\n###\s)", text)

    stycken: list[str] = []
    for del_ in delar:
        del_ = del_.strip()
        if not del_:
            continue
        if len(del_) <= CHUNK_MAX_TECKEN:
            stycken.append(del_)
        else:
            # Dela ytterligare på dubbla radbrytningar
            understycken = re.split(r"\n{2,}", del_)
            nuvarande = ""
            for us in understycken:
                us = us.strip()
                if not us:
                    continue
                if len(nuvarande) + len(us) + 2 <= CHUNK_MAX_TECKEN:
                    nuvarande = (nuvarande + "\n\n" + us).strip() if nuvarande else us
                else:
                    if nuvarande:
                        stycken.append(nuvarande)
                    # Stycket är fortfarande för långt — dela på meningsgränser
                    if len(us) > CHUNK_MAX_TECKEN:
                        meningar = re.split(r"(?<=[.?!])\s+", us)
                        nuvarande = ""
                        for m in meningar:
                            if len(nuvarande) + len(m) + 1 <= CHUNK_MAX_TECKEN:
                                nuvarande = (nuvarande + " " + m).strip() if nuvarande else m
                            else:
                                if nuvarande:
                                    stycken.append(nuvarande)
                                # Hårt snitt om meningen i sig är för lång
                                while len(m) > CHUNK_MAX_TECKEN:
                                    stycken.append(m[:CHUNK_MAX_TECKEN])
                                    m = m[CHUNK_MAX_TECKEN:]
                                nuvarande = m
                        if nuvarande:
                            stycken.append(nuvarande)
                            nuvarande = ""
                    else:
                        nuvarande = us
            if nuvarande:
                stycken.append(nuvarande)

    # Bygg chunks med positionsinfo och överlapp
    chunks = []
    pos = 0
    foregaende_slut = ""   # sista CHUNK_OVERLAPP tecken ur föregående chunk
    chunk_index = 0
    for s in stycken:
        s = s.strip()
        if len(s) < CHUNK_MIN_TECKEN:
            continue

        # Lägg till överlapp från föregående chunk som prefix (om det finns)
        overlapp_langd = len(foregaende_slut)
        if foregaende_slut:
            text_med_overlapp = foregaende_slut + "\n\n" + s
        else:
            text_med_overlapp = s

        # Hitta faktisk startposition i originaltexten (approximativt)
        idx = text.find(s[:40], pos)
        start = idx if idx >= 0 else pos
        slut  = start + len(s)

        chunks.append({
            "chunk_index":   chunk_index,
            "text":          text_med_overlapp,
            "tecken_start":  start,
            "tecken_slut":   slut,
            "tecken_overlapp": overlapp_langd,
        })
        chunk_index += 1
        pos = max(pos, slut)
        # Spara svansen av detta chunk som överlapp till nästa
        foregaende_slut = s[-CHUNK_OVERLAPP:] if len(s) > CHUNK_OVERLAPP else s

    return chunks


# ── Databas ────────────────────────────────────────────────────────────────────

def _hamta_dokument_utan_chunks(kilde: str | None, tvinga: bool) -> list[dict]:
    """Returnerar dokument som saknar chunks (eller alla om tvinga=True)."""
    from db import _cursor, _ar_postgres, _prefix

    if not _ar_postgres():
        log.error("Embedding kräver PostgreSQL — SQLite-backend stöder ej pgvector.")
        return []

    pfx = _prefix()

    if tvinga:
        villkor = "WHERE d.fulltext_md IS NOT NULL AND d.fulltext_md != ''"
        params: tuple = ()
    else:
        villkor = f"""
            WHERE d.fulltext_md IS NOT NULL
              AND d.fulltext_md != ''
              AND NOT EXISTS (
                  SELECT 1 FROM {pfx}chunks c WHERE c.dok_id = d.id
              )
        """
        params = ()

    kilde_villkor = ""
    if kilde and kilde != "alla":
        kilde_villkor = "AND d.kilde = %s"
        params = (kilde,)

    with _cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id, d.kilde, d.dok_type, d.beteckning, d.tittel,
                   length(d.fulltext_md) AS teckenlangd
            FROM   {pfx}dokument d
            {villkor}
            {kilde_villkor}
            ORDER  BY d.id
            """,
            params,
        )
        rader = cur.fetchall()

    return [
        {
            "id":          r[0],
            "kilde":       r[1],
            "dok_type":    r[2],
            "beteckning":  r[3],
            "tittel":      r[4],
            "teckenlangd": r[5],
        }
        for r in rader
    ]


def _hamta_fulltext(dok_id: int) -> str | None:
    """Hämtar fulltext_md för ett dokument."""
    from db import _cursor, _prefix

    with _cursor() as cur:
        cur.execute(
            f"SELECT fulltext_md FROM {_prefix()}dokument WHERE id = %s",
            (dok_id,),
        )
        rad = cur.fetchone()
    return rad[0] if rad else None


def _spara_chunks(dok_id: int, chunks: list[dict], embeddings):
    """Tar bort befintliga chunks och sparar nya med embeddings."""
    from db import _cursor, _prefix

    pfx = _prefix()
    with _cursor() as cur:
        cur.execute(f"DELETE FROM {pfx}chunks WHERE dok_id = %s", (dok_id,))
        for ch, emb in zip(chunks, embeddings):
            cur.execute(
                f"""
                INSERT INTO {pfx}chunks
                    (dok_id, chunk_index, text, tecken_start, tecken_slut,
                     tecken_overlapp, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                """,
                (
                    dok_id,
                    ch["chunk_index"],
                    ch["text"],
                    ch["tecken_start"],
                    ch["tecken_slut"],
                    ch.get("tecken_overlapp", 0),
                    "[" + ",".join(str(float(x)) for x in emb) + "]",
                ),
            )


# ── Embedding-körning ──────────────────────────────────────────────────────────

def embed_dokument(dok_id: int, tittel: str) -> int:
    """
    Chunkar och embeddar ett enstaka dokument.
    Returnerar antal genererade chunks (0 vid fel eller tom text).
    """
    text = _hamta_fulltext(dok_id)
    if not text:
        return 0

    chunks = chunka_text(text)
    if not chunks:
        return 0

    modell = _hamta_modell()

    # Lägg till dokumenttiteln som kontext i varje chunk (ökar precision)
    texter = [f"{tittel}\n\n{ch['text']}" if tittel else ch["text"] for ch in chunks]

    import os as _os
    log_path = Path(__file__).parent / "logs" / "embedding.log"
    log_fd   = _os.open(str(log_path), _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT)
    save_fd1 = _os.dup(1)
    try:
        _os.dup2(log_fd, 1)
        embeddings = modell.encode(
            texter,
            batch_size=EMBEDDING_BATCH,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
    finally:
        _os.dup2(save_fd1, 1)
        _os.close(save_fd1)
        _os.close(log_fd)

    _spara_chunks(dok_id, chunks, embeddings)
    return len(chunks)


def kör_embedding(kilde: str | None = None, tvinga: bool = False) -> dict:
    """
    Huvud-entry-point. Embeddar alla dokument som saknar chunks.

    Returnerar statistik: {total, lyckade, hoppade, fel, chunks_totalt}
    """
    dokument = _hamta_dokument_utan_chunks(kilde, tvinga)

    if not dokument:
        log.info("Inga dokument att embeda.")
        return {"total": 0, "lyckade": 0, "hoppade": 0, "fel": 0, "chunks_totalt": 0}

    log.info(
        "Embeddar %d dokument (kilde=%s, tvinga=%s)",
        len(dokument),
        kilde or "alla",
        tvinga,
    )

    stat = {"total": len(dokument), "lyckade": 0, "hoppade": 0, "fel": 0, "chunks_totalt": 0}

    for dok in dokument:
        try:
            antal = embed_dokument(dok["id"], dok["tittel"] or "")
            if antal == 0:
                stat["hoppade"] += 1
                log.debug("Hoppade (tomt): id=%s %s", dok["id"], dok["beteckning"] or dok["tittel"])
            else:
                stat["lyckade"]      += 1
                stat["chunks_totalt"] += antal
                log.debug(
                    "OK  id=%-6s  %3d chunks  %s",
                    dok["id"],
                    antal,
                    (dok["beteckning"] or dok["tittel"] or "")[:60],
                )
        except Exception as exc:
            stat["fel"] += 1
            log.warning("FEL  id=%s  %s: %s", dok["id"], dok["beteckning"], exc)

    log.info(
        "Klar — lyckade: %d, hoppade: %d, fel: %d, chunks: %d",
        stat["lyckade"],
        stat["hoppade"],
        stat["fel"],
        stat["chunks_totalt"],
    )
    return stat


# ── IVFFlat-index ──────────────────────────────────────────────────────────────

def bygg_ivfflat_index(lists: int = 100):
    """
    Bygger IVFFlat-index för ANN-sökning i chunks-tabellen.

    Ska köras EFTER att data laddats in — indexet är statiskt och måste
    byggas om när mer än ~20 % ny data tillkommer.

    Rekommenderat lists-värde: sqrt(antal_rader). För ~100k chunks: 316.
    Standard: 100 (lämpligt för <50k chunks).

    Kräver PostgreSQL + pgvector.
    """
    from db import _cursor, _ar_postgres, _prefix

    if not _ar_postgres():
        log.error("IVFFlat-index kräver PostgreSQL + pgvector — SQLite stöds ej.")
        return

    pfx = _prefix()
    log.info("Bygger IVFFlat-index (lists=%d)...", lists)
    with _cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_nor_chunks_embedding")
        cur.execute(
            f"""
            CREATE INDEX idx_nor_chunks_embedding ON {pfx}chunks
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = {lists})
            """
        )
    log.info("IVFFlat-index byggt.")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chunkar och embeddar norska dokument med NbAiLab/nb-sbert-base."
    )
    parser.add_argument(
        "--kilde",
        default="alla",
        choices=["alla", "lovdata", "stortinget", "regjeringen.no"],
        help="Källfilter (standard: alla)",
    )
    parser.add_argument(
        "--tvinga",
        action="store_true",
        help="Återskapa chunks även för dokument som redan är embeddade",
    )
    parser.add_argument(
        "--bygg-index",
        action="store_true",
        help="Bygg IVFFlat-index efter att embedding körts",
    )
    parser.add_argument(
        "--lists",
        type=int,
        default=100,
        help="IVFFlat lists-parameter (standard 100)",
    )
    args = parser.parse_args()

    kilde_arg = None if args.kilde == "alla" else args.kilde
    stat = kör_embedding(kilde=kilde_arg, tvinga=args.tvinga)

    print(
        f"\nResultat: {stat['lyckade']} lyckade, {stat['hoppade']} hoppade, "
        f"{stat['fel']} fel, {stat['chunks_totalt']} chunks totalt."
    )

    if args.bygg_index:
        from db import _ar_postgres
        if _ar_postgres():
            bygg_ivfflat_index(args.lists)
        else:
            log.error("--bygg-index kräver PostgreSQL + pgvector — SQLite stöds ej.")
