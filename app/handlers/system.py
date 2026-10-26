"""Login, the page itself, liveness and readiness, the INN stub."""
import hashlib
import json
from functools import lru_cache

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app import settings
from app.auth import same, token_for, validate_any
from app.schemas import Health, LoginInput, LoginOut, Readiness
from app.settings import log
from app.storage import SCHEMA_VERSION, conn, schema_version

router = APIRouter()

@router.post('/api/demo/login', response_model=LoginOut)
def login(payload: LoginInput):
    if not same(payload.password, settings.DEMO_PASSWORDS[payload.role]):
        raise HTTPException(401, 'Неверный демонстрационный пароль')
    return {'role': payload.role, 'access_token': token_for(payload.role), 'token_type':'bearer', 'warning':'Учётные записи являются демонстрационными, не production IAM'}

STATIC_FILES = sorted(p for p in (settings.ROOT / 'static').rglob('*') if p.is_file())

@lru_cache(maxsize=1)
def index_html(stamp):
    """index.html with ?v=<content hash> on static links; rebuilt only when a static file changes."""
    digest = hashlib.sha256(b''.join(p.read_bytes() for p in STATIC_FILES if p.name != 'index.html')).hexdigest()[:10]
    return (settings.ROOT / 'static' / 'index.html').read_text(encoding='utf-8').replace('__ASSET_VERSION__', digest)

@router.get('/', response_class=HTMLResponse, include_in_schema=False)
def index(): return index_html(max(p.stat().st_mtime_ns for p in STATIC_FILES))

def engine():
    return 'postgres' if settings.DATABASE_URL else 'sqlite'

@router.get('/api/health', response_model=Health)
def health():
    """Liveness: the process answers. Whether it can serve requests is /api/ready."""
    return {'status':'ok', 'database':engine()}

@router.get('/api/ready', response_model=Readiness, response_model_exclude_none=True, responses={503: {'model': Readiness, 'description': 'База недоступна или схема не обновлена'}})
def ready():
    """Readiness: the database answers and its schema is current; 503 otherwise."""
    try:
        with conn() as c:
            c.execute('SELECT 1').fetchone()
            version = schema_version(c)
    except Exception as e:
        log.warning(json.dumps({'event': 'not_ready', 'error': type(e).__name__}))
        return JSONResponse({'status':'unavailable', 'database':engine(), 'detail':'База данных недоступна'}, status_code=503)
    if version != SCHEMA_VERSION:
        return JSONResponse({'status':'unavailable', 'database':engine(), 'schema_version':version,
                             'detail':f"Схема базы {version or 'не создана'}, приложению нужна {SCHEMA_VERSION}: выполните миграции"}, status_code=503)
    return {'status':'ready', 'database':engine(), 'schema_version':version}

@router.get('/api/mock/company/{inn}')
def mock_company(inn: str, authorization: str | None = Header(None)):
    validate_any(authorization)
    if len(inn) not in (10,12) or not inn.isdigit():
        raise HTTPException(422, 'Синтетический ИНН должен содержать 10 или 12 цифр')
    return {'inn':inn, 'source':'LOCAL_SYNTHETIC_FNS_STUB', 'verified':True, 'company':'ООО Демонстрационный бизнес', 'note':'Не является запросом в ФНС или подтверждением существования компании'}
