#!/usr/bin/env bash
# ============================================================
# yoosee-nvr - host hardening
#
# Optional. It locks the box down to the local network, restricts file
# permissions on anything holding credentials, and generates the TLS
# certificate the dashboard uses.
#
#   sudo bash scripts/harden.sh --dry-run     # print what would change
#   sudo bash scripts/harden.sh               # apply
#
# READ THIS FIRST: the firewall step sets the INPUT policy to DROP. If the
# detected subnet is wrong and you are connected over SSH, you will lock
# yourself out. --dry-run shows the detected subnet; pass --subnet to override.
# ============================================================

set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
log()  { echo "${GREEN}[harden]${NC} $1"; }
warn() { echo "${YELLOW}[warn]${NC} $1"; }
err()  { echo "${RED}[error]${NC} $1" >&2; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
SUBNET=""
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SKIP_FIREWALL=0
SKIP_SSH=0

usage() {
    cat <<EOF
Usage: sudo bash scripts/harden.sh [options]

  --dry-run           Show what would change, change nothing
  --subnet CIDR       Local network allowed to reach SSH and the dashboard
                      (default: auto-detected from the default route)
  --user NAME         Account that owns the install (default: $SERVICE_USER)
  --skip-firewall     Do not touch iptables
  --skip-ssh          Do not touch the SSH configuration
  -h, --help          This message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --subnet) SUBNET="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --skip-firewall) SKIP_FIREWALL=1; shift ;;
        --skip-ssh) SKIP_SSH=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) err "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    err "Run as root: sudo bash $0"
    exit 1
fi

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  would run: $*"
    else
        "$@"
    fi
}

# ------------------------------------------------------------
# Detect the local subnet from the interface holding the default route.
# ------------------------------------------------------------
detect_subnet() {
    local iface cidr
    iface=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
    [[ -z "$iface" ]] && return 1
    cidr=$(ip -4 -o addr show dev "$iface" | awk '{print $4; exit}')
    [[ -z "$cidr" ]] && return 1
    # Normalize 192.168.18.37/24 -> 192.168.18.0/24
    python3 - "$cidr" <<'PY'
import ipaddress, sys
print(ipaddress.ip_interface(sys.argv[1]).network)
PY
}

if [[ -z "$SUBNET" ]]; then
    SUBNET=$(detect_subnet || true)
    if [[ -z "$SUBNET" ]]; then
        err "Could not detect the local subnet. Pass --subnet, e.g. --subnet 192.168.1.0/24"
        exit 1
    fi
fi

# Read the dashboard port from .env, falling back to the documented default.
WEB_PORT=9847
if [[ -f "$PROJECT_DIR/.env" ]]; then
    env_port=$(grep -E '^WEB_PORT=' "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    [[ -n "$env_port" ]] && WEB_PORT="$env_port"
fi

echo
echo "============================================"
echo "  yoosee-nvr hardening"
echo "============================================"
echo "  Project:   $PROJECT_DIR"
echo "  User:      $SERVICE_USER"
echo "  Subnet:    $SUBNET"
echo "  Dashboard: port $WEB_PORT"
[[ $DRY_RUN -eq 1 ]] && echo "  Mode:      DRY RUN (nothing will change)"
echo

if [[ $DRY_RUN -eq 0 && $SKIP_FIREWALL -eq 0 ]]; then
    warn "The firewall step will DROP every inbound connection except from $SUBNET."
    warn "If you are on SSH from outside that range, you will lose access."
    read -r -p "Type the subnet again to confirm: " confirm
    if [[ "$confirm" != "$SUBNET" ]]; then
        err "Confirmation did not match. Aborting."
        exit 1
    fi
fi

# ------------------------------------------------------------
# 1. Firewall
# ------------------------------------------------------------
if [[ $SKIP_FIREWALL -eq 1 ]]; then
    log "1/5 Firewall: skipped"
else
    log "1/5 Firewall: allowing SSH and the dashboard from $SUBNET only"

    # Order matters: allow loopback and established traffic BEFORE the policy
    # flips to DROP, so the current SSH session survives the change.
    run iptables -F INPUT
    run iptables -A INPUT -i lo -j ACCEPT
    run iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    run iptables -A INPUT -p tcp --dport 22 -s "$SUBNET" -j ACCEPT
    run iptables -A INPUT -p tcp --dport "$WEB_PORT" -s "$SUBNET" -j ACCEPT
    run iptables -A INPUT -p icmp -s "$SUBNET" -j ACCEPT
    run iptables -P INPUT DROP

    if command -v iptables-save >/dev/null 2>&1; then
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "  would run: iptables-save > /etc/iptables.rules"
        else
            iptables-save > /etc/iptables.rules
            log "Rules saved to /etc/iptables.rules"
            warn "Install iptables-persistent (or netfilter-persistent) to restore them on boot"
        fi
    fi
fi

# ------------------------------------------------------------
# 2. SSH
# ------------------------------------------------------------
if [[ $SKIP_SSH -eq 1 ]]; then
    log "2/5 SSH: skipped"
else
    log "2/5 SSH: disabling root login, limiting auth attempts"
    SSH_DROPIN=/etc/ssh/sshd_config.d/yoosee-nvr.conf
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  would write: $SSH_DROPIN"
    else
        mkdir -p /etc/ssh/sshd_config.d
        cat > "$SSH_DROPIN" <<'EOSSH'
# yoosee-nvr hardening
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
X11Forwarding no
PermitEmptyPasswords no
EOSSH
        # Validate before restarting: a bad config plus a restart means no SSH.
        if sshd -t; then
            systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true
            log "SSH configuration applied"
        else
            rm -f "$SSH_DROPIN"
            err "sshd rejected the configuration; reverted, SSH untouched"
        fi
    fi
fi

# ------------------------------------------------------------
# 3. File permissions
# ------------------------------------------------------------
log "3/5 Permissions: restricting credentials and footage to $SERVICE_USER"

run chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"

for secret in "$PROJECT_DIR/.env" "$PROJECT_DIR/.flask_secret_key"; do
    [[ -f "$secret" ]] && run chmod 600 "$secret"
done

RECORDING_DIR="$PROJECT_DIR/recordings"
if [[ -f "$PROJECT_DIR/.env" ]]; then
    env_dir=$(grep -E '^RECORDING_DIR=' "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '[:space:]')
    if [[ -n "$env_dir" ]]; then
        [[ "$env_dir" = /* ]] && RECORDING_DIR="$env_dir" || RECORDING_DIR="$PROJECT_DIR/$env_dir"
    fi
fi

if [[ -d "$RECORDING_DIR" ]]; then
    run chown -R "$SERVICE_USER":"$SERVICE_USER" "$RECORDING_DIR"
    run chmod 700 "$RECORDING_DIR"
    log "Footage restricted: $RECORDING_DIR"
fi

RCLONE_CONF="/home/$SERVICE_USER/.config/rclone/rclone.conf"
if [[ -f "$RCLONE_CONF" ]]; then
    run chmod 600 "$RCLONE_CONF"
    log "rclone token restricted (holds your cloud OAuth refresh token)"
fi

# ------------------------------------------------------------
# 4. TLS certificate
# ------------------------------------------------------------
log "4/5 TLS: self-signed certificate for the dashboard"
CERT_DIR="$PROJECT_DIR/certs"

if [[ -f "$CERT_DIR/server.crt" ]]; then
    log "Certificate already exists, keeping it"
else
    run mkdir -p "$CERT_DIR"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  would run: openssl req -x509 ... -out $CERT_DIR/server.crt"
    else
        openssl req -x509 -newkey rsa:4096 -nodes \
            -keyout "$CERT_DIR/server.key" \
            -out "$CERT_DIR/server.crt" \
            -days 3650 \
            -subj "/CN=yoosee-nvr/O=Home/C=US" \
            2>/dev/null
        chown -R "$SERVICE_USER":"$SERVICE_USER" "$CERT_DIR"
        chmod 700 "$CERT_DIR"
        chmod 600 "$CERT_DIR/server.key"
        chmod 644 "$CERT_DIR/server.crt"
        log "Certificate generated (self-signed, 10 years)"
        warn "Browsers will warn about a self-signed certificate; that is expected"
    fi
fi

# ------------------------------------------------------------
# 5. Configuration check
# ------------------------------------------------------------
log "5/5 Checking configuration"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    warn "No .env found. Copy .env.example to .env before starting."
else
    if grep -qE '^DASHBOARD_PASS=change-me' "$PROJECT_DIR/.env"; then
        err "DASHBOARD_PASS is still the example value. Change it."
    fi
    if grep -qE '^CAMERA_PASS=change-me' "$PROJECT_DIR/.env"; then
        err "CAMERA_PASS is still the example value. Change it."
    fi
    if grep -qE '^DASHBOARD_PASS=' "$PROJECT_DIR/.env" && \
       ! grep -qE '^DASHBOARD_PASS_HASH=' "$PROJECT_DIR/.env"; then
        warn "Dashboard password is stored in plaintext."
        warn "Generate a hash: python3 scripts/hash_password.py"
    fi
fi

echo
echo "============================================"
if [[ $DRY_RUN -eq 1 ]]; then
    echo "  DRY RUN complete - nothing changed"
else
    echo "  ${GREEN}Hardening complete${NC}"
fi
echo "============================================"
echo
echo "  Firewall:    inbound limited to $SUBNET"
echo "  SSH:         root login disabled, 3 auth attempts"
echo "  Permissions: secrets 600, footage 700"
echo "  TLS:         $CERT_DIR/server.crt"
echo
echo "  Restart the service to pick up the changes:"
echo "    sudo systemctl restart yoosee-nvr"
echo
