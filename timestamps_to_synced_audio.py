#!/usr/bin/env python3
"""Convert timestamped text (SRT or segments.txt) into synchronized speech on macOS.

Requirements:
  - macOS built-in `say`
  - ffmpeg / ffprobe available in PATH

Typical workflow:
  1) Generate English timestamps with whisper_to_timestamps.py
  2) Translate the .srt or .segments.txt to Portuguese, keeping timestamps unchanged
  3) Run this script to synthesize the Portuguese audio aligned to those timestamps
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


TIMESTAMP_RE = r"\d{2}:\d{2}:\d{2}[,.]\d{3}"
SRT_BLOCK_RE = re.compile(
    rf"^\s*(?:(?P<idx>\d+)\s*\n)?\s*(?P<start>{TIMESTAMP_RE})\s*-->\s*(?P<end>{TIMESTAMP_RE})\s*\n(?P<text>.*)$",
    re.DOTALL,
)
SEGMENT_LINE_RE = re.compile(
    rf"^\s*(?P<start>{TIMESTAMP_RE})\s*-->\s*(?P<end>{TIMESTAMP_RE})\s*(?:\t+|\s{{2,}})(?P<text>.*)$"
)
LOCALE_RE = re.compile(r"^(?P<voice>.*?)\s+(?P<locale>[a-z]{2}[_-][A-Z]{2})\s+#")


@dataclass(order=True)
class Cue:
    start: float
    end: float
    text: str
    index: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def run(cmd: List[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def check_dependencies() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This script uses the macOS `say` command and must be run on macOS.")
    if shutil.which("say") is None:
        raise SystemExit("`say` was not found in PATH.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("`ffmpeg` was not found in PATH. Install it with: brew install ffmpeg")
    if shutil.which("ffprobe") is None:
        raise SystemExit("`ffprobe` was not found in PATH. It usually comes with ffmpeg.")


def parse_timestamp(ts: str) -> float:
    ts = ts.replace(",", ".")
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(".")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(content: str) -> List[Cue]:
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    cues: List[Cue] = []
    cue_idx = 1
    for block in blocks:
        lines = [line.rstrip() for line in block.strip().splitlines()]
        if not lines:
            continue
        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0].strip()):
            timestamp_line = lines[1].strip()
            text_lines = lines[2:]
            raw_idx = int(lines[0].strip())
        else:
            timestamp_line = lines[0].strip()
            text_lines = lines[1:]
            raw_idx = cue_idx
        if "-->" not in timestamp_line:
            continue
        start_str, end_str = [part.strip() for part in timestamp_line.split("-->", 1)]
        text = " ".join(line.strip() for line in text_lines).strip()
        cues.append(Cue(start=parse_timestamp(start_str), end=parse_timestamp(end_str), text=text, index=raw_idx))
        cue_idx += 1
    return cues


def parse_segments_txt(content: str) -> List[Cue]:
    cues: List[Cue] = []
    for i, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = SEGMENT_LINE_RE.match(line)
        if not match:
            raise ValueError(
                "Could not parse line as segments.txt format. Expected: "
                "HH:MM:SS,mmm --> HH:MM:SS,mmm<TAB>text"
            )
        cues.append(
            Cue(
                start=parse_timestamp(match.group("start")),
                end=parse_timestamp(match.group("end")),
                text=match.group("text").strip(),
                index=i,
            )
        )
    return cues


def parse_timestamped_file(path: Path) -> List[Cue]:
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".srt":
        cues = parse_srt(content)
    else:
        try:
            cues = parse_segments_txt(content)
        except ValueError:
            cues = parse_srt(content)
    cues = [Cue(start=c.start, end=c.end, text=re.sub(r"\s+", " ", c.text).strip(), index=c.index) for c in cues]
    cues = [c for c in cues if c.duration > 0]
    cues.sort(key=lambda c: (c.start, c.end, c.index))
    if not cues:
        raise SystemExit(f"No valid timestamped cues found in: {path}")
    return cues


def list_voices() -> str:
    return run(["say", "-v", "?"], capture_output=True).stdout


def choose_portuguese_voice(explicit_voice: Optional[str]) -> str:
    if explicit_voice:
        return explicit_voice
    output = list_voices()
    for line in output.splitlines():
        match = LOCALE_RE.match(line)
        if not match:
            continue
        locale = match.group("locale").lower().replace("-", "_")
        if locale.startswith("pt_"):
            return match.group("voice").strip()
    raise SystemExit(
        "No Portuguese voice was auto-detected. Run with --list-voices and pass one via --voice."
    )


def ffprobe_duration_seconds(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
    )
    return float(result.stdout.strip())


def build_atempo_chain(target_tempo: float) -> str:
    """Return a daisy-chained atempo filter string.

    Keeps each factor in [0.5, 2.0] for better quality.
    The overall product equals target_tempo.
    """
    if target_tempo <= 0:
        raise ValueError("target_tempo must be > 0")

    factors: List[float] = []
    remaining = target_tempo

    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5

    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0

    factors.append(remaining)
    factors = [f for f in factors if not math.isclose(f, 1.0, rel_tol=1e-9, abs_tol=1e-9)]
    if not factors:
        return "anull"
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def create_silence_wav(path: Path, duration_seconds: float, sample_rate: int) -> None:
    frames = max(0, int(round(duration_seconds * sample_rate)))
    chunk_frames = min(frames, 65_536)
    silence_chunk = b"\x00\x00" * chunk_frames  # mono, 16-bit PCM
    remaining = frames
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        while remaining > 0:
            to_write = min(remaining, chunk_frames)
            wav_file.writeframes(silence_chunk[: to_write * 2])
            remaining -= to_write


def synthesize_with_say(text: str, output_aiff: Path, voice: str, rate: Optional[int]) -> None:
    cmd = ["say", "-v", voice]
    if rate is not None:
        cmd.extend(["-r", str(rate)])
    cmd.extend(["-o", str(output_aiff), text])
    run(cmd)


def fit_segment_to_slot(
    raw_aiff: Path,
    fitted_wav: Path,
    slot_duration: float,
    sample_rate: int,
) -> float:
    raw_duration = ffprobe_duration_seconds(raw_aiff)
    if slot_duration <= 0:
        raise ValueError("slot_duration must be > 0")

    tempo = max(0.01, raw_duration / slot_duration)
    atempo_filter = build_atempo_chain(tempo)
    filter_chain = ",".join(
        [
            atempo_filter,
            f"apad=whole_dur={slot_duration:.6f}",
            f"atrim=0:{slot_duration:.6f}",
            "asetpts=N/SR/TB",
        ]
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_aiff),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-af",
            filter_chain,
            str(fitted_wav),
        ]
    )
    return tempo


def append_wavs(wav_paths: Iterable[Path], output_wav: Path, sample_rate: int) -> None:
    with wave.open(str(output_wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for wav_path in wav_paths:
            with wave.open(str(wav_path), "rb") as src:
                if src.getnchannels() != 1 or src.getsampwidth() != 2 or src.getframerate() != sample_rate:
                    raise ValueError(f"Unexpected WAV format for {wav_path}")
                out.writeframes(src.readframes(src.getnframes()))


def transcode_output(input_wav: Path, output_path: Path) -> None:
    if output_path.suffix.lower() == ".wav":
        shutil.copyfile(input_wav, output_path)
        return

    cmd = ["ffmpeg", "-y", "-i", str(input_wav)]
    if output_path.suffix.lower() == ".mp3":
        cmd.extend(["-q:a", "2"])
    cmd.append(str(output_path))
    run(cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a translated SRT / segments.txt file into synchronized audio on macOS."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Translated .srt or .segments.txt file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Final audio file (.wav or .mp3).",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="macOS `say` voice name. If omitted, the script tries to auto-pick a Portuguese voice.",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=None,
        help="Optional base `say` speech rate (words per minute). Example: 185",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=24000,
        help="Output sample rate. Default: 24000",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="Print `say -v ?` and exit.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep intermediate files instead of deleting them.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_voices:
        check_dependencies()
        print(list_voices())
        return

    check_dependencies()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise SystemExit(f"Input timestamp file not found: {input_path}")

    voice = choose_portuguese_voice(args.voice)
    cues = parse_timestamped_file(input_path)

    if args.keep_temp:
        temp_dir_path = output_path.with_suffix(output_path.suffix + ".parts")
        temp_dir_path.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        temp_dir_path = Path(tempfile.mkdtemp(prefix="synced_audio_"))
        cleanup = True

    print(f"Voice: {voice}")
    print(f"Parsed {len(cues)} cues from {input_path.name}")
    print(f"Working directory: {temp_dir_path}")

    wav_parts: List[Path] = []
    cursor = 0.0

    try:
        for i, cue in enumerate(cues, start=1):
            if cue.start < cursor - 1e-6:
                print(
                    f"Warning: overlapping cue at index {cue.index}; clamping start from {cue.start:.3f}s to {cursor:.3f}s",
                    file=sys.stderr,
                )
                cue = Cue(start=cursor, end=max(cursor, cue.end), text=cue.text, index=cue.index)

            gap = max(0.0, cue.start - cursor)
            if gap > 0:
                silence_path = temp_dir_path / f"{i:04d}_gap.wav"
                create_silence_wav(silence_path, gap, args.sample_rate)
                wav_parts.append(silence_path)
                cursor += gap

            slot_duration = cue.duration
            if slot_duration <= 0:
                continue

            if cue.text:
                raw_aiff = temp_dir_path / f"{i:04d}_raw.aiff"
                fitted_wav = temp_dir_path / f"{i:04d}_slot.wav"
                synthesize_with_say(cue.text, raw_aiff, voice, args.rate)
                tempo = fit_segment_to_slot(raw_aiff, fitted_wav, slot_duration, args.sample_rate)
                wav_parts.append(fitted_wav)
                print(
                    f"[{i:04d}/{len(cues)}] {cue.start:8.3f}-{cue.end:8.3f}s | tempo={tempo:5.2f} | {textwrap.shorten(cue.text, width=70, placeholder='…')}"
                )
            else:
                silent_slot = temp_dir_path / f"{i:04d}_empty.wav"
                create_silence_wav(silent_slot, slot_duration, args.sample_rate)
                wav_parts.append(silent_slot)
                print(f"[{i:04d}/{len(cues)}] {cue.start:8.3f}-{cue.end:8.3f}s | silence")

            cursor = cue.end

        if not wav_parts:
            raise SystemExit("No audio parts were generated.")

        final_wav = temp_dir_path / "final_synced.wav"
        append_wavs(wav_parts, final_wav, args.sample_rate)
        transcode_output(final_wav, output_path)
        print()
        print(f"Done: {output_path}")

    finally:
        if cleanup:
            shutil.rmtree(temp_dir_path, ignore_errors=True)


if __name__ == "__main__":
    main()
