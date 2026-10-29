#!/bin/bash
# orb-key: pyproject.toml uv.lock .python-version .orb/setup.sh
# orb-size: M   (`orb up` with no --size starts here; see the PR that added it for S against M timings)
#
# What a machine needs to run hypergraph's CI checks (.orb/check.sh): the ready-made machine of skill `orbs`. orb.py
# runs it as root from /root/hypergraph on a fresh base orb and snapshots the result, keyed on the files in the
# `orb-key:` line. Every step is skipped when already done. No keys. Written 2026-10-29 (friction
# 2026-10-29-hypergraph-has-no-ready-made-orb-so-a-viz-worker-installs-de: a viz worker spent ~6 min installing by hand).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
R=$(cd "$(dirname "$0")/.." && pwd)
T0=$(date +%s)
step() { echo "== $* ($(( $(date +%s) - T0 )) s)"; }

for p in git ca-certificates; do
  dpkg -s "$p" >/dev/null 2>&1 || { step "apt: $p"; apt-get update -qq && apt-get install -y -qq --no-install-recommends "$p" >/dev/null; }
done

if ! command -v uv >/dev/null; then
  step "uv"
  curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

# CI's test job environment: the dev group and the daft extra. Not --all-extras: `examples` pulls pytrec-eval, which
# needs a C compiler and no test imports.
lock=$(cat "$R/uv.lock" "$R/pyproject.toml" "$R/.python-version" | sha256sum | cut -c1-16)
if [ "$(cat "$R/.venv/.orb-lock" 2>/dev/null)" != "$lock" ]; then
  step "uv sync --frozen --group dev --extra daft"
  (cd "$R" && uv python install >/dev/null && uv sync --frozen -q --group dev --extra daft)
  # The viz tests' browsers, from the locked Playwright so their revisions match it: Chromium for the suite, WebKit
  # for CI's viz-webkit job, each with its system libraries
  step "Playwright chromium + webkit"
  (cd "$R" && uv run --no-sync playwright install --with-deps chromium webkit >/dev/null)
  echo "$lock" > "$R/.venv/.orb-lock"
fi

step "ready: $(uv --version), $(cd "$R" && uv run --no-sync python --version)"
