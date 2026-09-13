import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb


@dataclass
class SessionRow:
    session_key: str
    channel: str
    native_id: str
    workspace_dir: str
    model: str
    status: str
    created_at: datetime


class SessionHandle:
    def __init__(self, conn: duckdb.DuckDBPyConnection, session_key: str):
        self._conn = conn
        self._session_key = session_key

    def append_message(self, message: dict) -> None:
        seq = self._next_seq()
        self._conn.execute(
            "INSERT INTO messages (session_key, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [self._session_key, seq, message.get("role", ""), json.dumps(message), datetime.now(timezone.utc).isoformat()],
        )

    def load_history(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE session_key = ? ORDER BY seq", [self._session_key]
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_status(self, status: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET status = ? WHERE session_key = ?", [status, self._session_key]
        )

    def _next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_key = ?", [self._session_key]
        ).fetchone()
        return row[0]


class StorageService:
    def __init__(self, db_path: str, workspace_root: str, default_model: str):
        self._db_path = db_path
        self._workspace_root = Path(workspace_root)
        self._default_model = default_model
        self._conn: duckdb.DuckDBPyConnection | None = None

    def startup(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self._db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_key VARCHAR PRIMARY KEY,
                channel VARCHAR,
                native_id VARCHAR,
                workspace_dir VARCHAR,
                model VARCHAR,
                status VARCHAR,
                created_at VARCHAR
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                session_key VARCHAR,
                seq INTEGER,
                role VARCHAR,
                content VARCHAR,
                created_at VARCHAR,
                PRIMARY KEY (session_key, seq)
            )"""
        )

    def shutdown(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_or_create(self, channel: str, native_id: str) -> SessionRow:
        session_key = f"{channel}:{native_id}"
        row = self._conn.execute(
            "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at "
            "FROM sessions WHERE session_key = ?",
            [session_key],
        ).fetchone()
        if row is not None:
            return self._row_from_tuple(row)

        workspace_dir = str(self._workspace_root / channel / native_id)
        Path(workspace_dir).mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        created_at_str = created_at.isoformat()
        self._conn.execute(
            "INSERT INTO sessions (session_key, channel, native_id, workspace_dir, model, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [session_key, channel, native_id, workspace_dir, self._default_model, "active", created_at_str],
        )
        return SessionRow(
            session_key=session_key,
            channel=channel,
            native_id=native_id,
            workspace_dir=workspace_dir,
            model=self._default_model,
            status="active",
            created_at=created_at,
        )

    def handle_for(self, row: SessionRow) -> SessionHandle:
        return SessionHandle(self._conn, row.session_key)

    def active_sessions(self, channel: str) -> list[SessionRow]:
        rows = self._conn.execute(
            "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at "
            "FROM sessions WHERE channel = ? AND status = 'active'",
            [channel],
        ).fetchall()
        return [self._row_from_tuple(r) for r in rows]

    def _row_from_tuple(self, r) -> SessionRow:
        created_at_str = r[6]
        created_at = datetime.fromisoformat(created_at_str)
        return SessionRow(
            session_key=r[0], channel=r[1], native_id=r[2], workspace_dir=r[3],
            model=r[4], status=r[5], created_at=created_at,
        )
