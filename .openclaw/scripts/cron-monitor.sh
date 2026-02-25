#!/usr/bin/env bash
# .openclaw/scripts/cron-monitor.sh
#
# Runs the morning standup and monitors CI/PR status.
# Designed to be called by cron or the OpenClaw scheduler.
#
# Usage:
#   .openclaw/scripts/cron-monitor.sh [--standup] [--ci-watch <pr_number>]
#
# Cron example (weekdays at 9am):
#   0 9 * * 1-5 /path/to/repo/.openclaw/scripts/cron-monitor.sh --standup
#
# Setup:
#   1. Copy .openclaw/.env.example to .openclaw/.env and fill in values.
#   2. chmod +x .openclaw/scripts/cron-monitor.sh
#   3. Add to crontab: crontab -e

set -euo pipefail

# ─── Load environment ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.openclaw/.env"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

GITHUB_REPO="${GITHUB_REPO:-gilad-rubin/hypergraph}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# ─── Helpers ─────────────────────────────────────────────────────────────────

send_telegram() {
  local message="$1"
  if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
    echo "[monitor] Telegram not configured — printing to stdout instead:"
    echo "$message"
    return 0
  fi
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    --data-urlencode "parse_mode=Markdown" \
    > /dev/null
}

get_open_prs() {
  gh pr list --repo "$GITHUB_REPO" \
    --json number,title,headRefName,url,reviewDecision \
    --jq '.[] | "#\(.number) \(.title) [\(.reviewDecision // "PENDING")] \(.url)"' 2>/dev/null || echo "(none)"
}

get_failed_runs() {
  gh run list --repo "$GITHUB_REPO" --status failure --limit 5 \
    --json databaseId,name,headBranch,url \
    --jq '.[] | "❌ \(.name) on \(.headBranch): \(.url)"' 2>/dev/null || echo "(none)"
}

get_autobuild_issues() {
  gh issue list --repo "$GITHUB_REPO" --label "autobuild" \
    --json number,title,url \
    --jq '.[] | "#\(.number) \(.title): \(.url)"' 2>/dev/null || echo "(none)"
}

# ─── Standup mode ────────────────────────────────────────────────────────────

run_standup() {
  local date_str
  date_str=$(date '+%Y-%m-%d %H:%M')

  local open_prs failed_runs autobuild_issues
  open_prs=$(get_open_prs)
  failed_runs=$(get_failed_runs)
  autobuild_issues=$(get_autobuild_issues)

  local report
  report="🦞 *Hypergraph Standup — ${date_str}*

*Open PRs:*
${open_prs}

*Failed CI Runs:*
${failed_runs}

*Autobuild Issues:*
${autobuild_issues}"

  echo "$report"
  send_telegram "$report"

  # Trigger OpenClaw morning-standup skill if gateway is running
  if command -v openclaw &>/dev/null; then
    openclaw agent --message "/morning-standup" --thinking low 2>/dev/null || true
  fi
}

# ─── CI watch mode ───────────────────────────────────────────────────────────

watch_pr_ci() {
  local pr_number="$1"
  echo "[monitor] Watching CI for PR #${pr_number}..."

  local max_wait=1800  # 30 minutes
  local interval=60
  local elapsed=0

  while [[ $elapsed -lt $max_wait ]]; do
    local status
    status=$(gh pr checks "$pr_number" --repo "$GITHUB_REPO" \
      --json state --jq '.[].state' 2>/dev/null | sort -u || echo "UNKNOWN")

    if echo "$status" | grep -q "FAILURE\|failure"; then
      send_telegram "❌ CI failed on PR #${pr_number}. Run: /address-bugs ${pr_number}"
      echo "[monitor] CI failed on PR #${pr_number}"
      return 1
    elif echo "$status" | grep -q "SUCCESS\|success"; then
      send_telegram "✅ CI passed on PR #${pr_number}. Run: /run-review ${pr_number}"
      echo "[monitor] CI passed on PR #${pr_number}"
      return 0
    fi

    echo "[monitor] CI still running (${elapsed}s elapsed)... sleeping ${interval}s"
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  send_telegram "⏰ CI watch timed out for PR #${pr_number} after ${max_wait}s"
  echo "[monitor] Timed out waiting for CI on PR #${pr_number}"
  return 1
}

# ─── Entry point ─────────────────────────────────────────────────────────────

MODE="${1:-}"
case "$MODE" in
  --standup)
    run_standup
    ;;
  --ci-watch)
    PR_NUMBER="${2:-}"
    if [[ -z "$PR_NUMBER" ]]; then
      echo "Usage: $0 --ci-watch <pr_number>"
      exit 1
    fi
    watch_pr_ci "$PR_NUMBER"
    ;;
  *)
    # Default: run standup
    run_standup
    ;;
esac
