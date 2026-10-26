"""Loan arithmetic: annuity payment, amortization schedule and the seasonal cash flow timeline."""

def payment(amount:int, months:int, rate:float) -> int:
    r = rate / 12
    return round(amount * r / (1 - (1 + r) ** (-months)))

def amortization(amount:int, months:int, rate:float, prepay=None):
    """Annuity schedule; the last payment absorbs rounding so the balance closes at zero.

    prepay=(month, amount, mode) adds an early repayment on top of that month's payment;
    afterwards the payment stays and the term shrinks ('term'), or the payment is recalculated
    for the remaining months ('payment')."""
    r = rate / 12
    monthly = payment(amount, months, rate)
    balance = amount
    rows = []
    m = 0
    while balance > 0:
        m += 1
        interest = round(balance * r)
        principal = balance if m >= months else min(balance, monthly - interest)
        extra = min(prepay[1], balance - principal) if prepay and m == prepay[0] else 0
        balance -= principal + extra
        rows.append({'month':m, 'payment':principal+interest, 'principal':principal, 'interest':interest, 'prepayment':extra, 'balance':balance})
        if extra and prepay[2] == 'payment' and m < months:
            monthly = payment(balance, months - m, rate)
    return rows

def summary(rows):
    interest = sum(x['interest'] for x in rows)
    return {'months':len(rows), 'total_paid':sum(x['payment'] + x['prepayment'] for x in rows), 'overpayment':interest}

WINTER = (1, 2, 11, 12)

def seasonal_timeline(rows, cashflow, existing_payments, seasonality, start):
    """Cash flow left after debt for twelve months from `start` (calendar month of the first payment); winter months drop by `seasonality` percent."""
    seasonal_cash = cashflow * (1 - seasonality / 100)
    timeline = []
    for i in range(12):
        calendar = (start - 1 + i) % 12 + 1
        # Illustrative stress scenario in winter months, not a cash flow prediction.
        winter = calendar in WINTER
        flow = round(seasonal_cash if winter else cashflow)
        loan = rows[i]['payment'] if i < len(rows) else 0
        timeline.append({'month':i+1, 'calendar_month':calendar, 'winter':winter, 'cashflow':flow, 'loan_payment':loan,
                         'existing_payments':existing_payments, 'balance_after_debt':flow-loan-existing_payments})
    return timeline
