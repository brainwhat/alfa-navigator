"""Browser tests run against a real uvicorn process with its own SQLite file."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = 'e2e-password'
TOKENS = {'client': 'e2e-client-token', 'bank': 'e2e-bank-token'}

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

@pytest.fixture(scope='session')
def server():
    port = free_port()
    db = Path(tempfile.mkdtemp()) / 'e2e.db'
    env = {k: v for k, v in os.environ.items() if k not in ('DATABASE_URL', 'POSTGRES_URL', 'VERCEL_ENV')}
    env |= {'DB_PATH': str(db), 'DEMO_LOGIN_PASSWORD': PASSWORD,
            'DEMO_CLIENT_KEY': TOKENS['client'], 'DEMO_BANK_KEY': TOKENS['bank']}
    log = open(db.parent / 'uvicorn.log', 'w')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(url + '/api/ready', timeout=1)
            break
        except OSError:
            if process.poll() is not None:
                break
            time.sleep(0.2)
    else:
        process.terminate()
        pytest.fail('сервер не поднялся: ' + (db.parent / 'uvicorn.log').read_text())
    yield url
    process.terminate()
    process.wait(10)
    log.close()

@pytest.fixture(scope='session')
def base_url(server):
    # pytest-playwright resolves page.goto('/') against this
    return server
