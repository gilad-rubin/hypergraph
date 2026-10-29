#!/bin/bash
# orb-key: pyproject.toml uv.lock .python-version scripts/ci.sh .orb/setup.sh
# orb-size: M   (`orb up` with no --size starts here: the suite took 628 s on S and 248 s on M, 2026-10-29)
#
# What a machine needs to run hypergraph's CI checks (.orb/check.sh, which runs scripts/ci.sh): the ready-made machine
# of skill `orbs`. orb.py runs it as root from /root/hypergraph on a fresh base orb and snapshots the result, keyed on
# the files in the `orb-key:` line. Every step is skipped when already done. No keys. Written 2026-10-29 (friction
# 2026-10-29-hypergraph-has-no-ready-made-orb-so-a-viz-worker-installs-de: a viz worker spent ~6 min installing by hand).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
R=$(cd "$(dirname "$0")/.." && pwd)
T0=$(date +%s)
step() { echo "== $* ($(( $(date +%s) - T0 )) s)"; }

NODE_VERSION=22.14.0  # the viz tests run the JS layout under `node` (tests/viz), as on CI's ubuntu-24.04 runner

# Node in /opt/node, unpacked with --no-same-owner: a tarball's uid 1000 on /usr/local/bin locks Morph's exec out
# (orbs skill, Gotchas). Without it ci.sh's test stage refuses.
for p in git ca-certificates xz-utils; do
  dpkg -s "$p" >/dev/null 2>&1 || { step "apt: $p"; apt-get update -qq && apt-get install -y -qq --no-install-recommends "$p" >/dev/null; }
done

if [ "$(node --version 2>/dev/null)" != "v$NODE_VERSION" ]; then
  step "Node $NODE_VERSION"
  rm -rf /opt/node.new && mkdir -p /opt/node.new
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" |
    tar -xJ --no-same-owner -C /opt/node.new --strip-components=1
  rm -rf /opt/node && mv /opt/node.new /opt/node
  ln -sf /opt/node/bin/node /usr/local/bin/node
fi

if ! command -v uv >/dev/null; then
  step "uv"
  curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

# The environments, keyed on the lock, pyproject, .python-version and ci.sh. .venv is a worker's own `uv run pytest`;
# .venv-ci/* are the ones scripts/ci.sh syncs into, per stage. Not --all-extras: `examples` pulls pytrec-eval, which
# needs a C compiler and no test imports.
lock=$(cat "$R/uv.lock" "$R/pyproject.toml" "$R/.python-version" "$R/scripts/ci.sh" | sha256sum | cut -c1-16)
if [ "$(cat "$R/.venv-ci/.orb-lock" 2>/dev/null)" != "$lock" ]; then
  cd "$R"
  uv python install 3.10 3.12 >/dev/null
  sync_env() {  # sync_env DIR PYTHON [ARGS]
    step "uv sync into $1 on $2 ${*:3}"
    UV_PROJECT_ENVIRONMENT="$1" UV_PYTHON="$2" uv sync --frozen -q --group dev "${@:3}"
  }
  sync_env .venv 3.12 --extra daft
  sync_env .venv-ci/py3.12 3.12 --extra daft  # lint, test 3.12, viz-webkit
  sync_env .venv-ci/py3.10 3.10               # typecheck
  sync_env .venv-ci/otel 3.10                 # otel-floor, which pins its OpenTelemetry on top itself
  # The browsers, from the locked Playwright so their revisions match it: Chromium for the test stage, WebKit for
  # viz-webkit, each with its system libraries
  step "Playwright chromium + webkit"
  .venv-ci/py3.12/bin/playwright install --with-deps chromium webkit >/dev/null
  echo "$lock" > .venv-ci/.orb-lock
fi

step "ready: node $(node --version), $(uv --version), $(ls "$R/.venv-ci" | tr '\n' ' ')"
