#!/usr/bin/env python3
"""Normalize the public roast fixtures shipped in the official Artisan repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reference_library import parse_artisan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artisan_repo", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.artisan_repo / "src/test/sanity/data/artisan"
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("profile*.alog")):
        reference = parse_artisan(path.read_text(errors="replace"), path.name, {"level": "unknown"})
        reference["id"] = f"artisan-public-{path.stem}"
        reference["source"] = f"Official Artisan public test fixture: {path.name}"
        (args.output / f"{reference['id']}.json").write_text(json.dumps(reference, indent=2) + "\n")
        print(reference["id"], reference["name"], reference["machine"])


if __name__ == "__main__":
    main()
