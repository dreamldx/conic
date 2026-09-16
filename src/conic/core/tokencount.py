_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK symbols and punctuation (，。！？…)
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),  # Fullwidth forms (全角字符)
)


def _is_cjk(code: int) -> bool:
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(messages: list[dict]) -> int:
    """Estimate token usage with CJK characters weighted individually.

    Western text approximates ~1 token per 4 chars. CJK characters typically
    cost about one token each (some encoders more), so counting them under
    the 4-char rule would badly underestimate Chinese-heavy transcripts.
    """
    total = 0.0
    for m in messages:
        text = str(m.get("content", ""))
        cjk_count = sum(1 for ch in text if _is_cjk(ord(ch)))
        non_cjk = len(text) - cjk_count
        total += cjk_count + non_cjk / 4.0
    return int(total)
