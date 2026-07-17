from __future__ import annotations

from types import SimpleNamespace

from cadscene.sfm.reconstruction import configure_pycolmap_feature_options


def test_pycolmap_gpu_options_and_device_are_explicit() -> None:
    class Options:
        use_gpu = False
        gpu_index = "-1"

    class Device:
        cuda = "cuda"
        cpu = "cpu"

    fake_pycolmap = SimpleNamespace(
        FeatureExtractionOptions=Options,
        FeatureMatchingOptions=Options,
        Device=Device,
    )

    extraction, matching, device = configure_pycolmap_feature_options(
        fake_pycolmap,
        use_gpu=True,
        gpu_index="2",
    )

    assert extraction.use_gpu is True
    assert extraction.gpu_index == "2"
    assert matching.use_gpu is True
    assert matching.gpu_index == "2"
    assert device == "cuda"
