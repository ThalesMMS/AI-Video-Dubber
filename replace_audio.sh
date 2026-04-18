#!/usr/bin/env bash
set -euo pipefail

# Replace the audio track of a video file with a new audio file.
# Usage: bash replace_audio.sh <video> <audio> [output]
#
# Example:
#   bash replace_audio.sh input.mp4 input.pt-BR.synced.mp3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VIDEO="${1:?Usage: $0 <video> <audio> [output]}"
AUDIO="${2:?Usage: $0 <video> <audio> [output]}"
OUTPUT="${3:-${VIDEO%.*}.pt.synced.mp4}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found in PATH." >&2
  echo "macOS: brew install ffmpeg" >&2
  echo "Ubuntu: sudo apt-get install ffmpeg" >&2
  exit 1
fi

echo "Video:  ${VIDEO}"
echo "Audio:  ${AUDIO}"
echo "Output: ${OUTPUT}"

ffmpeg -y -i "${VIDEO}" -i "${AUDIO}" \
  -c:v copy -map 0:v:0 -map 1:a:0 \
  -shortest \
  "${OUTPUT}"

echo "Done → ${OUTPUT}"
