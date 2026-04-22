import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# edited by bridge test
from cc_feishu_bridge.format.edit_diff import (
    colorize_diff,
    _lcs_diff,
    _truncate_diff,
    format_edit_card,
    format_write_card,
    build_edit_marker,
    build_write_marker,
    _DiffMarker,
    DiffLine,
    COLOR_RED, COLOR_GREEN, COLOR_GREY,
    MAX_DIFF_LINES,
)


class TestLCS:
    def test_no_change(self):
        diff = colorize_diff("foo\nbar", "foo\nbar")
        assert all(d.type == "context" for d in diff)
        assert len(diff) == 2

    def test_add_lines(self):
        diff = colorize_diff("foo", "foo\nbar")
        types = [d.type for d in diff]
        assert types == ["context", "insertion"]

    def test_remove_lines(self):
        diff = colorize_diff("foo\nbar", "foo")
        types = [d.type for d in diff]
        assert types == ["context", "deletion"]

    def test_replace_line(self):
        diff = colorize_diff("foo", "bar")
        types = [d.type for d in diff]
        assert types == ["deletion", "insertion"]

    def test_empty_old(self):
        diff = colorize_diff("", "foo\nbar")
        assert all(d.type == "insertion" for d in diff)

    def test_empty_new(self):
        diff = colorize_diff("foo\nbar", "")
        assert all(d.type == "deletion" for d in diff)


class TestTruncate:
    def test_under_limit(self):
        lines = [DiffLine("context", f"line {i}") for i in range(30)]
        result = _truncate_diff(lines)
        assert len(result) == 30

    def test_over_limit_truncates(self):
        lines = [DiffLine("context", f"line {i}") for i in range(60)]
        result = _truncate_diff(lines)
        assert len(result) < 60


class TestFormatCard:
    def test_edit_card_structure(self):
        diff = colorize_diff("foo", "bar")
        card = format_edit_card("/tmp/test.txt", diff)
        assert card["schema"] == "2.0"
        assert "body" in card
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "markdown"
        assert elements[1]["tag"] == "markdown"

    def test_edit_card_annotated_text_coloring(self):
        diff = colorize_diff("foo", "bar")
        card = format_edit_card("/tmp/test.txt", diff)
        md_element = card["body"]["elements"][1]
        assert md_element["tag"] == "markdown"
        content = md_element["content"]
        # Diff lines should be colored with font tags and line numbers
        assert "<font color='red'>1 │ - foo</font>" in content
        assert "<font color='green'>2 │ + bar</font>" in content

    def test_write_card_structure(self):
        card = format_write_card("/tmp/test.txt", ["line1", "line2"])
        assert card["schema"] == "2.0"
        assert "body" in card


class TestBuildMarker:
    def test_edit_marker(self):
        inp = '{"file_path": "/tmp/a.txt", "old_string": "foo", "new_string": "bar"}'
        marker = build_edit_marker(inp)
        assert isinstance(marker, _DiffMarker)
        assert marker.tool_name == "Edit"
        assert hasattr(marker, 'card')

    def test_edit_invalid_json_falls_back(self):
        from cc_feishu_bridge.format.reply_formatter import ReplyFormatter
        f = ReplyFormatter()
        result = f.format_tool_call("Edit", "not json")
        assert isinstance(result, str)  # falls back to string

    def test_write_marker(self):
        import json
        inp = json.dumps({"file_path": "/tmp/a.txt", "content": "hello\nworld"})
        result = build_write_marker(inp)
        assert isinstance(result, list)
        marker = result[0]
        assert isinstance(marker, _DiffMarker)
        assert marker.tool_name == "Write"
