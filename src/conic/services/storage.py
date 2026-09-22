import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from loguru import logger

from conic.services import queries
from conic.services.models import Message, ModelCatalogEntry, Session

DEFAULT_MODEL_CONTEXT_LENGTH = 65535


class SessionHandle:
    def __init__(self, conn: duckdb.DuckDBPyConnection, session_key: str):
        self._conn = conn
        self._session_key = session_key

    def append_message(self, message: dict, turn_id: int) -> None:
        seq = self._next_seq()
        msg = Message(
            session_key=self._session_key,
            seq=seq,
            role=message.get("role", ""),
            content=json.dumps(message),
            created_at=datetime.now(UTC),
            turn_id=turn_id,
        )
        sql, params = queries.insert_message_sql(msg)
        self._conn.execute(sql, params)

    def load_history(self) -> list[dict]:
        sql, params = queries.load_history_sql(self._session_key)
        rows = self._conn.execute(sql, params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_status(self, status: str) -> None:
        sql, params = queries.set_session_status_sql(self._session_key, status)
        self._conn.execute(sql, params)

    def save_variables(self, variables: dict) -> None:
        sql, params = queries.set_session_variables_sql(self._session_key, variables)
        self._conn.execute(sql, params)

    def _next_seq(self) -> int:
        sql, params = queries.next_seq_sql(self._session_key)
        row = self._conn.execute(sql, params).fetchone()
        return row[0]


class StorageService:
    def __init__(self, db_path: str, workspace_root: str, default_model: str):
        self._db_path = db_path
        self._workspace_root = Path(workspace_root)
        self._default_model = default_model
        self._conn: duckdb.DuckDBPyConnection | None = None

    def startup(self) -> None:
        logger.debug("initializing duckdb at {}", self._db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self._db_path)
        self._conn.execute(*queries.create_sessions_table_sql())
        self._conn.execute(*queries.create_messages_table_sql())
        self._conn.execute(*queries.add_sessions_variables_column_sql())
        self._conn.execute(*queries.add_messages_turn_id_column_sql())
        self._conn.execute(*queries.create_model_catalog_table_sql())
        for sql, params in queries.add_model_catalog_extra_columns_sql():
            self._conn.execute(sql, params)

    def shutdown(self) -> None:
        if self._conn is not None:
            logger.debug("closing duckdb connection")
            self._conn.close()
            self._conn = None

    def get_or_create(self, channel: str, native_id: str) -> Session:
        session_key = f"{channel}:{native_id}"
        sql, params = queries.get_session_sql(session_key)
        row = self._conn.execute(sql, params).fetchone()
        if row is not None:
            logger.debug("resuming session {}", session_key)
            return self._session_from_row(row)

        workspace_dir = str(self._workspace_root / channel / native_id)
        logger.info("creating session {} workspace={}", session_key, workspace_dir)
        Path(workspace_dir).mkdir(parents=True, exist_ok=True)
        session = Session(
            session_key=session_key,
            channel=channel,
            native_id=native_id,
            workspace_dir=workspace_dir,
            model=self._default_model,
            status="active",
            created_at=datetime.now(UTC),
            variables={},
        )
        sql, params = queries.insert_session_sql(session)
        self._conn.execute(sql, params)
        return session

    def handle_for(self, row: Session) -> SessionHandle:
        return SessionHandle(self._conn, row.session_key)

    def active_sessions(self, channel: str) -> list[Session]:
        sql, params = queries.list_active_sessions_sql(channel)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._session_from_row(r) for r in rows]

    def save_model_catalog(self, entries: list[ModelCatalogEntry]) -> None:
        self._conn.execute(*queries.clear_model_catalog_sql())
        for entry in entries:
            sql, params = queries.insert_model_catalog_entry_sql(entry)
            self._conn.execute(sql, params)

    def list_model_catalog(self) -> list[ModelCatalogEntry]:
        sql, params = queries.list_model_catalog_sql()
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ModelCatalogEntry(
                id=r[0], name=r[1], description=r[2], context_length=r[3], supports_tools=r[4],
                pricing_prompt=r[5], pricing_completion=r[6],
                input_modalities=json.loads(r[7]) if r[7] else [],
                output_modalities=json.loads(r[8]) if r[8] else [],
                supported_parameters=json.loads(r[9]) if r[9] else [],
                fetched_at=_parse_stored_datetime(r[10]),
            )
            for r in rows
        ]

    def get_model_context_length(self, model_id: str) -> int:
        sql, params = queries.get_model_context_length_sql(model_id)
        row = self._conn.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return DEFAULT_MODEL_CONTEXT_LENGTH
        return row[0]

    @staticmethod
    def _session_from_row(r) -> Session:
        return Session(
            session_key=r[0], channel=r[1], native_id=r[2], workspace_dir=r[3],
            model=r[4], status=r[5], created_at=_parse_stored_datetime(r[6]),
            variables=json.loads(r[7]) if r[7] else {},
        )


def _parse_stored_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
