"""Live updates: the change feed, its stream and the two-screen page."""
import asyncio
import json
from app import settings
from app.handlers.live import stream
from conftest import client, CLIENT, BANK, create, evaluate

def cursor():
    return client.get('/api/changes',headers=BANK).json()['cursor']

def test_changes_need_login_and_a_valid_cursor():
    assert client.get('/api/changes').status_code==401
    assert client.get('/api/changes/stream').status_code==401
    assert client.get('/api/changes?after=x;drop',headers=BANK).status_code==422

def test_changes_since_cursor():
    start=cursor()
    assert client.get('/api/changes?after='+start,headers=CLIENT).json()=={'cursor':start,'changes':[],'reset':False}
    row=create(company='ООО Живые обновления')
    evaluate(row['id'])
    feed=client.get('/api/changes?after='+start,headers=CLIENT).json()
    mine=[(c['actor'],c['action']) for c in feed['changes'] if c['application_id']==row['id']]
    assert mine==[('client','submitted'),('bank','evaluated')]
    assert feed['changes'][-1]['company']=='ООО Живые обновления'
    assert feed['reset'] is True  # a new application changes the count
    assert client.get('/api/changes?after='+feed['cursor'],headers=CLIENT).json()['changes']==[]

def test_deleted_demo_data_asks_for_a_full_reload():
    assert client.post('/api/demo/seed',headers=BANK).status_code==200
    start=cursor()
    assert client.delete('/api/demo/seed',headers=BANK).status_code==200
    feed=client.get('/api/changes?after='+start,headers=BANK).json()
    assert feed['reset'] is True and feed['cursor']!=start

def test_stream_sends_the_cursor_and_missed_changes_at_once(monkeypatch):
    monkeypatch.setattr(settings,'STREAM_SECONDS',0)
    start=cursor()
    row=create()
    with client.stream('GET','/api/changes/stream?after='+start,headers=BANK) as r:
        assert r.status_code==200 and r.headers['content-type'].startswith('text/event-stream')
        body=r.read().decode()
    assert body.startswith('retry: ')
    data=[json.loads(line[5:]) for line in body.splitlines() if line.startswith('data:')]
    assert len(data)==1 and data[0]['changes'][-1]['application_id']==row['id']

class Connected:
    async def is_disconnected(self):
        return False

def test_stream_reports_changes_while_open(monkeypatch):
    # TestClient collects a streamed body whole, so the generator is driven directly
    monkeypatch.setattr(settings,'STREAM_SECONDS',5)
    monkeypatch.setattr(settings,'STREAM_POLL_SECONDS',0.1)
    start=cursor()
    async def run():
        messages=stream(Connected(),start)
        first=await anext(messages)
        row=await asyncio.to_thread(create,company='ООО Во время потока')
        second=await anext(messages)
        await messages.aclose()
        return first,second,row
    first,second,row=asyncio.run(run())
    data=lambda m: json.loads(m.split('data: ',1)[1])
    assert data(first)['changes']==[] and data(first)['cursor']==start
    assert [c['application_id'] for c in data(second)['changes']]==[row['id']]

def test_split_page_frames_the_app():
    r=client.get('/split')
    assert r.status_code==200 and '/?pane=client' in r.text and '/?pane=bank' in r.text
    assert '__ASSET_VERSION__' not in r.text
    assert client.get('/').headers['X-Frame-Options']=='SAMEORIGIN'
