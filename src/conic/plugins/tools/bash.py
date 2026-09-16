import asyncio
from dataclasses import dataclass

from loguru import logger

from conic.types.messages import BuildSystemPrompt, ToolCallResult
from conic.plugins import meta


@dataclass
class BashCall:
    command: str


class BashToolPlugin:
    llm_name = "bash"
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the session workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    }

    def __init__(self, workspace_dir: str, timeout: float = 60.0, max_output_bytes: int = 20_000):
        self._workspace_dir = workspace_dir
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes

    def register(self, bus) -> None:
        bus.on_request(meta.ToolCallRequestEvent, self.execute)
        bus.on(meta.BuildSystemPromptEvent, self.contribute_bash_guidance)

    async def contribute_bash_guidance(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["bash"] = (
            f"The bash tool kills any command still running after {self._timeout:g} seconds "
            "and reports it as a timeout error. Do not run long-running, blocking, or "
            "interactive commands (dev servers, watch mode, `tail -f`, waiting for user "
            "input, long `sleep`s) -- they will simply time out. Prefer commands that "
            "complete quickly and return their result directly.\n"
            f"Commands start in this session's workspace directory ({self._workspace_dir}). "
            "Stay inside it: don't `cd` out, and don't reference absolute paths that point "
            "elsewhere on the filesystem."
        )
        return msg

    async def execute(self, call: BashCall) -> ToolCallResult:
        logger.debug("bash: {}", call.command[:100])
        try:
            proc = await asyncio.create_subprocess_shell(
                call.command,
                cwd=self._workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolCallResult(error=str(exc))

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolCallResult(error=f"command timed out after {self._timeout}s")
        except OSError as exc:
            # Attempt cleanup without masking the original error
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass  # Ignore cleanup failures
            return ToolCallResult(error=str(exc))

        output = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        combined = output + (("\n" + err) if err else "")
        combined = self._truncate(combined)
        if proc.returncode != 0:
            return ToolCallResult(error=f"exit code {proc.returncode}: {combined}")
        return ToolCallResult(output=combined)

    def _truncate(self, text: str) -> str:
        data = text.encode()
        if len(data) <= self._max_output_bytes:
            return text
        return data[: self._max_output_bytes].decode(errors="replace") + "\n...[truncated]"
