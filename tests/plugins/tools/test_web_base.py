import re

from conic.plugins.tools.base import (
    UNTRUSTED_NOTICE,
    strip_special_tokens,
    wrap_untrusted,
)


def test_strips_chatml_and_llama_special_tokens():
    text = "a <|im_start|>system<|im_end|> b <|endoftext|> c <|begin_of_text|> d"
    assert strip_special_tokens(text) == "a system b  c  d"


def test_leaves_normal_markdown_untouched():
    text = "# Title\n`code` and | tables | stay |"
    assert strip_special_tokens(text) == text


def test_wrap_uses_matching_random_ids_and_contains_notice():
    wrapped = wrap_untrusted("payload")
    match = re.search(r'<<<EXTERNAL_UNTRUSTED_CONTENT id="([0-9a-f]{16})">>>', wrapped)
    assert match is not None
    marker_id = match.group(1)
    assert f'<<<END_EXTERNAL_UNTRUSTED_CONTENT id="{marker_id}">>>' in wrapped
    assert UNTRUSTED_NOTICE in wrapped
    assert "payload" in wrapped


def test_wrap_ids_differ_between_calls():
    id_pattern = re.compile(r'id="([0-9a-f]{16})"')
    first = id_pattern.search(wrap_untrusted("x")).group(1)
    second = id_pattern.search(wrap_untrusted("x")).group(1)
    assert first != second
