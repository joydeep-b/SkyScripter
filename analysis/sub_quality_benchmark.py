#!/usr/bin/env python3

from pathlib import Path
import sys

try:
    from analysis.sub_quality_scoring.cli import main
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analysis.sub_quality_scoring.cli import main


if __name__ == "__main__":
    main()
