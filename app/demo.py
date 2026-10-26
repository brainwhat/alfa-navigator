"""Synthetic demo applications with backdated history, and the seed/reset endpoints."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app import settings
from app.auth import require
from app.handlers.applications import accept, clarify, evaluate, manual_decision, request_info, withdraw
from app.schemas import AcceptInput, ApplicationInput, Clarification, DeletedOut, ManualDecision, RequestInfo, SeedOut
from app.storage import clock, conn
from app.workflow import insert_application

router = APIRouter()

# Synthetic history for demos: (company, purpose, terms, hours since submission, steps).
# A step is (hours after the previous one, action, *arguments); every step runs the real endpoint.
DEMO_PREFIX = 'DEMO'  # real ids are hex, so they never start with it
SMALL = dict(revenue=900_000, available_cashflow=210_000, existing_payments=15_000)
TIGHT = dict(revenue=1_600_000, available_cashflow=240_000, existing_payments=30_000)
DEMO_SCENARIOS = [
    ('ООО «Цветочная лавка»', 'Закупка сезонного товара', dict(amount=500_000, months=12, **SMALL), 0.5, []),
    ('ООО «Пекарня на Садовой»', 'Второй цех', dict(amount=1_200_000, months=24, **SMALL), 3, []),
    ('ИП Смирнова А. В.', 'Ремонт салона', dict(amount=600_000, months=18, revenue=450_000, available_cashflow=120_000, existing_payments=0), 20, []),
    ('ООО «ТехноСервис»', 'Оборудование для мастерской', dict(amount=2_500_000, months=36, revenue=1_800_000, available_cashflow=300_000, existing_payments=40_000), 31, []),
    ('ООО «Зелёный квартал»', 'Благоустройство территории', dict(amount=4_000_000, months=48, revenue=2_500_000, available_cashflow=380_000, existing_payments=60_000), 54, []),
    ('ООО «Логистик Плюс»', 'Два грузовых фургона', dict(amount=3_000_000, months=24, revenue=2_200_000, available_cashflow=260_000, existing_payments=60_000), 40,
     [(5, 'request_info', ['available_cashflow', 'existing_payments'], 'Подтвердите денежный поток и текущие платежи по лизингу')]),
    ('ООО «Кофейня Бриз»', 'Открытие точки у метро', dict(amount=1_500_000, months=24, revenue=800_000, available_cashflow=150_000, existing_payments=20_000), 30,
     [(3, 'request_info', ['available_cashflow', 'revenue'], 'Уточните выручку и поток с учётом летнего сезона'),
      (20, 'clarify', dict(available_cashflow=210_000, revenue=1_100_000), 'Приложили выписку за лето')]),
    ('ООО «Мастерская Уюта»', 'Станки для мебели', dict(amount=800_000, months=24, revenue=700_000, available_cashflow=180_000, existing_payments=0), 26, [(4, 'evaluate')]),
    ('ООО «Фермерский двор»', 'Холодильная камера', dict(amount=1_000_000, months=36, **SMALL), 160, [(2, 'evaluate')]),
    ('ООО «Гостевой дом»', 'Строительство гостевых домиков', dict(amount=5_000_000, months=60, revenue=1_000_000, available_cashflow=250_000, existing_payments=0), 110,
     [(7, 'evaluate'), (16, 'accept', None)]),
    ('ООО «Швейный цех»', 'Промышленные швейные машины', dict(amount=1_500_000, months=24, revenue=900_000, available_cashflow=120_000, existing_payments=0), 44, [(9, 'evaluate')]),
    ('ООО «Северный склад»', 'Расширение склада', dict(amount=5_000_000, months=24, **TIGHT), 50, [(12, 'evaluate')]),
    ('ООО «Типография Литера»', 'Цифровая печатная машина', dict(amount=3_000_000, months=12, **TIGHT), 12, [(6, 'evaluate')]),
    ('ООО «АвтоДеталь»', 'Пополнение склада запчастей', dict(amount=2_000_000, months=36, revenue=1_500_000, available_cashflow=300_000, existing_payments=20_000), 230,
     [(3, 'evaluate'), (20, 'accept', None)]),
    ('ООО «Студия Вектор»', 'Рабочие станции для дизайнеров', dict(amount=3_000_000, months=12, **TIGHT), 200, [(8, 'evaluate'), (30, 'accept', 'term')]),
    ('ООО «Чистый город»', 'Поломоечные машины', dict(amount=5_000_000, months=24, **TIGHT), 180, [(30, 'evaluate'), (10, 'accept', 'amount')]),
    ('ИП Ковалёв Д. С.', 'Оборудование для шиномонтажа', dict(amount=900_000, months=24, **SMALL), 100, [(1, 'evaluate'), (2, 'accept', None)]),
    ('ООО «Ремстрой»', 'Строительная техника', dict(amount=2_000_000, months=24, revenue=1_400_000, available_cashflow=280_000, existing_payments=30_000, overdue=True), 70, [(5, 'evaluate')]),
    ('ООО «Старт-Ап Лаб»', 'Разработка приложения', dict(amount=1_000_000, months=12, revenue=200_000, available_cashflow=60_000, existing_payments=50_000), 120, [(26, 'evaluate')]),
    ('ООО «Городской сад»', 'Теплица', dict(amount=1_500_000, months=36, revenue=1_000_000, available_cashflow=230_000, existing_payments=10_000), 220, [(4, 'evaluate')]),
    ('ООО «Медиа Пульс»', 'Студийное оборудование', dict(amount=3_000_000, months=12, **TIGHT), 250, [(10, 'evaluate')]),
    ('ООО «Вкусный дом»', 'Кухонное оборудование', dict(amount=1_200_000, months=24, **SMALL), 90, [(6, 'evaluate'), (15, 'withdraw')]),
    ('ООО «Энергосбыт-Регион»', 'Модернизация подстанции', dict(amount=1_500_000, months=36, revenue=1_200_000, available_cashflow=260_000, existing_payments=10_000, overdue=True), 60,
     [(3, 'evaluate'), (5, 'decision', 'approved', 'Просрочка была технической и закрыта, подтверждено справкой')]),
    ('ООО «Быстрый маркет»', 'Ремонт торгового зала', dict(amount=2_000_000, months=24, revenue=1_500_000, available_cashflow=320_000, existing_payments=20_000), 80,
     [(2, 'evaluate'), (4, 'decision', 'rejected', 'Сомнения в источнике выручки, нужна выездная проверка')]),
    ('ООО «Северная рыба»', 'Рефрижератор', dict(amount=800_000, months=24, revenue=900_000, available_cashflow=200_000, existing_payments=0, overdue=True), 140,
     [(2, 'evaluate'), (6, 'decision', 'approved', 'Поручительство собственника покрывает риск просрочки'), (12, 'accept', None)]),
    ('ООО «Детский клуб Радуга»', 'Второй филиал', dict(amount=2_000_000, months=24, revenue=900_000, available_cashflow=160_000, existing_payments=20_000), 170,
     [(4, 'request_info', ['available_cashflow', 'revenue'], 'Подтвердите выручку и поток после открытия группы продлённого дня'),
      (18, 'clarify', dict(available_cashflow=260_000, revenue=1_300_000), 'Приложили отчёт за квартал'),
      (3, 'evaluate'), (8, 'accept', None)]),
]

def run_scenario(id, company, purpose, terms, age, steps, base):
    at = base - timedelta(hours=age)
    clock.set(at)
    insert_application(ApplicationInput(company=company, purpose=purpose, **terms), id)
    for hours, action, *args in steps:
        at += timedelta(hours=hours)
        clock.set(at)
        if action == 'evaluate': evaluate(id, expected=None, role='bank')
        elif action == 'request_info': request_info(id, RequestInfo(fields=args[0], reason=args[1]), expected=None, role='bank')
        elif action == 'clarify': clarify(id, Clarification(note=args[1], **args[0]), expected=None, role='client')
        elif action == 'accept': accept(id, AcceptInput(offer=args[0]), expected=None, role='client')
        elif action == 'withdraw': withdraw(id, expected=None, role='client')
        elif action == 'decision': manual_decision(id, ManualDecision(decision=args[0], reason=args[1]), expected=None, role='bank')

def clear_demo(c):
    c.execute('DELETE FROM events WHERE application_id LIKE ?', (DEMO_PREFIX + '%',))
    return c.execute('DELETE FROM applications WHERE id LIKE ?', (DEMO_PREFIX + '%',)).rowcount

def demo_seed_enabled():
    if not settings.ENABLE_DEMO_SEED:
        raise HTTPException(403, 'Демо-данные отключены (ENABLE_DEMO_SEED)')

@router.post('/api/demo/seed', response_model=SeedOut)
def seed_demo(role: str = Depends(require('bank'))):
    """Recreate the synthetic demo applications; real applications are left alone."""
    demo_seed_enabled()
    base = datetime.now(timezone.utc)
    with conn() as c:
        clear_demo(c)
        token = clock.set(None)
        try:
            for n, scenario in enumerate(DEMO_SCENARIOS):
                run_scenario(f'{DEMO_PREFIX}{n:06d}', *scenario, base)
        finally:
            clock.reset(token)
    return {'created': len(DEMO_SCENARIOS)}

@router.delete('/api/demo/seed', response_model=DeletedOut)
def delete_demo(role: str = Depends(require('bank'))):
    demo_seed_enabled()
    with conn() as c:
        return {'deleted': clear_demo(c)}

@router.post('/api/demo/reset')
def reset_demo(role: str = Depends(require('bank'))):
    """For local synthetic demos only. Do not expose in a real bank."""
    if not settings.ENABLE_DEMO_RESET:
        raise HTTPException(403, 'Сброс отключён')
    with conn() as c:
        c.execute('DELETE FROM events')
        c.execute('DELETE FROM applications')
    return {'ok':True}
