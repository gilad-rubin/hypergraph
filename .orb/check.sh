#!/bin/bash
# CI's checks (.github/workflows/ci.yml) on one machine from the checkout, with no install step: `orb base --for
# gilad-rubin/hypergraph` runs it to prove a new ready-made machine before the old one is deleted, and a worker runs it
# before a PR. One Python (.python-version) where CI runs a matrix, and no otel-floor job, which downgrades the
# environment. Each step's last lines and seconds; a failing step's whole output.
set -euo pipefail
cd "$(dirname "$0")/.."
check() {
  local t=$(date +%s) out
  if out=$("$@" 2>&1); then
    echo "$out" | tail -8; echo "== ok in $(( $(date +%s) - t )) s: $*"
  else
    echo "$out"; echo "== FAILED after $(( $(date +%s) - t )) s: $*"; return 1
  fi
}
# CI's pytest: warnings are errors, and a run with any skip or warning fails (a missing browser skips, so it fails here)
strict() {
  local xml; xml=$(mktemp --suffix .xml)
  uv run --no-sync pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning' --junitxml="$xml" "$@"
  uv run --no-sync python - "$xml" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root.find("testsuite")
bad = {k: int(suite.attrib.get(k, "0")) for k in ("skipped", "warnings")}
sys.exit(f"CI requires zero skips and warnings, found {bad}" if any(bad.values()) else None)
PY
}
check uv run --no-sync python --version
check uv run --no-sync ruff check .
check uv run --no-sync ruff format --check .
check uv run --no-sync python scripts/gen_sync.py --check
check uv run --no-sync mypy
check strict
check env HYPERGRAPH_VIZ_ENGINES=webkit bash -c "$(declare -f strict); strict tests/viz -m viz_browser"
