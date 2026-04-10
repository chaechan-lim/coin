#!/bin/bash
# Symphony-ClaudeCode: Coin project setup script
# Usage: ./scripts/setup-coin.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYMPHONY_DIR="$(dirname "$SCRIPT_DIR")"
COIN_DIR="/home/chans/coin"
WORKFLOW="$COIN_DIR/WORKFLOW.md"
ENV_FILE="$SYMPHONY_DIR/.env"

echo "=== Symphony-ClaudeCode: Coin Project Setup ==="
echo ""

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."

if ! command -v claude &> /dev/null; then
    echo "  ERROR: claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
    exit 1
fi
echo "  claude CLI: $(claude --version 2>/dev/null || echo 'OK')"

if ! command -v gh &> /dev/null; then
    echo "  ERROR: gh CLI not found. Install: sudo apt install gh"
    exit 1
fi
echo "  gh CLI: $(gh --version | head -1)"

if ! command -v git &> /dev/null; then
    echo "  ERROR: git not found"
    exit 1
fi
echo "  git: $(git --version)"

# 2. Check .env
echo ""
echo "[2/5] Checking environment..."

if [ ! -f "$ENV_FILE" ]; then
    echo "  .env not found. Creating from template..."
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    echo "  IMPORTANT: Edit $ENV_FILE and fill in your API keys!"
    echo ""
    echo "  Required:"
    echo "    SYMPHONY_LINEAR_API_KEY  — Get from Linear > Settings > API > Personal API Keys"
    echo "    SYMPHONY_DISCORD_WEBHOOK_URL — Your Discord webhook URL"
    echo ""
    read -p "  Press Enter after editing .env, or Ctrl+C to exit..."
fi

# Source .env
set -a
source "$ENV_FILE"
set +a

# Validate required vars
if [ -z "${SYMPHONY_LINEAR_API_KEY:-}" ] || [ "$SYMPHONY_LINEAR_API_KEY" = "lin_api_xxxxx" ]; then
    echo "  ERROR: SYMPHONY_LINEAR_API_KEY not set in .env"
    echo "  Get your key from: Linear > Settings > API > Personal API Keys"
    exit 1
fi
echo "  Linear API key: configured"

# 3. Check GitHub auth
echo ""
echo "[3/5] Checking GitHub authentication..."

if ! gh auth status &> /dev/null; then
    echo "  gh not authenticated. Running: gh auth login"
    gh auth login
fi
echo "  GitHub: authenticated"

# 4. Verify coin repo access
echo ""
echo "[4/5] Verifying coin repository..."

if [ ! -d "/home/chans/coin/.git" ]; then
    echo "  ERROR: /home/chans/coin is not a git repository"
    exit 1
fi
echo "  Repo: /home/chans/coin"
echo "  Remote: $(git -C /home/chans/coin remote get-url origin)"
echo "  Branch: $(git -C /home/chans/coin branch --show-current)"

# 5. Validate workflow
echo ""
echo "[5/5] Validating workflow..."

cd "$SYMPHONY_DIR"
source .venv/bin/activate 2>/dev/null || true
symphony validate "$WORKFLOW"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To start Symphony:"
echo "  cd $SYMPHONY_DIR"
echo "  source .venv/bin/activate"
echo "  symphony run --workflow $COIN_DIR/WORKFLOW.md --port 8002"
echo ""
echo "To run one cycle (test):"
echo "  symphony run --workflow $COIN_DIR/WORKFLOW.md --once"
echo ""
echo "Dashboard will be at: http://192.168.50.244:8002"
