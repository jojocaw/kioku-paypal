import pytest

from kioku.guard import Blocked, Redactor, Vault, check_tool_rules, mask_secrets
from kioku.llm import ToolCall


def redact(text, terms=('Aiko Tanaka', 'Ren', '花子')):
    r = Redactor(terms)
    v = Vault()
    out, found = r.redact(text, v)
    return out, found, r, v


@pytest.mark.parametrize('text,kind', [
    ('mail me at mika.sato@example.com please', 'EMAIL'),
    ('call 090-1234-5678 after six', 'PHONE'),
    ('call 03-1234-5678 after six', 'PHONE'),
    ('call +81 90 1234 5678 after six', 'PHONE'),
    ('call (555) 123-4567 after six', 'PHONE'),
    ('card 4111 1111 1111 1111 exp 12/29', 'CARD'),
    ('my api_key: sk-abcdefghijklmnop1234 ok', 'SECRET'),
    ('wifi password: hunter2hunter2', 'SECRET'),
    ('ship to 〒150-0001', 'POSTCODE'),
    ('Aiko Tanaka was sick today', 'NAME'),
    ('花子の誕生日', 'NAME'),
])
def test_personal_data_is_replaced(text, kind):
    out, found, _, _ = redact(text)
    assert found[kind] == 1
    assert f'[{kind}_1]' in out


@pytest.mark.parametrize('text', [
    'sold 412 items on 2026-10-05 at 12:30',
    'flour went up 8% to ¥6,800 per 25kg bag',
    'invoice MF-2609-118 is due 2026/10/31',
    'card-like but invalid 4111 1111 1111 1112',
    'Renovation starts Monday',  # "Ren" is a private name, but only as a whole word
])
def test_ordinary_text_is_left_alone(text):
    out, found, _, _ = redact(text)
    assert out == text and not found


def test_restore_round_trip_and_same_value_same_placeholder():
    text = 'Aiko Tanaka (aiko@example.com) said hi. Ask Aiko Tanaka again.'
    out, _, r, v = redact(text)
    assert out == '[NAME_1] ([EMAIL_1]) said hi. Ask [NAME_1] again.'
    assert r.restore(out, v) == text
    assert r.restore('unknown [EMAIL_9] stays', v) == 'unknown [EMAIL_9] stays'
    assert r.restore('NAME_1 wrote from EMAIL_1; NAME_7 is unknown', v) == 'Aiko Tanaka wrote from aiko@example.com; NAME_7 is unknown'


def call(name, **args):
    return ToolCall('c1', name, args)


def test_tool_rules():
    assert check_tool_rules(call('search_memory', query='flour'))[0] == 'allow'
    assert check_tool_rules(call('disable_guard'))[:2] == ('block', 'tools.unknown')
    assert check_tool_rules(call('save_memory', text='door code is [SECRET_1]'))[:2] == ('block', 'memory.no_secrets')
    assert check_tool_rules(call('save_memory', text='token=abcdefgh123'))[:2] == ('block', 'memory.no_secrets')
    assert check_tool_rules(call('save_memory', text='Ren likes melon bread'))[0] == 'allow'
    assert check_tool_rules(call('save_memory', text='card [CARD_1] for the oven'))[:2] == ('block', 'memory.no_secrets')
    assert check_tool_rules(call('read_note', path='../../.env'))[:2] == ('block', 'memory.path')
    assert check_tool_rules(call('read_note', path='/etc/passwd'))[:2] == ('block', 'memory.path')
    action, rule, reason = check_tool_rules(call('make_payment', what='flour', amount=120, currency='USD', payee='Mill Co'))
    assert (action, rule) == ('ask', 'spend.ask_owner') and '120' in reason and 'flour' in reason and 'Mill Co' in reason
    assert check_tool_rules(call('make_payment', what='oven', amount=5000, payee='X'))[:2] == ('block', 'spend.limit')
    assert check_tool_rules(call('make_payment', what='oven repair', amount=48000, currency='JPY', payee='Sato'), 100000)[0] == 'ask'
    assert check_tool_rules(call('make_payment', what='gift cards', amount=150, payee='support desk'))[:2] == ('block', 'spend.scam_pattern')
    assert check_tool_rules(call('make_payment', what='x', amount='lots', payee='X'))[:2] == ('block', 'spend.bad_amount')
    assert check_tool_rules(call('send_message', to='[EMAIL_1]', body='hi'))[:2] == ('ask', 'outbound.ask_owner')
    assert check_tool_rules(ToolCall('c', 'add_task', {'_unparsed': '{bad'}))[:2] == ('block', 'tools.bad_arguments')


def test_budget_hard_stop_writes_a_receipt(make_kioku):
    k, fake = make_kioku('never used', daily_token_budget=10_000)
    k.ledger.record(ts=k.clock().isoformat(), task='chat', model='m', ok=True,
                    prompt_tokens=9_000, completion_tokens=900, cost_usd=0.001, latency_ms=1)
    with pytest.raises(Blocked) as e:
        k.guard.before_model([{'role': 'user', 'content': 'hello'}], k.vault, 'chat', 500)
    assert e.value.rule == 'budget.daily'
    last = k.audit.rows(1)[0]
    assert last['decision'] == 'block' and last['rule'] == 'budget.daily' and last['id'] == e.value.receipt


def test_earlier_tool_call_arguments_are_redacted_too(make_kioku):
    k, _ = make_kioku()
    msgs = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'c1', 'type': 'function', 'function': {'name': 'send_message', 'arguments': '{"to": "aiko@example.com"}'}}]}]
    safe = k.guard.before_model(msgs, k.vault, 'chat', 100)
    assert 'aiko@example.com' not in str(safe) and '[EMAIL_1]' in str(safe)
    assert msgs[0]['tool_calls'][0]['function']['arguments'] == '{"to": "aiko@example.com"}'  # the owner's copy is untouched


def test_secrets_never_reach_disk():
    assert mask_secrets('wifi password: hunter2hunter2, card 4111 1111 1111 1111.') == \
        'wifi password: [withheld], card [card-withheld].'
    assert mask_secrets('sold 412 on 2026-10-05, invalid 4111 1111 1111 1112') == 'sold 412 on 2026-10-05, invalid 4111 1111 1111 1112'
    assert mask_secrets('wifi password: [withheld], ok') == 'wifi password: [withheld], ok'  # masking twice changes nothing
    assert mask_secrets('the wifi password is komorebi-2017-bread.') == 'the wifi password is [withheld].'
    _, _, r, v = redact('password: hunter2hunter2 for Aiko Tanaka')
    assert r.restore('[SECRET_1] / [NAME_1]', v) == 'hunter2hunter2 / Aiko Tanaka'           # the owner's screen
    assert r.restore('[SECRET_1] / [NAME_1]', v, withhold=True) == '[withheld] / Aiko Tanaka'  # the owner's files


def test_receipts_use_the_kioku_clock(make_kioku):
    k, _ = make_kioku()
    receipt = k.audit.log('test', 'allow', 'x', 'y')
    assert k.audit.rows(1)[0]['ts'].startswith('2026-10-05T09:30') and receipt == 'R-00001'
    aid = k.approvals.request('make_payment', {}, 'r', receipt)
    assert k.approvals.list()[0]['created'].startswith('2026-10-05T09:30') and aid == 'A-001'


@pytest.mark.parametrize('text,done,expected', [
    ("I've saved that to your notes.", set(), ['save_memory']),
    ("I've saved that to your notes.", {'save_memory'}, []),
    ("Noted — 205 croissants today.", set(), []),                       # an acknowledgement, not a claim
    ("Saved: butter stock 18 blocks.", set(), ['save_memory']),
    ("Got it — alarm code is 4721. I'll keep it in mind.", set(), ['save_memory']),
    ("Got it. Saved to the lemon-bun topic.", set(), ['save_memory']),
    ("OK. I'll remind you next August.", {'save_memory'}, ['add_task']),
    ("The email has been sent to Mill Co.", {'send_message'}, ['send_message']),  # never done inside a turn
    ("Queued for your approval as A-004; once you approve, it goes out.", {'queue'}, []),
    ("I must wait for your approval before any message is sent.", set(), []),
    ("Got it — alarm code saved as 4721-komorebi.", set(), ['save_memory']),
    ("With the discount you saved as much as ¥3,800 a month.", set(), []),
    ("Queued as payment A-010 — waiting for your approval in the panel.", set(), ['queue']),
    ("The email to Mill Co has been queued for your approval (A-009).", {'queue'}, []),
    ("Any payment like this will wait for your approval.", set(), []),
    ("Payment queued for your approval (A-010).", set(), ['queue']),
    ("This request will be queued for your approval in the approvals panel.", set(), ['queue']),
    ("If you approve something there, I'll proceed; otherwise it stays queued.", set(), []),
    ("If you ask me to pay, I'll queue it for your approval.", set(), []),
    ("Payment was made to Sato.", set(), ['make_payment']),
    ("I can't approve it. I'll note that task A-001 is marked done.", set(), ['complete_task']),
    ("I've marked the oven check as done.", {'complete_task'}, []),
    ("I can't mark approvals as done — only you can decide them.", set(), []),
    ("I've recorded ¥9,000 from Ikoi Nursery.", set(), ['record_payment']),       # a payment, not a note in memory
    ("I've recorded ¥6,800 for the flour in your notes.", set(), ['save_memory']),
])
def test_unbacked_claims(text, done, expected):
    from kioku.guard import unbacked_claims
    assert unbacked_claims(text, done) == expected


def test_a_withheld_marker_is_not_hidden_again():
    out, found, _, _ = redact('wifi password: [withheld] (changed in July)')
    assert out == 'wifi password: [withheld] (changed in July)' and not found
