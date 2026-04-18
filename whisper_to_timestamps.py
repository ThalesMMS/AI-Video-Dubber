#!/usr/bin/env python3
"""Transcribe an audio file to timestamped text using local openai-whisper.

Outputs:
  - <prefix>.srt                (standard subtitle file)
  - <prefix>.segments.txt       (one segment per line; easy to translate)
  - <prefix>.json               (full Whisper result, including words if enabled)
  - <prefix>.txt                (plain transcript without timestamps)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Mapping, Any



def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_text_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def write_srt(segments: Iterable[Mapping[str, Any]], output_path: Path) -> None:
    lines: list[str] = []
    idx = 1
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = format_srt_timestamp(float(seg["start"]))
        end = format_srt_timestamp(float(seg["end"]))
        lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
        idx += 1
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_segments_txt(segments: Iterable[Mapping[str, Any]], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        for seg in segments:
            text = str(seg.get("text", "")).strip().replace("\n", " ")
            if not text:
                continue
            start = format_srt_timestamp(float(seg["start"]))
            end = format_srt_timestamp(float(seg["end"]))
            f.write(f"{start} --> {end}\t{text}\n")


def make_json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    # numpy / torch scalars often expose .item()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return obj


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe an MP3 (or any ffmpeg-readable media file) into timestamped text using local Whisper."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input media file (e.g. input.mp3 or input.mp4).",
    )
    parser.add_argument(
        "--model",
        default="large-v3",
        help="Whisper model name. Default: large-v3",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Language code for the source audio. Default: en",
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Prefix for output files. Default: based on input filename.",
    )
    parser.add_argument(
        "--without-word-timestamps",
        action="store_true",
        help="Disable word-level timestamps in the JSON output.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show Whisper progress.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    prefix = (
        Path(args.output_prefix).expanduser().resolve()
        if args.output_prefix
        else input_path.with_suffix("")
    )

    try:
        import whisper
    except ImportError as e:
        raise SystemExit("The `openai-whisper` package is not installed. Install it with: pip install -U openai-whisper") from e

    model = whisper.load_model(args.model)
    result = model.transcribe(
        str(input_path),
        language=args.language,
        task="transcribe",
        verbose=args.verbose,
        word_timestamps=not args.without_word_timestamps,
        fp16=False,
    )

    segments = result.get("segments", [])
    if not segments:
        raise SystemExit("Whisper returned no segments.")

    srt_path = prefix.with_suffix(".srt")
    segments_txt_path = prefix.with_suffix(".segments.txt")
    json_path = prefix.with_suffix(".json")
    txt_path = prefix.with_suffix(".txt")

    write_srt(segments, srt_path)
    write_segments_txt(segments, segments_txt_path)
    json_path.write_text(
        json.dumps(make_json_safe(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    txt_path.write_text(str(result.get("text", "")).strip() + "\n", encoding="utf-8")

    print("Done.")
    print(f"SRT:          {srt_path}")
    print(f"Segments TXT: {segments_txt_path}")
    print(f"JSON:         {json_path}")
    print(f"Plain TXT:    {txt_path}")
    print()
    print("For translation, edit either the .srt or .segments.txt file and preserve the timestamps.")


if __name__ == "__main__":
    main()
