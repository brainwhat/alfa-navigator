"""Storage: one connection API for Postgres and SQLite, the clock, versioned migrations and the event log."""
import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from app import settings
from app.settings import log

# Milliseconds spent in SQL during the current request, for the request log.
sql_ms = ContextVar('sql_ms', default=None)

class Db:
    """One connection for both engines: sqlite-style '?' placeholders, and query time goes to the request log."""
    def __init__(self, raw, postgres):
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql, params=()):
        started = time.perf_counter()
        try:
            return self.raw.execute(sql.replace('?', '%s') if self.postgres else sql, params)
        finally:
            if (spent := sql_ms.get()) is not None:
                spent[0] += (time.perf_counter() - started) * 1000

_open_connection = ContextVar('open_connection', default=None)

@contextmanager
def conn():
    """Open a connection, or reuse the one already open in this context so nested calls share a transaction."""
    if _open_connection.get() is not None:
        yield _open_connection.get()
        return
    if settings.DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        # prepare_threshold=None: Neon's pooled endpoint runs PgBouncer in transaction mode.
        with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=5) as raw:
            token = _open_connection.set(Db(raw, postgres=True))
            try:
                yield _open_connection.get()
            finally:
                _open_connection.reset(token)
        return
    raw = sqlite3.connect(settings.DB, timeout=10, isolation_level=None)
    raw.row_factory = sqlite3.Row
    token = _open_connection.set(Db(raw, postgres=False))
    try:
        # Take the write lock up front: concurrent requests queue for it, instead of failing with
        # "database is locked" when two transactions try to upgrade their read locks at once.
        raw.execute('BEGIN IMMEDIATE')
        yield _open_connection.get()
        raw.execute('COMMIT')
    except BaseException:
        if raw.in_transaction:
            raw.execute('ROLLBACK')
        raise
    finally:
        _open_connection.reset(token)
        raw.close()

# The demo seeder backdates its history by moving this clock; requests use real time.
clock = ContextVar('clock', default=None)

def utcnow() -> datetime:
    return clock.get() or datetime.now(timezone.utc)

def now() -> str:
    return utcnow().isoformat()

def event(c, id, actor, action, description, data=None, at=None):
    c.execute('INSERT INTO events (application_id, occurred_at, actor, action, description, data) VALUES (?,?,?,?,?,?)',
              (id, at or now(), actor, action, description, json.dumps(data, ensure_ascii=False) if data else None))

STATUSES = ('submitted', 'needs_info', 'clarified', 'approved', 'counteroffer', 'rejected', 'accepted', 'withdrawn', 'expired')

def columns(c, table):
    if settings.DATABASE_URL:
        return {r['column_name'] for r in c.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?', (table,))}
    return {r[1] for r in c.execute(f'PRAGMA table_info({table})')}

def add_columns(c, table, new):
    existing = columns(c, table)
    for name, kind in new:
        if name not in existing:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')

def schema_v1(c):
    """The schema as it was before migrations were versioned, so existing databases pass through unchanged."""
    c.execute('''CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL, company TEXT NOT NULL, purpose TEXT NOT NULL, amount INTEGER NOT NULL,
     months INTEGER NOT NULL, available_cashflow INTEGER NOT NULL, revenue INTEGER NOT NULL,
     existing_payments INTEGER NOT NULL, overdue INTEGER NOT NULL, status TEXT NOT NULL,
     score INTEGER, decision TEXT, alternative_amount INTEGER, explanation TEXT)''')
    event_id = 'BIGSERIAL PRIMARY KEY' if settings.DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    c.execute(f'''CREATE TABLE IF NOT EXISTS events (id {event_id},
     application_id TEXT NOT NULL, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
     action TEXT NOT NULL, description TEXT NOT NULL)''')
    add_columns(c, 'applications', [('offer_expires_at', 'TEXT'), ('info_request', 'TEXT')])

def schema_v2(c):
    """Optimistic locking, idempotent creation, resubmission link, structured events and indexes."""
    add_columns(c, 'applications', [('version', 'INTEGER NOT NULL DEFAULT 0'), ('idempotency_key', 'TEXT'), ('previous_id', 'TEXT')])
    add_columns(c, 'events', [('data', 'TEXT')])
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS applications_idempotency_key ON applications (idempotency_key)')
    c.execute('CREATE INDEX IF NOT EXISTS applications_open_offers ON applications (status, offer_expires_at)')
    c.execute('CREATE INDEX IF NOT EXISTS events_history ON events (application_id, id)')
    if settings.DATABASE_URL:
        # NOT VALID checks new rows only, so legacy data can never block the deploy
        c.execute('ALTER TABLE events ADD CONSTRAINT events_application_fk FOREIGN KEY (application_id) REFERENCES applications (id) NOT VALID')
        allowed = ', '.join(f"'{s}'" for s in STATUSES)
        c.execute(f'ALTER TABLE applications ADD CONSTRAINT applications_status_check CHECK (status IN ({allowed})) NOT VALID')

MIGRATIONS = [schema_v1, schema_v2]
SCHEMA_VERSION = len(MIGRATIONS)

def migrate():
    with conn() as c:
        if settings.DATABASE_URL:
            # Serverless instances may cold-start at once; serialize migrations.
            c.execute('SELECT pg_advisory_xact_lock(20261025)')
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        done = {r['version'] for r in c.execute('SELECT version FROM schema_migrations')}
        for number, step in enumerate(MIGRATIONS, 1):
            if number not in done:
                step(c)
                c.execute('INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)', (number, now()))
                log.info(json.dumps({'event': 'migrated', 'version': number}))

def schema_version(c):
    """Applied schema version, None when migrations never ran."""
    if not columns(c, 'schema_migrations'):
        return None
    return c.execute('SELECT MAX(version) AS v FROM schema_migrations').fetchone()['v']
