# MCP-server för norsk riksdags- och rättsdata

MCP-server (Model Context Protocol) som ger AI-verktyg tillgång till norsk parlamentarisk data och rättslig information via Stortingets API, Lovdatas bulk-nedladdning och regjeringen.no.

Verktygen har prefixet `nor_` och täcker:

- **Stortinget** — saker, spørsmål, høringer, vedtak och remissvar (1986-87 och framåt)
- **Lovdata** — gällande norska lagar och forskrifter (lokal cache, daglig synk)
- **regjeringen.no** — proposisjoner, stortingsmeldinger och NOU som Markdown (PDF-extraktion med OCR-fallback)

Sökning stöder fulltextsökning (alla datakällor) och semantisk sökning med pgvector (kräver PostgreSQL + NbAiLab/nb-sbert-base).

---

## Datakällor

| Källa | Innehåll | Uppdateringsfrekvens |
|---|---|---|
| data.stortinget.no | Saker, spørsmål, høringer, vedtak, innspill (XML + metadata) | Live-API |
| Lovdata bulk | Gällande lagar och forskrifter | Daglig synk |
| regjeringen.no | Proposisjoner, NOU, Meld. St. (PDF → Markdown) | Vid anrop / DB-cache |

---

## Krav

- Python 3.11+
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

Initiera databasschemat:

```
python3 mcp_server.py --initiera
```

Kör den första synken av Lovdata-cachen:

```
bash synk_daglig.sh
```

Generera embeddings för semantisk sökning (kräver PostgreSQL + pgvector):

```
python3 nor_embedding.py
python3 nor_embedding.py --bygg-index
```

---

## Konfiguration i Claude Desktop

Lägg till följande i Claude Desktops MCP-konfiguration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "norge": {
      "command": "/sökväg/till/.venv/bin/python3",
      "args": ["/sökväg/till/stream-13-norge/mcp_server.py"],
      "env": {}
    }
  }
}
```

Använd `MCP_TRANSPORT=http` i `.env` för hostad driftsättning (lyssnar på `MCP_HOST:MCP_PORT`, standard `127.0.0.1:8003`).

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
| `nor_sok_lovdata` | Söker i lokal Lovdata-cache (lagar och forskrifter) |
| `nor_sok_i_dokument` | Sökning inom ett specifikt cachat dokument, avsnitt för avsnitt — alla källor |
| `nor_sok_semantisk` | Semantisk sökning med pgvector (kräver PostgreSQL + embeddings) |

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
| `nor_hamta_vedtak` | Hämtar stortingsvedtak (parlamentariska beslut) för en session eller ett specifikt vedtak |
| `nor_hamta_horinginnspill` | Hämtar skriftliga innspill (remissvar) till en høring, med fulltext |
| `nor_lista_emner` | Hämtar Stortingets ämnesklassificering (ca 250 ämnen i 2-nivåhierarki) |

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

## Licens

GNU Affero General Public License v3.0 eller senare — se [LICENSE](LICENSE).
