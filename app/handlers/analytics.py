"""Bank analytics: historical funnel from the event log, offer outcomes, rates and SLA."""
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from app import settings
from app.auth import require
from app.policy import NEGATIVE_FACTORS, rate_for
from app.schemas import Analytics
from app.storage import conn, now, utcnow
from app.workflow import OPEN_OFFER, expire_offers, offered_terms

router = APIRouter()

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

@router.get('/api/analytics', response_model=Analytics)
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
    limit = settings.BANK_SLA_HOURS * 60
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
        'sla': {'hours': settings.BANK_SLA_HOURS, 'responses': len(responses),
                'avg_minutes': average(responses), 'median_minutes': median(responses),
                'within_sla_pct': round(100 * sum(1 for m in responses if m <= limit) / len(responses)) if responses else None,
                'breached': sum(1 for m in responses if m > limit),
                'first_responses': len(first), 'first_avg_minutes': average(first), 'first_median_minutes': median(first),
                'waiting_now': len(open_waits), 'breached_now': sum(1 for m in open_waits if m > limit),
                'oldest_wait_minutes': round(max(open_waits)) if open_waits else None},
    }
