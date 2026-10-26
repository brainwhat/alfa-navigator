"""Bank statement import: analysis that saves nothing and a synthetic sample to try it on."""
from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.auth import require
from app.schemas import StatementAnalysis, StatementInput
from app.statements import analyze, sample_csv

router = APIRouter()

@router.post('/api/statement/analyze', response_model=StatementAnalysis)
def analyze_statement(payload: StatementInput, role: str = Depends(require('client'))):
    """Average revenue, free cash flow, debt payments and seasonality from a CSV statement.
    The statement is read in memory and not stored; the signed summary goes into the application."""
    return analyze(payload.csv)

@router.get('/api/statement/sample.csv', response_class=Response,
            responses={200: {'content': {'text/csv': {}}, 'description': 'Синтетическая выписка кофейни за 12 месяцев'}})
def sample_statement():
    """A synthetic coffee shop statement: daily card revenue, suppliers, rent, salaries, taxes, a loan and a lease."""
    return Response(sample_csv().encode('utf-8'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="alfa-navigator-sample-statement.csv"'})
