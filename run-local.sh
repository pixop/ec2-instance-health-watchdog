#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$PROJECT_ROOT/.env}"
VENV_DIR="$PROJECT_ROOT/.venv"

pick_python_3_10_plus() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN="$(pick_python_3_10_plus)"; then
  echo "Python 3.10+ is required. Install python3.10 or newer."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE"
  echo "Create it from the sample:"
  echo "  cp \"$PROJECT_ROOT/.env.example\" \"$PROJECT_ROOT/.env\""
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
elif ! "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Existing .venv uses Python <3.10, recreating virtualenv..."
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"

set -a
source "$ENV_FILE"
set +a

exec "$VENV_DIR/bin/python" -m ec2_watchdog.watchdog
