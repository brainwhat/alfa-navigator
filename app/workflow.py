"""Application state: reading with lazy offer expiry, atomic transitions, issued offers and whose move it is."""
import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException

from app import settings
from app.policy import POLICY_VERSION
from app.schemas import ApplicationInput
from app.storage import conn, event, now, utcnow

OPEN_OFFER = ('approved', 'counteroffer')
# Whose move it is; every status change also sets updated_at, so it marks when the wait began.
WAITING_ON = {'submitted':'bank', 'clarified':'bank', 'needs_info':'client', 'approved':'client', 'counteroffer':'client'}
STALE = 'Заявку уже изменили в другой вкладке или другим участником. Проверьте актуальные данные и повторите действие.'

def offer_deadline() -> str:
    return (utcnow() + timedelta(days=settings.OFFER_VALID_DAYS)).isoformat()

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

def queue_item(row, at):
    since = datetime.fromisoformat(row['updated_at'])
    row['waiting_on'] = WAITING_ON.get(row['status'])
    row['in_status_minutes'] = int((at - since).total_seconds() // 60)
    due = since + timedelta(hours=settings.BANK_SLA_HOURS) if row['waiting_on'] == 'bank' else None
    row['sla_due_at'] = due and due.isoformat()
    row['sla_breached'] = bool(due and at > due)
    return row

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

def issue(terms, basis):
    """Make terms a binding offer: the client accepts exactly these numbers, even if tariffs change later."""
    return {**terms, 'offer_id':uuid4().hex[:12], 'policy_version':POLICY_VERSION, 'issued_at':now(), 'basis':basis}

def stamped(explanation, basis):
    """Issue the offers of a scoring result: each gets an id and is stored as is."""
    if explanation['offer']:
        explanation['offer'] = issue(explanation['offer'], basis)
    explanation['offers'] = [issue(o, basis) for o in explanation['offers']]
    return explanation

def offered_terms(explanation):
    """The offer an approved application carries; older applications kept it only in `requested`."""
    return explanation.get('offer') or explanation.get('requested') or {}

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
