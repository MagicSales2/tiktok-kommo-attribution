#!/usr/bin/env bash
# Prueba rápida contra el servicio ya levantado.
#   ./scripts/smoke.sh
#   BASE=http://localhost:8010 ./scripts/smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

read_env() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env | head -1 | cut -d= -f2-
}

BASE="${BASE:-$(read_env PUBLIC_BASE_URL)}"
BASE="${BASE:-http://localhost:8000}"
BASE="${BASE%/}"
SECRET="${KOMMO_WEBHOOK_SECRET:-$(read_env KOMMO_WEBHOOK_SECRET)}"

echo "== health =="
curl -fsS "$BASE/api/health" | python3 -m json.tool

echo
echo "== puente (dry_run, no guarda) =="
curl -fsS "$BASE/r?dry_run=1&ttclid=click-de-prueba&utm_source=tiktok&utm_campaign=smoke" \
  | python3 -m json.tool

echo
echo "== webhook (dry_run, no encola) =="
WEBHOOK="$BASE/webhook/kommo"
if [ -n "$SECRET" ]; then
  WEBHOOK="$WEBHOOK?secret=$SECRET&dry_run=1"
else
  WEBHOOK="$WEBHOOK?dry_run=1"
fi
curl -fsS -X POST "$WEBHOOK" \
  -H 'Content-Type: application/json' \
  -d '{"leads":{"status":[{"id":999999,"status_id":1,"pipeline_id":1}]}}' \
  | python3 -m json.tool

echo
echo "Listo. Revisa el panel para ver la cola y los logs."
