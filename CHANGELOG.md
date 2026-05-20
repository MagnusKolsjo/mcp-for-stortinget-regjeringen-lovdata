# Ändringslogg — mcp-for-stortinget-regjeringen-lovdata

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

---

## [1.1.0] — 2026-05-16

### Tillagt

**Lovdata: dynamisk paketsupptäckt**
- `hamta_lovdata_paket_lista()` i `lovdata_sync.py` — anropar `/v1/publicData/list` vid varje synk och returnerar tillgängliga paketnamn
- URL:er för nedladdning byggs nu dynamiskt: `https://api.lovdata.no/v1/publicData/get/{paket_namn}.tar.bz2` istället för hårdkodade strängar
- `_KALLOR`-konfigurationen refererar nu till `paket_namn` (t.ex. `gjeldende-lover`) — URL härleds vid körning
- Om `/v1/publicData/list` inte svarar fortsätter synken med känd konfiguration utan avbrott

**nor_hamta_vedtak (nytt MCP-verktyg, det 10:e)**
- `hamta_vedtak_liste(sesjonid)` i `stortinget.py` — hämtar alla stortingsvedtak för en session via `/stortingsvedtak?sesjonid=X`
- `hamta_vedtak_fulltext(vedtakid)` i `stortinget.py` — hämtar fulltext för ett specifikt vedtak
- `nor_hamta_vedtak` i `mcp_server.py` — lista per session, enskilt vedtak med fulltext, option att hämta fulltext för hela sessionen

**Dokumentfiltrering i nor_sok_stortinget**
- `nor_sok_stortinget` har fått tre nya filterparametrar: `dokumentgruppe`, `emne` och `sak_status`
- Partiell matchning, case-insensitiv — t.ex. `dokumentgruppe='lovsak'`, `emne='klima'`, `sak_status='behandlet'`
- Tom sträng = ingen filter (bakåtkompatibelt)

**Utökad sak-normalisering**
- `_normalisera_sak()` i `stortinget.py` extraherar nu `innstillingstekst` och `stikkord_liste` från sak-XML
- `sok_saker()` söker nu även i `innstillingstekst` och `stikkord` (utöver tittel och emner)

**nor_hamta_horinginnspill (nytt MCP-verktyg, det 11:e)**
- `hamta_skriftlige_innspill(horingid)` i `stortinget.py` — hämtar remissvar till en høring via `/horingsinnspill?horingid=X`
- `nor_hamta_horinginnspill` i `mcp_server.py` — returnerar avsändare, datum och ingress; option `med_fulltext=True` hämtar fulltext via `eksport_id`

**nor_lista_emner (nytt MCP-verktyg, det 12:e)**
- `hamta_emner()` i `stortinget.py` — hämtar Stortingets ämnesklassificering via `/emner` (~250 ämnen i 2-nivåhierarki)
- `nor_lista_emner` i `mcp_server.py` — returnerar hierarki uppdelad i toppnivå och undernivå

### Tekniska noter (1.1.0)

- Lovdata `/v1/publicData/list` hanterar JSON-svar som lista av strängar eller lista av dicts — båda formaten stöds
- `_normalisera_sak()` bakåtkompatibelt: `sak_status` och `sist_oppdatert` är separata fält
- Alla nya verktyg är FD-1-säkra och har fullständig felhantering

---

## [1.0.0] — 2026-05-15

### Tillagt

**Stortinget (grundstruktur)**
- `mcp_server.py` med FastMCP och stöd för både stdio- och HTTP-transport (`MCP_TRANSPORT` i `.env`)
- `stortinget.py` — XML-klient för Stortingets öppna data-API (`data.stortinget.no/eksport`)
- `db.py` — databasabstraktion med stöd för PostgreSQL (schema `norge`) och SQLite
- `db/schema_postgres.sql` och `db/schema_sqlite.sql` — idempotenta DDL-skript
- `nor_lista_sesjoner` — hämtar alla 43 tillgängliga sesjoner (1986-87 och framåt)
- `nor_sok_stortinget` — sökning i saker, innstillinger, referater, spørsmål och høringer
- `nor_hamta_dokument` — fulltext och metadata via dokument-ID eller beteckning

**regjeringen.no**
- `regjeringen.py` — PDF-pipeline för proposisjoner, NOU och Meld. St.: URL-normalisering, HTML-hämtning, PDF-URL-extraktion, pymupdf4llm-extraktion, OCR-fallback (ocrmypdf), FD-1-skydd
- `nor_hamta_regjeringen` — exponerar PDF-pipelinen som MCP-verktyg
- `nor_hamta_dokument` integrerad med `regjeringen.hamta_og_ekstraher()` för automatisk PDF-hantering

**Lovdata bulk**
- `lovdata_sync.py` — laddar ner dagliga tarbollarna (lagar + centrala föreskrifter), kontrollerar SHA-256-checksummor, parsar HTML-med-.xml-extension via BeautifulSoup, konverterar till Markdown, upsert i `norge.dokument`
- `nor_sok_lovdata` — fulltextsökning i norska lagar och föreskrifter (PostgreSQL FTS + SQLite LIKE)
- `nor_hamta_lovdokument` — hämtar ett enskilt lovdata-dokument med fulltext
- Daglig launchd-synk installerad: `04:00` (körs vid uppvakning om datorn sov)
- Vid driftsättning: 738 lagar och 3 427 centrala föreskrifter inlästa

**Databas och FTS**
- `fts_sok()` i `db.py` — PostgreSQL FTS med norsk stemming (`to_tsvector('norwegian', ...)`, `plainto_tsquery`), GIN-index, `ts_rank_cd`-ranking, OR-logik för kommaseparerade termer; SQLite LIKE
- `nor_sok` — aggregerat sökverktyg: Stortinget live-API + Lovdata-cache + regjeringen.no-cache i ett anrop
- `nor_sok_i_dokument` — paragrafnivåsökning inom ett cachat dokument (splittning på `### `-rubriker)

**Semantisk sökning och termexpansion**
- `nor_embedding.py` — chunkar `fulltext_md` (~800 tecken, paragrafgränser), genererar 768-dimensionella vektorer med `NbAiLab/nb-sbert-base`, lagrar i `norge.chunks.embedding` (pgvector)
- `vektor_sok()` i `db.py` — pgvector ANN-sökning (`<=>` cosinus), IVFFlat-index (`lists=100`)
- `nor_sok_semantisk` — semantisk sökning: kodar frågan via modellen, kör `vektor_sok()`, stöder källfilter och query-expansion
- `expandera_fraga()` i `mcp_server.py` — norsk termexpansion via OpenAI-kompatibel LLM-endpoint (`QUERY_EXPANSION_ENABLED=true`)
- `prompts/expansion_prompt.txt` — bokmål + nynorsk parlamentarisk och juridisk terminologi
- FD-1-skydd kring `sentence-transformers`-laddning och `.encode()`-anrop
- Vid driftsättning: 131 655 chunks med embeddings (4 162 dokument)

**Konfiguration och infrastruktur**
- `config.example.env` — komplett konfigurationsmall med alla variabler dokumenterade
- `requirements.txt` — alla beroenden listade med minimiversioner
- `nor_embedding.py` CLI: `--kilde alla|lovdata|stortinget|regjeringen.no`, `--tvinga`, `--bygg-index`, `--lists N`
- `lovdata_sync.py` CLI: `--kalla lover|forskrifter|alla`, `--tvinga`, `--installera-schema [launchd|cron|auto]`

### Tekniska noter

- Stortingets API returnerar XML för samtliga endpoints — ingen JSON-endpoint finns
- Sessions-ID-format: `2024-2025` (fyrsiffrigt)
- Lovdata-filer är HTML med `.xml`-extension — parsas med BeautifulSoup, inte lxml
- Checksumkontroll via SHA-256 mot `norge.sync_status` (HTTP saknar `Last-Modified`/`ETag`)
- SQLite saknar pgvector — `nor_sok_semantisk` returnerar felmeddelande om PostgreSQL inte är konfigurerat
- Embeddingmodellen laddas lat vid första anrop för att inte blockera stdio-uppstart

[1.1.0]: https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata/releases/tag/v1.1.0
[1.0.0]: https://github.com/MagnusKolsjo/mcp-for-stortinget-regjeringen-lovdata/releases/tag/v1.0.0
