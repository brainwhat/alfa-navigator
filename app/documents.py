"""Decision document: the application, the decision with its explanation, the terms, the payment
schedule and the event history as a PDF. Knows nothing about HTTP; the handler passes the card."""
from datetime import datetime, timedelta, timezone

from fontTools.ttLib import TTFont
from fpdf import FPDF, FontFace
from fpdf.enums import XPos, YPos

from app import settings
from app.policy import APPROVE_SCORE, POLICY_VERSION
from app.schedules import amortization, summary
from app.workflow import offered_terms

FONTS = {'': settings.ROOT / 'fonts' / 'Onest-Regular.ttf', 'B': settings.ROOT / 'fonts' / 'Onest-SemiBold.ttf'}
# The web subsets of Onest cover Russian and Latin text; anything else (emoji, other scripts) becomes '?'
GLYPHS = set(TTFont(FONTS['']).getBestCmap())
MSK = timezone(timedelta(hours=3), 'МСК')
ACCENT, INK, MUTED, LINE, SOFT = (239, 49, 36), (33, 33, 36), (110, 112, 122), (225, 227, 232), (246, 247, 249)

STATUS = {'submitted': 'На рассмотрении', 'needs_info': 'Нужны сведения', 'clarified': 'Уточнена', 'approved': 'Одобрена',
          'counteroffer': 'Другие условия', 'rejected': 'Отказ', 'accepted': 'Условия приняты', 'withdrawn': 'Отозвана',
          'expired': 'Предложение истекло'}
AUTO = {'approved': 'одобрение', 'counteroffer': 'другие условия', 'rejected': 'отказ'}
OFFER = {'amount': 'Меньше сумма', 'term': 'Дольше срок', 'requested': 'Запрошенные условия'}
ACTOR = {'bank': 'Банк', 'client': 'Клиент', 'system': 'Система'}

def text(s) -> str:
    # the web subsets have no ruble sign, documents write "руб." anyway; a sentence may end right after it
    s = str(s if s is not None else '').replace('\u00a0₽', '\u00a0руб.').replace(' ₽', '\u00a0руб.').replace('₽', 'руб.').replace('руб..', 'руб.')
    return ''.join(ch if ord(ch) in GLYPHS or ch == '\n' else '?' for ch in s)

def money(n) -> str:
    return '—' if n is None else f'{n:,}'.replace(',', ' ') + ' руб.'

def percent(x, digits=0) -> str:
    return '—' if x is None else f'{x * 100:.{digits}f}'.replace('.', ',') + '%'

def when(iso) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime('%d.%m.%Y %H:%M') if iso else '—'

class Document(FPDF):
    def __init__(self, x, generated):
        super().__init__(format='A4')
        self.card = x  # not self.x: FPDF keeps its cursor there
        self.generated = generated
        for style, path in FONTS.items():
            self.add_font('Onest', style, str(path))
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=18)
        self.set_title(f"Решение по заявке № {x['id']}")
        self.set_author('Альфа.Навигатор (демо)')
        self.set_creator('Альфа.Навигатор')

    def header(self):
        self.set_font('Onest', 'B', 11)
        self.set_text_color(*ACCENT)
        self.cell(0, 6, 'Альфа.Навигатор', new_x=XPos.LMARGIN)
        self.set_font('Onest', '', 8)
        self.set_text_color(*ACCENT)
        self.cell(0, 6, 'ДЕМО · синтетические данные · не оферта и не кредитное решение', align='R',
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(5)

    def footer(self):
        self.set_y(-12)
        self.set_font('Onest', '', 7.5)
        self.set_text_color(*MUTED)
        x = self.card
        policy = (x.get('explanation') or {}).get('policy_version') or POLICY_VERSION
        self.cell(0, 5, text(f"Заявка № {x['id']} · правила {policy} · версия заявки {x['version']} · сформировано {self.generated}"))
        self.cell(0, 5, f'стр. {self.page_no()} из {{nb}}', align='R')

    def section(self, title):
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(4)
        self.set_font('Onest', 'B', 12)
        self.set_text_color(*INK)
        self.cell(0, 7, text(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def para(self, s, size=9.5, color=INK, bold=False):
        self.set_font('Onest', 'B' if bold else '', size)
        self.set_text_color(*color)
        self.multi_cell(0, size * 0.5, text(s), align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def grid(self, rows, widths, align=None, head=True, total=False):
        """A table; the first row is the heading, numbers are right-aligned."""
        self.set_font('Onest', '', 8.5)
        self.set_text_color(*INK)
        self.set_draw_color(*LINE)
        style = FontFace(emphasis='BOLD', color=MUTED, fill_color=SOFT)
        with self.table(col_widths=widths, text_align=align or 'LEFT', line_height=5.2, padding=(1.2, 2),
                        borders_layout='HORIZONTAL_LINES', first_row_as_headings=head, headings_style=style,
                        width=self.epw) as table:
            for i, values in enumerate(rows):
                bold = total and i == len(rows) - 1
                row = table.row(style=FontFace(emphasis='BOLD') if bold else None)
                for v in values:
                    row.cell(text(v))
        self.ln(1)

    def pairs(self, items):
        """Label and value, two pairs per line."""
        rows = [[*items[i], *(items[i + 1] if i + 1 < len(items) else ('', ''))] for i in range(0, len(items), 2)]
        self.grid(rows, (36, 51, 36, 51), head=False)

def terms_row(name, t):
    return [name, money(t.get('amount')), f"{t.get('months')} мес.", percent(t.get('annual_rate')),
            money(t.get('monthly_payment')), money(t.get('overpayment'))]

def outcome(x, e):
    """What the decision is right now, in one paragraph."""
    s, override = x['status'], e.get('manual_override')
    auto = AUTO.get(x['decision'], x['decision'] or '—')
    lines = {
        'accepted': 'Клиент принял условия банка. Подписание и выдача в демо смоделированы.',
        'approved': f"Заявка одобрена. Клиент может принять условия до {when(x.get('offer_expires_at'))}.",
        'counteroffer': f"Запрошенные условия не проходят по правилам; банк предлагает другие. Клиент выбирает вариант до {when(x.get('offer_expires_at'))}.",
        'rejected': 'Отказ: по демо-правилам подходящих условий нет.',
        'expired': f"Срок действия предложения истёк {when(x.get('offer_expires_at'))}. Банк может его продлить.",
        'withdrawn': 'Клиент отозвал заявку.',
        'needs_info': 'Банк запросил уточнение сведений; ниже — результат последней оценки.',
        'clarified': 'Клиент уточнил сведения, заявка ждёт повторной оценки; ниже — результат последней оценки.',
        'submitted': 'Заявка ждёт оценки банка.',
    }
    result = f'Автоскоринг: {auto}, {x["score"]} из 100. ' + lines.get(s, '')
    if override:
        verdict = 'одобрил' if override.get('decision') == 'approved' else 'отклонил'
        result += f" Сотрудник банка вручную {verdict} заявку {when(override.get('at'))}: {override.get('reason')}"
    return result

def described(ev):
    """The event as the log has it; a scoring event spells its decision code out."""
    data = ev.get('data') or {}
    if ev['action'] == 'evaluated' and data.get('decision'):
        return f"Скоринг: {data.get('score')} баллов, {AUTO.get(data['decision'], data['decision'])}"
    return ev['description']

def schedule_terms(x, e):
    """The terms the payment schedule is shown for: agreed, approved, or the first offered variant."""
    if e.get('accepted_terms'):
        return 'согласованные условия', e['accepted_terms']
    if x['status'] in ('approved', 'expired') and e.get('offer'):
        return 'одобренные условия', offered_terms(e)
    if x['status'] in ('counteroffer', 'expired') and e.get('offers'):
        o = e['offers'][0]
        return f"вариант «{OFFER.get(o['id'], o['id'])}»", o
    return None, None

def decision_pdf(x, at=None) -> bytes:
    """The decision document for an enriched application card that has been scored."""
    e = x.get('explanation') or {}
    pdf = Document(x, (at or datetime.now(timezone.utc)).astimezone(MSK).strftime('%d.%m.%Y %H:%M МСК'))
    pdf.add_page()

    pdf.set_font('Onest', 'B', 18)
    pdf.set_text_color(*INK)
    pdf.cell(0, 9, text(f"Решение по заявке № {x['id']}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.para(f"{x['company']} · {x['purpose']}", size=10.5, color=MUTED)
    pdf.para('Сформировано демонстрационным прототипом на синтетических данных. Ставки и правила вымышлены и не соответствуют '
             'ставкам или кредитной политике Альфа-Банка. Не является офертой или кредитным решением.', size=8, color=MUTED)
    pdf.ln(2)
    pdf.pairs([('Статус', STATUS.get(x['status'], x['status'])), ('Подана', when(x['created_at'])),
               ('Правила', e.get('policy_version') or POLICY_VERSION), ('Изменена', when(x['updated_at']))])
    pdf.ln(1)
    pdf.para(outcome(x, e))

    widths = (40, 30, 18, 16, 28, 28)
    heading = ['Условия', 'Сумма', 'Срок', 'Ставка', 'Платёж в месяц', 'Переплата']
    if e.get('accepted_terms'):
        t = e['accepted_terms']
        pdf.section('Согласованные условия')
        pdf.grid([heading, terms_row('Приняты клиентом', t)], widths)
        if (t.get('amount'), t.get('months')) != (x['amount'], x['months']):
            pdf.para(f"Клиент принял предложение банка вместо запрошенных {money(x['amount'])} на {x['months']} мес. "
                     'Оценка риска ниже относится к первоначальному запросу.', size=8.5, color=MUTED)
        if t.get('offer_id'):
            pdf.para(f"Предложение № {t['offer_id']} от {when(t.get('issued_at'))}.", size=8.5, color=MUTED)
    elif x['status'] in ('approved', 'expired') and e.get('offer'):
        t = offered_terms(e)
        pdf.section('Одобренные условия')
        pdf.grid([heading, terms_row('Одобрено', t)], widths)
        pdf.para(f"Предложение № {t.get('offer_id', '—')} от {when(t.get('issued_at'))}, действует до {when(x.get('offer_expires_at'))}. "
                 'Клиент принимает ровно эти условия, даже если тарифы позже изменятся.', size=8.5, color=MUTED)
    elif e.get('offers') and x['status'] in ('counteroffer', 'expired'):
        pdf.section('Предложенные условия')
        assessment = e.get('assessment') or {}
        rows = [['Вариант', 'Сумма', 'Срок', 'Ставка', 'Платёж', 'Переплата', 'Балл', 'Нагрузка']]
        if e.get('requested'):
            rows.append([*terms_row('Запрошено', e['requested']), str(assessment.get('score', '—')),
                         percent(assessment.get('payment_to_cashflow'), 1)])
        rows += [[*terms_row(OFFER.get(o['id'], o['id']), o), str(o.get('score', '—')), percent(o.get('payment_to_cashflow'), 1)]
                 for o in e['offers']]
        pdf.grid(rows, (30, 25, 15, 15, 24, 25, 11, 17), align=('LEFT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT'))
        pdf.para('Запрошенные условия не одобрены. Балл и нагрузка — по базовой ставке; ставка варианта зависит от его балла.', size=8.5, color=MUTED)

    pdf.section('Запрос клиента')
    pdf.pairs([('Сумма', money(x['amount'])), ('Срок', f"{x['months']} мес."),
               ('Выручка в месяц', money(x['revenue'])), ('Свободный поток', money(x['available_cashflow'])),
               ('Платежи по долгам', money(x['existing_payments'])), ('Просрочки', 'есть' if x['overdue'] else 'нет')])

    a = e.get('assessment')
    if a:
        pdf.section('Как сложилась оценка')
        rows = [['Фактор', 'Пояснение', 'Баллы'], ['Исходный балл', '', str(a['base_score'])]]
        rows += [[c['factor'], c.get('detail') or '', f"−{abs(c['points'])}" if c['points'] else '0'] for c in a['contributions']]
        rows.append(['Итог', f'порог автоодобрения — {APPROVE_SCORE} баллов', str(a['score'])])
        pdf.grid(rows, (82, 72, 20), align=('LEFT', 'LEFT', 'RIGHT'), total=True)
        pdf.para(f"Платёж по запрошенным условиям по базовой ставке {percent(a['reference_rate'])}: {money(a['monthly_payment'])} в месяц. "
                 f"Вместе с действующими платежами {money(x['existing_payments'])} это {percent(a['payment_to_cashflow'], 1)} "
                 f"свободного потока {money(x['available_cashflow'])}.", size=8.5, color=MUTED)
    tips = e.get('how_to_improve') or []
    if tips and x['status'] in ('counteroffer', 'rejected', 'expired'):
        pdf.section('Как получить одобрение запрошенных условий')
        for tip in tips:
            pdf.para('•  ' + tip)

    title, t = schedule_terms(x, e)
    if t:
        rows = amortization(t['amount'], t['months'], t['annual_rate'])
        total = summary(rows)
        pdf.section(f'График платежей: {title}')
        pdf.para(f"{money(t['amount'])} на {t['months']} мес. под {percent(t['annual_rate'])}: всего выплат {money(total['total_paid'])}, "
                 f"из них проценты {money(total['overpayment'])}.", size=8.5, color=MUTED)
        pdf.ln(1)
        pdf.grid([['Месяц', 'Платёж', 'Основной долг', 'Проценты', 'Остаток']]
                 + [[str(r['month']), money(r['payment']), money(r['principal']), money(r['interest']), money(r['balance'])] for r in rows],
                 (20, 38, 40, 36, 40), align=('LEFT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT'))

    pdf.section('История событий')
    pdf.grid([['Когда', 'Кто', 'Событие']] + [[when(ev['occurred_at']), ACTOR.get(ev['actor'], ev['actor']), described(ev)]
                                             for ev in x.get('events', [])], (30, 18, 126))
    return bytes(pdf.output())
