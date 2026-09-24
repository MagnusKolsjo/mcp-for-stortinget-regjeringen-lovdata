# MCP-server för norsk riksdags- och rättsdata

MCP-server (Model Context Protocol) som ger AI-verktyg tillgång till norsk parlamentarisk data och rättslig information via Stortingets API, Lovdatas bulk-nedladdning och regjeringen.no.

Verktygen har prefixet `nor_` och täcker:

- **Stortinget** — saker, spørsmål, høringer, vedtak och remissvar (1986-87 och framåt)
- **Lovdata** — gällande norska lagar och forskrifter, och Norsk Lovtidend avd. I (lokal cache, daglig synk)
- **regjeringen.no** — proposisjoner, stortingsmeldinger och NOU som Markdown (PDF-extraktion med OCR-fallback)

Sökning stöder fulltextsökning (alla datakällor) och semantisk sökning med pgvector (kräver PostgreSQL + NbAiLab/nb-sbert-base).

---

## Datakällor

| Källa | Innehåll | Uppdateringsfrekvens |
|---|---|---|
| data.stortinget.no | Saker, spørsmål, høringer, vedtak, innspill (XML + metadata) | Live-API |
| Lovdata bulk | Gällande lagar och forskrifter | Daglig synk |
| Lovdata bulk | Norsk Lovtidend avd. I (kungjorda lagar och sentrala forskrifter, 2001–) | Daglig synk; paket som inte ändrats hoppas över |
| regjeringen.no | Proposisjoner, NOU, Meld. St. (PDF → Markdown) | Vid anrop / DB-cache |

---

## Krav

- Python 3.11+
- MCP Python SDK 2.x (`mcp>=2.0,<3`)
- PostgreSQL (rekommenderat) eller SQLite
- Vid PostgreSQL: pgvector-tillägget för semantisk sökning
- Delade beroenden installeras i gemensam `.venv` (se installationssteget nedan)

---

## Installation

Installera beroenden i projektets gemensamma virtuella miljö:

```
pip install -r requirements.txt
```

Kopiera och anpassa konfigurationsfilen:

```
cp config.example.env .env
```

Redigera `.env` och fyll i databasuppgifter och övriga inställningar.

Databasschemat skapas, och befintliga databaser uppdateras, automatiskt
när servern eller synkskriptet startar. Det går också att köra separat:

```
python3 -c "import db; db.initiera_schema()"
```

Kör den första synken av Lovdata-cachen:

```
bash synk_daglig.sh
```

Första synken laddar ned Lovtidend-paketet för tidigare år (~70 MB) och
läser in omkring 40 000 dokument; det tar en stund. Därefter hämtas det
bara när Lovdata har ändrat det.

Generera embeddings för semantisk sökning (kräver PostgreSQL + pgvector):

```
python3 nor_embedding.py
python3 nor_embedding.py --bygg-index --minne 4GB
```

Standardkörningen embeddar alla källor, även Norsk Lovtidend, och
embeddar om dokument vars text ändrats sedan förra körningen. Första
körningen efter att Lovtidend lästs in är lång: omkring 40 000 dokument ger
400 000–600 000 chunks, i storleksordningen en till en och en halv timme
(cirka 120 chunks/s med GPU/MPS; betydligt längre på bara CPU).

Embeddings lagras som `halfvec(768)` med ett HNSW-index (m=16,
ef_construction=64): omkring 4,5 kB per chunk inklusive index, alltså
ungefär 2,5 GB för Lovtidend. HNSW tål att chunks läggs till och behöver inte
byggas om efter varje körning. `--bygg-index` bygger om det, vilket går
mycket snabbare när grafen ryms i `maintenance_work_mem` (drygt 2 kB per
chunk, `--minne`). Sökdjupet styrs av `NOR_HNSW_EF_SEARCH` (standard 100).

## Uppgradering av en befintlig installation (från 1.1.0)

Ordningen spelar roll; steg 3 och 4 ändrar databasen och tar tid.

1. Installera den nya koden och `requirements.txt` (mcp 2.x) och starta
   servern en gång. Uppstarten lägger till de nya kolumnerna. En databas med
   fler än 50 000 chunks lagrar fortfarande embeddings som `vector` med
   IVFFlat; det loggas, och servern fungerar ändå. Mindre databaser
   konverteras direkt vid uppstarten.
2. Synka Lovdata (`bash synk_daglig.sh` eller `python3 lovdata_sync.py`).
   Första synken läser in Lovtidend och läser om gällande lagar och
   forskrifter med den nya parsern.
3. Byt vektorlagringen till `halfvec` med HNSW-index före den stora
   embeddingkörningen, så att de nya chunks skrivs som `halfvec` direkt:
   `python3 konvertera_vektorer.py --torrkorning`, därefter
   `python3 konvertera_vektorer.py --minne 4GB`. `norge.chunks` är låst under
   omskrivningen; semantiska sökningar väntar tills den är klar.
4. Embedda: `python3 nor_embedding.py` (se tidsuppskattningen ovan).

---

## Konfiguration i Claude Desktop

Lägg till följande i Claude Desktops MCP-konfiguration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "norge": {
      "command": "/<SOKVAG_TILL_VENV>/bin/python3",
      "args": ["/<SOKVAG_TILL_REPO>/mcp_server.py"],
      "env": {}
    }
  }
}
```

### http-transport

Sätt `MCP_TRANSPORT=http` och `MCP_API_KEY` i `.env` för delad drift. Servern
talar Streamable HTTP på `http://MCP_HOST:MCP_PORT/mcp` (standard
`127.0.0.1:8003`) och kräver `Authorization: Bearer <MCP_API_KEY>` på alla
anrop. Utan `MCP_API_KEY` startar servern inte i http-läge. SSE stöds inte.

---

## Daglig synk

Installera launchd-schema (macOS) för automatisk daglig synk kl. 04:00:

```
bash synk_daglig.sh --installera-schema
```

---

## MCP-verktyg

### Sökning

| Verktyg | Beskrivning |
|---|---|
| `nor_sok` | Samlad sökning över alla källor: Stortinget (live), Lovdata och regjeringen.no (cache) |
| `nor_sok_stortinget` | Söker saker, spørsmål och høringer i Stortinget för en given session |
| `nor_sok_lovdata` | Söker i lokal Lovdata-cache: gällande lagar och forskrifter, eller Lovtidend (`dok_type='lovtidend'`) |
| `nor_sok_i_dokument` | Sökning inom ett specifikt cachat dokument, avsnitt för avsnitt — alla källor |
| `nor_sok_semantisk` | Semantisk sökning med pgvector (kräver PostgreSQL + embeddings); `dok_type` väljer dokumenttyp, standard allt utom Lovtidend |

### Hämtning av dokument

| Verktyg | Beskrivning |
|---|---|
| `nor_lista_publikasjoner` | Listar en saks publikationsreferenser utan fulltext — vägen från sökträff till dokument |
| `nor_hamta_dokument` | Hämtar metadata och fulltext för ett Stortinget-dokument (sakid eller publikasjonid) |
| `nor_hamta_lovdokument` | Hämtar fulltext och metadata för ett Lovdata-dokument ur lokal cache |
| `nor_hamta_regjeringen` | Hämtar en proposisjon, NOU eller Meld. St. från regjeringen.no (PDF → Markdown, cachas) |

### Stortinget — specialiserade verktyg

| Verktyg | Beskrivning |
|---|---|
| `nor_lista_sesjoner` | Listar alla Stortingssesjoner (43 st, 1986-87 och framåt) |
| `nor_hamta_vedtak` | Hämtar stortingsvedtak (parlamentariska beslut) för en session, eller ett enskilt vedtak ur en session |
| `nor_hamta_horinginnspill` | Hämtar skriftliga innspill (remissvar) till en høring, med fulltext |
| `nor_lista_emner` | Hämtar Stortingets ämnesklassificering (ca 250 ämnen i 2-nivåhierarki) |

---

## Norsk Lovtidend — vilken ändringslag ändrade vad och när

Lovtidend avd. I innehåller lagar och sentrala forskrifter i den form de
kungjordes, främst ändringslagar och ändringsforskrifter. Varje dokument bär
vilka författningar det ändrar (`endrer`), när det kungjordes (`dato`) och
när det träder i kraft (`ikraft`, ofta fritext som "Kongen bestemmer").

```
nor_sok_lovdata(fraga="LOV-2005-06-17-62", dok_type="lovtidend")
   → kungjorda dokument som ändrar arbeidsmiljøloven, nyast först
nor_hamta_lovdokument(lovdata_id="LTI/lov/2026-01-23-1")
   → ändringstexten
```

Övriga termer söks med fulltext bland Lovtidend-dokumenten. `dok_type='alla'`
avser som tidigare den gällande, konsoliderade texten.

---

## Söktermer — hur frågan tolkas

Sökverktygen delar samma kontrakt:

- **Komma separerar termer** och betyder OR mellan dem.
- **Flera ord inom en term** betyder AND — alla orden måste förekomma.
- Varje träff bär **`matchade_termer`** som visar vilken term som gav träffen.

```
"konverteringsterapi, omvendelsesterapi, forbud mot konverteringsterapi"
   → poster som innehåller "konverteringsterapi"
   ELLER "omvendelsesterapi"
   ELLER alla tre orden "forbud", "mot" och "konverteringsterapi"
```

En flerordig term hålls alltså ihop. Att i stället matcha ord för ord med OR
gör att en fras som `forbud mot konverteringsterapi` rankar in varje ärende som
råkar innehålla ordet *mot* — resultatet ser rimligt ut men är innehållsligt fel.

---

## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. En enskild sak hos
Stortinget kan ha ett tiotal publikationer på hundratusentals tecken vardera,
så hämtverktygen har uttryckliga gränser i stället för att returnera allt:

| Parameter | Innebörd |
|---|---|
| `bara_metadata=True` | Sakens metadata och publikationsreferenser, ingen fulltext |
| `publikasjon="<eksport_id\|lenke_url>"` | Hämta bara en publikation ur saken |
| `max_tecken` | Teckentak för texten (`0` = ingen trunkering) |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare |

Ett trunkerat svar säger alltid ifrån, med `trunkerad`, `tecken_totalt`,
`tecken_visade` och `fortsatt_fran_tecken`. Kapningen sker på ordgräns.

**Rekommenderat arbetssätt för en stor sak:**

```
nor_sok_stortinget(fraga="...")                    → hitta sakid
nor_lista_publikasjoner(sakid)                     → se dokumenten + regjeringen_url
nor_hamta_dokument(id=sakid, publikasjon="inns-202324-105l", max_tecken=20000)
nor_hamta_regjeringen(url=<regjeringen_url>)       → proposisjonen
```

Proposisjoner och stortingsmeldinger distribueras inte av Stortingets API. URL:en
till regjeringen.no finns i sakens fält `regjeringen_url`.

---

## Felhantering

Förväntade fel — okänd identifierare, dokument som saknas i cachen, källor
som inte svarar eller blockerar automatiserad åtkomst — returneras som
verktygsfel (`isError`) med ett meddelande som säger vad som gick fel och
vad man kan göra i stället. regjeringen.no ligger bakom en Cloudflare-
utmaning som servern känner igen och förklarar, men inte försöker passera.

Stortingets tak är 100 anrop per minut. Klienten håller sig till 90 per
minut (`STORTINGET_RATE_LIMIT`) och respekterar `Retry-After` om källan
ändå svarar HTTP 429.

---

## Licens

GNU Affero General Public License v3.0 eller senare — se [LICENSE](LICENSE).
