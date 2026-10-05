"""The tools Kioku's model may call, and how the owner's side carries them out.

Tools with an outside effect (send_message, make_payment) are never run directly: the guard turns them into
an approval request. In this repository they run against a local outbox / payments file (simulated); a real
deployment would plug in mail or payment providers behind the same approval step.

The invoice tools (INVOICE_TOOLS) are offered only when PayPal is connected. Billing, reminding and recording a
payment wait for the owner like sending and paying; after the owner approves they run against PayPal (money.py).
"""
from __future__ import annotations

import ast
import json
import operator
from datetime import datetime
from pathlib import Path

from .memory import Memory


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {'type': 'function', 'function': {'name': name, 'description': description,
                                             'parameters': {'type': 'object', 'properties': props, 'required': required}}}


S = {'type': 'string'}
TOOLS = [
    _fn('search_memory', "Search the owner's memory (daily notes, weekly/monthly reviews, topic notes) by keywords. "
        'Use it before answering anything about the past.', {'query': S}, ['query']),
    _fn('read_note', 'Read one memory note by path, e.g. "topics/flour-prices.md" or "daily/2026-09-12.md".',
        {'path': S}, ['path']),
    _fn('save_memory', 'Save something the owner wants remembered (a fact, decision, preference, plan or number). '
        'topic: a short reusable subject name, 1-3 words, e.g. "flour prices", "staff", "ovens" — reuse an existing one '
        'when it fits; leave it out for one-off notes.', {'text': S, 'topic': S}, ['text']),
    _fn('add_task', "Add a task to the owner's task list.", {'text': S, 'due': S}, ['text']),
    _fn('list_tasks', "List the owner's open tasks.", {}, []),
    _fn('complete_task', "Mark one of the owner's open tasks as done, when the owner says it is done. "
        'text: words from the task.', {'text': S}, ['text']),
    _fn('calculate', 'Do arithmetic exactly, e.g. "(6800-6300)*28" or "6800*0.98". Use it for every total, difference, '
        'percentage or price change — never work numbers out in your head.', {'expression': S}, ['expression']),
    _fn('send_message', "Send an email or message for the owner. It always waits for the owner's approval.",
        {'to': S, 'subject': S, 'body': S}, ['to', 'body']),
    _fn('make_payment', "Pay for something for the owner. It always waits for the owner's approval.",
        {'what': S, 'amount': {'type': 'number'}, 'currency': S, 'payee': S}, ['what', 'amount', 'payee']),
]

N = {'type': 'number'}
INVOICE_TOOLS = [
    _fn('create_invoice', "Bill one of the owner's customers through PayPal. customer: the name from the customer book. "
        'items: what was sold, each with name, quantity and unit_price. It always waits for the owner\'s approval.',
        {'customer': S, 'items': {'type': 'array', 'items': {'type': 'object', 'properties': {'name': S, 'quantity': N, 'unit_price': N},
                                                              'required': ['name', 'quantity', 'unit_price']}},
         'due_days': {'type': 'integer'}, 'note': S}, ['customer', 'items']),
    _fn('check_invoices', "Read the owner's invoices from PayPal: who has paid and what is still due. Call it before answering "
        "anything about payments. customer: optional name; which: 'open' (default), 'paid' or 'all'.", {'customer': S, 'which': S}, []),
    _fn('send_reminder', 'Send a payment reminder through PayPal for an unpaid invoice. invoice: its number (e.g. #0007) or the '
        "customer's name. It always waits for the owner's approval.", {'invoice': S, 'note': S}, ['invoice']),
    _fn('record_payment', 'Record money the owner received outside PayPal (cash or bank transfer) against an invoice. invoice: its '
        "number or the customer's name; amount: leave out for the full amount due. It always waits for the owner's approval.",
        {'invoice': S, 'amount': N, 'method': S}, ['invoice']),
]
INVOICE_TOOL_NAMES = {t['function']['name'] for t in INVOICE_TOOLS}


OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def calculate(expression: str) -> str:
    """+ - * / % ** and brackets on plain numbers (commas and ¥ allowed). Nothing else is evaluated."""
    text = expression.replace(',', '').replace('¥', '').replace('×', '*').replace('÷', '/').strip()
    if len(text) > 200:
        raise ValueError('expression too long')

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            if isinstance(node.op, ast.Pow) and abs(ev(node.right)) > 10:
                raise ValueError('exponent too large')
            return OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](ev(node.operand))
        raise ValueError('only numbers and + - * / % ** ( ) are allowed')

    value = ev(ast.parse(text, mode='eval'))
    value = round(value, 6)
    return f'{expression} = {int(value):,}' if float(value).is_integer() else f'{expression} = {value:,}'


def run_tool(name: str, args: dict, memory: Memory, now: datetime) -> str:
    """Carry out an allowed tool on the owner's side. Returns the text the model sees as the tool result."""
    if name == 'search_memory':
        hits = memory.search(str(args.get('query', '')), k=6)
        if not hits:
            return 'No matching notes.'
        return '\n'.join(f'- {h.path} › {h.heading}: {h.text[:500]}' for h in hits)
    if name == 'read_note':
        body = memory.read(str(args.get('path', '')))
        return body[:6000] if body else 'No such note.'
    if name == 'save_memory':
        rel = memory.save_note(str(args.get('text', '')), now, args.get('topic') or None)
        return f'Saved to {rel}.'
    if name == 'add_task':
        memory.add_task(str(args.get('text', '')), args.get('due') or None)
        return 'Task added.'
    if name == 'calculate':
        try:
            return calculate(str(args.get('expression', '')))
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as e:
            return f'Could not calculate: {e}'
    if name == 'complete_task':
        task = memory.complete_task(str(args.get('text', '')), now)
        return f'Marked done: {task}' if task else 'No open task matches those words.'
    if name == 'list_tasks':
        tasks = memory.open_tasks()
        return '\n'.join(f'- {t}' for t in tasks) if tasks else 'No open tasks.'
    raise ValueError(f'no runner for {name}')


def carry_out_approved(item: dict, data_dir: Path, now: datetime) -> str:
    """Run an action the owner approved. Simulated: written to outbox.jsonl / payments.jsonl."""
    file = {'send_message': 'outbox.jsonl', 'make_payment': 'payments.jsonl'}[item['tool']]
    with (Path(data_dir) / file).open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': now.isoformat(timespec='seconds'), 'approval': item['id'], **item['arguments']},
                           ensure_ascii=False) + '\n')
    return f'{item["tool"]} {item["id"]} carried out (simulated, see {file})'
