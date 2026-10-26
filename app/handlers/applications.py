"""Applications: lists, creation and the commands of both roles. Each command checks the role,
the status and the version the user saw, then goes through workflow.transition."""
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from app.auth import require, validate_any
from app.documents import decision_pdf
from app.policy import POLICY_VERSION, RATE, offer, rate_for, rub, score_application
from app.schemas import (AcceptInput, Application, ApplicationInput, CLARIFY_FIELDS, Clarification, ListItem,
                         ManualDecision, RequestInfo)
from app.storage import conn, now
from app.workflow import (OPEN_OFFER, enriched, expire_offers, insert_application, issue, load_for_change,
                          offer_deadline, offered_terms, queue_item, stamped, transition)

router = APIRouter()
# exclude_unset: optional explanation keys appear only when stored, not as nulls
APPLICATION = dict(response_model=Application, response_model_exclude_unset=True)

def expected_version(if_match: str | None = Header(None, description='Версия заявки (поле version), которую видел пользователь. Если заявка с тех пор изменилась, ответ 409.')):
    if if_match is None:
        return None
    try:
        return int(if_match.removeprefix('W/').strip('"'))
    except ValueError:
        raise HTTPException(422, 'If-Match должен содержать номер версии заявки')

def queue():
    at = datetime.now(timezone.utc)
    with conn() as c:
        expire_offers(c)
        rows = c.execute('SELECT id, company, amount, months, status, created_at, updated_at, offer_expires_at, version FROM applications ORDER BY created_at DESC')
        return [queue_item(dict(row), at) for row in rows]

@router.get('/api/my-applications', response_model=list[ListItem])
def my_applications(role: str = Depends(require('client'))):
    # In demo there is exactly one synthetic customer; this is NOT multi-tenant isolation.
    return queue()

@router.get('/api/applications', response_model=list[ListItem])
def list_applications(role: str = Depends(require('bank'))):
    return queue()

@router.post('/api/applications', status_code=201, **APPLICATION)
def create_application(payload:ApplicationInput, response: Response,
                       idempotency_key: str | None = Header(None, max_length=100, description='Повтор запроса с тем же ключом вернёт уже созданную заявку'),
                       role: str = Depends(require('client'))):
    row = insert_application(payload, uuid4().hex[:10].upper(), idempotency_key)
    if row.pop('replayed', False):
        response.status_code = 200
        response.headers['Idempotency-Replayed'] = 'true'
    return row

@router.get('/api/applications/{id}', **APPLICATION)
def get_application(id:str, authorization: str | None = Header(None)):
    validate_any(authorization)
    with conn() as c:return enriched(c,id)

@router.get('/api/applications/{id}/decision.pdf', response_class=Response,
            responses={200: {'content': {'application/pdf': {}}, 'description': 'Документ решения: условия, оценка, график платежей, история'},
                       409: {'description': 'Заявку ещё не оценили'}})
def decision_document(id:str, authorization: str | None = Header(None)):
    """The decision as a PDF with the application number, dates, rules version and a demo mark."""
    validate_any(authorization)
    with conn() as c:
        x=enriched(c,id)
    if x['score'] is None:
        raise HTTPException(409, 'Решения ещё нет: банк не оценил заявку')
    return Response(decision_pdf(x), media_type='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="alfa-navigator-{id}.pdf"'})

@router.post('/api/applications/{id}/evaluate', **APPLICATION)
def evaluate(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Оценка возможна только для новой или уточнённой заявки')
        score,decision,alternative,explanation=score_application(row)
        deadline=offer_deadline() if decision in OPEN_OFFER else None
        transition(c,row,'bank','evaluated',f'Скоринг {score}; результат: {decision}; правила {POLICY_VERSION}',{'decision':decision,'score':score},
                   status=decision,score=score,decision=decision,alternative_amount=alternative,
                   explanation=json.dumps(stamped(explanation,'auto'),ensure_ascii=False),offer_expires_at=deadline)
        return enriched(c,id)

@router.post('/api/applications/{id}/request-info', **APPLICATION)
def request_info(id:str, payload:RequestInfo, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','clarified'):
            raise HTTPException(409, 'Уточнение на этом этапе недоступно')
        fields=list(dict.fromkeys(payload.fields))
        transition(c,row,'bank','requested_info',f"{payload.reason} (уточнить: {', '.join(CLARIFY_FIELDS[f] for f in fields)})",{'fields':fields},
                   status='needs_info',info_request=json.dumps({'fields':fields,'reason':payload.reason},ensure_ascii=False))
        return enriched(c,id)

@router.post('/api/applications/{id}/clarify', **APPLICATION)
def clarify(id:str, payload:Clarification, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status']!='needs_info': raise HTTPException(409,'Уточнение не запрошено')
        # requests made before field selection existed asked for the cash flow only
        requested=json.loads(row['info_request'])['fields'] if row.get('info_request') else ['available_cashflow']
        values=payload.model_dump(exclude_none=True, exclude={'note'})
        if missing:=[CLARIFY_FIELDS[f] for f in requested if f not in values]:
            raise HTTPException(422, 'Заполните запрошенные поля: '+', '.join(missing))
        if extra:=[CLARIFY_FIELDS[f] for f in values if f not in requested]:
            raise HTTPException(422, 'Банк не запрашивал эти поля: '+', '.join(extra))
        shown=lambda f,v: ('есть' if v else 'нет') if f=='overdue' else rub(v)
        changed={f:v for f,v in values.items() if v!=(bool(row[f]) if f=='overdue' else row[f])}
        changes='; '.join(f'{CLARIFY_FIELDS[f]}: {shown(f,row[f])} → {shown(f,v)}' for f,v in changed.items()) or 'данные подтверждены без изменений'
        values={f:int(v) if f=='overdue' else v for f,v in values.items()}  # column names come from the ClarifyField whitelist
        transition(c,row,'client','clarified',f'{payload.note}. Изменения: {changes}',{'changed':sorted(changed)},
                   **values,status='clarified',info_request=None)
        return enriched(c,id)

@router.post('/api/applications/{id}/accept', **APPLICATION)
def accept(id:str, payload: AcceptInput | None = None, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in OPEN_OFFER:
            raise HTTPException(409, 'Принять можно только одобренные условия')
        explanation=json.loads(row['explanation'] or '{}')
        if row['status']=='approved':
            # the offer stored at approval, never repriced: tariffs may have changed since
            terms=offered_terms(explanation) or offer('requested', row['amount'], row['months'], rate_for(row['score']))
        else:
            # applications scored before offers existed only have alternative_amount
            offers=explanation.get('offers') or [offer('amount', row['alternative_amount'], row['months'], RATE)]
            choice=payload.offer if payload else None
            terms=next((o for o in offers if choice in (None, o['id'])), None)
            if terms is None: raise HTTPException(422, 'Такого варианта условий нет')
        if payload and payload.offer_id and payload.offer_id!=terms.get('offer_id'):
            raise HTTPException(409, 'Банк изменил предложение. Обновите карточку и проверьте условия.')
        explanation['accepted_terms']=terms
        transition(c,row,'client','accepted',f"Клиент принял условия: {rub(terms['amount'])} на {terms['months']} мес. под {terms['annual_rate']*100:g}%; подписание и выдача смоделированы",
                   {'offer_id':terms.get('offer_id'),'amount':terms['amount'],'months':terms['months'],'annual_rate':terms['annual_rate']},
                   status='accepted',explanation=json.dumps(explanation,ensure_ascii=False))
        return enriched(c,id)

@router.post('/api/applications/{id}/extend', **APPLICATION)
def extend_offer(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    """Give the client another OFFER_VALID_DAYS, reopening an expired offer."""
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('approved','counteroffer','expired'):
            raise HTTPException(409, 'Продлить можно только действующее или истёкшее предложение')
        override=json.loads(row['explanation'] or '{}').get('manual_override') or {}
        status=row['status'] if row['status']!='expired' else 'approved' if override.get('decision')=='approved' else row['decision']
        deadline=offer_deadline()
        until=datetime.fromisoformat(deadline).strftime('%d.%m.%Y')
        transition(c,row,'bank','extended',f'Банк продлил предложение до {until}',{'until':deadline},status=status,offer_expires_at=deadline)
        return enriched(c,id)

@router.post('/api/applications/{id}/withdraw', **APPLICATION)
def withdraw(id:str, expected: int | None = Depends(expected_version), role: str = Depends(require('client'))):
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('submitted','needs_info','clarified','approved','counteroffer'):
            raise HTTPException(409, 'Эту заявку уже нельзя отозвать')
        transition(c,row,'client','withdrawn','Клиент отозвал заявку',status='withdrawn')
        return enriched(c,id)

@router.post('/api/applications/{id}/decision', **APPLICATION)
def manual_decision(id:str, payload:ManualDecision, expected: int | None = Depends(expected_version), role: str = Depends(require('bank'))):
    """Analyst overrides the automatic result; the auto decision stays in the decision column."""
    with conn() as c:
        row=load_for_change(c,id,expected)
        if row['status'] not in ('approved','counteroffer','rejected'):
            raise HTTPException(409, 'Ручное решение возможно только после скоринга и до принятия условий')
        if row['status']==payload.decision:
            raise HTTPException(409, 'Заявка уже в этом статусе')
        explanation=json.loads(row['explanation'])
        if payload.decision=='approved':
            # a new offer on the requested terms, priced by the score: 26% when the score is below auto-approval
            terms=offer('requested', row['amount'], row['months'], rate_for(row['score']))
            explanation['requested']=terms
            explanation['offer']=issue(terms,'manual')
        else:
            explanation['offer']=None
        # the demo has one shared bank role, so the author is the role, not a named employee
        explanation['manual_override']={'decision':payload.decision,'reason':payload.reason,'auto_decision':row['decision'],'at':now(),'author':'bank'}
        deadline=offer_deadline() if payload.decision=='approved' else None
        verdict='одобрил' if payload.decision=='approved' else 'отклонил'
        auto={'approved':'одобрение','counteroffer':'другие условия','rejected':'отказ'}.get(row['decision'], row['decision'])
        transition(c,row,'bank','manual_decision',f'Сотрудник банка вручную {verdict} заявку (автоскоринг: {auto}): {payload.reason}',
                   {'decision':payload.decision,'auto_decision':row['decision'],'offer_id':(explanation['offer'] or {}).get('offer_id')},
                   status=payload.decision,explanation=json.dumps(explanation,ensure_ascii=False),offer_expires_at=deadline)
        return enriched(c,id)
