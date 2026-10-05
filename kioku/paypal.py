"""PayPal client for the actions the owner approved (Invoicing v2; the sandbox unless told otherwise).

Only the owner's side uses it: after the owner pressed Approve, or to read an invoice's status. The model never sees
the keys, the customers' email addresses or this client — it asks through tools, and the guard decides.
Standard library only.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal

SANDBOX = 'https://api-m.sandbox.paypal.com'
ZERO_DECIMAL = {'JPY', 'HUF', 'TWD'}  # currencies PayPal takes in whole units
PAID = {'PAID', 'MARKED_AS_PAID'}
CLOSED = PAID | {'CANCELLED', 'REFUNDED', 'MARKED_AS_REFUNDED'}


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str = ''):
        super().__init__(f'PayPal said {status} {name}: {message}' + (f' (debug id {debug_id})' if debug_id else ''))
        self.status, self.name, self.debug_id = status, name, debug_id


def plain(value) -> str:
    """Decimal('40.0') → "40", Decimal('2.50') → "2.5": no exponent, no trailing zeros."""
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def amount(value: Decimal | int | str, currency: str) -> dict:
    step = Decimal(1) if currency in ZERO_DECIMAL else Decimal('0.01')
    return {'currency_code': currency, 'value': str(Decimal(str(value)).quantize(step))}


class PayPal:
    def __init__(self, client_id: str, secret: str, base_url: str = SANDBOX, opener=None):
        self._client_id, self._secret = client_id, secret
        self.base_url = base_url.rstrip('/')
        self._open = opener or urllib.request.urlopen  # tests pass a fake
        self._token, self._expires = '', 0.0

    def __repr__(self) -> str:  # never shows the keys
        return f'PayPal({self.base_url})'

    @property
    def sandbox(self) -> bool:
        return 'sandbox' in self.base_url

    # ------------------------------------------------------------ HTTP
    def _send(self, method: str, path: str, body: dict | None = None, *, form: str | None = None,
              auth: str = '') -> tuple[int, dict]:
        headers = {'Accept': 'application/json', 'Prefer': 'return=representation'}
        data = None
        if form is not None:
            data, headers['Content-Type'] = form.encode(), 'application/x-www-form-urlencoded'
        elif body is not None:
            data, headers['Content-Type'] = json.dumps(body).encode(), 'application/json'
        headers['Authorization'] = auth or f'Bearer {self._bearer()}'
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._open(req, timeout=30) as r:
                raw = r.read().decode('utf-8') or '{}'
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace') or '{}'
            try:
                err = json.loads(raw)
            except json.JSONDecodeError:
                err = {'message': raw[:200]}
            detail = '; '.join(d.get('description') or d.get('issue', '') for d in err.get('details', []) if isinstance(d, dict))
            raise PayPalError(e.code, err.get('name') or err.get('error', 'ERROR'),
                              (err.get('message') or err.get('error_description') or '') + (f' — {detail}' if detail else ''),
                              err.get('debug_id', '')) from None

    def _bearer(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            basic = base64.b64encode(f'{self._client_id}:{self._secret}'.encode()).decode()
            _, tok = self._send('POST', '/v1/oauth2/token', form='grant_type=client_credentials', auth=f'Basic {basic}')
            self._token, self._expires = tok['access_token'], time.time() + float(tok.get('expires_in', 300))
        return self._token

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            return self._send(method, path, body)[1]
        except PayPalError as e:
            if e.status != 401:
                raise
            self._token = ''  # expired early: one fresh token, one more try
            return self._send(method, path, body)[1]

    # ------------------------------------------------------------ invoices
    def create_invoice(self, *, business_name: str, email: str, items: list[dict], currency: str, due: date | None = None,
                       note: str = '', reference: str = '', invoice_date: date | None = None) -> str:
        """items: [{'name', 'quantity', 'unit_price'}]. Returns the invoice id (INV2-…), still a draft.
        invoice_date: the owner's today (PayPal accepts past dates; the demo's clock runs behind the real one)."""
        detail = {'currency_code': currency}
        if invoice_date:
            detail['invoice_date'] = invoice_date.isoformat()
        if note:
            detail['note'] = note[:4000]
        if reference:
            detail['reference'] = reference[:120]
        detail['payment_term'] = ({'term_type': 'DUE_ON_DATE_SPECIFIED', 'due_date': due.isoformat()} if due
                                  else {'term_type': 'DUE_ON_RECEIPT'})
        body = {'detail': detail,
                'primary_recipients': [{'billing_info': {'business_name': business_name[:300], 'email_address': email}}],
                'items': [{'name': str(i['name'])[:200], 'quantity': plain(i['quantity']),
                           'unit_amount': amount(i['unit_price'], currency), 'unit_of_measure': 'QUANTITY'} for i in items]}
        out = self._call('POST', '/v2/invoicing/invoices', body)
        return out.get('id') or out.get('href', '').rstrip('/').rsplit('/', 1)[-1]

    def send_invoice(self, invoice_id: str, *, notify: bool = True) -> str:
        """Sends the draft (PayPal emails the customer when notify is on). Returns the customer's payment page."""
        out = self._call('POST', f'/v2/invoicing/invoices/{invoice_id}/send',
                         {'send_to_recipient': notify, 'send_to_invoicer': False})
        return out.get('href', '')

    def get_invoice(self, invoice_id: str) -> dict:
        return self._call('GET', f'/v2/invoicing/invoices/{invoice_id}')

    def remind(self, invoice_id: str, note: str = '') -> None:
        body = {'send_to_recipient': True, 'send_to_invoicer': False}
        if note:
            body['note'] = note[:4000]
        self._call('POST', f'/v2/invoicing/invoices/{invoice_id}/remind', body)

    def record_payment(self, invoice_id: str, value: Decimal, currency: str, *, method: str = 'CASH',
                       paid_on: date | None = None, note: str = '') -> str:
        body = {'method': method, 'payment_date': (paid_on or date.today()).isoformat(), 'amount': amount(value, currency)}
        if note:
            body['note'] = note[:2000]
        return self._call('POST', f'/v2/invoicing/invoices/{invoice_id}/payments', body).get('payment_id', '')

    def cancel(self, invoice_id: str, note: str = '') -> None:
        self._call('POST', f'/v2/invoicing/invoices/{invoice_id}/cancel',
                   {'send_to_recipient': True, 'send_to_invoicer': False, **({'note': note[:4000]} if note else {})})


def summary(inv: dict) -> dict:
    """The parts of a PayPal invoice Kioku keeps: status and money, as PayPal states them."""
    detail = inv.get('detail', {})
    total = inv.get('amount', {})
    due = inv.get('due_amount') or {}
    paid = (inv.get('payments') or {}).get('paid_amount') or {}
    return {'id': inv.get('id', ''), 'number': detail.get('invoice_number', ''), 'status': inv.get('status', ''),
            'currency': total.get('currency_code', ''), 'total': total.get('value', '0'),
            'due_amount': due.get('value', total.get('value', '0')), 'paid_amount': paid.get('value', '0'),
            'due_date': (detail.get('payment_term') or {}).get('due_date', ''),
            'payer_url': (detail.get('metadata') or {}).get('recipient_view_url', '')}
