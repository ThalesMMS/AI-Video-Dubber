#!/usr/bin/env python3
"""Translate an SRT file to a target language using an OpenAI-compatible API."""

import argparse
import json
import os
import re
import sys
import urllib.request

API_BASE = os.environ.get("LLM_API_BASE", "http://localhost:8000")
API_KEY = os.environ.get("LLM_API_KEY", "apikey")
MODEL = os.environ.get("LLM_MODEL", "")
BATCH_SIZE = 15  # subtitles per request


def parse_srt(text: str) -> list[dict]:
    """Parse SRT content into a list of {index, timestamp, text} dicts."""
    blocks = re.split(r"\n\n+", text.strip())
    entries = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        index = lines[0].strip()
        timestamp = lines[1].strip()
        body = "\n".join(lines[2:])
        entries.append({"index": index, "timestamp": timestamp, "text": body})
    return entries


def build_srt(entries: list[dict]) -> str:
    """Rebuild SRT content from a list of entries."""
    parts = []
    for e in entries:
        parts.append(f"{e['index']}\n{e['timestamp']}\n{e['text']}\n")
    return "\n".join(parts) + "\n"


def _resolve_model(api_base: str, api_key: str, model: str) -> str:
    """Auto-detect model name from the API if not explicitly set."""
    if model:
        return model
    try:
        req = urllib.request.Request(
            f"{api_base}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        models = data.get("data", [])
        if models:
            name = models[0]["id"]
            print(f"Auto-detected model: {name}")
            return name
    except Exception as exc:
        print(f"Warning: could not auto-detect model ({exc}), using 'default'", file=sys.stderr)
    return "default"


def translate_batch(texts: list[str], language: str, model: str, api_base: str, api_key: str) -> list[str]:
    """Send a batch of subtitle lines to the LLM and return translations."""
    numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))

    prompt = (
        "You are a professional subtitle translator. "
        f"Translate the following English subtitle lines to {language}. "
        "Keep each line's numbering tag [N] exactly as-is. "
        "Output ONLY the translated lines, one per original, preserving the [N] tags. "
        "Do NOT add any explanation or extra text.\n\n"
        f"{numbered}"
    )

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode()

    req = urllib.request.Request(
        f"{api_base}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    reply = data["choices"][0]["message"]["content"].strip()

    # Parse [N] tagged lines from the response
    result = {}
    for line in reply.splitlines():
        m = re.match(r"\[(\d+)\]\s*(.*)", line.strip())
        if m:
            result[int(m.group(1))] = m.group(2).strip()

    # Fallback: return originals for any missing indices
    translated = []
    for i, t in enumerate(texts):
        if i in result:
            translated.append(result[i])
        else:
            print(f"  WARNING: missing translation for batch item [{i}], keeping original", file=sys.stderr)
            translated.append(t)

    return translated


def main():
    parser = argparse.ArgumentParser(description="Translate SRT via LLM")
    parser.add_argument("--input", required=True, help="Input SRT file")
    parser.add_argument("--output", required=True, help="Output SRT file")
    parser.add_argument("--language", default="Brazilian Portuguese (pt-BR)", help="Target language name (default: Brazilian Portuguese (pt-BR))")
    parser.add_argument("--api-base", default=API_BASE, help="LLM API base URL (or set LLM_API_BASE env var)")
    parser.add_argument("--api-key", default=API_KEY, help="LLM API key (or set LLM_API_KEY env var)")
    parser.add_argument("--model", default=MODEL, help="LLM model name (auto-detected if empty, or set LLM_MODEL env var)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Subtitles per API call")
    args = parser.parse_args()

    api_base = args.api_base
    api_key = args.api_key
    model = _resolve_model(api_base, api_key, args.model)

    with open(args.input, encoding="utf-8") as f:
        entries = parse_srt(f.read())

    print(f"API: {api_base}  Model: {model}")
    print(f"Parsed {len(entries)} subtitle entries from {args.input}")
    print(f"Target language: {args.language}")

    # Translate in batches
    for start in range(0, len(entries), args.batch_size):
        batch = entries[start : start + args.batch_size]
        texts = [e["text"] for e in batch]
        end = start + len(batch)
        print(f"Translating entries {start + 1}–{end} of {len(entries)}...")
        translated = translate_batch(texts, args.language, model, api_base, api_key)
        for i, t in enumerate(translated):
            entries[start + i]["text"] = t

    srt_out = build_srt(entries)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(srt_out)

    print(f"Done → {args.output}")


if __name__ == "__main__":
    main()
