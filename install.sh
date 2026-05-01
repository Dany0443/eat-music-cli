#!/usr/bin/env bash
# eatmusic installer — removes any old installation first
set -euo pipefail

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
WORK_DIR="$(mktemp -d -t eatmusic-src-XXXX)"
LOG_FILE="$(mktemp -t eatmusic-install-XXXX.log)"
LOG_PATH_HINT="${LOG_FILE}.path"
SRC_DIR_HINT="${LOG_FILE}.srcdir"
trap 'rm -rf "$WORK_DIR"; rm -f "$LOG_FILE" "$LOG_PATH_HINT" "$SRC_DIR_HINT"' EXIT
INSTALL_STATE_DIR="$HOME/.config/eatcli"
INSTALL_MARKER_FILE="$INSTALL_STATE_DIR/installed.txt"
APP_VERSION="$(awk -F'"' '/^version = /{print $2; exit}' "$DIR/pyproject.toml" 2>/dev/null || true)"
[[ -z "$APP_VERSION" ]] && APP_VERSION="unknown"
PREV_INSTALL=0
PREV_VERSION=""
SOURCE_BASE_URL="${EATCLI_SOURCE_BASE_URL:-https://webjuniors.org/eatcli}"
SOURCE_TARBALL_URL="${EATCLI_SOURCE_TARBALL_URL:-$SOURCE_BASE_URL/source.tar.gz}"
INSTALL_SRC_DIR="$DIR"

AUTO_YES=0
SKIP_FFMPEG=0
DO_UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) AUTO_YES=1 ;;
        --skip-ffmpeg) SKIP_FFMPEG=1 ;;
        --uninstall) DO_UNINSTALL=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: install.sh [options]
  -y, --yes         Non-interactive mode
  --skip-ffmpeg     Skip ffmpeg installation
  --uninstall       Fully remove eatmusic and all config/state files
EOF
            exit 0
            ;;
        *) ;;
    esac
done

TOTAL_STEPS=7
STEP_I=0
BAR_W=40
SPIN='|/-\'
BANNER_DELAY=0.0015
BANNER_CHUNK=4
STEP_ANIM_TICKS=20
STEP_TICK_SECONDS=0.18
APT_UPDATED=0
SUDO_CMD=""
OS_ID="unknown"
OS_LIKE=""
OS_PRETTY="Unknown Linux"
OS_FAMILY="unknown"
PKG_MGR="unknown"

log_info() { echo -e "${CYAN}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die() {
    echo ""
    echo -e "${RED}[ERROR]${NC} $*"
    echo -e "${YELLOW}[INFO]${NC} Last installer output:"
    tail -n 40 "$LOG_FILE" || true
    exit 1
}

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_LIKE="${ID_LIKE:-}"
        OS_PRETTY="${PRETTY_NAME:-$OS_ID}"
    fi
    local sig="${OS_ID} ${OS_LIKE}"
    if [[ "$sig" == *debian* ]] || [[ "$sig" == *ubuntu* ]] || [[ "$sig" == *linuxmint* ]] || [[ "$sig" == *pop* ]]; then
        OS_FAMILY="debian"
        PKG_MGR="apt"
    elif [[ "$sig" == *fedora* ]] || [[ "$sig" == *rhel* ]] || [[ "$sig" == *centos* ]]; then
        OS_FAMILY="rhel"
        PKG_MGR="dnf"
    elif [[ "$sig" == *arch* ]] || [[ "$sig" == *manjaro* ]]; then
        OS_FAMILY="arch"
        PKG_MGR="pacman"
    elif [[ "$sig" == *suse* ]]; then
        OS_FAMILY="suse"
        PKG_MGR="zypper"
    else
        OS_FAMILY="unknown"
        PKG_MGR="unknown"
    fi
}

detect_previous_install() {
    if [[ -f "$INSTALL_MARKER_FILE" ]]; then
        PREV_INSTALL=1
        PREV_VERSION="$(awk -F': ' '/^version: /{print $2; exit}' "$INSTALL_MARKER_FILE" 2>/dev/null || true)"
    fi
}

configure_privilege() {
    if [[ "$(id -u)" -eq 0 ]]; then
        SUDO_CMD=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO_CMD="sudo"
    else
        SUDO_CMD=""
    fi
}

confirm_start() {
    if [[ "$AUTO_YES" -eq 1 ]]; then
        return
    fi
    printf "${BOLD}Continue installation? [Y/n]: ${NC}"
    read -r ans < /dev/tty
    ans="${ans:-Y}"
    if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        die "Installation canceled by user."
    fi
}

confirm_pkg_install() {
    local pkg="$1"
    if [[ "$AUTO_YES" -eq 1 ]]; then
        return 0
    fi
    printf "${BOLD}Install dependency '${pkg}'? [Y/n]: ${NC}"
    local ans
    read -r ans < /dev/tty
    ans="${ans:-Y}"
    [[ "$ans" =~ ^[Yy]$ ]]
}

must_have_or_die() {
    local cmd="$1" hint="$2"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        die "Required command missing: $cmd. $hint"
    fi
}

pkg_install() {
    local pkgs=("$@")
    if (( ${#pkgs[@]} == 0 )); then
        return
    fi
    if [[ -z "$SUDO_CMD" && "$(id -u)" -ne 0 && "$PKG_MGR" != "brew" ]]; then
        die "Need root privileges (sudo) to install packages: ${pkgs[*]}"
    fi
    case "$PKG_MGR" in
        apt)
            if [[ "$APT_UPDATED" -eq 0 ]]; then
                ${SUDO_CMD} apt-get update -qq
                APT_UPDATED=1
            fi
            ${SUDO_CMD} apt-get install -y -qq "${pkgs[@]}"
            ;;
        dnf)
            ${SUDO_CMD} dnf install -y "${pkgs[@]}"
            ;;
        pacman)
            ${SUDO_CMD} pacman -Sy --noconfirm "${pkgs[@]}"
            ;;
        zypper)
            ${SUDO_CMD} zypper --non-interactive install "${pkgs[@]}"
            ;;
        *)
            die "Unsupported package manager for auto install: $PKG_MGR"
            ;;
    esac
}

draw_progress() {
    local percent="$1" label="$2" spin_char="$3"
    local filled=$((percent * BAR_W / 100))
    local empty=$((BAR_W - filled))
    local bar
    bar="$(printf '%*s' "$filled" '' | tr ' ' '=')"
    bar="${bar}$(printf '%*s' "$empty" '' | tr ' ' ' ')"
    printf "\r${BOLD}Installing:${NC} [%-${BAR_W}s] %3d%%  %s %s" "$bar" "$percent" "$spin_char" "$label"
}

print_smooth() {
    local line i step
    step=$BANNER_CHUNK
    (( step < 1 )) && step=1
    while IFS= read -r line; do
        for ((i=0; i<${#line}; i+=step)); do
            printf "%s" "${line:i:step}"
            sleep "$BANNER_DELAY"
        done
        printf "\n"
    done
}

run_step() {
    local label="$1" fn="$2"
    STEP_I=$((STEP_I + 1))
    local start_pct=$(((STEP_I - 1) * 100 / TOTAL_STEPS))
    local end_pct=$((STEP_I * 100 / TOTAL_STEPS))
    local span=$((end_pct - start_pct))
    local target_mid=$((start_pct + (span * 9 / 10)))
    (( target_mid >= end_pct )) && target_mid=$((end_pct - 1))
    (( target_mid < start_pct )) && target_mid=$start_pct

    : > "$LOG_FILE"
    ("$fn") >"$LOG_FILE" 2>&1 &
    local pid=$!
    local s=0
    local pct=$start_pct
    local frames target_delta
    frames=$STEP_ANIM_TICKS
    (( frames < 1 )) && frames=1
    target_delta=$((target_mid - start_pct))
    while kill -0 "$pid" 2>/dev/null; do
        local frame="${SPIN:$((s % 4)):1}"
        local k=$s
        (( k > frames )) && k=$frames
        next_pct=$((start_pct + (k * target_delta / frames)))
        if (( next_pct > pct )); then
            pct=$next_pct
        fi
        draw_progress "$pct" "$label" "$frame"
        sleep "$STEP_TICK_SECONDS"
        s=$((s + 1))
    done

    if ! wait "$pid"; then
        draw_progress "$start_pct" "$label" "!"
        printf "\n"
        die "Step failed: $label"
    fi
    draw_progress "$end_pct" "$label" " "
}

step_clean() {
    if [[ "$PREV_INSTALL" -eq 1 ]]; then
        if [[ -n "$PREV_VERSION" ]]; then
            log_info "Previous install detected (version: $PREV_VERSION). Replacing core files and keeping config."
        else
            log_info "Previous install detected. Replacing core files and keeping config."
        fi
    else
        log_info "No previous install marker found. Performing clean install."
    fi

    if command -v pipx >/dev/null 2>&1; then
        pipx uninstall eatmusic >/dev/null 2>&1 || true
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -m pip uninstall eatmusic -y >/dev/null 2>&1 || true
    fi
    if command -v pip3 >/dev/null 2>&1; then
        pip3 uninstall eatmusic -y >/dev/null 2>&1 || true
    fi

    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY'
import pathlib, shutil, site
for sp in site.getsitepackages() + [site.getusersitepackages()]:
    for p in pathlib.Path(sp).glob("eatmusic*"):
        shutil.rmtree(p, ignore_errors=True)
PY
    fi

    # Keep user config files by request; only remove old legacy cache artifacts.
    OLD_CACHE_DB="$HOME/.cache/eatmusic/cache.db"
    if [ -f "$OLD_CACHE_DB" ]; then
        rm -f "$OLD_CACHE_DB"
        rmdir "$HOME/.cache/eatmusic" 2>/dev/null || true
    fi
}

step_prepare_source() {
    # Local dev install (running installer from repo checkout).
    if [[ -f "$DIR/pyproject.toml" ]]; then
        INSTALL_SRC_DIR="$DIR"
    else
        must_have_or_die curl "Install curl and rerun."
        must_have_or_die tar "Install tar and rerun."
        mkdir -p "$WORK_DIR/src"
        curl -fsSL "$SOURCE_TARBALL_URL" -o "$WORK_DIR/source.tar.gz"
        curl -fsSL "$SOURCE_BASE_URL/source.tar.gz.sha256" -o "$WORK_DIR/source.tar.gz.sha256"
        (cd "$WORK_DIR" && sha256sum -c "source.tar.gz.sha256") || die "Tarball checksum failed — download may be corrupted or tampered with."
        tar -xzf "$WORK_DIR/source.tar.gz" -C "$WORK_DIR/src"

        local pp
        pp="$(find "$WORK_DIR/src" -maxdepth 4 -type f -name pyproject.toml | head -n1 || true)"
        [[ -n "$pp" ]] || die "Downloaded source archive has no pyproject.toml"
        INSTALL_SRC_DIR="$(dirname "$pp")"
    fi

    # Refresh version from actual install source if available.
    local parsed
    parsed="$(awk -F'"' '/^version = /{print $2; exit}' "$INSTALL_SRC_DIR/pyproject.toml" 2>/dev/null || true)"
    [[ -n "$parsed" ]] && APP_VERSION="$parsed"
    # Write resolved source dir so parent shell can read it back (subshell isolation).
    echo "$INSTALL_SRC_DIR" > "$SRC_DIR_HINT"
}

step_python() {
    command -v python3 >/dev/null 2>&1 || {
        echo "python3 is not installed"
        exit 1
    }
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJ=${PY_VER%%.*}; PY_MIN=${PY_VER##*.}
    if [[ $PY_MAJ -lt 3 || ($PY_MAJ -eq 3 && $PY_MIN -lt 10) ]]; then
        echo "Python $PY_VER found; need >= 3.10"
        exit 1
    fi
}

step_dependencies() {
    local need=()
    local mandatory=("python3" "pip3")
    local opt_ffmpeg=0

    case "$PKG_MGR" in
        apt)
            command -v python3 >/dev/null 2>&1 || need+=("python3")
            command -v pip3 >/dev/null 2>&1 || need+=("python3-pip")
            command -v pipx >/dev/null 2>&1 || need+=("pipx")
            if [[ "$SKIP_FFMPEG" -eq 0 ]]; then
                command -v ffmpeg >/dev/null 2>&1 || need+=("ffmpeg")
            fi
            ;;
        dnf)
            command -v python3 >/dev/null 2>&1 || need+=("python3")
            command -v pip3 >/dev/null 2>&1 || need+=("python3-pip")
            command -v pipx >/dev/null 2>&1 || need+=("pipx")
            if [[ "$SKIP_FFMPEG" -eq 0 ]]; then
                command -v ffmpeg >/dev/null 2>&1 || need+=("ffmpeg")
            fi
            ;;
        pacman)
            command -v python3 >/dev/null 2>&1 || need+=("python")
            command -v pip3 >/dev/null 2>&1 || need+=("python-pip")
            command -v pipx >/dev/null 2>&1 || need+=("pipx")
            if [[ "$SKIP_FFMPEG" -eq 0 ]]; then
                command -v ffmpeg >/dev/null 2>&1 || need+=("ffmpeg")
            fi
            ;;
        zypper)
            command -v python3 >/dev/null 2>&1 || need+=("python3")
            command -v pip3 >/dev/null 2>&1 || need+=("python3-pip")
            command -v pipx >/dev/null 2>&1 || need+=("pipx")
            if [[ "$SKIP_FFMPEG" -eq 0 ]]; then
                command -v ffmpeg >/dev/null 2>&1 || need+=("ffmpeg")
            fi
            ;;
        *)
            ;;
    esac

    if (( ${#need[@]} > 0 )); then
        local selected=()
        local pkg
        for pkg in "${need[@]}"; do
            if confirm_pkg_install "$pkg"; then
                selected+=("$pkg")
            else
                log_warn "Skipped dependency: $pkg"
                if [[ "$pkg" == "ffmpeg" ]]; then
                    opt_ffmpeg=1
                fi
            fi
        done
        if (( ${#selected[@]} > 0 )); then
            pkg_install "${selected[@]}"
        fi
    fi

    # Mandatory runtime checks after optional prompts.
    for req in "${mandatory[@]}"; do
        case "$req" in
            python3) must_have_or_die python3 "Install it with your distro package manager and rerun." ;;
            pip3)    must_have_or_die pip3 "Install python3-pip and rerun." ;;
        esac
    done

    # pipx is preferred but optional; installer can continue with pip3.
    if ! command -v pipx >/dev/null 2>&1; then
        log_warn "pipx not found; installer will use pip3 --user mode."
    fi

    if [[ "$SKIP_FFMPEG" -eq 0 ]] && ! command -v ffmpeg >/dev/null 2>&1; then
        if [[ "$opt_ffmpeg" -eq 1 ]]; then
            log_warn "ffmpeg skipped by user. Downloads may fail/convert badly without it."
        else
            die "ffmpeg is required for reliable audio extraction. Install it or run with --skip-ffmpeg."
        fi
    fi
}

step_install() {
    if ! command -v pipx >/dev/null 2>&1 && ! command -v pip3 >/dev/null 2>&1; then
        echo "Neither pipx nor pip3 is available"
        exit 1
    fi
    if command -v pipx >/dev/null 2>&1; then
        pipx install "$INSTALL_SRC_DIR" --force --quiet
    else
        python3 -m pip install --user -e "$INSTALL_SRC_DIR" --quiet >/dev/null 2>&1 || \
        python3 -m pip install --user -e "$INSTALL_SRC_DIR" --quiet --break-system-packages
    fi
}

step_path() {
    LOCAL_BIN="$HOME/.local/bin"
    RC_FILE="$HOME/.bashrc"
    [[ "${SHELL:-}" == */zsh ]] && RC_FILE="$HOME/.zshrc"

    if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
        grep -qxF "export PATH=\"\$HOME/.local/bin:\$PATH\"" "$RC_FILE" 2>/dev/null || \
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC_FILE"
        echo "source_needed=$RC_FILE" > "$LOG_PATH_HINT"
    fi
}

step_write_marker() {
    mkdir -p "$INSTALL_STATE_DIR"
    cat > "$INSTALL_MARKER_FILE" <<EOF
eatcli install marker
disclaimer: DO NOT MODIFY THIS FILE MANUALLY.
version: $APP_VERSION
installed_at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
install_script: https://webjuniors.org/eatcli/install.sh
EOF
}


do_uninstall() {
    echo -e "${BOLD}${RED}Uninstalling eatmusic...${NC}"

    if command -v pipx >/dev/null 2>&1; then
        pipx uninstall eatmusic >/dev/null 2>&1 && echo "  [OK] Removed pipx package" || true
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -m pip uninstall eatmusic -y >/dev/null 2>&1 || true
    fi
    if command -v pip3 >/dev/null 2>&1; then
        pip3 uninstall eatmusic -y >/dev/null 2>&1 || true
    fi

    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import pathlib, shutil, site
for sp in site.getsitepackages() + [site.getusersitepackages()]:
    for p in pathlib.Path(sp).glob('eatmusic*'):
        shutil.rmtree(p, ignore_errors=True)
        print('  [OK] Removed', p)
"
    fi

    local eat_bin="$HOME/.local/bin/eat"
    if [[ -f "$eat_bin" ]]; then
        rm -f "$eat_bin"
        echo "  [OK] Removed $eat_bin"
    fi

    if [[ -d "$HOME/.config/eatcli" ]]; then
        rm -rf "$HOME/.config/eatcli"
        echo "  [OK] Removed ~/.config/eatcli"
    fi

    if [[ -d "$HOME/.cache/eatmusic" ]]; then
        rm -rf "$HOME/.cache/eatmusic"
        echo "  [OK] Removed ~/.cache/eatmusic"
    fi

    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$rc" ]] && grep -qF '.local/bin' "$rc"; then
            sed -i '/export PATH="\$HOME\/.local\/bin:\$PATH"/d' "$rc"
            echo "  [OK] Cleaned PATH from $rc"
        fi
    done

    echo ""
    echo -e "${GREEN}eatmusic fully uninstalled.${NC}"
    echo "Open a new terminal to reset your PATH."
    exit 0
}

detect_os
detect_previous_install
configure_privilege
[[ "$DO_UNINSTALL" -eq 1 ]] && do_uninstall
log_info "Detected OS: $OS_PRETTY"
if [[ "$OS_FAMILY" == "debian" ]]; then
    log_info "Using native Ubuntu/Debian install strategy (apt)."
elif [[ "$PKG_MGR" != "unknown" ]]; then
    log_warn "Non-Ubuntu system detected. Using $PKG_MGR strategy."
fi
if [[ "$OS_FAMILY" != "debian" && "$PKG_MGR" == "unknown" ]]; then
    die "Unsupported Linux distribution. Please install python3, pip/pipx, and ffmpeg manually."
fi
confirm_start

echo -e "${BOLD}${CYAN}"
log_info "Starting installer..."
run_step "Cleaning previous installation" "step_clean"
run_step "Preparing source package" "step_prepare_source"
[[ -f "$SRC_DIR_HINT" ]] && INSTALL_SRC_DIR="$(cat "$SRC_DIR_HINT")"
run_step "Installing prerequisites" "step_dependencies"
run_step "Checking Python runtime" "step_python"
run_step "Installing eatmusic package" "step_install"
run_step "Configuring PATH" "step_path"
run_step "Writing install marker" "step_write_marker"
printf "\n\n"

# Keep full live logs during install, then clear screen for a clean final summary.
if [[ -t 1 ]]; then
    clear
fi
echo -e "${BOLD}${CYAN}"
print_smooth <<'EOF'
$$$$$$$$\           $$\            $$$$$$\  $$\       $$$$$$\ 
$$  _____|          $$ |          $$  __$$\ $$ |      \_$$  _|
$$ |      $$$$$$\ $$$$$$\         $$ /  \__|$$ |        $$ |  
$$$$$\    \____$$\\_$$  _|        $$ |      $$ |        $$ |  
$$  __|   $$$$$$$ | $$ |          $$ |      $$ |        $$ |  
$$ |     $$  __$$ | $$ |$$\       $$ |  $$\ $$ |        $$ |  
$$$$$$$$\\$$$$$$$ | \$$$$  |      \$$$$$$  |$$$$$$$$\ $$$$$$\ 
\________|\_______|  \____/        \______/ \________|\______|
EOF
echo -e "${NC}"
echo "Installation complete."
if [ -f "$LOG_PATH_HINT" ]; then
    RC_FILE="$(cut -d= -f2- "$LOG_PATH_HINT")"
    rm -f "$LOG_PATH_HINT"
    echo "Run this command in current shell: source $RC_FILE"
fi
echo "Usage:"
echo "  eat <spotify_url>"
echo "  eat --setup"