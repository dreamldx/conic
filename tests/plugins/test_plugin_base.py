import pytest

from conic.plugins import Plugin


class _PluginWithName(Plugin):
    Name = "test_plugin"

    def register(self, bus) -> None:
        pass


def test_subclass_without_name_raises_typeerror():
    with pytest.raises(TypeError, match="must define 'Name'"):

        class _(Plugin):
            pass


def test_subclass_with_name_does_not_raise():
    class _(Plugin):
        Name = "ok"

    assert _.Name == "ok"


def test_name_is_accessible_on_class():
    assert _PluginWithName.Name == "test_plugin"


def test_default_register_raises_not_implemented():
    class _(Plugin):
        Name = "stub"

    with pytest.raises(NotImplementedError):
        _().register(bus=None)


def test_register_does_not_raise_when_overridden():
    plugin = _PluginWithName()
    plugin.register(bus=None)