"""Bank statement import: parse a CSV export, classify operations and compute monthly revenue,
free cash flow, debt payments and the seasonal profile. The statement is analysed in memory and
never stored: only the aggregated summary leaves this module, signed so that the bank can trust
that the client did not edit it. Synthetic data only, see docs/limitations.md."""
import calendar
import csv
import hashlib
import hmac
import io
import json
import random
import re
from collections import defaultdict
from datetime import date, timedelta

from fastapi import HTTPException

from app import settings
from app.storage import utcnow

MAX_ROWS = 20_000
# Header keywords per column; credit/debit go first: "Сумма (приход)" is an incoming amount, not a signed one.
# In a Russian statement "Дебет" is money leaving the client's account and "Кредит" money arriving.
COLUMNS = [('credit', ('приход', 'поступлен', 'зачислен', 'кредит', 'credit')),
           ('debit', ('расход', 'списан', 'дебет', 'debit')),
           ('date', ('дата', 'date')),
           ('amount', ('сумма', 'amount')),
           ('purpose', ('назначение', 'описание', 'комментарий', 'purpose', 'description')),
           ('party', ('контрагент', 'получатель', 'плательщик', 'counterparty'))]
# Incoming money that is not revenue and outgoing money that is not an operating expense
NOT_REVENUE = ('собственн', 'между своими', 'между счетами', 'кредит', 'займ', 'заём', 'возврат')
NOT_EXPENSE = ('собственн', 'между своими', 'между счетами', 'дивиденд')
DEBT = ('кредит', 'займ', 'заём', 'лизинг')
CATEGORIES = {'revenue': 'Выручка', 'other_in': 'Поступления не из выручки: свои средства, кредиты, возвраты',
              'expenses': 'Операционные расходы и налоги', 'debt': 'Платежи по кредитам и лизингу',
              'other_out': 'Переводы себе и выплаты владельцу'}
SIGNED = ('period', 'operations', 'revenue', 'expenses', 'available_cashflow', 'existing_payments', 'monthly', 'seasonality')
MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']

def bad(message):
    return HTTPException(422, message)

def parse_date(s):
    s = s.strip()
    if m := re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', s):
        d, mo, y = map(int, m.groups())
        y += 2000 if y < 100 else 0
    elif m := re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', s):
        y, mo, d = map(int, m.groups())
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None

def parse_amount(s):
    """'1 234,56', '-1234.56', '(1 234,56)', '1.234,56' and '1,234.56' all read as rubles."""
    s = re.sub(r'[\s  ₽]|руб\.?|RUB', '', s.strip(), flags=re.I)
    if not s:
        return None
    negative = s.startswith('(') and s.endswith(')') or s.startswith('-')
    s = s.strip('()+-')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    else:
        s = s.replace(',', '.')
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value

def columns_of(header):
    roles = {}
    for i, cell in enumerate(header):
        name = cell.strip().lower()
        for role, words in COLUMNS:
            if role not in roles and any(w in name for w in words):
                roles[role] = i
                break
    return roles if 'date' in roles and ({'amount'} & roles.keys() or {'credit', 'debit'} <= roles.keys()) else None

def operations(text):
    """Rows of the statement as (date, signed amount, description); skipped rows are counted."""
    sample = text[:5000]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=';,\t').delimiter
    except csv.Error:
        delimiter = ';'
    rows = csv.reader(io.StringIO(text), delimiter=delimiter)
    roles = None
    for _ in range(30):  # bank exports put the account and the period above the table
        header = next(rows, None)
        if header is None:
            break
        if roles := columns_of(header):
            break
    if not roles:
        raise bad('Не нашли таблицу операций: нужны столбцы с датой и суммой (или приходом и расходом)')
    result, skipped = [], 0
    for row in rows:
        if len(result) + skipped >= MAX_ROWS:
            raise bad(f'Слишком длинная выписка: не больше {MAX_ROWS:,} операций'.replace(',', ' '))
        cell = lambda role: row[roles[role]] if role in roles and roles[role] < len(row) else ''
        day = parse_date(cell('date'))
        if {'credit', 'debit'} <= roles.keys():
            incoming, outgoing = parse_amount(cell('credit')) or 0, parse_amount(cell('debit')) or 0
            amount = abs(incoming) - abs(outgoing) if incoming or outgoing else None
        else:
            amount = parse_amount(cell('amount'))
        if day is None or not amount:
            skipped += any(c.strip() for c in row)  # empty and summary lines are not errors
            continue
        result.append((day, round(amount), f"{cell('party')} {cell('purpose')}".strip()))
    if not result:
        raise bad('В выписке не нашлось ни одной операции с датой и суммой')
    return result, skipped

def category(amount, description):
    text = description.lower()
    if amount > 0:
        return 'other_in' if any(w in text for w in NOT_REVENUE) else 'revenue'
    if any(w in text for w in NOT_EXPENSE):
        return 'other_out'
    return 'debt' if any(w in text for w in DEBT) else 'expenses'

def analyze(text):
    """Monthly averages and the seasonal profile; returns the signed summary plus what the client sees only."""
    ops, skipped = operations(text)
    first, last = min(o[0] for o in ops), max(o[0] for o in ops)
    months = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(lambda: {'total': 0, 'count': 0, 'examples': defaultdict(int)})
    for day, amount, description in ops:
        kind = category(amount, description)
        months[(day.year, day.month)][kind] += abs(amount)
        t = totals[kind]
        t['total'] += abs(amount)
        t['count'] += 1
        t['examples'][re.sub(r'\d[\d./-]*', '…', description)[:80] or '—'] += abs(amount)
    keys = sorted(months)
    warnings = []
    # partial first and last months would understate the averages
    if len(keys) >= 4 and first.day > 7:
        keys.pop(0)
        warnings.append(f'Первый месяц ({MONTHS[first.month - 1]} {first.year}) неполный и не учтён в средних')
    if len(keys) >= 4 and last.day < calendar.monthrange(last.year, last.month)[1] - 7:
        keys.pop()
        warnings.append(f'Последний месяц ({MONTHS[last.month - 1]} {last.year}) неполный и не учтён в средних')
    if len(keys) < 2:
        raise bad('Нужна выписка хотя бы за 2 месяца')
    monthly = [{'month': f'{y}-{m:02d}', 'revenue': months[(y, m)]['revenue'], 'expenses': months[(y, m)]['expenses'],
                'debt': months[(y, m)]['debt'], 'cashflow': months[(y, m)]['revenue'] - months[(y, m)]['expenses']} for y, m in keys]
    avg = lambda field: round(sum(r[field] for r in monthly) / len(monthly))
    cashflow = avg('cashflow')
    by_calendar = defaultdict(list)
    for r in monthly:
        by_calendar[int(r['month'][5:])].append(r['cashflow'])
    profile = [round(sum(v) / len(v) / cashflow, 3) if v and cashflow > 0 else None for v in (by_calendar.get(m) for m in range(1, 13))]
    known = [(f, m) for m, f in enumerate(profile, 1) if f is not None]
    worst = min(known) if known else None
    if len(by_calendar) < 12:
        warnings.append(f'Сезонность оценена по {len(by_calendar)} месяцам из 12: для полной картины нужна выписка за год')
    if cashflow <= 0:
        warnings.append('Расходы по выписке не меньше выручки: свободного потока нет, заявку с такими данными подать нельзя')
    summary = {'period': {'from': first.isoformat(), 'to': last.isoformat(), 'months': len(monthly)},
               'operations': len(ops), 'revenue': avg('revenue'), 'expenses': avg('expenses'),
               'available_cashflow': cashflow, 'existing_payments': avg('debt'), 'monthly': monthly,
               'seasonality': {'profile': profile, 'worst_month': worst and worst[1],
                               'drop_pct': max(0, round((1 - worst[0]) * 100)) if worst else 0,
                               'months_covered': len(by_calendar)}}
    summary['signature'] = sign(summary)
    categories = [{'id': k, 'name': name, 'total': totals[k]['total'], 'count': totals[k]['count'],
                   'examples': [d for d, _ in sorted(totals[k]['examples'].items(), key=lambda x: -x[1])[:3]]}
                  for k, name in CATEGORIES.items() if totals[k]['count']]
    if skipped:
        warnings.append(f'Пропущено строк без даты или суммы: {skipped}')
    return {'summary': summary, 'categories': categories, 'warnings': warnings}

def sign(summary):
    body = json.dumps({k: summary[k] for k in SIGNED}, sort_keys=True, ensure_ascii=False)
    return hmac.new((settings.DEMO_BANK_KEY + ':statement').encode(), body.encode(), hashlib.sha256).hexdigest()

def verified(summary: dict) -> dict:
    """The summary as the server computed it, or 422 when the client changed anything in it."""
    if not hmac.compare_digest(summary.get('signature', ''), sign(summary)):
        raise bad('Сводка выписки изменена после расчёта: загрузите выписку заново')
    return {k: summary[k] for k in SIGNED}

def check(row, statement):
    """Declared figures against the statement: equal within 1% counts as confirmed."""
    result = []
    for field in ('revenue', 'available_cashflow', 'existing_payments'):
        declared, actual = row[field], statement[field]
        diff = (declared - actual) / actual if actual else (0 if declared == actual else 1)
        result.append({'field': field, 'declared': declared, 'statement': actual, 'confirmed': abs(diff) <= 0.01, 'diff': round(diff, 3)})
    return result

# A synthetic coffee shop for the demo: summer peak, winter dip, fixed costs, one loan and one lease
SEASON = [0.75, 0.78, 0.9, 1.0, 1.1, 1.2, 1.25, 1.2, 1.05, 0.95, 0.85, 1.0]

def sample_csv():
    """Twelve full months up to the last complete one, deterministic for a given month."""
    today = utcnow().date()
    end = today.replace(day=1) - timedelta(days=1)
    start = date(end.year - 1, end.month, 1) + timedelta(days=calendar.monthrange(end.year - 1, end.month)[1])
    rnd = random.Random(f'{start}')
    rows = []
    add = lambda d, party, purpose, amount: rows.append((d, party, purpose, amount))
    month = start
    while month <= end:
        days = calendar.monthrange(month.year, month.month)[1]
        revenue = 1_600_000 * SEASON[month.month - 1]
        for d in range(1, days + 1):
            day = month.replace(day=d)
            add(day, 'ПАО Эквайринг-Демо', f'Зачисление по эквайрингу за {day:%d.%m.%Y}', revenue / days * rnd.uniform(0.85, 1.15))
        for d in (3, 10, 17, 24):
            add(month.replace(day=d), 'ООО «Зерно и молоко»', 'Оплата поставки кофе и молока по счёту', -revenue * 0.5 / 4 * rnd.uniform(0.9, 1.1))
        add(month.replace(day=1), 'ИП Арендодатель Демо', 'Аренда помещения за месяц', -150_000)
        add(month.replace(day=5), 'АО Энергосбыт-Демо', 'Коммунальные услуги', -30_000 * rnd.uniform(0.9, 1.2))
        add(month.replace(day=10), 'Сотрудники', 'Заработная плата за первую половину месяца', -110_000)
        add(month.replace(day=25), 'Сотрудники', 'Заработная плата за вторую половину месяца', -110_000)
        add(month.replace(day=25), 'УФК (ФНС России)', 'Налог по упрощённой системе, авансовый платёж', -revenue * 0.06)
        add(month.replace(day=15), 'Банк-Демо', 'Погашение кредита по договору КД-118, основной долг и проценты', -22_000)
        add(month.replace(day=20), 'ООО Лизинг-Демо', 'Лизинговый платёж за кофемашину по договору Л-77', -8_000)
        month = month.replace(day=days) + timedelta(days=1)
    middle = start + timedelta(days=150)
    add(middle, 'Иванов И. И. (учредитель)', 'Взнос собственных средств учредителя', 300_000)
    add(middle + timedelta(days=40), 'ООО Кофе и город', 'Перевод собственных средств на счёт в другом банке', -100_000)
    out = io.StringIO()
    w = csv.writer(out, delimiter=';', lineterminator='\r\n')
    w.writerow(['Выписка по счёту 40702810000000000001 — СИНТЕТИЧЕСКИЕ ДАННЫЕ для демо Альфа.Навигатора'])
    w.writerow([f'Клиент: ООО Кофе и город; период {start:%d.%m.%Y}–{end:%d.%m.%Y}'])
    w.writerow([])
    w.writerow(['Дата операции', 'Контрагент', 'Назначение платежа', 'Приход', 'Расход'])
    money = lambda v: f'{v:,.2f}'.replace(',', ' ').replace('.', ',')
    for d, party, purpose, amount in sorted(rows, key=lambda r: r[0]):
        w.writerow([f'{d:%d.%m.%Y}', party, purpose, money(amount) if amount > 0 else '', money(-amount) if amount < 0 else ''])
    return '﻿' + out.getvalue()
