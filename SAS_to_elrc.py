#!/usr/bin/env python3
"""Convert SingAlongSync (JSON) to Enhanced LRC.

Usage:
    python SAS_to_elrc.py lyrics.json              # writes lyrics.lrc
    python SAS_to_elrc.py lyrics.json out.lrc
    python SAS_to_elrc.py lyrics.json --no-end     # omit trailing end-time tag
"""
import argparse
import json
from pathlib import Path


def ts(seconds: float) -> str:
    """Seconds -> mm:ss.xx (centiseconds)."""
    cs = round(seconds * 100)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


def convert(lines: list[dict], end_tag: bool = True) -> str:
    out = []
    for entry in lines:
        start = entry["start"]
        words = entry.get("words") or []

        if not words:  # no word-level data: fall back to plain LRC line
            out.append(f"[{ts(start)}]{entry['line']}")
            continue

        parts = [f"<{ts(w['start'])}>{w['word'].strip()}" for w in words]
        text = " ".join(parts)
        if end_tag:
            text += f" <{ts(words[-1]['end'])}>"
        out.append(f"[{ts(start)}]{text}")

    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="SingAlongSync JSON file")
    ap.add_argument("output", type=Path, nargs="?", help="output .lrc (default: input name)")
    ap.add_argument("--no-end", action="store_true", help="don't add end timestamp after last word")
    args = ap.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    output = args.output or args.input.with_suffix(".lrc")
    output.write_text(convert(data, end_tag=not args.no_end), encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
