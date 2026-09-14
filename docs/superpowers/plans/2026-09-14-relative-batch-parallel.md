# Relative-height batch rendering plan

User approved parallel scheduling with identical imagery on 2026-09-14.

Goal: render the three full-pose SRT/video pairs beside their original videos.
No production renderer or global height policy changes. Reuse existing calibrated
projection, CAD loading, smoothing, relative altitude, labels and encoder settings.

1. Add temporary batch scheduler tests: actual concurrent work, bounded input,
   ordered output, failure propagation, and pixel equality to the sequential renderer.
2. Implement a bounded ordered ThreadPoolExecutor wrapper in
   `tmp/parallel_relative_render.py`; geometry rendering only is parallel. Text and
   video encoding stay sequential to avoid shared font state and frame reordering.
3. Load the same CAD once per batch, compare sequential/parallel raw pixels at
   first/middle/last frames of all three actual inputs. Measure throughput before
   replacing existing render processes. Fail closed on differences.
4. Stop only this turn's three sequential workers after verification. Keep their
   incomplete outputs in the audit directory. Render new full videos to temporary
   names, validate source frame counts, and rename only after successful encoding.
5. Full-decode all three MP4s, inspect first/middle/last frames, deliver original-folder links.

Verification: `D:/anaconda3/python.exe -m pytest tmp/test_parallel_relative_render.py -q`.
Integration: `D:/anaconda3/python.exe -u tmp/parallel_relative_render.py check` then `render`.
