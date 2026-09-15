#!/usr/bin/env python3
"""Convert a signed Artisan log to a read-only companion replay fixture."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


EVENT_NAMES = ("CHARGE", "DRY_END", "FCs", "FCe", "SCs", "SCe", "DROP", "COOL")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = ast.literal_eval(args.input.read_text(errors="replace"))
    points = [
        {"elapsed": round(float(t), 3), "et": float(et), "bt": float(bt)}
        for t, et, bt in zip(source["timex"], source["temp1"], source["temp2"])
    ]
    events = []
    for name, idx in zip(EVENT_NAMES, source.get("timeindex", [])):
        if idx and idx < len(points):
            events.append({"name": name, **points[idx], "source": "Artisan marker", "confidence": 1.0})
    result = {
        "source": args.input.name,
        "date": source.get("roastisodate"),
        "time": source.get("roasttime"),
        "bean": source.get("beans") or "Reference roast #1",
        "points": points,
        "events": events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
