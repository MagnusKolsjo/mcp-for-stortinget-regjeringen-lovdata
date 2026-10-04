#!/bin/bash
# synk_daglig.sh — Daglig synk av norsk riksdags- och rättsdata.
#
# Körordning:
#   1. Lovdata (kontrollerar checksum — hoppar om oförändrat, annars full synk)
#   2. Chunkning + embedding för nya/uppdaterade dokument
#
# Anropas av launchd dagligen kl. 04:00.
# Kör manuellt: bash ~/MCP-Servers/norge/synk_daglig.sh

set -euo pipefail

MAPP="$HOME/MCP-Servers/norge"
LOGG="$MAPP/logs/synk_daglig.log"

mkdir -p "$MAPP/logs"

echo "=============================" >> "$LOGG"
echo "Daglig synk startad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "=============================" >> "$LOGG"

# Ladda .env om den finns
if [ -f "$MAPP/.env" ]; then
    set -a
    source "$MAPP/.env"
    set +a
fi

cd "$MAPP"

# Välj Python-tolk efter att .env laddats, så att PYTHON_SOKVAG i .env gäller
PYTHON="${PYTHON_SOKVAG:-$HOME/MCP-Servers/.venv/bin/python3}"

# ---------------------------------------------------------------------------
# Steg 1: Lovdata (kontrollerar SHA-256 — hoppar om tarball oförändrad)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 1: Lovdata bulk-synk" >> "$LOGG"
"$PYTHON" "$MAPP/lovdata_sync.py" >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 1 klar" >> "$LOGG"

# ---------------------------------------------------------------------------
# Steg 2: Chunkning + embedding för nya/uppdaterade dokument
#
# Alla källor embeddas, inte bara Lovdata. Stortinget- och regjeringen-dokument
# hamnar i cachen när de hämtas via MCP-verktygen, men fick tidigare aldrig
# några vektorer — vilket gjorde att semantisk sökning mot dem alltid gav noll
# träffar utan att det framgick varför.
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 2: Chunkning och embedding" >> "$LOGG"
"$PYTHON" "$MAPP/nor_embedding.py" --kilde alla >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 2 klar" >> "$LOGG"

echo "Daglig synk avslutad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "" >> "$LOGG"
