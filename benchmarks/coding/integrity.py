"""Dataset integrity: a checksum of every benchmark input.

The benchmark's credibility rests on its datasets not drifting unnoticed. This computes a
SHA-256 per dataset file (newlines normalized, so git's autocrlf cannot cause a spurious
mismatch) and stores them in ``checksums.json``. A test compares the live files against
that lock; changing a dataset therefore requires regenerating the lock — an intentional
act, recorded in the diff.

    python -m benchmarks.coding.integrity      # regenerate checksums.json after an edit
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
CHECKSUMS = DATASETS_DIR / "checksums.json"


def dataset_checksums() -> dict[str, str]:
    """SHA-256 of every dataset file, keyed by POSIX-relative path. Newline-normalized."""
    checksums: dict[str, str] = {}
    for path in sorted(DATASETS_DIR.rglob("*")):
        if not path.is_file() or path.name == CHECKSUMS.name:
            continue
        normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        checksums[path.relative_to(DATASETS_DIR).as_posix()] = hashlib.sha256(
            normalized
        ).hexdigest()
    return checksums


def write_checksums() -> Path:
    CHECKSUMS.write_text(
        json.dumps(dataset_checksums(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return CHECKSUMS


def load_checksums() -> dict[str, str]:
    return json.loads(CHECKSUMS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    path = write_checksums()
    print(f"Wrote {path} ({len(load_checksums())} files).")
