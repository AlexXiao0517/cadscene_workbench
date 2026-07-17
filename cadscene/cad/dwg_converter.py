from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def configured_converter() -> str | None:
    """返回显式配置的 DWG 转换命令模板。"""
    value = os.environ.get("CADSCENE_DWG_CONVERTER", "").strip()
    return value or None


def convert_dwg(source: str | Path, output: str | Path) -> Path | None:
    """使用无 shell 的命令模板转换 DWG；未配置时返回 None。"""
    template = configured_converter()
    if template is None:
        return None
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template_tokens = shlex.split(template, posix=False)
    command = [
        token.replace("{input}", str(source_path)).replace("{output}", str(output_path)).strip('"')
        for token in template_tokens
    ]
    if not any("{input}" in token for token in template_tokens):
        command.extend([str(source_path), str(output_path)])
    completed = subprocess.run(command, capture_output=True, text=True, shell=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown converter error").strip()
        raise RuntimeError(f"DWG converter failed: {detail}")
    if not output_path.exists():
        raise RuntimeError("DWG converter completed but did not create the requested DXF")
    return output_path
