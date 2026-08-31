#!/usr/bin/env bash
#
# Smoke-test the DEPLOYABLE — the one thing no test covers: a single uvicorn
# process serving the real web/dist + the API + the rate limiter + the
# security headers, exactly as the box runs it (production flags, limiter
# ENABLED). Boots that command against your local build + export, checks the
# endpoints and headers, then tears the server down. Run from the repo root
# before a deploy, or after any server change.
#
#   ./deploy/smoke_test.sh
#
set -uo pipefail

PORT="${PORT:-8010}"
BASE="http://127.0.0.1:${PORT}"

# Preconditions — the box has both of these; without them we'd test nothing.
[[ -f web/dist/index.html ]] || { echo "FAIL: web/dist not built (cd web && npm run build)"; exit 1; }
[[ -f data/export/citywide.json.gz ]] || { echo "FAIL: no export in data/export/"; exit 1; }

echo "booting: uvicorn server.app:app --port ${PORT} (production flags, limiter on)"
uv run uvicorn server.app:app --port "${PORT}" --no-access-log --no-server-header >/tmp/shadewalker_smoke.log 2>&1 &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null' EXIT

# Wait for /health — the citywide graph load takes ~15s.
ready=0
for _ in $(seq 1 60); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "${ready}" -eq 1 ]] || { echo "FAIL: server never became ready"; tail -20 /tmp/shadewalker_smoke.log; exit 1; }

pass=0; fail=0
ok()  { echo "  ok   $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL $1"; fail=$((fail + 1)); }

curl -sf "${BASE}/health" | grep -q '"status":"ok"' \
  && ok "/health returns ok" || bad "/health"

curl -sf "${BASE}/" | grep -qi "Shade Walker" \
  && ok "/ serves the real frontend shell" || bad "/ (static mount)"

asset=$(curl -sf "${BASE}/" | grep -oE '/assets/[^"]+\.js' | head -1)
curl -sfI "${BASE}${asset}" >/dev/null \
  && ok "hashed asset serves (${asset})" || bad "asset ${asset}"

curl -sfI "${BASE}/" | grep -qi "referrer-policy: strict-origin-when-cross-origin" \
  && ok "Referrer-Policy header on static response" || bad "Referrer-Policy header"

curl -sfI "${BASE}/" | grep -qi "x-content-type-options: nosniff" \
  && ok "X-Content-Type-Options header on static response" || bad "nosniff header"

curl -sf "${BASE}/route?from_lat=40.717&from_lon=-73.977&to_lat=40.704&to_lon=-73.986" | grep -q '"routes"' \
  && ok "/route returns a routes payload" || bad "/route"

echo
echo "passed ${pass}, failed ${fail}"
[[ "${fail}" -eq 0 ]] && echo "SMOKE TEST PASSED" || echo "SMOKE TEST FAILED"
exit "${fail}"
