"""Errors, readiness, migrations and the published API contract."""
import json
from pathlib import Path
from app import settings, storage
from app.main import app
from conftest import client, CLIENT, BANK, payload

def test_login_with_cyrillic_password_is_401():
    r=client.post('/api/demo/login',json={'role':'client','password':'пароль'})
    assert r.status_code==401 and r.json()['detail']
    assert client.post('/api/demo/login',json={'role':'client','password':'x'*201}).status_code==422
    assert client.get('/api/applications',headers={'Authorization':'Bearer \xe9t\xe9'.encode('latin-1')}).status_code==403

def test_validation_errors_are_readable():
    r=client.post('/api/applications',json=payload(amount=-1,company=''),headers=CLIENT)
    body=r.json()
    assert r.status_code==422 and isinstance(body['detail'],str)
    fields={e['field']:e['message'] for e in body['errors']}
    assert fields['amount']=='не меньше 100 000' and 'символ' in fields['company']
    assert 'Сумма: не меньше 100 000' in body['detail']

def test_errors_carry_request_id():
    r=client.get('/api/applications/NOPE',headers={**BANK,'X-Request-ID':'trace-42'})
    assert r.status_code==404 and r.headers['X-Request-ID']=='trace-42'

def test_readiness(monkeypatch):
    r=client.get('/api/ready')
    assert r.status_code==200 and r.json()['schema_version']==storage.SCHEMA_VERSION
    if settings.DATABASE_URL:
        monkeypatch.setattr(settings,'DATABASE_URL','postgresql://nobody@127.0.0.1:1/none')
    else:
        monkeypatch.setattr(settings,'DB',Path('/nonexistent-dir/navigator.db'))
    r=client.get('/api/ready')
    assert r.status_code==503 and r.json()['status']=='unavailable'
    assert client.get('/api/health').status_code==200

def test_migrations_are_repeatable():
    storage.migrate()
    assert client.get('/api/ready').json()['schema_version']==storage.SCHEMA_VERSION

def test_static_cache_headers():
    page=client.get('/')
    assert page.headers['Cache-Control']=='no-store' and '__ASSET_VERSION__' not in page.text
    asset=page.text.split('/static/app.js?v=')[1].split('"')[0]
    r=client.get('/static/app.js?v='+asset)
    assert r.status_code==200 and 'immutable' in r.headers['Cache-Control']

def test_openapi_snapshot_is_current():
    snapshot=json.loads((Path(__file__).parent.parent/'docs'/'openapi.json').read_text(encoding='utf-8'))
    assert snapshot==app.openapi(), 'docs/openapi.json устарел: python scripts/export_openapi.py'

def test_success_responses_have_schemas():
    spec=app.openapi()
    for path in ('/api/applications/{id}/evaluate','/api/applications/{id}/accept','/api/precheck','/api/schedule','/api/analytics'):
        operation=next(iter(spec['paths'][path].values()))
        assert operation['responses']['200']['content']['application/json']['schema']
