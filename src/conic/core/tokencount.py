def estimate_tokens(messages: list[dict]) -> int:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return total_chars // 4
