#!/usr/bin/env bash
set -euo pipefail

# Master dubbing pipeline: video → extract audio → transcribe → translate → TTS → final video
#
# Usage:
#   bash dub_video.sh --input video.mp4 --language pt-BR
#   bash dub_video.sh --input video.mp4 --language es --force
#
# Produces: video.pt-BR.synced.mp4 (or video.es.synced.mp4, etc.)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${SCRIPT_DIR}/.venv}"
DATA_DIR="${DATA_DIR:-${HOME}/.cache/piper-voices}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"

# ── Defaults ──────────────────────────────────────────────────────────────
INPUT=""
LANGUAGE="pt-BR"
OUTPUT=""
FORCE=false

# ── Language → Piper voice / display name mapping ─────────────────────────
lang_to_voice() {
  case "$1" in
    pt-BR|pt_BR) echo "pt_BR-faber-medium" ;;
    es|es-ES)    echo "es_ES-davefx-medium" ;;
    fr|fr-FR)    echo "fr_FR-siwis-medium" ;;
    de|de-DE)    echo "de_DE-thorsten-medium" ;;
    it|it-IT)    echo "it_IT-riccardo-x_low" ;;
    *)
      echo ""
      return 1
      ;;
  esac
}

lang_to_display() {
  case "$1" in
    pt-BR|pt_BR) echo "Brazilian Portuguese (pt-BR)" ;;
    es|es-ES)    echo "Spanish (es)" ;;
    fr|fr-FR)    echo "French (fr)" ;;
    de|de-DE)    echo "German (de)" ;;
    it|it-IT)    echo "Italian (it)" ;;
    *)           echo "$1" ;;
  esac
}

# ── Parse arguments ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)    INPUT="$2";    shift 2 ;;
    --language) LANGUAGE="$2"; shift 2 ;;
    --output)   OUTPUT="$2";   shift 2 ;;
    --force)    FORCE=true;    shift   ;;
    --help|-h)
      echo "Usage: $0 --input <video.mp4> [--language <lang>] [--output <out.mp4>] [--force]"
      echo ""
      echo "Supported languages: pt-BR, es, fr, de, it"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${INPUT}" ]]; then
  echo "Error: --input is required." >&2
  echo "Usage: $0 --input <video.mp4> [--language <lang>] [--output <out.mp4>] [--force]" >&2
  exit 1
fi

# ── Resolve paths ─────────────────────────────────────────────────────────
INPUT_ABS="$(cd "$(dirname "${INPUT}")" && pwd)/$(basename "${INPUT}")"
BASENAME="${INPUT_ABS%.*}"

AUDIO_MP3="${BASENAME}.mp3"
SRT_FILE="${BASENAME}.srt"
TRANSLATED_SRT="${BASENAME}.${LANGUAGE}.srt"
SYNCED_AUDIO="${BASENAME}.${LANGUAGE}.synced.mp3"
FINAL_VIDEO="${OUTPUT:-${BASENAME}.${LANGUAGE}.synced.mp4}"

VOICE="$(lang_to_voice "${LANGUAGE}")" || {
  echo "Error: unsupported language '${LANGUAGE}'." >&2
  echo "Supported: pt-BR, es, fr, de, it" >&2
  exit 1
}
DISPLAY_LANG="$(lang_to_display "${LANGUAGE}")"

# ── Dependency checks ─────────────────────────────────────────────────────
for cmd in ffmpeg ffprobe; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "${cmd} not found in PATH." >&2
    exit 1
  fi
done

# ── Helper ────────────────────────────────────────────────────────────────
banner() {
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "═══════════════════════════════════════════════════════════════"
}

should_run() {
  # Returns 0 (true) if --force or file does not exist
  [[ "${FORCE}" == "true" ]] || [[ ! -f "$1" ]]
}

# ── Print summary ─────────────────────────────────────────────────────────
echo ""
echo "Dubbing Pipeline"
echo "  Input:    ${INPUT_ABS}"
echo "  Language: ${DISPLAY_LANG}"
echo "  Voice:    ${VOICE}"
echo "  Output:   ${FINAL_VIDEO}"
echo ""

# ── Step 0: Setup virtual environment & dependencies ─────────────────────
banner "Step 0/5 — Setting up environment"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}..."
  "${SYSTEM_PYTHON}" -m venv "${VENV_DIR}"
fi
PYTHON="${VENV_DIR}/bin/python"

echo "Installing / updating dependencies (this may take a while the first time)..."
"${PYTHON}" -m pip install -U pip wheel 2>&1 | tail -1
"${PYTHON}" -m pip install -U openai-whisper piper-tts 2>&1 | tail -1

echo "Downloading TTS voice: ${VOICE}..."
"${PYTHON}" -m piper.download_voices "${VOICE}" --data-dir "${DATA_DIR}"
echo "Environment ready."

# ── Step 1: Extract audio ─────────────────────────────────────────────────
banner "Step 1/5 — Extract audio → ${AUDIO_MP3}"
if should_run "${AUDIO_MP3}"; then
  ffmpeg -y -i "${INPUT_ABS}" -vn -acodec libmp3lame -q:a 2 "${AUDIO_MP3}"
  echo "Done."
else
  echo "Skipped (file exists). Use --force to re-run."
fi

# ── Step 2: Transcribe with Whisper ───────────────────────────────────────
banner "Step 2/5 — Transcribe audio → ${SRT_FILE}"
if should_run "${SRT_FILE}"; then
  "${PYTHON}" "${SCRIPT_DIR}/whisper_to_timestamps.py" \
    --input "${AUDIO_MP3}" \
    --model "${WHISPER_MODEL}" \
    --language en
  echo "Done."
else
  echo "Skipped (file exists). Use --force to re-run."
fi

# ── Step 3: Translate SRT ─────────────────────────────────────────────────
banner "Step 3/5 — Translate subtitles → ${TRANSLATED_SRT}"
if should_run "${TRANSLATED_SRT}"; then
  "${PYTHON}" "${SCRIPT_DIR}/translate_srt.py" \
    --input "${SRT_FILE}" \
    --output "${TRANSLATED_SRT}" \
    --language "${DISPLAY_LANG}"
  echo "Done."
else
  echo "Skipped (file exists). Use --force to re-run."
fi

# ── Step 4: TTS sync ─────────────────────────────────────────────────────
banner "Step 4/5 — Generate synced audio → ${SYNCED_AUDIO}"
if should_run "${SYNCED_AUDIO}"; then
  "${PYTHON}" "${SCRIPT_DIR}/sync_ptbr_piper.py" \
    --python-exe "${PYTHON}" \
    --input "${TRANSLATED_SRT}" \
    --output "${SYNCED_AUDIO}" \
    --voice "${VOICE}" \
    --data-dir "${DATA_DIR}"
  echo "Done."
else
  echo "Skipped (file exists). Use --force to re-run."
fi

# ── Step 5: Replace audio in video ────────────────────────────────────────
banner "Step 5/5 — Merge video + audio → ${FINAL_VIDEO}"
ffmpeg -y -i "${INPUT_ABS}" -i "${SYNCED_AUDIO}" \
  -c:v copy -map 0:v:0 -map 1:a:0 \
  -shortest \
  "${FINAL_VIDEO}"

banner "Pipeline complete!"
echo "  Output: ${FINAL_VIDEO}"
echo ""
