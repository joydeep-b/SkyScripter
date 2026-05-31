from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "processing" / "collect_multiuser_project_subs.py"


def load_collect_module():
    spec = importlib.util.spec_from_file_location("collect_multiuser_project_subs", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_convert_xisfs_to_fits_processes_jobs_in_chunks(tmp_path: Path):
    module = load_collect_module()
    invocation_log = tmp_path / "siril_invocations.txt"
    fake_siril = tmp_path / "fake_siril.py"
    fake_siril.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path

work_dir = Path(sys.argv[sys.argv.index("-d") + 1])
links = sorted(work_dir.glob("pp_light_*.xisf"))
process_dir = work_dir / ".process"
process_dir.mkdir()
report_lines = []
for link in links:
    output = process_dir / f"{{link.stem}}.fit"
    output.write_bytes(b"converted")
    report_lines.append(f"'{{link.name}}' -> '.process/{{output.name}}'\\n")
(process_dir / "pp_light_conversion.txt").write_text("".join(report_lines), encoding="utf-8")
with Path({str(invocation_log)!r}).open("a", encoding="utf-8") as handle:
    handle.write(f"{{len(links)}}\\n")
""",
        encoding="utf-8",
    )
    os.chmod(fake_siril, 0o755)

    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    output_dir = tmp_path / "output"
    destinations = []
    conversion_jobs = []
    for index in range(5):
        source = source_dir / f"light_{index}.xisf"
        source.write_bytes(b"xisf")
        destination = output_dir / ".converted_xisf" / f"light_{index}.fit"
        destinations.append(destination)
        conversion_jobs.append((source, destination))

    module.convert_xisfs_to_fits(
        conversion_jobs=conversion_jobs,
        output_dir=output_dir,
        siril_path=str(fake_siril),
        log_path=output_dir / "xisf_conversion.log",
        batch_size=2,
    )

    assert invocation_log.read_text(encoding="utf-8").splitlines() == ["2", "2", "1"]
    assert [destination.read_bytes() for destination in destinations] == [b"converted"] * 5
    assert not module.conversion_work_dir(output_dir).exists()
