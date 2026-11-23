"""ControlTagFilter — WALK body-command support tests.

The filter strips [WALK:dx,dy] / [WALK_DESKTOP] from the display stream
and surfaces them via on_control (the stream loop turns them into
body-command SSE events). EMOTION/NEXT_CHAT behavior must stay intact.
"""

from __future__ import annotations

from gateway.think_filter import ControlTagFilter


def _collect():
    events = []

    def on_control(kind, dx, dy):
        events.append((kind, dx, dy))

    return events, ControlTagFilter(on_control=on_control)


def test_walk_tag_is_stripped_and_surfaced():
    events, f = _collect()
    out = f.feed("你好呀[WALK:120,-40]我想去那边看看") + f.flush()
    assert out == "你好呀我想去那边看看"
    assert events == [("walk", 120, -40)]


def test_walk_desktop_tag():
    events, f = _collect()
    out = f.feed("去另一个桌面看看[WALK_DESKTOP]")
    assert "WALK" not in out
    assert events == [("walk_desktop", None, None)]


def test_walk_tag_split_across_deltas():
    events, f = _collect()
    o1 = f.feed("走[WAL")
    o2 = f.feed("K:50,60]")
    assert "WAL" not in o1 and "WAL" not in o2
    assert events == [("walk", 50, 60)]


def test_multiple_walks():
    events, f = _collect()
    f.feed("[WALK:10,0]先走一步[WALK:0,20]再走一步[WALK_DESKTOP]")
    assert events == [
        ("walk", 10, 0),
        ("walk", 0, 20),
        ("walk_desktop", None, None),
    ]


def test_turn_tag_is_stripped_and_surfaced():
    events, f = _collect()
    out = f.feed("我看看另一边[TURN:left]那边有什么") + f.flush()
    assert out == "我看看另一边那边有什么"
    assert "TURN" not in out
    assert events == [("turn:left", None, None)]


def test_turn_tag_split_across_deltas():
    events, f = _collect()
    o1 = f.feed("转[TU")
    o2 = f.feed("RN:right]头")
    assert "TU" not in o1 and "RN" not in o2
    assert events == [("turn:right", None, None)]


def test_emotion_and_next_chat_untouched():
    events, f = _collect()
    out = f.feed("好开心[EMOTION:happy][NEXT_CHAT:300]")
    assert out == "好开心"
    assert events == []  # EMOTION/NEXT_CHAT are NOT surfaced to on_control


def test_bracket_text_not_confused_with_walk():
    events, f = _collect()
    out = f.feed("这是一个普通括号 [参见说明] 结束")
    assert out == "这是一个普通括号 [参见说明] 结束"
    assert events == []


def _chunkings(text):
    yield [text]
    for split in range(len(text) + 1):
        yield [text[:split], text[split:]]
    for width in (1, 2, 3, 7):
        yield [text[i : i + width] for i in range(0, len(text), width)]


def test_mixed_controls_follow_text_order_for_all_chunkings():
    text = (
        "前[TURN:right][WALK_DESKTOP][WALK:10,-20]中"
        "[WALK_DESKTOP][TURN:left][WALK:-3,4][WALK_DESKTOP]"
        "[EMOTION:happy][NEXT_CHAT:300]后[说明]"
    )
    expected = [
        ("turn:right", None, None),
        ("walk_desktop", None, None),
        ("walk", 10, -20),
        ("walk_desktop", None, None),
        ("turn:left", None, None),
        ("walk", -3, 4),
        ("walk_desktop", None, None),
    ]
    for chunks in _chunkings(text):
        events, f = _collect()
        out = "".join(f.feed(chunk) for chunk in chunks) + f.flush()
        assert out == "前中后[说明]", chunks
        assert events == expected, chunks
        assert f.feed("") == f.flush() == ""
        assert events == expected


def test_control_case_and_whitespace_for_all_chunkings():
    text = (
        "前[ \t TuRn \n : RiGhT \t][ "
        + " " * 32
        + "WaLk_DeSkToP \n][ wAlK \t: -12 , 34 \n]"
        "[ eMoTiOn : happy ][ NeXt_ChAt \t: 300 ]后"
    )
    for chunks in _chunkings(text):
        events, f = _collect()
        out = "".join(f.feed(chunk) for chunk in chunks) + f.flush()
        assert out == "前后", chunks
        assert events == [
            ("turn:right", None, None),
            ("walk_desktop", None, None),
            ("walk", -12, 34),
        ], chunks


def test_every_truncated_control_prefix_is_dropped():
    for tag in (
        "[WALK:123,-45]",
        "[WALK_DESKTOP]",
        "[TURN:right]",
        "[EMOTION:happy]",
        "[NEXT_CHAT:300]",
        "[ \t walk \n : -123 , 45 ]",
        "[ turn : LeFt ]",
        "[ walk_desktop \t]",
    ):
        for end in range(1, len(tag)):
            for chunks in _chunkings("前" + tag[:end]):
                events, f = _collect()
                out = "".join(f.feed(chunk) for chunk in chunks)
                assert out == "前", chunks
                assert events == []
                # A bracket alone is plain text, not an identifiable control.
                expected_tail = tag[:end] if not tag[1:end].strip() else ""
                assert f.flush() == expected_tail, chunks
                assert f.flush() == ""
                assert events == []
                assert f.feed("后[TURN:left]") + f.flush() == "后"
                assert events == [("turn:left", None, None)]


def test_plain_brackets_and_unfinished_plain_text_are_preserved():
    text = "普通[说明]、[参见[详细说明]]、[WAX]、[TUNE]、[]。末尾[备注"
    for chunks in _chunkings(text):
        events, f = _collect()
        out = "".join(f.feed(chunk) for chunk in chunks) + f.flush()
        assert out == text, chunks
        assert events == []


def test_invalid_control_values_are_stripped_without_execution():
    text = "前[WALK:no,2][TURN:up][WALK_DESKTOP:bad][EMOTION:][NEXT_CHAT:bad]后"
    for chunks in _chunkings(text):
        events, f = _collect()
        out = "".join(f.feed(chunk) for chunk in chunks) + f.flush()
        assert out == "前后", chunks
        assert events == []


def test_completed_controls_fire_immediately_not_at_flush():
    events, f = _collect()
    assert f.feed("[TURN:right][WALK_DESKTOP][WALK:") == ""
    assert events == [("turn:right", None, None), ("walk_desktop", None, None)]
    assert f.feed("12,34") == ""
    assert len(events) == 2
    assert f.feed("][WALK_DESKTOP]") == ""
    assert events == [
        ("turn:right", None, None),
        ("walk_desktop", None, None),
        ("walk", 12, 34),
        ("walk_desktop", None, None),
    ]
    assert f.flush() == ""
    assert len(events) == 4
