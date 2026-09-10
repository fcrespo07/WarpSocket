#!/usr/bin/env bash
# OutWarp - Linux installer / bootstrapper
#
# Installs the OutWarp client or server as a pipx-managed venv under
# /opt/pipx/venvs/outwarp-{client,server}/, exposing the entry-points under
# /usr/local/bin/. The system-wide pipx layout (PIPX_HOME=/opt/pipx,
# PIPX_BIN_DIR=/usr/local/bin) keeps every system service - the systemd unit,
# the autostart desktop file, the privileged helper - pointing at a single
# stable binary path that survives in-place upgrades.
#
# Two ways to run:
#   1. Local (from a clone):    sudo bash installer/linux/install.sh
#   2. Remote (via curl pipe):  curl -fsSL https://raw.githubusercontent.com/fcrespo07/OutWarp/main/installer/linux/install.sh | sudo bash
#
# Environment overrides:
#   OUTWARP_COMPONENT=server|client       Skip the interactive prompt
#   OUTWARP_VERSION=v0.5.5                Pin a specific OutWarp release tag
#                                         (default: latest from GitHub Releases)
#   OUTWARP_REPO_DIR=/path/to/clone       Dev mode: install from local source
#                                         instead of downloading a wheel
#   OUTWARP_RUN_WIZARD=0                  Skip the final `outwarp-server setup`
#   OUTWARP_SERVER_GUI=1                  Install the pywebview server GUI too
#                                         (default: auto-detect via $DISPLAY/$WAYLAND_DISPLAY)
#   WSTUNNEL_VERSION=v10.5.2              Override the pinned wstunnel release
#                                         (default: the version OutWarp pins;
#                                         only change it if you move the server too)
#   OUTWARP_FORCE_IPV4=0                  Disable the default IPv4-only mode for apt/curl
#                                         (default: 1 - many bridged-VM LANs have broken IPv6)
#   OUTWARP_SKIP_CHECKSUM=1               Skip SHA256SUMS verification of the downloaded
#                                         wheel (only honoured on legacy releases that
#                                         pre-date the manifest; never use as a workaround
#                                         for a fetch failure).

set -euo pipefail

# IPv4-only is the default: bridged VMs frequently get an IPv6 address via SLAAC
# from a router whose upstream doesn't actually route IPv6 -> curl/apt hang for
# minutes on TCP timeout per mirror. Override with OUTWARP_FORCE_IPV4=0.
FORCE_IPV4="${OUTWARP_FORCE_IPV4:-1}"
# --retry 3 handles transient TCP RSTs and HTTP 5xx from GitHub's CDN without
# making the user re-run the whole installer; --retry-connrefused covers the
# (rare) DNS/SYN-level flake. --max-time bounds the worst case per attempt so a
# stuck mirror cannot stall the install indefinitely.
CURL_OPTS=(
    --fail --silent --show-error --location
    --connect-timeout 10 --max-time 300
    --retry 3 --retry-delay 2 --retry-connrefused
)
[[ "$FORCE_IPV4" == "1" ]] && CURL_OPTS+=(-4)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
readonly GITHUB_REPO="fcrespo07/OutWarp"
readonly GITHUB_REPO_URL="https://github.com/${GITHUB_REPO}"
# pipx layout: PIPX_HOME holds the venvs (one per app under venvs/<app>/);
# PIPX_BIN_DIR holds the exposed entry-points. Setting them via env keeps the
# script working on pipx >= 1.0 (where the `--global` flag does not yet exist)
# while still producing the same on-disk result as `pipx install --global`.
readonly PIPX_HOME="/opt/pipx"
readonly PIPX_BIN_DIR="/usr/local/bin"
readonly CLIENT_BIN_LINK="${PIPX_BIN_DIR}/outwarp"
readonly CLIENT_CLI_BIN_LINK="${PIPX_BIN_DIR}/outwarp-cli"
readonly SERVER_BIN_LINK="${PIPX_BIN_DIR}/outwarp-server"
readonly SERVER_GUI_BIN_LINK="${PIPX_BIN_DIR}/outwarp-server-gui"
readonly CLIENT_SUDOERS="/etc/sudoers.d/outwarp"
readonly CLIENT_HELPER="/usr/local/libexec/outwarp-priv"
readonly WSTUNNEL_BIN="/usr/local/bin/wstunnel"
# pipx venv paths (canonical PIPX_HOME layout: $PIPX_HOME/venvs/<app>/).
readonly CLIENT_VENV="${PIPX_HOME}/venvs/outwarp-client"
readonly SERVER_VENV="${PIPX_HOME}/venvs/outwarp-server"
# Legacy install layout - pre-pipx; cleaned up by migrate_legacy_install().
readonly LEGACY_CLIENT_PREFIX="/opt/outwarp-client"
readonly LEGACY_SERVER_PREFIX="/opt/outwarp-server"
# Minimum pipx version required for `pipx install "<path>[extras]"`.
readonly REQUIRED_PIPX="1.4.0"

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

banner() {
    cat <<EOF
${BOLD}${CYAN}
    ____        __  _       __
   / __ \\__  __/ /_| |     / /___ __________
  / / / / / / / __/| | /| / / __ \`/ ___/ __ \\
 / /_/ / /_/ / /_  | |/ |/ / /_/ / /  / /_/ /
 \\____/\\__,_/\\__/  |__/|__/\\__,_/_/  / .___/
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
        die "No supported package manager found (apt/dnf/pacman). Install dependencies manually and rerun with OUTWARP_SKIP_DEPS=1."
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
    # Try newer minor versions first so 3.14/3.15-only systems work even when
    # no `python3` symlink is present; fall back to the generic `python3` at
    # the end for distros where only that name is exposed (the version-floor
    # check still rejects pre-3.11 interpreters).
    for py in python3.15 python3.14 python3.13 python3.12 python3.11 python3; do
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

ensure_python_venv() {
    # pipx itself depends on the venv module. Debian/Ubuntu split out
    # `pythonX.Y-venv` from `pythonX.Y`, so this is a real failure mode on a
    # fresh Ubuntu where the system Python isn't paired with its venv package.
    if "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
        return 0
    fi
    warn "$PYTHON_BIN does not have venv support — installing"
    case "$PKG_MANAGER" in
        apt)
            local ver pkg
            ver=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            pkg="python${ver}-venv"
            info "Installing $pkg"
            $SUDO bash -c "$PKG_UPDATE_CMD" >/dev/null
            if ! $SUDO bash -c "$PKG_INSTALL_CMD $pkg" 2>/dev/null; then
                $SUDO bash -c "$PKG_INSTALL_CMD python3-venv" \
                    || die "Could not install $pkg or python3-venv. Install manually and rerun."
            fi
            ;;
        dnf|pacman)
            die "$PYTHON_BIN is missing the venv module. Reinstall python (e.g. '$PKG_INSTALL_CMD python3') and rerun."
            ;;
    esac
    "$PYTHON_BIN" -m venv --help >/dev/null 2>&1 \
        || die "venv still unavailable after install — check the package manager output above"
    ok "venv support installed"
}

ensure_python() {
    if find_python; then
        ok "Python: ${BOLD}${PYTHON_BIN}${RESET} ($($PYTHON_BIN --version))"
    else
        warn "Python 3.11+ not found - installing"
        install_python
        ok "Python installed: ${BOLD}${PYTHON_BIN}${RESET}"
    fi
    ensure_python_venv
}

# ----------------------------------------------------------------------------
# pipx
# ----------------------------------------------------------------------------
# Compare two semantic-ish version strings (X.Y.Z or X.Y). Returns 0 if
# $1 >= $2, else 1. Reused by the version-floor check for pipx.
_version_ge() {
    [[ "$1" == "$2" ]] && return 0
    # sort -V handles X.Y / X.Y.Z / X.Y.Z.dev mixed inputs reasonably.
    local lower
    lower=$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)
    [[ "$lower" == "$2" ]]
}

_install_pipx_native() {
    # Try the distro package first - it ships a sane PIPX_HOME default and
    # tracks security updates via the package manager.
    case "$PKG_MANAGER" in
        apt)
            $SUDO bash -c "$PKG_UPDATE_CMD" >/dev/null 2>&1 || true
            $SUDO bash -c "$PKG_INSTALL_CMD pipx"
            ;;
        dnf)
            $SUDO bash -c "$PKG_INSTALL_CMD pipx"
            ;;
        pacman)
            $SUDO bash -c "$PKG_INSTALL_CMD python-pipx"
            ;;
    esac
}

_bootstrap_pipx_from_pip() {
    # Fallback for distros that don't ship pipx (Ubuntu 22.04 etc.). Build a
    # dedicated venv at /opt/pipx-bootstrap/ to host pipx itself, then expose
    # its entry-point under /usr/local/bin/. This venv is owned by us and the
    # only thing it ships is the pipx launcher - safe to wipe and recreate
    # on a re-run.
    info "Bootstrapping pipx into /opt/pipx-bootstrap (distro has no pipx package)"
    $SUDO rm -rf /opt/pipx-bootstrap
    $SUDO "$PYTHON_BIN" -m venv /opt/pipx-bootstrap
    $SUDO /opt/pipx-bootstrap/bin/pip install --quiet --disable-pip-version-check \
        "pipx>=${REQUIRED_PIPX}"
    $SUDO ln -sf /opt/pipx-bootstrap/bin/pipx "${PIPX_BIN_DIR}/pipx"
}

ensure_pipx() {
    local current
    if command -v pipx >/dev/null 2>&1; then
        current=$(pipx --version 2>/dev/null | head -1 | tr -d '[:space:]')
        if [[ -n "$current" ]] && _version_ge "$current" "$REQUIRED_PIPX"; then
            ok "pipx: ${BOLD}${current}${RESET}"
            return
        fi
        warn "pipx ${current:-unknown} is older than required ${REQUIRED_PIPX} - upgrading"
    else
        info "pipx not found - installing"
    fi

    # Try the package manager first; fall back to the bootstrap venv on failure
    # (e.g. Ubuntu 22.04 ships no pipx package at all).
    if _install_pipx_native 2>/dev/null && command -v pipx >/dev/null 2>&1; then
        current=$(pipx --version 2>/dev/null | head -1 | tr -d '[:space:]')
        if [[ -n "$current" ]] && _version_ge "$current" "$REQUIRED_PIPX"; then
            ok "pipx installed via $PKG_MANAGER: ${BOLD}${current}${RESET}"
            return
        fi
        warn "distro pipx ${current:-unknown} too old - falling back to a bootstrap venv"
    fi

    _bootstrap_pipx_from_pip
    current=$(pipx --version 2>/dev/null | head -1 | tr -d '[:space:]')
    _version_ge "$current" "$REQUIRED_PIPX" \
        || die "pipx bootstrap produced version $current (need $REQUIRED_PIPX+)"
    ok "pipx bootstrapped: ${BOLD}${current}${RESET}"
}

# Run pipx with the system-wide PIPX_HOME/PIPX_BIN_DIR layout so every install
# lands under /opt/pipx/ with entry-points in /usr/local/bin/.
pipx_run() {
    $SUDO env "PIPX_HOME=${PIPX_HOME}" "PIPX_BIN_DIR=${PIPX_BIN_DIR}" \
        "PIPX_DEFAULT_PYTHON=${PYTHON_BIN}" pipx "$@"
}

# Is the given pipx app already installed in the system-wide layout?
pipx_has_app() {
    pipx_run list --short 2>/dev/null | awk '{print $1}' | grep -qx "$1"
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
# Pinned wstunnel version. OutWarp pins this on purpose: the WebSocket-upgrade
# handshake format changed between wstunnel releases, so a client running a
# different version than the server fails the upgrade with HTTP 400 and the
# tunnel carries no traffic. The single source of truth is
# installer/wstunnel-version.txt — keep this constant in sync with it (and with
# server/Dockerfile + scripts/fetch_bundled_binaries.py). This script is run
# standalone via `curl | sudo bash`, so it can't read that file at runtime;
# server/tests/test_wstunnel_version_pin.py enforces that they all agree.
# Override for a one-off with WSTUNNEL_VERSION=vX.Y.Z (only useful if you also
# move the server to the same version).
readonly WSTUNNEL_VERSION_DEFAULT="10.5.2"

# Extract the semver from `<binary> --version` (e.g. "wstunnel-cli 10.5.5").
wstunnel_binary_version() {
    "$1" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

# Download + install a specific wstunnel version to $WSTUNNEL_BIN.
install_wstunnel() {
    local version="${1#v}" arch tarball url tmpdir
    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch="amd64"   ;;
        aarch64) arch="arm64"   ;;
        armv7l)  arch="armv7"   ;;
        *) die "Unsupported architecture: $arch" ;;
    esac

    tarball="wstunnel_${version}_linux_${arch}.tar.gz"
    url="https://github.com/erebe/wstunnel/releases/download/v${version}/${tarball}"

    tmpdir=$(mktemp -d)
    info "Downloading $url (~5 MB)"
    curl "${CURL_OPTS[@]}" -o "$tmpdir/$tarball" "$url" || die "Download failed: $url"
    tar -xzf "$tmpdir/$tarball" -C "$tmpdir"
    $SUDO install -m 0755 "$tmpdir/wstunnel" "$WSTUNNEL_BIN"
    rm -rf "$tmpdir"
    ok "wstunnel ${version} installed: ${BOLD}${WSTUNNEL_BIN}${RESET}"
}

ensure_wstunnel() {
    local want="${WSTUNNEL_VERSION:-$WSTUNNEL_VERSION_DEFAULT}"
    want="${want#v}"

    local on_path have
    on_path=$(command -v wstunnel 2>/dev/null || true)

    # Case 1: OutWarp already manages $WSTUNNEL_BIN — make sure it's the pinned
    # version. A leftover newer/older binary here is exactly what silently
    # breaks tunnels, so heal it instead of accepting it.
    if [[ -x "$WSTUNNEL_BIN" ]]; then
        have=$(wstunnel_binary_version "$WSTUNNEL_BIN")
        if [[ "$have" == "$want" ]]; then
            ok "wstunnel: ${BOLD}${have}${RESET} (pinned ${want})"
            return
        fi
        warn "wstunnel at $WSTUNNEL_BIN is ${have:-unknown}, but OutWarp pins ${want}."
        warn "A client/server version mismatch breaks the WS upgrade (HTTP 400 -> no traffic)."
        info "Replacing it with ${want}..."
        install_wstunnel "$want"
        return
    fi

    # Case 2: a wstunnel exists elsewhere on PATH (distro package, manual
    # install). Install the pinned one to $WSTUNNEL_BIN — OutWarp resolves
    # wstunnel via PATH and /usr/local/bin normally precedes /usr/bin, so this
    # makes the tunnel use the right version. Warn in case PATH order differs.
    if [[ -n "$on_path" ]]; then
        have=$(wstunnel_binary_version "$on_path")
        warn "Found wstunnel ${have:-unknown} at $on_path (not OutWarp-managed)."
        warn "OutWarp pins ${want}; installing it to ${WSTUNNEL_BIN} so the tunnel uses the right one."
        install_wstunnel "$want"
        if [[ "$on_path" != "$WSTUNNEL_BIN" && "$have" != "$want" ]]; then
            warn "Note: $on_path may still shadow ${WSTUNNEL_BIN} depending on \$PATH order."
            warn "If tunnels fail with HTTP 400, remove $on_path or align both ends to one version."
        fi
        return
    fi

    # Case 3: nothing installed.
    info "Installing wstunnel ${want}..."
    install_wstunnel "$want"
}

# ----------------------------------------------------------------------------
# Install source resolution (wheel from GitHub Releases, or local checkout)
# ----------------------------------------------------------------------------
INSTALL_SOURCE=""           # path passed to pipx install: wheel file OR src dir
SOURCE_IS_LOCAL=0           # 1 = local source tree, 0 = downloaded wheel
_WHEEL_TMP=""               # the temp .whl file, cleaned up on EXIT
_WHEEL_DIR_TMP=""           # tmp dir hosting wheel + SHA256SUMS during verify
OUTWARP_VERSION_RESOLVED="" # cached `latest` tag (without leading v)

_cleanup_tmp_wheel() {
    [[ -n "$_WHEEL_TMP" && -f "$_WHEEL_TMP" ]] && rm -f "$_WHEEL_TMP" || true
    [[ -n "$_WHEEL_DIR_TMP" && -d "$_WHEEL_DIR_TMP" ]] && rm -rf "$_WHEEL_DIR_TMP" || true
}
trap _cleanup_tmp_wheel EXIT

_resolve_outwarp_version() {
    if [[ -n "${OUTWARP_VERSION:-}" ]]; then
        OUTWARP_VERSION_RESOLVED="${OUTWARP_VERSION#v}"
        info "Using pinned OutWarp version: ${BOLD}v${OUTWARP_VERSION_RESOLVED}${RESET}"
        return
    fi
    info "Querying GitHub for latest OutWarp release..."
    local response tag
    if ! response=$(curl "${CURL_OPTS[@]}" \
        "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>&1); then
        die "Failed to query GitHub API: $response"
    fi
    tag=$(printf '%s\n' "$response" | grep -m1 '"tag_name":' \
        | sed -E 's/.*"([^"]+)".*/\1/')
    [[ -n "$tag" ]] || die "Could not parse latest OutWarp release tag (API rate limited? set OUTWARP_VERSION=vX.Y.Z to bypass)"
    OUTWARP_VERSION_RESOLVED="${tag#v}"
    ok "Latest OutWarp release: ${BOLD}v${OUTWARP_VERSION_RESOLVED}${RESET}"
}

# Validate a wheel filename against PEP 427. The release pipeline guarantees
# the format; we belt-and-suspenders check it here so a tampered redirect that
# served a `.tar.gz` or a wrong-version wheel fails loudly instead of pip
# silently building from sdist.
_assert_pep427_wheel_name() {
    local name="$1" expected_pkg="$2" expected_version="$3"
    local re="^${expected_pkg}-${expected_version//./\\.}-py3-none-any\\.whl\$"
    [[ "$name" =~ $re ]] \
        || die "Wheel filename does not match PEP 427: got '$name', expected '${expected_pkg}-${expected_version}-py3-none-any.whl'"
}

# Verify the downloaded wheel's SHA256 against the release's SHA256SUMS.txt.
# Semantics intentionally mirror the in-app updater (updater.py::verify_*):
#   - empty URL  → legacy release without manifest; skip with a warning
#   - fetch fail → refuse (could be a MITM dropping the manifest selectively)
#   - missing entry → refuse (wrong release or tampered manifest)
#   - mismatch   → refuse
_verify_wheel_sha256() {
    local wheel_path="$1" wheel_name="$2" checksums_url="$3"

    if [[ "${OUTWARP_SKIP_CHECKSUM:-0}" == "1" ]]; then
        warn "OUTWARP_SKIP_CHECKSUM=1 - skipping integrity check (NOT recommended)"
        return 0
    fi

    if [[ -z "$checksums_url" ]]; then
        warn "Release has no SHA256SUMS.txt asset (legacy release) - integrity check skipped"
        return 0
    fi

    info "Verifying SHA256 against ${BOLD}$(basename "$checksums_url")${RESET}"
    local sums_tmp
    sums_tmp=$(mktemp)
    if ! curl "${CURL_OPTS[@]}" -o "$sums_tmp" "$checksums_url" 2>/dev/null; then
        rm -f "$sums_tmp"
        die "Could not fetch SHA256SUMS from $checksums_url

A network failure here is not the same as 'no manifest available' — refusing
to install. Re-run when the connection is stable, or set
OUTWARP_SKIP_CHECKSUM=1 only if you understand the risk."
    fi

    local expected actual
    expected=$(grep -E "  ${wheel_name}\$" "$sums_tmp" | awk '{print $1}' | head -1)
    rm -f "$sums_tmp"
    if [[ -z "$expected" ]]; then
        die "Wheel '$wheel_name' is not listed in SHA256SUMS.txt - aborting (wrong release or tampered manifest)"
    fi
    actual=$(sha256sum "$wheel_path" | awk '{print $1}')
    if [[ "$expected" != "$actual" ]]; then
        die "SHA256 mismatch for $wheel_name:
  expected: $expected
  actual:   $actual"
    fi
    ok "Integrity verified (SHA256: ${actual:0:16}...)"
}

# Sets INSTALL_SOURCE + SOURCE_IS_LOCAL for the given package ("server" or
# "client"). Downloads the wheel into a temp file if needed, validating its
# filename + integrity before returning.
prepare_install_source() {
    local package="$1"

    if [[ -n "${OUTWARP_REPO_DIR:-}" ]]; then
        INSTALL_SOURCE="$OUTWARP_REPO_DIR/$package"
        SOURCE_IS_LOCAL=1
        [[ -d "$INSTALL_SOURCE" ]] \
            || die "OUTWARP_REPO_DIR set but $INSTALL_SOURCE does not exist"
        ok "Using local source: ${BOLD}${INSTALL_SOURCE}${RESET}"
        return
    fi

    local script_dir candidate
    script_dir="$( cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd )"
    candidate="$( cd "$script_dir/../.." && pwd 2>/dev/null || true )"
    if [[ -n "$candidate" && -f "$candidate/CLAUDE.md" && -d "$candidate/$package" ]]; then
        INSTALL_SOURCE="$candidate/$package"
        SOURCE_IS_LOCAL=1
        ok "Using local checkout: ${BOLD}${INSTALL_SOURCE}${RESET}"
        return
    fi

    SOURCE_IS_LOCAL=0
    [[ -n "$OUTWARP_VERSION_RESOLVED" ]] || _resolve_outwarp_version

    local pkg_norm
    case "$package" in
        client) pkg_norm="outwarp_client" ;;
        server) pkg_norm="outwarp_server" ;;
        *) die "Unknown package: $package" ;;
    esac

    local wheel="${pkg_norm}-${OUTWARP_VERSION_RESOLVED}-py3-none-any.whl"
    local release_base="https://github.com/${GITHUB_REPO}/releases/download/v${OUTWARP_VERSION_RESOLVED}"
    local wheel_url="${release_base}/${wheel}"
    # SHA256SUMS.txt has been a release asset since 0.4.x. Older releases that
    # predate it return 404; _verify_wheel_sha256 handles that gracefully (a
    # 404 reaches us as a fetch failure, which is now strict — but those old
    # releases also lack OUTWARP_SKIP_CHECKSUM-worthy wheels, so install older
    # versions with OUTWARP_VERSION=v0.3.x at your own risk).
    local sums_url="${release_base}/SHA256SUMS.txt"

    _WHEEL_DIR_TMP=$(mktemp -d --suffix=".outwarp-wheel")
    _WHEEL_TMP="${_WHEEL_DIR_TMP}/${wheel}"

    info "Downloading ${BOLD}${wheel}${RESET} (~few MB)..."
    if ! curl "${CURL_OPTS[@]}" -o "$_WHEEL_TMP" "$wheel_url"; then
        die "Failed to download wheel: $wheel_url

Possible causes:
  • The release v${OUTWARP_VERSION_RESOLVED} doesn't include Linux wheels yet
    (re-run with OUTWARP_VERSION=<older-tag> if you need a specific version)
  • Network problem (check https://github.com/${GITHUB_REPO}/releases)"
    fi
    ok "Downloaded $(du -h "$_WHEEL_TMP" | awk '{print $1}') wheel"

    _assert_pep427_wheel_name "$wheel" "$pkg_norm" "$OUTWARP_VERSION_RESOLVED"

    # If the SHA256SUMS asset HEADs as missing (legacy release), pass empty URL
    # so the helper skips politely instead of refusing on a fetch failure.
    local effective_sums_url="$sums_url"
    if ! curl "${CURL_OPTS[@]}" --head -o /dev/null "$sums_url" 2>/dev/null; then
        effective_sums_url=""
    fi
    _verify_wheel_sha256 "$_WHEEL_TMP" "$wheel" "$effective_sums_url"

    INSTALL_SOURCE="$_WHEEL_TMP"
}

# ----------------------------------------------------------------------------
# Legacy install migration
# ----------------------------------------------------------------------------
# Pre-pipx installs lived in /opt/outwarp-{client,server}/.venv/ with symlinks
# pointing at .venv/bin/<entry-point>. Detect that layout and dismantle it
# cleanly before pipx takes over /usr/local/bin/. This runs *before* pipx
# install for the same component so a half-finished pipx install on top of a
# stale venv can't surprise the user with `outwarp` resolving to two different
# pythons depending on $PATH order.
migrate_legacy_install() {
    local component="$1" legacy_prefix old_bin
    case "$component" in
        client)
            legacy_prefix="$LEGACY_CLIENT_PREFIX"
            ;;
        server)
            legacy_prefix="$LEGACY_SERVER_PREFIX"
            ;;
        *) die "Unknown component for legacy migration: $component" ;;
    esac

    # Stop any leftover legacy process FIRST, before bailing out on the
    # "no legacy dir" fast path. Reason: a previous failed run may have
    # already removed $legacy_prefix/.venv from disk, but the running
    # process keeps the binaries mapped via kernel-held inodes — and
    # keeps spamming the log file with stale tracebacks against the
    # deleted venv. Without this pre-bail kill we'd never reap that
    # zombie on a subsequent successful install.
    if [[ "$component" == "client" ]]; then
        $SUDO pkill -f "${legacy_prefix}/.venv/bin/" 2>/dev/null || true
    fi

    [[ -d "$legacy_prefix/.venv" ]] || return 0

    warn "Found legacy install at ${BOLD}${legacy_prefix}${RESET} - migrating to pipx layout"

    # Remove only symlinks that point into the legacy venv. A symlink pointing
    # elsewhere (e.g. user-created shim) gets left alone.
    for old_bin in "$CLIENT_BIN_LINK" "$CLIENT_CLI_BIN_LINK" "$SERVER_BIN_LINK" "$SERVER_GUI_BIN_LINK"; do
        if [[ -L "$old_bin" ]]; then
            local target
            target=$(readlink "$old_bin")
            if [[ "$target" == "$legacy_prefix/.venv/bin/"* ]]; then
                $SUDO rm -f "$old_bin"
                ok "Removed legacy symlink: $old_bin"
            fi
        fi
    done

    $SUDO rm -rf "$legacy_prefix/.venv"
    # Empty parent dir is harmless to leave; only remove if it has no contents
    # other than the venv we just deleted (config dirs etc. stay).
    $SUDO rmdir "$legacy_prefix" 2>/dev/null || true
    ok "Legacy venv removed"
}

# ----------------------------------------------------------------------------
# pipx install helper
# ----------------------------------------------------------------------------
# Install or upgrade an OutWarp app under the system-wide pipx layout. Handles
# both fresh installs and in-place upgrades (so re-running the script after
# `OUTWARP_VERSION=vX.Y.Z` was bumped just works).
pipx_install_app() {
    local app="$1" source="$2" extras="$3"

    local spec
    if [[ -n "$extras" ]]; then
        spec="${source}[${extras}]"
    else
        spec="$source"
    fi

    if pipx_has_app "$app"; then
        info "Reinstalling existing ${BOLD}${app}${RESET} (pipx)"
    else
        info "Installing ${BOLD}${app}${RESET} via pipx"
    fi

    # `pipx install --force` is idempotent: it covers both fresh installs and
    # in-place upgrades while overwriting any stale /usr/local/bin/ symlink
    # from a previous (legacy or pipx) install. We deliberately avoid the
    # separate uninstall+install pair because between the two there's no
    # working entry-point on disk — a failure there leaves the user with
    # nothing installed.
    #
    # `--pip-args` MUST use `=` to glue the value: pipx >= 1.12 parses its
    # argv with argparse, which refuses values that start with `-` when the
    # option and value are separate tokens (the dash makes argparse think
    # it's another option, not the value). See:
    #   https://github.com/pypa/pipx/issues/<rejects-flag-like-pip-args>
    local pipx_args=(
        "install"
        "--force"
        "--pip-args=--disable-pip-version-check"
        "$spec"
    )
    pipx_run "${pipx_args[@]}"
}

# ----------------------------------------------------------------------------
# Server install
# ----------------------------------------------------------------------------
want_server_gui() {
    # Default OFF as of 0.5.x: the Textual TUI (`outwarp-server tui`) is the
    # primary admin UI on Linux and the pywebview GUI is opt-in. Set
    # OUTWARP_SERVER_GUI=1 to ALSO install the GTK/WebKit stack and pull
    # pywebview via the `gui-linux` extra. This keeps a vanilla server
    # install minimal (no webkit2gtk, no libayatana-appindicator).
    case "${OUTWARP_SERVER_GUI:-0}" in
        1|true|yes) return 0 ;;
        *) return 1 ;;
    esac
}

want_client_gui() {
    # Mirror of want_server_gui — see the comment there. The TUI is the
    # default on Linux; this gate (OUTWARP_CLIENT_GUI=1) opts into the
    # webkitgtk + appindicator stack and pulls pywebview via gui-linux.
    case "${OUTWARP_CLIENT_GUI:-0}" in
        1|true|yes) return 0 ;;
        *) return 1 ;;
    esac
}

ensure_server_gui_system_deps() {
    info "Installing server GUI system dependencies (webkitgtk + tray indicator)"
    case "$PKG_MANAGER" in
        apt)
            local webkit_pkg="gir1.2-webkit2-4.1"
            if ! $SUDO bash -c "$PKG_INSTALL_CMD $webkit_pkg" >/dev/null 2>&1; then
                webkit_pkg="gir1.2-webkit2-4.0"
                warn "gir1.2-webkit2-4.1 not available - falling back to 4.0"
            fi
            $SUDO bash -c "$PKG_INSTALL_CMD python3-gi gir1.2-gtk-3.0 $webkit_pkg gir1.2-ayatanaappindicator3-0.1" \
                || warn "Some GUI deps failed - the GUI may not start (CLI is unaffected)"
            ;;
        dnf)
            $SUDO bash -c "$PKG_INSTALL_CMD python3-gobject gtk3 webkit2gtk4.1 libappindicator-gtk3" \
                || $SUDO bash -c "$PKG_INSTALL_CMD python3-gobject gtk3 webkit2gtk4.0 libappindicator-gtk3" \
                || warn "Some GUI deps failed - the GUI may not start (CLI is unaffected)"
            ;;
        pacman)
            $SUDO bash -c "$PKG_INSTALL_CMD python-gobject webkit2gtk-4.1 libayatana-appindicator" \
                || $SUDO bash -c "$PKG_INSTALL_CMD python-gobject webkit2gtk libappindicator-gtk3" \
                || warn "Some GUI deps failed - the GUI may not start (CLI is unaffected)"
            ;;
    esac
}

install_server() {
    info "Installing OutWarp server (pipx layout: ${BOLD}${SERVER_VENV}${RESET})"

    prepare_install_source "server"

    local install_gui="false" extras="tui"
    if want_server_gui; then
        # gui-linux (not gui) so we pull the unmarkered pywebview — the
        # default `gui` extra excludes pywebview on Linux via PEP 508
        # markers since 0.5.x.
        install_gui="true"
        extras="gui-linux,tui"
        ensure_server_gui_system_deps
    else
        info "Installing TUI-only build (Textual). The admin UI is ${BOLD}outwarp-server tui${RESET}."
        info "Set ${BOLD}OUTWARP_SERVER_GUI=1${RESET} to also install the pywebview GUI (webkit2gtk + appindicator)."
    fi

    pipx_install_app "outwarp-server" "$INSTALL_SOURCE" "$extras"

    # Only purge the legacy /opt/outwarp-server install AFTER pipx confirmed
    # the new venv works. Doing it before means a pipx failure leaves the
    # host with nothing installed at all (and the still-running service holds
    # only file inodes from a wiped venv).
    migrate_legacy_install "server"

    # Sanity check: the entry-point pipx exposes must resolve to the venv we
    # just created. If something else (an old symlink, a /usr/local stray) is
    # in the way, `outwarp-server --version` would silently invoke the wrong
    # binary.
    [[ -x "$SERVER_BIN_LINK" ]] \
        || die "pipx finished but $SERVER_BIN_LINK is missing - investigate /usr/local/bin/"
    ok "outwarp-server installed: ${BOLD}$(${SUDO} ${SERVER_BIN_LINK} --version 2>/dev/null | awk '{print $2}')${RESET}"

    # 0.5.0 collapse: no more outwarp-server-gui binary - the admin GUI lives
    # behind `outwarp-server gui`. We only check the GUI extras pulled the
    # pywebview deps in.
    if [[ "$install_gui" == "true" ]]; then
        $SUDO "$SERVER_VENV/bin/python" -c "import webview" 2>/dev/null \
            || warn "GUI extras requested but pywebview is missing in $SERVER_VENV"
    fi
}

run_setup_wizard() {
    if [[ "${OUTWARP_RUN_WIZARD:-1}" == "0" ]]; then
        info "Skipping setup wizard (OUTWARP_RUN_WIZARD=0)"
        echo
        echo "${BOLD}Run the wizard later with:${RESET}"
        echo "  sudo outwarp-server setup"
        return
    fi
    echo
    info "Launching the setup wizard..."
    echo
    $SUDO outwarp-server setup </dev/tty
}

# ----------------------------------------------------------------------------
# Client install
# ----------------------------------------------------------------------------
ensure_client_system_deps() {
    info "Installing client GUI dependencies (webkitgtk + tray indicator)"
    case "$PKG_MANAGER" in
        apt)
            local webkit_pkg="gir1.2-webkit2-4.1"
            if ! $SUDO bash -c "$PKG_INSTALL_CMD $webkit_pkg" >/dev/null 2>&1; then
                webkit_pkg="gir1.2-webkit2-4.0"
                warn "gir1.2-webkit2-4.1 not available - falling back to 4.0"
            fi
            $SUDO bash -c "$PKG_INSTALL_CMD python3-gi gir1.2-gtk-3.0 $webkit_pkg gir1.2-ayatanaappindicator3-0.1" \
                || warn "Some GUI deps failed - the window may not open (tray will still work with reduced features)"
            ;;
        dnf)
            $SUDO bash -c "$PKG_INSTALL_CMD python3-gobject gtk3 webkit2gtk4.1 libappindicator-gtk3" \
                || $SUDO bash -c "$PKG_INSTALL_CMD python3-gobject gtk3 webkit2gtk4.0 libappindicator-gtk3" \
                || warn "Some GUI deps failed"
            ;;
        pacman)
            $SUDO bash -c "$PKG_INSTALL_CMD python-gobject webkit2gtk-4.1 libayatana-appindicator" \
                || $SUDO bash -c "$PKG_INSTALL_CMD python-gobject webkit2gtk libappindicator-gtk3" \
                || warn "Some GUI deps failed"
            ;;
    esac
}

TARGET_USER=""
TARGET_HOME=""
resolve_target_user() {
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        TARGET_USER="$SUDO_USER"
    elif [[ -n "${OUTWARP_USER:-}" ]]; then
        TARGET_USER="$OUTWARP_USER"
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
# OutWarp privileged helper. Installed by the OutWarp installer.
# Invoked via `sudo -n` by the user-mode tray app. Single sudoers entry
# whitelists this script for the desktop user.
set -euo pipefail

WG_CONF_DIR="/etc/wireguard-outwarp"
NAME_RE='^[A-Za-z0-9_=+.-]{1,15}$'
IPV4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}$'

die() { echo "outwarp-priv: $*" >&2; exit 2; }
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
        # Pass the full path (not the bare name) so wg-quick never touches
        # /etc/wireguard — other tools (e.g. omarchy-vpn) scan that directory
        # and adopt any .conf they find as one of their own managed tunnels,
        # which fights OutWarp for control of its own interface.
        wg-quick down "$conf" >/dev/null 2>&1 || true
        exec wg-quick up "$conf"
        ;;
    down)
        name="${1:-}"; need_name "$name"
        conf="$WG_CONF_DIR/$name.conf"
        wg-quick down "$conf" >/dev/null 2>&1 || true
        rm -f "$conf"
        ;;
    is-active)
        name="${1:-}"; need_name "$name"
        wg show "$name" >/dev/null 2>&1
        ;;
    dump)
        # Bridges `wg show <iface> dump` across the privilege boundary so the
        # TUI's StatsSampler can read rx/tx/handshake counters without sudo.
        # `wg show` requires CAP_NET_ADMIN; without root it prints
        # "Operation not permitted" on stderr and still exits 0, so the
        # StatsSampler can't detect the failure from a direct call.
        name="${1:-}"; need_name "$name"
        wg show "$name" dump
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
    killswitch-on)
        command -v nft >/dev/null 2>&1 || die "nftables (nft) not installed — cannot engage kill switch"
        [[ $# -ge 1 ]] || die "killswitch-on needs at least one allowlist IP"
        for ip in "$@"; do need_ipv4 "$ip"; done
        nft delete table inet outwarp_ks >/dev/null 2>&1 || true
        nft add table inet outwarp_ks
        nft add chain inet outwarp_ks out '{ type filter hook output priority 0 ; policy drop ; }'
        nft add rule inet outwarp_ks out oif lo accept
        nft add rule inet outwarp_ks out ct state established,related accept
        for ip in "$@"; do
            nft add rule inet outwarp_ks out ip daddr "$ip" accept
        done
        ;;
    killswitch-off)
        command -v nft >/dev/null 2>&1 || exit 0
        nft delete table inet outwarp_ks >/dev/null 2>&1 || true
        ;;
    killswitch-status)
        command -v nft >/dev/null 2>&1 || exit 1
        nft list table inet outwarp_ks >/dev/null 2>&1
        ;;
    *) die "unknown command: ${cmd:-<empty>}" ;;
esac
HELPER_EOF
    $SUDO install -m 0755 -o root -g root "$tmp" "$CLIENT_HELPER"
    rm -f "$tmp"
    ok "Helper installed: ${BOLD}${CLIENT_HELPER}${RESET}"
}

write_client_sudoers() {
    info "Writing sudoers rule to ${CLIENT_SUDOERS}"
    local tmp
    tmp=$(mktemp)
    cat >"$tmp" <<EOF
# Managed by OutWarp installer - do not edit by hand.
# Lets the tray app (running as $TARGET_USER) invoke the OutWarp
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

remove_client_autostart() {
    # Deletes the XDG autostart entry that pre-0.5.x installs (or a
    # OUTWARP_CLIENT_GUI=1 install on this machine before the user flipped
    # to the TUI) might have left behind. Safe to call unconditionally:
    # a missing file is a no-op, and the entry is owned by us so removing
    # it does not surprise the user.
    local desktop_file="$TARGET_HOME/.config/autostart/outwarp.desktop"
    if [[ -f "$desktop_file" ]]; then
        rm -f "$desktop_file"
        ok "Removed legacy autostart entry: $desktop_file"
    fi
}

install_client_desktop() {
    # Install the application launcher .desktop entry and icon so OutWarp
    # appears in GNOME Shell, KDE Plasma, Rofi, etc.
    info "Installing application launcher entry"

    # Locate the bundled icon from the installed venv.
    local icon_src
    icon_src=$(find "${CLIENT_VENV}/lib" -name "app_icon.png" \
        -path "*/outwarp/resources/app_icon.png" 2>/dev/null | head -1 || true)

    if [[ -n "$icon_src" && -f "$icon_src" ]]; then
        $SUDO install -Dm 0644 "$icon_src" /usr/share/pixmaps/outwarp.png
        $SUDO install -Dm 0644 "$icon_src" /usr/share/icons/hicolor/128x128/apps/outwarp.png
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            $SUDO gtk-update-icon-cache -qf /usr/share/icons/hicolor 2>/dev/null || true
        fi
        ok "Icon installed: /usr/share/pixmaps/outwarp.png"
    else
        warn "Could not locate app_icon.png — launcher will use a generic icon"
    fi

    # Write the .desktop file. Terminal=true tells the DE to open a terminal
    # emulator — the right behaviour for a TUI application.
    $SUDO install -Dm 0644 /dev/stdin /usr/share/applications/outwarp.desktop <<'DESKTOP_EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=OutWarp
Comment=WireGuard-over-WebSocket VPN tunnel client
Exec=outwarp-cli tui
Icon=outwarp
Terminal=true
Categories=Network;VPN;System;
Keywords=wireguard;vpn;wstunnel;tunnel;
StartupNotify=false
DESKTOP_EOF

    if command -v update-desktop-database >/dev/null 2>&1; then
        $SUDO update-desktop-database /usr/share/applications 2>/dev/null || true
    fi
    ok "Launcher installed: /usr/share/applications/outwarp.desktop"
}

remove_client_desktop() {
    # Remove the system-wide launcher installed by install_client_desktop().
    # Called from install_client() on re-install to stay idempotent;
    # users who want a full purge should also run this path.
    for f in \
        /usr/share/applications/outwarp.desktop \
        /usr/share/pixmaps/outwarp.png \
        /usr/share/icons/hicolor/128x128/apps/outwarp.png; do
        if [[ -f "$f" ]]; then
            $SUDO rm -f "$f"
            ok "Removed: $f"
        fi
    done
    if command -v update-desktop-database >/dev/null 2>&1; then
        $SUDO update-desktop-database /usr/share/applications 2>/dev/null || true
    fi
}

install_client_completions() {
    # Register shell completions via argcomplete (installed with the [tui] extra).
    # Only attempts bash and zsh; silently skips if argcomplete is missing or
    # the completion directories don't exist.
    local reg
    reg="${CLIENT_VENV}/bin/register-python-argcomplete"
    [[ -x "$reg" ]] || return 0

    if [[ -d /etc/bash_completion.d ]]; then
        "$reg" outwarp-cli > /etc/bash_completion.d/outwarp-cli 2>/dev/null \
            && ok "bash completions: /etc/bash_completion.d/outwarp-cli" \
            || warn "Could not write bash completions (non-fatal)"
    fi

    local zsh_dir="/usr/share/zsh/site-functions"
    if [[ -d "$zsh_dir" ]]; then
        "$reg" --shell zsh outwarp-cli > "${zsh_dir}/_outwarp-cli" 2>/dev/null \
            && ok "zsh completions: ${zsh_dir}/_outwarp-cli" \
            || true
    fi
}

install_client() {
    info "Installing OutWarp client (pipx layout: ${BOLD}${CLIENT_VENV}${RESET})"

    prepare_install_source "client"

    resolve_target_user

    local extras="tui"
    if want_client_gui; then
        # gui-linux extra pulls pywebview (which the marker excludes from
        # base deps on Linux) plus pystray/Pillow even though they're already
        # in base deps — being explicit here keeps the spec readable when
        # someone audits what the [gui-linux] flag actually grants.
        extras="tui,gui-linux"
        ensure_client_system_deps
    else
        info "Installing TUI-only build (Textual). The default UI is ${BOLD}outwarp-cli tui${RESET}."
        info "Set ${BOLD}OUTWARP_CLIENT_GUI=1${RESET} to also install the pywebview tray (webkit2gtk + appindicator)."
    fi

    pipx_install_app "outwarp-client" "$INSTALL_SOURCE" "$extras"

    # Only purge the legacy /opt/outwarp-client install AFTER pipx confirmed
    # the new venv works. Doing it before means a pipx failure leaves the
    # user with nothing installed and the running tray pinned to a wiped
    # venv via kernel-held inodes (the failure mode that bit 0.4.x → 0.5.0
    # upgraders during the --pip-args incident).
    migrate_legacy_install "client"

    # 0.5.0 collapse: the only entry-point pipx ships is outwarp-cli; the GUI
    # lives behind `outwarp-cli gui`. Old `outwarp` / `outwarp-uninstall` bins
    # are removed automatically by `pipx reinstall` on upgrade.
    [[ -x "$CLIENT_CLI_BIN_LINK" ]] \
        || die "pipx finished but $CLIENT_CLI_BIN_LINK is missing - investigate /usr/local/bin/"
    ok "outwarp-cli installed: ${BOLD}$(${SUDO} ${CLIENT_CLI_BIN_LINK} --version 2>/dev/null | awk '{print $2}')${RESET}"

    write_client_helper
    write_client_sudoers
    # Autostart is a GUI-only concept: an XDG autostart entry needs a window
    # manager to launch the tray, but the TUI lives in a terminal the user
    # opens by hand. Sweep any leftover autostart file from previous installs
    # so re-running the installer always converges on the TUI default.
    remove_client_autostart
    # Install system-wide application launcher entry (GNOME, KDE, etc.).
    install_client_desktop
    # Register shell completions (bash, zsh) for outwarp-cli.
    install_client_completions

    cat <<EOF

${BOLD}${GREEN}Client installed.${RESET}

  ${BOLD}Quick start:${RESET}
    1. Drop your ${BOLD}.owcfg${RESET} file somewhere accessible by ${TARGET_USER}.
    2. Launch ${BOLD}outwarp-cli tui${RESET} — interactive terminal UI.
       The first run prompts for the .owcfg via the import modal.
    3. Or find ${BOLD}OutWarp${RESET} in your application launcher (GNOME, KDE, etc.).

  ${BOLD}Headless / SSH / systemd:${RESET}
    1. ${BOLD}outwarp-cli import /path/to/profile.owcfg${RESET}
    2. ${BOLD}outwarp-cli connect${RESET}            # foreground, Ctrl+C to stop
       ${BOLD}outwarp-cli service install${RESET}    # background daemon (systemd --user)
       ${BOLD}outwarp-cli status${RESET}             # one-shot state probe
       ${BOLD}outwarp-cli logs --follow${RESET}      # tail -f the log file

  ${DIM}Uninstall:        outwarp-cli uninstall${RESET}
  ${DIM}Privileged helper: $CLIENT_HELPER${RESET}
  ${DIM}Sudoers rule: $CLIENT_SUDOERS  (revoke with: sudo rm $CLIENT_SUDOERS)${RESET}
  ${DIM}pipx venv: $CLIENT_VENV${RESET}
  ${DIM}Want the tray GUI too? Re-run with OUTWARP_CLIENT_GUI=1 (installs webkitgtk + pywebview).${RESET}

EOF
}

# ----------------------------------------------------------------------------
# Component picker
# ----------------------------------------------------------------------------
pick_component() {
    local component="${OUTWARP_COMPONENT:-}"
    if [[ -z "$component" ]]; then
        echo
        echo "${BOLD}Which component do you want to install?${RESET}"
        echo "  ${BOLD}1)${RESET} Server  - runs wstunnel + WireGuard, accepts client connections"
        echo "  ${BOLD}2)${RESET} Client  - connects to an OutWarp server"
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

    # Both server and client need: Python 3.11+, pipx, wireguard-tools, wstunnel.
    # GUI-only deps (webkitgtk for pywebview, appindicator for pystray) are
    # installed inside install_client / install_server to keep headless server
    # installs minimal.
    info "Preparing system dependencies"
    ensure_python
    ensure_pipx
    ensure_wireguard
    ensure_wstunnel

    pick_component

    echo
    ok "${BOLD}${GREEN}All done.${RESET}"
}

main "$@"
