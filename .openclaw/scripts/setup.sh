#!/usr/bin/env bash
# .openclaw/scripts/setup.sh
#
# First-time setup for the hypergraph OpenClaw dev team.
# Run this once after cloning the repository.
#
# Usage:
#   .openclaw/scripts/setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPENCLAW_DIR="$REPO_ROOT/.openclaw"

echo ""
echo "🦞 Hypergraph OpenClaw Setup"
echo "=============================="
echo ""

# ─── 1. Check prerequisites ──────────────────────────────────────────────────

echo "▶ Checking prerequisites..."

check_command() {
  if ! command -v "$1" &>/dev/null; then
    echo "  ❌ $1 is not installed. $2"
    return 1
  else
    echo "  ✅ $1 found: $(command -v "$1")"
  fi
}

check_command "node" "Install Node.js ≥22 from https://nodejs.org"
check_command "gh" "Install GitHub CLI from https://cli.github.com"
check_command "uv" "Install uv from https://docs.astral.sh/uv/getting-started/installation/"
check_command "git" "Install git from https://git-scm.com"

# Check Node version
NODE_VERSION=$(node --version | sed 's/v//' | cut -d. -f1)
if [[ "$NODE_VERSION" -lt 22 ]]; then
  echo "  ❌ Node.js ≥22 required (found v${NODE_VERSION}). Please upgrade."
  exit 1
fi
echo "  ✅ Node.js v${NODE_VERSION} (≥22 required)"

# Check gh auth
if ! gh auth status &>/dev/null; then
  echo "  ❌ GitHub CLI not authenticated. Run: gh auth login"
  exit 1
fi
echo "  ✅ GitHub CLI authenticated"

echo ""

# ─── 2. Install OpenClaw ─────────────────────────────────────────────────────

echo "▶ Installing OpenClaw..."
if command -v openclaw &>/dev/null; then
  CURRENT_VERSION=$(openclaw --version 2>/dev/null || echo "unknown")
  echo "  ℹ OpenClaw already installed: $CURRENT_VERSION"
  echo "  Updating to latest..."
  npm install -g openclaw@latest
else
  npm install -g openclaw@latest
fi
echo "  ✅ OpenClaw installed: $(openclaw --version)"
echo ""

# ─── 3. Set up .env ──────────────────────────────────────────────────────────

echo "▶ Setting up environment..."
ENV_FILE="$OPENCLAW_DIR/.env"
ENV_EXAMPLE="$OPENCLAW_DIR/.env.example"

if [[ -f "$ENV_FILE" ]]; then
  echo "  ℹ .openclaw/.env already exists — skipping."
else
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "  ✅ Created .openclaw/.env from .env.example"
  echo ""
  echo "  ⚠️  ACTION REQUIRED: Edit .openclaw/.env and fill in your API keys:"
  echo "     - ANTHROPIC_API_KEY (required for Zoe + Claude-Planner)"
  echo "     - OPENAI_API_KEY    (required for Codex-Agent)"
  echo "     - GEMINI_API_KEY    (required for Gemini-Reviewer)"
  echo "     - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (optional, for notifications)"
  echo ""
  echo "  Run this script again after filling in the .env file."
  exit 0
fi

echo ""

# ─── 4. Create workspace directories ─────────────────────────────────────────

echo "▶ Setting up workspace..."
WORKSPACE_DIR="$HOME/.openclaw/workspaces/hypergraph"
mkdir -p "$WORKSPACE_DIR/memory"

# Copy workspace bootstrap files if they don't exist
if [[ ! -f "$WORKSPACE_DIR/MEMORY.md" ]]; then
  cp "$OPENCLAW_DIR/workspace/MEMORY.md" "$WORKSPACE_DIR/MEMORY.md"
  echo "  ✅ Copied MEMORY.md to $WORKSPACE_DIR"
else
  echo "  ℹ MEMORY.md already exists in workspace — skipping."
fi

# Create plans directory in repo if it doesn't exist
mkdir -p "$REPO_ROOT/plans"
if [[ ! -f "$REPO_ROOT/plans/.gitkeep" ]]; then
  touch "$REPO_ROOT/plans/.gitkeep"
  echo "  ✅ Created plans/ directory"
fi

echo ""

# ─── 5. Configure OpenClaw ───────────────────────────────────────────────────

echo "▶ Configuring OpenClaw..."

# Point OpenClaw config to the project's openclaw.json
openclaw config set "gateway.mode" "local" 2>/dev/null || true
echo "  ✅ Gateway mode set to local"

echo ""

# ─── 6. Make scripts executable ──────────────────────────────────────────────

echo "▶ Making scripts executable..."
chmod +x "$OPENCLAW_DIR/scripts/cron-monitor.sh"
chmod +x "$OPENCLAW_DIR/scripts/setup.sh"
echo "  ✅ Scripts are executable"
echo ""

# ─── 7. Run doctor ───────────────────────────────────────────────────────────

echo "▶ Running OpenClaw doctor..."
openclaw doctor 2>/dev/null || echo "  ⚠️  Doctor reported issues — check output above."
echo ""

# ─── 8. Summary ──────────────────────────────────────────────────────────────

echo "=============================="
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Start the gateway:    openclaw gateway --port 18789 --verbose"
echo "  2. Run a standup:        openclaw agent --message '/morning-standup'"
echo "  3. Ship a feature:       openclaw agent --message '/orchestrate-feature: <description>'"
echo ""
echo "Optional: set up the daily standup cron:"
echo "  crontab -e"
echo "  # Add: 0 9 * * 1-5 $OPENCLAW_DIR/scripts/cron-monitor.sh --standup"
echo ""
echo "Full docs: .openclaw/README.md"
echo ""
