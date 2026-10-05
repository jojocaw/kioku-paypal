'use strict';
// Kioku demo page: plain JavaScript, no build step. Everything comes from the JSON API in kioku/web.py.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const MODEL_NAMES = {
  'nvidia/Nemotron-3_5-Lightning': 'Nemotron 3.5 Lightning',
  'nvidia/nemotron-3-super-120b-a12b': 'Nemotron 3 Super',
  'nvidia/Nemotron-3-Ultra-550b-a55b': 'Nemotron 3 Ultra',
};
const modelName = id => MODEL_NAMES[id] || id;
let STATE = null;
let busy = false;

async function api(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { data = { error: 'Unexpected answer from the server.' }; }
  if (!res.ok && !data.error) data.error = `Error ${res.status}`;
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------- tiny Markdown (escaped first, then a few marks)
function linkTarget(name) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(name)) return `daily/${name}.md`;
  if (/^\d{4}-W\d{2}$/.test(name)) return `weekly/${name}.md`;
  if (/^\d{4}-\d{2}$/.test(name)) return `monthly/${name}.md`;
  return `topics/${name}.md`;
}
function inline(s) {
  return esc(s)
    .replace(/\[\[([^\]]+)\]\]/g, (m, n) => `<a href="#" class="wl" data-note="${esc(linkTarget(n))}">${n}</a>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/(^|\s)_([^_]+)_(?=\s|$|[.,])/g, '$1<em>$2</em>');
}
function md(text) {
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`<${list}>` + out.splice(listStart).join('') + `</${list}>`); list = null; } };
  let listStart = 0;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/))) {
      const kind = /^\s*\d+\./.test(line) ? 'ol' : 'ul';
      if (list !== kind) { close(); list = kind; listStart = out.length; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    close();
    if (!line.trim()) continue;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
    else if ((m = line.match(/^>\s?(.*)$/))) out.push(`<blockquote>${inline(m[1])}</blockquote>`);
    else out.push(`<p>${inline(line)}</p>`);
  }
  close();
  return `<div class="md">${out.join('')}</div>`;
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  $$('.view').forEach(v => { v.hidden = v.id !== `panel-${name}`; });
  const narrow = matchMedia('(max-width: 960px)').matches;
  $('#panel-chat').classList.toggle('show', name === 'chat' || !narrow);
  $('.side').classList.toggle('show', name !== 'chat');
  if (name === 'memory' && !$('#tree').dataset.loaded) loadMemory();
  if (name === 'guard') loadGuard();
  if (name === 'approvals') loadApprovals();
  if (name === 'usage') loadUsage();
  if (name === 'money') loadMoney();
  try { localStorage.setItem('kioku-tab', name); } catch (e) { /* private mode */ }
}

// ---------------------------------------------------------------- header, stats, suggestions
function fmtNow(iso) {
  const d = new Date(iso.slice(0, 16));
  return d.toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
async function loadState() {
  STATE = await api('/api/state');
  if (STATE.error) return;
  const p = STATE.persona || {};
  const c = STATE.counts;
  const dayN = p.started ? Math.round((new Date(STATE.now.slice(0, 10)) - new Date(p.started)) / 864e5) + 1 : null;
  $('#who').innerHTML = (STATE.mode === 'demo'
    ? `<span class="chip">Demo owner: <strong>${esc(p.name)}</strong> · ${esc(p.business)} <em>(fictional)</em></span>` : '')
    + `<span class="chip">${esc(fmtNow(STATE.now))}</span>`
    + (dayN ? `<span class="chip">day <strong>${dayN}</strong> with Kioku</span>` : '');
  $('#stats').innerHTML = [
    [c.days, 'daily notes'], [c.weekly, 'weekly reviews'], [c.monthly, 'monthly reviews'], [c.topics, 'topic notes'],
    [c.receipts.toLocaleString(), 'guard receipts'], [c.redacted.toLocaleString(), 'calls with personal data hidden', 'hide'],
    [c.blocked, 'blocked', 'block'], [c.asked, 'asked the owner', 'ask'],
  ].map(([n, label, cls]) => `<span class="stat ${cls || ''}"><b>${n}</b>${label}</span>`).join('');
  $('#pending-count').textContent = c.pending || '';
  const money = STATE.money || {};
  $('#tab-money').hidden = !money.on;
  if (money.on) {
    $('#late-count').textContent = money.overdue || '';
    $('#stats').insertAdjacentHTML('beforeend', `<span class="stat pp"><b>${money.open}</b>open invoices · ${esc(money.due)} due</span>`
      + (money.overdue ? `<span class="stat block"><b>${money.overdue}</b>overdue</span>` : ''));
  }
  $('#limits').textContent = STATE.mode === 'demo'
    ? `Demo limits: ${STATE.limits.chats_left} messages and ${STATE.limits.jobs_left} habit runs left this hour.` : '';
  if (!$('#suggest').dataset.done) renderSuggestions(STATE.suggestions || []);
}
function renderSuggestions(list) {
  if (!list.length) return;
  const groups = {};
  list.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
  $('#suggest').innerHTML = Object.entries(groups).map(([g, items]) =>
    `<div class="row"><span class="group">${esc(g)}</span>` + items.map(s =>
      `<button type="button" class="${s.group.startsWith('Try') ? 'rule' : ''}" data-text="${esc(s.text)}">${esc(s.label || s.text)}</button>`).join('') + '</div>').join('')
    + '<button type="button" class="toggle" id="ideas">Hide ideas</button>';
  $('#suggest').dataset.done = '1';
}

// ---------------------------------------------------------------- chat
function intro() {
  const p = (STATE && STATE.persona) || {};
  $('#msgs').innerHTML = `<div class="intro">${STATE && STATE.mode === 'demo'
    ? `You are <b>${esc(p.name)}</b>, owner of ${esc(p.business)} — a fictional bakery. ${esc(p.about || '')} Ask Kioku about the past, bill a customer through PayPal, or try to make it break its rules. Everything you do stays in your private copy.`
    : 'Talk to Kioku. It remembers through your notes.'}</div>`;
}
function highlightPlaceholders(s) {
  return esc(s).replace(/\[(?:NAME|EMAIL|PHONE|CARD|SECRET|POSTCODE|GOV_ID)_\d+\]/g, m => `<span class="ph">${m}</span>`);
}
function addMine(text) {
  const el = document.createElement('div');
  el.className = 'msg me';
  el.innerHTML = `<div class="bubble">${esc(text).replace(/\n/g, '<br>')}</div>`;
  $('#msgs').append(el);
  el.scrollIntoView({ block: 'end' });
  return el;
}
function eventChip(e) {
  if (e.tool === 'recall_check') {
    return `<button class="ev allow" data-receipt="${esc(e.receipt)}" title="receipt ${esc(e.receipt)}">checked · searched the notes before saying “I don't know”</button>`;
  }
  if (e.tool === 'action_check') {
    return `<button class="ev allow" data-receipt="${esc(e.receipt)}" title="receipt ${esc(e.receipt)}">checked · asked the guard to decide, not the model</button>`;
  }
  if (e.tool === 'paypal_check') {
    const text = e.rule === 'money.check_first' ? 'checked · read PayPal before answering'
      : e.rule === 'honesty.paypal_checked' ? `checked with PayPal · ${e.detail.replace(/^checked with PayPal: /, '')}`
      : e.rule === 'honesty.paypal_corrected' ? 'corrected · PayPal says otherwise' : 'could not reach PayPal';
    return `<button class="ev ${esc(e.decision)} pp" data-receipt="${esc(e.receipt)}" title="${esc(e.detail)} · receipt ${esc(e.receipt)}">${esc(text)}</button>`;
  }
  if (e.tool === 'check_invoices' && e.decision === 'allow') {
    return `<button class="ev allow pp" data-receipt="${esc(e.receipt)}" title="${esc(e.detail)} · receipt ${esc(e.receipt)}">read PayPal · check_invoices</button>`;
  }
  if (e.tool === 'reply_check') {
    const text = e.rule === 'honesty.note_added' ? 'checked · told you plainly what was not done' : 'checked · the reply said “done”, so Kioku did it';
    return `<button class="ev ${esc(e.decision)}" data-receipt="${esc(e.receipt)}" title="${esc(e.detail)} · receipt ${esc(e.receipt)}">${esc(text)}</button>`;
  }
  const label = {
    allow: `allowed · ${e.tool}`,
    ask: `waiting for you · ${e.tool} (${(e.detail.match(/A-\d+/) || [''])[0]})`,
    block: `blocked · ${e.tool} — ${e.rule}`,
  }[e.decision];
  return `<button class="ev ${e.decision}" data-receipt="${esc(e.receipt)}" title="${esc(e.detail)} · receipt ${esc(e.receipt)}">${esc(label)}</button>`;
}
function addReply(mine, r) {
  if (r.seen && r.seen !== $('.bubble', mine).textContent) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.innerHTML = `<button class="seen-toggle" type="button">what the model saw</button>`;
    const seen = document.createElement('div');
    seen.className = 'seen';
    seen.hidden = true;
    seen.innerHTML = `<small>Sent to Nemotron — private details replaced before leaving; put back only on your side:</small>${highlightPlaceholders(r.seen)}`;
    meta.firstChild.addEventListener('click', () => { seen.hidden = !seen.hidden; });
    mine.append(meta, seen);
  }
  const el = document.createElement('div');
  el.className = 'msg';
  const calls = r.calls || [];
  const tokens = calls.reduce((a, c) => a + c.tokens, 0);
  const cost = calls.reduce((a, c) => a + c.cost_usd, 0);
  const secs = calls.reduce((a, c) => a + c.latency_ms, 0) / 1000;
  el.innerHTML = `<div class="bubble">${md(r.text)}</div>`
    + ((r.events || []).length ? `<div class="events">${r.events.map(eventChip).join('')}</div>` : '')
    + (calls.length ? `<div class="meta">${esc(modelName(calls[0].model))} · ${calls.length} call${calls.length > 1 ? 's' : ''} · ${tokens.toLocaleString()} tokens · $${cost.toFixed(5)} · ${secs.toFixed(1)} s</div>` : '');
  $('#msgs').append(el);
  el.scrollIntoView({ block: 'end' });
}
function collapseIdeas(collapse) {
  $('#suggest').classList.toggle('collapsed', collapse);
  const t = $('#ideas');
  if (t) t.textContent = collapse ? 'Show ideas to try' : 'Hide ideas';
}
async function loadHistory() {
  const h = await api('/api/history');
  let mine = null;
  for (const t of h.turns || []) {
    if (t.role === 'owner') mine = addMine(t.text);
    else if (mine) { addReply(mine, { text: t.text }); mine = null; }
  }
  if ((h.turns || []).length) collapseIdeas(true);
}
async function send(text) {
  text = text.trim();
  if (!text || busy) return;
  collapseIdeas(true);
  busy = true;
  $('#send').disabled = true;
  const mine = addMine(text);
  const wait = document.createElement('div');
  wait.className = 'typing';
  wait.textContent = 'Kioku is thinking…';
  $('#msgs').append(wait);
  const r = await api('/api/chat', { text });
  wait.remove();
  if (r.error) addReply(mine, { text: `Sorry — ${r.error}` });
  else addReply(mine, r);
  busy = false;
  $('#send').disabled = false;
  $('#input').focus();
  loadState();
  if ((r.events || []).some(e => e.decision === 'ask')) loadApprovals();
  if ((r.events || []).some(e => /check_invoices|paypal_check/.test(e.tool)) && !$('#panel-money').hidden) loadMoney();
  if ((r.events || []).some(e => e.tool === 'save_memory' && e.decision === 'allow')) $('#tree').dataset.loaded = '';
}

// ---------------------------------------------------------------- memory
async function loadMemory(open) {
  const t = await api('/api/memory');
  if (t.error) return;
  $('#tree').innerHTML = t.groups.map(([name, files]) => files.length ? `<details ${['Index', 'Topics'].includes(name) ? 'open' : ''}>
      <summary>${esc(name)} <span>${files.length}</span></summary>
      ${files.map(f => `<button data-note="${esc(f)}">${esc(f.replace(/^\w+\//, '').replace(/\.md$/, ''))}</button>`).join('')}
    </details>` : '').join('');
  $('#tree').dataset.loaded = '1';
  openNote(open || 'index.md');
}
async function openNote(path) {
  const n = await api(`/api/note?path=${encodeURIComponent(path)}`);
  $$('#tree button').forEach(b => b.classList.toggle('on', b.dataset.note === path));
  $('#note').innerHTML = n.error ? `<p class="empty">${esc(n.error)}</p>`
    : n.text ? `<div class="path">${esc(path)}</div>${md(n.text)}` : `<p class="empty">No note at ${esc(path)} yet.</p>`;
  $('#note').scrollTop = 0;
}

// ---------------------------------------------------------------- guard
let guardRows = [];
let guardFilter = 'all';
const WHAT = { model_call: 'model call', tool_call: 'tool call', approval: 'approval' };
async function loadGuard(flash) {
  const g = await api('/api/audit');
  if (g.error) return;
  guardRows = g.rows;
  $('#guard-summary').innerHTML = Object.entries(g.summary).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<span><b>${n}</b> ${esc(k)}</span>`).join('');
  renderGuard(flash);
}
function renderGuard(flash) {
  const keep = {
    all: () => true, block: r => r.decision === 'block', ask: r => r.decision === 'ask',
    redact: r => r.rule === 'privacy.redact', owner: r => r.rule === 'owner.decision',
  }[guardFilter];
  const rows = guardRows.filter(keep).slice(0, 250);
  $('#receipts tbody').innerHTML = rows.map(r => `<tr id="r-${esc(r.id)}" class="${r.id === flash ? 'flash' : ''}">
      <td class="mono">${esc(r.id)}</td><td class="mono">${esc(r.ts.slice(5, 16).replace('T', ' '))}</td>
      <td>${esc(WHAT[r.event] || r.event)}${r.tool ? ` · ${esc(r.tool)}` : r.task ? ` · ${esc(r.task)}` : ''}</td>
      <td><span class="badge ${esc(r.decision)}">${esc(r.decision)}</span></td><td class="mono">${esc(r.rule)}</td><td>${esc(r.reason)}</td></tr>`).join('')
    || `<tr><td colspan="6" class="empty">Nothing here yet.</td></tr>`;
  if (flash) { const row = document.getElementById(`r-${flash}`); if (row) row.scrollIntoView({ block: 'center' }); }
}

// ---------------------------------------------------------------- approvals (the owner's controls)
async function loadApprovals() {
  const a = await api('/api/approvals');
  if (a.error) return;
  const pending = a.items.filter(i => i.status === 'pending');
  $('#pending-count').textContent = pending.length || '';
  const TITLE = { make_payment: 'Payment', send_message: 'Message', create_invoice: 'Invoice · PayPal',
    send_reminder: 'Reminder · PayPal', record_payment: 'Payment received · PayPal' };
  const value = (k, v) => k === 'items' && Array.isArray(v)
    ? v.map(it => `${esc(it.name)} — ${esc(it.quantity)} × ${esc(it.unit_price)}`).join('<br>')
    : esc(typeof v === 'object' ? JSON.stringify(v) : v);
  const card = i => {
    const args = i.arguments || {};
    const pp = /invoice|reminder|record_payment/.test(i.tool);
    const rows = (pp ? [['what', null]] : []).concat(Object.entries(args))
      .map(([k, v]) => k === 'what' && v === null ? `<dt>what</dt><dd><strong>${esc(i.reason)}</strong></dd>` : `<dt>${esc(k)}</dt><dd>${value(k, v)}</dd>`).join('');
    const outcome = i.result ? `<p class="outcome ${esc(i.outcome || '')}">${esc(i.result)}</p>` : '';
    return `<div class="card ${i.status}${pp ? ' pp' : ''}">
      <h3><span class="mono">${esc(i.id)}</span> ${esc(TITLE[i.tool] || i.tool)} <span class="badge ${esc(i.status)}">${esc(i.status)}</span>
        <span class="mono" style="color:var(--muted)">${esc((i.created || '').slice(0, 16).replace('T', ' '))}</span></h3>
      <dl>${rows}</dl>${outcome}
      ${i.status === 'pending' ? `<div class="acts"><button class="approve" data-id="${esc(i.id)}" data-approve="1">Approve${pp ? ' — runs in PayPal' : ''}</button><button class="reject" data-id="${esc(i.id)}" data-approve="0">Reject</button></div>` : ''}
    </div>`;
  };
  $('#approvals').innerHTML = (pending.length ? pending.map(card).join('') : '<p class="empty">Nothing is waiting for you.</p>')
    + (a.items.length > pending.length ? `<h3 style="margin:18px 0 8px;font-size:14px">Decided</h3>` + a.items.filter(i => i.status !== 'pending').map(card).join('') : '');
}
async function decide(id, approve) {
  $$(`#approvals button[data-id="${id}"]`).forEach(b => { b.disabled = true; });
  const r = await api('/api/approvals/decide', { id, approve });
  if (r.error) alert(r.error);
  await loadApprovals();
  loadState();
  if (STATE && STATE.money && STATE.money.on) loadMoney(true);
}

// ---------------------------------------------------------------- money (PayPal)
let invoiceGrid = null;
let CUR = 'JPY';
function fmtMoney(v, cur = CUR) {
  if (v === null || v === undefined || v === '') return '';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur, maximumFractionDigits: cur === 'JPY' ? 0 : 2 }).format(v); }
  catch (e) { return `${v} ${cur}`; }
}
const STATUS = { DRAFT: 'draft', SENT: 'sent', UNPAID: 'sent', SCHEDULED: 'scheduled', PAID: 'paid', MARKED_AS_PAID: 'paid (recorded)',
  PARTIALLY_PAID: 'partly paid', CANCELLED: 'cancelled', REFUNDED: 'refunded', MARKED_AS_REFUNDED: 'refunded' };
function statusPill(r) {
  const cls = r.paid ? 'paid' : r.status === 'CANCELLED' ? 'cancelled' : r.late ? 'late' : 'open';
  const text = (STATUS[r.status] || r.status.toLowerCase()) + (r.late ? ` · ${r.late} day${r.late > 1 ? 's' : ''} late` : '');
  return `<span class="pill-st ${cls}">${esc(text)}</span>`;
}
function invoiceColumns() {
  const money = p => fmtMoney(p.value);
  return [
    { field: 'number', headerName: 'Invoice', width: 132, valueFormatter: p => `#${p.value}`,
      cellRenderer: p => `#${esc(p.value)}${p.data.shared ? ' <span class="shared" title="Shared by everyone trying the demo: read-only">demo</span>' : ''}` },
    { field: 'customer', headerName: 'Customer', flex: 1, minWidth: 110 },
    { field: 'total', headerName: 'Total', type: 'rightAligned', valueFormatter: money, width: 96 },
    { field: 'due', headerName: 'Still due', type: 'rightAligned', valueFormatter: money, width: 96 },
    { field: 'status', headerName: 'PayPal says', width: 166, cellRenderer: p => statusPill(p.data),
      comparator: (a, b, na, nb) => (nb.data.late - na.data.late) || String(a).localeCompare(String(b)) },
    { field: 'due_date', headerName: 'Due', width: 104, headerTooltip: 'Issued dates are in the tooltip of each row', tooltipValueGetter: p => `issued ${p.data.created}` },
    { field: 'items', headerName: 'Items', flex: 1.4, minWidth: 160, tooltipField: 'items' },
    { field: 'url', headerName: '', width: 104, sortable: false,
      cellRenderer: p => p.value ? `<a href="${esc(p.value)}" target="_blank" rel="noopener noreferrer">payer page ↗</a>` : '' },
  ];
}
function plainInvoiceTable(rows) {  // when AG Grid could not load from the CDN
  return `<div class="table-wrap"><table><thead><tr><th>Invoice</th><th>Customer</th><th class="num">Total</th><th class="num">Still due</th><th>PayPal says</th><th>Due</th><th>Items</th></tr></thead><tbody>${
    rows.map(r => `<tr><td class="mono">#${esc(r.number)}</td><td>${esc(r.customer)}</td><td class="num">${esc(fmtMoney(r.total))}</td><td class="num">${esc(fmtMoney(r.due))}</td><td>${statusPill(r)}</td><td class="mono">${esc(r.due_date)}</td><td>${esc(r.items)}</td></tr>`).join('')
    || '<tr><td colspan="7" class="empty">No invoices yet.</td></tr>'}</tbody></table></div>`;
}
function gridTheme() {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const base = agGrid.themeQuartz.withParams({
    fontFamily: 'inherit', fontSize: 13, headerFontWeight: 600, accentColor: v('--accent'), borderColor: v('--line'),
    backgroundColor: v('--panel'), foregroundColor: v('--ink'), headerBackgroundColor: v('--soft'), oddRowBackgroundColor: v('--panel'),
    rowHoverColor: v('--me'), wrapperBorderRadius: 10, spacing: 6,
  });
  return dark && agGrid.colorSchemeDark ? base.withPart(agGrid.colorSchemeDark) : base;
}
function renderInvoices(d) {
  const el = $('#invoice-grid');
  if (!window.agGrid || !agGrid.createGrid) { el.innerHTML = plainInvoiceTable(d.rows); return; }
  if (!invoiceGrid) {
    try { if (agGrid.ModuleRegistry && agGrid.AllCommunityModule) agGrid.ModuleRegistry.registerModules([agGrid.AllCommunityModule]); } catch (e) { /* already registered */ }
    invoiceGrid = agGrid.createGrid(el, {
      theme: gridTheme(), columnDefs: invoiceColumns(), rowData: d.rows, domLayout: 'autoHeight', getRowId: p => p.data.number,
      defaultColDef: { sortable: true, resizable: true, suppressHeaderMenuButton: true },
      rowClassRules: { 'row-late': p => p.data.late > 0, 'row-paid': p => p.data.paid },
      tooltipShowDelay: 300, overlayNoRowsTemplate: '<span class="empty">No invoices yet — ask Kioku to bill a customer.</span>',
    });
  } else {
    invoiceGrid.setGridOption('rowData', d.rows);
  }
}
function renderMoneySummary(d) {
  const sm = d.summary || {};
  const late = d.rows.filter(r => r.late > 0);
  const paid = d.rows.filter(r => r.paid);
  $('#money-sum').innerHTML = [
    [sm.open, `open · ${sm.due} still due`, ''], [late.length, `overdue · ${fmtMoney(late.reduce((a, r) => a + r.due, 0))}`, late.length ? 'late' : ''],
    [paid.length, 'paid', 'paid'], [sm.customers, 'customers in the book', ''],
  ].map(([n, label, cls]) => `<span class="stat ${cls}"><b>${n ?? 0}</b>${esc(label)}</span>`).join('');
  const checked = d.rows.map(r => r.checked).filter(Boolean).sort().pop();
  $('#money-note').textContent = (sm.sandbox ? 'PayPal sandbox — no real money. ' : '')
    + (checked ? `Last read from PayPal: ${checked}` : 'Statuses as last saved — “Read PayPal again” reads them live.');
}
async function loadMoney(force) {
  const d = await api('/api/invoices');
  if (d.error || !d.on) return;
  CUR = d.currency || 'JPY';
  renderMoneySummary(d);
  renderInvoices(d);
  loadCustomers();
}
async function refreshMoney() {
  const b = $('#money-refresh');
  b.disabled = true; b.textContent = 'Reading PayPal…';
  const d = await api('/api/invoices/refresh', {});
  b.disabled = false; b.textContent = 'Read PayPal again';
  if (d.error) { $('#money-note').textContent = d.error; return; }
  renderMoneySummary(d);
  renderInvoices(d);
  loadState();
}
async function loadCustomers(data) {
  const c = data || await api('/api/customers');
  if (c.error) return;
  $('#customers').innerHTML = (c.rows || []).map(x => `<li><strong>${esc(x.name)}</strong> <span class="mono">${esc(x.email)}</span>${x.note ? ` <span class="fine">— ${esc(x.note)}</span>` : ''}</li>`).join('')
    || '<li class="empty">No customers yet.</li>';
}

// ---------------------------------------------------------------- usage
async function loadUsage() {
  const u = await api('/api/usage');
  if (u.error) return;
  $('#usage').innerHTML = `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Model</th><th class="num">Calls</th><th class="num">Tokens</th><th class="num">Cost</th><th class="num">Avg time</th></tr></thead><tbody>
    ${u.rows.map(r => `<tr><td>${esc(r.task.replace(/_/g, ' '))}</td><td>${esc(modelName(r.model))}</td><td class="num">${r.calls}</td><td class="num">${r.tokens.toLocaleString()}</td><td class="num">$${r.cost_usd.toFixed(4)}</td><td class="num">${(r.avg_latency_ms / 1000).toFixed(1)} s</td></tr>`).join('')}
    <tr><td><b>Total</b></td><td></td><td class="num"><b>${u.totals.calls}</b></td><td class="num"><b>${u.totals.tokens.toLocaleString()}</b></td><td class="num"><b>$${u.totals.cost_usd.toFixed(4)}</b></td><td></td></tr>
    </tbody></table></div>` + recallTable(u.recall);
}

// The recall check (demo/eval_recall.py): the same questions with chat on each model, so the routing rests on numbers.
function recallTable(rc) {
  if (!rc || !rc.rows || !rc.rows.length) return '';
  const light = rc.rows.find(r => /lightning/i.test(r.model)), sup = rc.rows.find(r => /super/i.test(r.model));
  const verdict = light && sup && light.correct >= sup.correct && sup.cost_usd > light.cost_usd
    ? `<p class="lead recall-verdict">Lightning answers as well as Super at about 1/${Math.round(sup.cost_usd / light.cost_usd)} of the cost, so everyday chat stays on Lightning.</p>` : '';
  return `<p class="lead recall-lead">Recall check: ${rc.rows[0].questions} questions about this memory, asked on its “today”; an answer counts only if it carries every expected fact.</p>
    <div class="table-wrap"><table><thead><tr><th>Chat model</th><th class="num">Correct</th><th class="num">Cost for all</th><th class="num">Avg time</th></tr></thead><tbody>
    ${rc.rows.map(r => `<tr><td>${esc(modelName(r.model))}</td><td class="num">${r.correct} / ${r.questions}</td><td class="num">$${r.cost_usd.toFixed(4)}</td><td class="num">${(Math.round(r.avg_latency_s * 10) / 10).toFixed(1)} s</td></tr>`).join('')}
    </tbody></table></div>${verdict}`;
}

// ---------------------------------------------------------------- daily rhythm
async function runJob(job, btn) {
  $$('.run button').forEach(b => { b.disabled = true; });
  btn.textContent = btn.textContent.replace(/…$/, '') + '…';
  $('#run-out').innerHTML = '<p class="typing">Working… (a review on Super can take half a minute)</p>';
  const r = await api('/api/run', { job });
  $$('.run button').forEach(b => { b.disabled = false; b.textContent = b.textContent.replace(/…$/, ''); });
  $('#run-out').innerHTML = r.error ? `<p class="empty">Sorry — ${esc(r.error)}</p>`
    : r.path ? `<div class="path">${esc(r.path)}</div>${md(r.text)}` : `<p class="empty">${esc(r.note || 'Nothing written.')}</p>`;
  $('#tree').dataset.loaded = '';
  loadState();
}

// ---------------------------------------------------------------- wiring
document.addEventListener('click', e => {
  const t = e.target.closest('button, a');
  if (!t) return;
  if (t.matches('.tabs button')) return showTab(t.dataset.tab);
  if (t.id === 'ideas') return collapseIdeas(!$('#suggest').classList.contains('collapsed'));
  if (t.matches('.suggest button')) { $('#input').value = t.dataset.text; return send(t.dataset.text).then(() => { $('#input').value = ''; }); }
  if (t.dataset.note) { e.preventDefault(); showTab('memory'); if ($('#tree').dataset.loaded) return openNote(t.dataset.note); return loadMemory(t.dataset.note); }
  if (t.matches('.ev')) { showTab('guard'); guardFilter = 'all'; $$('#guard-filters button').forEach(b => b.classList.toggle('on', b.dataset.f === 'all')); return loadGuard(t.dataset.receipt); }
  if (t.matches('#guard-filters button')) { guardFilter = t.dataset.f; $$('#guard-filters button').forEach(b => b.classList.toggle('on', b === t)); return renderGuard(); }
  if (t.matches('.approve, .reject')) return decide(t.dataset.id, t.dataset.approve === '1');
  if (t.matches('.run button')) return runJob(t.dataset.job, t);
  if (t.id === 'money-refresh') return refreshMoney();
  if (t.id === 'reset') { e.preventDefault(); if (confirm('Start over with a fresh copy of the demo?')) api('/api/reset', {}).then(() => location.reload()); }
});
$('#composer').addEventListener('submit', e => { e.preventDefault(); const v = $('#input').value; $('#input').value = ''; send(v); });
$('#add-customer').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  const r = await api('/api/customers', { name: f.name.value, email: f.email.value, note: f.note.value });
  if (r.error) { alert(r.error); return; }
  f.reset();
  loadCustomers(r);
  loadState();
});
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); }
});

(async () => {
  await loadState();
  intro();
  await loadHistory();
  let tab = 'memory';
  try { tab = localStorage.getItem('kioku-tab') || 'memory'; } catch (e) { /* private mode */ }
  if (tab === 'chat' && !matchMedia('(max-width: 960px)').matches) tab = 'memory';
  showTab(tab);
  loadApprovals();
})();
