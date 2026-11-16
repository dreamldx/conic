import pytest

from conic.services.models import safe_float


@pytest.mark.parametrize("value", [None, "N/A", "", object(), float("nan"), float("inf"), "-inf"])
def test_safe_float_returns_default_for_unusable_values(value):
    assert safe_float(value) == 0.0
    assert safe_float(value, 1.5) == 1.5


@pytest.mark.parametrize(("value", "expected"), [(2, 2.0), ("3.5", 3.5), (0.25, 0.25), (0, 0.0)])
def test_safe_float_converts_numeric_values(value, expected):
    assert safe_float(value) == expected
