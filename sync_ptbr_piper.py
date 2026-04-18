#!/usr/bin/env python3
"""Generate synchronized PT-BR narration from SRT/segments using local Piper TTS.

Design goals
------------
1. Better quality than macOS `say` / eSpeak for Portuguese.
2. Keep sync primarily by:
   - grouping tiny subtitle fragments into larger phrasing units,
   - adjusting Piper's `length_scale` first,
   - using only small ffmpeg tempo corrections as a last resort.
3. Remain practical: one script, local only, no API.

Expected setup
--------------
- Python 3.10+
- ffmpeg + ffprobe available in PATH
- piper-tts installed in the active environment

Typical usage
-------------
    python sync_ptbr_piper.py \
      --input input.pt-BR.srt \
      --output input.pt-BR.synced.mp3 \
      --voice pt_BR-faber-medium

To install Piper first:
    python -m pip install -U piper-tts

To download a voice manually:
    python -m piper.download_voices pt_BR-faber-medium --data-dir ~/.cache/piper-voices
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

TIMESTAMP_RE = r"\d{2}:\d{2}:\d{2}[,.]\d{3}"
SEGMENT_LINE_RE = re.compile(
    rf"^\s*(?P<start>{TIMESTAMP_RE})\s*-->\s*(?P<end>{TIMESTAMP_RE})\s*(?:\t+|\s{{2,}})(?P<text>.*)$"
)

SENTENCE_END_RE = re.compile(r"[.!?…:][\]\)\"'”’]*\s*$")
PAUSE_END_RE = re.compile(r"[,;:!?…\.][\]\)\"'”’]*\s*$")
ELLIPSIS_RE = re.compile(r"\.\.\.+")
WHITESPACE_RE = re.compile(r"\s+")

KNOWN_PT_BR_VOICES = (
    "pt_BR-faber-medium",
    "pt_BR-cadu-medium",
    "pt_BR-jeff-medium",
    "pt_BR-edresson-low",
)


@dataclass(order=True)
class Cue:
    start: float
    end: float
    text: str
    index: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Group:
    gid: int
    cues: List[Cue]
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class Attempt:
    length_scale: float
    duration: float
    raw_wav: Path


@dataclass
class GroupReport:
    gid: int
    cue_indices: list[int]
    start: float
    end: float
    target_duration: float
    text: str
    chosen_length_scale: float
    raw_duration: float
    speedup_applied: float
    trimmed_fallback: bool
    sample_rate: int


class CommandError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def debug(msg: str, *, enabled: bool) -> None:
    if enabled:
        print(msg, file=sys.stderr)



def run(
    cmd: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            check=check,
            text=True,
            capture_output=capture_output,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        joined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
        raise CommandError(
            f"Command failed ({exc.returncode}): {' '.join(cmd)}\n{joined}".strip()
        ) from exc
    except FileNotFoundError as exc:
        raise CommandError(f"Command not found: {cmd[0]}") from exc



def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(
            f"Required executable not found in PATH: {name}\n"
            f"Install it first and re-run."
        )



def parse_timestamp(ts: str) -> float:
    ts = ts.replace(",", ".")
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(".")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0



def format_seconds(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hh, rem = divmod(milliseconds, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, ms = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"



def text_excerpt(text: str, width: int = 72) -> str:
    return textwrap.shorten(text, width=width, placeholder="…")


# ---------------------------------------------------------------------------
# Parsing and text prep
# ---------------------------------------------------------------------------

def parse_srt(content: str) -> List[Cue]:
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    cues: List[Cue] = []
    next_index = 1

    for block in blocks:
        lines = [line.rstrip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue

        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0].strip()):
            timestamp_line = lines[1].strip()
            text_lines = lines[2:]
            cue_index = int(lines[0].strip())
        else:
            timestamp_line = lines[0].strip()
            text_lines = lines[1:]
            cue_index = next_index

        if "-->" not in timestamp_line:
            continue

        start_str, end_str = [part.strip() for part in timestamp_line.split("-->", 1)]
        text = " ".join(line.strip() for line in text_lines).strip()
        cues.append(
            Cue(
                start=parse_timestamp(start_str),
                end=parse_timestamp(end_str),
                text=text,
                index=cue_index,
            )
        )
        next_index += 1

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



def normalize_pt_br_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    text = ELLIPSIS_RE.sub("…", text)
    text = text.replace("&", " e ")
    text = re.sub(r"(?<=\d)[\.,](?=\d)", " vírgula ", text)
    text = text.replace("front-end", "front end")
    text = text.replace("Front-end", "Front end")
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text



def parse_timestamped_file(path: Path, *, normalize_text: bool = True) -> List[Cue]:
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".srt":
        cues = parse_srt(content)
    else:
        try:
            cues = parse_segments_txt(content)
        except ValueError:
            cues = parse_srt(content)

    cleaned: List[Cue] = []
    for cue in cues:
        text = WHITESPACE_RE.sub(" ", cue.text).strip()
        if normalize_text:
            text = normalize_pt_br_text(text)
        cleaned.append(Cue(start=cue.start, end=cue.end, text=text, index=cue.index))

    cleaned = [c for c in cleaned if c.duration > 0 and c.text]
    cleaned.sort(key=lambda c: (c.start, c.end, c.index))
    if not cleaned:
        raise SystemExit(f"No valid cues found in: {path}")
    return cleaned



def ends_sentence(text: str) -> bool:
    return bool(SENTENCE_END_RE.search(text.strip()))



def ends_pause(text: str) -> bool:
    return bool(PAUSE_END_RE.search(text.strip()))



def group_cues(
    cues: Sequence[Cue],
    *,
    max_group_gap: float,
    max_group_duration: float,
    max_group_chars: int,
    sentence_break_gap: float,
    min_sentence_group_duration: float,
) -> List[Group]:
    if not cues:
        return []

    grouped: List[List[Cue]] = []
    current: List[Cue] = [cues[0]]

    for cue in cues[1:]:
        prev = current[-1]
        gap = max(0.0, cue.start - prev.end)
        prospective_duration = cue.end - current[0].start
        prospective_chars = sum(len(c.text) for c in current) + len(cue.text) + max(0, len(current))
        should_break = False

        if gap > max_group_gap:
            should_break = True
        elif prospective_duration > max_group_duration:
            should_break = True
        elif prospective_chars > max_group_chars:
            should_break = True
        elif ends_sentence(prev.text) and (
            gap >= sentence_break_gap or prospective_duration >= min_sentence_group_duration
        ):
            should_break = True

        if should_break:
            grouped.append(current)
            current = [cue]
        else:
            current.append(cue)

    if current:
        grouped.append(current)

    groups: List[Group] = []
    for gid, chunk in enumerate(grouped, start=1):
        pieces = [chunk[0].text.strip()]
        for prev, cur in zip(chunk, chunk[1:]):
            gap = max(0.0, cur.start - prev.end)
            if ends_pause(prev.text):
                sep = " "
            elif gap >= 0.45:
                sep = ", "
            else:
                sep = " "
            pieces.append(sep)
            pieces.append(cur.text.strip())

        text = "".join(pieces)
        text = WHITESPACE_RE.sub(" ", text).strip()
        groups.append(
            Group(
                gid=gid,
                cues=list(chunk),
                text=text,
                start=chunk[0].start,
                end=chunk[-1].end,
            )
        )

    return groups


# ---------------------------------------------------------------------------
# Piper integration
# ---------------------------------------------------------------------------

def python_exe_from_arg(python_exe: Optional[str]) -> str:
    return python_exe or sys.executable



def ensure_piper_available(python_exe: str) -> None:
    try:
        run([python_exe, "-m", "piper", "--help"], capture_output=True)
    except CommandError as exc:
        raise SystemExit(
            "piper-tts is not installed in this Python environment.\n"
            "Install it with:\n"
            f"  {python_exe} -m pip install -U piper-tts\n\n"
            f"Details: {exc}"
        ) from exc



def locate_voice_files(data_dir: Path, voice: str) -> tuple[Optional[Path], Optional[Path]]:
    model_name = f"{voice}.onnx"
    config_name = f"{voice}.onnx.json"

    direct_model = data_dir / model_name
    direct_config = data_dir / config_name
    if direct_model.exists() and direct_config.exists():
        return direct_model, direct_config

    models = list(data_dir.rglob(model_name))
    configs = list(data_dir.rglob(config_name))
    model = models[0] if models else None
    config = configs[0] if configs else None
    return model, config



def ensure_voice_downloaded(
    python_exe: str,
    voice: str,
    data_dir: Path,
    *,
    verbose: bool,
) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    model, config = locate_voice_files(data_dir, voice)
    if model and config:
        return model, config

    debug(f"Downloading voice {voice} to {data_dir}", enabled=verbose)
    run(
        [python_exe, "-m", "piper.download_voices", voice, "--data-dir", str(data_dir)],
        capture_output=not verbose,
    )

    model, config = locate_voice_files(data_dir, voice)
    if not model or not config:
        raise SystemExit(
            f"Voice download finished but files were not found for {voice} in {data_dir}."
        )
    return model, config



def load_voice_defaults(config_path: Path) -> dict[str, float | int]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    inference = payload.get("inference", {})
    audio = payload.get("audio", {})
    return {
        "sample_rate": int(audio.get("sample_rate", 22050)),
        "length_scale": float(inference.get("length_scale", 1.0)),
        "noise_scale": float(inference.get("noise_scale", 0.667)),
        "noise_w": float(inference.get("noise_w", 0.8)),
    }



def piper_flag_variants() -> list[dict[str, str]]:
    return [
        {
            "length_scale": "--length-scale",
            "noise_scale": "--noise-scale",
            "noise_w": "--noise-w",
            "sentence_silence": "--sentence-silence",
        },
        {
            "length_scale": "--length_scale",
            "noise_scale": "--noise_scale",
            "noise_w": "--noise_w",
            "sentence_silence": "--sentence_silence",
        },
    ]



def synthesize_with_piper(
    python_exe: str,
    model_path: Path,
    config_path: Path,
    text: str,
    output_wav: Path,
    *,
    length_scale: float,
    noise_scale: float,
    noise_w: float,
    sentence_silence: float,
    speaker: Optional[int],
    verbose: bool,
) -> None:
    last_error: Optional[CommandError] = None
    for flags in piper_flag_variants():
        cmd = [
            python_exe,
            "-m",
            "piper",
            "-m",
            str(model_path),
            "-c",
            str(config_path),
            "-f",
            str(output_wav),
            flags["length_scale"],
            f"{length_scale:.4f}",
            flags["noise_scale"],
            f"{noise_scale:.4f}",
            flags["noise_w"],
            f"{noise_w:.4f}",
            flags["sentence_silence"],
            f"{sentence_silence:.3f}",
        ]
        if speaker is not None:
            cmd.extend(["--speaker", str(speaker)])
        cmd.extend(["--", text])

        try:
            run(cmd, capture_output=not verbose)
            return
        except CommandError as exc:
            last_error = exc
            continue

    raise SystemExit(
        "Could not synthesize with Piper.\n"
        f"Last error:\n{last_error}"
    )


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

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
    silence_chunk = b"\x00\x00" * chunk_frames
    remaining = frames
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        while remaining > 0:
            to_write = min(remaining, chunk_frames)
            wav_file.writeframes(silence_chunk[: to_write * 2])
            remaining -= to_write



def ffmpeg_filter_to_slot(
    input_wav: Path,
    output_wav: Path,
    *,
    slot_duration: float,
    sample_rate: int,
    speedup: float,
    trimmed_fallback: bool,
) -> None:
    filters: list[str] = []
    if not math.isclose(speedup, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        filters.append(build_atempo_chain(speedup))
    filters.append(f"apad=whole_dur={slot_duration:.6f}")
    filters.append(f"atrim=0:{slot_duration:.6f}")
    if trimmed_fallback and slot_duration > 0.12:
        fade_start = max(0.0, slot_duration - 0.06)
        filters.append(f"afade=t=out:st={fade_start:.6f}:d=0.06")
    filters.append("asetpts=N/SR/TB")

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_wav),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-af",
            ",".join(filters),
            str(output_wav),
        ],
        capture_output=True,
    )



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
    suffix = output_path.suffix.lower()
    if suffix == ".wav":
        shutil.copyfile(input_wav, output_path)
        return

    cmd = ["ffmpeg", "-y", "-i", str(input_wav)]
    if suffix == ".mp3":
        cmd.extend(["-q:a", "2"])
    elif suffix in {".m4a", ".aac"}:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        raise SystemExit("Output extension must be .wav, .mp3, .m4a, or .aac")
    cmd.append(str(output_path))
    run(cmd, capture_output=True)


# ---------------------------------------------------------------------------
# Synthesis strategy
# ---------------------------------------------------------------------------

def choose_scale_candidates(
    *,
    base_scale: float,
    target_duration: float,
    min_scale: float,
    max_scale: float,
) -> list[float]:
    # Order matters. We try natural pacing first, then progressively faster.
    raw = [
        base_scale,
        base_scale * 0.94,
        base_scale * 0.88,
        base_scale * 0.82,
        base_scale * 0.76,
        base_scale * 1.06,
    ]
    if target_duration < 2.0:
        raw.extend([base_scale * 0.90, base_scale * 0.84])
    clipped = [min(max_scale, max(min_scale, s)) for s in raw]

    seen: list[float] = []
    for value in clipped:
        rounded = round(value, 4)
        if rounded not in seen:
            seen.append(rounded)
    return seen



def select_best_attempt(
    attempts: Sequence[Attempt],
    *,
    target_duration: float,
    max_atempo: float,
) -> Attempt:
    viable = [a for a in attempts if a.duration <= target_duration * max_atempo]
    if viable:
        def score(a: Attempt) -> tuple[float, float]:
            # Prefer slightly under target instead of over target.
            delta = a.duration - target_duration
            penalty = abs(delta) + (0.15 if delta > 0 else 0.0)
            return (penalty, a.duration)

        return min(viable, key=score)

    return min(attempts, key=lambda a: a.duration)



def fit_attempt_to_slot(
    attempt: Attempt,
    output_wav: Path,
    *,
    target_duration: float,
    sample_rate: int,
    max_atempo: float,
) -> tuple[float, bool]:
    raw_duration = attempt.duration
    if raw_duration <= target_duration + 1e-6:
        ffmpeg_filter_to_slot(
            attempt.raw_wav,
            output_wav,
            slot_duration=target_duration,
            sample_rate=sample_rate,
            speedup=1.0,
            trimmed_fallback=False,
        )
        return 1.0, False

    exact_speedup = raw_duration / target_duration
    if exact_speedup <= max_atempo:
        ffmpeg_filter_to_slot(
            attempt.raw_wav,
            output_wav,
            slot_duration=target_duration,
            sample_rate=sample_rate,
            speedup=exact_speedup,
            trimmed_fallback=False,
        )
        return exact_speedup, False

    # Final fallback: apply the allowed max speedup and trim the tail.
    ffmpeg_filter_to_slot(
        attempt.raw_wav,
        output_wav,
        slot_duration=target_duration,
        sample_rate=sample_rate,
        speedup=max_atempo,
        trimmed_fallback=True,
    )
    return max_atempo, True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_output_default(input_path: Path) -> Path:
    return input_path.with_name(input_path.stem + ".synced.mp3")



def render_groups(
    *,
    python_exe: str,
    groups: Sequence[Group],
    model_path: Path,
    config_path: Path,
    sample_rate: int,
    voice_defaults: dict[str, float | int],
    args: argparse.Namespace,
    work_dir: Path,
) -> tuple[list[Path], list[GroupReport]]:
    wav_parts: list[Path] = []
    reports: list[GroupReport] = []
    cursor = 0.0

    base_length_scale = float(
        args.length_scale if args.length_scale is not None else voice_defaults["length_scale"]
    )
    noise_scale = float(
        args.noise_scale if args.noise_scale is not None else voice_defaults["noise_scale"]
    )
    noise_w = float(args.noise_w if args.noise_w is not None else voice_defaults["noise_w"])

    for group in groups:
        gap = max(0.0, group.start - cursor)
        if gap > 1e-6:
            silence_wav = work_dir / f"{group.gid:04d}_gap.wav"
            create_silence_wav(silence_wav, gap, sample_rate)
            wav_parts.append(silence_wav)
            cursor += gap

        slot_duration = group.duration
        raw_attempts: list[Attempt] = []
        candidate_scales = choose_scale_candidates(
            base_scale=base_length_scale,
            target_duration=slot_duration,
            min_scale=args.min_length_scale,
            max_scale=args.max_length_scale,
        )

        for attempt_num, length_scale in enumerate(candidate_scales, start=1):
            raw_wav = work_dir / f"{group.gid:04d}_raw_{attempt_num:02d}.wav"
            synthesize_with_piper(
                python_exe,
                model_path,
                config_path,
                group.text,
                raw_wav,
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w=noise_w,
                sentence_silence=args.sentence_silence,
                speaker=args.speaker,
                verbose=args.verbose,
            )
            duration = ffprobe_duration_seconds(raw_wav)
            raw_attempts.append(Attempt(length_scale=length_scale, duration=duration, raw_wav=raw_wav))

            debug(
                f"[group {group.gid:03d}] try scale={length_scale:.3f} raw={duration:.3f}s target={slot_duration:.3f}s | {text_excerpt(group.text)}",
                enabled=args.verbose,
            )

            if duration <= slot_duration * args.max_atempo:
                # Good enough; later selection still prefers the closest fit.
                pass

        best = select_best_attempt(raw_attempts, target_duration=slot_duration, max_atempo=args.max_atempo)
        fitted_wav = work_dir / f"{group.gid:04d}_slot.wav"
        speedup, trimmed = fit_attempt_to_slot(
            best,
            fitted_wav,
            target_duration=slot_duration,
            sample_rate=sample_rate,
            max_atempo=args.max_atempo,
        )
        wav_parts.append(fitted_wav)
        cursor = group.end

        reports.append(
            GroupReport(
                gid=group.gid,
                cue_indices=[cue.index for cue in group.cues],
                start=group.start,
                end=group.end,
                target_duration=slot_duration,
                text=group.text,
                chosen_length_scale=best.length_scale,
                raw_duration=best.duration,
                speedup_applied=speedup,
                trimmed_fallback=trimmed,
                sample_rate=sample_rate,
            )
        )

        print(
            f"[{group.gid:03d}/{len(groups):03d}] "
            f"{format_seconds(group.start)} → {format_seconds(group.end)} | "
            f"slot={slot_duration:5.2f}s raw={best.duration:5.2f}s "
            f"scale={best.length_scale:0.2f} tempo={speedup:0.2f}" +
            (" TRIM" if trimmed else "") +
            f" | {text_excerpt(group.text)}"
        )

    return wav_parts, reports



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synchronized PT-BR narration from SRT/segments using local Piper TTS."
    )
    parser.add_argument("--input", required=True, help="Input .srt or .segments.txt file")
    parser.add_argument("--output", default=None, help="Output audio file (.mp3/.wav/.m4a/.aac)")
    parser.add_argument(
        "--voice",
        default="pt_BR-faber-medium",
        help="Piper voice model name (default: pt_BR-faber-medium)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(Path.home() / ".cache" / "piper-voices"),
        help="Directory where Piper voices are stored/downloaded",
    )
    parser.add_argument(
        "--python-exe",
        default=None,
        help="Python executable to use for `python -m piper` calls (default: current Python)",
    )
    parser.add_argument(
        "--speaker",
        type=int,
        default=None,
        help="Optional speaker ID for multi-speaker voices",
    )
    parser.add_argument(
        "--sentence-silence",
        type=float,
        default=0.04,
        help="Small extra sentence pause passed to Piper (default: 0.04)",
    )
    parser.add_argument(
        "--length-scale",
        type=float,
        default=None,
        help="Override Piper length scale (smaller=faster, larger=slower)",
    )
    parser.add_argument(
        "--noise-scale",
        type=float,
        default=None,
        help="Override Piper noise scale",
    )
    parser.add_argument(
        "--noise-w",
        type=float,
        default=None,
        help="Override Piper noise_w",
    )
    parser.add_argument(
        "--min-length-scale",
        type=float,
        default=0.72,
        help="Fastest Piper length scale candidate allowed (default: 0.72)",
    )
    parser.add_argument(
        "--max-length-scale",
        type=float,
        default=1.10,
        help="Slowest Piper length scale candidate allowed (default: 1.10)",
    )
    parser.add_argument(
        "--max-atempo",
        type=float,
        default=1.12,
        help="Maximum ffmpeg tempo correction after TTS (default: 1.12)",
    )
    parser.add_argument(
        "--max-group-gap",
        type=float,
        default=0.35,
        help="Merge adjacent subtitle cues when gap is below this (default: 0.35)",
    )
    parser.add_argument(
        "--max-group-duration",
        type=float,
        default=12.0,
        help="Maximum target duration for a merged synthesis group (default: 12.0)",
    )
    parser.add_argument(
        "--max-group-chars",
        type=int,
        default=300,
        help="Maximum characters per merged synthesis group (default: 300)",
    )
    parser.add_argument(
        "--sentence-break-gap",
        type=float,
        default=0.18,
        help="If a cue ends a sentence and the next gap is at least this value, start a new group (default: 0.18)",
    )
    parser.add_argument(
        "--min-sentence-group-duration",
        type=float,
        default=3.2,
        help="Once a sentence-ending group reaches this duration, stop merging further cues (default: 3.2)",
    )
    parser.add_argument(
        "--no-text-normalization",
        action="store_true",
        help="Disable light PT-BR text normalization",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep intermediate files for inspection",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="Optional JSON report path with per-group diagnostics",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse/group only; do not synthesize audio",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose debug logs",
    )
    parser.add_argument(
        "--show-known-voices",
        action="store_true",
        help="Print a few known PT-BR voice names and exit",
    )
    return parser.parse_args()



def main() -> None:
    args = parse_args()

    if args.show_known_voices:
        print("Known PT-BR voices to try:")
        for name in KNOWN_PT_BR_VOICES:
            print(f"  - {name}")
        return

    require_binary("ffmpeg")
    require_binary("ffprobe")

    python_exe = python_exe_from_arg(args.python_exe)
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else build_output_default(input_path)
    data_dir = Path(args.data_dir).expanduser().resolve()

    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    cues = parse_timestamped_file(input_path, normalize_text=not args.no_text_normalization)
    groups = group_cues(
        cues,
        max_group_gap=args.max_group_gap,
        max_group_duration=args.max_group_duration,
        max_group_chars=args.max_group_chars,
        sentence_break_gap=args.sentence_break_gap,
        min_sentence_group_duration=args.min_sentence_group_duration,
    )

    print(f"Parsed {len(cues)} cues → {len(groups)} synthesis groups")
    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print(f"Voice : {args.voice}")

    if args.dry_run:
        total_duration = groups[-1].end - groups[0].start
        print(f"Total target duration: {total_duration:.2f}s")
        for group in groups[: min(12, len(groups))]:
            print(
                f"  group {group.gid:03d} | {format_seconds(group.start)} → {format_seconds(group.end)} | "
                f"{len(group.cues)} cues | {group.char_count:3d} chars | {text_excerpt(group.text)}"
            )
        if len(groups) > 12:
            print(f"  ... {len(groups) - 12} more groups omitted")
        return

    ensure_piper_available(python_exe)
    model_path, config_path = ensure_voice_downloaded(
        python_exe,
        args.voice,
        data_dir,
        verbose=args.verbose,
    )
    voice_defaults = load_voice_defaults(config_path)
    sample_rate = int(voice_defaults["sample_rate"])

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.keep_temp:
        work_dir = output_path.with_suffix(output_path.suffix + ".parts")
        work_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="sync_ptbr_piper_"))
        cleanup = True

    try:
        wav_parts, reports = render_groups(
            python_exe=python_exe,
            groups=groups,
            model_path=model_path,
            config_path=config_path,
            sample_rate=sample_rate,
            voice_defaults=voice_defaults,
            args=args,
            work_dir=work_dir,
        )
        if not wav_parts:
            raise SystemExit("No audio parts were generated.")

        final_wav = work_dir / "final_synced.wav"
        append_wavs(wav_parts, final_wav, sample_rate)
        transcode_output(final_wav, output_path)

        if args.report_json:
            report_path = Path(args.report_json).expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps([asdict(report) for report in reports], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Report: {report_path}")

        print(f"Done: {output_path}")
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
