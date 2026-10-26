"""Configuration from the environment, read once at import. Other modules read values as `settings.X`
at call time, so tests can change them with monkeypatch."""
import logging
import os
from pathlib import Path

ROOT = Path(__file__).parent
# Postgres when a connection string is set (Neon on Vercel sets DATABASE_URL), SQLite otherwise.
DATABASE_URL = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
DB = Path(os.environ.get('DB_PATH', '/tmp/alfa-navigator.db'))
if not DATABASE_URL:
    DB.parent.mkdir(parents=True, exist_ok=True)
# MIGRATE_ON_START=0 leaves migrations to an explicit `python -m app.main migrate` step; /api/ready reports the lag.
MIGRATE_ON_START = os.getenv('MIGRATE_ON_START', '1') == '1'

# Demo-only tokens, supplied by environment. Browser clients log in explicitly.
DEMO_CLIENT_KEY = os.getenv('DEMO_CLIENT_KEY', 'demo-client-change-me')
DEMO_BANK_KEY = os.getenv('DEMO_BANK_KEY', 'demo-bank-change-me')
DEMO_LOGIN_PASSWORD = os.getenv('DEMO_LOGIN_PASSWORD', 'alfa-demo-2026')
# Separate per-role passwords; both fall back to the shared demo password for local runs.
DEMO_PASSWORDS = {'client': os.getenv('DEMO_CLIENT_PASSWORD', DEMO_LOGIN_PASSWORD),
                  'bank': os.getenv('DEMO_BANK_PASSWORD', DEMO_LOGIN_PASSWORD)}
PUBLIC_DEFAULTS = {'demo-client-change-me', 'demo-bank-change-me', 'alfa-demo-2026'}
# Production and preview deployments are reachable from the internet.
VERCEL_ENV = os.getenv('VERCEL_ENV')
if VERCEL_ENV in ('production', 'preview'):
    if PUBLIC_DEFAULTS & {DEMO_CLIENT_KEY, DEMO_BANK_KEY, *DEMO_PASSWORDS.values()}:
        raise RuntimeError(f'{VERCEL_ENV} uses demo secrets from the public repo: set DEMO_CLIENT_KEY, DEMO_BANK_KEY, DEMO_CLIENT_PASSWORD, DEMO_BANK_PASSWORD')
    if not DATABASE_URL:
        raise RuntimeError(f'{VERCEL_ENV} needs a persistent database: set DATABASE_URL, otherwise data goes to a temporary SQLite in /tmp')

OFFER_VALID_DAYS = float(os.getenv('OFFER_VALID_DAYS', '7'))
BANK_SLA_HOURS = float(os.getenv('BANK_SLA_HOURS', '24'))
# Live updates: a stream lives under Vercel's function limit (maxDuration 10 s in vercel.json),
# then the browser reconnects from the cursor it got; the database is checked every STREAM_POLL_SECONDS.
STREAM_SECONDS = float(os.getenv('STREAM_SECONDS', '8'))
STREAM_POLL_SECONDS = float(os.getenv('STREAM_POLL_SECONDS', '1'))
ENABLE_DEMO_SEED = os.getenv('ENABLE_DEMO_SEED', '1') == '1'
ENABLE_DEMO_RESET = os.getenv('ENABLE_DEMO_RESET', '0') == '1'

log = logging.getLogger('navigator')
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
