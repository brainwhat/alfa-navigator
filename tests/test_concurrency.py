"""Commands that race on one application: exactly one wins, the other gets 409 and leaves no trace."""
import threading
from uuid import uuid4
from fastapi.testclient import TestClient
from app import settings, workflow
from app.main import app
from conftest import client, CLIENT, BANK, create, evaluate

def race(monkeypatch, *calls):
    """Run the calls at once; each pauses right after its first read until the others have read too.

    On Postgres all of them read the same version and collide on the conditional UPDATE. SQLite
    serializes transactions, so the pause times out and the next command reads the new state."""
    barrier=threading.Barrier(len(calls),timeout=1)
    lock=threading.Lock()
    reads=[0]
    original=workflow.get_row
    def paused(c,id):
        row=original(c,id)
        with lock:
            reads[0]+=1
            first=reads[0]<=len(calls)
        if first:
            try: barrier.wait()
            except threading.BrokenBarrierError: pass
        return row
    monkeypatch.setattr(workflow,'get_row',paused)
    results=[None]*len(calls)
    def run(i,call):
        results[i]=call(TestClient(app))
    threads=[threading.Thread(target=run,args=(i,call)) for i,call in enumerate(calls)]
    for t in threads: t.start()
    for t in threads: t.join(30)
    monkeypatch.setattr(workflow,'get_row',original)
    return [r.status_code for r in results]

def history(id):
    return [e['action'] for e in client.get('/api/applications/'+id,headers=BANK).json()['events']]

def test_double_accept(monkeypatch):
    a=evaluate(create()['id'])
    accept=lambda c:c.post('/api/applications/'+a['id']+'/accept',headers=CLIENT)
    assert sorted(race(monkeypatch,accept,accept))==[200,409]
    assert history(a['id']).count('accepted')==1

def test_accept_against_withdraw(monkeypatch):
    a=evaluate(create()['id'])
    codes=race(monkeypatch,
               lambda c:c.post('/api/applications/'+a['id']+'/accept',headers=CLIENT),
               lambda c:c.post('/api/applications/'+a['id']+'/withdraw',headers=CLIENT))
    assert sorted(codes)==[200,409]
    final=client.get('/api/applications/'+a['id'],headers=BANK).json()
    winner='accepted' if codes[0]==200 else 'withdrawn'
    assert final['status']==winner and history(a['id'])[-1]==winner
    assert ('accepted_terms' in final['explanation'])==(winner=='accepted')
    assert {'accepted','withdrawn'}&set(history(a['id']))=={winner}

def test_evaluate_against_request_info(monkeypatch):
    id=create()['id']
    codes=race(monkeypatch,
               lambda c:c.post('/api/applications/'+id+'/evaluate',headers=BANK),
               lambda c:c.post('/api/applications/'+id+'/request-info',json={'reason':'Уточните финансы'},headers=BANK))
    assert sorted(codes)==[200,409]
    assert len({'evaluated','requested_info'}&set(history(id)))==1

def test_extend_against_expiry(monkeypatch):
    monkeypatch.setattr(settings,'OFFER_VALID_DAYS',-1)
    a=create()
    assert evaluate(a['id'])['status']=='expired'
    monkeypatch.setattr(settings,'OFFER_VALID_DAYS',7)
    extend=lambda c:c.post('/api/applications/'+a['id']+'/extend',headers=BANK)
    codes=race(monkeypatch,extend,extend)
    # a repeated extension of an open offer is allowed, so both may succeed when they run one after another;
    # either way every success logs exactly one event and a rejected command logs nothing
    assert 200 in codes and set(codes)<={200,409}
    assert history(a['id']).count('extended')==codes.count(200)
    final=client.get('/api/applications/'+a['id'],headers=BANK).json()
    assert final['status']=='approved' and final['version']==a['version']+2+codes.count(200)

def test_stale_card_version():
    a=create()
    stale={**BANK,'If-Match':str(a['version'])}
    client.post('/api/applications/'+a['id']+'/request-info',json={'reason':'Уточните финансы'},headers=BANK)
    r=client.post('/api/applications/'+a['id']+'/evaluate',headers=stale)
    assert r.status_code==409 and 'повторите' in r.json()['detail']
    fresh=client.get('/api/applications/'+a['id'],headers=BANK).json()
    assert fresh['version']==a['version']+1
    assert client.post('/api/applications/'+a['id']+'/withdraw',headers={**CLIENT,'If-Match':f'"{fresh["version"]}"'}).status_code==200
    assert client.post('/api/applications/'+a['id']+'/withdraw',headers={**CLIENT,'If-Match':'x'}).status_code==422

def test_idempotent_create():
    before=len(client.get('/api/applications',headers=BANK).json())
    key={**CLIENT,'Idempotency-Key':'retry-'+uuid4().hex}
    body=dict(company='ООО Повтор',purpose='Проверка повтора',amount=1_000_000,months=36,revenue=1_000_000,available_cashflow=250_000,existing_payments=0)
    first=client.post('/api/applications',json=body,headers=key)
    again=client.post('/api/applications',json=body,headers=key)
    assert first.status_code==201 and again.status_code==200 and again.headers['Idempotency-Replayed']=='true'
    assert first.json()['id']==again.json()['id']
    assert len(client.get('/api/applications',headers=BANK).json())==before+1
    assert client.post('/api/applications',json={**body,'amount':2_000_000},headers=key).status_code==422
    assert client.post('/api/applications',json=body,headers=CLIENT).json()['id']!=first.json()['id']
