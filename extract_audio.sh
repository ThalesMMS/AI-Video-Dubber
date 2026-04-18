#!/usr/bin/env bash
set -euo pipefail

# Extract audio from a video file as MP3.
# Usage: bash extract_audio.sh [input.mp4] [output.mp3]
#
# Example:
#   bash extract_audio.sh input.mp4 input.mp3

VIDEO="${1:?Usage: $0 <video> [output.mp3]}"
OUTPUT="${2:-${VIDEO%.*}.mp3}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found in PATH." >&2
  exit 1
fi

echo "Video:  ${VIDEO}"
echo "Output: ${OUTPUT}"

ffmpeg -y -i "${VIDEO}" -vn -acodec libmp3lame -q:a 2 "${OUTPUT}"

echo "Done → ${OUTPUT}"
