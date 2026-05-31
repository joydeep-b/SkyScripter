import argparse
import os
from pathlib import Path

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import commands
from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring import metrics


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Score astrophotography subs and evaluate sub-quality metrics.",
        formatter_class=HelpFormatter,
        epilog=(
            "Examples:\n"
            "  python analysis/sub_quality.py score markarians_calibrated/H --metric stellar_quality\n"
            "  python analysis/sub_quality.py label markarians_calibrated/H --max-pairs 20\n"
            "  python analysis/sub_quality.py visualize\n"
            "  python analysis/sub_quality.py eval --metric stellar_quality\n"
            "  python analysis/sub_quality.py compare-metrics --workers 10\n\n"
            "During labeling, press l/left for the left image, r/right for the right image, "
            "t for tie, x to reject the pair, or q to quit."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    score_parser = subparsers.add_parser(
        "score",
        help="Score every FITS sub in one or more directories.",
        formatter_class=HelpFormatter,
        description="Run a registered sub-quality metric on arbitrary FITS directories.",
    )
    score_parser.add_argument("input_dirs", nargs="+", type=Path, help="Directories containing FITS subs.")
    score_parser.add_argument(
        "--metric",
        choices=metrics.METRIC_NAMES,
        default=metrics.RECOMMENDED_METRIC,
        help="Quality metric to run.",
    )
    score_parser.add_argument("--output", type=Path, default=None, help="Optional output file path.")
    score_parser.add_argument(
        "--output-format",
        choices=("csv", "jsonl"),
        default="csv",
        help="Score output format.",
    )
    score_parser.add_argument("--siril-path", type=Path, default=None, help="Path to Siril executable.")
    score_parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Parallel worker processes for per-sub metric computation.",
    )
    score_parser.add_argument(
        "--siril-timeout",
        type=float,
        default=psq.DEFAULT_SIRIL_TIMEOUT,
        help="Timeout per Siril metric extraction in seconds.",
    )
    score_parser.set_defaults(func=commands.run_score)

    label_parser = subparsers.add_parser(
        "label",
        help="Collect pairwise visual labels.",
        formatter_class=HelpFormatter,
        description=(
            "Collect side-by-side labels for FITS subs. By default labels are appended "
            "to sub_quality_pairs.jsonl and preview PNGs are cached next to it."
        ),
    )
    label_parser.add_argument("input_dirs", nargs="+", type=Path, help="Directories containing FITS subs.")
    label_parser.add_argument(
        "--dataset",
        default=dataset.DEFAULT_DATASET_PATH,
        type=Path,
        help="JSONL label dataset path.",
    )
    label_parser.add_argument("--cache-dir", type=Path, default=None, help="Preview PNG cache directory.")
    label_parser.add_argument(
        "--pair-mode",
        choices=("sequential", "random"),
        default="random",
        help="Metric-independent pair generation mode.",
    )
    label_parser.add_argument("--max-pairs", type=int, default=None, help="Maximum pairs to label.")
    label_parser.add_argument("--seed", type=int, default=0, help="Seed for random pair/display order.")
    label_parser.add_argument("--siril-path", type=Path, default=None, help="Path to Siril executable.")
    label_parser.add_argument(
        "--siril-timeout",
        type=float,
        default=psq.DEFAULT_SIRIL_TIMEOUT,
        help="Timeout per Siril preview render in seconds.",
    )
    label_parser.add_argument("--ask-note", action="store_true", help="Prompt for an optional note per pair.")
    label_parser.set_defaults(func=commands.run_label)

    eval_parser = subparsers.add_parser(
        "eval",
        help="Evaluate one quality metric against saved labels.",
        formatter_class=HelpFormatter,
        description="Evaluate a quality metric against saved pairwise labels.",
    )
    eval_parser.add_argument(
        "--dataset",
        default=dataset.DEFAULT_DATASET_PATH,
        type=Path,
        help="JSONL label dataset path.",
    )
    eval_parser.add_argument("--siril-path", type=Path, default=None, help="Path to Siril executable.")
    eval_parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Parallel worker processes for per-sub metric computation.",
    )
    eval_parser.add_argument(
        "--metric",
        choices=metrics.METRIC_NAMES,
        default=metrics.RECOMMENDED_METRIC,
        help="Quality metric to evaluate.",
    )
    eval_parser.add_argument(
        "--siril-timeout",
        type=float,
        default=psq.DEFAULT_SIRIL_TIMEOUT,
        help="Timeout per Siril metric extraction in seconds.",
    )
    eval_parser.add_argument(
        "--epsilon",
        type=float,
        default=0.0,
        help="Score difference treated as a metric tie.",
    )
    eval_parser.add_argument("--worst", type=int, default=10, help="Number of worst violations to print.")
    eval_parser.add_argument(
        "--visualize",
        action="store_true",
        help="Show the worst metric-violating pairs one at a time with scores and dataset winner.",
    )
    eval_parser.set_defaults(func=commands.run_eval)

    compare_parser = subparsers.add_parser(
        "compare-metrics",
        help="Evaluate registered metrics and formula-sweep candidates side by side.",
        formatter_class=HelpFormatter,
        description=(
            "Compare registered metrics against the labeled pairwise dataset, then run "
            "a report-only formula sweep over star/background features."
        ),
    )
    compare_parser.add_argument(
        "--dataset",
        default=dataset.DEFAULT_DATASET_PATH,
        type=Path,
        help="JSONL label dataset path.",
    )
    compare_parser.add_argument("--siril-path", type=Path, default=None, help="Path to Siril executable.")
    compare_parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Parallel worker processes for per-sub metric computation.",
    )
    compare_parser.add_argument(
        "--siril-timeout",
        type=float,
        default=psq.DEFAULT_SIRIL_TIMEOUT,
        help="Timeout per Siril metric extraction in seconds.",
    )
    compare_parser.add_argument(
        "--epsilon",
        type=float,
        default=0.0,
        help="Score difference treated as a metric tie.",
    )
    compare_parser.add_argument("--top", type=int, default=15, help="Number of ranked metrics to print.")
    compare_parser.add_argument("--worst", type=int, default=10, help="Number of worst violations to visualize.")
    compare_parser.add_argument(
        "--no-sweep",
        dest="sweep",
        action="store_false",
        help="Disable report-only formula sweep and compare registered metrics only.",
    )
    compare_parser.add_argument(
        "--visualize-best",
        action="store_true",
        help="Visualize worst violations for the top-ranked metric or formula.",
    )
    compare_parser.set_defaults(func=commands.run_compare_metrics, sweep=True)

    visualize_parser = subparsers.add_parser(
        "visualize",
        help="Visualize saved labels.",
        formatter_class=HelpFormatter,
        description=(
            "Display labeled comparison pairs from the JSONL dataset using the saved "
            "preview PNGs. Only clear left/right preference labels are shown by default. "
            "Press space/n for the next label, q to quit, Shift+L/Shift+R to relabel, "
            "or Shift+D to delete the pair."
        ),
    )
    visualize_parser.add_argument(
        "--dataset",
        default=dataset.DEFAULT_DATASET_PATH,
        type=Path,
        help="JSONL label dataset path.",
    )
    visualize_parser.add_argument(
        "--winner",
        action="append",
        choices=sorted(dataset.CLEAR_WINNERS),
        help="Only show clear preference labels with this winner. May be passed more than once.",
    )
    visualize_parser.add_argument("--start", type=int, default=1, help="1-based label index to start from.")
    visualize_parser.add_argument("--limit", type=int, default=None, help="Maximum labels to show.")
    visualize_parser.set_defaults(func=commands.run_visualize)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    args.func(args)
