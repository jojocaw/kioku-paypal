"""Money on the owner's side: the customer book, the invoice ledger, and the PayPal actions the owner approved.

PayPal is the record of truth. The ledger (invoices.json) is Kioku's copy, refreshed from PayPal whenever a status
matters: before a reminder or a payment is queued, when the owner asks who has paid, and when a reply says someone
paid. Customers' email addresses live only in customers.json; the model knows customers by name.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .guard import PATTERNS
from .paypal import CLOSED, PAID, PayPal, plain, summary

SYMBOL = {'JPY': '¥', 'USD': '$', 'EUR': '€', 'GBP': '£'}
AMOUNT = r'(?:[¥$€£]\s?[1-9][\d,]*(?:\.\d+)?|\b[1-9][\d,]*(?:\.\d+)?\s?(?:yen|JPY|USD|EUR|GBP)\b|[1-9][\d,]*\s?円)'
# What a reply says about a customer's money, clause by clause: "nothing left to pay" is read first ("has no outstanding
# balance", "owes you nothing"), then "not paid" ("hasn't paid", "still has ¥36,600 due"), then "paid".
SAYS_CLEAR = re.compile(r"(?i)\bno\s+(?:outstanding|unpaid)\b|\bnothing\s+(?:is\s+)?(?:still\s+)?(?:due|owed|owing|outstanding|left\s+to\s+pay)\b"
                        r"|\bno\s+(?:balance|amount|money|payment)\s+(?:is\s+)?(?:still\s+)?(?:due|owed|owing|outstanding|left|remaining)\b"
                        r"|\bowes?\s+(?:you\s+|us\s+)?nothing\b|\b(?:doesn't|does not|don't|do not|no longer)\s+owes?\b"
                        r"|[¥$€£]\s?0(?![\d,.])\s+(?:is\s+)?(?:still\s+)?(?:due|owed|owing|outstanding|remaining|left)\b"
                        r"|(?:未払い|未入金)(?:は|が)?(?:なし|ありません)")
SAYS_UNPAID = re.compile(r"(?i)\b(?:hasn't|has not|haven't|have not|didn't|did not|not yet|never)\b[^.!?]{0,24}\b(?:paid|settled)\b"
                         r"|\bunpaid\b|\boverdue\b|\bstill (?:owes?|due|open)\b|\bowes? you\b|\boutstanding\b|\bnot (?:yet )?(?:been )?(?:paid|settled)\b"
                         r"|\b(?:has|have|is|are)\s+yet\s+to\s+(?:pay|be\s+paid|settle)\b"
                         rf"|\bowes?\s+(?:us\s+)?{AMOUNT}|{AMOUNT}\s+(?:is\s+|are\s+)?(?:still\s+)?(?:due|owed|owing|outstanding|unpaid|remaining|left)\b"
                         rf"|\bbalance\s+(?:due\s+|remaining\s+)?(?:of|is)\s+{AMOUNT}"
                         r"|未払い|未入金|未納|滞納|(?:支払|入金)(?:い|は|が)*まだ|まだ[^。！？]{0,12}(?:支払|入金|払)[^。！？]{0,8}(?:ません|ない)"
                         r"|済み?では(?:ありません|ない)")
SAYS_PAID = re.compile(r"(?i)\b(?:has|have|had|already|just|fully)\s+paid\b|\bpaid (?:in full|up|you|it|the|their|his|her|on|yesterday|today|last)\b"
                       r"|\bpaid\s*[¥$€£\d]|\bpayment (?:was |has been |is )?(?:received|in|complete|completed)\b|\bsettled\b"
                       r"|\bmarked as paid\b|\b(?:is|are|was|were|has been) (?:now )?(?:fully )?paid\b"
                       r"|支払い?済|入金済|完済|支払(?:われ|い)ました|お支払いいただきました|入金(?:が)?ありました|入金(?:を|が)確認(?:でき|され)?ました")
SENTENCE = re.compile(r'[^.!?。！？\n]+[.!?。！？]?')
CLAUSE = re.compile(r'\s[—–]\s|;\s|:\s|,\s(?=(?:but|while|whereas|though|although)\b)')
MAX_ITEMS = 30
REPEAT_DAYS = 7  # the same customer and the same total within a week is flagged as a possible repeat
RECORD_WINDOW = timedelta(minutes=30)  # "I've recorded the payment" holds only for a payment Kioku recorded this recently
NOTE_MAX = 300  # characters of a note to the customer: the approval card shows all of it, and only that goes to PayPal
# A note or an item name with an email address, a web address or a phone number is refused: payment details reach a
# customer only from PayPal's own invoice page — never from words copied out of a forwarded message.
CONTACT = [rx for kind, rx, _ in PATTERNS if kind in ('EMAIL', 'PHONE')] + [
    re.compile(r'(?i)\bhttps?://|\bwww\.|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|info|biz|io|co|jp|me|app|dev|link|example|test)\b')]
NOTE_CONTACT = ('the note to the customer holds an email address, web address or phone number — Kioku never puts contact or '
                'payment details in a note; the customer pays on PayPal\'s own invoice page')


def norm(name: str) -> str:
    """"Café Hana" → "cafehana": no accents, case, spaces or punctuation."""
    s = unicodedata.normalize('NFKD', name or '')
    return re.sub(r'[\W_]+', '', ''.join(ch for ch in s if not unicodedata.combining(ch)).lower())


def fmt(value, currency: str) -> str:
    v = Decimal(str(value))
    text = f'{v:,.0f}' if currency in ('JPY',) or v == v.to_integral() else f'{v:,.2f}'
    sym = SYMBOL.get(currency)
    return f'{sym}{text}' if sym else f'{text} {currency}'


def has_contact(text: str) -> bool:
    return any(rx.search(text or '') for rx in CONTACT)


def note_of(args: dict) -> str:
    """The note to the customer exactly as it would reach PayPal: one line, at most NOTE_MAX characters."""
    return ' '.join(str(args.get('note') or '').split())[:NOTE_MAX]


def says(clause: str) -> str:
    """'paid', 'unpaid' or '' — what one clause says about a customer's money ("owes you nothing" counts as paid)."""
    if SAYS_CLEAR.search(clause):
        return 'paid'
    return 'unpaid' if SAYS_UNPAID.search(clause) else 'paid' if SAYS_PAID.search(clause) else ''


@dataclass
class Customer:
    name: str
    email: str
    note: str = ''


class Customers:
    """The owner's customer book. Only the owner's controls add to it — no tool can."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[Customer]:
        if not self.path.is_file():
            return []
        return [Customer(**c) for c in json.loads(self.path.read_text(encoding='utf-8'))]

    def add(self, name: str, email: str, note: str = '') -> Customer:
        name, email = name.strip(), email.strip()
        if not name or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[A-Za-z]{2,}', email):
            raise ValueError('a customer needs a name and a valid email address')
        rows = [c for c in self.all() if norm(c.name) != norm(name)]
        rows.append(Customer(name, email, note.strip()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([c.__dict__ for c in rows], ensure_ascii=False, indent=1), encoding='utf-8')
        return rows[-1]

    def find(self, name: str) -> tuple[Customer | None, list[Customer]]:
        """(the customer, []) for one match; (None, candidates) when it is unclear; (None, []) when unknown."""
        want = norm(name)
        if not want:
            return None, []
        rows = self.all()
        exact = [c for c in rows if norm(c.name) == want]
        if exact:
            return exact[0], []
        words = [norm(w) for w in re.split(r'\s+', name) if norm(w)]
        near = [c for c in rows if all(w in norm(c.name) for w in words)]
        return (near[0], []) if len(near) == 1 else (None, near)


class Ledger:
    """Kioku's copy of the invoices it created (invoices.json), newest last."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else []

    def _save(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.path)

    def add(self, row: dict) -> None:
        self._save(self.all() + [row])

    def update(self, invoice_id: str, **fields) -> dict:
        rows = self.all()
        for r in rows:
            if r['id'] == invoice_id:
                r.update(fields)
                self._save(rows)
                return r
        raise KeyError(invoice_id)

    def find(self, ref: str) -> tuple[dict | None, list[dict]]:
        """By PayPal id, invoice number ("#0007", "0007") or customer name (their newest open invoice)."""
        ref = (ref or '').strip()
        rows = self.all()
        key = ref.lstrip('#').strip()
        for r in reversed(rows):
            if key and (r['id'] == key or r.get('number', '').lstrip('0') == key.lstrip('0') and key.lstrip('0')):
                return r, []
        want = norm(ref)
        mine = [r for r in reversed(rows) if want and (want == norm(r['customer']) or want in norm(r['customer']))]
        open_ = [r for r in mine if r.get('status') not in CLOSED]
        if len({r['customer'] for r in mine}) > 1:
            return None, mine
        return (open_ or mine or [None])[0], []


def total_of(items: list, currency: str) -> tuple[Decimal | None, list[dict], str]:
    """Exact total of the items, the cleaned items, and '' — or (None, [], what is wrong)."""
    if not isinstance(items, list) or not items:
        return None, [], 'an invoice needs at least one item'
    if len(items) > MAX_ITEMS:
        return None, [], f'at most {MAX_ITEMS} items per invoice'
    clean, total = [], Decimal(0)
    for i in items:
        if not isinstance(i, dict) or not str(i.get('name', '')).strip():
            return None, [], 'every item needs a name'
        if has_contact(str(i['name'])):
            return None, [], 'an item name cannot hold an email address, web address or phone number'
        try:
            qty = Decimal(str(i.get('quantity', 1)).replace(',', ''))
            price = Decimal(str(i.get('unit_price', '')).replace(',', '').replace('¥', '').replace('$', ''))
        except InvalidOperation:
            return None, [], f'"{i.get("name")}": quantity and unit price must be numbers'
        if qty <= 0 or price <= 0 or qty > 100_000:
            return None, [], f'"{i.get("name")}": quantity and unit price must be above zero'
        if currency in ('JPY',) and price != price.to_integral():
            return None, [], f'"{i.get("name")}": yen prices are whole numbers'
        clean.append({'name': str(i['name']).strip()[:200], 'quantity': qty, 'unit_price': price})
        total += qty * price
    return total, clean, ''


def describe_items(items: list[dict], currency: str) -> str:
    return ', '.join(f'{i["name"]} {plain(i["quantity"])} × {fmt(i["unit_price"], currency)}' for i in items)


class Money:
    def __init__(self, data_dir: Path, paypal: PayPal | None, clock, currency: str = 'JPY',
                 max_invoice: float = 300_000, notify: bool = True):
        self.customers = Customers(Path(data_dir) / 'customers.json')
        self.ledger = Ledger(Path(data_dir) / 'invoices.json')
        self.paypal, self.clock, self.currency = paypal, clock, currency
        self.max_invoice, self.notify = Decimal(str(max_invoice)), notify

    @property
    def on(self) -> bool:
        return self.paypal is not None

    # ------------------------------------------------------------ the guard's checks (owner side, real values)
    def check(self, name: str, args: dict, context: str = '') -> tuple[str, str, str]:
        """(action, rule, reason) for an invoice tool. Reads PayPal when a status matters; never writes.
        context: the owner's message — an email address in it never changes where an invoice goes."""
        if not self.on:
            return 'block', 'paypal.off', 'PayPal is not connected'
        if name == 'check_invoices':
            return 'allow', 'paypal.read', 'reads invoice statuses from PayPal'
        if name == 'create_invoice':
            cust, maybe = self.customers.find(str(args.get('customer', '')))
            if not cust:
                if maybe:
                    return 'block', 'invoice.which_customer', 'more than one customer matches: ' + ', '.join(c.name for c in maybe[:5])
                return ('block', 'invoice.unknown_customer',
                        f'"{args.get("customer", "")}" is not in your customer book — only the owner can add customers')
            total, items, problem = total_of(args.get('items'), self.currency)
            if problem:
                return 'block', 'invoice.bad_items', problem
            if total > self.max_invoice:
                return 'block', 'invoice.limit', f'{fmt(total, self.currency)} is over the limit of {fmt(self.max_invoice, self.currency)} per invoice'
            note = note_of(args)
            if has_contact(note):
                return 'block', 'invoice.note_contact', NOTE_CONTACT
            reason = f'bill {cust.name} {fmt(total, self.currency)} through PayPal ({describe_items(items, self.currency)})'
            if note:  # the card shows the note word for word: text from a forwarded message never rides along unseen
                reason += f' — note to the customer: “{note}”'
            others = {e.lower() for e in re.findall(r'[\w.%+-]+@[\w-]+(?:\.[\w-]+)+', context or '')} - {cust.email.lower()}
            if others:
                reason += f' — to the address in your customer book; the message names {len(others)} other address' + ('es' if len(others) > 1 else '') + ', not used'
            since = self.clock() - timedelta(days=REPEAT_DAYS)
            repeat = [r for r in self.ledger.all() if r['customer'] == cust.name and Decimal(r['total']) == total
                      and r.get('status') != 'CANCELLED' and datetime.fromisoformat(r['created']) >= since]
            if repeat:
                r = repeat[-1]
                return 'ask', 'invoice.possible_repeat', f'{reason} — careful: the same total went to {cust.name} on {r["created"][:10]} (#{r["number"]})'
            return 'ask', 'invoice.ask_owner', reason
        if name in ('send_reminder', 'record_payment'):
            row, maybe = self.ledger.find(str(args.get('invoice', '') or args.get('customer', '')))
            if not row:
                if maybe:
                    return 'block', 'invoice.which', 'more than one customer matches — say which invoice'
                return 'block', 'invoice.unknown', 'no invoice of yours matches that'
            row = self.refresh_one(row)
            if row['status'] in PAID:
                return 'block', 'invoice.already_paid', f'PayPal shows #{row["number"]} ({row["customer"]}) as paid'
            if row.get('shared'):  # the public demo's starting invoices are the same PayPal invoices for every visitor
                return ('block', 'demo.shared_invoice', f'#{row["number"]} is shared by everyone trying the demo, so it stays as it '
                        'is — ask Kioku to bill a customer, then try reminders and payments on that invoice')
            if row['status'] in CLOSED:
                return 'block', 'invoice.closed', f'PayPal shows #{row["number"]} as {row["status"].lower()}'
            due = Decimal(row['due_amount'])
            if name == 'send_reminder':
                note = note_of(args)
                if has_contact(note):
                    return 'block', 'invoice.note_contact', NOTE_CONTACT
                return 'ask', 'invoice.ask_owner', (f'remind {row["customer"]} about #{row["number"]} ({fmt(due, row["currency"])} due) '
                                                    'through PayPal' + (f' — note to the customer: “{note}”' if note else ''))
            try:
                value = Decimal(str(args.get('amount') or due).replace(',', '').replace('¥', ''))
            except InvalidOperation:
                return 'block', 'invoice.bad_amount', 'the amount is not a number'
            if value <= 0 or value > due:
                return 'block', 'invoice.bad_amount', f'#{row["number"]} has {fmt(due, row["currency"])} due; {fmt(value, row["currency"])} does not fit'
            method = str(args.get('method') or 'cash').lower()
            return 'ask', 'invoice.ask_owner', f'record {fmt(value, row["currency"])} received from {row["customer"]} ({method}) on #{row["number"]} in PayPal'
        return 'block', 'tools.unknown', f'"{name}" is not an invoice tool'

    # ------------------------------------------------------------ approved actions
    def carry_out(self, item: dict, approval_id: str) -> tuple[str, list[str]]:
        """Run an approved invoice action against PayPal. Returns (what happened, notes for the owner's memory)."""
        name, args, now = item['tool'], item['arguments'], self.clock()
        if name == 'create_invoice':
            cust, _ = self.customers.find(str(args.get('customer', '')))
            total, items, problem = total_of(args.get('items'), self.currency)
            if not cust or problem:
                raise ValueError(problem or 'the customer is no longer in the customer book')
            due = (now + timedelta(days=int(args.get('due_days') or 14))).date()
            iid = self.paypal.create_invoice(business_name=cust.name, email=cust.email, items=items, currency=self.currency,
                                             due=due, note=note_of(args), reference=f'Kioku {approval_id}',
                                             invoice_date=now.date())
            payer = self.paypal.send_invoice(iid, notify=self.notify)
            s = summary(self.paypal.get_invoice(iid))
            row = {**s, 'customer': cust.name, 'items': [{'name': i['name'], 'quantity': plain(i['quantity']),
                                                        'unit_price': plain(i['unit_price'])} for i in items],
                   'created': now.isoformat(timespec='seconds'), 'approval': approval_id,
                   'payer_url': s['payer_url'] or payer, 'checked': now.isoformat(timespec='seconds')}
            self.ledger.add(row)
            text = (f'Invoice #{row["number"]} sent to {cust.name} through PayPal: {fmt(total, self.currency)} '
                    f'({describe_items(items, self.currency)}), due {due.isoformat()}.')
            return text, [text]
        row, _ = self.ledger.find(str(args.get('invoice', '') or args.get('customer', '')))
        if not row:
            raise ValueError('that invoice is no longer in the ledger')
        if name == 'send_reminder':
            self.paypal.remind(row['id'], note_of(args))
            self.ledger.update(row['id'], reminded=now.isoformat(timespec='seconds'))
            text = f'Reminder sent to {row["customer"]} about invoice #{row["number"]} through PayPal.'
            return text, [text]
        if name == 'record_payment':
            value = Decimal(str(args.get('amount') or row['due_amount']).replace(',', '').replace('¥', ''))
            method = {'cash': 'CASH', 'bank': 'BANK_TRANSFER', 'bank transfer': 'BANK_TRANSFER', 'bank_transfer': 'BANK_TRANSFER',
                      'check': 'CHECK', 'cheque': 'CHECK'}.get(str(args.get('method') or 'cash').lower(), 'OTHER')
            self.paypal.record_payment(row['id'], value, row['currency'], method=method, paid_on=now.date(),
                                       note=f'Recorded by Kioku {approval_id}')
            row = self.refresh_one(row)
            # what Kioku itself recorded, and when: the only thing that makes a reply's "Recorded ¥12,320 …" true
            row = self.ledger.update(row['id'], recorded=row.get('recorded', []) + [
                {'at': now.isoformat(timespec='seconds'), 'amount': plain(value), 'approval': approval_id}])
            text = (f'{fmt(value, row["currency"])} from {row["customer"]} recorded on invoice #{row["number"]} in PayPal '
                    f'— PayPal now shows it as {row["status"].replace("_", " ").lower()}.')
            return text, [text]
        raise ValueError(f'no runner for {name}')

    # ------------------------------------------------------------ reading PayPal
    def refresh_one(self, row: dict) -> dict:
        s = summary(self.paypal.get_invoice(row['id']))
        return self.ledger.update(row['id'], **{k: v for k, v in s.items() if k not in ('payer_url',) or v},
                                  checked=self.clock().isoformat(timespec='seconds'))

    def refresh(self, customer: str = '', which: str = 'open') -> tuple[list[dict], list[dict]]:
        """(rows to show, rows that became paid since the last check). Reads each matching invoice from PayPal."""
        rows = self.ledger.all()
        if customer:
            want = norm(customer)
            rows = [r for r in rows if want and want in norm(r['customer'])]
        if which == 'open':
            rows = [r for r in rows if r.get('status') not in CLOSED]
        elif which == 'paid':
            rows = [r for r in rows if r.get('status') in PAID]
        out, newly_paid = [], []
        for r in rows[-20:]:
            before = r.get('status')
            r2 = self.refresh_one(r)
            out.append(r2)
            if r2['status'] in PAID and before not in PAID:
                newly_paid.append(r2)
        if which == 'open':  # still open, plus the ones that were paid since the last look (the owner wants to hear that)
            out = [r for r in out if r['status'] not in CLOSED] + newly_paid
        return out, newly_paid

    def verify(self, text: str) -> list[dict]:
        """Every sentence of a reply that says a customer has or has not paid, checked against PayPal (read-only).
        Returns [{'customer', 'says', 'row', 'ok'}] for customers with invoices in the ledger."""
        if not self.on:
            return []
        customers = [c for c in self.customers.all() if len(norm(c.name)) >= 3]
        rows = self.ledger.all()
        out, fresh = [], {}
        for sentence in SENTENCE.findall(text or ''):
            if sentence.strip().endswith(('?', '？')):
                continue
            # clause by clause: "Yes, Hotel Sakura paid last week — #0006 still has ¥36,600 due" says both, and each is
            # checked; a clause that only names someone ("- Hotel Sakura: ¥36,600 still due") says who the next one is about
            label = (set(), [])
            for clause in (c.strip() for c in CLAUSE.split(sentence)):
                numbers = {n.lstrip('0') for n in re.findall(r'#\s?(\d+)', clause)} - {''}
                named_in = {r['customer'] for r in rows if r.get('number', '').lstrip('0') in numbers}  # "#0003 is still unpaid"
                who = [c for c in customers if norm(c.name) in norm(clause) or c.name in named_in]
                said = says(clause)
                if not said:
                    rest = re.sub(r'#\s?\d+', ' ', clause)
                    for c in who:
                        rest = re.sub(re.escape(c.name), ' ', rest, flags=re.I)
                    if who and len(re.findall(r'[^\W\d_]+', rest)) <= 2:  # just a name: "- **Hotel Sakura** (#0006)"
                        label = (numbers, who)
                    continue
                if not who:
                    numbers, who = label
                for c in who:
                    theirs = [r for r in rows if r['customer'] == c.name]
                    if not theirs:
                        continue
                    named = [r for r in theirs if r.get('number', '').lstrip('0') in numbers]
                    row = (named or theirs)[-1]
                    if row['id'] not in fresh:
                        fresh[row['id']] = self.refresh_one(row)
                    row = fresh[row['id']]
                    out.append({'customer': c.name, 'says': said, 'row': row, 'ok': (row['status'] in PAID) == (said == 'paid')})
        return out

    def claim_holds(self, tool: str, said: str, reply: str = '') -> bool:
        """The reply says an invoice or a reminder went out, or a payment was recorded (said: the sentences that say so;
        reply: the whole reply, for the customer's name when those sentences leave it out). True only when Kioku's own
        records show it for that customer — an invoice or a reminder the owner approved within the last day, or a payment
        Kioku recorded after the owner's approval within the last half hour, for the amount and invoice the claim names.
        An invoice PayPal shows as paid from before (paid in cash last week, say) never makes a new "Recorded ¥12,320"
        true: the claim must be about something actually done just now."""
        now = self.clock()
        customers = [c.name for c in self.customers.all() if len(norm(c.name)) >= 3]
        names = [n for n in customers if norm(n) in norm(said)] or [n for n in customers if norm(n) in norm(reply)]
        rows = [r for r in self.ledger.all() if not names or r['customer'] in names]
        recent = lambda ts, window=timedelta(hours=24): bool(ts) and datetime.fromisoformat(ts) >= now - window  # noqa: E731
        if tool == 'create_invoice':
            return any(r.get('approval') not in ('', 'demo') and recent(r.get('created')) for r in rows)
        if tool == 'send_reminder':
            return any(recent(r.get('reminded')) for r in rows)
        if tool == 'record_payment':
            numbers = {n.lstrip('0') for n in re.findall(r'#\s?(\d+)', said)} - {''}
            amounts = {Decimal(a.replace(',', '')) for a in re.findall(r'[¥$€£]\s?(\d[\d,]*(?:\.\d+)?)', said)}
            return any(recent(p['at'], RECORD_WINDOW) and (not numbers or r.get('number', '').lstrip('0') in numbers)
                       and (not amounts or Decimal(p['amount']) in amounts) for r in rows for p in r.get('recorded', []))
        return False

    def named_in(self, text: str) -> set[str]:
        """The customers a message names: by name or a distinctive word of it ("Sakura"), or by one of their invoice
        numbers ("#0011")."""
        numbers = {n.lstrip('0') for n in re.findall(r'#\s?(\d+)', text or '')} - {''}
        names = {r['customer'] for r in self.ledger.all() if r.get('number', '').lstrip('0') in numbers}
        for c in self.customers.all():
            words = re.findall(r'\w{4,}', c.name.lower())
            if (len(norm(c.name)) >= 3 and norm(c.name) in norm(text)) \
                    or any(re.search(rf'(?i)\b{re.escape(w)}\b', text or '') for w in words):
                names.add(c.name)
        return names

    def about(self, name: str, args: dict) -> str:
        """The customer an invoice tool call is for, from the customer book or the ledger ('' when none matches)."""
        if name == 'create_invoice':
            cust, _ = self.customers.find(str(args.get('customer', '')))
            return cust.name if cust else ''
        row, _ = self.ledger.find(str(args.get('invoice', '') or args.get('customer', '')))
        return row['customer'] if row else ''

    def correction(self, v: dict) -> str:
        r, at = v['row'], self.clock().strftime('%H:%M')
        state = r['status'].replace('_', ' ').lower()
        if r['status'] in PAID:
            return f"PayPal shows {r['customer']}'s invoice #{r['number']} as {state} (checked {at})"
        return (f"PayPal shows {r['customer']}'s invoice #{r['number']} as {state} — "
                f"{fmt(r.get('due_amount') or r['total'], r['currency'])} still due (checked {at})")

    def open_summary(self) -> str:
        """The open invoices from the ledger (refresh first), split into overdue and not yet due — so a model writing a
        briefing cannot blur the two."""
        today, late, coming = self.clock().date(), [], []
        for r in self.ledger.all():
            if r.get('status') in CLOSED:
                continue
            days = (today - date.fromisoformat(r['due_date'])).days if r.get('due_date') else 0
            due = fmt(r.get('due_amount') or r['total'], r['currency'])
            if days > 0:
                late.append(f'- #{r["number"]} · {r["customer"]} · {due} still due · {days} day{"s" if days != 1 else ""} overdue')
            else:
                coming.append(f'- #{r["number"]} · {r["customer"]} · {due} still due · due {r.get("due_date") or "on receipt"}')
        parts = (['Overdue:'] + late if late else []) + (['Not due yet:'] + coming if coming else [])
        return '\n'.join(parts)

    def line(self, r: dict) -> str:
        due = Decimal(r.get('due_amount') or r['total'])
        state = r['status'].replace('_', ' ').lower()
        late = ''
        if r['status'] not in CLOSED and r.get('due_date'):
            days = (self.clock().date() - date.fromisoformat(r['due_date'])).days
            late = f', {days} day{"s" if days != 1 else ""} overdue' if days > 0 else f', due {r["due_date"]}'
        owed = f', {fmt(due, r["currency"])} still due' if r['status'] not in CLOSED else ''
        return f'#{r["number"]} · {r["customer"]} · {fmt(r["total"], r["currency"])} · {state}{owed}{late}'
