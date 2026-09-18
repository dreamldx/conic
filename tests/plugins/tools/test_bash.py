import sys

from conic.plugins.tools.bash import BashCall, BashToolPlugin
from conic.core.bus import MessageBus
from conic.types.messages import BuildSystemPrompt


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


async def test_contributes_a_bash_guidance_section_with_the_configured_timeout():
    tool = BashToolPlugin(workspace_dir="/tmp/ws", timeout=42)
    bus = MessageBus()
    tool.register(bus)

    result = await bus.chain("build_system_prompt", BuildSystemPrompt(sections={}))

    assert "bash" in result.sections
    assert "42" in result.sections["bash"]
    assert "long-running" in result.sections["bash"]
    assert "/tmp/ws" in result.sections["bash"]


def test_register_wires_tool_call_request():
    from conic.core.bus import MessageBus

    tool = BashToolPlugin(workspace_dir=".")
    bus = MessageBus()
    tool.register(bus)
    assert bus._request["tool_call"][0][0] is BashCall


async def test_execute_reports_oserror_from_communicate(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path), timeout=5)
    bus = MessageBus()
    tool.register(bus)
    result = await bus.request("tool_call", BashCall(command="nonexistent_command_xyz"))
    assert result.error is not None


async def test_execute_command_times_out_with_short_timeout(tmp_path):
    import asyncio
    tool = BashToolPlugin(workspace_dir=str(tmp_path), timeout=0.1)
    bus = MessageBus()
    tool.register(bus)
    if asyncio.get_event_loop_policy().__class__.__name__ == "WindowsProactorEventLoopPolicy":
        cmd = 'python -c "import time; time.sleep(10)"'
    else:
        cmd = "sleep 10"
    result = await bus.request("tool_call", BashCall(command=cmd))
    assert result.error is not None
    assert "timed out" in result.error
