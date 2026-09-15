# Third-party notices / 第三方组件说明

The [MIT License](LICENSE) applies to original CADScene Workbench code and
documentation. It does not replace third-party copyright notices or licenses.
项目自身采用 MIT；第三方组件、模型、原生程序、字体和媒体素材并不因此改为 MIT。

## Code shipped in the source tree and Python wheel

| Component | Location | Notice |
| --- | --- | --- |
| Three.js r128 and its controls/adaptations | `apps/web_camera_viewer/vendor/` | MIT, Copyright © 2010–2021 three.js authors. Full text: [threejs-MIT.txt](third_party_licenses/threejs-MIT.txt); [upstream r128 license](https://github.com/mrdoob/three.js/blob/r128/LICENSE). Existing source headers are retained. |

The Python wheel contains application code, browser resources and pipeline
configuration. It does **not** contain a Python interpreter, FFmpeg executable,
COLMAP executable, OpenGV executable or user projects. Installing dependencies
with pip obtains separate distributions under their respective terms.

## Separately installed runtime dependencies

The following identifies major upstream projects, not a blanket license for
every native library in their wheels. Consult the exact installed distribution's
`*.dist-info/licenses/`, `LICENSE*`, `COPYING*` and upstream notices.

| Component | Upstream / principal project license |
| --- | --- |
| NumPy / SciPy | [NumPy](https://github.com/numpy/numpy/blob/main/LICENSE.txt) / [SciPy](https://github.com/scipy/scipy/blob/main/LICENSE.txt), BSD-family project licenses; bundled numerical libraries have additional notices. |
| PyYAML / ezdxf / pyproj | [PyYAML](https://github.com/yaml/pyyaml/blob/main/LICENSE), [ezdxf](https://github.com/mozman/ezdxf/blob/master/LICENSE), [pyproj](https://github.com/pyproj4/pyproj/blob/main/LICENSE), MIT project licenses; PROJ and its database have their own distribution notices. |
| Pillow | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE), HPND-style license with additional third-party notices. |
| OpenCV Python | [OpenCV Python licensing](https://github.com/opencv/opencv-python#licensing), including separate wrapper, OpenCV and bundled codec notices. |
| imageio-ffmpeg | [BSD-2-Clause Python wrapper](https://github.com/imageio/imageio-ffmpeg/blob/main/LICENSE); the FFmpeg binary is separately licensed, not BSD because the wrapper is BSD. |
| pycolmap / COLMAP | [COLMAP license](https://github.com/colmap/colmap/blob/4.1.0/COPYING.txt), BSD-3-Clause for COLMAP itself; dependencies are separately licensed. |
| psutil | [psutil license](https://github.com/giampaolo/psutil/blob/master/LICENSE), BSD-3-Clause. |
| PyAV | [PyAV licensing](https://github.com/PyAV-Org/PyAV/blob/main/LICENSE.txt); linked FFmpeg libraries are separately licensed. |

## Portable Windows distribution: separate release gate

A portable bundle is a larger aggregate than the Python wheel. Its interpreter,
native libraries, codecs, GPU runtime, fonts and external backend must retain
their notices and satisfy their applicable redistribution requirements. Merely
including this file is **not** evidence that those requirements are complete.

The locally tested 0.1.5 runtime inventory includes:

- Conda FFmpeg **8.1.2**, build `gpl_he504192_904`, with `--enable-gpl`,
  `--enable-version3`, `libx264` and `libx265` in its reported configuration.
- imageio-ffmpeg's **7.1 essentials** executable from gyan.dev, also built with
  `--enable-gpl --enable-version3` and statically included third-party libraries.
- COLMAP **4.1.0 CUDA**, pycolmap **4.1.1**, the separately supplied pinned
  pure-rotation backend, OpenGV and LLVM-MinGW native runtime DLLs.

[FFmpeg's official licensing guidance](https://ffmpeg.org/legal.html) explains
that enabled GPL components affect the FFmpeg build and that corresponding
source/build materials must match the distributed binaries. The two versions
above cannot be described as MIT or simply as the Python wrapper's license.
COLMAP likewise explicitly distinguishes its own license from dependency terms.

Before public binary publication, preserve version-specific notices and record
the exact binary hashes, source versions, patches, build instructions and any
required corresponding source delivery. Also establish redistribution permission
for external backends. The owner has authorized the original code of the pinned
`pure_rotation_camera_poc` backend under MIT; this does not relicense OpenGV or
other dependencies in that separate repository. Its source snapshot and distinct
notices must accompany the applicable distribution. The original backend license
text is [pure_rotation_backend-MIT.txt](third_party_licenses/pure_rotation_backend-MIT.txt);
OpenGV's retained text is [opengv-BSD-3-Clause.txt](third_party_licenses/opengv-BSD-3-Clause.txt).
Until all materials are verified, the local
portable ZIP is **not approved for public release**. See the release page for
which artifacts have actually been published; do not infer availability from a
local file or a build command.

The source repository records outstanding checks in
`packaging/windows/PUBLIC_RELEASE_CHECKLIST.md`.

## User inputs

Uploaded video, CAD, telemetry, terrain and annotations remain outside the source
license grant. Do not include project storage or sample customer assets in a
public source archive or release without the relevant rights and authorization.
