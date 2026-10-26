"""The decision PDF: who may download it, for which applications, and what the text turns into."""
from app.documents import decision_pdf, described, text
from conftest import client, CLIENT, BANK, create, evaluate

def test_pdf_needs_login_and_a_decision():
    row=create()
    url=f"/api/applications/{row['id']}/decision.pdf"
    assert client.get(url).status_code==401
    r=client.get(url,headers=CLIENT)
    assert r.status_code==409 and 'не оценил' in r.json()['detail']
    assert client.get('/api/applications/NOPE/decision.pdf',headers=BANK).status_code==404

def test_pdf_of_a_scored_application():
    row=create(amount=5_000_000,months=24,revenue=1_600_000,available_cashflow=240_000,existing_payments=30_000)
    evaluate(row['id'])
    for headers in (CLIENT,BANK):
        r=client.get(f"/api/applications/{row['id']}/decision.pdf",headers=headers)
        assert r.status_code==200 and r.headers['content-type']=='application/pdf'
        assert r.headers['content-disposition']==f'attachment; filename="alfa-navigator-{row["id"]}.pdf"'
        assert r.content.startswith(b'%PDF-') and r.content.rstrip().endswith(b'%%EOF')

def test_pdf_for_every_demo_status():
    assert client.post('/api/demo/seed',headers=BANK).status_code==200
    rows=client.get('/api/applications',headers=BANK).json()
    statuses=set()
    for x in rows:
        card=client.get('/api/applications/'+x['id'],headers=BANK).json()
        if card.get('score') is None:
            continue
        statuses.add(card['status'])
        pdf=decision_pdf(card)
        assert pdf.startswith(b'%PDF-') and pdf.count(b'/Type /Page\n')>=1
    assert {'approved','counteroffer','rejected','accepted','withdrawn','expired'}<=statuses
    client.delete('/api/demo/seed',headers=BANK)

def test_text_fits_the_font():
    assert text('Уменьшить сумму до 2 100 000 ₽ на тот же срок')=='Уменьшить сумму до 2 100 000 руб. на тот же срок'
    assert text('Поток 240 000 ₽.')=='Поток 240 000 руб.'
    assert text('Кофе ☕ № 1 «Ёлка»')=='Кофе ? № 1 «Ёлка»'
    assert text(None)==''

def test_scoring_event_is_spelled_out():
    ev={'action':'evaluated','description':'Скоринг 45; результат: counteroffer; правила demo-rules-v1','data':{'decision':'counteroffer','score':45}}
    assert described(ev)=='Скоринг: 45 баллов, другие условия'
    assert described({'action':'withdrawn','description':'Клиент отозвал заявку','data':None})=='Клиент отозвал заявку'
