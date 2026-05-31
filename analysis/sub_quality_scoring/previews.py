from pathlib import Path
import hashlib

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring import siril


def preview_cache_dir(dataset_path: Path, explicit_cache_dir: Path | None) -> Path:
    if explicit_cache_dir is not None:
        return explicit_cache_dir.expanduser().resolve()
    return dataset_path.expanduser().resolve().with_name(f"{dataset_path.stem}_previews")


def preview_path_for(sub_path: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha1(str(dataset.canonical_path(sub_path)).encode("utf-8")).hexdigest()[:16]
    safe_name = psq.sanitize_label(sub_path.stem)[:80]
    return cache_dir / f"{safe_name}_{digest}.png"


def render_preview(sub_path: Path, cache_dir: Path, siril_path: str, timeout: float) -> Path:
    output_path = preview_path_for(sub_path, cache_dir)
    if output_path.exists():
        return output_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_stem = output_path.with_suffix("")
    script = f"""requires 1.2.0
load {siril.quote(sub_path.name)}
autostretch
savepng {siril.quote(str(output_stem))}
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
