"""The main client -> bank -> client flow and the races the UI must survive, in a real browser."""
import re
from uuid import uuid4

from playwright.sync_api import Page, expect

from conftest import PASSWORD, TOKENS

def login(page: Page, role):
    page.get_by_role('radio', name=re.compile('Предприниматель' if role == 'client' else 'Сотрудник банка')).click()
    page.get_by_label('Демо-пароль').fill(PASSWORD)
    page.get_by_role('button', name=re.compile('Войти')).click()
    expect(page.locator('#workspace')).to_be_visible()

def switch(page: Page, role):
    page.get_by_role('tab', name=re.compile('Кабинет банка' if role == 'bank' else 'Кабинет предпринимателя')).click()

def create(page: Page, company, **terms):
    """An application created through the API, as another session would do it."""
    body = dict(company=company, purpose='Проверка интерфейса', amount=1_000_000, months=36, revenue=1_000_000,
                available_cashflow=250_000, existing_payments=0, overdue=False) | terms
    r = page.request.post('/api/applications', data=body, headers={'Authorization': 'Bearer ' + TOKENS['client']})
    assert r.ok, r.text()
    return r.json()

def applications(page: Page, company):
    r = page.request.get('/api/applications', headers={'Authorization': 'Bearer ' + TOKENS['bank']})
    return [x for x in r.json() if x['company'] == company]

def test_main_flow(page: Page):
    company = f'ООО E2E {uuid4().hex[:6]}'
    page.goto('/')
    login(page, 'client')
    page.locator('.chipbtn', has_text='Другие условия').click()
    page.get_by_label('Компания', exact=True).fill(company)
    expect(page.locator('#precheck')).to_contain_text('Предложат другие условия')
    page.get_by_role('button', name=re.compile('Подать заявку')).click()
    card = page.locator('#clientDetail')
    expect(card).to_contain_text(company)
    expect(card.locator('.pill').first).to_have_text('На рассмотрении')
    expect(card).to_contain_text('Ход банка')

    switch(page, 'bank')
    login(page, 'bank')
    bank = page.locator('#bankDetail')
    expect(bank).to_contain_text(company)  # the selection carries over between roles
    bank.get_by_role('button', name=re.compile('Провести скоринг')).click()
    expect(bank).to_contain_text('Банк предлагает другие условия')
    expect(bank.locator('.breakdown')).to_contain_text('Итог')

    switch(page, 'client')
    card.get_by_role('button', name='Выбрать').first.click()
    agreed = card.locator('.terms.ok')
    expect(agreed).to_contain_text('Согласованные условия')
    expect(agreed).to_contain_text('2 100 000 ₽')
    expect(card.locator('.pill').first).to_have_text('Условия приняты')
    assert [x['status'] for x in applications(page, company)] == ['accepted']

def test_late_response_does_not_replace_card(page: Page):
    a, b = create(page, f'ООО Ранняя {uuid4().hex[:6]}'), create(page, f'ООО Поздняя {uuid4().hex[:6]}')
    page.goto('/')
    login(page, 'bank')
    held = []
    # hold the response for A until B is on screen
    page.route(f"**/api/applications/{a['id']}", lambda route: held.append(route))
    page.locator('#bankList .item', has_text=a['id']).click()
    page.wait_for_timeout(200)
    assert held, 'запрос карточки A не был отправлен'
    page.locator('#bankList .item', has_text=b['id']).click()
    bank = page.locator('#bankDetail')
    expect(bank).to_contain_text(b['company'])
    page.unroute(f"**/api/applications/{a['id']}")
    for route in held:
        try:
            route.continue_()
        except Exception:
            pass  # the page already cancelled the stale request
    page.wait_for_timeout(500)
    expect(bank).to_contain_text(b['company'])
    expect(bank).not_to_contain_text(a['company'])
    with page.expect_request('**/evaluate') as request:
        bank.get_by_role('button', name=re.compile('Провести скоринг')).click()
    assert b['id'] in request.value.url and request.value.headers['if-match'] == str(b['version'])
    expect(bank.locator('.pill').first).to_have_text('Одобрена')

def test_double_click_creates_one_application(page: Page):
    company = f'ООО Двойной {uuid4().hex[:6]}'
    page.goto('/')
    login(page, 'client')
    page.get_by_label('Компания', exact=True).fill(company)
    page.get_by_role('button', name=re.compile('Подать заявку')).dblclick()
    expect(page.locator('#clientDetail')).to_contain_text(company)
    page.wait_for_timeout(500)
    assert len(applications(page, company)) == 1

def test_stale_card_is_refreshed(page: Page):
    x = create(page, f'ООО Две вкладки {uuid4().hex[:6]}')
    bank = {'Authorization': 'Bearer ' + TOKENS['bank']}
    assert page.request.post(f"/api/applications/{x['id']}/evaluate", headers=bank).ok
    # live updates would refresh the card by themselves; without them the command meets a stale version
    page.route(re.compile(r'/api/changes'), lambda route: route.abort())
    page.goto('/')
    login(page, 'client')
    page.locator('#clientList .item', has_text=x['id']).click()
    card = page.locator('#clientDetail')
    expect(card.locator('.pill').first).to_have_text('Одобрена')
    # another tab accepts the offer while this card still shows it as open
    assert page.request.post(f"/api/applications/{x['id']}/accept", headers={'Authorization': 'Bearer ' + TOKENS['client']}).ok
    page.once('dialog', lambda dialog: dialog.accept())
    card.get_by_role('button', name='Отозвать заявку').click()
    expect(page.locator('#toasts')).to_contain_text('повторите действие')
    expect(card.locator('.pill').first).to_have_text('Условия приняты')
    assert [s['status'] for s in applications(page, x['company'])] == ['accepted']

def test_split_screen_live_updates(page: Page):
    """Two screens: the bank sees a new application without clicking, the client sees the scoring at once."""
    company = f'ООО Два экрана {uuid4().hex[:6]}'
    page.goto('/split')
    client = page.frame_locator('iframe[title="Кабинет предпринимателя"]')
    bank = page.frame_locator('iframe[title="Кабинет банка"]')
    for pane in (client, bank):
        pane.get_by_label('Демо-пароль').fill(PASSWORD)
        pane.get_by_role('button', name=re.compile('Войти')).click()
        expect(pane.locator('#live')).to_have_text('Онлайн')

    client.locator('.chipbtn', has_text='Другие условия').click()
    client.get_by_label('Компания', exact=True).fill(company)
    client.get_by_role('button', name=re.compile('Подать заявку')).click()
    bank_card = bank.locator('#bankDetail')
    expect(bank_card).to_contain_text(company)  # the selection follows the other screen

    bank_card.get_by_role('button', name=re.compile('Провести скоринг')).click()
    # the client screen has no other trigger within the 5 s expect timeout: background polling is every 30 s
    expect(client.locator('#clientDetail')).to_contain_text('Банк предлагает другие условия')
    expect(client.locator('.toast', has_text='банк провёл скоринг')).to_be_visible()

def test_demo_tour(page: Page):
    """The guided tour leads one application from login to accepted terms; the viewer only presses its buttons."""
    page.goto('/split?tour')
    tour = page.locator('#tour')
    step = tour.locator('#tourCount')
    expect(step).to_have_text('Шаг 1 из 8')
    expect(page.locator('#tourSpot')).to_be_visible()  # the password field of the first window
    for name in ('Кабинет предпринимателя', 'Кабинет банка'):
        pane = page.frame_locator(f'iframe[title="{name}"]')
        pane.get_by_label('Демо-пароль').fill(PASSWORD)
        pane.get_by_role('button', name=re.compile('Войти')).click()
    for n, button in [(2, 'Сделать за меня'), (3, 'Сделать за меня'), (4, 'Далее'), (5, 'Сделать за меня'),
                      (6, 'Далее'), (7, 'Сделать за меня')]:
        expect(step).to_have_text(f'Шаг {n} из 8')
        tour.get_by_role('button', name=button).click()
    expect(step).to_have_text('Шаг 8 из 8')
    bank = page.frame_locator('iframe[title="Кабинет банка"]')
    expect(bank.locator('#bankDetail .pill').first).to_have_text('Условия приняты')
    tour.get_by_role('button', name='Завершить').click()
    expect(tour).to_be_hidden()
