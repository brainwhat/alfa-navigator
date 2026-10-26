"""Live updates: what changed in the event log since a cursor, as one answer or as a server-sent event stream."""
import asyncio
import json
import time

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import settings
from app.auth import validate_any
from app.schemas import Changes
from app.storage import conn

router = APIRouter()

LIMIT = 50
# Cursor "<last event id>-<number of applications>": the event id catches every command,
# the count catches deleted demo data, which leaves no events behind.
CURSOR = r'^\d{1,18}-\d{1,9}$'

def changes(after: str | None):
    with conn() as c:
        last = c.execute('SELECT COALESCE(MAX(id), 0) AS id FROM events').fetchone()['id']
        count = c.execute('SELECT COUNT(*) AS n FROM applications').fetchone()['n']
        result = {'cursor': f'{last}-{count}', 'changes': [], 'reset': False}
        if not after:
            return result
        seen, known = map(int, after.split('-'))
        # Postgres may commit a smaller event id after a larger one; such an event only misses its notice,
        # the data itself is still read by the browser's next refresh.
        rows = c.execute('''SELECT e.application_id, a.company, e.actor, e.action, e.description, e.occurred_at
            FROM events e LEFT JOIN applications a ON a.id = e.application_id WHERE e.id > ? ORDER BY e.id DESC LIMIT ?''',
            (seen, LIMIT + 1)).fetchall()
        result['changes'] = [dict(r) for r in reversed(rows[:LIMIT])]
        result['reset'] = count != known or last < seen or len(rows) > LIMIT
        return result

@router.get('/api/changes', response_model=Changes)
def changes_since(after: str | None = Query(None, pattern=CURSOR, description='Курсор из предыдущего ответа; без него — только текущий курсор'),
                  authorization: str | None = Header(None)):
    """Changes since the cursor in one answer: the fallback where a proxy buffers the event stream."""
    validate_any(authorization)
    return changes(after)

def message(data) -> str:
    return 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'

async def stream(request: Request, after: str | None):
    # The first message goes out at once: the browser learns the cursor and that the stream is not buffered.
    state = await run_in_threadpool(changes, after)
    yield 'retry: 2000\n\n' + message(state)
    cursor, deadline = state['cursor'], time.monotonic() + settings.STREAM_SECONDS
    while time.monotonic() + settings.STREAM_POLL_SECONDS < deadline:
        await asyncio.sleep(settings.STREAM_POLL_SECONDS)
        if await request.is_disconnected():
            return
        state = await run_in_threadpool(changes, cursor)
        if state['cursor'] != cursor:
            cursor = state['cursor']
            yield message(state)

@router.get('/api/changes/stream', response_class=StreamingResponse,
            responses={200: {'content': {'text/event-stream': {}}, 'description': 'Поток событий: в data тот же объект, что у /api/changes'}})
def changes_stream(request: Request, after: str | None = Query(None, pattern=CURSOR, description='Курсор, с которого продолжить'),
                   authorization: str | None = Header(None)):
    """Server-sent events: the current cursor at once, then every change while the stream lives (STREAM_SECONDS).
    The stream then ends and the browser reconnects with the last cursor, so nothing is lost in between."""
    validate_any(authorization)
    return StreamingResponse(stream(request, after), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
