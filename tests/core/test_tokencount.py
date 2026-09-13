from conic.core.tokencount import estimate_tokens


def test_empty_messages_has_zero_tokens():
    assert estimate_tokens([]) == 0


def test_longer_content_yields_more_tokens():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "hi " * 1000}]
    assert estimate_tokens(long) > estimate_tokens(short)
