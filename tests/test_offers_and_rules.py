"""Stored offers, policy thresholds, schedules, simulator and analytics history."""
import pytest
from app import policy, schedules
from conftest import client, CLIENT, BANK, create, evaluate

def test_accept_keeps_stored_price(monkeypatch):
    d=evaluate(create()['id'])
    offered=d['explanation']['offer']
    assert offered['annual_rate']==0.18 and offered['offer_id']
    monkeypatch.setattr(policy,'RATE_TIERS',[(85,0.20),(70,0.22),(65,0.24),(0,0.28)])
    r=client.post('/api/applications/'+d['id']+'/accept',json={'offer_id':offered['offer_id']},headers=CLIENT).json()
    assert r['explanation']['accepted_terms']==offered

def test_accept_rejects_replaced_offer():
    d=evaluate(create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)['id'])
    r=client.post('/api/applications/'+d['id']+'/accept',json={'offer':'term','offer_id':'old-offer'},headers=CLIENT)
    assert r.status_code==409
    term=next(o for o in d['explanation']['offers'] if o['id']=='term')
    r=client.post('/api/applications/'+d['id']+'/accept',json={'offer':'term','offer_id':term['offer_id']},headers=CLIENT).json()
    assert r['explanation']['accepted_terms']['offer_id']==term['offer_id']

def test_manual_approval_issues_new_offer():
    d=evaluate(create(overdue=True)['id'])
    assert d['explanation']['offer'] is None
    r=client.post('/api/applications/'+d['id']+'/decision',json={'decision':'approved','reason':'Просрочка техническая, подтверждена'},headers=BANK).json()
    e=r['explanation']
    assert e['offer']['basis']=='manual' and e['offer']['annual_rate']==0.26 and e['requested']['annual_rate']==0.26
    assert e['assessment']['reference_rate']==0.22 and e['manual_override']['author']=='bank'
    assert r['events'][-1]['data']['offer_id']==e['offer']['offer_id']

def test_alternatives_carry_own_assessment():
    d=evaluate(create(amount=3_000_000,months=12,available_cashflow=240_000,existing_payments=30_000,revenue=1_600_000)['id'])
    assert d['explanation']['assessment']['score']<policy.APPROVE_SCORE
    for o in d['explanation']['offers']:
        assert o['score']>=policy.APPROVE_SCORE and o['payment_to_cashflow']<=policy.OFFER_LOAD

def row(load=0.5, amount=1_000_000, revenue=1_000_000, overdue=False):
    monthly=schedules.payment(amount,36,policy.RATE)
    return dict(amount=amount,months=36,revenue=revenue,existing_payments=0,overdue=overdue,available_cashflow=round(monthly/load))

@pytest.mark.parametrize('load,points',[(0.59,0),(0.61,-20),(0.74,-20),(0.76,-40)])
def test_load_thresholds(load,points):
    contributions=policy.assess(row(load=load))[3]
    assert contributions[0]['points']==points

def test_revenue_threshold_and_score_sum():
    assert policy.assess(row(amount=4_000_000,revenue=1_000_000))[3][2]['points']==0
    assert policy.assess(row(amount=4_000_000,revenue=999_999))[3][2]['points']==-15
    score,_,_,contributions=policy.assess(row(load=0.8,overdue=True))
    assert score==max(0,policy.BASE_SCORE+sum(c['points'] for c in contributions))==10

@pytest.mark.parametrize('amount,months,rate',[(100_000,6,0.18),(20_000_000,60,0.26),(3_333_333,37,0.22),(1_000_000,12,0.2)])
def test_schedule_closes(amount,months,rate):
    rows=schedules.amortization(amount,months,rate)
    assert len(rows)==months and rows[-1]['balance']==0
    assert sum(r['principal'] for r in rows)==amount and all(r['balance']>=0 for r in rows)
    pre=schedules.amortization(amount,months,rate,(3,amount//3,'payment'))
    assert sum(r['principal']+r['prepayment'] for r in pre)==amount and pre[-1]['balance']==0 and len(pre)==months

def test_simulator_stops_payments_after_loan():
    s=client.post('/api/simulate',headers=CLIENT,json={'amount':100_000,'months':6,'available_cashflow':100_000,'existing_payments':5_000,'seasonality':0,'start_month':3}).json()
    rows=s['timeline']
    assert len(rows)==12 and all(r['loan_payment']>0 for r in rows[:6]) and all(r['loan_payment']==0 for r in rows[6:])
    assert sum(r['loan_payment'] for r in rows)==sum(r['payment'] for r in schedules.amortization(100_000,6,policy.RATE))
    assert rows[6]['balance_after_debt']==95_000
    assert [r['calendar_month'] for r in rows[:3]]==[3,4,5] and rows[-1]['calendar_month']==2 and rows[-1]['winter']

def test_simulator_winter_follows_calendar():
    s=client.post('/api/simulate',headers=CLIENT,json={'amount':1_000_000,'months':24,'available_cashflow':200_000,'existing_payments':0,'seasonality':50,'start_month':10}).json()
    assert [r['winter'] for r in s['timeline'][:4]]==[False,True,True,True]
    assert s['timeline'][1]['cashflow']==100_000

def funnel():
    r=client.get('/api/analytics',headers=BANK).json()
    return {x['stage']:x['count'] for x in r['funnel']},r

def test_funnel_keeps_history_after_manual_reject():
    before,old=funnel()
    a=evaluate(create()['id'])
    assert a['status']=='approved'
    client.post('/api/applications/'+a['id']+'/decision',json={'decision':'rejected','reason':'Сомнения в источнике выручки'},headers=BANK)
    after,new=funnel()
    assert after['Получили предложение']-before['Получили предложение']==1
    assert new['offers']['rejected']-old['offers']['rejected']==1
    assert new['manual']['total']-old['manual']['total']==1

def test_analytics_period_and_first_response():
    r=client.get('/api/analytics?days=1',headers=BANK).json()
    assert r['period_days']==1 and r['sla']['first_responses']<=r['sla']['responses']
    assert client.get('/api/analytics?days=0',headers=BANK).status_code==422

def test_resubmission_links_previous():
    a=create()
    client.post('/api/applications/'+a['id']+'/withdraw',headers=CLIENT)
    r=client.post('/api/applications',json={**{k:a[k] for k in ('company','purpose','revenue','available_cashflow','existing_payments')},'amount':800_000,'months':24,'previous_id':a['id']},headers=CLIENT)
    assert r.status_code==201 and r.json()['previous_id']==a['id'] and a['id'] in r.json()['events'][0]['description']
    assert client.post('/api/applications',json={**{k:a[k] for k in ('company','purpose','revenue','available_cashflow','existing_payments')},'amount':800_000,'months':24,'previous_id':'NOPE'},headers=CLIENT).status_code==422
