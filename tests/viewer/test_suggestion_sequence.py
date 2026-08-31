from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "apps" / "web_camera_viewer" / "suggestion_sequence.js"


def _evaluate(expression: str) -> object:
    script = (
        f"const sequence = require({json.dumps(str(MODULE))});"
        f"console.log(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_ordered_suggestions_are_chronological_without_mutating_input() -> None:
    result = _evaluate(
        "(() => {"
        "const source = [{frame_index: 30}, {frame_index: '10'}, {frame_index: 20}];"
        "const ordered = sequence.ordered(source);"
        "return {ordered: ordered.map(item => Number(item.frame_index)), "
        "source: source.map(item => Number(item.frame_index))};"
        "})()"
    )

    assert result == {"ordered": [10, 20, 30], "source": [30, 10, 20]}


def test_next_suggestion_starts_at_first_and_loops_after_last() -> None:
    result = _evaluate(
        "(() => {"
        "const items = [{frame_index: 30}, {frame_index: 10}, {frame_index: 20}];"
        "return ["
        "sequence.next(items, null).frame_index,"
        "sequence.next(items, 10).frame_index,"
        "sequence.next(items, 30).frame_index"
        "];"
        "})()"
    )

    assert result == [10, 20, 10]


def test_first_after_uses_next_frame_and_wraps_when_anchor_was_last() -> None:
    result = _evaluate(
        "(() => {"
        "const items = [{frame_index: 10}, {frame_index: 30}, {frame_index: 50}];"
        "return ["
        "sequence.firstAfter(items, 30).frame_index,"
        "sequence.firstAfter(items, 50).frame_index,"
        "sequence.firstAfter(items, 25).frame_index"
        "];"
        "})()"
    )

    assert result == [50, 10, 30]
