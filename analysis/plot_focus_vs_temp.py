#!/usr/bin/env python3

"""Plot autofocus solution (focus position) vs. temperature for each filter.

Parses Ekos .analyze log files, extracts AutofocusComplete entries, and
produces a scatter plot with a linear best-fit line per filter plus a
histogram of best-focus HFR by filter.
"""

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np

VALID_FILTERS = {"L", "R", "G", "B", "S", "H", "O"}
SOLUTION_RE = re.compile(r"Solution:\s*(\d+)")

FILTER_COLORS = {
    "L": "gray",
    "R": "red",
    "G": "green",
    "B": "blue",
    "H": "crimson",
    "S": "darkorange",
    "O": "teal",
}


@dataclass
class AutofocusResult:
    temperature: float
    focus_position: int
    hfr: Optional[float]
    filter_name: str


def _extract_solution_hfr(data_field: str, solution_pos: int) -> Optional[float]:
    """Extract the HFR at the solution position from the focus-step data.

    The data field contains groups of position|HFR|weight|outlier separated by
    '|'.  The last group is the verification step at the solution position.
    """
    tokens = data_field.split("|")
    if len(tokens) < 4:
        return None
    # The last group of 4 is the solution verification step.
    try:
        return float(tokens[-3])
    except (ValueError, IndexError):
        return None


def parse_autofocus_entries(
    log_dir: Path,
) -> Dict[str, List[AutofocusResult]]:
    """Return {filter: [AutofocusResult, ...]} from all logs."""
    data: Dict[str, List[AutofocusResult]] = defaultdict(list)

    for log_path in sorted(log_dir.glob("*.analyze")):
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("AutofocusComplete,"):
                    continue
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                try:
                    temperature = float(parts[2])
                except ValueError:
                    continue
                filt = parts[5]
                if filt not in VALID_FILTERS:
                    continue
                m = SOLUTION_RE.search(line)
                if m is None:
                    continue
                focus_pos = int(m.group(1))
                hfr = _extract_solution_hfr(parts[6], focus_pos)
                data[filt].append(AutofocusResult(
                    temperature=temperature,
                    focus_position=focus_pos,
                    hfr=hfr,
                    filter_name=filt,
                ))

    return data


def robust_polyfit(
    x: np.ndarray, y: np.ndarray, deg: int = 1, sigma: float = 2.0, max_iter: int = 10
) -> Tuple[np.ndarray, np.ndarray]:
    """Iterative sigma-clipped polynomial fit.

    Returns (coeffs, inlier_mask).
    """
    mask = np.ones(len(x), dtype=bool)
    for _ in range(max_iter):
        coeffs = np.polyfit(x[mask], y[mask], deg)
        residuals = y - np.polyval(coeffs, x)
        std = np.std(residuals[mask])
        new_mask = np.abs(residuals) < sigma * std
        if np.array_equal(new_mask, mask):
            break
        mask = new_mask
    return coeffs, mask


def plot_focus_vs_temp(ax: plt.Axes, data: Dict[str, List[AutofocusResult]]) -> None:
    """Scatter plot with robust linear fit per filter."""
    for filt in sorted(data.keys()):
        pts = data[filt]
        temps = np.array([r.temperature for r in pts])
        positions = np.array([r.focus_position for r in pts])
        color = FILTER_COLORS.get(filt, "black")

        if len(pts) >= 2:
            coeffs, inlier_mask = robust_polyfit(temps, positions)
            outlier_mask = ~inlier_mask

            ax.scatter(
                temps[inlier_mask], positions[inlier_mask],
                s=12, alpha=0.5, color=color, label=filt, zorder=2,
            )
            if outlier_mask.any():
                ax.scatter(
                    temps[outlier_mask], positions[outlier_mask],
                    s=6, alpha=0.35, color=color, marker="x", zorder=1,
                )

            t_range = np.linspace(temps.min(), temps.max(), 200)
            ax.plot(
                t_range,
                np.polyval(coeffs, t_range),
                color=color,
                linewidth=2,
                alpha=0.8,
                zorder=3,
            )
        else:
            ax.scatter(
                temps, positions,
                s=12, alpha=0.5, color=color, label=filt, zorder=2,
            )

    ax.set_ylim(5500, 6000)
    ax.set_xlabel("Temperature (°C)", fontsize=13)
    ax.set_ylabel("Focus Position (steps)", fontsize=13)
    ax.set_title("Autofocus Position vs. Temperature", fontsize=15)
    ax.legend(title="Filter", fontsize=11, title_fontsize=12)
    ax.grid(True, alpha=0.3)


def plot_hfr_histogram(ax: plt.Axes, data: Dict[str, List[AutofocusResult]]) -> None:
    """Histogram of best-focus HFR by filter."""
    bins = np.linspace(0, 3, 61)
    for filt in sorted(data.keys()):
        hfrs = [r.hfr for r in data[filt] if r.hfr is not None]
        if not hfrs:
            continue
        color = FILTER_COLORS.get(filt, "black")
        ax.hist(
            hfrs, bins=bins, alpha=0.5, color=color, label=filt, edgecolor="none",
        )

    ax.set_xlim(0, 3)
    ax.set_xlabel("HFR (pixels)", fontsize=13)
    ax.set_ylabel("Count", fontsize=13)
    ax.set_title("Best-Focus HFR Distribution by Filter", fontsize=15)
    ax.legend(title="Filter", fontsize=11, title_fontsize=12)
    ax.grid(True, alpha=0.3)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot autofocus position vs. temperature from Ekos logs."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("~/.local/share/kstars/analyze"),
        help="Directory containing .analyze log files (default: ~/.local/share/kstars/analyze).",
    )
    parser.add_argument(
        "--filters",
        type=str,
        default=None,
        help="Comma-separated list of filters to plot (e.g. L,R,G). Default: all.",
    )
    args = parser.parse_args()
    args.directory = args.directory.expanduser()

    if not args.directory.is_dir():
        parser.error(f"Not a directory: {args.directory}")
    
    print(f"Parsing autofocus data from {args.directory}")
    data = parse_autofocus_entries(args.directory)
    if not data:
        print("No autofocus data found.")
        return

    if args.filters:
        selected = {f.strip() for f in args.filters.split(",")}
        unknown = selected - VALID_FILTERS
        if unknown:
            parser.error(f"Unknown filter(s): {', '.join(sorted(unknown))}. "
                         f"Valid: {', '.join(sorted(VALID_FILTERS))}")
        data = {k: v for k, v in data.items() if k in selected}
        if not data:
            print("No data for the selected filters.")
            return

    total = sum(len(v) for v in data.values())
    print(f"Parsed {total} autofocus results across {len(data)} filters:")
    for filt in sorted(data.keys()):
        n_hfr = sum(1 for r in data[filt] if r.hfr is not None)
        print(f"  {filt}: {len(data[filt])} points ({n_hfr} with HFR)")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 7))
    plot_focus_vs_temp(ax1, data)
    plot_hfr_histogram(ax2, data)
    fig.tight_layout()
    out_path = "focus_vs_temp.png"
    fig.savefig(out_path, dpi=150)
    print(f"Plot saved to {out_path}")
    plt.show()


if __name__ == "__main__":
    main()
