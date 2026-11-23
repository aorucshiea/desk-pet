"""心声 ([INNER:…]) tests — the model's own choice of output mode.

心声 = 开口（默认）或 在心里想。When the model wraps its WHOLE reply in
[INNER:…], the content must NEVER reach the display stream (the user sees
nothing — no output, no thinking process); the captured text goes to
ephemeral memory instead (it really thought it, just didn't say it).

These tests pin the ControlTagFilter contract:
- complete tag across arbitrarily-split deltas → zero display leakage,
  on_inner fires once with the content
- unclosed tag at flush → content salvaged to on_inner, raw text suppressed
- normal replies (incl. other control tags) unaffected
"""

from __future__ import annotations

from gateway.think_filter import ControlTagFilter


def _spy():
    inner: list[str] = []
    ctrl: list[tuple] = []
    f = ControlTagFilter(
        on_control=lambda kind, dx, dy: ctrl.append((kind, dx, dy)),
        on_inner=lambda text: inner.append(text),
    )
    return inner, ctrl, f


def test_inner_whole_reply_no_leak_and_captured():
    inner, _, f = _spy()
    out = f.feed("[INNER:今天有点想说点什么，但还是算了]") + f.flush()
    assert out == ""
    assert inner == ["今天有点想说点什么，但还是算了"]


def test_inner_split_across_many_deltas():
    inner, _, f = _spy()
    text = "[INNER:窗外的雨还在下，我想起来一些以前的事，不过现在不是说这些的时候]"
    emitted = []
    # 2-char deltas force mid-tag, mid-content splits
    for i in range(0, len(text), 2):
        emitted.append(f.feed(text[i : i + 2]))
    emitted.append(f.flush())
    assert "".join(emitted) == "", "inner turn must not leak a single char"
    assert inner == ["窗外的雨还在下，我想起来一些以前的事，不过现在不是说这些的时候"]


def test_inner_unclosed_tag_salvaged_at_flush():
    inner, _, f = _spy()
    out = f.feed("[INNER:有点困了，但是还不想睡") + f.flush()
    assert out == "", "dangling [INNER:… must never leak"
    assert inner == ["有点困了，但是还不想睡"]


def test_normal_reply_passthrough_untouched():
    inner, ctrl, f = _spy()
    out = f.feed("我在呢。今天想做什么？") + f.flush()
    assert out == "我在呢。今天想做什么？"
    assert inner == []
    assert ctrl == []


def test_walk_still_works_alongside_inner_support():
    inner, ctrl, f = _spy()
    out = f.feed("走两步[WALK:30,0]") + f.flush()
    assert out == "走两步"
    assert ctrl == [("walk", 30, 0)]
    assert inner == []


def test_inner_with_leading_whitespace():
    inner, _, f = _spy()
    out = f.feed("  [INNER:嗯……]  ") + f.flush()
    assert "INNER" not in out
    assert inner == ["嗯……"]


def test_no_on_inner_callback_is_safe():
    # Default construction (no on_inner): INNER still stripped, no crash.
    f = ControlTagFilter()
    out = f.feed("[INNER:只在自己心里]") + f.flush()
    assert out == ""


def _chunkings(text):
    yield [text]
    for split in range(len(text) + 1):
        yield [text[:split], text[split:]]
    for width in (1, 2, 3, 7):
        yield [text[i : i + width] for i in range(0, len(text), width)]


def test_nested_inner_is_opaque_for_all_chunkings():
    content = (
        "第一层[WALK:1,2][TURN:left][WALK_DESKTOP]"
        "还有[说明[细节]][INNER:内层[WALK:3,4]]之后"
        "[EMOTION:happy][NEXT_CHAT:300]"
    )
    text = "[ \t iNnEr \n :  " + content + " \n]"
    for chunks in _chunkings(text):
        inner, ctrl, f = _spy()
        for chunk in chunks:
            assert f.feed(chunk) == "", chunks
        assert inner == [content], chunks
        assert ctrl == []
        assert f.flush() == f.flush() == ""
        assert inner == [content]


def test_inner_and_control_callbacks_share_text_order():
    text = (
        "[TURN:right]前[INNER:想[WALK:1,2][说明]]"
        "[WALK_DESKTOP]中[INNER:又想[INNER:深处[TURN:left]]]"
        "[WALK:7,8][WALK_DESKTOP][TURN:left]后"
    )
    for chunks in _chunkings(text):
        events = []
        f = ControlTagFilter(
            on_control=lambda kind, dx, dy: events.append((kind, dx, dy)),
            on_inner=lambda content: events.append(("inner", content)),
        )
        out = "".join(f.feed(chunk) for chunk in chunks) + f.flush()
        assert out == "前中后", chunks
        assert events == [
            ("turn:right", None, None),
            ("inner", "想[WALK:1,2][说明]"),
            ("walk_desktop", None, None),
            ("inner", "又想[INNER:深处[TURN:left]]"),
            ("walk", 7, 8),
            ("walk_desktop", None, None),
            ("turn:left", None, None),
        ], chunks


def test_unclosed_nested_inner_is_salvaged_without_losing_brackets():
    for content in (
        "想[WALK:1,2]",
        "想[TURN:left][说明[细节]]",
        "想[INNER:里面[WALK_DESKTOP]]",
        "想[说明[TURN:ri",
        "想[WALK:",
        " \n 想[说明] \t ",
    ):
        for chunks in _chunkings("[INNER:" + content):
            inner, ctrl, f = _spy()
            for chunk in chunks:
                assert f.feed(chunk) == "", chunks
                assert inner == []
                assert ctrl == []
            assert f.flush() == ""
            assert inner == [content.strip()], chunks
            assert ctrl == []
            assert f.flush() == ""
            assert inner == [content.strip()]
            assert f.feed("[WALK:1,2]公开") + f.flush() == "公开"
            assert ctrl == [("walk", 1, 2)]


def test_inner_truncated_at_every_character_is_salvaged_or_dropped():
    header = "[ \t InNeR \n :"
    content = "想[WALK:1,2][说明[细节]][INNER:深处[TURN:left]]"
    text = header + content + "]"
    for end in range(1, len(text)):
        for chunks in _chunkings(text[:end]):
            inner, ctrl, f = _spy()
            for chunk in chunks:
                assert f.feed(chunk) == "", chunks
            assert inner == []
            expected_tail = text[:end] if not text[1:end].strip() else ""
            assert f.flush() == expected_tail, chunks
            assert f.flush() == ""
            expected = [text[len(header) : end]] if end > len(header) else []
            assert inner == expected, chunks
            assert ctrl == []


def test_nested_inner_is_hidden_without_inner_callback():
    for ending in ("", "]"):
        text = "[INNER:想[WALK:1,2][TURN:left][WALK_DESKTOP][说明]" + ending
        for chunks in _chunkings(text):
            ctrl = []
            f = ControlTagFilter(
                on_control=lambda kind, dx, dy: ctrl.append((kind, dx, dy)),
            )
            for chunk in chunks:
                assert f.feed(chunk) == "", chunks
            assert f.flush() == ""
            assert ctrl == []


def test_inner_callback_waits_for_outer_closing_bracket():
    inner, ctrl, f = _spy()
    assert f.feed("前[INNER:想[INNER:更深[WALK:1,2]]") == "前"
    assert inner == ctrl == []
    assert f.feed("[TURN:left][说明]") == ""
    assert inner == ctrl == []
    assert f.feed("]后[TURN:right]") == "后"
    assert inner == ["想[INNER:更深[WALK:1,2]][TURN:left][说明]"]
    assert ctrl == [("turn:right", None, None)]
    assert f.flush() == ""
    assert len(inner) == len(ctrl) == 1


def test_empty_complete_inner_and_empty_salvage():
    inner, ctrl, f = _spy()
    assert f.feed("[INNER: \t ]") + f.flush() == ""
    assert inner == [""]
    assert f.feed("[INNER: \t ") + f.flush() == ""
    assert inner == [""]
    assert ctrl == []


def test_long_inner_streamed_character_by_character():
    content = "思考[说明[细节]][WALK:1,2][TURN:left][INNER:继续]" * 4096
    inner, ctrl, f = _spy()
    for char in "[INNER:" + content:
        assert f.feed(char) == ""
    assert inner == ctrl == []
    assert f.feed("]结束") == "结束"
    assert inner == [content]
    assert ctrl == []
    assert f.flush() == ""
