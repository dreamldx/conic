from conic.plugins.tools.edit_file import EditFileCall, EditFileToolPlugin


async def test_edits_non_ascii_utf8_content_correctly(tmp_path):
    (tmp_path / "a.txt").write_text("你好 world 🎉", encoding="utf-8")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="world", new_text="世界"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "你好 世界 🎉"


async def test_replaces_unique_occurrence(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="world", new_text="there"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text() == "hello there"


async def test_errors_when_old_text_not_found(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="missing", new_text="x"))
    assert result.error is not None
    assert (tmp_path / "a.txt").read_text() == "hello world"


async def test_errors_when_old_text_is_not_unique(tmp_path):
    (tmp_path / "a.txt").write_text("foo foo")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="foo", new_text="bar"))
    assert result.error is not None
    assert "not unique" in result.error
    assert (tmp_path / "a.txt").read_text() == "foo foo"


async def test_errors_on_missing_file(tmp_path):
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="missing.txt", old_text="a", new_text="b"))
    assert result.error is not None


async def test_rejects_path_outside_workspace(tmp_path):
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="../outside.txt", old_text="a", new_text="b"))
    assert result.error is not None
