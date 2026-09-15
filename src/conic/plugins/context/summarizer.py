from conic.core.messagealign import align_cut
from conic.core.messages import ModelRequest, SummarizeRequest, SummarizeResult
from conic.plugins import meta

#: Marker prefix used to identify already-folded summary messages so we can
#: fold them back in on the next summarize pass instead of stacking fresh
#: copies forever.
SUMMARY_PREFIX = "[Earlier conversation summary]"


class SummarizerPlugin:
    def __init__(self, keep_recent: int = 5):
        self._keep_recent = keep_recent
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.SummarizeEvent, self.summarize)

    async def summarize(self, req: SummarizeRequest) -> SummarizeResult:
        # Any system message that is an *existing* summary is not a real prompt
        # section: it is conversation state we must re-fold into the transcript
        # so it gets compressed again, never re-emitted verbatim.
        existing_summaries: list[dict] = []
        real_system: list[dict] = []
        rest: list[dict] = []
        for m in req.messages:
            if m.get("role") == "system":
                content = str(m.get("content", "")).lstrip()
                if content.startswith(SUMMARY_PREFIX):
                    existing_summaries.append(m)
                else:
                    real_system.append(m)
            else:
                rest.append(m)

        if self._keep_recent:
            cut_index = align_cut(rest, max(len(rest) - self._keep_recent, 0))
        else:
            cut_index = len(rest)

        # Old summaries always go into the fold; only real conversation tail is
        # kept verbatim as `recent`.
        to_summarize = [*existing_summaries, *rest[:cut_index]]
        recent = rest[cut_index:]

        if not to_summarize:
            return SummarizeResult(messages=req.messages)

        transcript = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in to_summarize)
        prompt = [
            {
                "role": "system",
                "content": (
                    "Summarize the following conversation history concisely, preserving key "
                    "facts and decisions. If any earlier summary blocks are present, fold "
                    "them together into a single consolidated summary rather than repeating them."
                ),
            },
            {"role": "user", "content": transcript},
        ]
        response = await self._bus.request(meta.ModelRequestEvent, ModelRequest(messages=prompt, tools=[]))
        summary_message = {"role": "system", "content": f"{SUMMARY_PREFIX}\n{response.text}"}
        return SummarizeResult(messages=[*real_system, summary_message, *recent])
