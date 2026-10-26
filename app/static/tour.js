'use strict';
// Тур по демо на странице /split: одна заявка проходит путь от подачи до принятых условий.
// Тур подсвечивает нужный элемент в окне клиента или банка и сам переходит дальше, когда шаг выполнен;
// «Сделать за меня» выполняет шаг вместо зрителя. Окна — фреймы того же источника, поэтому тур
// читает их разметку и состояние (navState из app.js) напрямую.
(() => {
  const $ = id => document.getElementById(id);
  const frames = Object.fromEntries([...document.querySelectorAll('iframe[data-role]')].map(f => [f.dataset.role, f]));
  const narrow = matchMedia('(max-width:900px)');
  const OPEN = ['approved', 'counteroffer'];
  const FAILED = {rejected: 'Отказ', withdrawn: 'Отозвана', expired: 'Предложение истекло'};

  const doc = role => frames[role].contentDocument;
  const win = role => frames[role].contentWindow;
  function state(role) {
    try { return win(role).navState?.() || null; } catch (e) { return null; }
  }
  const shown = el => !!el && el.getClientRects().length > 0;
  function button(role, text) {
    const box = doc(role)?.getElementById(role === 'bank' ? 'bankDetail' : 'clientDetail');
    return [...(box?.querySelectorAll('button') || [])].find(b => b.textContent.trim().startsWith(text) && shown(b));
  }

  let app = null;        // заявка, которую ведёт тур
  let known = new Set(); // заявки, которые были у клиента до подачи
  // Заявка тура, если окно показывает именно её
  const ours = role => {
    const c = state(role)?.current;
    return app && c?.id === app ? c : null;
  };
  const open = role => () => win(role).pick(app, false);

  // click — «Сделать за меня» нажимает подсвеченный элемент; act — своё действие, пока шаг не выполнен.
  // done — шаг выполнен, тур идёт дальше сам; ready — можно нажать «Далее».
  const STEPS = [{
    title: 'Войдите в оба кабинета',
    text: () => 'Тур проведёт одну заявку от подачи до принятых условий. ' + (narrow.matches
      ? 'Кабинеты предпринимателя и банка переключаются вкладками сверху, тур сам откроет нужный. Введите демо-пароль в каждом.'
      : 'Слева кабинет предпринимателя, справа — банка. Введите демо-пароль в каждом окне.'),
    role: () => ['client', 'bank'].find(r => !state(r)?.authed) || null,
    target: role => doc(role)?.getElementById('loginPassword'),
    done: () => state('client')?.authed && state('bank')?.authed,
    wait: 'Ждём вход в оба окна',
  }, {
    title: 'Предприниматель: заявка на 5 млн ₽',
    text: 'Сценарий «Другие условия» заполняет форму кофейни: 5 млн ₽ на 24 месяца при свободном потоке 240 тыс. ₽ в месяц. Под формой сразу видна предварительная оценка по тем же правилам, что у банка.',
    role: 'client',
    target: () => [...(doc('client')?.querySelectorAll('.chipbtn') || [])].find(b => b.textContent === 'Другие условия'),
    click: true,
    done: () => !!doc('client')?.getElementById('presetNote')?.textContent.includes('нагрузка выше порога'),
    wait: 'Нажмите «Другие условия» над формой',
  }, {
    title: 'Подайте заявку',
    text: 'Заявка уходит в банк. Следите за окном банка: заявка появится там сразу, без перезагрузки, с уведомлением и уже открытой карточкой.',
    role: 'client',
    target: () => doc('client')?.getElementById('submitBtn'),
    enter: () => { known = new Set(state('client')?.ids); },
    click: true,
    done: () => {
      const c = state('client')?.current;
      if (c && c.status === 'submitted' && !known.has(c.id)) app = c.id;
      return !!app;
    },
    wait: 'Нажмите «Подать заявку»',
  }, {
    title: 'Банк: заявка уже здесь',
    text: 'Карточка открылась сама: выбор заявки общий для двух окон. Видно, чей сейчас ход и до какого срока банк должен ответить по SLA.',
    role: 'bank',
    target: () => ours('bank') && doc('bank').querySelector('#bankDetail .detail-head'),
    ready: () => !!ours('bank'),
    act: open('bank'), actLabel: 'Открыть заявку',
    wait: 'Ждём, пока заявка откроется в окне банка',
  }, {
    title: 'Банк: проведите скоринг',
    text: 'Демо-модель считает балл: исходные 85 минус вычеты за нагрузку, просрочки и размер запроса. Запрошенные условия не проходят, поэтому банк предложит варианты.',
    role: 'bank',
    target: () => ours('bank') && button('bank', 'Провести скоринг'),
    click: true,
    done: () => {
      const c = ours('bank');
      return !!c && !['submitted', 'clarified', 'needs_info'].includes(c.status);
    },
    wait: 'Нажмите «Провести скоринг»',
  }, {
    title: 'Предприниматель видит ответ',
    text: () => ours('client')?.status === 'approved'
      ? 'Ответ пришёл сразу, с уведомлением и подсветкой карточки: банк одобрил запрошенные условия. Ставка зависит от балла.'
      : 'Ответ пришёл сразу, с уведомлением и подсветкой карточки. Банк предлагает другие условия: у каждого варианта своя ставка, платёж, переплата, балл и нагрузка.',
    role: 'client',
    target: () => ours('client') && doc('client').querySelector('#clientDetail .terms'),
    ready: () => OPEN.includes(ours('client')?.status),
    act: open('client'), actLabel: 'Открыть заявку',
    wait: 'Ждём ответ банка в окне предпринимателя',
  }, {
    title: 'Выберите вариант',
    text: 'Принимаются ровно выданные условия, даже если тарифы с тех пор изменились. После выбора наверху карточки остаются согласованные условия.',
    role: 'client',
    target: () => ours('client') && (button('client', 'Выбрать') || button('client', 'Принять условия')),
    click: true,
    done: () => ours('client')?.status === 'accepted',
    wait: 'Нажмите «Выбрать» у подходящего варианта',
  }, {
    title: 'Готово: условия приняты',
    text: 'Банк увидел выбор клиента в ту же секунду. История событий внизу карточки хранит каждый шаг, а вкладка «Аналитика» показывает воронку и SLA по всем заявкам.',
    role: 'bank',
    target: () => ours('bank') && doc('bank').querySelector('#bankDetail .detail-head'),
    ready: () => ours('bank')?.status === 'accepted',
    act: open('bank'), actLabel: 'Открыть заявку',
    wait: 'Ждём обновление в окне банка',
  }];

  const card = $('tour'), spot = $('tourSpot');
  let index = -1, raf = 0, checked = 0, advancing = false, lastRole, scrolled, element = null, failed = false;

  function roleOf(step) {
    return typeof step.role === 'function' ? step.role() : step.role;
  }
  function targetOf(step, role) {
    if (!role) return null;
    try { return step.target(role) || null; } catch (e) { return null; }
  }
  function satisfied(step) {
    return step.done ? !!step.done() : step.ready ? !!step.ready() : true;
  }

  // Кольцо вокруг элемента внутри фрейма; часть, ушедшая за край окна, обрезается
  function place(el, role) {
    const f = role && frames[role].getBoundingClientRect();
    if (!shown(el) || !f.width) return spot.classList.add('hidden');
    const r = el.getBoundingClientRect(), pad = 6;
    const top = Math.max(f.top, f.top + r.top - pad), bottom = Math.min(f.bottom, f.top + r.bottom + pad);
    const left = Math.max(f.left, f.left + r.left - pad), right = Math.min(f.right, f.left + r.right + pad);
    if (bottom - top < 12 || right - left < 12) return spot.classList.add('hidden');
    Object.assign(spot.style, {top: top + 'px', left: left + 'px', width: right - left + 'px', height: bottom - top + 'px'});
    spot.classList.remove('hidden');
  }

  function go(i) {
    index = i;
    advancing = false;
    failed = false;
    lastRole = undefined;
    scrolled = null;
    const step = STEPS[i];
    $('tourCount').textContent = `Шаг ${i + 1} из ${STEPS.length}`;
    $('tourDots').innerHTML = STEPS.map((_, k) => `<span class="${k < i ? 'done' : k === i ? 'on' : ''}"></span>`).join('');
    $('tourTitle').textContent = step.title;
    $('tourText').textContent = '';
    $('tourNext').textContent = i === STEPS.length - 1 ? 'Завершить' : 'Далее';
    step.enter?.();
    checked = 0;
  }

  function check(step, role, el) {
    const c = app && (ours('client') || ours('bank'));
    if (c && FAILED[c.status]) {
      if (!failed) {
        failed = true;
        $('tourTitle').textContent = 'Сценарий сбился';
        $('tourText').textContent = `Заявка получила статус «${FAILED[c.status]}». Для тура нужен сценарий «Другие условия» без изменений в форме. Начните заново: тур подаст новую заявку.`;
        $('tourWait').textContent = '';
        $('tourAct').textContent = 'Начать заново';
        $('tourAct').classList.remove('hidden');
        $('tourNext').classList.add('hidden');
        spot.classList.add('hidden');
      }
      return;
    }
    const text = typeof step.text === 'function' ? step.text() : step.text;
    if ($('tourText').textContent !== text) $('tourText').textContent = text;
    const ok = satisfied(step), last = index === STEPS.length - 1;
    $('tourWait').textContent = ok ? (step.done ? 'Готово' : '') : step.wait;
    $('tourWait').classList.toggle('ok', ok);
    $('tourNext').classList.remove('hidden');
    $('tourNext').disabled = !ok;
    const act = last && ok ? 'Пройти ещё раз' : step.click && el && !ok ? 'Сделать за меня' : step.act && !ok ? step.actLabel : '';
    $('tourAct').textContent = act;
    $('tourAct').classList.toggle('hidden', !act);
    if (role !== lastRole) {
      lastRole = role;
      card.classList.toggle('left', role === 'bank');   // карточка закрывает другое окно, а не подсвеченное
      card.classList.toggle('right', role === 'client');
      if (role && narrow.matches) window.show(role);
    }
    if (step.done && ok && !advancing && !last) {
      advancing = true;
      setTimeout(() => { if (index === STEPS.indexOf(step)) go(index + 1); }, 700);
    }
  }

  function frame(t) {
    raf = requestAnimationFrame(frame);
    const step = STEPS[index], role = roleOf(step);
    element = failed ? null : targetOf(step, role);
    // плавная прокрутка во фрейме без фокуса не срабатывает, поэтому мгновенная
    const key = index + ':' + role;
    if (element && scrolled !== key && shown(element)) {
      scrolled = key;
      element.scrollIntoView({block: 'center'});
    }
    place(element, role);
    if (t - checked < 250) return;
    checked = t;
    check(step, role, element);
  }

  function start() {
    app = null;
    known = new Set();
    card.classList.remove('hidden');
    go(0);
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    cancelAnimationFrame(raf);
    card.classList.add('hidden');
    spot.classList.add('hidden');
  }

  $('tourNext').onclick = () => index === STEPS.length - 1 ? stop() : go(index + 1);
  $('tourAct').onclick = () => {
    const step = STEPS[index];
    if (failed || (index === STEPS.length - 1 && satisfied(step))) return start();
    if (step.click) element?.click(); else step.act?.();
  };
  $('tourClose').onclick = stop;
  $('tourStart').onclick = start;
  addEventListener('keydown', e => { if (e.key === 'Escape' && !card.classList.contains('hidden')) stop(); });
  if (new URLSearchParams(location.search).has('tour')) start();
})();
