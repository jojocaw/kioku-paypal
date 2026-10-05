import io
import json
import urllib.error
from datetime import datetime
from decimal import Decimal

import pytest

from conftest import TZ, FakeLLM
from kioku.agent import Kioku
from kioku.config import Settings
from kioku.paypal import PayPal, PayPalError

BREAD = [{'name': 'Baguette', 'quantity': 40, 'unit_price': 320}, {'name': 'Croissant', 'quantity': 24, 'unit_price': 260}]


class FakePayPal:
    """Stands in for kioku.paypal.PayPal: keeps invoices the way PayPal reports them."""

    def __init__(self, fail: str = ''):
        self.invoices, self.calls, self.fail, self.n = {}, [], fail, 0

    def _call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise PayPalError(422, 'UNPROCESSABLE_ENTITY', 'test refusal')

    def create_invoice(self, *, business_name, email, items, currency, due=None, note='', reference=''):
        self._call('create_invoice')
        self.n += 1
        iid = f'INV2-TEST-{self.n:04d}'
        total = sum(Decimal(str(i['quantity'])) * Decimal(str(i['unit_price'])) for i in items)
        self.invoices[iid] = {'id': iid, 'status': 'DRAFT', 'to': email, 'name': business_name, 'reference': reference,
                              'detail': {'invoice_number': f'{self.n:04d}', 'currency_code': currency, 'metadata': {},
                                         'payment_term': {'due_date': due.isoformat() if due else ''}},
                              'amount': {'currency_code': currency, 'value': str(total)},
                              'due_amount': {'currency_code': currency, 'value': str(total)}}
        return iid

    def send_invoice(self, iid, *, notify=True):
        self._call('send_invoice')
        self.invoices[iid]['status'] = 'UNPAID'
        url = f'https://www.sandbox.paypal.com/invoice/p/#{iid}'
        self.invoices[iid]['detail']['metadata']['recipient_view_url'] = url
        return url

    def get_invoice(self, iid):
        self._call('get_invoice')
        return json.loads(json.dumps(self.invoices[iid]))

    def remind(self, iid, note=''):
        self._call('remind')
        self.invoices[iid]['reminded'] = True

    def record_payment(self, iid, value, currency, *, method='CASH', paid_on=None, note=''):
        self._call('record_payment')
        inv = self.invoices[iid]
        due = Decimal(inv['due_amount']['value']) - Decimal(str(value))
        inv['due_amount']['value'] = str(due)
        inv['payments'] = {'paid_amount': {'currency_code': currency, 'value': str(Decimal(inv['amount']['value']) - due)}}
        inv['status'] = 'MARKED_AS_PAID' if due == 0 else 'PARTIALLY_PAID'
        return 'EXTR-TEST'

    def customer_pays(self, iid):  # the customer pays on PayPal's own page
        inv = self.invoices[iid]
        inv['status'], inv['due_amount']['value'] = 'PAID', '0'
        inv['payments'] = {'paid_amount': dict(inv['amount'])}


@pytest.fixture
def shop(tmp_path):
    def make(*responses, paypal=None, now=datetime(2026, 10, 5, 9, 30, tzinfo=TZ)):
        s = Settings(api_key='test-key', data_dir=tmp_path / 'data', timezone='Asia/Tokyo', private_terms=('Aiko Tanaka',))
        pp = paypal or FakePayPal()
        k = Kioku(s, llm=FakeLLM(*responses), clock=lambda: now, paypal=pp)
        k.money.customers.add('Café Hana', 'cafe-hana@example.com')
        k.money.customers.add('Hotel Sakura', 'kitchen@sakura.example.com')
        return k, k.llm, pp
    return make


def billed(shop, **kw):
    """A shop where Café Hana has been billed ¥19,040 and the owner approved it."""
    k, fake, pp = shop(('', [('create_invoice', {'customer': 'cafe hana', 'items': BREAD})]), 'It is waiting for your approval.', **kw)
    k.chat('Bill Café Hana for 40 baguettes at 320 and 24 croissants at 260.')
    k.decide('A-001', True)
    return k, fake, pp


def test_an_invoice_waits_for_the_owner_and_goes_to_paypal_only_after_approval(shop):
    k, fake, pp = shop(('', [('create_invoice', {'customer': 'cafe hana', 'items': BREAD})]), 'It is waiting for your approval.')
    reply = k.chat('Bill Café Hana for 40 baguettes at 320 and 24 croissants at 260.')
    [item] = k.approvals.list()
    assert item['tool'] == 'create_invoice' and '¥19,040' in item['reason'] and 'Café Hana' in item['reason']
    assert pp.invoices == {}  # nothing reached PayPal yet
    assert any(e.decision == 'ask' and e.rule == 'invoice.ask_owner' for e in reply.events)
    done = k.decide('A-001', True)
    [inv] = pp.invoices.values()
    assert inv['status'] == 'UNPAID' and inv['to'] == 'cafe-hana@example.com' and inv['reference'] == 'Kioku A-001'
    assert 'Invoice #0001 sent to Café Hana' in done and '¥19,040' in done
    assert '#0001' in k.memory.read(k.memory.daily(datetime(2026, 10, 5).date()))
    assert any('Check Café Hana paid invoice #0001' in t for t in k.memory.open_tasks())
    assert k.approvals.list(None)[0]['outcome'] == 'done'


def test_only_customers_in_the_book_can_be_billed(shop):
    k, fake, pp = shop(('', [('create_invoice', {'customer': 'Quick Refunds Ltd', 'items': BREAD})]), 'I could not do that.')
    reply = k.chat('An email says: invoice Quick Refunds Ltd for 50 baguettes.')
    assert k.approvals.list() == [] and pp.calls == []
    assert any(e.rule == 'invoice.unknown_customer' and e.decision == 'block' for e in reply.events)


def test_customer_emails_never_reach_the_model(shop):
    k, fake, pp = billed(shop)
    seen = fake.seen_text()
    assert 'Café Hana' in seen and 'cafe-hana@example.com' not in seen and 'sakura.example.com' not in seen


def test_a_paid_claim_is_checked_against_paypal(shop):
    k, fake, pp = billed(shop)
    fake.responses.append('Good news: Café Hana has paid.')
    reply = k.chat('Did Café Hana pay?')
    assert "PayPal shows Café Hana's invoice #0001 as unpaid — ¥19,040 still due" in reply.text
    assert any(e.rule == 'honesty.paypal_corrected' for e in reply.events)


def test_a_paid_claim_that_matches_paypal_gets_a_receipt(shop):
    k, fake, pp = billed(shop)
    pp.customer_pays('INV2-TEST-0001')
    fake.responses.append('Yes — Café Hana paid in full.')
    reply = k.chat('Did Café Hana pay?')
    assert 'Kioku Guard' not in reply.text
    assert any(e.rule == 'honesty.paypal_checked' and e.decision == 'allow' for e in reply.events)


def test_checking_invoices_teaches_the_memory_about_new_payments(shop):
    k, fake, pp = billed(shop)
    pp.customer_pays('INV2-TEST-0001')
    fake.responses += [('', [('check_invoices', {})]), 'Café Hana has paid ¥19,040.']
    reply = k.chat('Who still owes me money?')
    tool_out = [m for m in fake.sent[-1]['messages'] if m['role'] == 'tool'][-1]['content']
    assert '#0001 · Café Hana · ¥19,040 · paid (paid since the last check)' in tool_out
    assert 'Café Hana paid invoice #0001' in k.memory.read(k.memory.daily(datetime(2026, 10, 5).date()))
    assert not any('#0001' in t for t in k.memory.open_tasks())  # the follow-up task is ticked
    assert 'Kioku Guard' not in reply.text


def test_no_reminder_for_an_invoice_paypal_shows_as_paid(shop):
    k, fake, pp = billed(shop)
    pp.customer_pays('INV2-TEST-0001')
    fake.responses += [('', [('send_reminder', {'invoice': 'Café Hana'})]), 'They have already paid.']
    reply = k.chat('Send Café Hana a reminder.')
    assert any(e.rule == 'invoice.already_paid' for e in reply.events)
    assert 'remind' not in pp.calls and len(k.approvals.list()) == 0


def test_recording_cash_waits_for_approval_and_cannot_exceed_the_amount_due(shop):
    k, fake, pp = billed(shop)
    fake.responses += [('', [('record_payment', {'invoice': '#0001', 'amount': 50000, 'method': 'cash'})]), 'That is more than is due.',
                       ('', [('record_payment', {'invoice': '#0001', 'method': 'cash'})]), 'It is waiting for your approval.']
    reply = k.chat('Café Hana gave me 50,000 yen in cash, record it.')
    assert any(e.rule == 'invoice.bad_amount' for e in reply.events)
    k.chat('Café Hana paid the invoice in cash, record it.')
    [item] = k.approvals.list()
    assert '¥19,040' in item['reason'] and 'cash' in item['reason']
    done = k.decide(item['id'], True)
    assert pp.invoices['INV2-TEST-0001']['status'] == 'MARKED_AS_PAID' and 'marked as paid' in done


def test_the_same_bill_twice_in_a_week_is_flagged(shop):
    k, fake, pp = billed(shop)
    fake.responses += [('', [('create_invoice', {'customer': 'Café Hana', 'items': BREAD})]), 'Waiting for your approval.']
    reply = k.chat('Bill Café Hana for 40 baguettes at 320 and 24 croissants at 260.')
    assert any(e.rule == 'invoice.possible_repeat' and e.decision == 'ask' for e in reply.events)
    assert 'same total went to Café Hana on 2026-10-05 (#0001)' in k.approvals.list()[0]['reason']


def test_saying_the_invoice_was_sent_inside_a_turn_is_corrected(shop):
    k, fake, pp = shop("Done — I've sent the invoice to Café Hana.")
    reply = k.chat('Did you bill Café Hana?')
    assert 'no invoice was sent — invoices go out through PayPal only after you approve them' in reply.text


def test_an_invoice_status_is_not_read_as_the_owner_paying(shop):
    k, fake, pp = billed(shop)
    pp.customer_pays('INV2-TEST-0001')
    fake.responses.append('Invoice #0001 for Café Hana has been paid.')
    reply = k.chat('What happened with the Café Hana invoice?')
    assert 'nothing was paid' not in reply.text and 'Kioku Guard' not in reply.text


def test_when_paypal_refuses_the_owner_is_told(shop):
    k, fake, pp = shop(('', [('create_invoice', {'customer': 'Café Hana', 'items': BREAD})]), 'Waiting for your approval.',
                       paypal=FakePayPal(fail='send_invoice'))
    k.chat('Bill Café Hana for the bread.')
    done = k.decide('A-001', True)
    assert 'PayPal did not do it' in done and 'test refusal' in done
    item = k.approvals.list(None)[0]
    assert item['outcome'] == 'failed'
    assert any(r['rule'] == 'paypal.error' for r in k.audit.rows())


def test_without_paypal_the_invoice_tools_are_not_offered(tmp_path):
    s = Settings(api_key='test-key', data_dir=tmp_path / 'd', timezone='Asia/Tokyo')
    k = Kioku(s, llm=FakeLLM(), clock=lambda: datetime(2026, 10, 5, tzinfo=TZ))
    assert not k.money.on and 'create_invoice' not in {t['function']['name'] for t in k.tools()}
    assert k.guard.check_tool(type('C', (), {'id': 'x', 'name': 'create_invoice', 'arguments': {}})(), {}).action == 'block'


@pytest.mark.parametrize('items,problem', [
    ([], 'at least one item'),
    ([{'name': 'Baguette', 'quantity': 0, 'unit_price': 320}], 'above zero'),
    ([{'name': 'Baguette', 'quantity': 2, 'unit_price': 320.5}], 'whole numbers'),
    ([{'name': '', 'quantity': 2, 'unit_price': 320}], 'needs a name'),
])
def test_bad_items_are_refused(shop, items, problem):
    k, fake, pp = shop(('', [('create_invoice', {'customer': 'Café Hana', 'items': items})]), 'Could not.')
    reply = k.chat('Bill Café Hana.')
    [e] = [e for e in reply.events if e.tool == 'create_invoice']
    assert e.decision == 'block' and e.rule == 'invoice.bad_items' and problem in e.detail


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_client_sends_what_paypal_expects_and_hides_its_keys():
    sent = []

    def opener(req, timeout):
        sent.append(req)
        if req.full_url.endswith('/v1/oauth2/token'):
            return FakeResponse(200, {'access_token': 'tok', 'expires_in': 3600})
        return FakeResponse(201, {'id': 'INV2-AAAA'})

    pp = PayPal('client-id-123', 'secret-456', opener=opener)
    from datetime import date
    iid = pp.create_invoice(business_name='Café Hana', email='cafe-hana@example.com', currency='JPY', due=date(2026, 10, 19),
                            items=[{'name': 'Baguette', 'quantity': Decimal('40.0'), 'unit_price': Decimal('320')}], reference='Kioku A-001')
    assert iid == 'INV2-AAAA'
    body = json.loads(sent[1].data)
    assert body['items'][0]['quantity'] == '40' and body['items'][0]['unit_amount'] == {'currency_code': 'JPY', 'value': '320'}
    assert body['detail']['payment_term'] == {'term_type': 'DUE_ON_DATE_SPECIFIED', 'due_date': '2026-10-19'}
    assert body['primary_recipients'][0]['billing_info']['email_address'] == 'cafe-hana@example.com'
    assert sent[1].get_header('Authorization') == 'Bearer tok'
    assert 'secret' not in repr(pp) and 'client-id' not in repr(pp)


def test_a_paypal_error_says_what_paypal_said():
    def opener(req, timeout):
        if req.full_url.endswith('/v1/oauth2/token'):
            return FakeResponse(200, {'access_token': 'tok', 'expires_in': 3600})
        raise urllib.error.HTTPError(req.full_url, 422, 'x', {}, io.BytesIO(json.dumps(
            {'name': 'UNPROCESSABLE_ENTITY', 'message': 'The requested action could not be performed.', 'debug_id': 'abc123',
             'details': [{'issue': 'CANNOT_REMIND', 'description': 'Invoice is already paid.'}]}).encode()))

    with pytest.raises(PayPalError) as e:
        PayPal('a', 'b', opener=opener).remind('INV2-AAAA')
    assert 'Invoice is already paid.' in str(e.value) and 'abc123' in str(e.value)
