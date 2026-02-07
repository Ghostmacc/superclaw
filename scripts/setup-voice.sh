#!/usr/bin/env bash
# SuperClaw — Voice Bridge Setup
# Installs all dependencies for local voice and Discord voice modes.
# Requires: python3.12, ffmpeg, libportaudio2, NVIDIA GPU (optional but recommended)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUPERCLAW_DIR="$(dirname "$SCRIPT_DIR")"
BRIDGE_DIR="$SUPERCLAW_DIR/bridge"
VENV_DIR="$BRIDGE_DIR/voice-venv"
REQ_FILE="$BRIDGE_DIR/voice_requirements.txt"

# Colors
GREEN='\033[92m'
YELLOW='\033[93m'
RED='\033[91m'
CYAN='\033[96m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "  ${RED}[XX]${NC} $1"; }
info() { echo -e "  ${CYAN}[..]${NC} $1"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║          SUPERCLAW VOICE SETUP                   ║${NC}"
echo -e "${BOLD}║   Local Voice + Discord Voice Dependencies       ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Step 1: Check Python 3.12 ──────────────────────────────────────────────

echo -e "${BOLD}── Step 1: Python Environment ──${NC}"

PYTHON=""
if command -v python3.12 &>/dev/null; then
    PYTHON="python3.12"
    ok "Found python3.12 ($(python3.12 --version 2>&1))"
elif command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [[ "$PY_VER" == "3.12" ]] || [[ "$PY_VER" == "3.11" ]] || [[ "$PY_VER" == "3.13" ]]; then
        PYTHON="python3"
        ok "Found python3 ($PY_VER)"
    else
        warn "Default python3 is $PY_VER (3.14 may cause package issues)"
        if command -v python3.12 &>/dev/null; then
            PYTHON="python3.12"
        elif command -v python3.13 &>/dev/null; then
            PYTHON="python3.13"
        elif command -v python3.11 &>/dev/null; then
            PYTHON="python3.11"
        else
            PYTHON="python3"
            warn "Using python3 ($PY_VER) — some packages may fail"
        fi
    fi
else
    fail "Python 3 not found"
    exit 1
fi

info "Using: $PYTHON ($(${PYTHON} --version 2>&1))"

# ─── Step 2: System Dependencies ────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 2: System Dependencies ──${NC}"

NEED_APT=()

if ! dpkg -s libportaudio2 &>/dev/null 2>&1; then
    NEED_APT+=("libportaudio2")
fi
if ! dpkg -s portaudio19-dev &>/dev/null 2>&1; then
    NEED_APT+=("portaudio19-dev")
fi

if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
else
    fail "ffmpeg not found — required for audio processing"
    NEED_APT+=("ffmpeg")
fi

if [ ${#NEED_APT[@]} -gt 0 ]; then
    if command -v apt &>/dev/null; then
        info "Installing system packages: ${NEED_APT[*]}"
        sudo apt install -y "${NEED_APT[@]}"
        ok "System packages installed"
    elif command -v dnf &>/dev/null; then
        info "Installing system packages via dnf: ${NEED_APT[*]}"
        sudo dnf install -y "${NEED_APT[@]}"
        ok "System packages installed"
    elif command -v pacman &>/dev/null; then
        info "Installing system packages via pacman: ${NEED_APT[*]}"
        sudo pacman -S --noconfirm "${NEED_APT[@]}"
        ok "System packages installed"
    else
        fail "Could not detect package manager (tried apt, dnf, pacman)"
        fail "Please install manually: ${NEED_APT[*]}"
        exit 1
    fi
else
    ok "All system audio libraries present"
fi

# ─── Step 3: GPU Check ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 3: GPU Detection ──${NC}"

if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "unknown")
    GPU_MEM=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader 2>/dev/null || echo "unknown")
    ok "GPU: $GPU_NAME ($GPU_MEM free)"
    info "Whisper will use CUDA acceleration"
    USE_GPU="cuda"
else
    info "No NVIDIA GPU — Whisper will run on CPU (slower but functional)"
    USE_GPU="cpu"
fi

# ─── Step 4: Python Virtual Environment ─────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 4: Python Virtual Environment ──${NC}"

if [ ! -d "$VENV_DIR" ]; then
    info "Creating voice venv at bridge/voice-venv/ ..."
    $PYTHON -m venv "$VENV_DIR"
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

# Activate and upgrade pip
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet 2>/dev/null
ok "pip upgraded"

# ─── Step 5: Install Python Dependencies ────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 5: Python Dependencies ──${NC}"

if [ ! -f "$REQ_FILE" ]; then
    fail "voice_requirements.txt not found at $REQ_FILE"
    exit 1
fi

info "Installing voice dependencies (this may take a few minutes)..."
pip install -r "$REQ_FILE" 2>&1 | tail -5

# Install PyTorch with CUDA if GPU available
if [ "$USE_GPU" = "cuda" ]; then
    info "Installing PyTorch with CUDA support..."
    pip install torch --index-url https://download.pytorch.org/whl/cu126 --quiet 2>/dev/null || \
    pip install torch --index-url https://download.pytorch.org/whl/cu124 --quiet 2>/dev/null || \
    pip install torch --quiet 2>/dev/null
fi

ok "Python dependencies installed"

# ─── Step 6: Download Whisper Model ─────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 6: Whisper Model ──${NC}"

info "Pre-downloading faster-whisper 'base' model (~150MB)..."
python -c "
from faster_whisper import WhisperModel
model = WhisperModel('base', device='$USE_GPU', compute_type='float16' if '$USE_GPU' == 'cuda' else 'int8')
print('Model loaded successfully')
" 2>/dev/null && ok "Whisper 'base' model ready" || warn "Model will download on first use"

# ─── Step 7: Test Audio Devices ─────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 7: Audio Device Check ──${NC}"

python -c "
import sounddevice as sd
devices = sd.query_devices()
inputs = [d for d in devices if d['max_input_channels'] > 0]
outputs = [d for d in devices if d['max_output_channels'] > 0]
print(f'Input devices: {len(inputs)}')
for d in inputs:
    print(f'  - {d[\"name\"]} ({d[\"max_input_channels\"]}ch)')
print(f'Output devices: {len(outputs)}')
for d in outputs[:3]:
    print(f'  - {d[\"name\"]} ({d[\"max_output_channels\"]}ch)')
" 2>/dev/null && ok "Audio devices detected" || warn "No audio devices found (headless server?)"

# ─── Step 8: Test edge-tts ──────────────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 8: TTS Verification ──${NC}"

python -c "import edge_tts; print('edge-tts available')" 2>/dev/null && \
    ok "edge-tts ready (Microsoft Neural Voices)" || warn "edge-tts not available"

# ─── Step 9: Test Discord library ───────────────────────────────────────────

echo ""
echo -e "${BOLD}── Step 9: Discord Library ──${NC}"

python -c "import discord; print(f'py-cord {discord.__version__}')" 2>/dev/null && \
    ok "py-cord ready for Discord voice" || warn "py-cord not available"

# ─── Summary ────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Voice Setup Summary${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Venv:     ${CYAN}$VENV_DIR${NC}"
echo -e "  Activate: ${CYAN}source $VENV_DIR/bin/activate${NC}"
echo ""
echo -e "  ${BOLD}Start Local Voice:${NC}"
echo -e "    $VENV_DIR/bin/python $BRIDGE_DIR/voice_bridge.py"
echo ""
echo -e "  ${BOLD}Start Discord Voice Bot:${NC}"
echo -e "    DISCORD_BOT_TOKEN=your_token $VENV_DIR/bin/python $BRIDGE_DIR/discord_bridge.py"
echo ""
echo -e "  ${BOLD}Environment Variables:${NC}"
echo -e "    WHISPER_MODEL=base          (base/small/medium/large)"
echo -e "    WHISPER_DEVICE=$USE_GPU     (cuda/cpu)"
echo -e "    HERMES_URL=http://localhost:8787"
echo -e "    TTS_VOICE=en-US-AriaNeural"
echo -e "    DISCORD_BOT_TOKEN=          (from Discord Developer Portal)"
echo ""

deactivate 2>/dev/null || true
