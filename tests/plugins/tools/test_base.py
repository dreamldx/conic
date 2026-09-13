import pytest

from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


def test_resolves_path_inside_workspace(tmp_path):
    (tmp_path / "sub").mkdir()
    resolved = resolve_within_workspace(str(tmp_path), "sub/file.txt")
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()


def test_workspace_root_itself_is_allowed(tmp_path):
    resolved = resolve_within_workspace(str(tmp_path), ".")
    assert resolved == tmp_path.resolve()


def test_rejects_relative_escape(tmp_path):
    with pytest.raises(WorkspaceEscapeError):
        resolve_within_workspace(str(tmp_path), "../outside.txt")


def test_rejects_absolute_path_outside_workspace(tmp_path):
    with pytest.raises(WorkspaceEscapeError):
        resolve_within_workspace(str(tmp_path), str(tmp_path.parent / "outside.txt"))
