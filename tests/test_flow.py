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
def test_risk_based_rate():
    import app.main as m
    assert [m.rate_for(s) for s in (85,70,65,50)]==[0.18,0.20,0.22,0.26]
    best=evaluate(create()['id'])
    assert best['score']==85 and best['explanation']['annual_rate']==0.18
    assert best['explanation']['requested']['monthly_payment']==m.payment(1_000_000,36,0.18)
    weaker=evaluate(create(amount=5_000_000,months=60,revenue=1_000_000,available_cashflow=250_000)['id'])
    assert weaker['score']==70 and weaker['explanation']['annual_rate']==0.20
    r=client.post('/api/applications/'+best['id']+'/accept',headers=CLIENT).json()
    assert r['explanation']['accepted_terms']['annual_rate']==0.18
    s=client.post('/api/schedule',json={'amount':1_000_000,'months':36,'annual_rate':0.18},headers=CLIENT).json()
    assert s['monthly_payment']==m.payment(1_000_000,36,0.18) and s['annual_rate']==0.18
def test_manual_approval_priced_at_top_rate():
    a=create(overdue=True)
    evaluate(a['id'])
    client.post('/api/applications/'+a['id']+'/decision',json={'decision':'approved','reason':'Просрочка техническая, подтверждена'},headers=BANK)
    r=client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT).json()
    assert r['explanation']['accepted_terms']['annual_rate']==0.26
def test_offer_expiry_and_extension(monkeypatch):
    import app.main as m
    d=evaluate(create()['id'])
    assert d['status']=='approved' and d['offer_expires_at']>d['updated_at']
    monkeypatch.setattr(m,'OFFER_VALID_DAYS',-1)
    a=create()
    d=evaluate(a['id'])
    assert d['status']=='expired' and d['events'][-1]['actor']=='system'
    assert sum(e['action']=='expired' for e in client.get('/api/applications/'+a['id'],headers=BANK).json()['events'])==1
    assert client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT).status_code==409
    assert client.post('/api/applications/'+a['id']+'/extend',headers=CLIENT).status_code==403
    monkeypatch.setattr(m,'OFFER_VALID_DAYS',7)
    r=client.post('/api/applications/'+a['id']+'/extend',headers=BANK).json()
    assert r['status']=='approved' and r['events'][-1]['action']=='extended'
    assert client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT).json()['status']=='accepted'
    assert client.post('/api/applications/'+a['id']+'/extend',headers=BANK).status_code==409
def test_analytics():
    assert client.get('/api/analytics',headers=CLIENT).status_code==403
    before=client.get('/api/analytics',headers=BANK).json()
    a=create(); evaluate(a['id']); client.post('/api/applications/'+a['id']+'/accept',headers=CLIENT)
    b=create(overdue=True); evaluate(b['id'])
    create()
    r=client.get('/api/analytics',headers=BANK).json()
    funnel={x['stage']:x['count'] for x in r['funnel']}
    old={x['stage']:x['count'] for x in before['funnel']}
    assert funnel['Подано']-old['Подано']==3 and funnel['Прошли скоринг']-old['Прошли скоринг']==2
    assert funnel['Приняли условия']-old['Приняли условия']==1
    assert any(f['factor']=='В синтетической истории указаны просрочки' for f in r['top_factors'])
    assert r['sla']['responses']-before['sla']['responses']==2 and r['sla']['waiting_now']>=1
    assert r['sla']['within_sla_pct']==100 and r['sla']['breached_now']==0
def test_unapproved_terms_shown_at_reference_rate():
    d=evaluate(create(overdue=True)['id'])
    assert d['explanation']['requested']['annual_rate']==0.22
    r=client.post('/api/applications/'+d['id']+'/decision',json={'decision':'approved','reason':'Просрочка техническая, подтверждена'},headers=BANK).json()
    assert r['explanation']['requested']['annual_rate']==0.26
def test_clarify_several_fields():
    a=create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)
    id=a['id']
    assert client.post('/api/applications/'+id+'/request-info',json={'reason':'Уточните финансы','fields':[]},headers=BANK).status_code==422
    r=client.post('/api/applications/'+id+'/request-info',json={'reason':'Уточните финансы','fields':['revenue','existing_payments','overdue']},headers=BANK).json()
    assert r['info_request']['fields']==['revenue','existing_payments','overdue']
    assert client.post('/api/applications/'+id+'/clarify',json={'note':'Уточнили','revenue':2_000_000},headers=CLIENT).status_code==422
    assert client.post('/api/applications/'+id+'/clarify',json={'note':'Уточнили','revenue':2_000_000,'existing_payments':0,'overdue':False,'available_cashflow':500_000},headers=CLIENT).status_code==422
    r=client.post('/api/applications/'+id+'/clarify',json={'note':'Уточнили','revenue':2_000_000,'existing_payments':0,'overdue':False},headers=CLIENT).json()
    assert r['status']=='clarified' and r['info_request'] is None
    assert (r['revenue'],r['existing_payments'],r['overdue'],r['available_cashflow'])==(2_000_000,0,0,240_000)
    assert 'платежи по действующим долгам: 30 000 ₽ → 0 ₽' in r['events'][-1]['description']
def test_clarify_defaults_to_cashflow():
    id=create()['id']
    client.post('/api/applications/'+id+'/request-info',json={'reason':'Уточните финансы'},headers=BANK)
    assert client.post('/api/applications/'+id+'/clarify',json={'available_cashflow':300_000,'note':'Уточнено'},headers=CLIENT).json()['available_cashflow']==300_000
def test_prepayment():
    base={'amount':1_000_000,'months':24}
    plain=client.post('/api/schedule',json=base,headers=CLIENT).json()
    term=client.post('/api/schedule',json={**base,'prepay_month':6,'prepay_amount':300_000},headers=CLIENT).json()
    assert term['months']<24 and term['savings']>0 and term['monthly_after']==plain['monthly_payment']
    assert term['rows'][5]['prepayment']==300_000 and term['rows'][-1]['balance']==0
    assert sum(r['principal']+r['prepayment'] for r in term['rows'])==1_000_000
    assert term['baseline']['overpayment']==plain['overpayment']==term['overpayment']+term['savings']
    pay=client.post('/api/schedule',json={**base,'prepay_month':6,'prepay_amount':300_000,'prepay_mode':'payment'},headers=CLIENT).json()
    assert pay['months']==24 and pay['monthly_after']<plain['monthly_payment'] and 0<pay['savings']<term['savings']
    full=client.post('/api/schedule',json={**base,'prepay_month':3,'prepay_amount':5_000_000},headers=CLIENT).json()
    assert full['months']==3 and full['rows'][-1]['balance']==0
    assert client.post('/api/schedule',json={**base,'prepay_month':24,'prepay_amount':1000},headers=CLIENT).status_code==422
def test_demo_seed():
    import app.main as m
    from datetime import datetime, timezone
    assert client.post('/api/demo/seed',headers=CLIENT).status_code==403
    real=create(company='ООО Настоящая заявка')
    assert client.post('/api/demo/seed',headers=BANK).json()['created']==len(m.DEMO_SCENARIOS)
    items=client.get('/api/applications',headers=BANK).json()
    demo=[x for x in items if x['id'].startswith('DEMO')]
    statuses={x['status'] for x in demo}
    assert {'submitted','needs_info','clarified','approved','counteroffer','accepted','rejected','expired','withdrawn'}<=statuses
    assert any(x['sla_breached'] for x in demo)
    assert all(x['updated_at']<=datetime.now(timezone.utc).isoformat() for x in demo)
    a=client.get('/api/analytics',headers=BANK).json()
    assert {r['rate'] for r in a['approved_rates']}>={0.18,0.20,0.22,0.26} and a['offers']['expired']>=2
    assert client.post('/api/demo/seed',headers=BANK).status_code==200
    assert len([x for x in client.get('/api/applications',headers=BANK).json() if x['id'].startswith('DEMO')])==len(demo)
    assert client.delete('/api/demo/seed',headers=BANK).json()['deleted']==len(demo)
    ids=[x['id'] for x in client.get('/api/applications',headers=BANK).json()]
    assert real['id'] in ids and not any(i.startswith('DEMO') for i in ids)
