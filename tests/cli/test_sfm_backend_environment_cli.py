from __future__ import annotations

import json
import subprocess
import sys


def test_environment_check_outputs_required_json_fields() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.check_sfm_environment", "--json"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    for key in (
        "pycolmap_available",
        "pycolmap_version",
        "colmap_cli_available",
        "colmap_path",
        "colmap_version",
        "cuda_device_available",
        "gpu_names",
        "recommended_backend",
    ):
        assert key in payload

