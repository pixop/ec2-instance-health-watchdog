#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$PROJECT_ROOT/.env}"
VENV_DIR="$PROJECT_ROOT/.venv"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE"
  echo "Create it from the sample:"
  echo "  cp \"$PROJECT_ROOT/.env.example\" \"$PROJECT_ROOT/.env\""
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"

set -a
source "$ENV_FILE"
set +a

exec "$VENV_DIR/bin/python" -m ec2_watchdog.watchdog
