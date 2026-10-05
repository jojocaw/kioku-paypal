"""Give the demo bakery a customer book and three real invoices in the PayPal sandbox (run once).

    python demo/paypal/seed_invoices.py          # needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox) in paypal.env

Writes demo/seed/customers.json and demo/seed/invoices.json. The invoices are dated in the persona's September and
early October (PayPal accepts past dates), so on the persona's "today" (2026-10-05) one is overdue, one was paid in
cash and one is due next week. Visitors share them, so the guard lets visitors read them but not remind or record
payments on them (rule demo.shared_invoice); invoices a visitor makes are their own.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kioku.config import Settings  # noqa: E402
from kioku.paypal import PayPal, summary  # noqa: E402

SEED = ROOT / 'demo' / 'seed'
CUSTOMERS = [
    {'name': 'Café Hana', 'email': 'orders@cafe-hana.example.com', 'note': 'wholesale baguettes and country loaves, Fridays'},
    {'name': 'Hotel Sakura', 'email': 'kitchen@hotel-sakura.example.com', 'note': 'breakfast croissants and rolls, billed monthly'},
    {'name': 'Ikoi Nursery', 'email': 'office@ikoi-nursery.example.com', 'note': 'afternoon snack bread, billed monthly'},
]
INVOICES = [  # (customer, invoice date, due date, items, paid in cash on)
    ('Hotel Sakura', date(2026, 9, 16), date(2026, 9, 30),
     [{'name': 'Croissant', 'quantity': 120, 'unit_price': 260}, {'name': 'Butter roll', 'quantity': 60, 'unit_price': 90}], None),
    ('Café Hana', date(2026, 9, 26), date(2026, 10, 3),
     [{'name': 'Baguette', 'quantity': 40, 'unit_price': 320}, {'name': 'Pain de campagne', 'quantity': 6, 'unit_price': 680}],
     date(2026, 10, 2)),
    ('Ikoi Nursery', date(2026, 10, 1), date(2026, 10, 15),
     [{'name': 'Melon pan', 'quantity': 50, 'unit_price': 180}], None),
]


def main() -> None:
    if (SEED / 'invoices.json').exists() and '--force' not in sys.argv:
        sys.exit('demo/seed/invoices.json exists (use --force to make new ones)')
    s = Settings.from_env(ROOT)
    if 'sandbox' not in s.paypal_base_url or not s.paypal_client_id:
        sys.exit('needs sandbox PayPal keys')
    pp = PayPal(s.paypal_client_id, s.paypal_secret, s.paypal_base_url)
    emails = {c['name']: c['email'] for c in CUSTOMERS}
    rows = []
    for name, issued, due, items, paid_on in INVOICES:
        iid = pp.create_invoice(business_name=name, email=emails[name], items=items, currency='JPY', due=due,
                                invoice_date=issued, reference='Kioku demo')
        pp.send_invoice(iid, notify=True)
        if paid_on:
            total = sum(Decimal(i['quantity']) * Decimal(i['unit_price']) for i in items)
            pp.record_payment(iid, total, 'JPY', method='CASH', paid_on=paid_on, note='Paid in cash at the shop')
        row = summary(pp.get_invoice(iid))
        rows.append({**row, 'customer': name, 'items': [{k: str(v) for k, v in i.items()} for i in items],
                     'created': f'{issued.isoformat()}T17:00:00+09:00', 'approval': 'demo', 'shared': True, 'checked': ''})
        print(f'#{row["number"]} {name} ¥{row["total"]} {row["status"]} due {row["due_date"]}')
    (SEED / 'customers.json').write_text(json.dumps(CUSTOMERS, ensure_ascii=False, indent=1), encoding='utf-8')
    (SEED / 'invoices.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print('wrote demo/seed/customers.json and demo/seed/invoices.json')


if __name__ == '__main__':
    main()
