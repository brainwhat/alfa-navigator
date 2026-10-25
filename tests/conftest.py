import os
import tempfile
# Each test run gets a fresh SQLite file, unless CI points DATABASE_URL at Postgres.
f=tempfile.NamedTemporaryFile(suffix='.db',delete=False)
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
def evaluate(id):
    return client.post('/api/applications/'+id+'/evaluate',headers=BANK).json()
def terms(**overrides):
    data=payload(**overrides)
    del data['company'],data['purpose']
    return data
