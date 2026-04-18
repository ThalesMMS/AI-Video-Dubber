#!/usr/bin/env bash
set -euo pipefail

# Wrapper to create a small venv, install Piper, download a PT-BR voice,
# and run the sync script with sane defaults.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${SCRIPT_DIR}/.venv-tts}"
DATA_DIR="${DATA_DIR:-${HOME}/.cache/piper-voices}"
VOICE="${VOICE:-pt_BR-faber-medium}"

INPUT="${1:?Usage: $0 <input.srt> [output.mp3]}"
OUTPUT="${2:-${INPUT%.*}.synced.mp3}"
shift $(( $# > 0 ? 1 : 0 )) || true
shift $(( $# > 0 ? 1 : 0 )) || true
EXTRA_ARGS=("$@")

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found in PATH." >&2
  echo "macOS: brew install ffmpeg" >&2
  echo "Ubuntu: sudo apt-get install ffmpeg" >&2
  exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe not found in PATH (usually shipped with ffmpeg)." >&2
  exit 1
fi

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install -U pip wheel >/dev/null
"${VENV_DIR}/bin/python" -m pip install -U piper-tts >/dev/null
"${VENV_DIR}/bin/python" -m piper.download_voices "${VOICE}" --data-dir "${DATA_DIR}"

exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/sync_ptbr_piper.py" \
  --python-exe "${VENV_DIR}/bin/python" \
  --input "${INPUT}" \
  --output "${OUTPUT}" \
  --voice "${VOICE}" \
  --data-dir "${DATA_DIR}" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
