from conic.services.models import Message, Session


def create_sessions_table_sql() -> tuple[str, list]:
    return ("""\
CREATE TABLE IF NOT EXISTS sessions (
    session_key VARCHAR PRIMARY KEY,
    channel VARCHAR,
    native_id VARCHAR,
    workspace_dir VARCHAR,
    model VARCHAR,
    status VARCHAR,
    created_at VARCHAR
)""", [])


def create_messages_table_sql() -> tuple[str, list]:
    return ("""\
CREATE TABLE IF NOT EXISTS messages (
    session_key VARCHAR,
    seq INTEGER,
    role VARCHAR,
    content VARCHAR,
    created_at VARCHAR,
    PRIMARY KEY (session_key, seq)
)""", [])


def insert_message_sql(msg: Message) -> tuple[str, list]:
    return (
        "INSERT INTO messages (session_key, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        [msg.session_key, msg.seq, msg.role, msg.content, msg.created_at.isoformat()],
    )


def load_history_sql(session_key: str) -> tuple[str, list]:
    return "SELECT content FROM messages WHERE session_key = ? ORDER BY seq", [session_key]


def set_session_status_sql(session_key: str, status: str) -> tuple[str, list]:
    return "UPDATE sessions SET status = ? WHERE session_key = ?", [status, session_key]


def next_seq_sql(session_key: str) -> tuple[str, list]:
    return "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_key = ?", [session_key]


def get_session_sql(session_key: str) -> tuple[str, list]:
    return (
        "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at FROM sessions WHERE session_key = ?",
        [session_key],
    )


def insert_session_sql(session: Session) -> tuple[str, list]:
    return (
        "INSERT INTO sessions (session_key, channel, native_id, workspace_dir, model, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [session.session_key, session.channel, session.native_id, session.workspace_dir, session.model, session.status, session.created_at.isoformat()],
    )


def list_active_sessions_sql(channel: str) -> tuple[str, list]:
    return (
        "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at FROM sessions WHERE channel = ? AND status = 'active'",
        [channel],
    )
