"""Smoke check for a running Alfa Navigator instance (local runner or Vercel deployment).

Usage: python scripts/smoke.py BASE_URL [--full] [--strict]
  --full    also run the client -> bank e2e flow (creates a synthetic application)
  --strict  fail instead of skipping when Vercel Deployment Protection blocks access

Env: DEMO_LOGIN_PASSWORD (default alfa-demo-2026),
     VERCEL_AUTOMATION_BYPASS_SECRET (optional, for protected Vercel deployments).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PASSWORD = os.getenv('DEMO_LOGIN_PASSWORD', 'alfa-demo-2026')
BYPASS = os.getenv('VERCEL_AUTOMATION_BYPASS_SECRET')


def call(base, path, method='GET', body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if BYPASS:
        headers['x-vercel-protection-bypass'] = BYPASS
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read().decode()


def wait_healthy(base, attempts=30):
    for i in range(attempts):
        try:
            status, text = call(base, '/api/health')
            if status == 200 and json.loads(text).get('status') == 'ok':
                return
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    sys.exit(f'FAIL: {base}/api/health не ответил за {attempts} попыток')


def login(base, role):
    _, text = call(base, '/api/demo/login', 'POST', {'role': role, 'password': PASSWORD})
    return json.loads(text)['access_token']


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        sys.exit(__doc__)
    base = args[0].rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    try:
        wait_healthy(base)
    except urllib.error.HTTPError as e:
        # The app never answers 401 on /api/health, so this is Vercel Deployment Protection.
        message = (f'{base} закрыт Vercel Deployment Protection (401). '
                   'Задайте секрет VERCEL_AUTOMATION_BYPASS_SECRET или отключите защиту.')
        if '--strict' in sys.argv:
            sys.exit('FAIL: ' + message)
        print('::warning::' + message)
        return
    print('ok  health')

    status, page = call(base, '/')
    assert status == 200 and 'Альфа' in page, 'главная страница не отдаётся'
    print('ok  index.html')

    client, bank = login(base, 'client'), login(base, 'bank')
    print('ok  login client/bank')

    if '--full' in sys.argv:
        _, text = call(base, '/api/applications', 'POST', {
            'company': 'ООО Smoke-тест', 'purpose': 'Проверка CI', 'amount': 1_000_000, 'months': 24,
            'revenue': 1_600_000, 'available_cashflow': 240_000, 'existing_payments': 30_000,
            'overdue': False}, client)
        app_id = json.loads(text)['id']
        _, text = call(base, f'/api/applications/{app_id}/evaluate', 'POST', token=bank)
        decision = json.loads(text)['status']
        assert decision == 'approved', f'ожидалось approved, получено {decision}'
        _, text = call(base, f'/api/applications/{app_id}/accept', 'POST', token=client)
        assert json.loads(text)['status'] == 'accepted'
        print(f'ok  e2e: заявка {app_id} -> approved -> accepted')

    print(f'PASS {base}')


if __name__ == '__main__':
    main()
