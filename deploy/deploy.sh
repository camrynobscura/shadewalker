#!/usr/bin/env bash
#
# Deploy Shade Walker to the production box.
#
# Ships CODE + built frontend + the export. The box never builds: no Node,
# none of the 2.7GB data caches. Run from the repo root on the laptop,
# AFTER building the frontend with the CARTO key present:
#
#     (cd web && VITE_CARTO_KEY=... npm run build)
#     HOST=deploy@203.0.113.10 ./deploy/deploy.sh
#
# (deploy, not shadewalker: the service user has no login shell — see
# deploy/README.md §1 for the deploy user + its narrow sudoers rule.)
#
# Idempotent: re-run any time to push a new build/export. It rsyncs, then
# reconciles the box's venv from uv.lock and restarts the service.

set -euo pipefail

HOST="${HOST:?set HOST=user@ip — the droplet, e.g. deploy@203.0.113.10}"
DEST="${DEST:-/opt/shadewalker}"

# Guard: the frontend build must exist, or we'd ship an API-only box.
if [[ ! -f web/dist/index.html ]]; then
	echo "web/dist/index.html missing — run 'cd web && VITE_CARTO_KEY=... npm run build' first" >&2
	exit 1
fi
# Guard: the export must exist.
if [[ ! -f data/export/citywide.json.gz ]]; then
	echo "data/export/citywide.json.gz missing — run the pipeline build first" >&2
	exit 1
fi

# ALLOWLIST, not a denylist: -R (--relative) recreates exactly these paths
# under $DEST and nothing else. --delete prunes stale files WITHIN each of
# them (e.g. old hashed /assets from a prior deploy) but never touches
# paths not listed here — critically, the box's own .venv is safe.
rsync -avzR --delete \
	--exclude='__pycache__/' --exclude='*.pyc' \
	server pipeline web/dist data/export pyproject.toml uv.lock \
	"$HOST:$DEST/"

# On the box: install/refresh the SLIM runtime deps (no-op if unchanged),
# then restart. --no-default-groups = the 7 server packages only, never
# the build stack. (Needs uv on the box's PATH and a sudoers rule letting
# this user restart just this unit — see deploy/README.md.)
ssh "$HOST" "cd '$DEST' && uv sync --no-default-groups && sudo systemctl restart shadewalker"

echo "deployed to $HOST:$DEST — check https://shadewalker.nyc/health"
