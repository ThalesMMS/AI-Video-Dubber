#!/usr/bin/env bash
set -euo pipefail

# Translate an SRT file using an OpenAI-compatible LLM.
# Usage: bash translate_srt.sh [input.srt] [output.srt]
#
# Example:
#   bash translate_srt.sh input.srt input.pt-BR.srt

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT="${1:?Usage: $0 <input.srt> [output.srt]}"
OUTPUT="${2:-${INPUT%.srt}.pt.srt}"

exec python3 "${SCRIPT_DIR}/translate_srt.py" \
  --input "${INPUT}" \
  --output "${OUTPUT}"
