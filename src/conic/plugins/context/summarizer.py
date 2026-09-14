from conic.core.messagealign import align_cut
from conic.core.messages import ModelRequest, SummarizeRequest, SummarizeResult
from conic.plugins import meta


class SummarizerPlugin:
    def __init__(self, keep_recent: int = 5):
        self._keep_recent = keep_recent
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.SummarizeEvent, self.summarize)

    async def summarize(self, req: SummarizeRequest) -> SummarizeResult:
        system = [m for m in req.messages if m.get("role") == "system"]
        rest = [m for m in req.messages if m.get("role") != "system"]
        if self._keep_recent:
            cut_index = align_cut(rest, max(len(rest) - self._keep_recent, 0))
        else:
            cut_index = len(rest)
        to_summarize = rest[:cut_index]
        recent = rest[cut_index:]

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
        response = await self._bus.request(meta.ModelRequestEvent, ModelRequest(messages=prompt, tools=[]))
        summary_message = {"role": "system", "content": f"[Earlier conversation summary]\n{response.text}"}
        return SummarizeResult(messages=[*system, summary_message, *recent])
