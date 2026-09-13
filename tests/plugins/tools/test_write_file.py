from conic.plugins.tools.write_file import WriteFileCall, WriteFileToolPlugin


async def test_writes_and_reads_back_non_ascii_utf8_content(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="a.txt", content="你好 🎉"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "你好 🎉"


async def test_write_reports_oserror_as_result_error(tmp_path, monkeypatch):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))

    def broken_write_text(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", broken_write_text)
    result = await tool.execute(WriteFileCall(path="a.txt", content="hello"))
    assert result.error is not None
    assert "disk full" in result.error


async def test_creates_new_file(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="a.txt", content="hello\nworld"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text() == "hello\nworld"
    assert "created" in result.output


async def test_overwrites_existing_file(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="a.txt", content="new"))
    assert (tmp_path / "a.txt").read_text() == "new"
    assert "overwritten" in result.output


async def test_creates_parent_directories(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    await tool.execute(WriteFileCall(path="sub/dir/a.txt", content="hi"))
    assert (tmp_path / "sub" / "dir" / "a.txt").read_text() == "hi"


async def test_rejects_path_outside_workspace(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="../outside.txt", content="x"))
    assert result.error is not None
