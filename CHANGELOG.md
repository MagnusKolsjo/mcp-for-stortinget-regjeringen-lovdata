# Ändringslogg — mcp-for-stortinget-regjeringen-lovdata

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

---

## [1.1.0] — 2026-08-10

### Tillagt

- **`nor_lista_publikasjoner(sakid)`** — listar en saks publikationsreferenser utan
  fulltext. Sökträffar saknar publikationer eftersom Stortingets listendpoint
  (`/eksport/saker`) inte bär `publikasjon_referanse_liste` — bara den enskilda
  saken (`/eksport/sak`) gör det. Verktyget är den billiga vägen från en sökträff
  till sakens dokument.
- **`bara_metadata`** och **`publikasjon`** i `nor_hamta_dokument` — hämta sakens
  metadata utan fulltext, respektive en enskild publikation ur saken.
- **`max_tecken`** och **`fran_tecken`** i `nor_hamta_dokument`, `nor_sok_i_dokument`,
  `nor_hamta_lovdokument` och `nor_hamta_horinginnspill`. Trunkerade svar bär
  `trunkerad`, `tecken_totalt`, `tecken_visade` och `fortsatt_fran_tecken`, och
  kapas på ordgräns.
- **`regjeringen_url`** som eget fält på saken. Proposisjoner och meldinger pekar
  mot regjeringen.no, och URL:en låg tidigare bara inbäddad i
  publikationsreferenserna trots att `nor_hamta_regjeringen`s dokumentation
  hänvisade till ett fält med detta namn.
- **`matchade_termer`** per sökträff — visar vilken term som gav träffen.
- **`diagnostik`** i `nor_sok_semantisk` vid nollresultat, med ny hjälpfunktion
  `vektor_tackning()` i `db.py`. Svaret skiljer nu "inget matchade frågan" från
  "källan har inga embeddings".
- **`BotskyddFel`** i `regjeringen.py` — regjeringen.no ligger sedan 2026-08 bakom en
  Cloudflare JS-challenge som svarar HTTP 403 på hela domänen, oberoende av
  User-Agent. Verktyget känner igen `cf-mitigated: challenge` och returnerar
  `fel_typ: "kalla_blockerar_automatiserad_atkomst"` med de vägar som faktiskt
  fungerar, i stället för ett rått 403.
- **`StortingetFel`** i `stortinget.py` — Stortinget svarar HTTP 500 med ett
  `<feil>`-dokument när en identifierare är okänd eller saknar innehåll. Det
  översätts nu till ett begripligt besked i stället för ett rått serverfel.

### Ändrat

- **Söktermernas semantik.** Komma separerar termer (OR mellan dem); flera ord
  inom en term matchas med AND. Tidigare splittrades frågan även på blanksteg,
  vilket gjorde att en fras löstes upp i fristående ord med OR-logik.
  **Sökresultaten för befintliga anrop förändras** — sökningar som tidigare gav
  breda träfflistor ger nu färre och mer precisa träffar.
- `nor_hamta_horinginnspill` har `med_fulltext=True` som standard. Fulltexten
  ingår i samma svar från källan, så det kostar inget extra anrop.
- `synk_daglig.sh` kör `nor_embedding.py --kilde alla` i stället för
  `--kilde lovdata`. Stortinget- och regjeringen-dokument i cachen fick tidigare
  aldrig några vektorer.

### Fixat

- **`nor_hamta_dokument` kunde inte begränsas och sprängde MCP:s storleksgräns.**
  Verktyget returnerade sakens metadata plus fulltext för samtliga publikationer i
  ett svar. För en sak med sju publikationer avbröts anropet med
  "Tool result is too large" utan någon väg runt.
- **`nor_hamta_horinginnspill` returnerade tomma fält.** Fältnamnen var antagna,
  inte verifierade: koden läste `avsender`, `ingress` och `eksport_id`, medan
  källan levererar `organisasjon`, `tittel`, `dato`, `id` och `tekst`.
  Konsekvensen var att varje innspill fick tom avsändare och att `med_fulltext`
  var verkningslös, eftersom den byggde på ett `eksport_id` som aldrig existerat.
  Fulltexten fanns hela tiden i samma svar. Fältnamnen är nu verifierade mot ett
  live-svar (2026-08-10); notera att API:ets dokumentationssida visar ett äldre
  exempel med elementnamnen `horingsnotat_liste`/`horingsnotat`, vilket inte
  stämmer med vad tjänsten returnerar.
- **`nor_sok_i_dokument` hittade bara Lovdata-dokument.** SQL-frågan filtrerade på
  `kilde = 'lovdata'` medan dokumentationen utlovade att Stortinget-beteckning och
  titeldel fungerade. Sökningen går nu mot alla cachade källor, och felmeddelandet
  skiljer okänd identifierare från dokument som finns men saknar extraherad text.
- Tyst trunkering vid 600 tecken per träff i `nor_sok_i_dokument`.

### Bakgrund

Ändringarna genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet
— storlek, trunkering, adressering och sökning") i den här servern.

**OBS vid uppgradering:** `nor_lista_publikasjoner` är ett nytt verktyg i en
befintlig server. MCP-klienter som cachelägger verktygsindexet per servernamn kan
behöva ett nytt servernamn i konfigurationen för att se det.

---

### Ur tidigare opublicerat arbete

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
