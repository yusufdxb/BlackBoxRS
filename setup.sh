#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
CONFIG_DIR="${HOME}/.blackboxrs"

echo "=== BlackBoxRS Setup ==="
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "${VENV_DIR}" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv "${VENV_DIR}"
else
    echo "[1/4] Virtual environment already exists, skipping."
fi

# Activate venv
source "${VENV_DIR}/bin/activate"

# Upgrade pip
echo "[2/4] Upgrading pip..."
pip install --upgrade pip --quiet

# Install package in editable mode with dev dependencies
echo "[3/4] Installing BlackBoxRS and dependencies..."
pip install -e "${SCRIPT_DIR}[dev]" --quiet

# Create config directory
echo "[4/4] Creating config directory..."
mkdir -p "${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}/logs"

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Activate the environment:  source ${VENV_DIR}/bin/activate"
echo "  Initialize config:         robot-blackbox init"
echo "  Start recording:           robot-blackbox start --foreground"
echo ""
