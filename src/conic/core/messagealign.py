def align_cut(messages: list[dict], cut_index: int) -> int:
    """Adjust a proposed cut index so slicing ``messages[cut_index:]`` never
    orphans a ``role: "tool"`` message from the ``tool_calls``-bearing
    assistant message it replies to.

    Invariant of the result: the returned slice never contains a
    ``role: "tool"`` message unless the assistant message whose
    ``tool_calls`` it answers is also included (i.e. the slice never starts
    in the middle of an assistant-message + tool-replies run).
    """
    if cut_index <= 0:
        return max(cut_index, 0)

    # If the message at the proposed cut point is a tool reply, it's an
    # orphan: its owning assistant (tool_calls) message would be cut away.
    # Walk the cut index backward, one message at a time, past every
    # consecutive tool reply until we reach the assistant message that
    # produced them (or index 0), so the whole run stays together.
    while cut_index > 0 and messages[cut_index].get("role") == "tool":
        cut_index -= 1

    return cut_index
