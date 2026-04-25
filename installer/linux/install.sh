#!/usr/bin/env bash
# WarpSocket — Linux installer / bootstrapper
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

set -euo pipefail

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
readonly GITHUB_REPO="fcrespo07/WarpSocket"
readonly GITHUB_REPO_URL="https://github.com/${GITHUB_REPO}"
readonly INSTALL_PREFIX="/opt/warpsocket-server"
readonly BIN_LINK="/usr/local/bin/warpsocket-server"
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
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '  %s⚠%s %s\n' "$YELLOW" "$RESET" "$*"; }
err()   { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; }
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
  ${DIM}WireGuard over WebSocket — Linux installer${RESET}

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
        PKG_UPDATE_CMD="apt-get update -qq"
        PKG_INSTALL_CMD="DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"
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

install_python() {
    info "Installing Python 3.12 + venv..."
    case "$PKG_MANAGER" in
        apt)
            $SUDO bash -c "$PKG_UPDATE_CMD" >/dev/null
            # Try native first
            if $SUDO bash -c "$PKG_INSTALL_CMD python3.12 python3.12-venv" 2>/dev/null; then
                :
            else
                # Fallback: deadsnakes PPA (Ubuntu 22.04 / Mint 21.x)
                warn "python3.12 not in default repos — adding deadsnakes PPA"
                $SUDO bash -c "$PKG_INSTALL_CMD software-properties-common"
                $SUDO add-apt-repository -y ppa:deadsnakes/ppa
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
    find_python || die "Python 3.11+ install failed — install manually and rerun"
}

ensure_python() {
    if find_python; then
        ok "Python: ${BOLD}${PYTHON_BIN}${RESET} ($($PYTHON_BIN --version))"
    else
        warn "Python 3.11+ not found — installing"
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
        warn "Could not load wireguard kernel module — your kernel may be too old (need >= 5.6)"
    fi
    ok "WireGuard installed"
}

# ----------------------------------------------------------------------------
# wstunnel binary
# ----------------------------------------------------------------------------
fetch_latest_wstunnel_version() {
    curl -fsSL "https://api.github.com/repos/erebe/wstunnel/releases/latest" 2>/dev/null \
        | grep -m1 '"tag_name":' \
        | sed -E 's/.*"([^"]+)".*/\1/'
}

ensure_wstunnel() {
    if command -v wstunnel >/dev/null 2>&1; then
        ok "wstunnel: ${BOLD}$(wstunnel --version 2>&1 | head -1 || echo present)${RESET}"
        return
    fi

    info "Installing wstunnel..."
    local version arch tarball url tmpdir
    version="${WSTUNNEL_VERSION:-$(fetch_latest_wstunnel_version)}"
    [[ -z "$version" ]] && die "Could not determine wstunnel version. Set WSTUNNEL_VERSION=vX.Y.Z and retry."

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
    info "Downloading $url"
    curl -fsSL -o "$tmpdir/$tarball" "$url" || die "Download failed: $url"
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
    info "Repository not found locally — cloning"
    if ! command -v git >/dev/null 2>&1; then
        $SUDO bash -c "$PKG_INSTALL_CMD git"
    fi

    REPO_DIR="$DEFAULT_REPO_DIR"
    if [[ -d "$REPO_DIR" ]]; then
        warn "Directory $REPO_DIR exists — using as-is"
        return
    fi

    # Prefer gh if authenticated, fall back to https git
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh repo clone "$GITHUB_REPO" "$REPO_DIR"
    else
        warn "gh not authenticated — cloning via HTTPS (will fail if repo is private)"
        git clone "$GITHUB_REPO_URL" "$REPO_DIR" || die \
            "git clone failed. If the repo is private, run ${BOLD}gh auth login${RESET} first \
or set ${BOLD}WARPSOCKET_REPO_DIR=/path/to/already/cloned/repo${RESET}."
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
    if [[ ! -d "$INSTALL_PREFIX/.venv" ]]; then
        info "Creating venv with $PYTHON_BIN"
        $SUDO "$PYTHON_BIN" -m venv "$INSTALL_PREFIX/.venv"
    else
        ok "venv already exists — reusing"
    fi

    info "Installing Python dependencies (this may take ~30s)"
    $SUDO "$INSTALL_PREFIX/.venv/bin/pip" install --quiet --upgrade pip
    $SUDO "$INSTALL_PREFIX/.venv/bin/pip" install --quiet -e "$REPO_DIR/server"

    # Symlink so user can call `warpsocket-server` directly
    $SUDO ln -sf "$INSTALL_PREFIX/.venv/bin/warpsocket-server" "$BIN_LINK"
    ok "Linked: ${BOLD}${BIN_LINK}${RESET} → ${INSTALL_PREFIX}/.venv/bin/warpsocket-server"

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
# Client install (Linux client not yet supported)
# ----------------------------------------------------------------------------
install_client() {
    cat <<EOF

${YELLOW}${BOLD}⚠ Linux client not yet implemented${RESET}

The Windows client is fully working. Linux/macOS clients are next on the roadmap.
The platform abstraction layer (LinuxPlatform in client/warpsocket/platforms/linux.py)
is currently a stub.

For now, only the ${BOLD}server${RESET} component is available on Linux.
Re-run this script and choose the server, or install on a Windows machine.

EOF
    exit 0
}

# ----------------------------------------------------------------------------
# Component picker
# ----------------------------------------------------------------------------
pick_component() {
    local component="${WARPSOCKET_COMPONENT:-}"
    if [[ -z "$component" ]]; then
        echo
        echo "${BOLD}Which component do you want to install?${RESET}"
        echo "  ${BOLD}1)${RESET} Server  — runs wstunnel + WireGuard, accepts client connections"
        echo "  ${BOLD}2)${RESET} Client  — connects to a WarpSocket server"
        echo
        local choice
        while true; do
            choice=$(ask "Choice [1/2]" "1")
            case "$choice" in
                1|server) component=server; break ;;
                2|client) component=client; break ;;
                *) echo "Invalid choice — pick 1 or 2" ;;
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

    info "Component selection"
    local will_install_server=1
    if [[ "${WARPSOCKET_COMPONENT:-server}" == "client" ]]; then
        will_install_server=0
    fi

    if [[ "$will_install_server" -eq 1 ]]; then
        info "Preparing system dependencies"
        ensure_python
        ensure_wireguard
        ensure_wstunnel
    fi

    locate_repo
    pick_component

    echo
    ok "${BOLD}${GREEN}All done.${RESET}"
}

main "$@"
