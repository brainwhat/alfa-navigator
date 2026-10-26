'use strict';
// Альфа.Навигатор: интерфейс двух ролей. Правила и расчёты живут на сервере, здесь отображение и команды.

const $ = id => document.getElementById(id);
const money = n => Number(n || 0).toLocaleString('ru-RU') + ' ₽';
const BASE_RATE = 0.22;
const APPROVE_SCORE = 65;
const POLL_MS = 30000;
const OPEN_OFFER = ['approved', 'counteroffer'];
const CLOSED = ['accepted', 'rejected', 'withdrawn', 'expired'];

let records = [];       // список заявок текущей роли
let selected = null;    // id выбранной в списке заявки
let current = null;     // показанная карточка: команды применяются к её id и version
let mode = 'client';
let authToken = null;
let busy = false;
let resubmitOf = null;  // заявка, вместо которой подаётся новая
let createKey = null;   // Idempotency-Key текущей попытки подачи
let analyticsDays = '';
let flashIds = new Set();  // заявки, которые только что изменила другая сторона
let statement = null;     // разобранная выписка: ответ /api/statement/analyze

// Режим двух экранов: страница /split открывает приложение дважды, ?pane= закрепляет роль за окном
const pane = ['client', 'bank'].find(r => r === new URLSearchParams(location.search).get('pane')) || null;
if (pane) document.body.classList.add('pane');

const statusName = {submitted: 'На рассмотрении', needs_info: 'Нужны сведения', clarified: 'Уточнена', approved: 'Одобрена', counteroffer: 'Другие условия', rejected: 'Отказ', accepted: 'Условия приняты', withdrawn: 'Отозвана', expired: 'Предложение истекло'};
const fieldName = {available_cashflow: 'Свободный денежный поток, ₽/мес.', revenue: 'Месячная выручка, ₽', existing_payments: 'Платежи по действующим долгам, ₽/мес.', overdue: 'Наличие просрочек'};
const offerName = {amount: 'Меньше сумма', term: 'Дольше срок', requested: 'Запрошено'};
const actorName = {bank: 'Банк', client: 'Клиент', system: 'Система'};
const roleName = {client: 'предприниматель', bank: 'сотрудник банка'};
const MONTH_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTH_NAME = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

function payment(amount, months, rate = BASE_RATE) {
  const r = rate / 12;
  return Math.round(amount * r / (1 - Math.pow(1 + r, -months)));
}
function safe(s) {
  return String(s ?? '').replace(/[&<>"']/g, x => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[x]));
}
function pct(x) { return (x * 100).toFixed(1).replace('.', ',') + '%'; }
function rate(t) { return Math.round((t.annual_rate ?? BASE_RATE) * 100) + '%'; }
function over(t) { return t.overpayment != null ? ` • переплата ${money(t.overpayment)}` : ''; }
function fmtDay(ymd) { return ymd.split('-').reverse().join('.'); }
function fmtDate(iso) { return new Date(iso).toLocaleString('ru-RU', {dateStyle: 'short', timeStyle: 'short'}); }
function ago(min) {
  min = Math.max(0, min);
  if (min < 60) return min + ' мин';
  const h = Math.floor(min / 60);
  return h < 48 ? `${h} ч ${min % 60} мин` : `${Math.floor(h / 24)} дн ${h % 24} ч`;
}
function minutesLeft(iso) { return Math.round((new Date(iso) - Date.now()) / 60000); }

function toast(msg, kind) {
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.innerHTML = `<i class="ph ${{error: 'ph-warning-circle', live: 'ph-lightning'}[kind] || 'ph-check-circle'}"></i><span>${safe(msg)}</span>`;
  $('toasts').append(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, kind === 'error' ? 6000 : 4200);
}

// Единая обработка ответа: таймаут, ответ не в JSON, ошибки полей Pydantic, код запроса для поддержки.
async function api(path, method = 'GET', body, {headers = {}, signal, timeout = 15000} = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  signal?.addEventListener('abort', () => ctrl.abort());
  let res;
  try {
    res = await fetch('/api' + path, {
      method,
      headers: {'Content-Type': 'application/json', ...(authToken ? {Authorization: 'Bearer ' + authToken} : {}), ...headers},
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (e) {
    if (signal?.aborted) throw Object.assign(Error('Запрос отменён'), {name: 'AbortError'});
    throw Object.assign(Error(ctrl.signal.aborted ? 'Сервер не ответил за 15 секунд. Повторите попытку.' : 'Нет связи с сервером. Проверьте соединение и повторите.'), {status: 0});
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { /* HTML или текст от инфраструктуры */ }
  if (!res.ok) {
    let msg = typeof data?.detail === 'string' ? data.detail
      : Array.isArray(data?.detail) ? data.detail.map(d => d.msg).join('; ')
      : res.status >= 500 ? `Сервер временно недоступен (${res.status}). Повторите позже.` : `Ошибка ${res.status}`;
    const requestId = data?.request_id || res.headers.get('X-Request-ID');
    if (res.status >= 500 && requestId) msg += ` Код запроса: ${requestId}`;
    throw Object.assign(Error(msg), {status: res.status, errors: data?.errors || []});
  }
  if (data === null) throw Object.assign(Error('Сервер вернул неожиданный ответ'), {status: res.status});
  return data;
}

// Токены ролей живут до закрытия вкладки, чтобы переключаться между кабинетами без повторного входа
function readTokens() {
  try { return JSON.parse(sessionStorage.getItem('navTokens')) || {}; } catch (e) { return {}; }
}
let tokens = readTokens();
// Оба окна /split пишут в одно хранилище вкладки: перед записью берём его текущее содержимое
function storeToken(role, token) {
  tokens = {...readTokens(), [role]: token};
  if (!token) delete tokens[role];
  try { sessionStorage.setItem('navTokens', JSON.stringify(tokens)); } catch (e) { /* хранилище недоступно */ }
}
function saveRole(role) {
  if (pane) return;  // окно /split не меняет роль, с которой откроется обычный экран
  try { sessionStorage.setItem('navRole', role); } catch (e) { /* хранилище недоступно */ }
}
function pickRole(r) {
  $('loginRole').value = r;
  document.querySelectorAll('.role').forEach(b => {
    b.classList.toggle('on', b.dataset.role === r);
    b.classList.toggle('authed', !!tokens[b.dataset.role]);
    b.setAttribute('aria-checked', b.dataset.role === r);
  });
  const has = !!tokens[r];
  $('pwField').classList.toggle('hidden', has);
  $('loginBtn').innerHTML = (has ? 'Продолжить' : 'Войти') + '<i class="ph ph-arrow-right"></i>';
}
function tab(x) {
  if (!authToken || pane) return;
  if (tokens[x]) return enter(x);
  showLogin(x);
}
function showLogin(role) {
  authToken = null;
  pickRole(role);
  $('loginPassword').value = '';
  $('loginPanel').classList.remove('hidden');
  $('workspace').classList.add('hidden');
  $('session').classList.toggle('hidden', !Object.keys(tokens).length);
}
function selectTab(ids, active) {
  for (const id of ids) {
    const on = id === active;
    $(id).classList.toggle('active', on);
    $(id).setAttribute('aria-selected', on);
    $(id).tabIndex = on ? 0 : -1;
  }
}
async function enter(role) {
  authToken = tokens[role];
  mode = role;
  current = null;  // карточку нужно перерисовать с действиями новой роли
  saveRole(role);
  $('loginPassword').value = '';
  $('workspace').classList.remove('hidden');
  $('session').classList.remove('hidden');
  $('loginPanel').classList.add('hidden');
  $('client').classList.toggle('hidden', role !== 'client');
  $('bank').classList.toggle('hidden', role !== 'bank');
  selectTab(['clientTab', 'bankTab'], role + 'Tab');
  $('roleCaption').textContent = 'Роль: ' + roleName[role];
  startLive();
  await refresh();
  if (role === 'client') simulate();
}
async function login() {
  const role = $('loginRole').value;
  if (tokens[role]) return enter(role);
  try {
    const res = await api('/demo/login', 'POST', {role, password: $('loginPassword').value});
    storeToken(role, res.access_token);
    await enter(role);
  } catch (e) {
    toast(e.message, 'error');
  }
}
function logout() {
  stopLive();
  tokens = {};
  try { sessionStorage.removeItem('navTokens'); } catch (e) { /* хранилище недоступно */ }
  selected = null;
  current = null;
  records = [];
  resubmitOf = null;
  createKey = null;
  clearDetail();
  showLogin($('loginRole').value);
}

// Обновление данных. Ответ, пришедший после смены роли или повторного запроса, отбрасывается.
let refreshSeq = 0;
async function refresh({quiet = false} = {}) {
  if (!authToken || (quiet && busy)) return;
  const seq = ++refreshSeq, role = mode;
  try {
    const list = await api(role === 'bank' ? '/applications' : '/my-applications');
    if (seq !== refreshSeq || role !== mode) return;
    records = list;
    if (selected && !records.some(r => r.id === selected)) selected = null;
    renderList();
    if (selected) await loadDetail({quiet}); else clearDetail();
    if (role === 'bank' && !$('bankAnalytics').classList.contains('hidden')) await loadAnalytics({quiet});
    if (seq === refreshSeq) $('freshness').textContent = 'Обновлено в ' + new Date().toLocaleTimeString('ru-RU');
  } catch (e) {
    if (e.name === 'AbortError' || seq !== refreshSeq) return;
    if (e.status === 401 || e.status === 403) {
      stopLive();
      storeToken(mode, null);
      showLogin(mode);
      return toast('Сессия недействительна, войдите снова', 'error');
    }
    if (quiet) $('freshness').textContent = 'Не удалось обновить, повторим позже';
    else toast('Не удалось загрузить данные: ' + e.message, 'error');
  }
}
setInterval(() => { if (authToken && !document.hidden) refresh({quiet: true}); }, POLL_MS);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return stopLive();  // скрытая вкладка не держит соединение с сервером
  if (authToken) {
    startLive();
    refresh({quiet: true});
  }
});

// Живые обновления: сервер присылает новые события журнала потоком SSE. Поток читается через fetch,
// чтобы передать токен в заголовке, а не в адресе. Сервер закрывает поток через несколько секунд
// (лимит функции Vercel), и окно переподключается с последнего курсора, ничего не теряя.
// Если прокси буферизует поток и первое сообщение не пришло за 5 секунд, окно опрашивает /api/changes.
const LIVE_FIRST_MS = 5000, LIVE_POLL_MS = 3000;
let liveCtrl = null, liveCursor = null, liveBuffered = false;
const liveAction = {submitted: 'новая заявка', evaluated: 'банк провёл скоринг', requested_info: 'банк запросил уточнение',
  clarified: 'клиент уточнил данные', accepted: 'клиент принял условия', extended: 'банк продлил предложение',
  withdrawn: 'клиент отозвал заявку', manual_decision: 'банк вручную изменил решение', expired: 'предложение истекло'};
function sleep(ms, signal) {
  return new Promise(resolve => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, {once: true});
  });
}
function liveState(state) {
  const el = $('live');
  el.className = 'live ' + state;
  el.textContent = {on: 'Онлайн', poll: 'Онлайн, проверка раз в 3 с', lost: 'Нет связи, переподключаемся'}[state] || '';
  el.title = state === 'on' ? 'Изменения другой стороны появляются сразу' : '';
  el.classList.toggle('hidden', !el.textContent);
}
function startLive() {
  stopLive();
  if (!authToken || document.hidden) return;
  liveCtrl = new AbortController();
  liveLoop(liveCtrl);
}
function stopLive() {
  liveCtrl?.abort();
  liveCtrl = null;
  liveState('off');
}
async function liveLoop(ctrl) {
  let failures = 0;
  while (!ctrl.signal.aborted) {
    try {
      if (liveBuffered) {
        onChanges(await api('/changes' + (liveCursor ? '?after=' + liveCursor : ''), 'GET', null, {signal: ctrl.signal}));
        liveState('poll');
        await sleep(LIVE_POLL_MS, ctrl.signal);
      } else {
        const started = Date.now();
        await streamChanges(ctrl);
        if (Date.now() - started < 2000) await sleep(2000, ctrl.signal);  // поток оборвали сразу: не заваливаем сервер
      }
      failures = 0;
    } catch (e) {
      if (ctrl.signal.aborted) return;
      if (e.status === 401 || e.status === 403) return liveState('off');  // повторный вход предложит refresh()
      liveState('lost');
      await sleep(Math.min(30000, 1000 * 2 ** ++failures), ctrl.signal);
    }
  }
}
async function streamChanges(ctrl) {
  const req = new AbortController(), stop = () => req.abort();
  ctrl.signal.addEventListener('abort', stop, {once: true});
  const first = setTimeout(() => { liveBuffered = true; req.abort(); }, LIVE_FIRST_MS);
  try {
    const res = await fetch('/api/changes/stream' + (liveCursor ? '?after=' + liveCursor : ''), {headers: {Authorization: 'Bearer ' + authToken}, signal: req.signal});
    if (!res.ok) throw Object.assign(Error('Поток изменений: ' + res.status), {status: res.status});
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const {done, value} = await reader.read();
      if (done) return;
      buf += decoder.decode(value, {stream: true});
      let end;
      while ((end = buf.indexOf('\n\n')) >= 0) {
        const data = buf.slice(0, end).split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5)).join('\n');
        buf = buf.slice(end + 2);
        if (!data) continue;
        clearTimeout(first);
        liveState('on');
        onChanges(JSON.parse(data));
      }
    }
  } catch (e) {
    if (liveBuffered && !ctrl.signal.aborted) return;  // поток буферизуется: дальше опрос
    throw e;
  } finally {
    clearTimeout(first);
    ctrl.signal.removeEventListener('abort', stop);
  }
}
function onChanges(d) {
  const known = liveCursor !== null;
  liveCursor = d.cursor;
  if (!known || (!d.changes.length && !d.reset)) return;
  const others = d.changes.filter(c => c.actor !== mode);
  if (others.length > 3) toast(`Обновлено заявок: ${new Set(others.map(c => c.application_id)).size}`, 'live');
  else for (const c of others) toast(`${c.company || 'Заявка'}, № ${c.application_id}: ${liveAction[c.action] || c.description}`, 'live');
  flashIds = new Set(others.map(c => c.application_id));
  refresh({quiet: true}).finally(() => flashIds.clear());
}
function flash(el) {
  el.classList.remove('flash');
  void el.offsetWidth;  // перезапуск анимации
  el.classList.add('flash');
  el.addEventListener('animationend', () => el.classList.remove('flash'), {once: true});
}

const bankEmpty = $('bankDetail').innerHTML;
function clearDetail() {
  current = null;
  $('clientDetail').classList.add('hidden');
  $('bankDetail').className = 'empty';
  $('bankDetail').innerHTML = bankEmpty;
}
function detailBox() { return mode === 'bank' ? $('bankDetail') : $('clientDetail'); }

// Списки
function slaLine(x) {
  if (x.waiting_on === 'client') {
    const left = x.offer_expires_at && OPEN_OFFER.includes(x.status) ? minutesLeft(x.offer_expires_at) : null;
    return `<small class="sla ${left != null && left < 1440 ? 'warn' : ''}">Ждёт клиента ${ago(x.in_status_minutes)}${left != null ? ` • предложение действует ещё ${ago(left)}` : ''}</small>`;
  }
  if (x.waiting_on !== 'bank') return '';
  const total = Math.round((new Date(x.sla_due_at) - new Date(x.updated_at)) / 60000), left = total - x.in_status_minutes;
  return x.sla_breached
    ? `<small class="sla bad">SLA нарушен на ${ago(-left)}</small>`
    : `<small class="sla ${left < total / 4 ? 'warn' : ''}">Ждёт банк ${ago(x.in_status_minutes)} • до конца SLA ${ago(left)}</small>`;
}
function clientLine(x) {
  if (x.waiting_on === 'client') {
    const left = x.offer_expires_at && OPEN_OFFER.includes(x.status) ? ` • осталось ${ago(minutesLeft(x.offer_expires_at))}` : '';
    return `<small class="sla warn">Нужно ваше действие${left}</small>`;
  }
  return x.waiting_on === 'bank' ? '<small class="sla">Заявка у банка, ответ в течение суток</small>' : '';
}
function bankQueue() {
  const st = $('fStatus').value, q = $('fSearch').value.trim().toLowerCase(), min = +$('fMin').value || 0, max = +$('fMax').value || Infinity;
  const list = records.filter(x => (st === 'all' || (st === 'closed' ? CLOSED.includes(x.status) : x.waiting_on === st))
    && x.amount >= min && x.amount <= max
    && (!q || x.company.toLowerCase().includes(q) || x.id.toLowerCase().includes(q)));
  const rank = x => x.sla_breached ? 0 : x.waiting_on === 'bank' ? 1 : x.waiting_on === 'client' ? 2 : 3, sort = $('fSort').value;
  return list.sort(sort === 'amount' ? (a, b) => b.amount - a.amount
    : sort === 'new' ? (a, b) => b.created_at.localeCompare(a.created_at)
    : (a, b) => rank(a) - rank(b) || (rank(a) < 3 ? a.updated_at.localeCompare(b.updated_at) : b.created_at.localeCompare(a.created_at)));
}
function itemHtml(x, extra = '') {
  return `<div class="item ${selected === x.id ? 'selected' : ''}${flashIds.has(x.id) ? ' flash' : ''}" tabindex="0" role="button" aria-pressed="${selected === x.id}" onclick="pick('${x.id}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pick('${x.id}')}">
    <div class="row"><strong>${safe(x.company)}${x.id.startsWith('DEMO') ? '<span class="chip demo">демо</span>' : ''}</strong><span class="pill ${x.status}">${statusName[x.status] || x.status}</span></div>
    <div class="meta"><span>№ ${safe(x.id)}</span><span>${money(x.amount)}</span><span>${x.months} мес.</span></div>${extra}</div>`;
}
function emptyState(icon, text, hint = '') {
  return `<div class="empty"><i class="ph ${icon}"></i>${text}${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function renderList() {
  if (mode !== 'bank') {
    $('clientList').innerHTML = records.length ? records.map(x => itemHtml(x, clientLine(x))).join('') : emptyState('ph-tray', 'Заявок пока нет', 'Заполните форму, и заявка появится здесь');
    return;
  }
  const list = bankQueue(), waiting = records.filter(x => x.waiting_on === 'bank'), breached = waiting.filter(x => x.sla_breached).length;
  $('queueSummary').innerHTML = `<div class="qs"><div class="metric"><small>Ждут решения банка</small><strong>${waiting.length}</strong></div><div class="metric"><small>С нарушением SLA</small><strong class="${breached ? 'sla bad' : ''}" style="margin-top:4px">${breached}</strong></div></div>`;
  $('bankList').innerHTML = list.length ? list.map(x => itemHtml(x, slaLine(x))).join('')
    : records.length ? emptyState('ph-magnifying-glass', 'Нет заявок под выбранные фильтры')
    : emptyState('ph-tray', 'Заявок пока нет', 'Нажмите «Заполнить демо-данными», чтобы увидеть очередь');
}
for (const id of ['fStatus', 'fSort', 'fSearch', 'fMin', 'fMax']) $(id).addEventListener('input', renderList);
async function pick(id, announce = true) {
  selected = id;
  if (announce) shareSelection(id);
  renderList();
  try { await loadDetail(); } catch (e) { toast(e.message, 'error'); }
}
// В режиме двух экранов выбор заявки виден и во втором окне; /split пересылает сообщение
function shareSelection(id) {
  if (pane) parent.postMessage({type: 'nav:select', id}, location.origin);
}
// Тур по демо на /split читает состояние окна отсюда, а не из разметки
if (pane) window.navState = () => ({authed: !!authToken, ids: records.map(r => r.id),
  current: current && {id: current.id, status: current.status}});
addEventListener('message', async e => {
  if (!pane || e.origin !== location.origin || e.data?.type !== 'nav:select' || !authToken) return;
  const id = e.data.id;
  if (id === selected) return;
  if (!records.some(r => r.id === id)) await refresh({quiet: true});  // заявку могли подать только что
  if (!records.some(r => r.id === id)) return;
  await pick(id, false);
  // в узком окне карточка ниже очереди; плавная прокрутка в окне без фокуса не срабатывает
  const box = detailBox(), top = box.getBoundingClientRect().top;
  if (top < 0 || top > innerHeight * 0.6) box.scrollIntoView({block: 'start'});
});

// Карточка. Номер запроса и отмена предыдущего: ответ по прежнему выбору не перерисует карточку.
let detailSeq = 0, detailAbort = null;
async function loadDetail({quiet = false} = {}) {
  const id = selected, role = mode, seq = ++detailSeq;
  detailAbort?.abort();
  detailAbort = new AbortController();
  let x;
  try {
    x = await api('/applications/' + encodeURIComponent(id), 'GET', null, {signal: detailAbort.signal});
  } catch (e) {
    if (e.name === 'AbortError' || seq !== detailSeq) return;
    throw e;
  }
  if (seq !== detailSeq || id !== selected || role !== mode) return;
  // тихое обновление не трогает карточку, пока заявка не изменилась: не сбрасываем ввод и раскрытые графики
  if (quiet && current && current.id === x.id && current.version === x.version) return;
  const changed = quiet && current?.id === x.id;
  current = x;
  renderDetail(x);
  if (changed && flashIds.has(x.id)) flash(detailBox());
}
function kv(label, value) { return `<div><small>${label}</small><strong>${value}</strong></div>`; }
function num(label, value) { return `<div><small>${label}</small><strong>${value}</strong></div>`; }

function progress(x) {
  const s = x.status, events = x.events || [];
  const asked = s === 'needs_info' || events.some(e => e.action === 'requested_info');
  const scored = events.some(e => e.action === 'evaluated');
  const steps = [['sent', 'Отправлена']];
  if (asked) steps.push(['info', 'Уточнение']);
  steps.push(['review', 'Рассмотрение'], ['offer', s === 'rejected' ? 'Отказ' : s === 'expired' ? 'Предложение истекло' : 'Предложение'], ['done', s === 'withdrawn' ? 'Отозвана' : 'Условия приняты']);
  const order = steps.map(st => st[0]);
  const at = {submitted: 'review', clarified: 'review', needs_info: 'info', approved: 'offer', counteroffer: 'offer', rejected: 'offer', expired: 'offer', accepted: 'done', withdrawn: 'done'}[s];
  const idx = order.indexOf(at);
  // отозванная заявка: этапы до отзыва отмечаем по истории, а не по позиции
  const reached = s === 'withdrawn' ? order.indexOf(scored ? 'offer' : asked ? 'info' : 'sent') : idx - 1;
  return `<ol class="steps" aria-label="Ход заявки">${steps.map(([, label], i) => {
    const cls = i <= reached ? 'done' : i === idx ? (['rejected', 'expired', 'withdrawn'].includes(s) ? 'bad' : s === 'accepted' ? 'done' : 'current') : '';
    return `<li class="${cls}"${i === idx ? ' aria-current="step"' : ''}>${label}</li>`;
  }).join('')}</ol>`;
}
const CLIENT_NEXT = {
  submitted: 'Банк рассматривает заявку.', clarified: 'Банк рассматривает уточнённую заявку.',
  needs_info: 'Банк просит уточнить сведения: заполните форму ниже.',
  approved: 'Проверьте одобренные условия и примите их.', counteroffer: 'Банк предлагает другие условия: выберите подходящий вариант.',
  rejected: 'Измените параметры и подайте заявку заново.', expired: 'Попросите банк продлить предложение или подайте заявку заново.',
  withdrawn: 'Заявку можно изменить и подать заново.', accepted: 'Подписание и выдача смоделированы, действий не требуется.',
};
const BANK_NEXT = {
  submitted: 'Проведите скоринг или запросите уточнение.', clarified: 'Клиент уточнил данные: проведите скоринг.',
  needs_info: 'Ждём уточнение от клиента.',
  approved: 'Ждём ответа клиента. Предложение можно продлить или изменить решение вручную.',
  counteroffer: 'Ждём выбора клиента. Можно одобрить запрошенные условия вручную.',
  rejected: 'Можно одобрить вручную с обоснованием.', expired: 'Предложение можно продлить.',
  withdrawn: 'Клиент отозвал заявку.', accepted: 'Условия приняты, действий не требуется.',
};
function nextStep(x) {
  const who = x.waiting_on, item = records.find(r => r.id === x.id) || {};
  let deadline = '';
  if (who === 'bank' && item.sla_due_at) deadline = ` Ответ банка — до ${fmtDate(item.sla_due_at)}${item.sla_breached ? ', SLA уже нарушен' : ''}.`;
  if (who === 'client' && x.offer_expires_at && OPEN_OFFER.includes(x.status)) deadline = ` Предложение действует до ${fmtDate(x.offer_expires_at)}, осталось ${ago(minutesLeft(x.offer_expires_at))}.`;
  const title = !who ? 'Заявка завершена.' : who === mode ? 'Ваш ход.' : who === 'bank' ? 'Ход банка.' : 'Ход клиента.';
  const urgent = who === mode || (who === 'client' && OPEN_OFFER.includes(x.status) && minutesLeft(x.offer_expires_at) < 1440);
  return `<div class="notice next ${urgent ? 'warnbox' : ''}"><strong>${title}</strong> ${(mode === 'client' ? CLIENT_NEXT : BANK_NEXT)[x.status] || ''}${deadline}</div>`;
}
function termsCard(title, t, cls, note = '', action = '') {
  return `<div class="terms ${cls}"><h3>${title}</h3>
    <div class="nums">${num('Сумма', money(t.amount))}${num('Срок', t.months + ' мес.')}${num('Ставка', rate(t))}${num('Платёж в месяц', t.monthly_payment != null ? money(t.monthly_payment) : '—')}</div>
    <small class="muted">${t.overpayment != null ? `Переплата за весь срок ${money(t.overpayment)}` : ''}${t.offer_id ? ` • предложение № ${safe(t.offer_id)}${t.issued_at ? ' от ' + fmtDate(t.issued_at) : ''}` : ''}</small>
    ${note ? `<p class="terms-note">${note}</p>` : ''}${action}</div>`;
}
function validity(x) {
  if (!OPEN_OFFER.includes(x.status) || !x.offer_expires_at) return '';
  const left = minutesLeft(x.offer_expires_at);
  return `<p class="terms-note ${left < 1440 ? 'warn' : ''}">Действует до <strong>${fmtDate(x.offer_expires_at)}</strong>, осталось ${ago(left)}</p>`;
}
function scoreCells(o, a) {
  const score = o.score ?? a?.score, load = o.payment_to_cashflow ?? a?.payment_to_cashflow;
  return `<td>${score ?? '—'}</td><td>${load != null ? pct(load) : '—'}</td>`;
}
// Главное в карточке — действующие условия: принятые, одобренные или варианты на выбор.
function currentTerms(x) {
  const e = x.explanation || {}, client = mode === 'client';
  if (e.accepted_terms) {
    const t = e.accepted_terms, requested = t.amount === x.amount && t.months === x.months;
    const note = requested ? 'Клиент принял запрошенные условия.' : `Клиент принял предложение банка вместо запрошенных ${money(x.amount)} на ${x.months} мес. Оценка риска ниже относится к первоначальному запросу.`;
    return termsCard('Согласованные условия', t, 'ok', note) + scheduleBlock(t, 'согласованные условия', x);
  }
  if (x.status === 'approved') {
    const t = e.offer || e.requested || {id: 'requested', amount: x.amount, months: x.months, monthly_payment: payment(x.amount, x.months)};
    const action = client ? `<div class="actions"><button onclick="acceptOffer(null)">Принять условия (демо)</button></div>` : '';
    return termsCard('Одобренные условия', t, 'ok', validity(x), action) + scheduleBlock(t, 'одобренные условия', x);
  }
  const offers = x.status !== 'counteroffer' ? [] : e.offers || (x.alternative_amount ? [{id: 'amount', amount: x.alternative_amount, months: x.months, monthly_payment: payment(x.alternative_amount, x.months)}] : []);
  if (!offers.length) return '';
  const cols = o => `<td>${money(o.amount)}</td><td>${o.months} мес.</td><td>${rate(o)}</td><td>${o.monthly_payment != null ? money(o.monthly_payment) : '—'}</td><td>${o.overpayment != null ? money(o.overpayment) : '—'}</td>`;
  return `<div class="terms"><h3>Банк предлагает другие условия</h3>${validity(x)}
    <div class="scroll"><table class="cmp"><tr><th>Вариант</th><th>Сумма</th><th>Срок</th><th>Ставка</th><th>Платёж</th><th>Переплата</th><th>Оценка</th><th>Нагрузка</th>${client ? '<th></th>' : ''}</tr>
    ${e.requested ? `<tr class="muted"><td>Запрошено</td>${cols(e.requested)}${scoreCells({}, e.assessment)}${client ? '<td>не одобрено</td>' : ''}</tr>` : ''}
    ${offers.map(o => `<tr><td>${offerName[o.id] || o.id}</td>${cols(o)}${scoreCells(o)}${client ? `<td><button onclick="acceptOffer('${o.id}')">Выбрать</button></td>` : ''}</tr>`).join('')}</table></div>
    <p class="terms-note">Оценка и нагрузка — по базовой ставке ${Math.round(BASE_RATE * 100)}%, одобрение от ${APPROVE_SCORE} баллов при нагрузке до 60%. Ставка варианта зависит от его оценки.</p></div>`
    + offers.map(o => scheduleBlock(o, (offerName[o.id] || o.id).toLowerCase(), x)).join('');
}
function scoreBlock(x) {
  const e = x.explanation || {}, a = e.assessment;
  let h = `<h3>Оценка риска запрошенных условий</h3><div class="scorecard"><div><small>Демо-оценка</small><strong>${x.score}<small>/100</small></strong></div><div><div class="bar"><span class="${x.score >= 70 ? 'hi' : x.score >= APPROVE_SCORE ? 'mid' : ''}" style="width:${x.score}%"></span></div><small>Модель ${safe(e.policy_version)} • одобрение от ${APPROVE_SCORE} баллов</small></div></div>`;
  if (a) {
    h += `<div class="scroll"><table class="cmp breakdown"><tr><th>Как сложилась оценка</th><th>Баллы</th></tr><tr><td>Исходный балл</td><td>${a.base_score}</td></tr>
      ${a.contributions.map(c => `<tr><td>${safe(c.factor)}${c.detail ? `<br><small class="muted">${safe(c.detail)}</small>` : ''}</td><td class="${c.points < 0 ? 'neg' : ''}">${c.points ? '−' + Math.abs(c.points) : '0'}</td></tr>`).join('')}
      <tr class="total"><td>Итог</td><td>${a.score}</td></tr></table></div>
      <p class="terms-note">Платёж по запрошенным условиям по базовой ставке ${Math.round(a.reference_rate * 100)}%: ${money(a.monthly_payment)}/мес. Вместе с действующими платежами ${money(x.existing_payments)} это ${pct(a.payment_to_cashflow)} свободного потока ${money(x.available_cashflow)}.</p>`;
  } else {
    h += `<ul class="factors">${(e.factors || []).map(f => `<li>${safe(f)}</li>`).join('')}</ul>`;
  }
  if (e.rate_tiers) h += `<p><small class="muted">Ставка зависит от оценки риска: ${e.rate_tiers.map(([min, r]) => min ? `от ${min} баллов — ${Math.round(r * 100)}%` : `ниже — ${Math.round(r * 100)}% (только при ручном одобрении)`).join(', ')}.</small></p>`;
  if (e.manual_override) h += `<div class="notice"><strong>Решение принято сотрудником банка вручную</strong> (автоскоринг: ${safe(statusName[e.manual_override.auto_decision] || e.manual_override.auto_decision)})<br>${safe(e.manual_override.reason)}</div>`;
  const tips = ['counteroffer', 'rejected'].includes(x.status) ? e.how_to_improve || [] : [];
  if (tips.length) h += `<h3>Как получить одобрение запрошенных условий</h3><ul>${tips.map(t => `<li>${safe(t)}</li>`).join('')}</ul>`;
  return h;
}
function clientActions(x) {
  let h = '';
  if (x.status === 'needs_info') {
    const req = x.info_request || {fields: ['available_cashflow']};
    h += `<h3>Уточнение для банка</h3><div class="notice warnbox"><strong>Банк просит уточнить:</strong> ${req.fields.map(f => fieldName[f].replace(/,.*/, '').toLowerCase()).join(', ')}${req.reason ? `<br>${safe(req.reason)}` : ''}</div>
      <div class="fields" id="clarifyForm">${req.fields.map(f => f === 'overdue'
        ? `<div class="field"><span></span><label class="check"><input type="checkbox" id="cl_overdue" name="overdue" ${x.overdue ? 'checked' : ''}>Есть просрочки</label><small class="hint"></small></div>`
        : `<div class="field"><label for="cl_${f}">${fieldName[f]}</label><input type="number" id="cl_${f}" name="${f}" min="${f === 'existing_payments' ? 0 : 1}" value="${x[f]}" aria-describedby="clh_${f}"><small class="hint" id="clh_${f}"></small></div>`).join('')}
      <div class="field full"><label for="newNote">Комментарий</label><input id="newNote" name="note" value="Уточнили финансовые данные" aria-describedby="clh_note"><small class="hint" id="clh_note"></small></div></div>
      <div class="actions"><button onclick="clarify()">Отправить уточнение</button></div>`;
  }
  const canWithdraw = ['submitted', 'needs_info', 'clarified', 'approved', 'counteroffer'].includes(x.status);
  const canResubmit = ['rejected', 'withdrawn', 'expired'].includes(x.status);
  if (canWithdraw || canResubmit) h += `<div class="actions">${canResubmit ? `<button class="secondary" onclick="refill()">Изменить и подать заново</button>` : ''}${canWithdraw ? `<button class="secondary" onclick="withdraw()">Отозвать заявку</button>` : ''}</div>`;
  return h;
}
function bankActions(x) {
  let h = '';
  if (x.status === 'needs_info' && x.info_request) h += `<div class="notice">Запрошено у клиента: ${x.info_request.fields.map(f => fieldName[f].replace(/,.*/, '').toLowerCase()).join(', ')}${x.info_request.reason ? `<br><small class="muted">${safe(x.info_request.reason)}</small>` : ''}</div>`;
  if (['submitted', 'clarified'].includes(x.status)) {
    h += `<div class="actions"><button onclick="act('evaluate')"><i class="ph ph-lightning"></i>Провести скоринг</button><button class="secondary" onclick="$('infoForm').classList.toggle('hidden')" aria-controls="infoForm">Запросить уточнение</button></div>
      <div id="infoForm" class="notice hidden"><strong>Что уточнить у клиента</strong>${Object.entries(fieldName).map(([k, v]) => `<label class="check"><input type="checkbox" name="infoField" value="${k}" ${k === 'available_cashflow' ? 'checked' : ''}>${v.replace(/,.*/, '')}</label>`).join('')}
      <label for="infoReason" style="margin-top:12px">Комментарий для клиента</label><input id="infoReason" value="Уточните данные с учётом сезонности"><div class="actions"><button onclick="requestInfo()">Отправить запрос</button></div></div>`;
  }
  const buttons = [];
  if (['counteroffer', 'rejected'].includes(x.status)) buttons.push(`<button onclick="decide('approved')">Одобрить вручную</button>`);
  if (OPEN_OFFER.includes(x.status)) buttons.push(`<button class="secondary" onclick="decide('rejected')">Отклонить вручную</button>`);
  if ([...OPEN_OFFER, 'expired'].includes(x.status)) buttons.push(`<button class="secondary" onclick="act('extend')"><i class="ph ph-clock-countdown"></i>Продлить предложение</button>`);
  if (buttons.length) h += `<div class="actions">${buttons.join('')}</div>`;
  return h;
}
function renderDetail(x) {
  const client = mode === 'client';
  const previous = x.previous_id ? `<p class="terms-note">Повторная подача вместо заявки № ${records.some(r => r.id === x.previous_id) ? `<a href="#" onclick="event.preventDefault();pick('${safe(x.previous_id)}')">${safe(x.previous_id)}</a>` : safe(x.previous_id)}</p>` : '';
  const pdf = x.score != null ? `<button class="ghost sm" onclick="downloadDecision()" title="Решение, условия, оценка, график платежей и история одним документом"><i class="ph ph-file-text"></i>Решение в PDF</button>` : '';
  let h = `<div class="detail-head"><div><small>Заявка № ${safe(x.id)} • подана ${fmtDate(x.created_at)}</small><h2>${safe(x.company)}</h2><p>${safe(x.purpose)}</p>${previous}</div><div class="head-side"><span class="pill ${x.status}">${statusName[x.status] || x.status}</span>${pdf}</div></div>`;
  h += progress(x) + nextStep(x);
  if (x.status === 'expired') h += `<div class="notice warnbox">Срок действия предложения истёк.${client ? ' Попросите банк продлить его или измените заявку и подайте заново.' : ''}</div>`;
  h += currentTerms(x);
  h += client ? clientActions(x) : bankActions(x);
  h += `<h3>Запрос клиента</h3><div class="kv">${kv('Сумма', money(x.amount))}${kv('Срок', x.months + ' мес.')}${kv('Выручка в месяц', money(x.revenue))}${kv('Свободный поток в месяц', money(x.available_cashflow))}${kv('Платежи по долгам в месяц', money(x.existing_payments))}${kv('Просрочки', x.overdue ? '<span class="neg">есть</span>' : 'нет')}</div>`;
  h += statementBlock(x);
  if (x.score != null) h += scoreBlock(x);
  h += `<h3>История событий</h3><div class="timeline">${[...x.events].reverse().map(ev => `<div><strong>${safe(ev.description)}</strong><br><small>${new Date(ev.occurred_at).toLocaleString('ru-RU')} • ${actorName[ev.actor] || ev.actor}</small></div>`).join('')}</div>`;
  if (client) {
    $('clientDetail').classList.remove('hidden');
    $('clientDetail').innerHTML = h;
  } else {
    $('bankDetail').className = '';
    $('bankDetail').innerHTML = h;
  }
}

// Сезонность по выписке: поток каждого календарного месяца относительно среднего
function seasonChart(se) {
  const max = Math.max(1, ...se.profile.filter(f => f != null));
  const bars = se.profile.map((f, i) => {
    const label = `${MONTH_NAME[i]}: ${f == null ? 'нет в выписке' : pct(f) + ' от среднего потока'}`;
    const cls = f == null ? 'none' : i + 1 === se.worst_month ? 'worst' : f < 1 ? 'low' : '';
    return `<span class="${cls}" style="height:${f == null || f <= 0 ? 4 : Math.max(4, f / max * 100)}%" title="${label}" aria-label="${label}" role="img"></span>`;
  }).join('');
  const worst = se.worst_month ? `Худший месяц: ${MONTH_NAME[se.worst_month - 1]}, поток на ${se.drop_pct}% ниже среднего.` : '';
  return `<div><small class="muted">Сезонность: поток по месяцам относительно среднего</small><div class="season">${bars}</div>
    <div class="season-labels" aria-hidden="true">${MONTH_SHORT.map(m => `<span>${m}</span>`).join('')}</div><small class="muted">${worst}${se.months_covered < 12 ? ` Месяцев в выписке: ${se.months_covered} из 12.` : ''}</small></div>`;
}
function statementBlock(x) {
  const s = x.statement;
  if (!s) return '<p class="terms-note">Выписка не приложена: финансовые показатели заявлены клиентом.</p>';
  const all = s.check.every(c => c.confirmed);
  const rows = s.check.map(c => `<tr><td>${fieldName[c.field].replace(/,.*/, '')}</td><td>${money(c.declared)}</td><td>${money(c.statement)}</td>
    <td class="${c.confirmed ? 'ok' : 'off'}">${c.confirmed ? 'совпадает' : `${c.diff > 0 ? 'выше' : 'ниже'} на ${pct(Math.abs(c.diff))}`}</td></tr>`).join('');
  const months = s.monthly.map(m => `<tr><td>${MONTH_SHORT[+m.month.slice(5) - 1]} ${m.month.slice(0, 4)}</td><td>${money(m.revenue)}</td><td>${money(m.expenses)}</td><td>${money(m.debt)}</td><td class="${m.cashflow < 0 ? 'neg' : ''}">${money(m.cashflow)}</td></tr>`).join('');
  return `<h3>Сверка с выпиской</h3><div class="notice ${all ? '' : 'warnbox'}"><strong>${all ? 'Цифры заявки совпадают с выпиской.' : 'Часть цифр заявки отличается от выписки.'}</strong>
    Выписка с ${fmtDay(s.period.from)} по ${fmtDay(s.period.to)}: ${s.period.months} мес., ${s.operations} операций. Банк получил только итоговые суммы, операции не сохраняются.</div>
    <div class="scroll"><table class="cmp"><tr><th>Показатель</th><th>Заявлено</th><th>По выписке</th><th>Сверка</th></tr>${rows}</table></div>
    ${seasonChart(s.seasonality)}
    <details class="schedule"><summary>Помесячно по выписке</summary><div><div class="scroll"><table class="cmp"><tr><th>Месяц</th><th>Выручка</th><th>Расходы</th><th>Долги</th><th>Поток</th></tr>${months}</table></div></div></details>`;
}

// Команды: id и version берутся из показанной карточки, а не из текущего выбора в списке.
const doneMessage = {evaluate: 'Скоринг проведён', 'request-info': 'Запрос отправлен клиенту', clarify: 'Уточнение отправлено в банк', accept: 'Условия приняты', extend: 'Предложение продлено', withdraw: 'Заявка отозвана', decision: 'Решение сохранено'};
async function act(kind, body) {
  const x = current;
  if (!x || busy) return;
  const box = detailBox();
  busy = true;
  box.setAttribute('aria-busy', 'true');
  box.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    await api(`/applications/${encodeURIComponent(x.id)}/${kind}`, 'POST', body, {headers: {'If-Match': String(x.version)}});
    toast(doneMessage[kind] || 'Готово');
    busy = false;
    await refresh();
  } catch (e) {
    busy = false;
    if (e.errors?.length) showFieldErrors(box, e.errors);
    toast(e.message, 'error');
    if (e.status === 409) await refresh();
  } finally {
    busy = false;
    box.removeAttribute('aria-busy');
    if (box.isConnected && current === x) box.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
}
function acceptOffer(kind) {
  const e = current?.explanation || {};
  const chosen = kind ? (e.offers || []).find(o => o.id === kind) : e.offer || e.requested;
  act('accept', {...(kind ? {offer: kind} : {}), ...(chosen?.offer_id ? {offer_id: chosen.offer_id} : {})});
}
async function withdraw() {
  if (!confirm('Отозвать заявку? Это действие нельзя отменить.')) return;
  await act('withdraw');
}
async function decide(decision) {
  const reason = prompt(decision === 'approved' ? 'Почему заявка одобряется вопреки автоскорингу?' : 'Причина отказа');
  if (reason === null) return;
  if (reason.trim().length < 10) return toast('Укажите причину не короче 10 символов', 'error');
  await act('decision', {decision, reason: reason.trim()});
}
async function requestInfo() {
  const fields = [...document.querySelectorAll('input[name=infoField]:checked')].map(i => i.value), reason = $('infoReason').value.trim();
  if (!fields.length) return toast('Отметьте хотя бы одно поле', 'error');
  if (reason.length < 5) return toast('Напишите комментарий не короче 5 символов', 'error');
  await act('request-info', {reason, fields});
}
async function clarify() {
  const body = {note: $('newNote').value};
  for (const f of (current.info_request || {fields: ['available_cashflow']}).fields) body[f] = f === 'overdue' ? $('cl_overdue').checked : +$('cl_' + f).value;
  await act('clarify', body);
}

// Ошибки полей показываются под полем вместо подсказки
function setFieldError(input, message) {
  const hint = input.closest('.field')?.querySelector('.hint');
  input.classList.toggle('invalid', !!message);
  if (message) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
  if (!hint) return;
  if (hint.dataset.orig === undefined) hint.dataset.orig = hint.textContent;
  hint.textContent = message || hint.dataset.orig;
  hint.classList.toggle('err', !!message);
}
function showFieldErrors(box, errors) {
  let first = null;
  for (const {field, message} of errors) {
    const input = field && box.querySelector(`[name="${field}"]`);
    if (!input) continue;
    setFieldError(input, message);
    first = first || input;
  }
  first?.focus();
}

// Графики платежей: загружаются при раскрытии, при ошибке можно повторить; выгрузка в CSV
function scheduleBlock(t, title, x) {
  const terms = {amount: +t.amount, months: +t.months, annual_rate: +(t.annual_rate ?? BASE_RATE)};
  return `<details class="schedule" data-terms='${JSON.stringify(terms)}' data-title="${safe(title)}" data-app="${safe(x.id)}" data-policy="${safe(x.explanation?.policy_version || '')}" ontoggle="loadSchedule(this)"><summary>График платежей: ${safe(title)}</summary><div class="skeleton"><span></span><span></span><span></span></div></details>`;
}
async function loadSchedule(el) {
  if (!el.open || el.dataset.loaded) return;
  await renderSchedule(el);
}
async function renderSchedule(el, pp) {
  const t = JSON.parse(el.dataset.terms), box = el.querySelector('div');
  let s;
  try {
    s = await api('/schedule', 'POST', {...t, ...(pp || {})});
  } catch (e) {
    box.className = '';
    box.innerHTML = `<div class="notice warnbox">${safe(e.message)} <button type="button" class="secondary sm" onclick="renderSchedule(this.closest('details'))">Повторить</button></div>`;
    return;
  }
  el.dataset.loaded = 1;
  el._schedule = {s, t, pp};
  box.className = '';
  const note = pp ? `<div class="notice"><strong>Досрочно ${money(pp.prepay_amount)} в ${pp.prepay_month}-й месяц: экономия на процентах ${money(s.savings)}</strong><br>${pp.prepay_mode === 'term' ? `Срок сократится с ${s.baseline.months} до ${s.months} мес., платёж останется ${money(s.monthly_payment)}.` : `Платёж снизится с ${money(s.monthly_payment)} до ${money(s.monthly_after)}, срок останется ${s.months} мес.`}</div>` : '';
  box.innerHTML = `<p><small>${money(t.amount)} на ${t.months} мес. под ${rate(s)}: всего выплат ${money(s.total_paid)}, из них проценты ${money(s.overpayment)}</small></p>
    <div class="prepay"><label>Досрочно, ₽<input type="number" class="pp-amount" min="1" step="10000" value="${pp?.prepay_amount || Math.round(t.amount / 5 / 10000) * 10000}"></label><label>В месяц<input type="number" class="pp-month" min="1" max="${t.months - 1}" value="${pp?.prepay_month || Math.min(6, t.months - 1)}"></label><label>После погашения<select class="pp-mode"><option value="term">Сократить срок</option><option value="payment" ${pp?.prepay_mode === 'payment' ? 'selected' : ''}>Уменьшить платёж</option></select></label><button type="button" class="secondary" onclick="prepay(this)">Рассчитать досрочное погашение</button>${pp ? `<button type="button" class="secondary" onclick="renderSchedule(this.closest('details'))">Без досрочного</button>` : ''}<button type="button" class="secondary" onclick="downloadCsv(this.closest('details'))">Скачать CSV</button></div>${note}
    <div class="scroll" style="max-height:320px"><table class="cmp"><tr><th>Месяц</th><th>Платёж</th>${pp ? '<th>Досрочно</th>' : ''}<th>Основной долг</th><th>Проценты</th><th>Остаток</th></tr>${s.rows.map(r => `<tr${r.prepayment ? ' style="font-weight:650"' : ''}><td>${r.month}</td><td>${money(r.payment)}</td>${pp ? `<td>${r.prepayment ? money(r.prepayment) : ''}</td>` : ''}<td>${money(r.principal)}</td><td>${money(r.interest)}</td><td>${money(r.balance)}</td></tr>`).join('')}</table></div>`;
}
function prepay(btn) {
  const el = btn.closest('details'), v = c => el.querySelector(c).value;
  const pp = {prepay_amount: +v('.pp-amount'), prepay_month: +v('.pp-month'), prepay_mode: v('.pp-mode')};
  if (!(pp.prepay_amount > 0 && pp.prepay_month > 0)) return toast('Укажите сумму и месяц досрочного погашения', 'error');
  renderSchedule(el, pp);
}
// Документ решения собирает сервер: номер, даты, версия правил, оценка, условия, график и история
async function downloadDecision() {
  const x = current;
  if (!x) return;
  try {
    const res = await fetch(`/api/applications/${encodeURIComponent(x.id)}/decision.pdf`, {headers: {Authorization: 'Bearer ' + authToken}, signal: AbortSignal.timeout(20000)});
    if (!res.ok) throw Error((await res.json().catch(() => null))?.detail || `Ошибка ${res.status}`);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await res.blob());
    a.download = `alfa-navigator-${x.id}.pdf`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) {
    toast('Не удалось скачать решение: ' + (e.name === 'TimeoutError' ? 'сервер не ответил за 20 секунд' : e.message), 'error');
  }
}
function downloadCsv(el) {
  const {s, t, pp} = el._schedule || {};
  if (!s) return;
  const rows = [
    ['Альфа.Навигатор — ДЕМО на синтетических данных, не оферта'],
    ['Заявка', el.dataset.app], ['Условия', el.dataset.title], ['Дата выгрузки', new Date().toLocaleString('ru-RU')],
    ['Правила', el.dataset.policy || '—'], ['Сумма, ₽', t.amount], ['Срок, мес.', t.months], ['Ставка, %', Math.round(s.annual_rate * 100)],
    ['Всего выплат, ₽', s.total_paid], ['Проценты, ₽', s.overpayment],
    ...(pp ? [['Досрочно, ₽', pp.prepay_amount], ['Месяц досрочного погашения', pp.prepay_month], ['Экономия, ₽', s.savings]] : []),
    [],
    ['Месяц', 'Платёж, ₽', 'Досрочно, ₽', 'Основной долг, ₽', 'Проценты, ₽', 'Остаток, ₽'],
    ...s.rows.map(r => [r.month, r.payment, r.prepayment, r.principal, r.interest, r.balance]),
  ];
  const csv = '﻿' + rows.map(r => r.map(c => /[;"\n]/.test(String(c)) ? `"${String(c).replace(/"/g, '""')}"` : c).join(';')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
  a.download = `alfa-navigator-${el.dataset.app}-${t.amount}-${t.months}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Форма заявки и симулятор
function fill(el) { el.style.setProperty('--p', (el.value - el.min) / (el.max - el.min) * 100 + '%'); }
function simTerms() {
  return {amount: +$('amount').value, months: +$('months').value, available_cashflow: +$('cashflow').value, existing_payments: +$('debts').value, seasonality: +$('stressSlider').value,
    ...(statement ? {profile: statement.summary.seasonality.profile} : {})};
}
function simulate() {
  const t = simTerms(), p = payment(t.amount, t.months), load = t.available_cashflow ? (p + t.existing_payments) / t.available_cashflow : 0;
  const se = statement?.summary.seasonality;
  $('stressField').classList.toggle('hidden', !!se);
  $('seasonNote').classList.toggle('hidden', !se);
  if (se) $('seasonNote').textContent = 'Сезонность из выписки' + (se.worst_month ? `: в худший месяц (${MONTH_NAME[se.worst_month - 1]}) поток на ${se.drop_pct}% ниже среднего.` : '.');
  $('amtLabel').textContent = money(t.amount);
  $('monthLabel').textContent = t.months + ' мес.';
  $('payLabel').textContent = money(p);
  $('loadLabel').textContent = pct(load);
  const lvl = load > .75 ? 'bad' : load > .6 ? 'warn' : 'ok';
  $('bar').style.width = Math.min(100, load * 100) + '%';
  $('bar').className = lvl;
  $('stressValue').textContent = t.seasonality + '%';
  for (const id of ['amtSlider', 'monthSlider', 'stressSlider']) fill($(id));
  $('advice').className = 'advice ' + lvl;
  $('advice').innerHTML = `<i class="ph ${lvl === 'ok' ? 'ph-check-circle' : 'ph-warning-circle'}"></i><span>${load > .75 ? 'Высокая нагрузка: попробуйте уменьшить сумму или увеличить срок.' : load > .6 ? 'Повышенная нагрузка: проверьте запас прочности на случай снижения выручки.' : 'Платёж выглядит умеренным по демонстрационному порогу. Это не гарантирует одобрение.'}</span>`;
  scheduleSimulation();
  schedulePrecheck();
}
let simTimer, simSeq = 0;
function scheduleSimulation() { clearTimeout(simTimer); simTimer = setTimeout(loadSimulation, 250); }
async function loadSimulation() {
  if (mode !== 'client' || !authToken) return;
  const seq = ++simSeq;
  let s;
  try {
    s = await api('/simulate', 'POST', simTerms());
  } catch (e) {
    if (seq === simSeq) $('seasonalChart').innerHTML = `<small class="muted">${e.status === 422 ? 'График появится, когда сумма, срок и поток заполнены корректно' : safe(e.message)}</small>`;
    return;
  }
  if (seq !== simSeq) return;
  renderChart(s);
}
// Положительный остаток растёт вверх от нулевой линии, отрицательный — вниз
function renderChart(s) {
  const vals = s.timeline.map(r => r.balance_after_debt);
  const top = Math.max(0, ...vals), bottom = Math.max(0, ...vals.map(v => -v)), range = Math.max(1, top + bottom), zero = top / range * 100;
  $('seasonalChart').style.setProperty('--zero', zero + '%');
  $('seasonalChart').innerHTML = s.timeline.map(r => {
    const v = r.balance_after_debt, h = Math.max(1.5, Math.abs(v) / range * 100), name = MONTH_SHORT[r.calendar_month - 1];
    const label = `${r.month}-й месяц (${name}): остаток ${money(v)}${r.loan_payment ? '' : ', кредит уже погашен'}`;
    return `<div class="col${r.winter ? ' winter' : ''}${r.loan_payment ? '' : ' paid'}" role="img" aria-label="${label}" title="${label}"><div class="b"><span class="${v < 0 ? 'neg' : ''}" style="top:${v < 0 ? zero : zero - h}%;height:${h}%"></span></div><small>${name}</small></div>`;
  }).join('');
  const worst = s.timeline.reduce((a, b) => b.balance_after_debt < a.balance_after_debt ? b : a);
  const paid = s.timeline.find(r => !r.loan_payment);
  $('chartSummary').innerHTML = `Минимальный остаток ${money(worst.balance_after_debt)} — ${MONTH_SHORT[worst.calendar_month - 1]}, ${worst.month}-й месяц${worst.balance_after_debt < 0 ? ' <span class="neg">(не хватает денег на платежи)</span>' : ''}.${paid ? ` Кредит погашен после ${paid.month - 1}-го месяца, дальше платёж по нему не списывается.` : ''}`;
  $('chartTable').querySelector('div').innerHTML = `<div class="scroll"><table class="cmp"><tr><th>Месяц</th><th>Поток</th><th>Кредит</th><th>Долги</th><th>Остаток</th></tr>${s.timeline.map(r => `<tr><td>${r.month} (${MONTH_SHORT[r.calendar_month - 1]})</td><td>${money(r.cashflow)}</td><td>${money(r.loan_payment)}</td><td>${money(r.existing_payments)}</td><td class="${r.balance_after_debt < 0 ? 'neg' : ''}">${money(r.balance_after_debt)}</td></tr>`).join('')}</table></div>`;
}

let precheckTimer, precheckSeq = 0;
function schedulePrecheck() { clearTimeout(precheckTimer); precheckTimer = setTimeout(precheck, 300); }
function formTerms() {
  const f = new FormData($('loanForm')), b = {overdue: f.has('overdue')};
  for (const k of ['amount', 'months', 'revenue', 'available_cashflow', 'existing_payments']) b[k] = +f.get(k);
  return b;
}
async function precheck() {
  if (mode !== 'client' || !authToken) return;
  const seq = ++precheckSeq;
  let p;
  try {
    p = await api('/precheck', 'POST', formTerms());
  } catch (e) {
    if (seq === precheckSeq) $('precheck').innerHTML = `<span class="muted">${e.status === 422 ? 'Предварительная оценка появится, когда все поля заполнены корректно' : safe(e.message)}</span>`;
    return;
  }
  if (seq !== precheckSeq) return;
  const verdict = {approved: 'Скорее всего одобрят', counteroffer: 'Предложат другие условия', rejected: 'Скорее всего откажут'}[p.decision];
  const icon = {approved: 'ph-check-circle', counteroffer: 'ph-warning-circle', rejected: 'ph-x-circle'}[p.decision] || 'ph-info';
  const t = p.offer || p.requested;
  $('precheck').innerHTML = `<div class="verdict ${p.decision}"><i class="ph ${icon}"></i><div>
    <div class="row"><div><small>Предварительная оценка</small><strong class="v">${verdict}</strong></div><span class="score">${p.score}<small>/100</small></span></div>
    <small>${p.offer ? `Ставка предложения ${rate(t)}` : `По базовой ставке ${rate(t)}`} • платёж ${money(t.monthly_payment)}/мес.${over(t)}${p.assessment ? ` • нагрузка ${pct(p.assessment.payment_to_cashflow)}` : ''}</small>
    ${p.offers.map(o => `<div class="offer"><small>${offerName[o.id]}: ${money(o.amount)} на ${o.months} мес. под ${rate(o)}, ${money(o.monthly_payment)}/мес.${over(o)}</small><button type="button" class="secondary sm" onclick="submitWith(${o.amount},${o.months})">Подать с этими условиями</button></div>`).join('')}
    ${p.how_to_improve.length ? `<ul>${p.how_to_improve.map(x => `<li><small>${safe(x)}</small></li>`).join('')}</ul>` : ''}
    <small class="disc">${safe(p.disclaimer)}</small></div></div>`;
}
function setTerms(amount, months) {
  $('amount').value = amount;
  $('months').value = months;
  $('amtSlider').value = amount;
  $('monthSlider').value = months;
  createKey = null;
  simulate();
}
function submitWith(amount, months) {
  setTerms(amount, months);
  $('loanForm').requestSubmit();
}

const PRESETS = {
  approve: {label: 'Одобрение', amount: 1_000_000, months: 36, revenue: 1_000_000, available_cashflow: 250_000, existing_payments: 0, overdue: false,
    expect: 'Ожидаемо: автоматическое одобрение, 85 баллов, ставка 18%.'},
  counter: {label: 'Другие условия', amount: 5_000_000, months: 24, revenue: 1_600_000, available_cashflow: 240_000, existing_payments: 30_000, overdue: false,
    expect: 'Ожидаемо: нагрузка выше порога, банк предложит меньшую сумму или более долгий срок.'},
  clarify: {label: 'Уточнение', amount: 1_500_000, months: 24, revenue: 800_000, available_cashflow: 150_000, existing_payments: 20_000, overdue: false,
    expect: 'Пограничная нагрузка 65%: сразу после скоринга — одобрение под 22%. Если банк запросит уточнение потока и клиент подтвердит 210 000 ₽, оценка вырастет до 85 баллов и ставка станет 18%.'},
  manual: {label: 'Ручное решение', amount: 1_500_000, months: 36, revenue: 1_200_000, available_cashflow: 260_000, existing_payments: 10_000, overdue: true,
    expect: 'Ожидаемо: отказ автоскоринга из-за просрочек; сотрудник банка может одобрить вручную с обоснованием, ставка 26%.'},
};
function preset(name) {
  clearStatement();  // сценарий заменяет цифры выписки
  const p = PRESETS[name], f = $('loanForm').elements;
  for (const k of ['revenue', 'available_cashflow', 'existing_payments']) f[k].value = p[k];
  f.overdue.checked = p.overdue;
  f.purpose.value = 'Демо-сценарий: ' + p.label.toLowerCase();
  $('presetNote').textContent = p.expect;
  $('presetNote').classList.remove('hidden');
  setTerms(p.amount, p.months);
}
function refill() {
  clearStatement();
  const f = $('loanForm').elements, x = current;
  for (const k of ['company', 'purpose', 'revenue', 'available_cashflow', 'existing_payments']) f[k].value = x[k];
  f.overdue.checked = !!x.overdue;
  resubmitOf = x.id;
  $('resubmit').innerHTML = `Повторная подача вместо заявки № ${safe(x.id)}. Измените параметры и отправьте форму. <button type="button" class="ghost sm" onclick="cancelResubmit()">Отменить</button>`;
  $('resubmit').classList.remove('hidden');
  setTerms(x.amount, x.months);
  $('loanForm').scrollIntoView({behavior: 'smooth'});
}
function cancelResubmit() {
  resubmitOf = null;
  $('resubmit').classList.add('hidden');
}

// Выписка: сервер считает средние и сезонность и подписывает сводку; форма заполняется ею,
// а заявка несёт сводку как есть, чтобы банк сверил с ней заявленные цифры
$('statementFile').addEventListener('change', async e => {
  const file = e.target.files[0];
  e.target.value = '';  // тот же файл можно выбрать снова
  if (!file) return;
  if (file.size > 3_000_000) return toast('Файл больше 3 МБ: выгрузите выписку за меньший период', 'error');
  const bytes = await file.arrayBuffer();
  let text;
  // выгрузки банков бывают в UTF-8 и в Windows-1251
  try { text = new TextDecoder('utf-8', {fatal: true}).decode(bytes); } catch (err) { text = new TextDecoder('windows-1251').decode(bytes); }
  analyzeStatement(text);
});
async function sampleStatement() {
  try {
    const res = await fetch('/api/statement/sample.csv');
    if (!res.ok) throw Error(`ошибка ${res.status}`);
    await analyzeStatement(await res.text());
  } catch (e) {
    toast('Не удалось загрузить пример: ' + e.message, 'error');
  }
}
async function analyzeStatement(text) {
  const box = $('statementResult');
  box.innerHTML = '<div class="skeleton"><span></span><span></span><span></span></div>';
  try {
    statement = await api('/statement/analyze', 'POST', {csv: text}, {timeout: 30000});
  } catch (e) {
    statement = null;
    box.innerHTML = `<div class="notice warnbox">${safe(e.message)}</div>`;
    return simulate();
  }
  const s = statement.summary, f = $('loanForm').elements;
  f.revenue.value = s.revenue;
  f.existing_payments.value = s.existing_payments;
  if (s.available_cashflow > 0) f.available_cashflow.value = s.available_cashflow;
  for (const k of ['revenue', 'available_cashflow', 'existing_payments']) setFieldError(f[k], '');
  createKey = null;
  $('presetNote').classList.add('hidden');
  renderStatement();
  simulate();
  toast(`Выписка за ${s.period.months} мес. разобрана: поля формы заполнены`);
}
function renderStatement() {
  const box = $('statementResult');
  if (!statement) return void (box.innerHTML = '');
  const s = statement.summary;
  const cats = statement.categories.map(c => `<tr><td>${safe(c.name)}</td><td>${c.count}</td><td>${money(c.total)}</td><td><small>${c.examples.map(safe).join('<br>')}</small></td></tr>`).join('');
  box.innerHTML = `<div class="stmt"><div class="row"><small class="muted">Выписка с ${fmtDay(s.period.from)} по ${fmtDay(s.period.to)}: ${s.period.months} мес. в средних, ${s.operations} операций</small><button type="button" class="ghost sm" onclick="clearStatement()">Убрать</button></div>
    <div class="stmt-nums">${num('Выручка в месяц', money(s.revenue))}${num('Свободный поток', money(s.available_cashflow))}${num('Платежи по долгам', money(s.existing_payments))}</div>
    ${seasonChart(s.seasonality)}
    ${statement.warnings.map(w => `<p class="terms-note warn">${safe(w)}</p>`).join('')}
    <details class="schedule"><summary>Как распознаны операции</summary><div><div class="scroll"><table class="cmp"><tr><th>Категория</th><th>Операций</th><th>Сумма за период</th><th>Крупнейшие по сумме</th></tr>${cats}</table></div>
      <p class="terms-note">Свободный поток: выручка минус операционные расходы и налоги. Платежи по кредитам и лизингу считаются отдельно, свои переводы, полученные кредиты и возвраты не считаются ни выручкой, ни расходами.</p></div></details>
    <small class="muted">Поля формы заполнены по выписке. Если измените их, банк увидит расхождение с выпиской.</small></div>`;
}
function clearStatement() {
  if (!statement) return;
  statement = null;
  renderStatement();
  simulate();
}

$('loanForm').addEventListener('input', e => {
  createKey = null;  // изменённая форма — новая заявка, а не повтор прежней попытки
  if (e.target.name) setFieldError(e.target, '');
  schedulePrecheck();
});
$('amtSlider').oninput = e => { $('amount').value = e.target.value; simulate(); };
$('monthSlider').oninput = e => { $('months').value = e.target.value; simulate(); };
for (const id of ['amount', 'months', 'cashflow', 'debts']) $(id).addEventListener('input', () => {
  if (id === 'amount') $('amtSlider').value = $('amount').value;
  if (id === 'months') $('monthSlider').value = $('months').value;
  simulate();
});
$('stressSlider').addEventListener('input', simulate);

$('loanForm').onsubmit = async e => {
  e.preventDefault();
  const btn = $('submitBtn');
  if (btn.disabled) return;
  const f = new FormData(e.target), body = Object.fromEntries(f.entries());
  for (const k of ['amount', 'months', 'revenue', 'available_cashflow', 'existing_payments']) body[k] = +body[k];
  body.overdue = f.has('overdue');
  if (resubmitOf) body.previous_id = resubmitOf;
  if (statement) body.statement = statement.summary;
  // один ключ на попытку: повтор после обрыва связи вернёт ту же заявку, а не создаст вторую
  createKey = createKey || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  try {
    const row = await api('/applications', 'POST', body, {headers: {'Idempotency-Key': createKey}});
    createKey = null;
    cancelResubmit();
    selected = row.id;
    current = null;
    shareSelection(row.id);
    await refresh();
    toast(`Заявка № ${row.id} отправлена. Статус и ответ банка — в карточке ниже.`);
    $('clientDetail').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (ex) {
    if (ex.errors?.length) showFieldErrors(e.target, ex.errors);
    toast(ex.message, 'error');
  } finally {
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }
};

// Кабинет банка: очередь и аналитика
function bankView(v) {
  const a = v === 'analytics';
  $('bankAnalytics').classList.toggle('hidden', !a);
  $('bankQueueView').classList.toggle('hidden', a);
  selectTab(['bqTab', 'baTab'], a ? 'baTab' : 'bqTab');
  if (a) loadAnalytics();
}
function share(a, b) { return b ? Math.round(100 * a / b) + '%' : '—'; }
function dur(m) { return m == null ? '—' : ago(Math.round(m)); }
function tile(label, value, note = '', cls = '') {
  return `<div class="metric"><small>${label}</small><strong class="${cls}">${value}</strong>${note ? `<small class="muted">${note}</small>` : ''}</div>`;
}
function bars(items) {
  const max = Math.max(1, ...items.map(i => i.value));
  return items.map(i => `<div class="hbar"><div class="row"><small>${safe(i.label)}</small><small><strong>${i.value}</strong>${i.note ? ` <span class="muted">${safe(i.note)}</span>` : ''}</small></div><div class="bar" aria-hidden="true"><span style="width:${100 * i.value / max}%"></span></div></div>`).join('');
}
let analyticsSeq = 0;
async function loadAnalytics({quiet = false} = {}) {
  const seq = ++analyticsSeq;
  let a;
  try {
    a = await api('/analytics' + (analyticsDays ? '?days=' + analyticsDays : ''));
  } catch (e) {
    if (!quiet && seq === analyticsSeq) $('bankAnalytics').innerHTML = `<div class="card">${safe(e.message)} <button class="secondary sm" onclick="loadAnalytics()">Повторить</button></div>`;
    return;
  }
  if (seq !== analyticsSeq || mode !== 'bank') return;
  const s = a.sla, f = a.funnel, o = a.offers, given = Object.values(o).reduce((x, y) => x + y, 0), d = a.auto_decisions;
  const periods = [['', 'За всё время'], ['1', 'За сутки'], ['7', 'За 7 дней'], ['30', 'За 30 дней']];
  const now = Object.entries(a.by_status).map(([k, n]) => `${(statusName[k] || k).toLowerCase()} — ${n}`).join(', ');
  const rates = list => list.map(x => ({label: Math.round(x.rate * 100) + '% годовых', value: x.count}));
  $('bankAnalytics').innerHTML = `<div class="toolbar"><label for="aPeriod" class="sr-only">Период</label><select id="aPeriod" class="period" onchange="analyticsDays=this.value;loadAnalytics()">${periods.map(([v, l]) => `<option value="${v}" ${v === analyticsDays ? 'selected' : ''}>${l}</option>`).join('')}</select><small class="muted">Заявки, поданные за выбранный период</small></div>
  <div class="stats">${tile('Заявок', a.total)}${tile('Ответы банка в SLA', s.within_sla_pct == null ? '—' : s.within_sla_pct + '%', `${s.responses} ответов, SLA ${s.hours} ч${s.breached ? `, нарушений ${s.breached}` : ''}`)}${tile('Первый ответ банка', dur(s.first_avg_minutes), `в среднем, медиана ${dur(s.first_median_minutes)}; каждый цикл — ${dur(s.avg_minutes)}`)}${tile('Ждут банк сейчас', s.waiting_now, s.breached_now ? `с нарушением SLA: ${s.breached_now}` : s.oldest_wait_minutes != null ? `дольше всех ${dur(s.oldest_wait_minutes)}` : 'очередь пуста', s.breached_now ? 'sla bad' : '')}</div>
  <div class="grid"><section class="card"><h2>Воронка</h2>${bars(f.map((x, i) => ({label: x.stage, value: x.count, note: i ? share(x.count, f[0].count) + ' от поданных' : ''})))}<p><small class="muted">Этап засчитывается, если заявка хоть раз его достигла, даже если позже решение изменилось. Сейчас: ${now || 'заявок нет'}.</small></p></section>
  <section class="card"><h2>Чем закончились предложения</h2>${bars([{label: 'Приняты', value: o.accepted}, {label: 'Ждут ответа клиента', value: o.pending}, {label: 'Истекли', value: o.expired}, {label: 'Отозваны клиентом', value: o.withdrawn}, {label: 'Отклонены банком позже', value: o.rejected}])}<p><small class="muted">Принято ${share(o.accepted, given)} выданных предложений. Средняя запрошенная сумма ${a.avg_requested_amount == null ? '—' : money(a.avg_requested_amount)}, средняя принятая ${a.avg_accepted_amount == null ? '—' : money(a.avg_accepted_amount)}.</small></p></section>
  <section class="card"><h2>Причины отказов и контрпредложений</h2>${a.top_factors.length ? bars(a.top_factors.map(x => ({label: x.factor, value: x.count}))) : emptyState('ph-tray', 'Пока нет')}<p><small class="muted">Автоскоринг: одобрено ${d.approved || 0}, другие условия ${d.counteroffer || 0}, отказ ${d.rejected || 0}. Ручных решений ${a.manual.total}, из них одобрений вопреки автоскорингу ${a.manual.approved_against_auto}.</small></p></section>
  <section class="card"><h2>Ставки</h2><h3 style="margin-top:8px">Одобренные заявки</h3>${a.approved_rates.length ? bars(rates(a.approved_rates)) : emptyState('ph-tray', 'Пока нет одобренных заявок')}<h3>Принятые условия</h3>${a.accepted_rates.length ? bars(rates(a.accepted_rates)) : emptyState('ph-tray', 'Пока никто не принял условия')}<p><small class="muted">Ставки берутся из выданных и принятых предложений, включая выбранные контрпредложения; 26% бывает только при ручном одобрении.</small></p></section></div>`;
}

async function seedDemo() {
  if (!confirm('Создать набор демонстрационных заявок с историей за последние дни? Прежние демо-заявки будут пересозданы, настоящие не изменятся.')) return;
  try {
    const r = await api('/demo/seed', 'POST', null, {timeout: 30000});
    selected = null;
    await refresh();
    toast(`Создано демо-заявок: ${r.created}`);
  } catch (e) {
    toast(e.message, 'error');
  }
}
async function clearDemo() {
  if (!confirm('Удалить все демонстрационные заявки? Настоящие заявки не изменятся.')) return;
  try {
    const r = await api('/demo/seed', 'DELETE');
    if (selected?.startsWith('DEMO')) selected = null;
    await refresh();
    toast(`Удалено демо-заявок: ${r.deleted}`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// Вкладки: стрелки, Home и End переключают соседние вкладки
document.addEventListener('keydown', e => {
  const t = e.target.closest?.('[role=tab]');
  if (!t || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const tabs = [...t.parentElement.querySelectorAll('[role=tab]')], i = tabs.indexOf(t);
  const next = tabs[e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
  e.preventDefault();
  next.focus();
  next.click();
});

simulate();
{
  let last = null;
  try { last = sessionStorage.getItem('navRole'); } catch (e) { /* хранилище недоступно */ }
  const role = pane || (tokens[last] ? last : Object.keys(tokens).find(r => tokens[r]));
  if (tokens[role]) enter(role); else pickRole(role || 'client');
}
