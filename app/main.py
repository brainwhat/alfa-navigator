"""Alfa Navigator: autonomous synthetic hackathon credit pipeline."""
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
import secrets

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).parent
# Postgres when a connection string is set (Neon on Vercel sets DATABASE_URL), SQLite otherwise.
DATABASE_URL = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row
else:
    DB = Path(os.environ.get('DB_PATH', '/tmp/alfa-navigator.db'))
    DB.parent.mkdir(parents=True, exist_ok=True)
app = FastAPI(title='Альфа.Навигатор — демо API', version='1.1.0')
log = logging.getLogger('navigator')
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

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

def same(given: str, expected: str) -> bool:
    # compare_digest takes ASCII-only str, so compare UTF-8 bytes: a Cyrillic password is just wrong, not an error
    return secrets.compare_digest(given.encode(), expected.encode())

def require(role: str):
    def dependency(authorization: str | None = Header(None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Требуется вход')
        token = authorization[7:]
        expected = DEMO_CLIENT_KEY if role == 'client' else DEMO_BANK_KEY
        if not same(token, expected):
            raise HTTPException(403, 'Недостаточно прав')
        return role
    return dependency

# Milliseconds spent in SQL during the current request, for the request log.
_sql_ms = ContextVar('sql_ms', default=None)

@app.middleware('http')
async def observe(request: Request, call_next):
    request_id = re.sub(r'[^A-Za-z0-9-]', '', request.headers.get('x-request-id', ''))[:64] or uuid4().hex[:16]
    sql = [0.0]
    _sql_ms.set(sql)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception(json.dumps({'event': 'unhandled_error', 'request_id': request_id, 'path': request.url.path}))
        response = JSONResponse({'detail': 'Внутренняя ошибка сервера', 'request_id': request_id}, status_code=500)
    path = request.url.path
    response.headers['X-Request-ID'] = request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith('/static/'):
        # index.html links static files with ?v=<content hash>, so a versioned URL never changes
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if 'v' in request.query_params else 'public, max-age=3600'
    else:
        response.headers['Cache-Control'] = 'no-store'
    if path.startswith('/api/'):
        log.info(json.dumps({'event': 'request', 'request_id': request_id, 'method': request.method, 'path': path,
                             'status': response.status_code, 'ms': round((time.perf_counter() - started) * 1000, 1),
                             'sql_ms': round(sql[0], 1)}))
    return response

FIELD_NAMES = {'company':'Компания', 'purpose':'Цель', 'amount':'Сумма', 'months':'Срок', 'revenue':'Месячная выручка',
               'available_cashflow':'Свободный денежный поток', 'existing_payments':'Платежи по действующим долгам',
               'overdue':'Просрочки', 'reason':'Комментарий', 'note':'Комментарий', 'fields':'Поля для уточнения',
               'password':'Пароль', 'role':'Роль', 'decision':'Решение', 'offer':'Вариант условий', 'annual_rate':'Ставка',
               'prepay_month':'Месяц досрочного погашения', 'prepay_amount':'Сумма досрочного погашения',
               'seasonality':'Сезонное падение', 'idempotency-key':'Idempotency-Key', 'days':'Период'}

def number(n):
    return f'{n:,}'.replace(',', ' ') if isinstance(n, int) else str(n)

def validation_message(error) -> str:
    ctx = error.get('ctx') or {}
    messages = {
        'missing': lambda: 'обязательное поле',
        'greater_than_equal': lambda: f"не меньше {number(ctx['ge'])}",
        'greater_than': lambda: f"больше {number(ctx['gt'])}",
        'less_than_equal': lambda: f"не больше {number(ctx['le'])}",
        'less_than': lambda: f"меньше {number(ctx['lt'])}",
        'string_too_short': lambda: f"не короче {ctx['min_length']} символов",
        'string_too_long': lambda: f"не длиннее {ctx['max_length']} символов",
        'too_short': lambda: f"нужно выбрать хотя бы {ctx['min_length']}",
        'int_parsing': lambda: 'нужно целое число',
        'int_type': lambda: 'нужно целое число',
        'int_from_float': lambda: 'нужно целое число',
        'float_parsing': lambda: 'нужно число',
        'bool_parsing': lambda: 'нужно «да» или «нет»',
        'literal_error': lambda: f"допустимые значения: {ctx['expected']}",
        'json_invalid': lambda: 'некорректный JSON',
    }
    try:
        return messages[error['type']]()
    except KeyError:
        return error['msg']

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """One error shape for the whole API: a readable detail plus per-field messages."""
    errors = []
    for e in exc.errors():
        field = next((str(x) for x in reversed(e['loc']) if isinstance(x, str) and x not in ('body', 'query', 'header')), None)
        errors.append({'field': field, 'message': validation_message(e)})
    detail = '; '.join(f"{FIELD_NAMES.get(e['field'], e['field'])}: {e['message']}" if e['field'] else e['message'] for e in errors)
    return JSONResponse({'detail': 'Проверьте данные. ' + detail, 'errors': errors}, status_code=422)

class LoginInput(BaseModel):
    role: Literal['client','bank']
    password: str = Field(max_length=200)

class LoginOut(BaseModel):
    role: str
    access_token: str
    token_type: str
    warning: str

@app.post('/api/demo/login', response_model=LoginOut)
def login(payload: LoginInput):
    if not same(payload.password, DEMO_PASSWORDS[payload.role]):
        raise HTTPException(401, 'Неверный демонстрационный пароль')
    return {'role': payload.role, 'access_token': DEMO_CLIENT_KEY if payload.role == 'client' else DEMO_BANK_KEY, 'token_type':'bearer', 'warning':'Учётные записи являются демонстрационными, не production IAM'}

app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

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
            if (spent := _sql_ms.get()) is not None:
                spent[0] += (time.perf_counter() - started) * 1000

_open_connection = ContextVar('open_connection', default=None)

@contextmanager
def conn():
    """Open a connection, or reuse the one already open in this context so nested calls share a transaction."""
    if _open_connection.get() is not None:
        yield _open_connection.get()
        return
    if DATABASE_URL:
        # prepare_threshold=None: Neon's pooled endpoint runs PgBouncer in transaction mode.
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=5) as raw:
            token = _open_connection.set(Db(raw, postgres=True))
            try:
                yield _open_connection.get()
            finally:
                _open_connection.reset(token)
        return
    raw = sqlite3.connect(DB, timeout=10, isolation_level=None)
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
_clock = ContextVar('clock', default=None)

def utcnow() -> datetime:
    return _clock.get() or datetime.now(timezone.utc)

def now() -> str:
    return utcnow().isoformat()

STATUSES = ('submitted', 'needs_info', 'clarified', 'approved', 'counteroffer', 'rejected', 'accepted', 'withdrawn', 'expired')

def columns(c, table):
    if DATABASE_URL:
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
    event_id = 'BIGSERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
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
    if DATABASE_URL:
        # NOT VALID checks new rows only, so legacy data can never block the deploy
        c.execute('ALTER TABLE events ADD CONSTRAINT events_application_fk FOREIGN KEY (application_id) REFERENCES applications (id) NOT VALID')
        allowed = ', '.join(f"'{s}'" for s in STATUSES)
        c.execute(f'ALTER TABLE applications ADD CONSTRAINT applications_status_check CHECK (status IN ({allowed})) NOT VALID')

MIGRATIONS = [schema_v1, schema_v2]
SCHEMA_VERSION = len(MIGRATIONS)

def migrate():
    with conn() as c:
        if DATABASE_URL:
            # Serverless instances may cold-start at once; serialize migrations.
            c.execute('SELECT pg_advisory_xact_lock(20261025)')
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        done = {r['version'] for r in c.execute('SELECT version FROM schema_migrations')}
        for number_, step in enumerate(MIGRATIONS, 1):
            if number_ not in done:
                step(c)
                c.execute('INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)', (number_, now()))
                log.info(json.dumps({'event': 'migrated', 'version': number_}))

# MIGRATE_ON_START=0 leaves migrations to an explicit `python -m app.main migrate` step; /api/ready reports the lag.
if os.getenv('MIGRATE_ON_START', '1') == '1':
    migrate()

POLICY_VERSION = 'demo-rules-v1'
# Reference rate: scoring measures the debt load at this rate, then the score sets the actual price.
RATE = 0.22
# (minimum score, annual rate). Auto-approvals and offers never price above RATE,
# so the load the scoring checked stays an upper bound; 26% is reachable only by a manual approval.
RATE_TIERS = [(85, 0.18), (70, 0.20), (65, 0.22), (0, 0.26)]

def rate_for(score) -> float:
    return next(rate for minimum, rate in RATE_TIERS if (score or 0) >= minimum)

def payment(amount:int, months:int, rate:float=RATE) -> int:
    r = rate / 12
    return round(amount * r / (1 - (1 + r) ** (-months)))

def amortization(amount:int, months:int, rate:float=RATE, prepay=None):
    """Annuity schedule; the last payment absorbs rounding so the balance closes at zero.

    prepay=(month, amount, mode) adds an early repayment on top of that month's payment;
    afterwards the payment stays and the term shrinks ('term'), or the payment is recalculated
    for the remaining months ('payment')."""
    r = rate / 12
    monthly = payment(amount, months, rate)
    balance = amount
    rows = []
    m = 0
    while balance > 0:
        m += 1
        interest = round(balance * r)
        principal = balance if m >= months else min(balance, monthly - interest)
        extra = min(prepay[1], balance - principal) if prepay and m == prepay[0] else 0
        balance -= principal + extra
        rows.append({'month':m, 'payment':principal+interest, 'principal':principal, 'interest':interest, 'prepayment':extra, 'balance':balance})
        if extra and prepay[2] == 'payment' and m < months:
            monthly = payment(balance, months - m, rate)
    return rows

class LoanTerms(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    revenue: int = Field(gt=0, le=1_000_000_000)
    available_cashflow: int = Field(gt=0, le=1_000_000_000, description='Выручка за вычетом операционных расходов и налогов, до платежей по действующим кредитам')
    existing_payments: int = Field(ge=0, le=1_000_000_000, description='Ежемесячные платежи по действующим кредитам и лизингу')
    overdue: bool = False

class ApplicationInput(LoanTerms):
    company: str = Field(min_length=2, max_length=120)
    purpose: str = Field(min_length=3, max_length=200)
    previous_id: str | None = Field(None, max_length=20, description='Заявка, вместо которой подаётся эта (повторная подача)')

CLARIFY_FIELDS = {'available_cashflow':'свободный денежный поток', 'revenue':'месячная выручка',
                  'existing_payments':'платежи по действующим долгам', 'overdue':'наличие просрочек'}
ClarifyField = Literal['available_cashflow','revenue','existing_payments','overdue']

class RequestInfo(BaseModel):
    reason: str = Field(min_length=5, max_length=300)
    fields: list[ClarifyField] = Field(default_factory=lambda: ['available_cashflow'], min_length=1)

class Clarification(BaseModel):
    note: str = Field(min_length=3, max_length=300)
    available_cashflow: int | None = Field(None, gt=0, le=1_000_000_000)
    revenue: int | None = Field(None, gt=0, le=1_000_000_000)
    existing_payments: int | None = Field(None, ge=0, le=1_000_000_000)
    overdue: bool | None = None

def event(c, id, actor, action, description, data=None, at=None):
    c.execute('INSERT INTO events (application_id, occurred_at, actor, action, description, data) VALUES (?,?,?,?,?,?)',
              (id, at or now(), actor, action, description, json.dumps(data, ensure_ascii=False) if data else None))

OFFER_VALID_DAYS = float(os.getenv('OFFER_VALID_DAYS', '7'))
OPEN_OFFER = ('approved', 'counteroffer')

def offer_deadline() -> str:
    return (utcnow() + timedelta(days=OFFER_VALID_DAYS)).isoformat()

def expire_offers(c, id=None):
    """Close offers past their deadline; runs lazily before applications are read or changed."""
    sql, params = "SELECT id, offer_expires_at FROM applications WHERE status IN ('approved','counteroffer') AND offer_expires_at < ?", (now(),)
    if id:
        sql, params = sql + ' AND id=?', params + (id,)
    for row in c.execute(sql, params).fetchall():
        deadline = row['offer_expires_at']
        # the status guard keeps concurrent requests from logging the expiry twice
        if c.execute("UPDATE applications SET status='expired', updated_at=?, version=version+1 WHERE id=? AND status IN ('approved','counteroffer')", (deadline, row['id'])).rowcount:
            event(c, row['id'], 'system', 'expired', 'Срок действия предложения истёк', at=deadline)

def get_row(c, id):
    expire_offers(c, id)
    row = c.execute('SELECT * FROM applications WHERE id=?', (id,)).fetchone()
    if row is None: raise HTTPException(404, 'Заявка не найдена')
    return dict(row)

def enriched(c, id):
    row = get_row(c, id)
    row['events'] = [dict(x) for x in c.execute('SELECT occurred_at, actor, action, description, data FROM events WHERE application_id=? ORDER BY id', (id,))]
    for e in row['events']:
        e['data'] = json.loads(e['data']) if e['data'] else None
    if row['explanation']: row['explanation'] = json.loads(row['explanation'])
    if row.get('info_request'): row['info_request'] = json.loads(row['info_request'])
    row['waiting_on'] = WAITING_ON.get(row['status'])
    return row

STALE = 'Заявку уже изменили в другой вкладке или другим участником. Проверьте актуальные данные и повторите действие.'

def expected_version(if_match: str | None = Header(None, description='Версия заявки (поле version), которую видел пользователь. Если заявка с тех пор изменилась, ответ 409.')):
    if if_match is None:
        return None
    try:
        return int(if_match.removeprefix('W/').strip('"'))
    except ValueError:
        raise HTTPException(422, 'If-Match должен содержать номер версии заявки')

def load_for_change(c, id, expected):
    row = get_row(c, id)
    if expected is not None and expected != row['version']:
        raise HTTPException(409, STALE)
    return row

def transition(c, row, actor, action, description, data=None, **changes):
    """Apply a change only if nobody changed the application since `row` was read.

    The version check makes check-then-write atomic on both engines: of two concurrent commands
    the second one finds a new version, changes nothing and gets 409. The event is written only
    after the update succeeded, in the same transaction."""
    changes['updated_at'] = now()
    assignments = ', '.join(f'{name}=?' for name in changes)  # names come from code, never from input
    if not c.execute(f'UPDATE applications SET {assignments}, version=version+1 WHERE id=? AND version=?',
                     (*changes.values(), row['id'], row['version'])).rowcount:
        raise HTTPException(409, STALE)
    event(c, row['id'], actor, action, description, data)

APPROVE_SCORE = 65
# Offers and advice keep all payments within 60% of free cash flow, stricter than the approval threshold.
OFFER_LOAD = 0.6
BASE_SCORE = 85

def percent(x):
    return f'{x * 100:.1f}'.replace('.', ',') + '%'

def assess(row):
    """Illustrative deterministic policy; NOT a real banking credit decision.

    Returns the score, the payment at the reference rate, the debt load and how each factor moved the score."""
    monthly = payment(row['amount'], row['months'])
    load = (monthly + row['existing_payments']) / row['available_cashflow']
    if load > 0.75:
        factors = [('Высокая доля платежей в свободном денежном потоке', -40, f'нагрузка {percent(load)} выше 75%')]
    elif load > 0.6:
        factors = [('Повышенная долговая нагрузка', -20, f'нагрузка {percent(load)} выше 60%')]
    else:
        factors = [('Долговая нагрузка в пределах демонстрационного порога', 0, f'нагрузка {percent(load)}, порог 60%')]
    if row['overdue']:
        factors.append(('В синтетической истории указаны просрочки', -35, None))
    else:
        factors.append(('Просрочек в синтетических данных нет', 0, None))
    ratio = f"сумма равна {row['amount'] / row['revenue']:.1f}".replace('.', ',') + ' мес. выручки'
    if row['amount'] > row['revenue'] * 4:
        factors.append(('Запрос существенно превышает месячную выручку', -15, ratio + ', порог 4'))
    else:
        factors.append(('Сумма сопоставима с заявленной выручкой', 0, ratio))
    contributions = [{'factor':f, 'points':p, 'detail':d} for f, p, d in factors]
    return max(0, BASE_SCORE + sum(p for _, p, _ in factors)), monthly, load, contributions

def assessment(row):
    """Risk view of the requested terms: the load is measured at the reference rate, whatever the final price."""
    score, monthly, load, contributions = assess(row)
    return {'score':score, 'base_score':BASE_SCORE, 'reference_rate':RATE, 'monthly_payment':monthly,
            'payment_to_cashflow':round(load, 3), 'contributions':contributions}

def affordable(row):
    score, _, load, _ = assess(row)
    return score >= APPROVE_SCORE and load <= OFFER_LOAD

def rub(n):
    return f'{n:,}'.replace(',', ' ') + ' ₽'

def offer(kind, amount, months, rate):
    total = sum(x['payment'] for x in amortization(amount, months, rate))
    return {'id':kind, 'amount':amount, 'months':months, 'annual_rate':rate, 'monthly_payment':payment(amount, months, rate), 'total_paid':total, 'overpayment':total-amount}

def issue(terms, basis):
    """Make terms a binding offer: the client accepts exactly these numbers, even if tariffs change later."""
    return {**terms, 'offer_id':uuid4().hex[:12], 'policy_version':POLICY_VERSION, 'issued_at':now(), 'basis':basis}

def priced(kind, row):
    score, _, load, _ = assess(row)
    return {**offer(kind, row['amount'], row['months'], rate_for(score)), 'score':score, 'payment_to_cashflow':round(load, 3)}

def find_offers(row):
    """Alternative terms the bank can offer instead of the requested ones."""
    offers = []
    # largest 100k-increment amount for the same term; avoid arbitrary approvals
    affordable_payment = max(0, OFFER_LOAD * row['available_cashflow'] - row['existing_payments'])
    r = RATE/12
    limit = int(affordable_payment * (1-(1+r)**(-row['months']))/r//100_000)*100_000
    if 100_000 <= limit < row['amount'] and affordable({**row, 'amount':limit}):
        offers.append(priced('amount', {**row, 'amount':limit}))
    # shortest longer term for the requested amount
    months = next((m for m in range(row['months']+1, 61) if affordable({**row, 'months':m})), None)
    if months:
        offers.append(priced('term', {**row, 'months':months}))
    return offers

def how_to_improve(row, offers):
    """What the client could change to get the requested loan approved."""
    if row['overdue']:
        return ['Погасить просрочки: с ними одобрение по демо-правилам невозможно при любых условиях']
    tips = []
    for o in offers:
        if o['id'] == 'amount':
            tips.append(f"Уменьшить сумму до {rub(o['amount'])} на тот же срок")
        else:
            tips.append(f"Увеличить срок до {o['months']} мес. на ту же сумму")
    monthly = payment(row['amount'], row['months'])
    max_existing = int(OFFER_LOAD * row['available_cashflow'] - monthly)
    if 0 <= max_existing < row['existing_payments'] and affordable({**row, 'existing_payments':max_existing}):
        tips.append(f"Снизить платежи по действующим долгам до {rub(max_existing)}/мес.")
    need_cashflow = math.ceil((monthly + row['existing_payments']) / OFFER_LOAD)
    if need_cashflow > row['available_cashflow'] and affordable({**row, 'available_cashflow':need_cashflow}):
        tips.append(f"Подтвердить свободный денежный поток от {rub(need_cashflow)}/мес.")
    return tips

def score_application(row):
    """Decision and explanation. `assessment` is the risk calculation at the reference rate;
    `offer` (approved) or `offers` (counteroffer) are the prices the client is offered."""
    risk = assessment(row)
    score = risk['score']
    offers = [] if score >= APPROVE_SCORE or row['overdue'] else find_offers(row)
    if score >= APPROVE_SCORE: decision = 'approved'
    elif offers: decision = 'counteroffer'
    else: decision = 'rejected'
    alternative = next((o['amount'] for o in offers if o['id'] == 'amount'), None)
    approved = offer('requested', row['amount'], row['months'], rate_for(score)) if decision == 'approved' else None
    explanation = {'policy_version':POLICY_VERSION, 'reference_rate':RATE, 'rate_tiers':RATE_TIERS,
                   'factors':[x['factor'] for x in risk['contributions']], 'assessment':risk,
                   # requested terms that were not approved are shown at the reference rate the scoring used
                   'requested':approved or offer('requested', row['amount'], row['months'], RATE),
                   'offer':approved, 'offers':offers,
                   'how_to_improve':[] if decision == 'approved' else how_to_improve(row, offers),
                   'disclaimer':'Демонстрационная модель на синтетических данных, не банковское решение.'}
    return score, decision, alternative, explanation

def validate_any(authorization: str | None):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Требуется вход')
    candidate = authorization[7:]
    if not (same(candidate, DEMO_CLIENT_KEY) or same(candidate, DEMO_BANK_KEY)):
        raise HTTPException(403, 'Недостаточно прав')

class Flexible(BaseModel):
    # explanations of older applications may carry extra or missing keys; keep whatever is stored
    model_config = ConfigDict(extra='allow')

class Terms(Flexible):
    id: str = Field(description='requested — запрошенные условия, amount — меньше сумма, term — дольше срок')
    amount: int
    months: int
    annual_rate: float | None = Field(None, description='Фактическая ставка этих условий')
    monthly_payment: int | None = None
    total_paid: int | None = None
    overpayment: int | None = None
    offer_id: str | None = Field(None, description='Есть только у выданного предложения; его условия не меняются')
    policy_version: str | None = None
    issued_at: str | None = None
    basis: Literal['auto','manual'] | None = Field(None, description='auto — по скорингу, manual — решение сотрудника')
    score: int | None = Field(None, description='Оценка риска этих условий (для альтернатив)')
    payment_to_cashflow: float | None = Field(None, description='Нагрузка этих условий по базовой ставке (для альтернатив)')

class Contribution(BaseModel):
    factor: str
    points: int = Field(description='Изменение балла: 0 или отрицательное число')
    detail: str | None = None

class Assessment(BaseModel):
    score: int
    base_score: int
    reference_rate: float = Field(description='Базовая ставка, по которой считается нагрузка')
    monthly_payment: int = Field(description='Платёж по запрошенным условиям по базовой ставке')
    payment_to_cashflow: float
    contributions: list[Contribution]

class ManualOverride(Flexible):
    decision: str
    reason: str
    auto_decision: str | None = None
    at: str | None = None
    author: str | None = None

class Explanation(Flexible):
    policy_version: str | None = None
    reference_rate: float | None = None
    rate_tiers: list[tuple[int, float]] | None = None
    factors: list[str] = []
    assessment: Assessment | None = Field(None, description='Расчёт риска по запрошенным условиям')
    requested: Terms | None = Field(None, description='Запрошенные условия; если не одобрены — по базовой ставке')
    offer: Terms | None = Field(None, description='Выданное предложение на запрошенных условиях (одобрение)')
    offers: list[Terms] = Field([], description='Выданные альтернативные предложения (контрпредложение)')
    how_to_improve: list[str] = []
    accepted_terms: Terms | None = None
    manual_override: ManualOverride | None = None
    disclaimer: str | None = None

class InfoRequest(BaseModel):
    # requests made before field selection existed asked for the cash flow only
    fields: list[str] = ['available_cashflow']
    reason: str | None = None

class Event(BaseModel):
    occurred_at: str
    actor: str
    action: str
    description: str
    data: dict[str, Any] | None = None

WaitingOn = Literal['bank','client'] | None

class Application(BaseModel):
    id: str
    created_at: str
    updated_at: str
    company: str
    purpose: str
    amount: int
    months: int
    available_cashflow: int
    revenue: int
    existing_payments: int
    overdue: bool
    status: str
    version: int = Field(description='Растёт с каждым изменением; передавайте в If-Match')
    waiting_on: WaitingOn = None
    score: int | None = None
    decision: str | None = Field(None, description='Автоматическое решение скоринга')
    alternative_amount: int | None = None
    explanation: Explanation | None = None
    offer_expires_at: str | None = None
    info_request: InfoRequest | None = None
    previous_id: str | None = None
    events: list[Event]

class ListItem(BaseModel):
    id: str
    company: str
    amount: int
    months: int
    status: str
    created_at: str
    updated_at: str
    offer_expires_at: str | None = None
    version: int
    waiting_on: WaitingOn = None
    in_status_minutes: int
    sla_due_at: str | None = None
    sla_breached: bool

# exclude_unset: optional explanation keys appear only when stored, not as nulls
APPLICATION = dict(response_model=Application, response_model_exclude_unset=True)

@app.get('/api/my-applications', response_model=list[ListItem])
def my_applications(role: str = Depends(require('client'))):
    # In demo there is exactly one synthetic customer; this is NOT multi-tenant isolation.
    at = datetime.now(timezone.utc)
    with conn() as c:
        expire_offers(c)
        rows = c.execute('SELECT id,company,amount,months,status,created_at,updated_at,offer_expires_at,version FROM applications ORDER BY created_at DESC')
        return [queue_item(dict(r), at) for r in rows]

class Precheck(Explanation):
    score: int
    decision: str

@app.post('/api/precheck', response_model=Precheck, response_model_exclude_unset=True)
def precheck(payload: LoanTerms, role: str = Depends(require('client'))):
    """Same rules as the bank's scoring, without saving anything."""
    score, decision, _, explanation = score_application(payload.model_dump())
    return {'score':score, 'decision':decision, **explanation,
            'disclaimer':'Предварительная оценка по демо-правилам, итоговое решение принимает банк.'}

class ScheduleInput(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    annual_rate: float = Field(RATE, ge=0.01, le=1)
    prepay_month: int | None = Field(None, ge=1, le=60, description='Месяц досрочного погашения')
    prepay_amount: int = Field(0, ge=0, le=20_000_000)
    prepay_mode: Literal['term','payment'] = Field('term', description='term — сократить срок, payment — уменьшить платёж')

class ScheduleRow(BaseModel):
    month: int
    payment: int
    principal: int
    interest: int
    prepayment: int
    balance: int

class ScheduleSummary(BaseModel):
    months: int
    total_paid: int
    overpayment: int

class ScheduleOut(ScheduleSummary):
    annual_rate: float
    monthly_payment: int
    rows: list[ScheduleRow]
    baseline: ScheduleSummary | None = Field(None, description='Тот же кредит без досрочного погашения')
    savings: int | None = None
    monthly_after: int | None = Field(None, description='Платёж в месяц после досрочного погашения')

def schedule_summary(rows):
    interest = sum(x['interest'] for x in rows)
    return {'months':len(rows), 'total_paid':sum(x['payment'] + x['prepayment'] for x in rows), 'overpayment':interest}

@app.post('/api/schedule', response_model=ScheduleOut)
def schedule(payload: ScheduleInput, authorization: str | None = Header(None)):
    validate_any(authorization)
    monthly = payment(payload.amount, payload.months, payload.annual_rate)
    result = {'annual_rate':payload.annual_rate, 'monthly_payment':monthly}
    if payload.prepay_month and payload.prepay_amount:
        if payload.prepay_month >= payload.months:
            raise HTTPException(422, 'Досрочное погашение возможно до последнего месяца')
        rows = amortization(payload.amount, payload.months, payload.annual_rate, (payload.prepay_month, payload.prepay_amount, payload.prepay_mode))
        baseline = schedule_summary(amortization(payload.amount, payload.months, payload.annual_rate))
        after = rows[payload.prepay_month]['payment'] if len(rows) > payload.prepay_month else 0
        result |= {'baseline':baseline, 'savings':baseline['overpayment'] - schedule_summary(rows)['overpayment'], 'monthly_after':after}
    else:
        rows = amortization(payload.amount, payload.months, payload.annual_rate)
    return result | schedule_summary(rows) | {'rows':rows}

class SimulationInput(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    available_cashflow: int = Field(gt=0, le=1_000_000_000)
    existing_payments: int = Field(ge=0, le=1_000_000_000)
    seasonality: int = Field(ge=0, le=60, description='Процент сезонного падения свободного потока')
    start_month: int | None = Field(None, ge=1, le=12, description='Календарный месяц первого платежа (1 — январь); по умолчанию следующий месяц')

class SimulationMonth(BaseModel):
    month: int = Field(description='Номер месяца с начала кредита')
    calendar_month: int = Field(description='Календарный месяц, 1 — январь')
    winter: bool
    cashflow: int
    loan_payment: int = Field(description='Платёж по графику; 0 после погашения кредита')
    existing_payments: int
    balance_after_debt: int

class SimulationOut(BaseModel):
    annual_rate: float = Field(description='Базовая ставка симуляции; итоговая ставка зависит от скоринга')
    monthly_payment: int
    base_load: float
    stress_load: float
    start_month: int
    timeline: list[SimulationMonth]
    safe_under_stress: bool
    disclaimer: str

WINTER = (1, 2, 11, 12)

@app.post('/api/simulate', response_model=SimulationOut)
def simulate(payload: SimulationInput, authorization: str | None = Header(None)):
    """Twelve months from the first payment; the loan payment comes from the amortization schedule, so it stops when the loan is repaid."""
    validate_any(authorization)
    rows = amortization(payload.amount, payload.months)
    monthly = rows[0]['payment']
    base = (monthly+payload.existing_payments)/payload.available_cashflow
    seasonal_cash = payload.available_cashflow*(1-payload.seasonality/100)
    stress = (monthly+payload.existing_payments)/seasonal_cash
    start = payload.start_month or utcnow().month % 12 + 1
    timeline = []
    for i in range(12):
        calendar = (start - 1 + i) % 12 + 1
        # Illustrative stress scenario in winter months, not a cash flow prediction.
        winter = calendar in WINTER
        flow = round(seasonal_cash if winter else payload.available_cashflow)
        loan = rows[i]['payment'] if i < len(rows) else 0
        timeline.append({'month':i+1, 'calendar_month':calendar, 'winter':winter, 'cashflow':flow, 'loan_payment':loan,
                         'existing_payments':payload.existing_payments, 'balance_after_debt':flow-loan-payload.existing_payments})
    return {'annual_rate':RATE, 'monthly_payment':monthly, 'base_load':round(base,4), 'stress_load':round(stress,4), 'start_month':start,
            'timeline':timeline, 'safe_under_stress':stress<=OFFER_LOAD, 'disclaimer':'Условная модель, а не кредитное решение или финансовый прогноз.'}

@app.get('/api/mock/company/{inn}')
def mock_company(inn: str, authorization: str | None = Header(None)):
    validate_any(authorization)
    if len(inn) not in (10,12) or not inn.isdigit():
        raise HTTPException(422, 'Синтетический ИНН должен содержать 10 или 12 цифр')
    return {'inn':inn, 'source':'LOCAL_SYNTHETIC_FNS_STUB', 'verified':True, 'company':'ООО Демонстрационный бизнес', 'note':'Не является запросом в ФНС или подтверждением существования компании'}

STATIC_FILES = sorted(p for p in (ROOT / 'static').rglob('*') if p.is_file())

@lru_cache(maxsize=1)
def index_html(stamp):
    """index.html with ?v=<content hash> on static links; rebuilt only when a static file changes."""
    digest = hashlib.sha256(b''.join(p.read_bytes() for p in STATIC_FILES if p.name != 'index.html')).hexdigest()[:10]
    return (ROOT / 'static' / 'index.html').read_text(encoding='utf-8').replace('__ASSET_VERSION__', digest)

@app.get('/', response_class=HTMLResponse, include_in_schema=False)
def index(): return index_html(max(p.stat().st_mtime_ns for p in STATIC_FILES))

class Health(BaseModel):
    status: str
    database: Literal['postgres','sqlite']

@app.get('/api/health', response_model=Health)
def health():
    """Liveness: the process answers. Whether it can serve requests is /api/ready."""
    return {'status':'ok', 'database':'postgres' if DATABASE_URL else 'sqlite'}

class Readiness(Health):
    schema_version: int | None = None
    detail: str | None = None

@app.get('/api/ready', response_model=Readiness, response_model_exclude_none=True, responses={503: {'model': Readiness, 'description': 'База недоступна или схема не обновлена'}})
def ready():
    """Readiness: the database answers and its schema is current; 503 otherwise."""
    engine = 'postgres' if DATABASE_URL else 'sqlite'
    try:
        with conn() as c:
            c.execute('SELECT 1').fetchone()
            version = c.execute('SELECT MAX(version) AS v FROM schema_migrations').fetchone()['v'] if columns(c, 'schema_migrations') else None
    except Exception as e:
        log.warning(json.dumps({'event': 'not_ready', 'error': type(e).__name__}))
        return JSONResponse({'status':'unavailable', 'database':engine, 'detail':'База данных недоступна'}, status_code=503)
    if version != SCHEMA_VERSION:
        return JSONResponse({'status':'unavailable', 'database':engine, 'schema_version':version,
                             'detail':f"Схема базы {version or 'не создана'}, приложению нужна {SCHEMA_VERSION}: выполните миграции"}, status_code=503)
    return {'status':'ready', 'database':engine, 'schema_version':version}

BANK_SLA_HOURS = float(os.getenv('BANK_SLA_HOURS', '24'))
# Whose move it is; every status change also sets updated_at, so it marks when the wait began.
WAITING_ON = {'submitted':'bank', 'clarified':'bank', 'needs_info':'client', 'approved':'client', 'counteroffer':'client'}

def queue_item(row, at):
    since = datetime.fromisoformat(row['updated_at'])
    row['waiting_on'] = WAITING_ON.get(row['status'])
    row['in_status_minutes'] = int((at - since).total_seconds() // 60)
    due = since + timedelta(hours=BANK_SLA_HOURS) if row['waiting_on'] == 'bank' else None
    row['sla_due_at'] = due and due.isoformat()
    row['sla_breached'] = bool(due and at > due)
    return row

@app.get('/api/applications', response_model=list[ListItem])
def list_applications(role: str = Depends(require('bank'))):
    at = datetime.now(timezone.utc)
    with conn() as c:
        expire_offers(c)
        rows = c.execute('SELECT id, company, amount, months, status, created_at, updated_at, offer_expires_at, version FROM applications ORDER BY created_at DESC')
        return [queue_item(dict(row), at) for row in rows]

NEGATIVE_FACTORS = {'Высокая доля платежей в свободном денежном потоке', 'Повышенная долговая нагрузка',
                    'В синтетической истории указаны просрочки', 'Запрос существенно превышает месячную выручку'}

def minutes_between(start:str, end:str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60

def average(values):
    return round(sum(values) / len(values)) if values else None

def median(values):
    return round(statistics.median(values)) if values else None

def event_decision(e):
    """Decision recorded by an evaluated or manual_decision event."""
    if e['data']:
        return json.loads(e['data']).get('decision')
    # events logged before they carried structured data
    if e['action'] == 'manual_decision':
        return 'approved' if 'вручную одобрил' in e['description'] else 'rejected'
    found = re.search(r'результат: (\w+)', e['description'])
    return found and found.group(1)

def offered_terms(explanation):
    """The offer an approved application carries; older applications kept it only in `requested`."""
    return explanation.get('offer') or explanation.get('requested') or {}

class Analytics(BaseModel):
    period_days: int | None
    total: int
    by_status: dict[str, int] = Field(description='Текущие статусы заявок')
    funnel: list[dict[str, Any]] = Field(description='Исторические этапы: заявка учитывается, если хоть раз достигла этапа')
    auto_decisions: dict[str, int]
    manual: dict[str, int]
    offers: dict[str, int] = Field(description='Чем закончились заявки, получившие предложение')
    top_factors: list[dict[str, Any]]
    approved_rates: list[dict[str, Any]]
    accepted_rates: list[dict[str, Any]]
    avg_requested_amount: int | None
    avg_accepted_amount: int | None
    sla: dict[str, Any]

@app.get('/api/analytics', response_model=Analytics)
def analytics(days: int | None = Query(None, ge=1, le=3650, description='Когорта: заявки, поданные за последние N дней'),
              role: str = Depends(require('bank'))):
    at = now()
    since = (utcnow() - timedelta(days=days)).isoformat() if days else ''
    with conn() as c:
        expire_offers(c)
        apps = [dict(r) for r in c.execute('SELECT id, status, amount, score, decision, explanation FROM applications WHERE created_at >= ?', (since,))]
        events = [dict(r) for r in c.execute('''SELECT e.application_id, e.occurred_at, e.action, e.description, e.data FROM events e
                                                JOIN applications a ON a.id = e.application_id WHERE a.created_at >= ? ORDER BY e.id''', (since,))]
    for a in apps:
        a['explanation'] = json.loads(a['explanation']) if a['explanation'] else {}
        a['effective'] = (a['explanation'].get('manual_override') or {}).get('decision') or a['decision']
    scored = [a for a in apps if a['decision']]

    # Funnel stages come from history, so a later rejection does not erase an offer already made.
    reached = defaultdict(set)
    for e in events:
        id, action = e['application_id'], e['action']
        if action == 'evaluated':
            reached[id].add('scored')
            if event_decision(e) in OPEN_OFFER: reached[id].add('offered')
        elif action == 'manual_decision' and event_decision(e) == 'approved':
            reached[id].add('offered')
        elif action == 'accepted':
            reached[id].add('accepted')
    stage = lambda name: [a for a in apps if name in reached[a['id']]]
    offered = stage('offered')
    accepted = [a for a in apps if a['status'] == 'accepted']
    # applications accepted before accepted_terms existed have no recorded terms
    accepted_terms = [a['explanation']['accepted_terms'] for a in accepted if 'accepted_terms' in a['explanation']]
    factors = Counter(f for a in scored if a['decision'] != 'approved' for f in a['explanation'].get('factors', []) if f in NEGATIVE_FACTORS)
    approved_rates = Counter(offered_terms(a['explanation']).get('annual_rate') or rate_for(a['score']) for a in scored if a['effective'] == 'approved')

    # Bank response time: from submission or clarification to the bank's next move (every cycle),
    # and separately from submission to the bank's first move.
    waiting, responses, first, answered = {}, [], [], set()
    submitted = {e['application_id']: e['occurred_at'] for e in events if e['action'] == 'submitted'}
    for e in events:
        id = e['application_id']
        if e['action'] in ('submitted', 'clarified'):
            waiting[id] = e['occurred_at']
        elif e['action'] in ('evaluated', 'requested_info') and id in waiting:
            responses.append(minutes_between(waiting.pop(id), e['occurred_at']))
            if id not in answered and id in submitted:
                answered.add(id)
                first.append(minutes_between(submitted[id], e['occurred_at']))
        elif e['action'] == 'withdrawn':
            waiting.pop(id, None)
    limit = BANK_SLA_HOURS * 60
    open_waits = [minutes_between(start, at) for start in waiting.values()]

    return {
        'period_days': days,
        'total': len(apps),
        'by_status': dict(Counter(a['status'] for a in apps)),
        'funnel': [{'stage':'Подано', 'count':len(apps)}, {'stage':'Прошли скоринг', 'count':len(stage('scored'))},
                   {'stage':'Получили предложение', 'count':len(offered)}, {'stage':'Приняли условия', 'count':len(stage('accepted'))}],
        'auto_decisions': dict(Counter(a['decision'] for a in scored)),
        'manual': {'total': sum(1 for e in events if e['action'] == 'manual_decision'),
                   'approved_against_auto': sum(1 for a in scored if a['effective'] == 'approved' and a['decision'] != 'approved')},
        'offers': {'accepted': sum(1 for a in offered if a['status'] == 'accepted'), 'pending': sum(1 for a in offered if a['status'] in OPEN_OFFER),
                   'expired': sum(1 for a in offered if a['status'] == 'expired'), 'withdrawn': sum(1 for a in offered if a['status'] == 'withdrawn'),
                   'rejected': sum(1 for a in offered if a['status'] == 'rejected')},
        'top_factors': [{'factor':f, 'count':n} for f, n in factors.most_common()],
        'approved_rates': [{'rate':rate, 'count':n} for rate, n in sorted(approved_rates.items())],
        'accepted_rates': [{'rate':rate, 'count':n} for rate, n in sorted(Counter(t['annual_rate'] for t in accepted_terms if t.get('annual_rate')).items())],
        'avg_requested_amount': average([a['amount'] for a in apps]),
        'avg_accepted_amount': average([t['amount'] for t in accepted_terms]),
        'sla': {'hours': BANK_SLA_HOURS, 'responses': len(responses),
                'avg_minutes': average(responses), 'median_minutes': median(responses),
                'within_sla_pct': round(100 * sum(1 for m in responses if m <= limit) / len(responses)) if responses else None,
                'breached': sum(1 for m in responses if m > limit),
                'first_responses': len(first), 'first_avg_minutes': average(first), 'first_median_minutes': median(first),
                'waiting_now': len(open_waits), 'breached_now': sum(1 for m in open_waits if m > limit),
                'oldest_wait_minutes': round(max(open_waits)) if open_waits else None},
    }

@app.post('/api/applications', status_code=201, **APPLICATION)
def create_application(payload:ApplicationInput, response: Response,
                       idempotency_key: str | None = Header(None, max_length=100, description='Повтор запроса с тем же ключом вернёт уже созданную заявку'),
                       role: str = Depends(require('client'))):
    row = insert_application(payload, uuid4().hex[:10].upper(), idempotency_key)
    if row.pop('replayed', False):
        response.status_code = 200
        response.headers['Idempotency-Replayed'] = 'true'
    return row

def insert_application(payload:ApplicationInput, id:str, key:str|None=None):
    time_ = now()
    with conn() as c:
        if payload.previous_id and not c.execute('SELECT 1 FROM applications WHERE id=?', (payload.previous_id,)).fetchone():
            raise HTTPException(422, 'Предыдущая заявка не найдена')
        # a NULL key never conflicts, so only requests that carry a key are deduplicated
        inserted = c.execute('''INSERT INTO applications (id,created_at,updated_at,company,purpose,amount,months,available_cashflow,revenue,existing_payments,overdue,status,idempotency_key,previous_id)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (idempotency_key) DO NOTHING''',
          (id,time_,time_,payload.company,payload.purpose,payload.amount,payload.months,payload.available_cashflow,payload.revenue,payload.existing_payments,int(payload.overdue),'submitted',key,payload.previous_id)).rowcount
        if not inserted:
            existing = dict(c.execute('SELECT * FROM applications WHERE idempotency_key=?', (key,)).fetchone())
            if any(existing[f] != v for f, v in payload.model_dump(include={'company','purpose','amount','months'}).items()):
                raise HTTPException(422, 'Этот Idempotency-Key уже использован для другой заявки')
            return enriched(c, existing['id']) | {'replayed': True}
        resubmission = f' повторно, вместо заявки № {payload.previous_id}' if payload.previous_id else ''
        event(c,id,'client','submitted',f'Клиент отправил заявку{resubmission}', {'previous_id':payload.previous_id} if payload.previous_id else None)
        return enriched(c,id)

@app.get('/api/applications/{id}', **APPLICATION)
def get_application(id:str, authorization: str | None = Header(None)):
    validate_any(authorization)
    with conn() as c:return enriched(c,id)

def stamped(explanation, basis):
    """Issue the offers of a scoring result: each gets an id and is stored as is."""
    if explanation['offer']:
        explanation['offer'] = issue(explanation['offer'], basis)
    explanation['offers'] = [issue(o, basis) for o in explanation['offers']]
    return explanation

@app.post('/api/applications/{id}/evaluate', **APPLICATION)
def evaluate(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Оценка возможна только для новой или уточнённой заявки')
        score,decision,alternative,explanation=score_application(row)
        deadline=offer_deadline() if decision in OPEN_OFFER else None
        transition(c,row,'bank','evaluated',f'Скоринг {score}; результат: {decision}; правила {POLICY_VERSION}',{'decision':decision,'score':score},
                   status=decision,score=score,decision=decision,alternative_amount=alternative,
                   explanation=json.dumps(stamped(explanation,'auto'),ensure_ascii=False),offer_expires_at=deadline)
        return enriched(c,id)

@app.post('/api/applications/{id}/request-info', **APPLICATION)
def request_info(id:str, payload:RequestInfo, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Уточнение на этом этапе недоступно')
        fields=list(dict.fromkeys(payload.fields))
        transition(c,row,'bank','requested_info',f"{payload.reason} (уточнить: {', '.join(CLARIFY_FIELDS[f] for f in fields)})",{'fields':fields},
                   status='needs_info',info_request=json.dumps({'fields':fields,'reason':payload.reason},ensure_ascii=False))
        return enriched(c,id)

@app.post('/api/applications/{id}/clarify', **APPLICATION)
def clarify(id:str, payload:Clarification, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status']!='needs_info': raise HTTPException(409,'Уточнение не запрошено')
        # requests made before field selection existed asked for the cash flow only
        requested=json.loads(row['info_request'])['fields'] if row.get('info_request') else ['available_cashflow']
        values=payload.model_dump(exclude_none=True, exclude={'note'})
        if missing:=[CLARIFY_FIELDS[f] for f in requested if f not in values]:
            raise HTTPException(422, 'Заполните запрошенные поля: '+', '.join(missing))
        if extra:=[CLARIFY_FIELDS[f] for f in values if f not in requested]:
            raise HTTPException(422, 'Банк не запрашивал эти поля: '+', '.join(extra))
        shown=lambda f,v: ('есть' if v else 'нет') if f=='overdue' else rub(v)
        changed={f:v for f,v in values.items() if v!=(bool(row[f]) if f=='overdue' else row[f])}
        changes='; '.join(f'{CLARIFY_FIELDS[f]}: {shown(f,row[f])} → {shown(f,v)}' for f,v in changed.items()) or 'данные подтверждены без изменений'
        values={f:int(v) if f=='overdue' else v for f,v in values.items()}  # column names come from the ClarifyField whitelist
        transition(c,row,'client','clarified',f'{payload.note}. Изменения: {changes}',{'changed':sorted(changed)},
                   **values,status='clarified',info_request=None)
        return enriched(c,id)

class AcceptInput(BaseModel):
    offer: Literal['amount','term'] | None = Field(None, description='Вариант условий для контрпредложения; по умолчанию первый')
    offer_id: str | None = Field(None, max_length=40, description='Номер предложения, которое видел клиент; если банк его заменил, ответ 409')

@app.post('/api/applications/{id}/accept', **APPLICATION)
def accept(id:str, payload: AcceptInput | None = None, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in OPEN_OFFER:
            raise HTTPException(409, 'Принять можно только одобренные условия')
        explanation=json.loads(row['explanation'] or '{}')
        if row['status']=='approved':
            # the offer stored at approval, never repriced: tariffs may have changed since
            terms=offered_terms(explanation) or offer('requested', row['amount'], row['months'], rate_for(row['score']))
        else:
            # applications scored before offers existed only have alternative_amount
            offers=explanation.get('offers') or [offer('amount', row['alternative_amount'], row['months'], RATE)]
            choice=payload.offer if payload else None
            terms=next((o for o in offers if choice in (None, o['id'])), None)
            if terms is None: raise HTTPException(422, 'Такого варианта условий нет')
        if payload and payload.offer_id and payload.offer_id!=terms.get('offer_id'):
            raise HTTPException(409, 'Банк изменил предложение. Обновите карточку и проверьте условия.')
        explanation['accepted_terms']=terms
        transition(c,row,'client','accepted',f"Клиент принял условия: {rub(terms['amount'])} на {terms['months']} мес. под {terms['annual_rate']*100:g}%; подписание и выдача смоделированы",
                   {'offer_id':terms.get('offer_id'),'amount':terms['amount'],'months':terms['months'],'annual_rate':terms['annual_rate']},
                   status='accepted',explanation=json.dumps(explanation,ensure_ascii=False))
        return enriched(c,id)

@app.post('/api/applications/{id}/extend', **APPLICATION)
def extend_offer(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    """Give the client another OFFER_VALID_DAYS, reopening an expired offer."""
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('approved','counteroffer','expired'):
            raise HTTPException(409, 'Продлить можно только действующее или истёкшее предложение')
        override=json.loads(row['explanation'] or '{}').get('manual_override') or {}
        status=row['status'] if row['status']!='expired' else 'approved' if override.get('decision')=='approved' else row['decision']
        deadline=offer_deadline()
        until=datetime.fromisoformat(deadline).strftime('%d.%m.%Y')
        transition(c,row,'bank','extended',f'Банк продлил предложение до {until}',{'until':deadline},status=status,offer_expires_at=deadline)
        return enriched(c,id)

@app.post('/api/applications/{id}/withdraw', **APPLICATION)
def withdraw(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','needs_info','clarified','approved','counteroffer'):
            raise HTTPException(409, 'Эту заявку уже нельзя отозвать')
        transition(c,row,'client','withdrawn','Клиент отозвал заявку',status='withdrawn')
        return enriched(c,id)

class ManualDecision(BaseModel):
    decision: Literal['approved','rejected']
    reason: str = Field(min_length=10, max_length=300)

@app.post('/api/applications/{id}/decision', **APPLICATION)
def manual_decision(id:str, payload:ManualDecision, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    """Analyst overrides the automatic result; the auto decision stays in the decision column."""
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('approved','counteroffer','rejected'):
            raise HTTPException(409, 'Ручное решение возможно только после скоринга и до принятия условий')
        if row['status']==payload.decision:
            raise HTTPException(409, 'Заявка уже в этом статусе')
        explanation=json.loads(row['explanation'])
        if payload.decision=='approved':
            # a new offer on the requested terms, priced by the score: 26% when the score is below auto-approval
            terms=offer('requested', row['amount'], row['months'], rate_for(row['score']))
            explanation['requested']=terms
            explanation['offer']=issue(terms,'manual')
        else:
            explanation['offer']=None
        # the demo has one shared bank role, so the author is the role, not a named employee
        explanation['manual_override']={'decision':payload.decision,'reason':payload.reason,'auto_decision':row['decision'],'at':now(),'author':'bank'}
        deadline=offer_deadline() if payload.decision=='approved' else None
        verdict='одобрил' if payload.decision=='approved' else 'отклонил'
        auto={'approved':'одобрение','counteroffer':'другие условия','rejected':'отказ'}.get(row['decision'], row['decision'])
        transition(c,row,'bank','manual_decision',f'Сотрудник банка вручную {verdict} заявку (автоскоринг: {auto}): {payload.reason}',
                   {'decision':payload.decision,'auto_decision':row['decision'],'offer_id':(explanation['offer'] or {}).get('offer_id')},
                   status=payload.decision,explanation=json.dumps(explanation,ensure_ascii=False),offer_expires_at=deadline)
        return enriched(c,id)

# Synthetic history for demos: (company, purpose, terms, hours since submission, steps).
# A step is (hours after the previous one, action, *arguments); every step runs the real endpoint.
DEMO_PREFIX = 'DEMO'  # real ids are hex, so they never start with it
SMALL = dict(revenue=900_000, available_cashflow=210_000, existing_payments=15_000)
TIGHT = dict(revenue=1_600_000, available_cashflow=240_000, existing_payments=30_000)
DEMO_SCENARIOS = [
    ('ООО «Цветочная лавка»', 'Закупка сезонного товара', dict(amount=500_000, months=12, **SMALL), 0.5, []),
    ('ООО «Пекарня на Садовой»', 'Второй цех', dict(amount=1_200_000, months=24, **SMALL), 3, []),
    ('ИП Смирнова А. В.', 'Ремонт салона', dict(amount=600_000, months=18, revenue=450_000, available_cashflow=120_000, existing_payments=0), 20, []),
    ('ООО «ТехноСервис»', 'Оборудование для мастерской', dict(amount=2_500_000, months=36, revenue=1_800_000, available_cashflow=300_000, existing_payments=40_000), 31, []),
    ('ООО «Зелёный квартал»', 'Благоустройство территории', dict(amount=4_000_000, months=48, revenue=2_500_000, available_cashflow=380_000, existing_payments=60_000), 54, []),
    ('ООО «Логистик Плюс»', 'Два грузовых фургона', dict(amount=3_000_000, months=24, revenue=2_200_000, available_cashflow=260_000, existing_payments=60_000), 40,
     [(5, 'request_info', ['available_cashflow', 'existing_payments'], 'Подтвердите денежный поток и текущие платежи по лизингу')]),
    ('ООО «Кофейня Бриз»', 'Открытие точки у метро', dict(amount=1_500_000, months=24, revenue=800_000, available_cashflow=150_000, existing_payments=20_000), 30,
     [(3, 'request_info', ['available_cashflow', 'revenue'], 'Уточните выручку и поток с учётом летнего сезона'),
      (20, 'clarify', dict(available_cashflow=210_000, revenue=1_100_000), 'Приложили выписку за лето')]),
    ('ООО «Мастерская Уюта»', 'Станки для мебели', dict(amount=800_000, months=24, revenue=700_000, available_cashflow=180_000, existing_payments=0), 26, [(4, 'evaluate')]),
    ('ООО «Фермерский двор»', 'Холодильная камера', dict(amount=1_000_000, months=36, **SMALL), 160, [(2, 'evaluate')]),
    ('ООО «Гостевой дом»', 'Строительство гостевых домиков', dict(amount=5_000_000, months=60, revenue=1_000_000, available_cashflow=250_000, existing_payments=0), 110,
     [(7, 'evaluate'), (16, 'accept', None)]),
    ('ООО «Швейный цех»', 'Промышленные швейные машины', dict(amount=1_500_000, months=24, revenue=900_000, available_cashflow=120_000, existing_payments=0), 44, [(9, 'evaluate')]),
    ('ООО «Северный склад»', 'Расширение склада', dict(amount=5_000_000, months=24, **TIGHT), 50, [(12, 'evaluate')]),
    ('ООО «Типография Литера»', 'Цифровая печатная машина', dict(amount=3_000_000, months=12, **TIGHT), 12, [(6, 'evaluate')]),
    ('ООО «АвтоДеталь»', 'Пополнение склада запчастей', dict(amount=2_000_000, months=36, revenue=1_500_000, available_cashflow=300_000, existing_payments=20_000), 230,
     [(3, 'evaluate'), (20, 'accept', None)]),
    ('ООО «Студия Вектор»', 'Рабочие станции для дизайнеров', dict(amount=3_000_000, months=12, **TIGHT), 200, [(8, 'evaluate'), (30, 'accept', 'term')]),
    ('ООО «Чистый город»', 'Поломоечные машины', dict(amount=5_000_000, months=24, **TIGHT), 180, [(30, 'evaluate'), (10, 'accept', 'amount')]),
    ('ИП Ковалёв Д. С.', 'Оборудование для шиномонтажа', dict(amount=900_000, months=24, **SMALL), 100, [(1, 'evaluate'), (2, 'accept', None)]),
    ('ООО «Ремстрой»', 'Строительная техника', dict(amount=2_000_000, months=24, revenue=1_400_000, available_cashflow=280_000, existing_payments=30_000, overdue=True), 70, [(5, 'evaluate')]),
    ('ООО «Старт-Ап Лаб»', 'Разработка приложения', dict(amount=1_000_000, months=12, revenue=200_000, available_cashflow=60_000, existing_payments=50_000), 120, [(26, 'evaluate')]),
    ('ООО «Городской сад»', 'Теплица', dict(amount=1_500_000, months=36, revenue=1_000_000, available_cashflow=230_000, existing_payments=10_000), 220, [(4, 'evaluate')]),
    ('ООО «Медиа Пульс»', 'Студийное оборудование', dict(amount=3_000_000, months=12, **TIGHT), 250, [(10, 'evaluate')]),
    ('ООО «Вкусный дом»', 'Кухонное оборудование', dict(amount=1_200_000, months=24, **SMALL), 90, [(6, 'evaluate'), (15, 'withdraw')]),
    ('ООО «Энергосбыт-Регион»', 'Модернизация подстанции', dict(amount=1_500_000, months=36, revenue=1_200_000, available_cashflow=260_000, existing_payments=10_000, overdue=True), 60,
     [(3, 'evaluate'), (5, 'decision', 'approved', 'Просрочка была технической и закрыта, подтверждено справкой')]),
    ('ООО «Быстрый маркет»', 'Ремонт торгового зала', dict(amount=2_000_000, months=24, revenue=1_500_000, available_cashflow=320_000, existing_payments=20_000), 80,
     [(2, 'evaluate'), (4, 'decision', 'rejected', 'Сомнения в источнике выручки, нужна выездная проверка')]),
    ('ООО «Северная рыба»', 'Рефрижератор', dict(amount=800_000, months=24, revenue=900_000, available_cashflow=200_000, existing_payments=0, overdue=True), 140,
     [(2, 'evaluate'), (6, 'decision', 'approved', 'Поручительство собственника покрывает риск просрочки'), (12, 'accept', None)]),
    ('ООО «Детский клуб Радуга»', 'Второй филиал', dict(amount=2_000_000, months=24, revenue=900_000, available_cashflow=160_000, existing_payments=20_000), 170,
     [(4, 'request_info', ['available_cashflow', 'revenue'], 'Подтвердите выручку и поток после открытия группы продлённого дня'),
      (18, 'clarify', dict(available_cashflow=260_000, revenue=1_300_000), 'Приложили отчёт за квартал'),
      (3, 'evaluate'), (8, 'accept', None)]),
]

def run_scenario(id, company, purpose, terms, age, steps, base):
    at = base - timedelta(hours=age)
    _clock.set(at)
    insert_application(ApplicationInput(company=company, purpose=purpose, **terms), id)
    for hours, action, *args in steps:
        at += timedelta(hours=hours)
        _clock.set(at)
        if action == 'evaluate': evaluate(id, expected=None, role='bank')
        elif action == 'request_info': request_info(id, RequestInfo(fields=args[0], reason=args[1]), expected=None, role='bank')
        elif action == 'clarify': clarify(id, Clarification(note=args[1], **args[0]), expected=None, role='client')
        elif action == 'accept': accept(id, AcceptInput(offer=args[0]), expected=None, role='client')
        elif action == 'withdraw': withdraw(id, expected=None, role='client')
        elif action == 'decision': manual_decision(id, ManualDecision(decision=args[0], reason=args[1]), expected=None, role='bank')

def clear_demo(c):
    c.execute('DELETE FROM events WHERE application_id LIKE ?', (DEMO_PREFIX + '%',))
    return c.execute('DELETE FROM applications WHERE id LIKE ?', (DEMO_PREFIX + '%',)).rowcount

def demo_seed_enabled():
    if os.getenv('ENABLE_DEMO_SEED', '1') != '1':
        raise HTTPException(403, 'Демо-данные отключены (ENABLE_DEMO_SEED)')

class SeedOut(BaseModel):
    created: int

class DeletedOut(BaseModel):
    deleted: int

@app.post('/api/demo/seed', response_model=SeedOut)
def seed_demo(role: str = Depends(require('bank'))):
    """Recreate the synthetic demo applications; real applications are left alone."""
    demo_seed_enabled()
    base = datetime.now(timezone.utc)
    with conn() as c:
        clear_demo(c)
        token = _clock.set(None)
        try:
            for n, scenario in enumerate(DEMO_SCENARIOS):
                run_scenario(f'{DEMO_PREFIX}{n:06d}', *scenario, base)
        finally:
            _clock.reset(token)
    return {'created': len(DEMO_SCENARIOS)}

@app.delete('/api/demo/seed', response_model=DeletedOut)
def delete_demo(role: str = Depends(require('bank'))):
    demo_seed_enabled()
    with conn() as c:
        return {'deleted': clear_demo(c)}

@app.post('/api/demo/reset')
def reset_demo(role: str = Depends(require('bank'))):
    """For local synthetic demos only. Do not expose in a real bank."""
    if os.getenv('ENABLE_DEMO_RESET','0') != '1':
        raise HTTPException(403, 'Сброс отключён')
    with conn() as c:
        c.execute('DELETE FROM events')
        c.execute('DELETE FROM applications')
    return {'ok':True}

if __name__ == '__main__' and sys.argv[1:] == ['migrate']:
    migrate()
    print(f'schema version {SCHEMA_VERSION}')
