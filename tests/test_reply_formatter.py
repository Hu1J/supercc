import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.format.reply_formatter import ReplyFormatter, FEISHU_MAX_MESSAGE_LENGTH


@pytest.fixture
def formatter():
    return ReplyFormatter()


def test_format_basic_text(formatter):
    result = formatter.format_text("Hello world")
    assert result == "Hello world"


def test_format_with_code(formatter):
    result = formatter.format_text("Use `print()` to debug")
    assert "`print()`" in result


def test_format_tool_call(formatter):
    result = formatter.format_tool_call("Read", "src/main.py")
    assert "📖" in result
    assert "Read" in result
    assert "src/main.py" in result


def test_split_messages_short(formatter):
    chunks = formatter.split_messages("Short message")
    assert len(chunks) == 1
    assert chunks[0] == "Short message"


def test_split_messages_long(formatter):
    long_text = "x" * 5000
    chunks = formatter.split_messages(long_text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= FEISHU_MAX_MESSAGE_LENGTH


def test_format_bash_with_description(formatter):
    import json
    tool_input = json.dumps({"command": "ls -la", "description": "List all files"})
    result = formatter.format_tool_call("Bash", tool_input)
    assert result == "💻 **Bash** — List all files\n```bash\nls -la\n```"


def test_format_bash_without_description(formatter):
    import json
    tool_input = json.dumps({"command": "git status"})
    result = formatter.format_tool_call("Bash", tool_input)
    assert result == "💻 **Bash**\n```bash\ngit status\n```"


def test_format_bash_invalid_json(formatter):
    result = formatter.format_tool_call("Bash", "ls -la")
    assert "💻 **Bash**" in result
    assert "ls -la" in result


def test_format_bash_multiline_description(formatter):
    import json
    tool_input = json.dumps({"command": "pytest", "description": "Run tests\nVerbose output"})
    result = formatter.format_tool_call("Bash", tool_input)
    assert "**Bash** — Run tests" in result
    assert "Verbose output" in result


def test_format_read_tool(formatter):
    import json
    tool_input = json.dumps({"file_path": "src/main.py"})
    result = formatter.format_tool_call("Read", tool_input)
    assert result == "📖 **Read**\n`src/main.py`"


def test_format_read_tool_with_offset_and_limit(formatter):
    import json
    tool_input = json.dumps({"file_path": "src/main.py", "offset": 40, "limit": 50})
    result = formatter.format_tool_call("Read", tool_input)
    assert result == "📖 **Read** — offset 40 — limit 50\n`src/main.py`"


def test_format_read_tool_string_input(formatter):
    # 非 JSON 字符串输入（fallback）
    result = formatter.format_tool_call("Read", "src/main.py")
    assert result == "📖 **Read**\n`src/main.py`"


def test_format_todowrite_with_items(formatter):
    import json
    tool_input = json.dumps({
        "todos": [
            {"content": "Write tests", "status": "completed", "activeForm": "Writing tests"},
            {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"},
            {"content": "Deploy", "status": "pending", "activeForm": "Deploying"},
        ]
    })
    result = formatter.format_tool_call("TodoWrite", tool_input)
    assert "📋 Todo List" in result
    assert "| ✅ | Write tests | Writing tests |" in result
    assert "| 🔄 | Fix bug | Fixing bug |" in result
    assert "| ⬜ | Deploy | Deploying |" in result
    assert "✅" in result  # completed items and table header have check icon
    assert "activeForm" not in result  # actual key name not leaked


def test_format_todowrite_empty(formatter):
    import json
    tool_input = json.dumps({"todos": []})
    result = formatter.format_tool_call("TodoWrite", tool_input)
    assert result == "✅ 所有任务已完成！"


def test_format_todowrite_invalid_json(formatter):
    result = formatter.format_tool_call("TodoWrite", "not json")
    assert result == "✅ 所有任务已完成！"


def test_format_todowrite_injection_safety(formatter):
    """Pipe and newline in content must be escaped to not break table."""
    import json
    tool_input = json.dumps({
        "todos": [
            {"content": "Task with | pipe\nnewline", "status": "pending", "activeForm": "Do|ing stuff"},
        ]
    })
    result = formatter.format_tool_call("TodoWrite", tool_input)
    # Pipe must be escaped so it renders literally, not as table cell
    assert r"\|" in result
    # No raw pipe in content (must be escaped)
    assert "| pipe\n" not in result


def test_format_todowrite_non_list_todos(formatter):
    """todos field is not a list — should gracefully treat as empty."""
    import json
    tool_input = json.dumps({"todos": "not a list"})
    result = formatter.format_tool_call("TodoWrite", tool_input)
    assert result == "✅ 所有任务已完成！"
