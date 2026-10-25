"""Write the app's OpenAPI schema to docs/openapi.json; with --check, fail if the snapshot is out of date.

Usage: python scripts/export_openapi.py [--check]
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / 'docs' / 'openapi.json'
sys.path.insert(0, str(ROOT))
# Importing the app migrates a database; never touch a configured one just to read the schema.
os.environ.pop('DATABASE_URL', None)
os.environ.pop('POSTGRES_URL', None)
os.environ['DB_PATH'] = str(Path(tempfile.mkdtemp()) / 'openapi.db')

from app.main import app  # noqa: E402

schema = json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n'
if '--check' in sys.argv:
    if not TARGET.exists() or TARGET.read_text(encoding='utf-8') != schema:
        sys.exit('FAIL: docs/openapi.json не совпадает с API. Обновите: python scripts/export_openapi.py')
    print('ok  docs/openapi.json актуален')
else:
    TARGET.write_text(schema, encoding='utf-8')
    print(f'written {TARGET.relative_to(ROOT)}: {len(app.openapi()["paths"])} paths')
