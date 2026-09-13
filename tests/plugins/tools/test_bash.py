import sys

from conic.plugins.tools.bash import BashCall, BashToolPlugin


async def test_execute_runs_command_and_returns_stdout(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(BashCall(command="echo hello"))
    assert result.error is None
    assert "hello" in result.output


async def test_execute_runs_in_workspace_directory(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    list_cmd = "dir /b" if sys.platform == "win32" else "ls"
    result = await tool.execute(BashCall(command=list_cmd))
    assert "marker.txt" in result.output


async def test_execute_reports_nonzero_exit_code_as_error(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    fail_cmd = f'{sys.executable} -c "import sys; sys.exit(3)"'
    result = await tool.execute(BashCall(command=fail_cmd))
    assert result.error is not None
    assert "3" in result.error


async def test_execute_times_out_long_running_command(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path), timeout=0.2)
    sleep_cmd = f'{sys.executable} -c "import time; time.sleep(5)"'
    result = await tool.execute(BashCall(command=sleep_cmd))
    assert result.error is not None
    assert "timed out" in result.error


async def test_execute_truncates_large_output(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path), max_output_bytes=100)
    big_cmd = f'{sys.executable} -c "print(\'x\' * 5000)"'
    result = await tool.execute(BashCall(command=big_cmd))
    assert result.error is None
    assert len(result.output.encode()) < 5000
    assert "truncated" in result.output


async def test_execute_handles_oserror_from_communicate(tmp_path, monkeypatch):
    """Test that OSError from proc.communicate() is caught and returned as ToolCallResult error."""
    from unittest.mock import AsyncMock, MagicMock

    tool = BashToolPlugin(workspace_dir=str(tmp_path))

    # Create a fake process object
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(side_effect=OSError("broken pipe"))
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()

    # Patch create_subprocess_shell to return our fake process
    async def fake_create_subprocess_shell(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_create_subprocess_shell)

    result = await tool.execute(BashCall(command="echo test"))
    assert result.error is not None
    assert "broken pipe" in result.error
    assert result.output is None


def test_register_wires_tool_call_request():
    from conic.core.bus import MessageBus

    tool = BashToolPlugin(workspace_dir=".")
    bus = MessageBus()
    tool.register(bus)
    assert bus._request["tool_call"][0][0] is BashCall
