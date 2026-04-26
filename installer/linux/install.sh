#!/usr/bin/env bash
# WarpSocket - Linux installer / bootstrapper
#
# Two ways to run:
#   1. Local (from a clone):    sudo bash installer/linux/install.sh
#   2. Remote (via curl pipe):  curl -fsSL https://raw.githubusercontent.com/fcrespo07/WarpSocket/main/installer/linux/install.sh | sudo bash
#
# Environment overrides:
#   WARPSOCKET_COMPONENT=server|client    Skip the interactive prompt
#   WARPSOCKET_REPO_DIR=/path/to/clone    Use existing repo instead of cloning
#   WARPSOCKET_RUN_WIZARD=0               Skip the final `warpsocket-server setup`
#   WSTUNNEL_VERSION=v10.5.2              Pin a specific wstunnel release
#   WARPSOCKET_FORCE_IPV4=0               Disable the default IPv4-only mode for apt/curl
#                                         (default: 1 - many bridged-VM LANs have broken IPv6)

set -euo pipefail

# IPv4-only is the default: bridged VMs frequently get an IPv6 address via SLAAC
# from a router whose upstream doesn't actually route IPv6 -> curl/apt hang for
# minutes on TCP timeout per mirror. Override with WARPSOCKET_FORCE_IPV4=0.
FORCE_IPV4="${WARPSOCKET_FORCE_IPV4:-1}"
CURL_OPTS=(--fail --silent --show-error --location --connect-timeout 10 --max-time 300)
[[ "$FORCE_IPV4" == "1" ]] && CURL_OPTS+=(-4)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
readonly GITHUB_REPO="fcrespo07/WarpSocket"
readonly GITHUB_REPO_URL="https://github.com/${GITHUB_REPO}"
readonly INSTALL_PREFIX="/opt/warpsocket-server"
readonly BIN_LINK="/usr/local/bin/warpsocket-server"
readonly CLIENT_PREFIX="/opt/warpsocket-client"
readonly CLIENT_BIN_LINK="/usr/local/bin/warpsocket"
readonly CLIENT_SUDOERS="/etc/sudoers.d/warpsocket"
readonly CLIENT_HELPER="/usr/local/libexec/warpsocket-priv"
readonly WSTUNNEL_BIN="/usr/local/bin/wstunnel"
readonly DEFAULT_REPO_DIR="${HOME:-/root}/WarpSocket"

# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

info()  { printf '%s==>%s %s\n' "$CYAN" "$RESET" "$*"; }
ok()    { printf '  %s[OK]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '  %s[!]%s  %s\n' "$YELLOW" "$RESET" "$*"; }
err()   { printf '  %s[X]%s  %s\n' "$RED" "$RESET" "$*" >&2; }
die()   { err "$*"; exit 1; }

# Read from /dev/tty so prompts work even when piped via curl|bash
ask() {
    local prompt="$1" default="${2:-}" reply
    if [[ -n "$default" ]]; then
        read -rp "$prompt [$default]: " reply </dev/tty || true
        echo "${reply:-$default}"
    else
        read -rp "$prompt: " reply </dev/tty || true
        echo "$reply"
    fi
}

ask_choice() {
    local prompt="$1" reply
    while true; do
        read -rp "$prompt " reply </dev/tty || true
        case "$reply" in
            [Ss]|[Yy]|[Ss][Ii]) return 0 ;;
            [Nn]|[Nn][Oo])      return 1 ;;
            *) echo "Please answer s/n" ;;
        esac
    done
}

banner() {
    cat <<EOF
${BOLD}${CYAN}
   _      __                  ____           __        __
  | | /| / /__ _ _____  ___  / __/__  ____  / /_____  / /_
  | |/ |/ / _ \`/ __/ _ \\/ _ \\_\\ \\/ _ \\/ __/ /  '_/ -_) __/
  |__/|__/\\_,_/_/  \\_,_/ .__/___/\\___/\\__/_/\\_\\\\__/\\__/
                       /_/
${RESET}
  ${DIM}WireGuard over WebSocket - Linux installer${RESET}

EOF
}

# ----------------------------------------------------------------------------
# System detection
# ----------------------------------------------------------------------------
SUDO=""
require_root_or_sudo() {
    if [[ $EUID -eq 0 ]]; then
        SUDO=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        info "Running with sudo for privileged operations"
    else
        die "This installer needs root privileges. Re-run with: ${BOLD}sudo bash $0${RESET}"
    fi
}

PKG_MANAGER=""
PKG_INSTALL_CMD=""
PKG_UPDATE_CMD=""
detect_package_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MANAGER="apt"
        local apt_opts="-o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o Acquire::Retries=2"
        [[ "$FORCE_IPV4" == "1" ]] && apt_opts="$apt_opts -o Acquire::ForceIPv4=true"
        PKG_UPDATE_CMD="apt-get $apt_opts update -qq"
        PKG_INSTALL_CMD="DEBIAN_FRONTEND=noninteractive apt-get $apt_opts install -y -qq"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MANAGER="dnf"
        PKG_UPDATE_CMD="dnf check-update || true"
        PKG_INSTALL_CMD="dnf install -y -q"
    elif command -v pacman >/dev/null 2>&1; then
        PKG_MANAGER="pacman"
        PKG_UPDATE_CMD="pacman -Sy --noconfirm"
        PKG_INSTALL_CMD="pacman -S --noconfirm --needed"
    else
        die "No supported package manager found (apt/dnf/pacman). Install dependencies manually and rerun with WARPSOCKET_SKIP_DEPS=1."
    fi
    ok "Package manager: ${BOLD}${PKG_MANAGER}${RESET}"
}

DISTRO_ID=""
DISTRO_VERSION=""
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_VERSION="${VERSION_ID:-unknown}"
        ok "Distribution: ${BOLD}${PRETTY_NAME:-$DISTRO_ID $DISTRO_VERSION}${RESET}"
    else
        warn "Could not detect distribution (no /etc/os-release)"
    fi
}

# ----------------------------------------------------------------------------
# Python 3.11+ detection / install
# ----------------------------------------------------------------------------
PYTHON_BIN=""
find_python() {
    local py ver major minor
    for py in python3.13 python3.12 python3.11 python3; do
        if command -v "$py" >/dev/null 2>&1; then
            ver=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
            major="${ver%.*}"
            minor="${ver#*.}"
            if [[ "$major" -eq 3 && "$minor" -ge 11 ]]; then
                PYTHON_BIN="$(command -v "$py")"
                return 0
            fi
        fi
    done
    return 1
}

_add_deadsnakes_apt_source() {
    # `add-apt-repository -y ppa:deadsnakes/ppa` breaks on Linux Mint because
    # `lsb_release -cs` returns the Mint codename (vanessa/wilma/...) which has no
    # corresponding deadsnakes release -> either 404 or hours-long retry loop.
    # We instead read UBUNTU_CODENAME from /etc/os-release and write the apt
    # source manually, with a signed-by keyring.
    local ubuntu_codename keyring keyfile
    # shellcheck disable=SC1091
    ubuntu_codename=$(. /etc/os-release; echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")
    [[ -z "$ubuntu_codename" ]] && die "Could not determine Ubuntu base codename for deadsnakes PPA"

    info "Adding deadsnakes PPA (codename: $ubuntu_codename)"
    $SUDO bash -c "$PKG_INSTALL_CMD ca-certificates curl gnupg"

    keyring="/etc/apt/keyrings/deadsnakes.gpg"
    keyfile=$(mktemp)
    # Deadsnakes signing key: F23C5A6CF475977595C89F51BA6932366A755776
    info "Fetching deadsnakes signing key"
    curl "${CURL_OPTS[@]}" -o "$keyfile" \
        "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xF23C5A6CF475977595C89F51BA6932366A755776" \
        || die "Failed to fetch deadsnakes GPG key"
    $SUDO mkdir -p /etc/apt/keyrings
    $SUDO bash -c "gpg --dearmor < '$keyfile' > '$keyring'"
    rm -f "$keyfile"

    echo "deb [signed-by=$keyring] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu $ubuntu_codename main" \
        | $SUDO tee /etc/apt/sources.list.d/deadsnakes.list >/dev/null
}

install_python() {
    info "Installing Python 3.12 + venv..."
    case "$PKG_MANAGER" in
        apt)
            info "Refreshing apt index (this can take ~30s)"
            $SUDO bash -c "$PKG_UPDATE_CMD" >/dev/null
            # Try native first
            if $SUDO bash -c "$PKG_INSTALL_CMD python3.12 python3.12-venv" 2>/dev/null; then
                :
            else
                warn "python3.12 not in default repos - adding deadsnakes PPA"
                _add_deadsnakes_apt_source
                info "Refreshing apt index after adding PPA"
                $SUDO bash -c "$PKG_UPDATE_CMD" >/dev/null
                $SUDO bash -c "$PKG_INSTALL_CMD python3.12 python3.12-venv"
            fi
            ;;
        dnf)
            $SUDO bash -c "$PKG_INSTALL_CMD python3.12"
            ;;
        pacman)
            $SUDO bash -c "$PKG_INSTALL_CMD python"
            ;;
    esac
    find_python || die "Python 3.11+ install failed - install manually and rerun"
}

ensure_python() {
    if find_python; then
        ok "Python: ${BOLD}${PYTHON_BIN}${RESET} ($($PYTHON_BIN --version))"
    else
        warn "Python 3.11+ not found - installing"
        install_python
        ok "Python installed: ${BOLD}${PYTHON_BIN}${RESET}"
    fi
}

# ----------------------------------------------------------------------------
# WireGuard tools
# ----------------------------------------------------------------------------
ensure_wireguard() {
    if command -v wg >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1; then
        ok "WireGuard tools: ${BOLD}$(wg --version | head -1)${RESET}"
        return
    fi
    info "Installing wireguard-tools..."
    case "$PKG_MANAGER" in
        apt)    $SUDO bash -c "$PKG_INSTALL_CMD wireguard-tools" ;;
        dnf)    $SUDO bash -c "$PKG_INSTALL_CMD wireguard-tools" ;;
        pacman) $SUDO bash -c "$PKG_INSTALL_CMD wireguard-tools" ;;
    esac

    if ! $SUDO modprobe wireguard 2>/dev/null; then
        warn "Could not load wireguard kernel module - your kernel may be too old (need >= 5.6)"
    fi
    ok "WireGuard installed"
}

# ----------------------------------------------------------------------------
# wstunnel binary
# ----------------------------------------------------------------------------
# Last-resort version when api.github.com is unreachable / rate-limited.
# Bump occasionally; users can always override with WSTUNNEL_VERSION=vX.Y.Z.
readonly WSTUNNEL_FALLBACK_VERSION="v10.5.2"

fetch_latest_wstunnel_version() {
    # Prints the tag (vX.Y.Z) on stdout, or returns non-zero with a diagnostic
    # on stderr. Never triggers errexit in the caller - wrap with `|| true`.
    local response
    if ! response=$(curl "${CURL_OPTS[@]}" \
        "https://api.github.com/repos/erebe/wstunnel/releases/latest" 2>&1); then
        printf '  %s[!]%s GitHub API query failed: %s\n' \
            "$YELLOW" "$RESET" "$(printf '%s' "$response" | head -1)" >&2
        return 1
    fi
    local tag
    tag=$(printf '%s\n' "$response" | grep -m1 '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')
    if [[ -z "$tag" ]]; then
        printf '  %s[!]%s GitHub API response did not contain a tag_name (rate limited?)\n' \
            "$YELLOW" "$RESET" >&2
        return 1
    fi
    printf '%s\n' "$tag"
}

ensure_wstunnel() {
    if command -v wstunnel >/dev/null 2>&1; then
        ok "wstunnel: ${BOLD}$(wstunnel --version 2>&1 | head -1 || echo present)${RESET}"
        return
    fi

    info "Installing wstunnel..."
    local version arch tarball url tmpdir
    if [[ -n "${WSTUNNEL_VERSION:-}" ]]; then
        version="$WSTUNNEL_VERSION"
        info "Using pinned wstunnel version: $version"
    else
        info "Querying GitHub for latest wstunnel release"
        version=$(fetch_latest_wstunnel_version || true)
        if [[ -z "$version" ]]; then
            warn "Falling back to known-good version $WSTUNNEL_FALLBACK_VERSION"
            warn "Override with WSTUNNEL_VERSION=vX.Y.Z if you need a different one."
            version="$WSTUNNEL_FALLBACK_VERSION"
        fi
    fi

    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch="amd64"   ;;
        aarch64) arch="arm64"   ;;
        armv7l)  arch="armv7"   ;;
        *) die "Unsupported architecture: $arch" ;;
    esac

    tarball="wstunnel_${version#v}_linux_${arch}.tar.gz"
    url="https://github.com/erebe/wstunnel/releases/download/${version}/${tarball}"

    tmpdir=$(mktemp -d)
    info "Downloading $url (~5 MB)"
    curl "${CURL_OPTS[@]}" -o "$tmpdir/$tarball" "$url" || die "Download failed: $url"
    tar -xzf "$tmpdir/$tarball" -C "$tmpdir"
    $SUDO install -m 0755 "$tmpdir/wstunnel" "$WSTUNNEL_BIN"
    rm -rf "$tmpdir"
    ok "wstunnel installed: ${BOLD}${WSTUNNEL_BIN}${RESET}"
}

# ----------------------------------------------------------------------------
# Repository: locate or clone
# ----------------------------------------------------------------------------
REPO_DIR=""
locate_repo() {
    # Override via env
    if [[ -n "${WARPSOCKET_REPO_DIR:-}" ]]; then
        REPO_DIR="$WARPSOCKET_REPO_DIR"
        [[ -d "$REPO_DIR" ]] || die "WARPSOCKET_REPO_DIR=$REPO_DIR does not exist"
        ok "Using repo at: ${BOLD}${REPO_DIR}${RESET}"
        return
    fi

    # Detect if we're being run from inside a clone
    local script_dir candidate
    script_dir="$( cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd )"
    candidate="$( cd "$script_dir/../.." && pwd 2>/dev/null || true )"
    if [[ -n "$candidate" && -f "$candidate/CLAUDE.md" && -d "$candidate/server" ]]; then
        REPO_DIR="$candidate"
        ok "Using local checkout: ${BOLD}${REPO_DIR}${RESET}"
        return
    fi

    # Need to clone
    info "Repository not found locally - cloning"
    if ! command -v git >/dev/null 2>&1; then
        $SUDO bash -c "$PKG_INSTALL_CMD git"
    fi

    REPO_DIR="$DEFAULT_REPO_DIR"
    if [[ -d "$REPO_DIR" ]]; then
        warn "Directory $REPO_DIR exists - using as-is"
        return
    fi

    # Prefer gh if authenticated, fall back to https git.
    # GIT_TERMINAL_PROMPT=0 prevents git from blocking on a credential prompt
    # when the repo is private and no auth is available - fail fast instead.
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        info "Cloning via gh (authenticated)"
        gh repo clone "$GITHUB_REPO" "$REPO_DIR"
    else
        warn "gh not authenticated - trying HTTPS clone (will fail fast if repo is private)"
        if ! GIT_TERMINAL_PROMPT=0 git clone "$GITHUB_REPO_URL" "$REPO_DIR" 2>&1; then
            die "git clone failed. If the repo is private, either:
    1) Install + auth gh:  ${BOLD}gh auth login${RESET}  and re-run this installer, or
    2) Clone manually and re-run with: ${BOLD}WARPSOCKET_REPO_DIR=/path/to/clone${RESET}"
        fi
    fi
    ok "Cloned to: ${BOLD}${REPO_DIR}${RESET}"
}

# ----------------------------------------------------------------------------
# Server install
# ----------------------------------------------------------------------------
install_server() {
    info "Installing WarpSocket server to ${BOLD}${INSTALL_PREFIX}${RESET}"

    [[ -d "$REPO_DIR/server" ]] || die "Server source not found at $REPO_DIR/server"

    $SUDO mkdir -p "$INSTALL_PREFIX"
    if [[ ! -x "$INSTALL_PREFIX/.venv/bin/pip" ]]; then
        if [[ -d "$INSTALL_PREFIX/.venv" ]]; then
            warn "Existing venv at $INSTALL_PREFIX/.venv is broken - recreating"
            $SUDO rm -rf "$INSTALL_PREFIX/.venv"
        fi
        info "Creating venv with $PYTHON_BIN"
        $SUDO "$PYTHON_BIN" -m venv "$INSTALL_PREFIX/.venv"
    else
        ok "venv already exists - reusing"
    fi

    info "Installing Python dependencies (this may take ~30-60s)"
    $SUDO "$INSTALL_PREFIX/.venv/bin/pip" install --upgrade --disable-pip-version-check pip
    $SUDO "$INSTALL_PREFIX/.venv/bin/pip" install --disable-pip-version-check -e "$REPO_DIR/server"

    # Symlink so user can call `warpsocket-server` directly
    $SUDO ln -sf "$INSTALL_PREFIX/.venv/bin/warpsocket-server" "$BIN_LINK"
    ok "Linked: ${BOLD}${BIN_LINK}${RESET} -> ${INSTALL_PREFIX}/.venv/bin/warpsocket-server"

    ok "Server installed (version: $(warpsocket-server --version | awk '{print $2}'))"
}

run_setup_wizard() {
    if [[ "${WARPSOCKET_RUN_WIZARD:-1}" == "0" ]]; then
        info "Skipping setup wizard (WARPSOCKET_RUN_WIZARD=0)"
        echo
        echo "${BOLD}Run the wizard later with:${RESET}"
        echo "  sudo warpsocket-server setup"
        return
    fi
    echo
    info "Launching the setup wizard..."
    echo
    $SUDO warpsocket-server setup
}

# ----------------------------------------------------------------------------
# Client install
# ----------------------------------------------------------------------------
ensure_client_system_deps() {
    info "Installing client GUI dependencies (tkinter + tray indicator)"
    case "$PKG_MANAGER" in
        apt)
            # python3-tk: tkinter base for customtkinter
            # gir + libayatana: lets pystray show in GNOME/Cinnamon shells that
            #                   route trays through AppIndicator
            $SUDO bash -c "$PKG_INSTALL_CMD python3-tk gir1.2-ayatanaappindicator3-0.1" \
                || warn "Some optional GUI deps failed - tray may render but with reduced features"
            ;;
        dnf)
            $SUDO bash -c "$PKG_INSTALL_CMD python3-tkinter libappindicator-gtk3" \
                || warn "Some optional GUI deps failed"
            ;;
        pacman)
            $SUDO bash -c "$PKG_INSTALL_CMD tk libappindicator-gtk3" \
                || warn "Some optional GUI deps failed"
            ;;
    esac
}

# Resolve the desktop user the tray will run as. When this script is invoked
# via `sudo`, $SUDO_USER points to the real user; otherwise we ask.
TARGET_USER=""
TARGET_HOME=""
resolve_target_user() {
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        TARGET_USER="$SUDO_USER"
    elif [[ -n "${WARPSOCKET_USER:-}" ]]; then
        TARGET_USER="$WARPSOCKET_USER"
    else
        echo
        TARGET_USER=$(ask "Desktop user that will run the tray app" "${USER:-}")
    fi
    [[ -z "$TARGET_USER" ]] && die "No target user specified"
    id "$TARGET_USER" >/dev/null 2>&1 || die "User '$TARGET_USER' does not exist"
    TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    [[ -d "$TARGET_HOME" ]] || die "Home directory of '$TARGET_USER' not found ($TARGET_HOME)"
    ok "Tray will run as: ${BOLD}${TARGET_USER}${RESET} (home: ${TARGET_HOME})"
}

write_client_helper() {
    # All privileged tunnel ops are funnelled through this helper. It validates
    # its inputs (interface name, IP) so a malicious caller can't escape the
    # whitelisted operation set even though the sudoers rule grants it root.
    info "Installing privileged helper at ${CLIENT_HELPER}"
    $SUDO install -d -m 0755 "$(dirname "$CLIENT_HELPER")"

    local tmp
    tmp=$(mktemp)
    cat >"$tmp" <<'HELPER_EOF'
#!/usr/bin/env bash
# WarpSocket privileged helper. Installed by the WarpSocket installer.
# Invoked via `sudo -n` by the user-mode tray app. Single sudoers entry
# whitelists this script for the desktop user.
set -euo pipefail

WG_CONF_DIR="/etc/wireguard"
NAME_RE='^[A-Za-z0-9_=+.-]{1,15}$'
IPV4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}$'

die() { echo "warpsocket-priv: $*" >&2; exit 2; }
need_name() { [[ "${1:-}" =~ $NAME_RE ]] || die "invalid tunnel name: ${1:-<empty>}"; }
need_ipv4() { [[ "${1:-}" =~ $IPV4_RE ]] || die "invalid IPv4: ${1:-<empty>}"; }

cmd="${1:-}"
shift || true

case "$cmd" in
    up)
        name="${1:-}"; need_name "$name"
        umask 077
        mkdir -p -m 0700 "$WG_CONF_DIR"
        conf="$WG_CONF_DIR/$name.conf"
        # Read conf body from stdin so the unprivileged caller never has to
        # write the WG private key to a user-readable temp file.
        cat > "$conf"
        chmod 0600 "$conf"
        wg-quick down "$name" >/dev/null 2>&1 || true
        exec wg-quick up "$name"
        ;;
    down)
        name="${1:-}"; need_name "$name"
        wg-quick down "$name" >/dev/null 2>&1 || true
        rm -f "$WG_CONF_DIR/$name.conf"
        ;;
    is-active)
        name="${1:-}"; need_name "$name"
        wg show "$name" >/dev/null 2>&1
        ;;
    route-add)
        ip="${1:-}"; gw="${2:-}"
        need_ipv4 "$ip"; need_ipv4 "$gw"
        ip route add "$ip/32" via "$gw"
        ;;
    route-del)
        ip="${1:-}"; need_ipv4 "$ip"
        ip route del "$ip/32" >/dev/null 2>&1 || true
        ;;
    *) die "unknown command: ${cmd:-<empty>}" ;;
esac
HELPER_EOF
    $SUDO install -m 0755 -o root -g root "$tmp" "$CLIENT_HELPER"
    rm -f "$tmp"
    ok "Helper installed: ${BOLD}${CLIENT_HELPER}${RESET}"
}

write_client_sudoers() {
    # Single entry: whitelist the helper. The helper itself enforces input
    # validation, so this is the entire blast radius granted to $TARGET_USER.
    info "Writing sudoers rule to ${CLIENT_SUDOERS}"
    local tmp
    tmp=$(mktemp)
    cat >"$tmp" <<EOF
# Managed by WarpSocket installer - do not edit by hand.
# Lets the tray app (running as $TARGET_USER) invoke the WarpSocket
# privileged helper without a password prompt. The helper validates inputs.
$TARGET_USER ALL=(root) NOPASSWD: $CLIENT_HELPER
EOF
    chmod 0440 "$tmp"
    if ! $SUDO visudo -cf "$tmp" >/dev/null; then
        rm -f "$tmp"
        die "Generated sudoers file failed validation - aborting"
    fi
    $SUDO install -m 0440 -o root -g root "$tmp" "$CLIENT_SUDOERS"
    rm -f "$tmp"
    ok "sudoers rule installed"
}

write_client_autostart() {
    local autostart_dir="$TARGET_HOME/.config/autostart"
    local desktop_file="$autostart_dir/warpsocket.desktop"
    local icon_path="$REPO_DIR/client/warpsocket/resources/app_icon.png"

    info "Creating autostart entry: $desktop_file"
    $SUDO -u "$TARGET_USER" mkdir -p "$autostart_dir"
    $SUDO -u "$TARGET_USER" tee "$desktop_file" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=WarpSocket
Comment=WireGuard over WebSocket - tray client
Exec=$CLIENT_BIN_LINK
Icon=$icon_path
Terminal=false
X-GNOME-Autostart-enabled=true
Categories=Network;
EOF
    ok "Autostart entry installed (will launch at next login)"
}

install_client() {
    info "Installing WarpSocket client to ${BOLD}${CLIENT_PREFIX}${RESET}"
    [[ -d "$REPO_DIR/client" ]] || die "Client source not found at $REPO_DIR/client"

    resolve_target_user
    ensure_client_system_deps

    $SUDO mkdir -p "$CLIENT_PREFIX"
    if [[ ! -x "$CLIENT_PREFIX/.venv/bin/pip" ]]; then
        if [[ -d "$CLIENT_PREFIX/.venv" ]]; then
            warn "Existing client venv at $CLIENT_PREFIX/.venv is broken - recreating"
            $SUDO rm -rf "$CLIENT_PREFIX/.venv"
        fi
        info "Creating client venv with $PYTHON_BIN"
        $SUDO "$PYTHON_BIN" -m venv "$CLIENT_PREFIX/.venv"
    else
        ok "Client venv already exists - reusing"
    fi

    info "Installing Python dependencies (this may take ~30-60s)"
    $SUDO "$CLIENT_PREFIX/.venv/bin/pip" install --upgrade --disable-pip-version-check pip
    $SUDO "$CLIENT_PREFIX/.venv/bin/pip" install --disable-pip-version-check -e "$REPO_DIR/client"

    $SUDO ln -sf "$CLIENT_PREFIX/.venv/bin/warpsocket" "$CLIENT_BIN_LINK"
    ok "Linked: ${BOLD}${CLIENT_BIN_LINK}${RESET} -> ${CLIENT_PREFIX}/.venv/bin/warpsocket"

    write_client_helper
    write_client_sudoers
    write_client_autostart

    cat <<EOF

${BOLD}${GREEN}Client installed.${RESET}

  ${BOLD}Next steps:${RESET}
    1. Drop your ${BOLD}.warpcfg${RESET} file somewhere accessible by ${TARGET_USER}.
    2. Launch ${BOLD}warpsocket${RESET} (or log out/in to trigger autostart).
    3. The first run prompts for the .warpcfg via the import wizard.

  ${DIM}Autostart entry: $TARGET_HOME/.config/autostart/warpsocket.desktop${RESET}
  ${DIM}Privileged helper: $CLIENT_HELPER${RESET}
  ${DIM}Sudoers rule: $CLIENT_SUDOERS  (revoke with: sudo rm $CLIENT_SUDOERS)${RESET}

EOF
}

# ----------------------------------------------------------------------------
# Component picker
# ----------------------------------------------------------------------------
pick_component() {
    local component="${WARPSOCKET_COMPONENT:-}"
    if [[ -z "$component" ]]; then
        echo
        echo "${BOLD}Which component do you want to install?${RESET}"
        echo "  ${BOLD}1)${RESET} Server  - runs wstunnel + WireGuard, accepts client connections"
        echo "  ${BOLD}2)${RESET} Client  - connects to a WarpSocket server"
        echo
        local choice
        while true; do
            choice=$(ask "Choice [1/2]" "1")
            case "$choice" in
                1|server) component=server; break ;;
                2|client) component=client; break ;;
                *) echo "Invalid choice - pick 1 or 2" ;;
            esac
        done
    fi

    case "$component" in
        server) install_server; run_setup_wizard ;;
        client) install_client ;;
        *) die "Unknown component: $component (must be 'server' or 'client')" ;;
    esac
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
main() {
    banner
    require_root_or_sudo
    detect_distro
    detect_package_manager

    # Both server and client need: Python 3.11+, wireguard-tools, wstunnel.
    # GUI-only client deps (tkinter, appindicator) are installed inside
    # install_client to keep the server install minimal.
    info "Preparing system dependencies"
    ensure_python
    ensure_wireguard
    ensure_wstunnel

    locate_repo
    pick_component

    echo
    ok "${BOLD}${GREEN}All done.${RESET}"
}

main "$@"
