from pathlib import Path
import re
import subprocess

from analysis import plot_sub_quality as psq


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_star_count(output: str) -> int:
    matches = re.findall(r"Found\s+([0-9]+)\s+Gaussian profile stars", output)
    if not matches:
        raise ValueError("Could not parse Siril findstar star count.")
    return max(int(match) for match in matches)


def parse_bgnoise(output: str) -> float:
    matches = re.findall(r"bgnoise:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", output)
    if not matches:
        raise ValueError("Could not parse Siril stat bgnoise.")
    return float(matches[-1])


def parse_background(output: str) -> float:
    matches = re.findall(r"Median:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", output)
    if not matches:
        raise ValueError("Could not parse Siril stat median background.")
    return float(matches[-1])


def get_siril_path(explicit_path: Path | None) -> str:
    return psq.get_siril_path(explicit_path)


def run_siril_script(
    script: str,
    working_dir: Path,
    siril_path: str,
    timeout: float,
    *,
    failure_context: str,
) -> str:
    result = subprocess.run(
        [siril_path, "-d", str(working_dir), "-s", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise RuntimeError(f"{failure_context} failed with exit code {result.returncode}:\n{output}")
    return output
