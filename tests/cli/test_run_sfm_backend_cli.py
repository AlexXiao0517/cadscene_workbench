from __future__ import annotations

import subprocess
import sys


def test_run_sfm_help_lists_backend_and_device_controls() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.run_sfm", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--backend" in result.stdout
    assert "--device" in result.stdout
    assert "--gpu-index" in result.stdout
    assert "--no-cpu-fallback" in result.stdout
    assert "--ba-global-frames-ratio" in result.stdout
    assert "--ba-global-points-ratio" in result.stdout
    assert "--ba-global-frames-freq" in result.stdout
    assert "--ba-global-points-freq" in result.stdout
    assert "--ba-global-max-num-iterations" in result.stdout
    assert "--ba-global-max-refinements" in result.stdout
