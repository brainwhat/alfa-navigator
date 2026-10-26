"""Statement import: formats, classification, averages, seasonality, the signed summary and the reconciliation."""
import copy
from app.documents import decision_pdf
from app.statements import analyze, parse_amount, parse_date
from conftest import client, CLIENT, BANK, payload, evaluate

def sample():
    return client.get('/api/statement/sample.csv').text

def summary_of(csv):
    r=client.post('/api/statement/analyze',json={'csv':csv},headers=CLIENT)
    assert r.status_code==200,r.text
    return r.json()

def test_sample_statement_of_the_coffee_shop():
    r=client.get('/api/statement/sample.csv')
    assert r.status_code==200 and r.headers['content-type'].startswith('text/csv') and 'СИНТЕТИЧЕСКИЕ' in r.text
    a=summary_of(r.text)
    s=a['summary']
    assert s['period']['months']==12 and s['operations']>400
    assert 1_550_000<=s['revenue']<=1_650_000 and s['existing_payments']==30_000
    assert 250_000<=s['available_cashflow']<=350_000
    se=s['seasonality']
    assert se['months_covered']==12 and se['worst_month'] in (1,2) and se['drop_pct']>40
    assert {c['id'] for c in a['categories']}=={'revenue','other_in','expenses','debt','other_out'}
    assert a['warnings']==[]

def test_analysis_needs_the_client_and_a_table():
    assert client.post('/api/statement/analyze',json={'csv':'x'*20}).status_code==401
    assert client.post('/api/statement/analyze',json={'csv':sample()},headers=BANK).status_code==403
    r=client.post('/api/statement/analyze',json={'csv':'Имя;Город\nИван;Москва\n'},headers=CLIENT)
    assert r.status_code==422 and 'столбцы с датой' in r.json()['detail']
    one_month='Дата;Сумма;Назначение\n05.03.2026;100000;Оплата\n10.03.2026;-40000;Аренда\n'
    r=client.post('/api/statement/analyze',json={'csv':one_month},headers=CLIENT)
    assert r.status_code==422 and '2 месяца' in r.json()['detail']

def test_signed_amounts_iso_dates_and_commas():
    rows=[f'2026-{m:02d}-{d:02d},{a},{t}' for m in (1,2,3) for d,a,t in ((5,100000,'Оплата от покупателя'),(10,-40000,'Аренда'),(15,-5000,'Погашение кредита'))]
    s=analyze('date,amount,description\n'+'\n'.join(rows)+'\n,,итого\n')['summary']
    assert (s['revenue'],s['expenses'],s['available_cashflow'],s['existing_payments'])==(100000,40000,60000,5000)
    assert s['seasonality']['profile'][:3]==[1.0,1.0,1.0] and s['seasonality']['profile'][3] is None

def test_debit_credit_columns_and_what_is_not_revenue():
    lines=['Выписка по счёту','','Дата;Контрагент;Назначение платежа;Дебет;Кредит']
    for m in (1,2):
        lines+=[f'10.{m:02d}.2026;Покупатель;Оплата по счёту;;"200 000,00"',
                f'11.{m:02d}.2026;Учредитель;Взнос собственных средств;;"50 000,00"',
                f'12.{m:02d}.2026;Банк;Зачисление кредита по договору;;"90 000,00"',
                f'13.{m:02d}.2026;Поставщик;Оплата товара;"120 000,00";',
                f'14.{m:02d}.2026;Лизинг;Лизинговый платёж;"10 000,00";',
                f'15.{m:02d}.2026;Я;Перевод между своими счетами;"30 000,00";',
                'мусорная строка;;;;']
    a=analyze('\n'.join(lines))
    s=a['summary']
    assert (s['revenue'],s['expenses'],s['existing_payments'],s['available_cashflow'])==(200000,120000,10000,80000)
    assert any('Пропущено строк' in w for w in a['warnings'])

def test_partial_edge_months_are_left_out_of_averages():
    rows=[f'{d:02d}.{m:02d}.2026;100000;Оплата' for m in range(1,7) for d in (2,27)]
    rows[0]='20.01.2026;100000;Оплата'  # January starts late
    a=analyze('Дата;Сумма;Назначение\n'+'\n'.join(rows[1:]+[rows[0]]))
    assert a['summary']['period']['months']==5 and any('Первый месяц' in w for w in a['warnings'])

def test_amounts_and_dates():
    assert [parse_amount(v) for v in ('1 234,56','-1234.56','(1 234,56)','1.234,56','1,234.56','12 345 ₽','','abc')]==[1234.56,-1234.56,-1234.56,1234.56,1234.56,12345.0,None,None]
    assert [str(parse_date(v)) for v in ('2026-03-05','05.03.26 12:00','31.02.2026','вчера')]==['2026-03-05','2026-03-05','None','None']

def test_application_carries_the_signed_summary():
    s=summary_of(sample())['summary']
    body=payload(revenue=s['revenue'],available_cashflow=s['available_cashflow'],existing_payments=s['existing_payments'],statement=s)
    r=client.post('/api/applications',json=body,headers=CLIENT)
    assert r.status_code==201,r.text
    x=r.json()
    assert all(c['confirmed'] for c in x['statement']['check'])
    assert 'с выпиской за 12 мес.' in x['events'][0]['description'] and 'signature' not in x['statement']
    card=client.get('/api/applications/'+x['id'],headers=BANK).json()
    assert card['statement']['seasonality']==s['seasonality']

def test_changed_figures_show_up_and_a_forged_summary_is_refused():
    s=summary_of(sample())['summary']
    body=payload(revenue=s['revenue'],available_cashflow=s['available_cashflow']+100_000,existing_payments=0,statement=s)
    check={c['field']:c for c in client.post('/api/applications',json=body,headers=CLIENT).json()['statement']['check']}
    assert check['revenue']['confirmed'] and not check['available_cashflow']['confirmed'] and check['available_cashflow']['diff']>0.3
    assert not check['existing_payments']['confirmed'] and check['existing_payments']['diff']==-1
    forged=copy.deepcopy(s)
    forged['available_cashflow']+=100_000
    r=client.post('/api/applications',json=body|{'statement':forged},headers=CLIENT)
    assert r.status_code==422 and 'изменена' in r.json()['detail']
    assert client.post('/api/applications',json=body|{'statement':s|{'signature':'0'*64}},headers=CLIENT).status_code==422

def test_application_without_statement_and_its_pdf():
    x=client.post('/api/applications',json=payload(),headers=CLIENT).json()
    assert x['statement'] is None
    s=summary_of(sample())['summary']
    y=client.post('/api/applications',json=payload(revenue=s['revenue'],available_cashflow=s['available_cashflow'],existing_payments=s['existing_payments'],statement=s),headers=CLIENT).json()
    for row in (x,y):
        evaluate(row['id'])
        r=client.get(f"/api/applications/{row['id']}/decision.pdf",headers=BANK)
        assert r.status_code==200 and r.content.startswith(b'%PDF-')
    assert decision_pdf(client.get('/api/applications/'+y['id'],headers=BANK).json()).startswith(b'%PDF-')

def test_simulator_follows_the_statement_seasons():
    profile=[0.5,0.6]+[1.1]*10
    r=client.post('/api/simulate',json=dict(amount=1_000_000,months=24,available_cashflow=200_000,existing_payments=0,seasonality=30,profile=profile,start_month=1),headers=CLIENT).json()
    jan,feb,mar=r['timeline'][:3]
    assert (jan['cashflow'],feb['cashflow'],mar['cashflow'])==(100_000,120_000,220_000)
    assert jan['winter'] and not mar['winter']
    assert r['stress_load']==round((r['monthly_payment'])/(200_000*0.5),4)
    bad=client.post('/api/simulate',json=dict(amount=1_000_000,months=24,available_cashflow=200_000,existing_payments=0,seasonality=30,profile=[1]*5),headers=CLIENT)
    assert bad.status_code==422
