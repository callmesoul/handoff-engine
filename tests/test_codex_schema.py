"""检查实际发送给 Codex 的结构化 schema 满足严格模式。"""
import json

from handoff.schema import default_schema_dir


def test_all_object_properties_are_required():
    schema = json.loads((default_schema_dir() / "codex-result.schema.json").read_text(encoding="utf-8"))

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)


def test_commit_message_field_validates():
    """commit_message 进了严格 schema：带上能过，缺了报错。"""
    from handoff import schema

    result = {
        "status": "completed",
        "summary": "改完了",
        "changed_paths": ["src/a.go"],
        "tests": [{"name": "unit", "status": "passed", "output": ""}],
        "migration_required": False,
        "risks": [],
        "notes": "",
        "commit_message": "feat: 新增 a",
    }
    assert schema.errors_for("codex-result", result) == []
    assert schema.errors_for(
        "codex-result", {k: v for k, v in result.items() if k != "commit_message"}
    )


def test_describe_command_completed_distinguishes_no_exit_code_from_success():
    """面板日志：exit=0 静默，但无退出码必须留痕，否则只剩 🔧 一行看不出后续。"""
    from handoff.codex_runner import _describe_item

    item = {"type": "command_execution", "command": "js build.js", "aggregated_output": ""}

    # 成功：刻意静默（现有行为）
    assert _describe_item({**item, "exit_code": 0}, completed=True) is None

    # 无退出码：必须给一行提示
    desc = _describe_item(item, completed=True)
    assert desc and "无退出码" in desc

    # 失败：保留原有 ✗ 格式
    failed = _describe_item(
        {**item, "exit_code": 1, "aggregated_output": "Error: something broke"}, completed=True
    )
    assert failed and "失败" in failed and "exit=1" in failed


def test_describe_mcp_tool_call_shows_argument_details():
    """面板日志：🔧 js 必须带出脚本内容/用途，不再只显示光秃秃的工具名。

    回归：node_repl 的 js 调用原来在面板上刷一整屏 `🔧 js`，看不出在干什么。
    现在 started 显示「工具名 + 用途 + 脚本摘要」，completed 静默避免重复。
    """
    from handoff.codex_runner import _describe_item

    item = {
        "type": "mcp_tool_call",
        "tool": "js",
        "arguments": {
            "title": "诊断 HTTP 测试请求 ID",
            "code": "var mfiles=await fs.readdir(root+'/middleware'); console.log(mfiles);",
        },
    }
    started = _describe_item(item, completed=False)
    assert started and started.startswith("🔧 js")
    assert "诊断 HTTP 测试请求 ID" in started
    assert "fs.readdir" in started  # 实际脚本内容必须出现

    # completed 静默，不重复刷同一行
    assert _describe_item(item, completed=True) is None

    # 失败时 completed 补一行错误
    failed_item = {
        **item,
        "result": {"is_error": True, "content": [{"type": "text", "text": "ReferenceError: x is not defined"}]},
    }
    failed = _describe_item(failed_item, completed=True)
    assert failed and "✗" in failed and "ReferenceError" in failed


def test_describe_mcp_tool_call_without_title_falls_back_to_json():
    """没有 title 的 MCP 工具：兜底也要带出参数内容，不能只剩工具名。"""
    from handoff.codex_runner import _describe_item

    item = {"type": "mcp_tool_call", "tool": "some_tool", "arguments": {"filename": "a.go", "flag": True}}
    desc = _describe_item(item, completed=False)
    assert desc and desc.startswith("🔧 some_tool")
    assert "a.go" in desc
