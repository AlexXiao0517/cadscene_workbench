from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STRICT_DIRS = ("cadscene", "configs", "tests", "apps")
RELAXED_DIRS = ("docs", "legacy", "cadscene_workbench_refactor_plan", "refactor_plan")
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("import project", re.compile(r"(^|\s)import\s+project(\b|[.\s])")),
    ("from project", re.compile(r"(^|\s)from\s+project(\b|[.\s])")),
    ("project/", re.compile(r"(^|[\"'=\s])(\.\./)?project/")),
    ("../project", re.compile(r"\.\./project")),
    ("cadvideo.sfm_align", re.compile(r"\bcadvideo\.sfm_align\b")),
    ("from cadvideo", re.compile(r"(^|\s)from\s+cadvideo(\b|[.\s])")),
    ("import cadvideo", re.compile(r"(^|\s)import\s+cadvideo(\b|[.\s])")),
)


@dataclass(frozen=True)
class DependencyFinding:
    path: Path
    line_number: int
    matched_text: str
    line: str


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in {".py", ".toml", ".yaml", ".yml", ".json", ".md", ".js", ".html", ".css", ".txt"}


def _strict_path(root: Path, path: Path) -> bool:
    rel = path.relative_to(root)
    if not rel.parts:
        return False
    first = rel.parts[0]
    if first in RELAXED_DIRS:
        return False
    return first in STRICT_DIRS


def scan_for_forbidden_dependencies(root: str | Path) -> list[DependencyFinding]:
    base = Path(root)
    findings: list[DependencyFinding] = []
    for path in base.rglob("*"):
        if not path.is_file() or not _is_text_file(path):
            continue
        if not _strict_path(base, path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        for idx, line in enumerate(lines, start=1):
            normalized = line.replace("\\", "/")
            for label, pattern in FORBIDDEN_PATTERNS:
                if pattern.search(normalized):
                    findings.append(DependencyFinding(path=path, line_number=idx, matched_text=label, line=line.strip()))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan_for_forbidden_dependencies(root)
    if findings:
        for finding in findings:
            rel = finding.path.relative_to(root)
            print(f"{rel}:{finding.line_number}: forbidden dependency `{finding.matched_text}`: {finding.line}")
        return 1
    print("No forbidden project/cadvideo runtime dependencies found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
