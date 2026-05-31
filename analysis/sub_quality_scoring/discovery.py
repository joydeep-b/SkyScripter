from dataclasses import dataclass
import math
from pathlib import Path
import random

from astropy.io import fits

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import dataset


@dataclass(frozen=True)
class FrameCandidate:
    path: Path
    group: str


@dataclass(frozen=True)
class CandidatePair:
    first_path: Path
    second_path: Path
    group: str


def read_candidate(sub_path: Path, input_dir: Path) -> FrameCandidate:
    with fits.open(sub_path, memmap=False) as hdul:
        header = hdul[0].header
        filter_name = str(header.get("FILTER", "UNKNOWN")).strip().upper() or "UNKNOWN"
        exposure = psq.header_float(header, ("EXPTIME", "EXPOSURE", "EXP_TIME"), default=1.0)
        if not math.isfinite(exposure) or exposure <= 0.0:
            exposure = 1.0
        width = int(header.get("NAXIS1", 0))
        height = int(header.get("NAXIS2", 0))
        if width <= 0 or height <= 0:
            image = psq.normalize_image_data(hdul[0].data)
            height, width = image.shape

    try:
        relative_parent = sub_path.parent.relative_to(input_dir)
    except ValueError:
        relative_parent = Path(sub_path.parent.name)
    parent_label = str(relative_parent) if str(relative_parent) != "." else input_dir.name
    group = psq.sanitize_label(f"{parent_label}_{filter_name}_{exposure:.8g}_{width}x{height}")
    return FrameCandidate(path=dataset.canonical_path(sub_path), group=group)


def discover_subs(input_dirs: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    sub_paths = []
    for input_dir in input_dirs:
        input_dir = dataset.canonical_path(input_dir)
        if not input_dir.exists() or not input_dir.is_dir():
            raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
        for sub_path in psq.discover_fits_files(input_dir):
            resolved = dataset.canonical_path(sub_path)
            if resolved not in seen:
                seen.add(resolved)
                sub_paths.append(resolved)
    return sorted(sub_paths)


def discover_candidates(input_dirs: list[Path]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    seen: set[Path] = set()
    for input_dir in input_dirs:
        input_dir = dataset.canonical_path(input_dir)
        if not input_dir.exists() or not input_dir.is_dir():
            raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
        for sub_path in psq.discover_fits_files(input_dir):
            resolved = dataset.canonical_path(sub_path)
            if resolved in seen:
                continue
            seen.add(resolved)
            candidate = read_candidate(sub_path, input_dir)
            grouped.setdefault(candidate.group, []).append(candidate.path)

    return {
        group: sorted(paths)
        for group, paths in sorted(grouped.items())
        if len(paths) >= 2
    }


def generate_pairs(
    grouped_paths: dict[str, list[Path]],
    *,
    mode: str,
    seed: int,
    max_pairs: int | None,
    skip_pairs: set[tuple[str, str]] | None = None,
) -> list[CandidatePair]:
    skip_pairs = skip_pairs or set()
    pairs = []
    rng = random.Random(seed)

    for group, paths in grouped_paths.items():
        sorted_paths = sorted(paths)
        if mode == "sequential":
            group_pairs = [
                CandidatePair(sorted_paths[index], sorted_paths[index + 1], group)
                for index in range(len(sorted_paths) - 1)
            ]
        elif mode == "random":
            group_pairs = [
                CandidatePair(sorted_paths[left], sorted_paths[right], group)
                for left in range(len(sorted_paths))
                for right in range(left + 1, len(sorted_paths))
            ]
            rng.shuffle(group_pairs)
        else:
            raise ValueError(f"Unknown pair mode: {mode}")

        pairs.extend(
            pair for pair in group_pairs
            if dataset.pair_key(pair.first_path, pair.second_path) not in skip_pairs
        )

    if mode == "random":
        rng.shuffle(pairs)
    if max_pairs is not None:
        pairs = pairs[:max_pairs]
    return pairs
