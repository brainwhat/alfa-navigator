"""Alfa Navigator: autonomous synthetic hackathon credit pipeline.

The app itself: request log and security headers, one error format, static files and the routers.
Modules: settings (environment), storage (database, migrations, event log), policy (scoring rules),
schedules (loan arithmetic), workflow (application state and transitions), schemas (API models),
handlers/* (HTTP endpoints, live updates in handlers/live), demo (synthetic data)."""
import json
import re
import sys
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import demo, settings
from app.handlers import analytics, applications, calculations, live, system
from app.settings import log
from app.storage import SCHEMA_VERSION, migrate, sql_ms

app = FastAPI(title='Альфа.Навигатор — демо API', version='1.1.0')

@app.middleware('http')
async def observe(request: Request, call_next):
    request_id = re.sub(r'[^A-Za-z0-9-]', '', request.headers.get('x-request-id', ''))[:64] or uuid4().hex[:16]
    sql = [0.0]
    sql_ms.set(sql)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception(json.dumps({'event': 'unhandled_error', 'request_id': request_id, 'path': request.url.path}))
        response = JSONResponse({'detail': 'Внутренняя ошибка сервера', 'request_id': request_id}, status_code=500)
    path = request.url.path
    response.headers['X-Request-ID'] = request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # the two-screen page /split frames the app itself; other sites still cannot
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith('/static/'):
        # index.html links static files with ?v=<content hash>, so a versioned URL never changes
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if 'v' in request.query_params else 'public, max-age=3600'
    else:
        response.headers['Cache-Control'] = 'no-store'
    if path.startswith('/api/'):
        log.info(json.dumps({'event': 'request', 'request_id': request_id, 'method': request.method, 'path': path,
                             'status': response.status_code, 'ms': round((time.perf_counter() - started) * 1000, 1),
                             'sql_ms': round(sql[0], 1)}))
    return response

FIELD_NAMES = {'company':'Компания', 'purpose':'Цель', 'amount':'Сумма', 'months':'Срок', 'revenue':'Месячная выручка',
               'available_cashflow':'Свободный денежный поток', 'existing_payments':'Платежи по действующим долгам',
               'overdue':'Просрочки', 'reason':'Комментарий', 'note':'Комментарий', 'fields':'Поля для уточнения',
               'password':'Пароль', 'role':'Роль', 'decision':'Решение', 'offer':'Вариант условий', 'annual_rate':'Ставка',
               'prepay_month':'Месяц досрочного погашения', 'prepay_amount':'Сумма досрочного погашения',
               'seasonality':'Сезонное падение', 'idempotency-key':'Idempotency-Key', 'days':'Период'}

def number(n):
    return f'{n:,}'.replace(',', ' ') if isinstance(n, int) else str(n)

def validation_message(error) -> str:
    ctx = error.get('ctx') or {}
    messages = {
        'missing': lambda: 'обязательное поле',
        'greater_than_equal': lambda: f"не меньше {number(ctx['ge'])}",
        'greater_than': lambda: f"больше {number(ctx['gt'])}",
        'less_than_equal': lambda: f"не больше {number(ctx['le'])}",
        'less_than': lambda: f"меньше {number(ctx['lt'])}",
        'string_too_short': lambda: f"не короче {ctx['min_length']} символов",
        'string_too_long': lambda: f"не длиннее {ctx['max_length']} символов",
        'too_short': lambda: f"нужно выбрать хотя бы {ctx['min_length']}",
        'int_parsing': lambda: 'нужно целое число',
        'int_type': lambda: 'нужно целое число',
        'int_from_float': lambda: 'нужно целое число',
        'float_parsing': lambda: 'нужно число',
        'bool_parsing': lambda: 'нужно «да» или «нет»',
        'literal_error': lambda: f"допустимые значения: {ctx['expected']}",
        'json_invalid': lambda: 'некорректный JSON',
    }
    try:
        return messages[error['type']]()
    except KeyError:
        return error['msg']

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """One error shape for the whole API: a readable detail plus per-field messages."""
    errors = []
    for e in exc.errors():
        field = next((str(x) for x in reversed(e['loc']) if isinstance(x, str) and x not in ('body', 'query', 'header')), None)
        errors.append({'field': field, 'message': validation_message(e)})
    detail = '; '.join(f"{FIELD_NAMES.get(e['field'], e['field'])}: {e['message']}" if e['field'] else e['message'] for e in errors)
    return JSONResponse({'detail': 'Проверьте данные. ' + detail, 'errors': errors}, status_code=422)

app.mount('/static', StaticFiles(directory=settings.ROOT / 'static'), name='static')
for module in (system, calculations, applications, analytics, live, demo):
    app.include_router(module.router)

if settings.MIGRATE_ON_START:
    migrate()

if __name__ == '__main__' and sys.argv[1:] == ['migrate']:
    migrate()
    print(f'schema version {SCHEMA_VERSION}')
