# Workbench Clip Preparation Performance Design

## Context

Opening a workbench for a requested source-video interval currently spends most of its time outside the actual workflow handoff. On the measured 878 MB, 345.56-second H.264 source, preparing a 57.72-second clip took about 214 seconds:

- about 139 seconds to build a decoded-frame index for the entire source;
- about 45 seconds to encode the requested interval with x264 `fast` while decoding from the beginning of the source;
- about 30 seconds to decode the completed clip again only to verify its frame count.

The DXF publication step is a file handoff and is not the bottleneck. The progress sidecar remains at zero while the first full-source decoded-frame probe blocks, so the user sees no movement during the longest phase.

## Goal

Reduce on-demand workbench clip preparation to roughly 15–20 seconds for the measured interval, while preserving the existing exact half-open PTS interval, exact frame map, atomic publication, and automatic safety fallback for videos whose packet metadata cannot prove a one-packet-per-decoded-frame relationship.

## Design

### Safe fast frame index

Add a fast metadata probe that reads the selected video stream's exact `time_base` and declared `nb_frames` with FFprobe. Combine that declaration with the existing packet PTS scan. The packet-derived index is accepted only when all of these conditions hold:

- the declared frame count exists and is a positive integer;
- packet count exactly equals the declared frame count;
- presentation PTS values are unique and strictly increasing after presentation-order sorting;
- packet durations can be converted to positive integer time-base units.

When any condition is not met, the export code calls the existing authoritative decoded-frame probe. This preserves correctness for containers without `nb_frames`, reordered or duplicated timestamps, and codecs/containers where packet count does not equal decoded-frame count.

### Exact input seek

For every clip, pass input-side `-ss` before `-copyts -i`, seeking to ten seconds before the requested absolute start. Keep the existing absolute `trim=start_pts=...:end_pts=...` filter. The pre-roll lets the decoder reconstruct inter-frame dependencies while avoiding decoding the source from time zero; the trim filter remains the exact authority for output boundaries.

### Fast output verification

After encoding, compare the output stream's declared `nb_frames` against the expected frame count from `clip_frame_map.json`. If it is unavailable, retain the existing decoded-frame verification fallback. No output is published unless the count agrees.

### Workbench encoding profile

The project-service on-demand export command explicitly selects x264 `veryfast` at CRF 18. The general CLI default remains `fast`, so existing offline/export behavior does not change. On the measured interval, `veryfast` encoded the exact 1,443 frames in about 14.2 seconds and produced a file about 3.7% larger than `fast`.

### Progress behavior

The existing progress contract remains intact. Safe sources should leave the zero-percent indexing phase in under a second, then publish frame-based FFmpeg progress during encoding. Unsafe sources may spend longer at indexing while the authoritative fallback runs, but correctness remains unchanged.

## Testing

- Unit-test declared-frame parsing, packet-index acceptance, and each safety rejection path.
- Unit-test that export prefers the fast index/count and falls back to decoded probing when metadata is unavailable.
- Unit-test command ordering and the ten-second input pre-roll while retaining absolute PTS trim.
- Unit-test that project workbench preparation passes `--preset veryfast`.
- Run focused tests, the full test suite, and a real export benchmark against the reported project clip.
- Confirm the benchmark output contains exactly 1,443 frames and the expected 57.8-second container duration before removing the benchmark artifact.

## Non-goals

- Stream-copy export is excluded because the measured source produced 12 extra frames for the requested interval.
- Reworking the viewer and downstream algorithms to reference the full source with an offset is excluded.
- The general-purpose clip-export CLI default is not changed.
