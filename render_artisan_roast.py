#!/usr/bin/env python3
"""Render an Artisan .alog as a clean confirmation graph."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import matplotlib.pyplot as plt


EVENT_NAMES = ("CHARGE", "DRY END", "FCs", "FCe", "SCs", "SCe", "DROP", "COOL")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    roast = ast.literal_eval(args.input.read_text(errors="replace"))
    times = roast["timex"]
    et = roast["temp1"]
    bt = roast["temp2"]
    charge_idx = roast.get("timeindex", [0])[0] or 0
    charge_time = times[charge_idx] if charge_idx < len(times) else 0
    minutes = [(value - charge_time) / 60 for value in times]

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(14, 8), dpi=150)
    fig.patch.set_facecolor("#12171d")
    ax.set_facecolor("#182028")
    ax.plot(minutes, et, color="#f05a70", linewidth=2.3, label="ET")
    ax.plot(minutes, bt, color="#32a8e6", linewidth=2.7, label="BT")

    colors = {
        "CHARGE": "#f0c84b",
        "FCs": "#79d98c",
        "DROP": "#ff9f43",
    }
    for name, idx in zip(EVENT_NAMES, roast.get("timeindex", [])):
        if not idx or idx >= len(times):
            continue
        x = minutes[idx]
        y = bt[idx]
        color = colors.get(name, "#b8c4cf")
        ax.axvline(x, color=color, linewidth=1.1, alpha=0.65, linestyle="--")
        ax.scatter([x], [y], s=48, color=color, edgecolor="#12171d", zorder=5)
        ax.annotate(
            f"{name}\n{x:0.1f} min · {y:0.0f}°F",
            (x, y),
            xytext=(8, 13),
            textcoords="offset points",
            color=color,
            fontsize=9,
            weight="bold",
        )

    title = roast.get("beans") or "Recorded roast #1 — bean name was blank"
    ax.set_title(title, fontsize=18, loc="left", pad=18, weight="bold")
    ax.text(
        1,
        1.02,
        f"{roast.get('roastisodate', '')} {roast.get('roasttime', '')}",
        transform=ax.transAxes,
        ha="right",
        color="#9aa8b5",
    )
    ax.set_xlabel("Minutes from CHARGE")
    ax.set_ylabel("Temperature (°F)")
    ax.set_xlim(min(minutes), max(minutes))
    ax.set_ylim(100, max(max(et), max(bt)) + 25)
    ax.grid(True, color="#81909e", alpha=0.16, linewidth=0.8)
    ax.legend(loc="lower right", frameon=False, ncol=2)
    for spine in ax.spines.values():
        spine.set_color("#44515d")
    fig.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, facecolor=fig.get_facecolor(), bbox_inches="tight")


if __name__ == "__main__":
    main()
