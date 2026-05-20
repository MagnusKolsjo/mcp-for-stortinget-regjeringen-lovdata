-- =============================================================================
-- MCP-server: Stortinget, Lovdata, regjeringen.no
-- Databasschema: PostgreSQL + pgvector
-- Kräver: CREATE EXTENSION vector; (pgvector installerat)
--
-- Tabellerna placeras i schemat norge för att undvika kollisioner med
-- andra arbetsströmmar som delar samma PostgreSQL-instans.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS norge;

-- ---------------------------------------------------------------------------
-- dokument: cachat dokument från Stortinget, Lovdata eller regjeringen.no
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS norge.dokument (
    id              BIGSERIAL       PRIMARY KEY,
    kilde           TEXT            NOT NULL,       -- 'stortinget', 'lovdata', 'regjeringen'
    dok_type        TEXT,                           -- 'innstilling', 'referat', 'lovvedtak', 'dok8',
                                                    --   'lov', 'forskrift', 'prop', 'nou', 'meld_st'
    beteckning      TEXT,                           -- t.ex. 'Innst. 1 S (2024-2025)', 'Prop. 1 S'
    tittel          TEXT,
    sesjonid        TEXT,                           -- t.ex. '2024-25' (Stortinget) eller NULL (Lovdata)
    dato            DATE,
    url             TEXT,
    publikasjonid   TEXT,                           -- Stortingets publikasjon-ID (unik per dokument)
    sakid           TEXT,                           -- Stortingets sak-ID (kan ha flera publikasjoner)
    lovdata_id      TEXT,                           -- Lovdatas dokumentidentifierare
    fulltext_md     TEXT,                           -- Extraherad text (Markdown)
    cachad_vid      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (kilde, publikasjonid),
    UNIQUE (kilde, lovdata_id)
);

CREATE INDEX IF NOT EXISTS idx_nor_dokument_kilde    ON norge.dokument (kilde);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_dok_type ON norge.dokument (dok_type);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_sesjonid ON norge.dokument (sesjonid);
CREATE INDEX IF NOT EXISTS idx_nor_dokument_dato     ON norge.dokument (dato);

-- Fulltextsökning (PostgreSQL FTS) — norsk stemming + stoppord
CREATE INDEX IF NOT EXISTS idx_nor_dokument_fts ON norge.dokument
    USING gin (to_tsvector('norwegian', coalesce(tittel,'') || ' ' || coalesce(fulltext_md,'')));

-- Lättvikts-index för titelsökning (snabbare för korta sökningar)
CREATE INDEX IF NOT EXISTS idx_nor_dokument_tittel_fts ON norge.dokument
    USING gin (to_tsvector('norwegian', coalesce(tittel,'')));

-- ---------------------------------------------------------------------------
-- chunks: textstycken (~800 tecken) ur ett cachat dokument
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS norge.chunks (
    id              BIGSERIAL   PRIMARY KEY,
    dok_id          BIGINT      NOT NULL REFERENCES norge.dokument (id) ON DELETE CASCADE,
    chunk_index     INTEGER     NOT NULL,
    text            TEXT        NOT NULL,
    tecken_start    INTEGER,
    tecken_slut     INTEGER,
    tecken_overlapp INTEGER     NOT NULL DEFAULT 0,  -- antal tecken överlapp med föregående chunk
    embedding       vector(768)                      -- norsk/nordisk sentence-transformer → 768 dim
);

CREATE INDEX IF NOT EXISTS idx_nor_chunks_dok_id ON norge.chunks (dok_id);

-- IVFFlat-index för ANN-sökning — byggs manuellt efter att data laddats in:
-- CREATE INDEX idx_nor_chunks_embedding ON norge.chunks
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- sync_status: tidsstämplar och checksummor per datakälla
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS norge.sync_status (
    kilde           TEXT        PRIMARY KEY,        -- 'stortinget', 'lovdata_lover',
                                                    --   'lovdata_forskrifter', 'regjeringen'
    sist_synkad     TIMESTAMPTZ,
    checksum        TEXT,                           -- SHA-256 av senaste tarball (Lovdata)
    detaljer        JSONB                           -- källspecifik checkpointinfo
);
