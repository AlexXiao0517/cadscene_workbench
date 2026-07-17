from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence


def ensure_dir(path: str | Path) -> Path:
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def read_json(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: str | Path, data, *, indent: int = 2) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    return out


def write_text(path: str | Path, text: str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def write_csv_utf8_sig(
    path: str | Path,
    rows: Iterable[Mapping[str, object]],
    fieldnames: Sequence[str] | None = None,
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    materialized = list(rows)
    names = list(fieldnames or (materialized[0].keys() if materialized else []))
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        for row in materialized:
            writer.writerow({name: row.get(name, "") for name in names})
    return out

