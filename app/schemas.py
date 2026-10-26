"""Request and response models of the API; they also produce the published OpenAPI schema."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.policy import RATE

class LoginInput(BaseModel):
    role: Literal['client','bank']
    password: str = Field(max_length=200)

class LoginOut(BaseModel):
    role: str
    access_token: str
    token_type: str
    warning: str

class LoanTerms(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    revenue: int = Field(gt=0, le=1_000_000_000)
    available_cashflow: int = Field(gt=0, le=1_000_000_000, description='Выручка за вычетом операционных расходов и налогов, до платежей по действующим кредитам')
    existing_payments: int = Field(ge=0, le=1_000_000_000, description='Ежемесячные платежи по действующим кредитам и лизингу')
    overdue: bool = False

class StatementPeriod(BaseModel):
    from_: str = Field(alias='from', description='Первая операция, ГГГГ-ММ-ДД')
    to: str = Field(description='Последняя операция, ГГГГ-ММ-ДД')
    months: int = Field(description='Полных месяцев в средних')
    model_config = ConfigDict(populate_by_name=True)

class StatementMonth(BaseModel):
    month: str = Field(description='ГГГГ-ММ')
    revenue: int
    expenses: int = Field(description='Операционные расходы и налоги')
    debt: int = Field(description='Платежи по кредитам и лизингу')
    cashflow: int = Field(description='Выручка минус операционные расходы')

class Seasonality(BaseModel):
    profile: list[float | None] = Field(min_length=12, max_length=12, description='Поток каждого календарного месяца (январь первым) к среднему; null — месяца нет в выписке')
    worst_month: int | None = Field(None, description='Календарный месяц с наименьшим потоком')
    drop_pct: int = Field(description='На сколько процентов поток худшего месяца ниже среднего')
    months_covered: int

class StatementFigures(BaseModel):
    period: StatementPeriod
    operations: int
    revenue: int = Field(description='Средняя выручка в месяц')
    expenses: int
    available_cashflow: int = Field(description='Средний свободный поток: выручка минус операционные расходы и налоги')
    existing_payments: int = Field(description='Средние платежи по кредитам и лизингу')
    monthly: list[StatementMonth]
    seasonality: Seasonality

class StatementSummary(StatementFigures):
    signature: str = Field(max_length=64, description='Подпись сервера: заявка примет сводку, только если её не меняли')

class StatementCheck(BaseModel):
    field: str
    declared: int = Field(description='Значение в заявке')
    statement: int = Field(description='Значение по выписке')
    confirmed: bool = Field(description='Совпадает с выпиской с точностью до 1%')
    diff: float = Field(description='Отклонение заявленного от выписки, доля')

class StoredStatement(StatementFigures):
    check: list[StatementCheck] = []

class StatementCategory(BaseModel):
    id: str
    name: str
    total: int
    count: int
    examples: list[str] = Field(description='Крупнейшие по сумме назначения платежей; не сохраняются')

class StatementAnalysis(BaseModel):
    summary: StatementSummary = Field(description='Передайте в поле statement заявки как есть')
    categories: list[StatementCategory]
    warnings: list[str]

class StatementInput(BaseModel):
    csv: str = Field(min_length=10, max_length=3_000_000, description='Текст выписки в CSV; разделитель ; , или табуляция')

class ApplicationInput(LoanTerms):
    company: str = Field(min_length=2, max_length=120)
    purpose: str = Field(min_length=3, max_length=200)
    previous_id: str | None = Field(None, max_length=20, description='Заявка, вместо которой подаётся эта (повторная подача)')
    statement: StatementSummary | None = Field(None, description='Сводка выписки из /api/statement/analyze; банк сверит с ней заявленные цифры')

CLARIFY_FIELDS = {'available_cashflow':'свободный денежный поток', 'revenue':'месячная выручка',
                  'existing_payments':'платежи по действующим долгам', 'overdue':'наличие просрочек'}
ClarifyField = Literal['available_cashflow','revenue','existing_payments','overdue']

class RequestInfo(BaseModel):
    reason: str = Field(min_length=5, max_length=300)
    fields: list[ClarifyField] = Field(default_factory=lambda: ['available_cashflow'], min_length=1)

class Clarification(BaseModel):
    note: str = Field(min_length=3, max_length=300)
    available_cashflow: int | None = Field(None, gt=0, le=1_000_000_000)
    revenue: int | None = Field(None, gt=0, le=1_000_000_000)
    existing_payments: int | None = Field(None, ge=0, le=1_000_000_000)
    overdue: bool | None = None

class AcceptInput(BaseModel):
    offer: Literal['amount','term'] | None = Field(None, description='Вариант условий для контрпредложения; по умолчанию первый')
    offer_id: str | None = Field(None, max_length=40, description='Номер предложения, которое видел клиент; если банк его заменил, ответ 409')

class ManualDecision(BaseModel):
    decision: Literal['approved','rejected']
    reason: str = Field(min_length=10, max_length=300)

class Flexible(BaseModel):
    # explanations of older applications may carry extra or missing keys; keep whatever is stored
    model_config = ConfigDict(extra='allow')

class Terms(Flexible):
    id: str = Field(description='requested — запрошенные условия, amount — меньше сумма, term — дольше срок')
    amount: int
    months: int
    annual_rate: float | None = Field(None, description='Фактическая ставка этих условий')
    monthly_payment: int | None = None
    total_paid: int | None = None
    overpayment: int | None = None
    offer_id: str | None = Field(None, description='Есть только у выданного предложения; его условия не меняются')
    policy_version: str | None = None
    issued_at: str | None = None
    basis: Literal['auto','manual'] | None = Field(None, description='auto — по скорингу, manual — решение сотрудника')
    score: int | None = Field(None, description='Оценка риска этих условий (для альтернатив)')
    payment_to_cashflow: float | None = Field(None, description='Нагрузка этих условий по базовой ставке (для альтернатив)')

class Contribution(BaseModel):
    factor: str
    points: int = Field(description='Изменение балла: 0 или отрицательное число')
    detail: str | None = None

class Assessment(BaseModel):
    score: int
    base_score: int
    reference_rate: float = Field(description='Базовая ставка, по которой считается нагрузка')
    monthly_payment: int = Field(description='Платёж по запрошенным условиям по базовой ставке')
    payment_to_cashflow: float
    contributions: list[Contribution]

class ManualOverride(Flexible):
    decision: str
    reason: str
    auto_decision: str | None = None
    at: str | None = None
    author: str | None = None

class Explanation(Flexible):
    policy_version: str | None = None
    reference_rate: float | None = None
    rate_tiers: list[tuple[int, float]] | None = None
    factors: list[str] = []
    assessment: Assessment | None = Field(None, description='Расчёт риска по запрошенным условиям')
    requested: Terms | None = Field(None, description='Запрошенные условия; если не одобрены — по базовой ставке')
    offer: Terms | None = Field(None, description='Выданное предложение на запрошенных условиях (одобрение)')
    offers: list[Terms] = Field([], description='Выданные альтернативные предложения (контрпредложение)')
    how_to_improve: list[str] = []
    accepted_terms: Terms | None = None
    manual_override: ManualOverride | None = None
    disclaimer: str | None = None

class InfoRequest(BaseModel):
    # requests made before field selection existed asked for the cash flow only
    fields: list[str] = ['available_cashflow']
    reason: str | None = None

class Event(BaseModel):
    occurred_at: str
    actor: str
    action: str
    description: str
    data: dict[str, Any] | None = None

WaitingOn = Literal['bank','client'] | None

class Application(BaseModel):
    id: str
    created_at: str
    updated_at: str
    company: str
    purpose: str
    amount: int
    months: int
    available_cashflow: int
    revenue: int
    existing_payments: int
    overdue: bool
    status: str
    version: int = Field(description='Растёт с каждым изменением; передавайте в If-Match')
    waiting_on: WaitingOn = None
    score: int | None = None
    decision: str | None = Field(None, description='Автоматическое решение скоринга')
    alternative_amount: int | None = None
    explanation: Explanation | None = None
    offer_expires_at: str | None = None
    info_request: InfoRequest | None = None
    previous_id: str | None = None
    statement: StoredStatement | None = Field(None, description='Сводка выписки и сверка с заявленными цифрами')
    events: list[Event]

class ListItem(BaseModel):
    id: str
    company: str
    amount: int
    months: int
    status: str
    created_at: str
    updated_at: str
    offer_expires_at: str | None = None
    version: int
    waiting_on: WaitingOn = None
    in_status_minutes: int
    sla_due_at: str | None = None
    sla_breached: bool

class Precheck(Explanation):
    score: int
    decision: str

class ScheduleInput(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    annual_rate: float = Field(RATE, ge=0.01, le=1)
    prepay_month: int | None = Field(None, ge=1, le=60, description='Месяц досрочного погашения')
    prepay_amount: int = Field(0, ge=0, le=20_000_000)
    prepay_mode: Literal['term','payment'] = Field('term', description='term — сократить срок, payment — уменьшить платёж')

class ScheduleRow(BaseModel):
    month: int
    payment: int
    principal: int
    interest: int
    prepayment: int
    balance: int

class ScheduleSummary(BaseModel):
    months: int
    total_paid: int
    overpayment: int

class ScheduleOut(ScheduleSummary):
    annual_rate: float
    monthly_payment: int
    rows: list[ScheduleRow]
    baseline: ScheduleSummary | None = Field(None, description='Тот же кредит без досрочного погашения')
    savings: int | None = None
    monthly_after: int | None = Field(None, description='Платёж в месяц после досрочного погашения')

class SimulationInput(BaseModel):
    amount: int = Field(ge=100_000, le=20_000_000)
    months: int = Field(ge=6, le=60)
    available_cashflow: int = Field(gt=0, le=1_000_000_000)
    existing_payments: int = Field(ge=0, le=1_000_000_000)
    seasonality: int = Field(ge=0, le=60, description='Процент сезонного падения свободного потока')
    profile: list[float | None] | None = Field(None, min_length=12, max_length=12,
        description='Сезонность по выписке: поток каждого календарного месяца к среднему; заменяет seasonality')
    start_month: int | None = Field(None, ge=1, le=12, description='Календарный месяц первого платежа (1 — январь); по умолчанию следующий месяц')

class SimulationMonth(BaseModel):
    month: int = Field(description='Номер месяца с начала кредита')
    calendar_month: int = Field(description='Календарный месяц, 1 — январь')
    winter: bool = Field(description='Сезонный спад: зима при ручном сценарии, месяц ниже среднего при профиле выписки')
    cashflow: int
    loan_payment: int = Field(description='Платёж по графику; 0 после погашения кредита')
    existing_payments: int
    balance_after_debt: int

class SimulationOut(BaseModel):
    annual_rate: float = Field(description='Базовая ставка симуляции; итоговая ставка зависит от скоринга')
    monthly_payment: int
    base_load: float
    stress_load: float
    start_month: int
    timeline: list[SimulationMonth]
    safe_under_stress: bool
    disclaimer: str

class Health(BaseModel):
    status: str
    database: Literal['postgres','sqlite']

class Readiness(Health):
    schema_version: int | None = None
    detail: str | None = None

class Analytics(BaseModel):
    period_days: int | None
    total: int
    by_status: dict[str, int] = Field(description='Текущие статусы заявок')
    funnel: list[dict[str, Any]] = Field(description='Исторические этапы: заявка учитывается, если хоть раз достигла этапа')
    auto_decisions: dict[str, int]
    manual: dict[str, int]
    offers: dict[str, int] = Field(description='Чем закончились заявки, получившие предложение')
    top_factors: list[dict[str, Any]]
    approved_rates: list[dict[str, Any]]
    accepted_rates: list[dict[str, Any]]
    avg_requested_amount: int | None
    avg_accepted_amount: int | None
    sla: dict[str, Any]

class SeedOut(BaseModel):
    created: int

class DeletedOut(BaseModel):
    deleted: int

class Change(BaseModel):
    application_id: str
    company: str | None = Field(None, description='Нет, если заявку уже удалили')
    actor: str
    action: str
    description: str
    occurred_at: str

class Changes(BaseModel):
    cursor: str = Field(description='Передайте в after следующего запроса, чтобы получить только новые изменения')
    changes: list[Change] = Field(description='Новые события журнала, старые первыми')
    reset: bool = Field(description='Изменился состав заявок или изменений больше, чем в ответе: перечитайте список целиком')
