from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "apps" / "workflow_portal" / "assets"
SCALE = 4
SIZE = 128
INK = (120, 128, 140, 255)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (SIZE * SCALE, SIZE * SCALE), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _points(values: list[tuple[float, float]]) -> list[tuple[int, int]]:
    return [(round(x * SCALE), round(y * SCALE)) for x, y in values]


def _line(draw: ImageDraw.ImageDraw, values: list[tuple[float, float]], width: int = 5) -> None:
    draw.line(_points(values), fill=INK, width=width * SCALE, joint="curve")


def _save(image: Image.Image, name: str) -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    image.resize((SIZE, SIZE), Image.Resampling.LANCZOS).save(
        ASSET_DIR / name, format="PNG", optimize=True
    )


def video_upload_icon() -> None:
    image, draw = _canvas()
    cloud = [
        (42, 88), (31, 88), (22, 79), (22, 67), (22, 55), (31, 46), (43, 45),
        (49, 29), (63, 20), (80, 24), (92, 29), (99, 40), (100, 51),
        (113, 53), (121, 63), (119, 76), (117, 84), (109, 89), (99, 89),
        (86, 89),
    ]
    _line(draw, cloud)
    _line(draw, [(64, 103), (64, 54)])
    _line(draw, [(49, 69), (64, 53), (79, 69)])
    _save(image, "upload-video-gray.png")


def cad_upload_icon() -> None:
    image, draw = _canvas()
    _line(draw, [(29, 14), (82, 14), (105, 37), (105, 113), (29, 113), (29, 14)])
    _line(draw, [(82, 14), (82, 37), (105, 37)])
    _line(draw, [(67, 92), (67, 53)])
    _line(draw, [(52, 68), (67, 52), (82, 68)])
    _line(draw, [(45, 97), (89, 97)], width=4)
    _save(image, "upload-cad-gray.png")


if __name__ == "__main__":
    video_upload_icon()
    cad_upload_icon()
