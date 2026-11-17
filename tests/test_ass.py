import pytest

from subtitle.ass import (
    build_ass,
    convert_fps,
    format_ass_time,
    parse_srt_time,
    resolve_overlaps,
    strip_srt_tags,
)


def test_parse_srt_time_with_comma():
    assert parse_srt_time("00:00:01,000") == pytest.approx(1.0)


def test_parse_srt_time_with_dot():
    assert parse_srt_time("01:02:03.456") == pytest.approx(3723.456)


def test_parse_srt_time_ignores_bom():
    assert parse_srt_time("\ufeff00:00:05,250") == pytest.approx(5.25)


def test_format_ass_time_hours():
    assert format_ass_time(3723.456) == "1:02:03.46"


def test_format_ass_time_centiseconds():
    assert format_ass_time(0.999) == "0:00:01.00"


def test_convert_24_to_25_preserves_frame_count():
    result = convert_fps([{"start": 1.0, "end": 2.0}], 24, 25)
    assert result[0]["start"] == pytest.approx(0.96)
    assert result[0]["end"] == pytest.approx(1.92)


def test_convert_uses_cumulative_correction():
    result = convert_fps([{"start": 60.0, "end": 61.0}], 24, 25)
    assert result[0]["start"] == pytest.approx(57.6)


def test_overlap_moves_later_cue_to_next_layer():
    result = resolve_overlaps(
        [
            {"start": 0.0, "end": 2.0, "layer": 0},
            {"start": 1.0, "end": 3.0, "layer": 0},
        ]
    )
    assert result[1]["layer"] == 1
    assert result[1]["start"] == pytest.approx(1.0)


def test_non_overlap_keeps_layer_zero():
    result = resolve_overlaps(
        [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    )
    assert result[0]["layer"] == 0
    assert result[1]["layer"] == 0


def test_strip_italics_tag():
    assert strip_srt_tags("<i>hello</i>") == "hello"


def test_strip_nested_font_tag():
    assert strip_srt_tags('<font color="#fff"><b>hi</b></font>') == "hi"


def test_ass_contains_script_info_header():
    result = build_ass([{"start": 0.0, "end": 1.0, "text": "hi"}])
    assert "[Script Info]" in result


def test_ass_contains_play_resolution():
    result = build_ass([{"start": 0.0, "end": 1.0, "text": "hi"}])
    assert "PlayResX: 1920" in result
    assert "PlayResY: 1080" in result


def test_ass_quotes_font_family_with_space():
    result = build_ass(
        [{"start": 0.0, "end": 1.0, "text": "hi"}],
        styles=[{"name": "Default", "font": "Noto Sans CJK"}],
    )
    assert 'Style: Default,"Noto Sans CJK"' in result


def test_empty_subtitle_list_still_returns_header():
    result = build_ass([])
    assert "[Script Info]" in result
    assert "[Events]" in result
