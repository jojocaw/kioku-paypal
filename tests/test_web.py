import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from conftest import FakeLLM
from kioku import web
from kioku.config import Settings


@pytest.fixture
def demo(tmp_path):
    """A demo server over a tiny fictional seed; every sandbox gets a scripted model."""
    seed = tmp_path / 'seed'
    (seed / 'memory' / 'daily').mkdir(parents=True)
    (seed / 'memory' / 'index.md').write_text('# Index\n\n## About the owner\n- Runs a bakery\n')
    (seed / 'memory' / 'daily' / '2026-10-04.md').write_text('# 2026-10-04\n\n## Key points\n- 412 croissants\n')
    (seed / 'persona.json').write_text(json.dumps(dict(
        name='Rin', business='Komorebi Bakery', started='2026-07-06', now='2026-10-05T09:00', timezone='Asia/Tokyo',
        private_terms=['Aiko'], max_payment=100000, suggestions=[])))
    settings = Settings(api_key='test-key', data_dir=tmp_path / 'unused')
    app = web.App(settings, seed, daily_usd=1.0, sandbox_root=tmp_path / 'sandboxes')
    scripted = []
    original = app._new_sandbox

    def sandbox_with_fake_model(sid):
        space = original(sid)
        space.k.llm = FakeLLM(*scripted)
        return space

    app._new_sandbox = sandbox_with_fake_model
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, f'http://127.0.0.1:{server.server_address[1]}', scripted
    server.shutdown()


def call(base, path, body=None, cookie=None, ctype='application/json'):
    data = None if body is None else (json.dumps(body).encode() if ctype == 'application/json' else body.encode())
    req = urllib.request.Request(base + path, data=data, method='POST' if data is not None else 'GET')
    if data is not None:
        req.add_header('Content-Type', ctype)
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b'{}'), r.headers.get('Set-Cookie')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}'), None


def first_chat(base, scripted, reply='Hello Rin.'):
    """The first change a visitor makes creates their sandbox; returns the cookie."""
    scripted.append(reply)
    code, r, set_cookie = call(base, '/api/chat', {'text': 'Ask Aiko about Sunday'})
    assert code == 200 and set_cookie and 'HttpOnly' in set_cookie and 'SameSite=Lax' in set_cookie
    return set_cookie.split(';')[0], r


def test_looking_is_free_and_the_first_change_makes_a_private_copy(demo):
    app, base, scripted = demo
    code, state, set_cookie = call(base, '/api/state')
    assert code == 200 and state['mode'] == 'demo' and set_cookie is None and app.spaces == {}   # no copy yet
    assert call(base, '/api/note?path=daily/2026-10-04.md')[1]['text'].startswith('# 2026-10-04')
    cookie, r = first_chat(base, scripted)
    assert r['text'] == 'Hello Rin.' and r['seen'] == 'Ask [NAME_1] about Sunday'
    _, mine, _ = call(base, '/api/state', cookie=cookie)
    _, other, _ = call(base, '/api/state')
    assert mine['counts']['receipts'] > 0 and other['counts']['receipts'] == 0 and len(app.spaces) == 1
    assert mine['now'].startswith('2026-10-05T09:0')
    assert not (app.seed / 'audit.jsonl').exists()                           # the seed itself was never written


def test_the_server_refuses_what_it_should(demo):
    app, base, scripted = demo
    assert call(base, '/api/note?path=../persona.json')[0] == 400
    assert call(base, '/static/../web.py')[0] == 404
    assert call(base, '/api/chat', 'text=hi', ctype='application/x-www-form-urlencoded')[0] == 400
    cookie, _ = first_chat(base, scripted)
    assert call(base, '/api/chat', {'text': 'x' * (web.MAX_TEXT + 1)}, cookie)[0] == 400
    assert call(base, '/api/approvals/decide', {'id': 'A-999', 'approve': True}, cookie)[0] == 400
    assert call(base, '/api/run', {'job': 'rm -rf'}, cookie)[0] == 400


def test_keep_awake_visits_until_the_last_day():
    from datetime import date, timedelta
    day, visits = [date(2026, 12, 14)], []
    def sleep(_):
        day[0] += timedelta(days=1)  # one visit a day in this fake clock
    web.keep_awake('https://demo.example/healthz', date(2026, 12, 15), today=lambda: day[0], sleep=sleep,
                   visit=visits.append)
    assert visits == ['https://demo.example/healthz'] * 2 and day[0] == date(2026, 12, 16)
    failing = []
    def boom(url):
        failing.append(url)
        raise OSError('host asleep')
    web.keep_awake('https://demo.example/healthz', date(2026, 12, 14), today=lambda: day[0] - timedelta(days=2),
                   sleep=lambda _: day.__setitem__(0, day[0] + timedelta(days=5)), visit=boom)
    assert len(failing) == 1  # an error is written down and the loop goes on until the last day


def test_head_requests_get_headers_only(demo):
    app, base, scripted = demo
    for path in ('/healthz', '/'):
        with urllib.request.urlopen(urllib.request.Request(base + path, method='HEAD')) as r:
            assert r.status == 200 and r.read() == b'' and int(r.headers['Content-Length']) > 0


def test_the_recall_check_is_shown_but_not_copied_into_sandboxes(tmp_path, demo):
    app, base, scripted = demo
    assert call(base, '/api/usage')[1]['recall'] is None  # no results published for this seed
    app.recall = {'rows': [{'model': 'nvidia/Nemotron-3_5-Lightning', 'questions': 22, 'correct': 21,
                            'cost_usd': 0.0075, 'avg_latency_s': 2.0}]}
    assert call(base, '/api/usage')[1]['recall']['rows'][0]['correct'] == 21
    (app.seed / 'recall.json').write_text('{}')
    cookie, _ = first_chat(base, scripted)
    folder = app.spaces[cookie.split('=', 1)[1]].folder
    assert not (folder / 'recall.json').exists() and not (folder / 'persona.json').exists()


def test_limits(demo):
    app, base, scripted = demo
    cookie, _ = first_chat(base, scripted)
    space = next(iter(app.spaces.values()))
    space.chats.extend([time.time()] * web.CHATS_PER_HOUR)
    code, r, _ = call(base, '/api/chat', {'text': 'hi'}, cookie)
    assert code == 429 and 'an hour' in r['error']
    space.chats.clear()
    app.meter.add(5.0)                                                       # everyone's budget for today is gone
    code, r, _ = call(base, '/api/chat', {'text': 'hi'}, cookie)
    assert code == 503 and 'budget' in r['error']
    app.new_by_ip['127.0.0.1'].extend([time.time()] * web.NEW_PER_IP_HOUR)   # one network opening many sessions
    code, r, _ = call(base, '/api/chat', {'text': 'hi'})
    assert code == 429 and 'network' in r['error']


def test_approvals_are_decided_by_the_page_not_the_model(demo):
    app, base, scripted = demo
    cookie, _ = first_chat(base, scripted)
    space = next(iter(app.spaces.values()))
    space.k.llm.responses += [('', [('make_payment', {'what': 'flour', 'amount': 6664, 'currency': 'JPY', 'payee': 'Mill Co'})]),
                              'Queued for your approval.']
    code, r, _ = call(base, '/api/chat', {'text': 'Pay Mill Co for the flour'}, cookie)
    assert [e['decision'] for e in r['events']] == ['ask']
    _, a, _ = call(base, '/api/approvals', cookie=cookie)
    assert a['items'][0]['status'] == 'pending'
    code, d, _ = call(base, '/api/approvals/decide', {'id': a['items'][0]['id'], 'approve': True}, cookie)
    assert code == 200 and 'carried out' in d['result']
    assert (space.folder / 'payments.jsonl').exists()


# ---------------------------------------------------------------- money (PayPal)
@pytest.fixture
def shop(tmp_path):
    """A demo server whose seed has a customer book and one shared invoice, with a stand-in for PayPal."""
    from test_money import FakePayPal
    seed = tmp_path / 'seed'
    (seed / 'memory').mkdir(parents=True)
    (seed / 'memory' / 'index.md').write_text('# Index\n')
    (seed / 'persona.json').write_text(json.dumps(dict(
        name='Rin', business='Komorebi Bakery', started='2026-07-06', now='2026-10-05T09:00', timezone='Asia/Tokyo',
        private_terms=[], max_payment=100000, suggestions=[])))
    pp = FakePayPal()
    from datetime import date
    iid = pp.create_invoice(business_name='Hotel Sakura', email='kitchen@hotel-sakura.example.com', currency='JPY', due=date(2026, 9, 30),
                            items=[{'name': 'Croissant', 'quantity': 120, 'unit_price': 260}])
    pp.send_invoice(iid)
    from kioku.paypal import summary
    (seed / 'invoices.json').write_text(json.dumps([{**summary(pp.get_invoice(iid)), 'customer': 'Hotel Sakura', 'items': [],
                                                     'created': '2026-09-16T17:00:00+09:00', 'shared': True, 'checked': ''}]))
    (seed / 'customers.json').write_text(json.dumps([{'name': 'Hotel Sakura', 'email': 'kitchen@hotel-sakura.example.com', 'note': ''},
                                                     {'name': 'Café Hana', 'email': 'orders@cafe-hana.example.com', 'note': ''}]))
    app = web.App(Settings(api_key='test-key', data_dir=tmp_path / 'unused'), seed, daily_usd=1.0,
                  sandbox_root=tmp_path / 'sandboxes', paypal=pp)
    scripted = []
    original = app._new_sandbox

    def sandbox_with_fake_model(sid):
        space = original(sid)
        space.k.llm = FakeLLM(*scripted)
        return space

    app._new_sandbox = sandbox_with_fake_model
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, f'http://127.0.0.1:{server.server_address[1]}', scripted, pp
    server.shutdown()


def test_the_money_tab_reads_the_shared_invoices_without_a_sandbox(shop):
    app, base, scripted, pp = shop
    code, d, _ = call(base, '/api/invoices')
    assert code == 200 and d['on'] and d['summary']['overdue'] == 1 and d['summary']['open'] == 1
    [row] = d['rows']
    assert row['customer'] == 'Hotel Sakura' and row['late'] == 5 and row['shared'] and row['total'] == 31200
    code, st, _ = call(base, '/api/state')
    assert st['money']['on'] and st['money']['due'] == '¥31,200'


def test_only_reserved_addresses_can_be_added_to_the_demo_customer_book(shop):
    app, base, scripted, pp = shop
    code, r, set_cookie = call(base, '/api/customers', {'name': 'Bistro Kaze', 'email': 'orders@bistro-kaze.example.com', 'note': 'new'})
    assert code == 200 and 'Bistro Kaze' in [c['name'] for c in r['rows']]
    code, r, _ = call(base, '/api/customers', {'name': 'Real Person', 'email': 'someone@gmail.com'}, set_cookie.split(';')[0])
    assert code == 400 and 'reserved address' in r['error'] and len(app.spaces) == 1
    space = next(iter(app.spaces.values()))
    assert any(row['rule'] == 'owner.customer_added' for row in space.k.audit.rows())


def test_a_visitor_cannot_remind_on_a_shared_demo_invoice(shop):
    app, base, scripted, pp = shop
    scripted += [('', [('send_reminder', {'invoice': 'Hotel Sakura'})]), 'That invoice is shared in the demo.']
    code, r, _ = call(base, '/api/chat', {'text': 'Send Hotel Sakura a reminder.'})
    assert code == 200 and any(e['rule'] == 'demo.shared_invoice' for e in r['events'])
    assert 'remind' not in pp.calls


def test_approving_an_invoice_runs_it_in_paypal_and_the_demo_caps_paypal_actions(shop, monkeypatch):
    app, base, scripted, pp = shop
    monkeypatch.setattr(web, 'PAYPAL_PER_HOUR', 1)
    bread = [{'name': 'Baguette', 'quantity': 40, 'unit_price': 320}]
    scripted += [('', [('create_invoice', {'customer': 'Café Hana', 'items': bread})]), 'Waiting for your approval.',
                 ('', [('create_invoice', {'customer': 'Café Hana', 'items': bread * 1 + [{'name': 'Loaf', 'quantity': 1, 'unit_price': 680}]})]),
                 'Waiting for your approval.']
    code, r, set_cookie = call(base, '/api/chat', {'text': 'Bill Café Hana for 40 baguettes at 320.'})
    cookie = set_cookie.split(';')[0]
    code, r, _ = call(base, '/api/approvals/decide', {'id': 'A-001', 'approve': True}, cookie)
    assert code == 200 and 'Invoice #0002 sent to Café Hana' in r['result']
    code, d, _ = call(base, '/api/invoices', cookie=cookie)
    assert [x['number'] for x in d['rows']][:1] == ['0002']
    call(base, '/api/chat', {'text': 'And bill Café Hana for 40 baguettes and a loaf.'}, cookie)
    code, r, _ = call(base, '/api/approvals/decide', {'id': 'A-002', 'approve': True}, cookie)
    assert code == 429 and 'PayPal actions an hour' in r['error']
    code, a, _ = call(base, '/api/approvals', cookie=cookie)
    assert [i['status'] for i in a['items'] if i['id'] == 'A-002'] == ['pending']  # still waiting, nothing ran


def test_read_paypal_again_needs_paypal(demo):
    app, base, scripted = demo
    code, r, _ = call(base, '/api/invoices/refresh', {})
    assert code == 400 and 'not connected' in r['error']


def test_the_page_may_load_the_invoice_grid_from_the_cdn_only(demo):
    app, base, scripted = demo
    with urllib.request.urlopen(base + '/') as r:
        csp = r.headers['Content-Security-Policy']
    assert "script-src 'self' https://cdn.jsdelivr.net" in csp and "default-src 'self'" in csp
