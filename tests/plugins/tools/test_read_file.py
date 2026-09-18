from conic.core.bus import MessageBus
from conic.plugins.tools.read_file import ReadFileCall, ReadFileToolPlugin
from conic.types.messages import BuildSystemPrompt


async def test_reads_non_ascii_utf8_content_correctly(tmp_path):
    (tmp_path / "a.txt").write_text("你好 🎉", encoding="utf-8")
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="a.txt"))
    assert result.error is None
    assert result.output == "你好 🎉"


async def test_read_reports_oserror_as_result_error(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello")
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))

    def broken_read_text(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.read_text", broken_read_text)
    result = await tool.execute(ReadFileCall(path="a.txt"))
    assert result.error is not None
    assert "disk full" in result.error


async def test_reads_full_small_file(tmp_path):
    (tmp_path / "a.txt").write_text("line1\nline2\nline3")
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="a.txt"))
    assert result.error is None
    assert result.output == "line1\nline2\nline3"


async def test_reads_with_offset_and_limit(tmp_path):
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(10)))
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="a.txt", offset=2, limit=3))
    assert result.output.splitlines()[:3] == ["line2", "line3", "line4"]
    assert "truncated" in result.output


async def test_errors_on_missing_file(tmp_path):
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="missing.txt"))
    assert result.error is not None


async def test_rejects_path_outside_workspace(tmp_path):
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="../outside.txt"))
    assert result.error is not None
    assert result.output is None


async def test_contributes_a_workspace_restriction_section_to_the_system_prompt(tmp_path):
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    bus = MessageBus()
    tool.register(bus)

    result = await bus.chain("build_system_prompt", BuildSystemPrompt(sections={}))

    assert "read_file" in result.sections
    assert str(tmp_path) in result.sections["read_file"]
