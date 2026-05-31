from pathlib import Path
import hashlib

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring import siril

# Cap the largest preview dimension. The PDF embeds previews at ~3 inches, so a
# full-resolution frame (tens of megapixels) is wasteful; this keeps preview
# files small while staying sharp at the rendered size.
PREVIEW_MAX_DIM = 1200


def preview_cache_dir(dataset_path: Path, explicit_cache_dir: Path | None) -> Path:
    if explicit_cache_dir is not None:
        return explicit_cache_dir.expanduser().resolve()
    return dataset_path.expanduser().resolve().with_name(f"{dataset_path.stem}_previews")


def preview_path_for(sub_path: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha1(str(dataset.canonical_path(sub_path)).encode("utf-8")).hexdigest()[:16]
    safe_name = psq.sanitize_label(sub_path.stem)[:80]
    return cache_dir / f"{safe_name}_{digest}.jpg"


def render_preview(sub_path: Path, cache_dir: Path, siril_path: str, timeout: float) -> Path:
    output_path = preview_path_for(sub_path, cache_dir)
    if output_path.exists():
        return output_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_stem = output_path.with_suffix("")
    # Save an 8-bit JPEG rather than PNG: Siril's savepng emits a 16-bit PNG for
    # high-precision frames, which ReportLab/PIL render as a washed-out (near
    # white) image. JPEG is always 8-bit, renders correctly, and is far smaller.
    script = f"""requires 1.2.0
setcpu 1
load {siril.quote(sub_path.name)}
autostretch
resample -maxdim={PREVIEW_MAX_DIM}
savejpg {siril.quote(str(output_stem))} 90
close
"""
    output = siril.run_siril_script(
        script,
        sub_path.parent,
        siril_path,
        timeout,
        failure_context=f"Siril preview render for {sub_path}",
    )
    if not output_path.exists():
        raise RuntimeError(f"Siril did not create expected preview: {output_path}\n{output}")
    return output_path
