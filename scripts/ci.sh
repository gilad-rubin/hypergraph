#!/usr/bin/env bash
# What CI runs, one stage per job of .github/workflows/ci.yml, which calls this
# script too, so the local check and CI cannot drift apart.
#
#   scripts/ci.sh                  every stage, the test stage on Python 3.12
#   scripts/ci.sh test 3.11        one stage (lint, typecheck, otel-floor, test, viz-webkit)
#
# Each Python gets its own environment under .venv-ci/, so a 3.10 stage never
# rebuilds the worktree's .venv. The browser stages install their engine with
# its system packages (apt), as on a fresh orb.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTEST_WARNINGS=(-W error -W 'ignore::pytest.PytestUnraisableExceptionWarning')

use_env() {  # use_env NAME PYTHON: run uv in .venv-ci/NAME on that Python
  export UV_PROJECT_ENVIRONMENT=".venv-ci/$1" UV_PYTHON="$2"
}

lint() {
  use_env py3.12 3.12
  uv sync --group dev
  uv run ruff check .
  uv run ruff format --check .
  # template_sync.py is generated from template_async.py; a hand-edit to either half fails here.
  uv run python scripts/gen_sync.py --check
}

typecheck() {
  use_env py3.10 3.10  # the floor, as [tool.mypy] says
  uv sync --group dev
  uv run mypy
}

otel_floor() {
  # pyproject declares OpenTelemetry >=1.24.0 (the first Span with add_link); every other
  # stage resolves the newest, so only this one exercises the floor. Python 3.10: protobuf<5
  # warns on 3.12+, and -W error turns that into a collection error.
  use_env otel 3.10
  uv sync --group dev
  uv pip install --python ".venv-ci/otel/bin/python" \
    opentelemetry-api==1.24.0 opentelemetry-sdk==1.24.0 opentelemetry-exporter-otlp-proto-http==1.24.0
  # --no-sync: a plain `uv run` would restore the newest SDK and the suite would pass against it.
  uv run --no-sync python - <<'PY'
import importlib.metadata as md

FLOOR = "1.24.0"
PINNED = ("opentelemetry-api", "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http")
drifted = {name: md.version(name) for name in PINNED if md.version(name) != FLOOR}
if drifted:
    raise SystemExit(f"The floor pin did not take: expected OpenTelemetry {FLOOR} everywhere, got {drifted}")
from opentelemetry.sdk.trace import Span

if not hasattr(Span, "add_link"):
    raise SystemExit(f"opentelemetry-sdk {FLOOR} has no Span.add_link, so the >=1.24.0 floor in pyproject.toml is wrong")
print(f"OpenTelemetry {FLOOR} installed; Span.add_link present.")
PY
  uv run --no-sync pytest tests/test_run_log "${PYTEST_WARNINGS[@]}"
}

test_stage() {  # test_stage PYTHON
  # The JS scene-builder parity tests run in Node, which GitHub's runner ships; without it they
  # skip and the zero-skip check fails late, so refuse at once.
  command -v node >/dev/null || { echo "ci.sh test: Node.js is not installed (CI's runner has it): apt-get install -y nodejs" >&2; exit 1; }
  use_env "py$1" "$1"
  uv sync --group dev --extra daft
  uv run --no-sync playwright install --with-deps chromium
  uv run --no-sync pytest "${PYTEST_WARNINGS[@]}" --junitxml=".venv-ci/pytest-$1.xml"
  python3 scripts/check_junit.py ".venv-ci/pytest-$1.xml"
}

viz_webkit() {
  # The browser viz tests (marked viz_browser) in WebKit, named explicitly so a missing
  # WebKit fails the run instead of skipping it. Some bugs show in one engine only.
  use_env py3.12 3.12
  uv sync --group dev
  uv run --no-sync playwright install --with-deps webkit
  HYPERGRAPH_VIZ_ENGINES=webkit uv run --no-sync pytest tests/viz -m viz_browser "${PYTEST_WARNINGS[@]}" --junitxml=.venv-ci/pytest-webkit.xml
  python3 scripts/check_junit.py .venv-ci/pytest-webkit.xml
}

stage() {
  echo "== ci.sh $*"
  case "$1" in
    lint) lint ;;
    typecheck) typecheck ;;
    otel-floor) otel_floor ;;
    test) test_stage "${2:-3.12}" ;;
    viz-webkit) viz_webkit ;;
    *) echo "unknown stage: $1 (lint, typecheck, otel-floor, test, viz-webkit)" >&2; exit 2 ;;
  esac
}

if [ $# -gt 0 ]; then
  stage "$@"
  exit
fi
# Every stage, each in a fresh process: set -e holds there (a subshell tested with || drops it)
# and no stage's environment leaks into the next. All run; the exit names each that failed.
failed=()
for s in lint typecheck otel-floor test viz-webkit; do
  bash "$0" "$s" || failed+=("$s")
done
if [ ${#failed[@]} -gt 0 ]; then
  echo "ci.sh: FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "ci.sh: every CI stage passed"
