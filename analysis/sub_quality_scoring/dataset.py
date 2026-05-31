from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path


DATASET_VERSION = 1
DEFAULT_DATASET_PATH = Path("sub_quality_pairs.jsonl")
WINNERS = {"left", "right", "tie", "reject"}
CLEAR_WINNERS = {"left", "right"}


@dataclass
class ComparisonRecord:
    left_path: Path
    right_path: Path
    winner: str
    left_preview: Path
    right_preview: Path
    group: str
    timestamp: str
    note: str = ""
    version: int = DATASET_VERSION

    def to_json_dict(self) -> dict:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "group": self.group,
            "left_path": str(self.left_path),
            "right_path": str(self.right_path),
            "winner": self.winner,
            "left_preview": str(self.left_preview),
            "right_preview": str(self.right_preview),
            "note": self.note,
        }

    @classmethod
    def from_json_dict(cls, row: dict) -> "ComparisonRecord":
        winner = row["winner"]
        if winner not in WINNERS:
            raise ValueError(f"Unknown comparison winner: {winner}")
        return cls(
            version=int(row.get("version", DATASET_VERSION)),
            timestamp=str(row.get("timestamp", "")),
            group=str(row.get("group", "")),
            left_path=Path(row["left_path"]),
            right_path=Path(row["right_path"]),
            winner=winner,
            left_preview=Path(row.get("left_preview", "")),
            right_preview=Path(row.get("right_preview", "")),
            note=str(row.get("note", "")),
        )


def now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve()


def pair_key(left_path: Path, right_path: Path) -> tuple[str, str]:
    paths = sorted((str(canonical_path(left_path)), str(canonical_path(right_path))))
    return paths[0], paths[1]


def read_comparisons(dataset_path: Path) -> list[ComparisonRecord]:
    if not dataset_path.exists():
        return []
    records = []
    with dataset_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(ComparisonRecord.from_json_dict(json.loads(line)))
            except Exception as exc:
                raise ValueError(f"Invalid dataset row {line_number} in {dataset_path}: {exc}") from exc
    return records


def append_comparison(dataset_path: Path, record: ComparisonRecord) -> None:
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with dataset_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_json_dict(), sort_keys=True) + "\n")


def write_comparisons(dataset_path: Path, records: list[ComparisonRecord]) -> None:
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dataset_path.with_name(f"{dataset_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_json_dict(), sort_keys=True) + "\n")
    temp_path.replace(dataset_path)


def existing_pair_keys(records: list[ComparisonRecord]) -> set[tuple[str, str]]:
    return {pair_key(record.left_path, record.right_path) for record in records}


def clear_preference_records(records: list[ComparisonRecord]) -> list[ComparisonRecord]:
    return [record for record in records if record.winner in CLEAR_WINNERS]


def filter_records_for_visualization(
    records: list[ComparisonRecord],
    *,
    winners: set[str] | None = None,
    start: int = 1,
    limit: int | None = None,
) -> list[ComparisonRecord]:
    if start < 1:
        raise ValueError("--start must be at least 1.")
    selected = [
        record for record in records
        if winners is None or record.winner in winners
    ]
    selected = selected[start - 1:]
    if limit is not None:
        selected = selected[:limit]
    return selected
