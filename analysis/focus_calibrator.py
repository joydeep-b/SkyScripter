#!/usr/bin/env python3

"""Offline CLI tool: fit per-filter linear focus-vs-temperature models,
compute filter offsets, and output calibration JSON + diagnostic plots."""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib.pyplot as plt
import numpy as np

from analysis.plot_focus_vs_temp import FILTER_COLORS, robust_polyfit


def load_focus_log(
    path: Path, min_r_squared: float
) -> Dict[str, List[Dict[str, Any]]]:
    """Read focus_log.csv, filter bad rows, return {filter: [row_dicts]}."""
    by_filter: Dict[str, List[Dict[str, Any]]] = {}
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        for raw in reader:
            if len(raw) < len(header):
                continue
            row = {header[i]: raw[i].strip() for i in range(len(header))}
            if not row.get("best_focus"):
                continue
            try:
                r_sq = float(row["r_squared"])
                if r_sq < min_r_squared:
                    continue
                row["best_focus"] = float(row["best_focus"])
                row["temperature"] = float(row["temperature"])
                row["fwhm"] = float(row["fwhm"])
                row["r_squared"] = r_sq
                row["datetime"] = row["datetime"]
            except (ValueError, KeyError):
                continue
            filt = row.get("filter", "")
            by_filter.setdefault(filt, []).append(row)
    return by_filter


def fit_filters(
    data: Dict[str, List[Dict[str, Any]]],
    reference_filter: str,
    min_points: int,
) -> Dict[str, Dict[str, Any]]:
    """Fit robust linear models per filter. Returns {filter: result_dict}."""
    results: Dict[str, Dict[str, Any]] = {}
    for filt, rows in data.items():
        if len(rows) < min_points:
            continue
        temps = np.array([r["temperature"] for r in rows])
        focuses = np.array([r["best_focus"] for r in rows])
        coeffs, inlier_mask = robust_polyfit(temps, focuses, deg=1)
        predicted = np.polyval(coeffs, temps)
        ss_res = np.sum((focuses[inlier_mask] - predicted[inlier_mask]) ** 2)
        ss_tot = np.sum(
            (focuses[inlier_mask] - np.mean(focuses[inlier_mask])) ** 2
        )
        r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        results[filt] = {
            "slope": round(float(coeffs[0]), 2),
            "intercept": round(float(coeffs[1]), 1),
            "r_squared": round(r_sq, 4),
            "n_points": int(inlier_mask.sum()),
            "coeffs": coeffs,
            "inlier_mask": inlier_mask,
            "temps": temps,
            "focuses": focuses,
        }

    ref_intercept = (
        results[reference_filter]["intercept"]
        if reference_filter in results
        else 0.0
    )
    for filt, res in results.items():
        res["offset"] = round(res["intercept"] - ref_intercept, 1)

    return results


def write_calibration_json(
    results: Dict[str, Dict[str, Any]],
    reference_filter: str,
    output_path: Path,
) -> None:
    filters_out = {}
    for filt in sorted(results):
        r = results[filt]
        filters_out[filt] = {
            "slope": r["slope"],
            "intercept": r["intercept"],
            "offset": r["offset"],
            "r_squared": r["r_squared"],
            "n_points": r["n_points"],
        }
    doc = {
        "reference_filter": reference_filter,
        "filters": filters_out,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"Calibration written to {output_path}")


def generate_plots(
    data: Dict[str, List[Dict[str, Any]]],
    results: Dict[str, Dict[str, Any]],
    reference_filter: str,
    plot_dir: Path,
    show: bool,
) -> None:
    plot_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. focus_vs_temp.png ---
    fig, ax = plt.subplots(figsize=(10, 7))
    for filt in sorted(results):
        r = results[filt]
        color = FILTER_COLORS.get(filt, "black")
        inlier = r["inlier_mask"]
        outlier = ~inlier
        label = f"{filt}  slope={r['slope']:.1f}  int={r['intercept']:.0f}"
        ax.scatter(
            r["temps"][inlier], r["focuses"][inlier],
            s=12, alpha=0.5, color=color, label=label, zorder=2,
        )
        if outlier.any():
            ax.scatter(
                r["temps"][outlier], r["focuses"][outlier],
                s=6, alpha=0.35, color=color, marker="x", zorder=1,
            )
        t_range = np.linspace(r["temps"].min(), r["temps"].max(), 200)
        ax.plot(t_range, np.polyval(r["coeffs"], t_range),
                color=color, linewidth=2, alpha=0.8, zorder=3)
    ax.set_xlabel("Temperature (°C)")
    ax.set_ylabel("Focus Position (steps)")
    ax.set_title("Focus Position vs. Temperature")
    ax.legend(title="Filter", fontsize=9, title_fontsize=10)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(plot_dir / "focus_vs_temp.png", dpi=150)

    # --- 2. filter_offsets.png ---
    fig, ax = plt.subplots(figsize=(8, 5))
    filts = sorted(results)
    offsets = [results[f]["offset"] for f in filts]
    colors = [FILTER_COLORS.get(f, "black") for f in filts]
    ax.bar(filts, offsets, color=colors, edgecolor="black", linewidth=0.5)
    ax.set_xlabel("Filter")
    ax.set_ylabel(f"Offset from {reference_filter} (steps)")
    ax.set_title("Filter Focus Offsets (at 0°C)")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(plot_dir / "filter_offsets.png", dpi=150)

    # --- 3. fwhm_histogram.png ---
    fig, ax = plt.subplots(figsize=(8, 5))
    all_fwhm = []
    for filt in sorted(data):
        fwhms = [r["fwhm"] for r in data[filt]]
        all_fwhm.extend(fwhms)
    if all_fwhm:
        bins = np.linspace(0, max(all_fwhm) * 1.1, 40)
        for filt in sorted(data):
            fwhms = [r["fwhm"] for r in data[filt]]
            if fwhms:
                ax.hist(fwhms, bins=bins, alpha=0.5,
                        color=FILTER_COLORS.get(filt, "black"),
                        label=filt, edgecolor="none", density=True)
    ax.set_xlabel("FWHM (arcsec)")
    ax.set_ylabel("Density")
    ax.set_title("Best-Focus FWHM Distribution by Filter")
    ax.legend(title="Filter")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(plot_dir / "fwhm_histogram.png", dpi=150)

    # --- 4. residuals.png ---
    fig, ax = plt.subplots(figsize=(10, 5))
    for filt in sorted(results):
        r = results[filt]
        color = FILTER_COLORS.get(filt, "black")
        predicted = np.polyval(r["coeffs"], r["temps"])
        residuals = r["focuses"] - predicted
        inlier = r["inlier_mask"]
        ax.scatter(r["temps"][inlier], residuals[inlier],
                   s=12, alpha=0.5, color=color, label=filt, zorder=2)
        if (~inlier).any():
            ax.scatter(r["temps"][~inlier], residuals[~inlier],
                       s=6, alpha=0.35, color=color, marker="x", zorder=1)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Temperature (°C)")
    ax.set_ylabel("Residual (steps)")
    ax.set_title("Focus Model Residuals")
    ax.legend(title="Filter")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(plot_dir / "residuals.png", dpi=150)

    # --- 5. focus_drift.png ---
    fig, ax = plt.subplots(figsize=(12, 5))
    for filt in sorted(data):
        rows = data[filt]
        times = []
        for r in rows:
            try:
                times.append(datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                times.append(datetime.fromisoformat(r["datetime"]))
        focuses = [r["best_focus"] for r in rows]
        color = FILTER_COLORS.get(filt, "black")
        ax.scatter(times, focuses, s=12, alpha=0.6, color=color, label=filt)
    ax.set_xlabel("Date/Time")
    ax.set_ylabel("Best Focus (steps)")
    ax.set_title("Focus Position Over Time")
    ax.legend(title="Filter")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(plot_dir / "focus_drift.png", dpi=150)

    print(f"Plots saved to {plot_dir}/")
    if show:
        plt.show()
    else:
        plt.close("all")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibrate focus-vs-temperature models per filter."
    )
    parser.add_argument("--focus-log", type=Path,
                        default=Path(".focus/focus_log.csv"))
    parser.add_argument("--output", type=Path,
                        default=Path("focus_calibration.json"))
    parser.add_argument("--reference-filter", default="L")
    parser.add_argument("--min-r-squared", type=float, default=0.5)
    parser.add_argument("--min-points", type=int, default=3)
    parser.add_argument("--plot-dir", type=Path,
                        default=Path(".focus/calibration"))
    parser.add_argument("--show", action="store_true",
                        help="Show plots interactively.")
    args = parser.parse_args()

    if not args.focus_log.exists():
        parser.error(f"Focus log not found: {args.focus_log}")

    data = load_focus_log(args.focus_log, args.min_r_squared)
    if not data:
        print("No valid data found in focus log.")
        return

    total = sum(len(v) for v in data.values())
    print(f"Loaded {total} points across {len(data)} filters: "
          f"{', '.join(f'{f}({len(v)})' for f, v in sorted(data.items()))}")

    results = fit_filters(data, args.reference_filter, args.min_points)
    if not results:
        print("No filters had enough data points for fitting.")
        return

    if args.reference_filter not in results:
        print(f"Warning: reference filter '{args.reference_filter}' has no "
              f"fit. Offsets will be absolute intercepts.")

    for filt in sorted(results):
        r = results[filt]
        print(f"  {filt}: slope={r['slope']:+.2f}  intercept={r['intercept']:.0f}  "
              f"offset={r['offset']:+.1f}  R²={r['r_squared']:.4f}  "
              f"n={r['n_points']}")

    write_calibration_json(results, args.reference_filter, args.output)
    generate_plots(data, results, args.reference_filter, args.plot_dir,
                   args.show)


if __name__ == "__main__":
    main()
