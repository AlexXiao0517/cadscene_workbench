from __future__ import annotations

import pytest

from cadscene.srt import parser


class HostileStream:
    def __init__(self, chunks: int) -> None:
        self.chunks = chunks
        self.reads = 0

    def read(self, size: int) -> bytes:
        assert size == parser._READ_CHUNK_SIZE
        if self.reads >= self.chunks:
            return b""
        self.reads += 1
        return b"x" * size


def test_parse_records_discards_oversized_unterminated_line_without_retaining_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = HostileStream(20)
    seen_block_lengths: list[int] = []
    monkeypatch.setattr(parser, "_parse_record_block", lambda block: seen_block_lengths.append(len(block)))

    with pytest.warns(RuntimeWarning, match="unterminated SRT line"):
        assert parser._parse_records(stream) == []
    assert stream.reads == 20
    assert max(seen_block_lengths, default=0) <= parser._MAX_PENDING_CHARS


def test_parse_records_discards_oversized_unseparated_block_without_retaining_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = HostileStream(20)
    stream.read = lambda size: (  # type: ignore[method-assign]
        (b"metadata\n" * (size // len(b"metadata\n")))
        if stream.reads < stream.chunks and not setattr(stream, "reads", stream.reads + 1)
        else b""
    )
    seen_block_lengths: list[int] = []
    monkeypatch.setattr(parser, "_parse_record_block", lambda block: seen_block_lengths.append(len(block)))

    with pytest.warns(RuntimeWarning, match="SRT block"):
        assert parser._parse_records(stream) == []
    assert stream.reads == 20
    assert max(seen_block_lengths, default=0) <= parser._MAX_BLOCK_CHARS


def test_parser_keeps_relative_and_absolute_altitude_distinct() -> None:
    record = parser._parse_record_block(
        "1\n00:00:00,000 --> 00:00:00,100\n"
        "[latitude: 30.0] [longitude: 120.0] [rel_alt: 12.5] [abs_alt: 86.0]\n"
    )

    assert record is not None
    assert record.rel_alt == 12.5
    assert record.abs_alt == 86.0


def test_public_loader_retains_gimbal_fields(tmp_path) -> None:
    path = tmp_path / "flight.srt"
    path.write_text(
        "1\n00:00:00,000 --> 00:00:00,100\n"
        "[latitude: 30.0] [longitude: 120.0] [rel_alt: 12.5] "
        "[gb_yaw: 90.0] [gb_pitch: -45.0] [gb_roll: 2.0]\n",
        encoding="utf-8",
    )

    records = parser.load_srt_records(path)

    assert len(records) == 1
    assert records[0].gimbal_yaw == 90.0
    assert records[0].gimbal_pitch == -45.0
    assert records[0].gimbal_roll == 2.0
