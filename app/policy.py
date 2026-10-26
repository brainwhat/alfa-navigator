"""Scoring policy demo-rules-v1: score, decision, alternative offers and advice.

Illustrative deterministic rules on synthetic data; NOT a real banking credit decision."""
import math

from app.schedules import amortization, payment

POLICY_VERSION = 'demo-rules-v1'
# Reference rate: scoring measures the debt load at this rate, then the score sets the actual price.
RATE = 0.22
# (minimum score, annual rate). Auto-approvals and offers never price above RATE,
# so the load the scoring checked stays an upper bound; 26% is reachable only by a manual approval.
RATE_TIERS = [(85, 0.18), (70, 0.20), (65, 0.22), (0, 0.26)]
APPROVE_SCORE = 65
# Offers and advice keep all payments within 60% of free cash flow, stricter than the approval threshold.
OFFER_LOAD = 0.6
BASE_SCORE = 85
NEGATIVE_FACTORS = {'Высокая доля платежей в свободном денежном потоке', 'Повышенная долговая нагрузка',
                    'В синтетической истории указаны просрочки', 'Запрос существенно превышает месячную выручку'}

def rate_for(score) -> float:
    return next(rate for minimum, rate in RATE_TIERS if (score or 0) >= minimum)

def percent(x):
    return f'{x * 100:.1f}'.replace('.', ',') + '%'

def rub(n):
    return f'{n:,}'.replace(',', ' ') + ' ₽'

def assess(row):
    """Score, the payment at the reference rate, the debt load and how each factor moved the score."""
    monthly = payment(row['amount'], row['months'], RATE)
    load = (monthly + row['existing_payments']) / row['available_cashflow']
    if load > 0.75:
        factors = [('Высокая доля платежей в свободном денежном потоке', -40, f'нагрузка {percent(load)} выше 75%')]
    elif load > 0.6:
        factors = [('Повышенная долговая нагрузка', -20, f'нагрузка {percent(load)} выше 60%')]
    else:
        factors = [('Долговая нагрузка в пределах демонстрационного порога', 0, f'нагрузка {percent(load)}, порог 60%')]
    if row['overdue']:
        factors.append(('В синтетической истории указаны просрочки', -35, None))
    else:
        factors.append(('Просрочек в синтетических данных нет', 0, None))
    ratio = f"сумма равна {row['amount'] / row['revenue']:.1f}".replace('.', ',') + ' мес. выручки'
    if row['amount'] > row['revenue'] * 4:
        factors.append(('Запрос существенно превышает месячную выручку', -15, ratio + ', порог 4'))
    else:
        factors.append(('Сумма сопоставима с заявленной выручкой', 0, ratio))
    contributions = [{'factor':f, 'points':p, 'detail':d} for f, p, d in factors]
    return max(0, BASE_SCORE + sum(p for _, p, _ in factors)), monthly, load, contributions

def assessment(row):
    """Risk view of the requested terms: the load is measured at the reference rate, whatever the final price."""
    score, monthly, load, contributions = assess(row)
    return {'score':score, 'base_score':BASE_SCORE, 'reference_rate':RATE, 'monthly_payment':monthly,
            'payment_to_cashflow':round(load, 3), 'contributions':contributions}

def affordable(row):
    score, _, load, _ = assess(row)
    return score >= APPROVE_SCORE and load <= OFFER_LOAD

def offer(kind, amount, months, rate):
    total = sum(x['payment'] for x in amortization(amount, months, rate))
    return {'id':kind, 'amount':amount, 'months':months, 'annual_rate':rate, 'monthly_payment':payment(amount, months, rate), 'total_paid':total, 'overpayment':total-amount}

def priced(kind, row):
    score, _, load, _ = assess(row)
    return {**offer(kind, row['amount'], row['months'], rate_for(score)), 'score':score, 'payment_to_cashflow':round(load, 3)}

def find_offers(row):
    """Alternative terms the bank can offer instead of the requested ones."""
    offers = []
    # largest 100k-increment amount for the same term; avoid arbitrary approvals
    affordable_payment = max(0, OFFER_LOAD * row['available_cashflow'] - row['existing_payments'])
    r = RATE/12
    limit = int(affordable_payment * (1-(1+r)**(-row['months']))/r//100_000)*100_000
    if 100_000 <= limit < row['amount'] and affordable({**row, 'amount':limit}):
        offers.append(priced('amount', {**row, 'amount':limit}))
    # shortest longer term for the requested amount
    months = next((m for m in range(row['months']+1, 61) if affordable({**row, 'months':m})), None)
    if months:
        offers.append(priced('term', {**row, 'months':months}))
    return offers

def how_to_improve(row, offers):
    """What the client could change to get the requested loan approved."""
    if row['overdue']:
        return ['Погасить просрочки: с ними одобрение по демо-правилам невозможно при любых условиях']
    tips = []
    for o in offers:
        if o['id'] == 'amount':
            tips.append(f"Уменьшить сумму до {rub(o['amount'])} на тот же срок")
        else:
            tips.append(f"Увеличить срок до {o['months']} мес. на ту же сумму")
    monthly = payment(row['amount'], row['months'], RATE)
    max_existing = int(OFFER_LOAD * row['available_cashflow'] - monthly)
    if 0 <= max_existing < row['existing_payments'] and affordable({**row, 'existing_payments':max_existing}):
        tips.append(f"Снизить платежи по действующим долгам до {rub(max_existing)}/мес.")
    need_cashflow = math.ceil((monthly + row['existing_payments']) / OFFER_LOAD)
    if need_cashflow > row['available_cashflow'] and affordable({**row, 'available_cashflow':need_cashflow}):
        tips.append(f"Подтвердить свободный денежный поток от {rub(need_cashflow)}/мес.")
    return tips

def score_application(row):
    """Decision and explanation. `assessment` is the risk calculation at the reference rate;
    `offer` (approved) or `offers` (counteroffer) are the prices the client is offered."""
    risk = assessment(row)
    score = risk['score']
    offers = [] if score >= APPROVE_SCORE or row['overdue'] else find_offers(row)
    if score >= APPROVE_SCORE: decision = 'approved'
    elif offers: decision = 'counteroffer'
    else: decision = 'rejected'
    alternative = next((o['amount'] for o in offers if o['id'] == 'amount'), None)
    approved = offer('requested', row['amount'], row['months'], rate_for(score)) if decision == 'approved' else None
    explanation = {'policy_version':POLICY_VERSION, 'reference_rate':RATE, 'rate_tiers':RATE_TIERS,
                   'factors':[x['factor'] for x in risk['contributions']], 'assessment':risk,
                   # requested terms that were not approved are shown at the reference rate the scoring used
                   'requested':approved or offer('requested', row['amount'], row['months'], RATE),
                   'offer':approved, 'offers':offers,
                   'how_to_improve':[] if decision == 'approved' else how_to_improve(row, offers),
                   'disclaimer':'Демонстрационная модель на синтетических данных, не банковское решение.'}
    return score, decision, alternative, explanation
