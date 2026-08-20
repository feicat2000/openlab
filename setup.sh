#!/usr/bin/env bash
# =============================================================================
#  OpenLab – Automated In-Vitro Experiment Platform
#  setup.sh  –  Creates virtualenv, installs deps, scaffolds directories,
#               generates .env template, and validates hardware connectivity.
# =============================================================================
set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}═══ $* ═══${NC}\n"; }

# ── Configuration ─────────────────────────────────────────────────────────────
VENV_DIR=".venv"
PYTHON_MIN="3.10"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${PROJECT_ROOT}/logs"
DATA_DIR="${PROJECT_ROOT}/data"
RUNS_DIR="${PROJECT_ROOT}/runs"

header "OpenLab Platform Setup"
echo -e "  Project root : ${PROJECT_ROOT}"
echo -e "  Virtual env  : ${VENV_DIR}"
echo -e "  Date         : $(date)"
echo ""

# ── 1. Python version check ───────────────────────────────────────────────────
header "1 · Python Version Check"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_BIN="$candidate"
            success "Found $candidate ($VER)"
            break
        fi
    fi
done

[ -z "$PYTHON_BIN" ] && error "Python >= ${PYTHON_MIN} not found. Install from https://python.org"

# ── 2. System dependencies ────────────────────────────────────────────────────
header "2 · System Dependencies"

OS="$(uname -s)"
if [ "$OS" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
        info "Detected Debian/Ubuntu – installing system packages..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq \
            python3-venv python3-dev build-essential \
            libffi-dev libssl-dev \
            libudev-dev libusb-1.0-0-dev \
            usbutils curl git
        success "System packages installed"
    elif command -v dnf &>/dev/null; then
        info "Detected Fedora/RHEL – installing system packages..."
        sudo dnf install -y python3-devel gcc libffi-devel openssl-devel \
            libusbx-devel curl git
        success "System packages installed"
    else
        warn "Unknown Linux distro – skipping system package install"
    fi
elif [ "$OS" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
        info "Detected macOS – installing Homebrew packages..."
        brew install python@3.11 libusb curl git || true
        success "Homebrew packages installed"
    else
        warn "Homebrew not found – skipping macOS package install"
    fi
else
    warn "Windows detected – run setup.bat instead (or use WSL2)"
fi

# ── 3. Virtual environment ────────────────────────────────────────────────────
header "3 · Virtual Environment"

if [ -d "${VENV_DIR}" ]; then
    warn "Existing venv found at ${VENV_DIR}"
    read -rp "  Recreate it? [y/N] " RECREATE
    if [[ "${RECREATE:-n}" =~ ^[Yy]$ ]]; then
        rm -rf "${VENV_DIR}"
        info "Removed old venv"
    else
        info "Keeping existing venv"
    fi
fi

if [ ! -d "${VENV_DIR}" ]; then
    info "Creating virtual environment..."
    "$PYTHON_BIN" -m venv "${VENV_DIR}"
    success "Created ${VENV_DIR}"
fi

# Activate
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
success "Activated ${VENV_DIR}"

# Upgrade pip / wheel / setuptools
info "Upgrading pip, wheel, setuptools..."
pip install --quiet --upgrade pip wheel setuptools

# ── 4. Install Python dependencies ───────────────────────────────────────────
header "4 · Python Dependencies"

info "Installing from requirements.txt (this may take a few minutes)..."

# Install non-hardware deps first (always succeed)
CORE_PKGS=(
    "pyyaml>=6.0.1"
    "pydantic>=2.5.0"
    "rich>=13.7.0"
    "click>=8.1.7"
    "python-dotenv>=1.0.0"
    "loguru>=0.7.2"
    "fastapi>=0.109.0"
    "uvicorn[standard]>=0.27.0"
    "websockets>=12.0"
    "requests>=2.31.0"
    "pyserial>=3.5"
    "psutil>=5.9.8"
    "pandas>=2.2.0"
    "openpyxl>=3.1.2"
    "jinja2>=3.1.3"
    "matplotlib>=3.8.3"
    "pytest>=8.0.0"
    "pytest-asyncio>=0.23.4"
    "pytest-cov>=4.1.0"
    "anthropic>=0.23.0"
    "apscheduler>=3.10.4"
)

pip install --quiet "${CORE_PKGS[@]}"
success "Core packages installed"

# Hardware-specific – warn on failure rather than abort
HARDWARE_PKGS=(
    "ur-rtde>=1.5.5"
    "opentrons-http-api-client>=0.1.0"
)

for pkg in "${HARDWARE_PKGS[@]}"; do
    if pip install --quiet "$pkg" 2>/dev/null; then
        success "Installed $pkg"
    else
        warn "Could not install $pkg (hardware driver – may require physical device)"
    fi
done

# Raspberry Pi GPIO (only on ARM)
ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" || "$ARCH" == "armv7l" ]]; then
    info "ARM detected – installing RPi.GPIO and gpiozero..."
    pip install --quiet RPi.GPIO gpiozero && success "GPIO packages installed" \
        || warn "GPIO install failed – check raspi-config"
fi

# ── 5. Directory scaffold ─────────────────────────────────────────────────────
header "5 · Directory Scaffold"

DIRS=(
    "config"
    "src/core"
    "src/equipment/peripherals"
    "src/safety"
    "src/ui"
    "protocols"
    "docs"
    "tests"
    "${LOG_DIR}"
    "${DATA_DIR}"
    "${RUNS_DIR}"
)

for d in "${DIRS[@]}"; do
    mkdir -p "${PROJECT_ROOT}/${d}"
    # Create __init__.py for Python packages
    [[ "$d" == src* ]] && touch "${PROJECT_ROOT}/${d}/__init__.py"
done

success "Directory structure created"

# ── 6. .env template ─────────────────────────────────────────────────────────
header "6 · Environment Configuration"

ENV_FILE="${PROJECT_ROOT}/.env"
if [ ! -f "${ENV_FILE}" ]; then
    cat > "${ENV_FILE}" << 'ENVEOF'
# ──────────────────────────────────────────────────────────────────────────────
#  OpenLab  –  Environment Configuration
#  Copy this file to .env and fill in values for your lab setup.
# ──────────────────────────────────────────────────────────────────────────────

# ── Opentrons ─────────────────────────────────────────────────────────────────
OPENTRONS_HOST=169.254.xx.xx          # OT-2/Flex IP (USB: 169.254.xx.xx)
OPENTRONS_PORT=31950
OPENTRONS_ROBOT_TYPE=OT2              # OT2 | FLEX

# ── Universal Robots Arm ──────────────────────────────────────────────────────
UR_HOST=192.168.1.10                  # UR controller IP
UR_PORT=30004                         # RTDE port
UR_TOOL_TCP=0,0,0.15,0,0,0           # TCP offset x,y,z,rx,ry,rz (m / rad)
UR_GRIPPER_PORT=/dev/ttyUSB0          # Robotiq gripper serial port

# ── Serial peripherals ────────────────────────────────────────────────────────
VORTEX_PORT=/dev/ttyUSB1              # IKA Vortex RS-232
CENTRIFUGE_PORT=/dev/ttyUSB2          # Eppendorf 5430/5810 RS-232
INCUBATOR_PORT=/dev/ttyUSB3           # Inheco ODTC / ThermoShaker RS-232
ROCKER_PORT=/dev/ttyUSB4              # Stuart rocker (relay via Arduino)

# ── Relay board (GPIO or USB-HID) ─────────────────────────────────────────────
RELAY_BACKEND=serial                  # serial | gpio | simulation
RELAY_PORT=/dev/ttyUSB5               # USB relay board (e.g. CH340)
MAG_SEPARATOR_RELAY_CHANNEL=1
ROCKER_RELAY_CHANNEL=2

# ── Safety ────────────────────────────────────────────────────────────────────
EMERGENCY_STOP_GPIO_PIN=17            # RPi BCM pin for physical E-stop button
MAX_CENTRIFUGE_RPM=14000
MAX_VORTEX_RPM=3000
MAX_INCUBATOR_TEMP_C=95

# ── API ───────────────────────────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8000
API_SECRET_KEY=change-me-in-production

# ── LLM (optional – for natural-language protocol parsing) ───────────────────
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
LLM_PROVIDER=anthropic                # anthropic | openai | none

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO                        # DEBUG | INFO | WARNING | ERROR
LOG_DIR=logs
SIMULATION_MODE=true                  # true = no real hardware (safe default)
ENVEOF
    success "Created .env template"
else
    warn ".env already exists – skipping"
fi

# ── 7. udev rules for USB devices ────────────────────────────────────────────
header "7 · udev Rules (Linux USB)"

if [ "$OS" = "Linux" ]; then
    UDEV_FILE="/etc/udev/rules.d/99-openlab.rules"
    if [ ! -f "$UDEV_FILE" ]; then
        info "Installing udev rules for Opentrons, UR arm, serial peripherals..."
        sudo tee "$UDEV_FILE" > /dev/null << 'UDEVEOF'
# OpenLab udev rules
# Opentrons OT-2 (USB)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0666", GROUP="dialout"
# Opentrons Flex (USB-C)
SUBSYSTEM=="usb", ATTRS{idVendor}=="04d8", MODE="0666", GROUP="dialout"
# Generic USB-Serial (CH340, CP2102, FTDI)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666", GROUP="dialout"  # CH340
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0666", GROUP="dialout"  # CP2102
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0666", GROUP="dialout"  # FTDI
# Robotiq gripper
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="robotiq_gripper", MODE="0666"
UDEVEOF
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        # Add user to dialout group
        sudo usermod -aG dialout "${USER}" && \
            warn "Added ${USER} to dialout group – re-login or run: newgrp dialout"
        success "udev rules installed"
    else
        info "udev rules already present"
    fi
fi

# ── 8. Hardware connectivity check ───────────────────────────────────────────
header "8 · Hardware Connectivity Check"

if [ -f "${ENV_FILE}" ]; then
    set -a; source "${ENV_FILE}"; set +a
fi

SIMULATION_MODE="${SIMULATION_MODE:-true}"

if [ "${SIMULATION_MODE}" = "true" ]; then
    warn "SIMULATION_MODE=true in .env – skipping real hardware checks"
    warn "Set SIMULATION_MODE=false and configure IPs/ports when hardware is ready"
else
    # Opentrons ping
    OT_HOST="${OPENTRONS_HOST:-}"
    if [ -n "$OT_HOST" ]; then
        if curl -sf --max-time 3 "http://${OT_HOST}:${OPENTRONS_PORT:-31950}/health" > /dev/null 2>&1; then
            success "Opentrons reachable at ${OT_HOST}"
        else
            warn "Opentrons NOT reachable at ${OT_HOST} – check USB/network connection"
        fi
    fi

    # UR arm ping
    UR_HOST="${UR_HOST:-}"
    if [ -n "$UR_HOST" ]; then
        if nc -z -w3 "$UR_HOST" "${UR_PORT:-30004}" 2>/dev/null; then
            success "UR arm reachable at ${UR_HOST}:${UR_PORT}"
        else
            warn "UR arm NOT reachable at ${UR_HOST} – check network/teach pendant"
        fi
    fi
fi

# ── 9. Quick smoke test ───────────────────────────────────────────────────────
header "9 · Python Import Smoke Test"

"${VENV_DIR}/bin/python" - << 'PYEOF'
import sys, importlib

REQUIRED = [
    "yaml", "pydantic", "rich", "click", "dotenv",
    "loguru", "fastapi", "uvicorn", "serial",
    "pandas", "jinja2", "anthropic", "apscheduler",
]
OPTIONAL = ["rtde", "opentrons_http_api_client"]

failed = []
for mod in REQUIRED:
    try:
        importlib.import_module(mod)
        print(f"  \033[32m✓\033[0m  {mod}")
    except ImportError as e:
        print(f"  \033[31m✗\033[0m  {mod}  ({e})")
        failed.append(mod)

for mod in OPTIONAL:
    try:
        importlib.import_module(mod)
        print(f"  \033[32m✓\033[0m  {mod} (optional)")
    except ImportError:
        print(f"  \033[33m–\033[0m  {mod} (optional – install when hardware present)")

if failed:
    print(f"\n\033[31mFailed required imports: {failed}\033[0m")
    sys.exit(1)
else:
    print("\n\033[32mAll required imports OK\033[0m")
PYEOF

# ── 10. Generate activation helper ───────────────────────────────────────────
header "10 · Activation Helper"

cat > "${PROJECT_ROOT}/activate_openlab.sh" << ACTEOF
#!/usr/bin/env bash
# Source this file to activate the OpenLab environment:
#   source activate_openlab.sh
source "${PROJECT_ROOT}/${VENV_DIR}/bin/activate"
export OPENLAB_ROOT="${PROJECT_ROOT}"
set -a; [ -f "${PROJECT_ROOT}/.env" ] && source "${PROJECT_ROOT}/.env"; set +a
echo "OpenLab environment active. Run: python main.py --help"
ACTEOF
chmod +x "${PROJECT_ROOT}/activate_openlab.sh"
success "Created activate_openlab.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
header "Setup Complete"

echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║          OpenLab Platform – Ready                   ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Next steps:"
echo -e ""
echo -e "  1. Edit  ${YELLOW}.env${NC}  with your hardware IPs / serial ports"
echo -e "  2. Activate env:   ${CYAN}source activate_openlab.sh${NC}"
echo -e "  3. Run platform:   ${CYAN}python main.py run protocols/dna_extraction.yaml${NC}"
echo -e "  4. Start REST API: ${CYAN}python main.py api${NC}"
echo -e "  5. Simulation:     ${CYAN}python main.py run protocols/dna_extraction.yaml --simulate${NC}"
echo -e ""
echo -e "  Docs: ${CYAN}docs/BOM.md${NC}  |  ${CYAN}docs/hardware_guide.md${NC}"
echo -e ""
