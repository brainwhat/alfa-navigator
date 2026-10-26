"""Smoke check for a running Alfa Navigator instance (local runner or Vercel deployment).

Usage: python scripts/smoke.py BASE_URL [--full] [--strict]
  --full    also run the client -> bank e2e flow (creates a synthetic application)
  --strict  fail instead of skipping when Vercel Deployment Protection blocks access

Without --full the check only reads: readiness (database answers, schema is current), the page,
both logins, an authenticated read of the application list, the PDF of an already scored application
and the live change feed.

Env: DEMO_CLIENT_PASSWORD / DEMO_BANK_PASSWORD (fall back to DEMO_LOGIN_PASSWORD, default alfa-demo-2026),
     VERCEL_AUTOMATION_BYPASS_SECRET (optional, for protected Vercel deployments).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

PASSWORD = os.getenv('DEMO_LOGIN_PASSWORD', 'alfa-demo-2026')
PASSWORDS = {'client': os.getenv('DEMO_CLIENT_PASSWORD', PASSWORD), 'bank': os.getenv('DEMO_BANK_PASSWORD', PASSWORD)}
BYPASS = os.getenv('VERCEL_AUTOMATION_BYPASS_SECRET')


def call(base, path, method='GET', body=None, token=None, headers=None):
    headers = {'Content-Type': 'application/json', **(headers or {})}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if BYPASS:
        headers['x-vercel-protection-bypass'] = BYPASS
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read().decode()


def wait_ready(base, attempts=30):
    """Wait until /api/ready answers 200: the process is up and the database is reachable."""
    last = 'нет ответа'
    for i in range(attempts):
        try:
            status, text = call(base, '/api/ready')
            if status == 200 and json.loads(text).get('status') == 'ready':
                return json.loads(text)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise
            last = f'{e.code} {e.read().decode()[:200]}'
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = str(e)
        time.sleep(1)
    sys.exit(f'FAIL: {base}/api/ready не готов за {attempts} попыток: {last}')


def login(base, role):
    _, text = call(base, '/api/demo/login', 'POST', {'role': role, 'password': PASSWORDS[role]})
    return json.loads(text)['access_token']


def check_pdf(base, token, app_id):
    headers = {'Authorization': 'Bearer ' + token, **({'x-vercel-protection-bypass': BYPASS} if BYPASS else {})}
    with urllib.request.urlopen(urllib.request.Request(f'{base}/api/applications/{app_id}/decision.pdf', headers=headers), timeout=20) as response:
        pdf = response.read()
    assert response.headers['Content-Type'] == 'application/pdf' and pdf.startswith(b'%PDF-'), 'решение не выгружается в PDF'
    print(f'ok  решение в PDF по заявке {app_id}: {len(pdf) // 1024} КБ')


def check_stream(base, token):
    """The first message of the change stream comes at once unless a proxy buffers the stream;
    a buffered stream is only a warning, the browser then polls /api/changes."""
    headers = {'Authorization': 'Bearer ' + token, **({'x-vercel-protection-bypass': BYPASS} if BYPASS else {})}
    started = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(base + '/api/changes/stream', headers=headers), timeout=30) as response:
        assert response.headers['Content-Type'].startswith('text/event-stream'), 'поток изменений отдаёт не text/event-stream'
        first = next((json.loads(line[5:]) for line in response if line.startswith(b'data:')), None)
        delay = time.monotonic() - started
    assert first and first['cursor'], 'в потоке изменений нет курсора'
    _, text = call(base, '/api/changes?after=' + first['cursor'], token=token)
    assert 'changes' in json.loads(text), '/api/changes не вернул изменения'
    if delay < 3:
        print(f'ok  поток изменений: первое сообщение за {delay:.1f} с')
    else:
        print(f'::warning::Поток изменений буферизуется (первое сообщение за {delay:.1f} с): браузер перейдёт на опрос /api/changes')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        sys.exit(__doc__)
    base = args[0].rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    try:
        ready = wait_ready(base)
    except urllib.error.HTTPError as e:
        # The app never answers 401 on /api/ready, so this is Vercel Deployment Protection.
        message = (f'{base} закрыт Vercel Deployment Protection (401). '
                   'Задайте секрет VERCEL_AUTOMATION_BYPASS_SECRET или отключите защиту.')
        if '--strict' in sys.argv:
            sys.exit('FAIL: ' + message)
        print('::warning::' + message)
        return
    print(f"ok  ready: {ready['database']}, схема {ready['schema_version']}")

    status, page = call(base, '/')
    assert status == 200 and 'Альфа' in page, 'главная страница не отдаётся'
    print('ok  index.html')

    client, bank = login(base, 'client'), login(base, 'bank')
    print('ok  login client/bank')

    _, text = call(base, '/api/applications', token=bank)
    queue = json.loads(text)
    print(f'ok  чтение очереди из базы: {len(queue)} заявок')
    # an already scored application, so production checks the PDF (fonts, Pillow) without writing anything
    scored = next((x['id'] for x in queue if x['status'] in ('approved', 'counteroffer', 'rejected', 'accepted', 'expired')), None)
    if scored:
        check_pdf(base, bank, scored)
    check_stream(base, client)

    if '--full' in sys.argv:
        application = {'company': 'ООО Smoke-тест', 'purpose': 'Проверка CI', 'amount': 1_000_000, 'months': 24,
                       'revenue': 1_600_000, 'available_cashflow': 240_000, 'existing_payments': 30_000, 'overdue': False}
        key = {'Idempotency-Key': 'smoke-' + uuid4().hex}
        _, text = call(base, '/api/applications', 'POST', application, client, key)
        app_id = json.loads(text)['id']
        status, text = call(base, '/api/applications', 'POST', application, client, key)
        assert status == 200 and json.loads(text)['id'] == app_id, 'повтор с тем же Idempotency-Key создал вторую заявку'
        _, text = call(base, f'/api/applications/{app_id}/evaluate', 'POST', token=bank)
        scored = json.loads(text)
        assert scored['status'] == 'approved', f"ожидалось approved, получено {scored['status']}"
        offer = scored['explanation']['offer']
        _, text = call(base, f'/api/applications/{app_id}/accept', 'POST', {'offer_id': offer['offer_id']}, client,
                       {'If-Match': str(scored['version'])})
        accepted = json.loads(text)
        assert accepted['status'] == 'accepted' and accepted['explanation']['accepted_terms'] == offer
        print(f'ok  e2e: заявка {app_id} -> approved -> accepted, условия совпали с предложением')
        check_pdf(base, client, app_id)

    print(f'PASS {base}')


if __name__ == '__main__':
    main()
