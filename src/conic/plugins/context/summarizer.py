from conic.core.messages import ModelRequest, SummarizeRequest, SummarizeResult


class SummarizerPlugin:
    def __init__(self, keep_recent: int = 5):
        self._keep_recent = keep_recent
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request("summarize", self.summarize)

    async def summarize(self, req: SummarizeRequest) -> SummarizeResult:
        system = [m for m in req.messages if m.get("role") == "system"]
        rest = [m for m in req.messages if m.get("role") != "system"]
        to_summarize = rest[: -self._keep_recent] if self._keep_recent else rest
        recent = rest[-self._keep_recent:] if self._keep_recent else []

        if not to_summarize:
            return SummarizeResult(messages=req.messages)

        transcript = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in to_summarize)
        prompt = [
            {
                "role": "system",
                "content": "Summarize the following conversation history concisely, preserving key facts and decisions.",
            },
            {"role": "user", "content": transcript},
        ]
        response = await self._bus.request("model_request", ModelRequest(messages=prompt, tools=[]))
        summary_message = {"role": "system", "content": f"[Earlier conversation summary]\n{response.text}"}
        return SummarizeResult(messages=[*system, summary_message, *recent])
