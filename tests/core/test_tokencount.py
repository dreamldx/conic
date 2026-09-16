from conic.core.tokencount import estimate_tokens


def test_empty_messages_has_zero_tokens():
    assert estimate_tokens([]) == 0


def test_longer_content_yields_more_tokens():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "hi " * 1000}]
    assert estimate_tokens(long) > estimate_tokens(short)


def test_cjk_text_costs_more_than_same_length_latin():
    cjk = [{"role": "user", "content": "你好世界" * 5}]  # 20 CJK chars -> 20 tokens
    latin = [{"role": "user", "content": "x" * 20}]     # 20 latin chars -> 5 tokens
    assert estimate_tokens(cjk) > estimate_tokens(latin)
    assert estimate_tokens(cjk) == 20
    assert estimate_tokens(latin) == 5


def test_mixed_content_weights_cjk():
    # "hi 你好" -> 3 non-CJK chars (3/4) + 2 CJK chars (2) = 2.75 -> 2
    assert estimate_tokens([{"role": "user", "content": "hi 你好"}]) == 2


def test_cjk_punctuation_counts_as_weighted():
    # all-fullwidth Chinese sentence; the 。and， must be weighted too
    msg = [{"role": "user", "content": "这是，中文。测试！"}]
    assert estimate_tokens(msg) == 9
