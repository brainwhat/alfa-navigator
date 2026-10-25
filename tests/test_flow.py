import os
import tempfile
f=tempfile.NamedTemporaryFile(delete=False)
f.close()
os.environ['DB_PATH']=f.name
from fastapi.testclient import TestClient
from app.main import app
client=TestClient(app)
CLIENT={'Authorization':'Bearer demo-client-change-me'}
BANK={'Authorization':'Bearer demo-bank-change-me'}
def payload(**overrides):
    data=dict(company='ООО Тест Кофе',purpose='Закупка оборудования',amount=1_000_000,months=36,revenue=1_000_000,available_cashflow=250_000,existing_payments=0,overdue=False)
    data.update(overrides)
    return data
def create(**kw):
    r=client.post('/api/applications',json=payload(**kw),headers=CLIENT)
    assert r.status_code==201,r.text
    return r.json()
def test_login_roles():
    assert client.post('/api/demo/login',json={'role':'client','password':'wrong'}).status_code==401
    assert client.get('/api/applications').status_code==401
    assert client.get('/api/applications',headers=CLIENT).status_code==403
    assert client.get('/api/my-applications',headers=BANK).status_code==403
    assert client.post('/api/applications',json=payload(),headers=BANK).status_code==403
    assert client.post('/api/demo/login',json={'role':'bank','password':'alfa-demo-2026'}).status_code==200
def test_approve_accept():
    a=create()
    assert client.post('/api/applications/'+a['id']+'/evaluate',headers=CLIENT).status_code==403
    r=client.post('/api/applications/'+a['id']+'/evaluate',headers=BANK)
    assert r.status_code==200
    assert r.json()['status']=='approved'
    assert r.json()['explanation']['policy_version']=='demo-rules-v1'
    assert client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT).json()['status']=='accepted'
    assert client.post('/api/applications/'+a['id']+'/evaluate',headers=BANK).status_code==409
def test_clarification_and_rejection():
    a=create(amount=5_000_000,available_cashflow=150_000,overdue=True)
    id=a['id']
    assert client.post('/api/applications/'+id+'/request-info',json={'reason':'Уточните финансы'},headers=BANK).json()['status']=='needs_info'
    assert client.post('/api/applications/'+id+'/clarify',json={'available_cashflow':160000,'note':'Уточнено'},headers=CLIENT).json()['status']=='clarified'
    assert client.post('/api/applications/'+id+'/evaluate',headers=BANK).json()['status']=='rejected'
    assert client.post('/api/applications/'+id+'/accept',headers=CLIENT).status_code==409
def test_counteroffer():
    a=create(amount=5_000_000,months=24,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
    d=client.post('/api/applications/'+a['id']+'/evaluate',headers=BANK).json()
    assert d['status']=='counteroffer'
    assert 100000<=d['alternative_amount']<5_000_000
def test_simulator_and_company_stub():
    r=client.post('/api/simulate',headers=CLIENT,json={'amount':3000000,'months':24,'available_cashflow':210000,'existing_payments':10000,'seasonality':30})
    assert r.status_code==200 and len(r.json()['timeline'])==12
    assert r.json()['stress_load']>=r.json()['base_load']
    assert client.get('/api/mock/company/1234567890',headers=CLIENT).json()['source']=='LOCAL_SYNTHETIC_FNS_STUB'
def test_validation_and_reset_closed():
    assert client.post('/api/applications',json=payload(amount=-1),headers=CLIENT).status_code==422
    assert client.post('/api/demo/reset',headers=BANK).status_code==403
def evaluate(id):
    return client.post('/api/applications/'+id+'/evaluate',headers=BANK).json()
def test_offers_and_choice():
    a=create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
    d=evaluate(a['id'])
    assert d['status']=='counteroffer'
    offers={o['id']:o for o in d['explanation']['offers']}
    assert offers['amount']['amount']<3_000_000 and offers['amount']['months']==12
    assert offers['term']['amount']==3_000_000 and 12<offers['term']['months']<=60
    assert any('срок' in t for t in d['explanation']['how_to_improve'])
    assert client.post('/api/applications/'+a['id']+'/accept',json={'offer':'bogus'},headers=CLIENT).status_code==422
    r=client.post('/api/applications/'+a['id']+'/accept',json={'offer':'term'},headers=CLIENT).json()
    assert r['status']=='accepted'
    assert r['explanation']['accepted_terms']['months']==offers['term']['months']
def test_offer_advice_is_approvable():
    a=create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
    for o in evaluate(a['id'])['explanation']['offers']:
        b=create(amount=o['amount'],months=o['months'],available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
        assert evaluate(b['id'])['status']=='approved'
def test_overdue_advice():
    d=evaluate(create(overdue=True)['id'])
    assert d['status']=='rejected' and d['explanation']['offers']==[]
    assert 'просрочки' in d['explanation']['how_to_improve'][0]
def test_withdraw():
    a=create()
    assert client.post('/api/applications/'+a['id']+'/withdraw',headers=BANK).status_code==403
    assert client.post('/api/applications/'+a['id']+'/withdraw',headers=CLIENT).json()['status']=='withdrawn'
    assert client.post('/api/applications/'+a['id']+'/withdraw',headers=CLIENT).status_code==409
    assert client.post('/api/applications/'+a['id']+'/evaluate',headers=BANK).status_code==409
def test_manual_decision():
    a=create(overdue=True)
    reason={'decision':'approved','reason':'Просрочка техническая, подтверждена справкой'}
    assert client.post('/api/applications/'+a['id']+'/decision',json=reason,headers=BANK).status_code==409
    assert evaluate(a['id'])['status']=='rejected'
    assert client.post('/api/applications/'+a['id']+'/decision',json=reason,headers=CLIENT).status_code==403
    assert client.post('/api/applications/'+a['id']+'/decision',json={'decision':'approved','reason':'коротко'},headers=BANK).status_code==422
    d=client.post('/api/applications/'+a['id']+'/decision',json=reason,headers=BANK).json()
    assert d['status']=='approved' and d['decision']=='rejected'
    assert d['explanation']['manual_override']['auto_decision']=='rejected'
    assert d['events'][-1]['action']=='manual_decision'
    assert client.post('/api/applications/'+a['id']+'/decision',json=reason,headers=BANK).status_code==409
    r=client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT).json()
    assert r['status']=='accepted' and r['explanation']['accepted_terms']['amount']==1_000_000
def terms(**overrides):
    data=payload(**overrides)
    del data['company'],data['purpose']
    return data
def test_precheck_matches_scoring_and_saves_nothing():
    before=len(client.get('/api/applications',headers=BANK).json())
    t=terms(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
    assert client.post('/api/precheck',json=t,headers=BANK).status_code==403
    p=client.post('/api/precheck',json=t,headers=CLIENT).json()
    assert len(client.get('/api/applications',headers=BANK).json())==before
    d=evaluate(create(**t)['id'])
    assert (p['decision'],p['score'],p['offers'])==(d['status'],d['score'],d['explanation']['offers'])
    assert p['requested']['overpayment']==p['requested']['total_paid']-3_000_000>0
    assert client.post('/api/precheck',json=terms(amount=-1),headers=CLIENT).status_code==422
def test_schedule():
    s=client.post('/api/schedule',json={'amount':3_000_000,'months':37},headers=CLIENT).json()
    rows=s['rows']
    assert len(rows)==37 and rows[-1]['balance']==0
    assert sum(r['principal'] for r in rows)==3_000_000
    assert s['overpayment']==sum(r['interest'] for r in rows)==s['total_paid']-3_000_000
    assert all(abs(r['payment']-s['monthly_payment'])<=1 for r in rows)
    assert client.post('/api/schedule',json={'amount':3_000_000,'months':37}).status_code==401
def test_offers_carry_overpayment():
    d=evaluate(create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)['id'])
    offers={o['id']:o for o in d['explanation']['offers']}
    assert offers['term']['overpayment']>d['explanation']['requested']['overpayment']>offers['amount']['overpayment']
def test_queue_sla(monkeypatch):
    import app.main as m
    a=create()
    item=next(x for x in client.get('/api/applications',headers=BANK).json() if x['id']==a['id'])
    assert item['waiting_on']=='bank' and not item['sla_breached'] and item['sla_due_at']
    monkeypatch.setattr(m,'BANK_SLA_HOURS',-1)
    item=next(x for x in client.get('/api/applications',headers=BANK).json() if x['id']==a['id'])
    assert item['sla_breached']
    client.post('/api/applications/'+a['id']+'/request-info',json={'reason':'Уточните финансы'},headers=BANK)
    item=next(x for x in client.get('/api/applications',headers=BANK).json() if x['id']==a['id'])
    assert item['waiting_on']=='client' and not item['sla_breached'] and item['sla_due_at'] is None
