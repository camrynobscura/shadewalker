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
# under $DEST and nothing else — critically, the box's own .venv is safe.
SHIPPED_DIRS=(server pipeline web/dist data/export)
rsync -avzR --delete \
	--exclude='__pycache__/' --exclude='*.pyc' \
	"${SHIPPED_DIRS[@]}" pyproject.toml uv.lock \
	"$HOST:$DEST/"

# --delete above is a no-op from a Mac: the stock rsync has been Apple's
# openrsync since macOS 14, and a real run against the box left stray
# files in place (35 old hashed /assets had piled up). So prune explicitly: inside each shipped DIRECTORY, whatever
# the box has that this tree doesn't is removed, one line per file. This
# is what keeps a deleted module out of server/, and a stray *.json.gz
# out of data/export — the server loads EVERY one it finds there.
for dir in "${SHIPPED_DIRS[@]}"; do
	ssh "$HOST" "cd '$DEST' && find '$dir' -type f -not -path '*/__pycache__/*'" |
		while IFS= read -r f; do [[ -e "$f" ]] || printf '%s\0' "$f"; done |
		ssh "$HOST" "cd '$DEST' && xargs -0 -r rm -v --"
done

# On the box: install/refresh the SLIM runtime deps (no-op if unchanged),
# then restart. --no-default-groups = the server packages only, never
# the build stack. (Needs uv on the box's PATH and a sudoers rule letting
# this user restart just this unit — see deploy/README.md.)
ssh "$HOST" "cd '$DEST' && uv sync --no-default-groups && sudo systemctl restart shadewalker"

echo "deployed to $HOST:$DEST — check https://shadewalker.nyc/health"
