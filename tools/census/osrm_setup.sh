#!/bin/zsh
# Local OSRM oracle for the exhaustive census (FIXES item 1a).
# Builds the MLD pipeline from data/oracle/new-york-latest.osm.pbf
# (vintage pinned in new-york-latest.timestamp.txt) with the stock
# FOOT profile, then serves on :5001.
#
#   ./osrm_setup.sh build    extract + partition + customize (~10-20 min)
#   ./osrm_setup.sh serve    run osrm-routed on :5001 (foreground)
#
# Port 5001: 5000 collides with macOS AirPlay Receiver; 5173 belongs
# to another project (never kill it); 8000 is our own server.
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ORACLE_DIR="$SCRIPT_DIR/../../data/oracle"
IMAGE="ghcr.io/project-osrm/osrm-backend:latest"
PBF="new-york-latest.osm.pbf"

case "${1:-}" in
  build)
    docker run --rm -v "$ORACLE_DIR:/data" "$IMAGE" \
      osrm-extract -p /opt/foot.lua "/data/$PBF"
    docker run --rm -v "$ORACLE_DIR:/data" "$IMAGE" \
      osrm-partition "/data/${PBF%.osm.pbf}.osrm"
    docker run --rm -v "$ORACLE_DIR:/data" "$IMAGE" \
      osrm-customize "/data/${PBF%.osm.pbf}.osrm"
    echo "build done"
    ;;
  serve)
    exec docker run --rm --name osrm-census -p 5001:5000 \
      -v "$ORACLE_DIR:/data" "$IMAGE" \
      osrm-routed --algorithm mld --max-table-size 1000 \
      "/data/${PBF%.osm.pbf}.osrm"
    ;;
  *)
    echo "usage: $0 build|serve" >&2
    exit 1
    ;;
esac
