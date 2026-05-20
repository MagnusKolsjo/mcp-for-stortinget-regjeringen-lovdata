-- =============================================================================
-- MCP-server: Stortinget, Lovdata, regjeringen.no
-- Databasschema: SQLite (utan pgvector — semantisk sökning kräver PostgreSQL)
--
-- Använd vid SQLite-konfiguration. Semantisk sökning kräver PostgreSQL + pgvector.
-- Aktivera FTS via: CREATE VIRTUAL TABLE nor_dokument_fts USING fts5(...)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- dokument
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dokument (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    kilde           TEXT        NOT NULL,
    dok_type        TEXT,
    beteckning      TEXT,
    tittel          TEXT,
    sesjonid        TEXT,
    dato            TEXT,       -- ISO 8601: YYYY-MM-DD
    url             TEXT,
    publikasjonid   TEXT,
    sakid           TEXT,
    lovdata_id      TEXT,
    fulltext_md     TEXT,
    cachad_vid      TEXT        NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kilde, publikasjonid),
    UNIQUE (kilde, lovdata_id)
);

CREATE INDEX IF NOT EXISTS idx_nor_dokument_kilde    ON dokument (kilde);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_dok_type ON dokument (dok_type);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_sesjonid ON dokument (sesjonid);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_dato     ON dokument (dato);

-- FTS5 för fulltextsökning (valfritt, aktivera manuellt):
-- CREATE VIRTUAL TABLE dokument_fts USING fts5(tittel, fulltext_md, content=dokument, content_rowid=id);

-- ---------------------------------------------------------------------------
-- chunks (utan embedding — vektorsökning kräver sqlite-vec, installeras separat)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    dok_id          INTEGER     NOT NULL REFERENCES dokument (id) ON DELETE CASCADE,
    chunk_index     INTEGER     NOT NULL,
    text            TEXT        NOT NULL,
    tecken_start    INTEGER,
    tecken_slut     INTEGER,
    tecken_overlapp INTEGER     NOT NULL DEFAULT 0   -- antal tecken överlapp med föregående chunk
    -- embedding lagras i separat tabell om sqlite-vec används
);

CREATE INDEX IF NOT EXISTS idx_nor_chunks_dok_id ON chunks (dok_id);

-- ---------------------------------------------------------------------------
-- sync_status
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_status (
    kilde           TEXT        PRIMARY KEY,
    sist_synkad     TEXT,
    checksum        TEXT,
    detaljer        TEXT        -- JSON
);
