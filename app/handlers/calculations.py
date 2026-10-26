"""Calculations that save nothing: pre-check by the scoring rules, payment schedule, seasonal simulator."""
from fastapi import APIRouter, Depends, Header, HTTPException

from app.auth import require, validate_any
from app.policy import OFFER_LOAD, RATE, score_application
from app.schedules import amortization, payment, seasonal_timeline, summary
from app.schemas import LoanTerms, Precheck, ScheduleInput, ScheduleOut, SimulationInput, SimulationOut
from app.storage import utcnow

router = APIRouter()

@router.post('/api/precheck', response_model=Precheck, response_model_exclude_unset=True)
def precheck(payload: LoanTerms, role: str = Depends(require('client'))):
    """Same rules as the bank's scoring, without saving anything."""
    score, decision, _, explanation = score_application(payload.model_dump())
    return {'score':score, 'decision':decision, **explanation,
            'disclaimer':'Предварительная оценка по демо-правилам, итоговое решение принимает банк.'}

@router.post('/api/schedule', response_model=ScheduleOut)
def schedule(payload: ScheduleInput, authorization: str | None = Header(None)):
    validate_any(authorization)
    monthly = payment(payload.amount, payload.months, payload.annual_rate)
    result = {'annual_rate':payload.annual_rate, 'monthly_payment':monthly}
    if payload.prepay_month and payload.prepay_amount:
        if payload.prepay_month >= payload.months:
            raise HTTPException(422, 'Досрочное погашение возможно до последнего месяца')
        rows = amortization(payload.amount, payload.months, payload.annual_rate, (payload.prepay_month, payload.prepay_amount, payload.prepay_mode))
        baseline = summary(amortization(payload.amount, payload.months, payload.annual_rate))
        after = rows[payload.prepay_month]['payment'] if len(rows) > payload.prepay_month else 0
        result |= {'baseline':baseline, 'savings':baseline['overpayment'] - summary(rows)['overpayment'], 'monthly_after':after}
    else:
        rows = amortization(payload.amount, payload.months, payload.annual_rate)
    return result | summary(rows) | {'rows':rows}

@router.post('/api/simulate', response_model=SimulationOut)
def simulate(payload: SimulationInput, authorization: str | None = Header(None)):
    """Twelve months from the first payment; the loan payment comes from the amortization schedule, so it stops when the loan is repaid."""
    validate_any(authorization)
    rows = amortization(payload.amount, payload.months, RATE)
    monthly = rows[0]['payment']
    base = (monthly+payload.existing_payments)/payload.available_cashflow
    worst = min([f for f in payload.profile or [] if f is not None], default=None)
    low = worst if worst is not None else 1 - payload.seasonality/100
    stress = (monthly+payload.existing_payments)/(payload.available_cashflow*low) if low > 0 else 99.0
    start = payload.start_month or utcnow().month % 12 + 1
    timeline = seasonal_timeline(rows, payload.available_cashflow, payload.existing_payments, payload.seasonality, start, payload.profile)
    return {'annual_rate':RATE, 'monthly_payment':monthly, 'base_load':round(base,4), 'stress_load':round(stress,4), 'start_month':start,
            'timeline':timeline, 'safe_under_stress':stress<=OFFER_LOAD, 'disclaimer':'Условная модель, а не кредитное решение или финансовый прогноз.'}
