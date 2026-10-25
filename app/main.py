"""Alfa Navigator: autonomous synthetic hackathon credit pipeline."""
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4
import secrets

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
# Postgres when a connection string is set (Neon on Vercel sets DATABASE_URL), SQLite otherwise.
DATABASE_URL = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row
else:
    DB = Path(os.environ.get('DB_PATH', '/tmp/alfa-navigator.db'))
    DB.parent.mkdir(parents=True, exist_ok=True)
app = FastAPI(title='Альфа.Навигатор — демо API', version='1.0.0')
# Demo-only tokens, supplied by environment. Browser clients log in explicitly.
DEMO_CLIENT_KEY = os.getenv('DEMO_CLIENT_KEY', 'demo-client-change-me')
DEMO_BANK_KEY = os.getenv('DEMO_BANK_KEY', 'demo-bank-change-me')
DEMO_LOGIN_PASSWORD = os.getenv('DEMO_LOGIN_PASSWORD', 'alfa-demo-2026')
# Separate per-role passwords; both fall back to the shared demo password for local runs.
DEMO_PASSWORDS = {'client': os.getenv('DEMO_CLIENT_PASSWORD', DEMO_LOGIN_PASSWORD),
                  'bank': os.getenv('DEMO_BANK_PASSWORD', DEMO_LOGIN_PASSWORD)}
PUBLIC_DEFAULTS = {'demo-client-change-me', 'demo-bank-change-me', 'alfa-demo-2026'}
if os.getenv('VERCEL_ENV') == 'production' and PUBLIC_DEFAULTS & {DEMO_CLIENT_KEY, DEMO_BANK_KEY, *DEMO_PASSWORDS.values()}:
    raise RuntimeError('Production uses demo secrets from the public repo: set DEMO_CLIENT_KEY, DEMO_BANK_KEY, DEMO_CLIENT_PASSWORD, DEMO_BANK_PASSWORD')

def require(role: str):
    def dependency(authorization: str | None = Header(None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Требуется вход')
        token = authorization[7:]
        expected = DEMO_CLIENT_KEY if role == 'client' else DEMO_BANK_KEY
        if not secrets.compare_digest(token, expected):
            raise HTTPException(403, 'Недостаточно прав')
        return role
    return dependency

@app.middleware('http')
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response

class LoginInput(BaseModel):
    role: Literal['client','bank']
    password: str

@app.post('/api/demo/login')
def login(payload: LoginInput):
    if not secrets.compare_digest(payload.password, DEMO_PASSWORDS[payload.role]):
        raise HTTPException(401, 'Неверный демонстрационный пароль')
    return {'role': payload.role, 'access_token': DEMO_CLIENT_KEY if payload.role == 'client' else DEMO_BANK_KEY, 'token_type':'bearer', 'warning':'Учётные записи являются демонстрационными, не production IAM'}

app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

class PgConnection:
    """psycopg connection that accepts the sqlite-style '?' placeholders used by the routes."""
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return self.raw.execute(sql.replace('?', '%s'), params)

@contextmanager
def conn():
    if DATABASE_URL:
        # prepare_threshold=None: Neon's pooled endpoint runs PgBouncer in transaction mode.
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None) as connection:
            yield PgConnection(connection)
        return
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

with conn() as c:
    if DATABASE_URL:
        # Serverless instances may cold-start at once; serialize schema creation.
        c.execute('SELECT pg_advisory_xact_lock(20261025)')
    c.execute('''CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL, company TEXT NOT NULL, purpose TEXT NOT NULL, amount INTEGER NOT NULL,
     months INTEGER NOT NULL, available_cashflow INTEGER NOT NULL, revenue INTEGER NOT NULL,
     existing_payments INTEGER NOT NULL, overdue INTEGER NOT NULL, status TEXT NOT NULL,
     score INTEGER, decision TEXT, alternative_amount INTEGER, explanation TEXT)''')
    event_id = 'BIGSERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    c.execute(f'''CREATE TABLE IF NOT EXISTS events (id {event_id},
     application_id TEXT NOT NULL, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
     action TEXT NOT NULL, description TEXT NOT NULL)''')

RATE = 0.22

def payment(amount:int, months:int) -> int:
    r = RATE / 12
    return round(amount * r / (1 - (1 + r) ** (-months)))

class ApplicationInput(BaseModel):
    company: str = Field(min_length=2, max_length=120)
    purpose: str = Field(min_length=3, max_length=200)
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    revenue: int = Field(gt=0, le=1_000_000_000)
    available_cashflow: int = Field(gt=0, le=1_000_000_000)
    existing_payments: int = Field(ge=0, le=1_000_000_000)
    overdue: bool = False

class RequestInfo(BaseModel):
    reason: str = Field(min_length=5, max_length=300)

class Clarification(BaseModel):
    available_cashflow: int = Field(gt=0, le=1_000_000_000)
    note: str = Field(min_length=3, max_length=300)

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def event(c, id, actor, action, description):
    c.execute('INSERT INTO events (application_id, occurred_at, actor, action, description) VALUES (?,?,?,?,?)',
              (id, now(), actor, action, description))

def get_row(c, id):
    row = c.execute('SELECT * FROM applications WHERE id=?', (id,)).fetchone()
    if row is None: raise HTTPException(404, 'Заявка не найдена')
    return dict(row)

def enriched(c, id):
    row = get_row(c, id)
    row['events'] = [dict(x) for x in c.execute('SELECT occurred_at, actor, action, description FROM events WHERE application_id=? ORDER BY id', (id,))]
    if row['explanation']: row['explanation'] = json.loads(row['explanation'])
    return row

def score_application(row):
    """Illustrative deterministic policy; NOT a real banking credit decision."""
    monthly = payment(row['amount'], row['months'])
    load = (monthly + row['existing_payments']) / row['available_cashflow']
    reasons = []
    score = 85
    if load > 0.75:
        score -= 40
        reasons.append('Высокая доля платежей в свободном денежном потоке')
    elif load > 0.6:
        score -= 20
        reasons.append('Повышенная долговая нагрузка')
    else:
        reasons.append('Долговая нагрузка в пределах демонстрационного порога')
    if row['overdue']:
        score -= 35
        reasons.append('В синтетической истории указаны просрочки')
    else:
        reasons.append('Просрочек в синтетических данных нет')
    if row['amount'] > row['revenue'] * 4:
        score -= 15
        reasons.append('Запрос существенно превышает месячную выручку')
    else:
        reasons.append('Сумма сопоставима с заявленной выручкой')
    score = max(0, score)
    alternative = None
    if not row['overdue'] and score < 65:
        # find largest 100k-increment offer satisfying 60% cashflow load; avoid arbitrary approvals
        affordable_payment = max(0, 0.6 * row['available_cashflow'] - row['existing_payments'])
        r = RATE/12
        limit = int(affordable_payment * (1-(1+r)**(-row['months']))/r//100_000)*100_000
        if 100_000 <= limit < row['amount']:
            alternative = limit
    if score >= 65: decision = 'approved'
    elif alternative: decision = 'counteroffer'
    else: decision = 'rejected'
    return score, decision, alternative, {'policy_version':'demo-rules-v1','annual_rate':RATE, 'monthly_payment':monthly,'payment_to_cashflow':round(load,3),'factors':reasons,'disclaimer':'Демонстрационная модель на синтетических данных, не банковское решение.'}


def validate_any(authorization: str | None):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Требуется вход')
    candidate = authorization[7:]
    if not (secrets.compare_digest(candidate, DEMO_CLIENT_KEY) or secrets.compare_digest(candidate, DEMO_BANK_KEY)):
        raise HTTPException(403, 'Недостаточно прав')

@app.get('/api/my-applications')
def my_applications(role: str = Depends(require('client'))):
    # In demo there is exactly one synthetic customer; this is NOT multi-tenant isolation.
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT id,company,amount,months,status,created_at FROM applications ORDER BY created_at DESC')]

class SimulationInput(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    available_cashflow: int = Field(gt=0, le=1_000_000_000)
    existing_payments: int = Field(ge=0, le=1_000_000_000)
    seasonality: int = Field(ge=0, le=60, description='Процент сезонного падения свободного потока')

@app.post('/api/simulate')
def simulate(payload: SimulationInput, authorization: str | None = Header(None)):
    validate_any(authorization)
    monthly=payment(payload.amount,payload.months)
    base=(monthly+payload.existing_payments)/payload.available_cashflow
    seasonal_cash=payload.available_cashflow*(1-payload.seasonality/100)
    stress=(monthly+payload.existing_payments)/seasonal_cash
    schedule=[]
    for m in range(1, 13):
        # Illustrative stress scenario in winter months, not a cash flow prediction.
        stress_month=m in (1,2,11,12)
        flow=round(seasonal_cash if stress_month else payload.available_cashflow)
        schedule.append({'month':m,'cashflow':flow,'loan_payment':monthly,'existing_payments':payload.existing_payments,'balance_after_debt':flow-monthly-payload.existing_payments})
    return {'annual_rate':RATE, 'monthly_payment':monthly,'base_load':round(base,4), 'stress_load':round(stress,4), 'timeline':schedule, 'safe_under_stress':stress<=0.6, 'disclaimer':'Условная модель, а не кредитное решение или финансовый прогноз.'}

@app.get('/api/mock/company/{inn}')
def mock_company(inn: str, authorization: str | None = Header(None)):
    validate_any(authorization)
    if len(inn) not in (10,12) or not inn.isdigit():
        raise HTTPException(422, 'Синтетический ИНН должен содержать 10 или 12 цифр')
    return {'inn':inn, 'source':'LOCAL_SYNTHETIC_FNS_STUB', 'verified':True, 'company':'ООО Демонстрационный бизнес', 'note':'Не является запросом в ФНС или подтверждением существования компании'}

@app.get('/')
def index(): return FileResponse(ROOT / 'static' / 'index.html')

@app.get('/api/health')
def health(): return {'status':'ok', 'database':'postgres' if DATABASE_URL else 'sqlite'}

@app.get('/api/applications')
def list_applications(role: str = Depends(require('bank'))):
    with conn() as c:
        return [dict(row) for row in c.execute('SELECT id, company, amount, months, status, created_at FROM applications ORDER BY created_at DESC')]

@app.post('/api/applications', status_code=201)
def create_application(payload:ApplicationInput, role: str = Depends(require('client'))):
    id = uuid4().hex[:10].upper()
    time = now()
    with conn() as c:
        c.execute('''INSERT INTO applications (id,created_at,updated_at,company,purpose,amount,months,available_cashflow,revenue,existing_payments,overdue,status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (id,time,time,payload.company,payload.purpose,payload.amount,payload.months,payload.available_cashflow,payload.revenue,payload.existing_payments,int(payload.overdue),'submitted'))
        event(c,id,'client','submitted','Клиент отправил заявку')
        return enriched(c,id)

@app.get('/api/applications/{id}')
def get_application(id:str, authorization: str | None = Header(None)):
    validate_any(authorization)
    with conn() as c:return enriched(c,id)

@app.post('/api/applications/{id}/evaluate')
def evaluate(id:str, role: str = Depends(require('bank'))):
    with conn() as c:
        row=get_row(c,id)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Оценка возможна только для новой или уточнённой заявки')
        score,decision,alternative,explanation=score_application(row)
        c.execute('UPDATE applications SET status=?, score=?, decision=?, alternative_amount=?, explanation=?, updated_at=? WHERE id=?',
                  (decision,score,decision,alternative,json.dumps(explanation,ensure_ascii=False),now(),id))
        event(c,id,'bank','evaluated',f'Скоринг {score}; результат: {decision}; правила demo-rules-v1')
        return enriched(c,id)

@app.post('/api/applications/{id}/request-info')
def request_info(id:str, payload:RequestInfo, role: str = Depends(require('bank'))):
    with conn() as c:
        row=get_row(c,id)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Уточнение на этом этапе недоступно')
        c.execute('UPDATE applications SET status=?,updated_at=? WHERE id=?', ('needs_info',now(),id))
        event(c,id,'bank','requested_info',payload.reason)
        return enriched(c,id)

@app.post('/api/applications/{id}/clarify')
def clarify(id:str, payload:Clarification, role: str = Depends(require('client'))):
    with conn() as c:
        row=get_row(c,id)
        if row['status']!='needs_info': raise HTTPException(409,'Уточнение не запрошено')
        c.execute('UPDATE applications SET available_cashflow=?,status=?,updated_at=? WHERE id=?',
                  (payload.available_cashflow,'clarified',now(),id))
        event(c,id,'client','clarified',payload.note)
        return enriched(c,id)

@app.post('/api/applications/{id}/accept')
def accept(id:str, role: str = Depends(require('client'))):
    with conn() as c:
        row=get_row(c,id)
        if row['status'] not in ('approved','counteroffer'):
            raise HTTPException(409, 'Принять можно только одобренные условия')
        amount = row['alternative_amount'] or row['amount']
        c.execute('UPDATE applications SET status=?,updated_at=? WHERE id=?',('accepted',now(),id))
        event(c,id,'client','accepted',f'Клиент принял условия на {amount} ₽; подписание и выдача смоделированы')
        return enriched(c,id)

@app.post('/api/demo/reset')
def reset_demo(role: str = Depends(require('bank'))):
    """For local synthetic demos only. Do not expose in a real bank."""
    if os.getenv('ENABLE_DEMO_RESET','0') != '1':
        raise HTTPException(403, 'Сброс отключён')
    with conn() as c:
        c.execute('DELETE FROM events')
        c.execute('DELETE FROM applications')
    return {'ok':True}
