#!/usr/bin/env bash
set -euo pipefail

# Installs the watchdog into /opt and configures systemd.
# Run as root: sudo ./scripts/install-systemd.sh

APP_USER="${APP_USER:-ec2-watchdog}"
APP_GROUP="${APP_GROUP:-ec2-watchdog}"
APP_DIR="${APP_DIR:-/opt/ec2-watchdog}"
ENV_DIR="${ENV_DIR:-/etc/ec2-watchdog}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/ec2-watchdog.env}"
SERVICE_NAME="${SERVICE_NAME:-ec2-watchdog.service}"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$EUID" -ne 0 ]]; then
  echo "This installer must run as root (use sudo)." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not installed." >&2
  exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
  echo "python3 venv module is missing. Install python3-venv first." >&2
  exit 1
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if ! getent group "$APP_GROUP" >/dev/null 2>&1; then
  groupadd --system "$APP_GROUP"
fi

install -d -m 0755 "$APP_DIR"
install -d -m 0750 "$ENV_DIR"

# Copy project files while excluding local/ephemeral files.
shopt -s dotglob nullglob
for path in "$PROJECT_ROOT"/* "$PROJECT_ROOT"/.[!.]* "$PROJECT_ROOT"/..?*; do
  base="$(basename "$path")"
  case "$base" in
    .|..|.git|.venv|.env|__pycache__)
      continue
      ;;
  esac
  cp -a "$path" "$APP_DIR/"
done
shopt -u dotglob nullglob

chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$PROJECT_ROOT/.env.example" ]]; then
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
    echo "Created $ENV_FILE from .env.example. Update it before production use."
  else
    cat >"$ENV_FILE" <<'EOF'
AWS_REGION=eu-central-1
TARGET_INSTANCE_ID=i-0123456789abcdef0
CHECK_INTERVAL_SECONDS=30
IMPAIRED_THRESHOLD_SECONDS=300
REBOOT_COOLDOWN_SECONDS=1800
LOG_LEVEL=INFO
REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false
EOF
    echo "Created default $ENV_FILE. Update it before production use."
  fi
fi

chown "root:$APP_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

run_as_app_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    su -s /bin/bash "$APP_USER" -c "$(printf '%q ' "$@")"
  fi
}

if [[ ! -d "$APP_DIR/.venv" ]]; then
  run_as_app_user python3 -m venv "$APP_DIR/.venv"
fi

run_as_app_user "$APP_DIR/.venv/bin/pip" install --upgrade pip
run_as_app_user "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

cp "$APP_DIR/systemd/$SERVICE_NAME" "$SERVICE_DEST"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "Installation complete."
echo "Service status:"
systemctl --no-pager status "$SERVICE_NAME" || true
