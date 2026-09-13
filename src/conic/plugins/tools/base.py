from pathlib import Path


class WorkspaceEscapeError(Exception):
    pass


def resolve_within_workspace(workspace_dir: str, path: str) -> Path:
    base = Path(workspace_dir).resolve()
    candidate = (base / path).resolve()
    if candidate != base and base not in candidate.parents:
        raise WorkspaceEscapeError(f"path escapes workspace: {path!r}")
    return candidate
