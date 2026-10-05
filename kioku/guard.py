"""Kioku Guard — rules enforced in code, outside the model, with a receipt for every decision.

The model never sees raw personal data: it is replaced by placeholders before every call and put back
only on the owner's side. The model cannot change the rules: no tool edits them, unknown tools are
blocked, spending is checked against a budget the model cannot read, and anything with an outside
effect (sending, paying) waits in an approval queue that only the owner can decide — from the owner's
own controls, never from text in the chat.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import MODELS, Settings
from .llm import LLMResult, ToolCall, UsageLedger


class Blocked(Exception):
    def __init__(self, rule: str, reason: str, receipt: str):
        super().__init__(reason)
        self.rule, self.reason, self.receipt = rule, reason, receipt


@dataclass
class Decision:
    action: str  # 'allow' | 'ask' | 'block'
    rule: str
    reason: str
    receipt: str = ''


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- receipts

class Audit:
    """Append-only audit log (audit.jsonl). Each row is a receipt: what was decided, by which rule, and why.
    Rows hold counts and kinds, never the personal data itself."""

    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or utcnow
        self._n = len(self.rows())

    def log(self, event: str, decision: str, rule: str, reason: str, **detail) -> str:
        self._n += 1
        receipt = f'R-{self._n:05d}'
        row = dict(id=receipt, ts=self.clock().isoformat(timespec='seconds'), event=event,
                   decision=decision, rule=rule, reason=reason, **detail)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        return receipt

    def rows(self, last: int | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = [json.loads(s) for s in self.path.read_text(encoding='utf-8').splitlines() if s.strip()]
        return rows[-last:] if last else rows

    def summary(self) -> dict:
        c = Counter((r['rule'], r['decision']) for r in self.rows())
        return {f'{rule} → {decision}': n for (rule, decision), n in sorted(c.items())}


# ---------------------------------------------------------------- personal data

def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


PATTERNS: list[tuple[str, re.Pattern, callable]] = [
    ('SECRET', re.compile(r'\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
                          r'|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})\b'), None),
    ('SECRET', re.compile(r'(?i)\b(?:password|passwd|passcode|api[_ -]?key|secret|token)(?:\s?[:=]\s?|\s+is\s+)'
                          r'(?P<v>\S{6,}?)(?=[.,;:!?)\]}\'"]*(?:\s|$))'), None),  # the value, without trailing punctuation
    ('EMAIL', re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}'), None),
    ('CARD', re.compile(r'(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)'),
     lambda s: 13 <= len(re.sub(r'\D', '', s)) <= 19 and luhn_ok(re.sub(r'\D', '', s))),
    ('PHONE', re.compile(r'''(?<![\w+])(?:
          \+\d{1,3}[\s.\-]?(?:\(?\d{1,4}\)?[\s.\-]?){1,4}\d{2,4}   # +81 90 1234 5678 / +1 (555) 123-4567
        | 0\d{1,4}[\s.\-]\d{1,4}[\s.\-]\d{3,4}                    # 03-1234-5678 / 090-1234-5678
        | 0[5789]0\d{8}                                           # 09012345678
        | \(\d{3}\)\s?\d{3}[\s.\-]\d{4}                           # (555) 123-4567
        | \d{3}[.\-]\d{3}[.\-]\d{4}                               # 555-123-4567
        )(?!\w)''', re.X), None),
    ('POSTCODE', re.compile(r'〒\s?\d{3}-?\d{4}'), None),
    ('GOV_ID', re.compile(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)'), None),
]
TAG = re.compile(r'\[(?:SECRET|EMAIL|CARD|PHONE|POSTCODE|GOV_ID|NAME)_\d+\]')
LOOSE_TAG = re.compile(r'\[?\b(SECRET|EMAIL|CARD|PHONE|POSTCODE|GOV_ID|NAME)_(\d+)\b\]?')  # brackets optional
# Passwords, keys and card numbers are never written into the owner's files, not even on the owner's side.
WITHHELD = {'SECRET': '[withheld]', 'CARD': '[card-withheld]'}


def mask_secrets(text: str) -> str:
    """Replace passwords, API keys and card numbers with a marker. Used for everything Kioku writes to disk."""
    for kind, rx, ok in PATTERNS:
        if kind not in WITHHELD:
            continue
        def sub(m, kind=kind, ok=ok):
            whole = m.group(0)
            if ok and not ok(whole):
                return whole
            if 'v' in m.re.groupindex:
                if m.string.startswith(tuple(WITHHELD.values()), m.start('v')):  # already masked
                    return whole
                a, b = m.start('v') - m.start(), m.end('v') - m.start()
                return whole[:a] + WITHHELD[kind] + whole[b:]
            return WITHHELD[kind]
        text = rx.sub(sub, text or '')
    return text


def withhold_known(text: str, vault: 'Vault') -> str:
    """Values already recognised as secrets in this session stay hidden on disk wherever they reappear — in a reply
    that repeats them back to the owner, or when the owner mentions them again without the word "password"."""
    for tag, value in vault.to_text.items():
        kind = tag[1:].split('_', 1)[0]
        if kind in WITHHELD and len(value) >= 4:
            text = (text or '').replace(value, WITHHELD[kind])
    return text


@dataclass
class Vault:
    """Per-session map between placeholders and the real values. Lives in memory only; never sent anywhere."""
    to_tag: dict = field(default_factory=dict)
    to_text: dict = field(default_factory=dict)
    seq: Counter = field(default_factory=Counter)

    def tag(self, kind: str, original: str) -> str:
        if original not in self.to_tag:
            self.seq[kind] += 1
            tag = f'[{kind}_{self.seq[kind]}]'
            self.to_tag[original], self.to_text[tag] = tag, original
        return self.to_tag[original]


class Redactor:
    def __init__(self, private_terms: tuple[str, ...] = ()):
        terms = sorted({t for t in private_terms if t.strip()}, key=len, reverse=True)
        parts = [rf'\b{re.escape(t)}\b' if re.fullmatch(r'[\w .\'-]+', t, re.ASCII) else re.escape(t) for t in terms]
        self.names = re.compile('|'.join(parts), re.I) if parts else None

    def redact(self, text: str, vault: Vault) -> tuple[str, Counter]:
        found = Counter()
        if not text:
            return text, found
        # Anything hidden once in this session stays hidden, whatever words now surround it (e.g. the owner-side copy
        # of an earlier tool call, where "[SECRET_1]" came back as the real value without the "password:" in front).
        for original in sorted(vault.to_tag, key=len, reverse=True):
            if len(original) >= 4 and original in text:
                tag = vault.to_tag[original]
                found[tag[1:].rsplit('_', 1)[0]] += text.count(original)
                text = text.replace(original, tag)
        for kind, rx, ok in PATTERNS:
            def sub(m, kind=kind, ok=ok):
                whole = m.group(0)
                if (ok and not ok(whole)) or TAG.search(whole) or ('v' in m.re.groupindex and (
                        TAG.match(m.string, m.start('v')) or m.string.startswith(tuple(WITHHELD.values()), m.start('v')))):
                    return whole  # never wrap a placeholder (or a "[withheld]" marker) in another one
                found[kind] += 1
                if 'v' in m.re.groupindex:  # only the value part, e.g. "password: [SECRET_1]"
                    a, b = m.start('v') - m.start(), m.end('v') - m.start()
                    return whole[:a] + vault.tag(kind, m.group('v')) + whole[b:]
                return vault.tag(kind, whole)
            text = rx.sub(sub, text)
        if self.names:
            def sub_name(m):
                found['NAME'] += 1
                return vault.tag('NAME', m.group(0))
            text = self.names.sub(sub_name, text)
        return text, found

    @staticmethod
    def restore(text: str, vault: Vault, withhold: bool = False) -> str:
        """Placeholders → real values. With withhold=True, secrets and card numbers stay hidden (text bound for disk)."""
        def put_back(m):
            kind, tag = m.group(1), f'[{m.group(1)}_{m.group(2)}]'
            if tag not in vault.to_text:
                return m.group(0)
            if withhold and kind in WITHHELD:
                return WITHHELD[kind]
            return vault.to_text[tag]
        return LOOSE_TAG.sub(put_back, text or '')

    def restore_obj(self, obj, vault: Vault, withhold: bool = False):
        if isinstance(obj, str):
            return self.restore(obj, vault, withhold)
        if isinstance(obj, list):
            return [self.restore_obj(x, vault, withhold) for x in obj]
        if isinstance(obj, dict):
            return {k: self.restore_obj(v, vault, withhold) for k, v in obj.items()}
        return obj


# ---------------------------------------------------------------- tools

# What each tool may do without asking. Anything not listed is blocked.
TOOL_RULES = {
    'search_memory': ('allow', 'reads the owner\'s own memory'),
    'read_note': ('allow', 'reads the owner\'s own memory'),
    'save_memory': ('allow', 'writes to the owner\'s own memory'),
    'add_task': ('allow', 'writes to the owner\'s own task list'),
    'list_tasks': ('allow', 'reads the owner\'s own task list'),
    'complete_task': ('allow', 'ticks a task in the owner\'s own task list'),
    'calculate': ('allow', 'arithmetic only, no side effects'),
    'send_message': ('ask', 'sends something outside — the owner decides'),
    'make_payment': ('ask', 'spends money — the owner decides (what, how much, to whom)'),
    # Invoice tools: offered only with PayPal connected; Money.check decides each call (customer book, limits, status)
    'create_invoice': ('ask', 'bills a customer through PayPal — the owner decides'),
    'check_invoices': ('allow', 'reads invoice statuses from PayPal'),
    'send_reminder': ('ask', 'contacts a customer through PayPal — the owner decides'),
    'record_payment': ('ask', 'changes the books in PayPal — the owner decides'),
}
INVOICE_TOOLS = {'create_invoice', 'check_invoices', 'send_reminder', 'record_payment'}
MAX_PAYMENT = 200  # default hard limit; Settings.max_payment overrides it
# Classic payment scams: paying in gift cards, prepaid cards or crypto to someone who asked for it.
SCAM = re.compile(r'(?i)gift\s*cards?|prepaid\s*cards?|itunes|google\s*play\s*cards?|steam\s*cards?|bitcoin|crypto|USDT|ギフトカード|プリペイド|電子マネー')


PAY_WORDS = re.compile(r'(?i)\b(?:pay|paid|payment|send|transfer|buy)\b|払|送金|購入')


def check_tool_rules(call: ToolCall, max_payment: float = MAX_PAYMENT, context: str = '') -> tuple[str, str, str]:
    """context = the owner's message in this turn (redacted): "pay them in gift cards" counts even if the tool call
    itself leaves the gift cards out."""
    """Return (action, rule, reason) for one tool call. Pure function: easy to test and to read."""
    name, args = call.name, call.arguments
    if name not in TOOL_RULES:
        return 'block', 'tools.unknown', f'"{name}" is not an allowed tool'
    if '_unparsed' in args:
        return 'block', 'tools.bad_arguments', 'the tool arguments were not valid JSON'
    action, reason = TOOL_RULES[name]
    if name == 'save_memory':  # credentials and card numbers never go into memory, whether redacted or not
        blob = json.dumps(args, ensure_ascii=False)
        if re.search(r'\[(?:SECRET|CARD)_\d+\]', blob) or mask_secrets(blob) != blob:
            return 'block', 'memory.no_secrets', 'looks like a password, API key or card number — not stored'
    if name == 'read_note':
        path = str(args.get('path', ''))
        if path.startswith('/') or '..' in Path(path).parts or '\\' in path:
            return 'block', 'memory.path', 'only notes inside the memory folder can be read'
    if name == 'make_payment':
        try:
            amount = float(args.get('amount', 0))
        except (TypeError, ValueError):
            return 'block', 'spend.bad_amount', 'the amount is not a number'
        if amount <= 0:
            return 'block', 'spend.bad_amount', 'the amount must be positive'
        what, payee = args.get('what', '?'), args.get('payee', '?')
        asked_in_scam_way = bool(SCAM.search(context or '') and PAY_WORDS.search(context or ''))
        if SCAM.search(f'{what} {payee} {args.get("currency", "")}') or asked_in_scam_way:  # the clearer reason goes first
            return 'block', 'spend.scam_pattern', f'paying "{what}" to "{payee}" looks like a known scam (gift cards / prepaid / crypto)'
        if amount > max_payment:
            return 'block', 'spend.limit', f'{amount:g} is over the hard limit of {max_payment:g}'
        return 'ask', 'spend.ask_owner', f'pay {amount:g} {args.get("currency", "")} for "{what}" to "{payee}"'.replace('  ', ' ')
    if name == 'send_message':
        return 'ask', 'outbound.ask_owner', f'send a message to {args.get("to", "?")}'
    return action, f'tools.{action}', reason


# What a reply may only say when a tool in the same turn did it. Sending and paying are never done inside a turn
# (they wait for the owner), so a reply that says "sent" or "paid" is always wrong.
CLAIMS = [
    ('save_memory', re.compile(r"(?i)(?:^|[.!?:—–]\s*)saved\b|\b(?:i(?:'ve| have) (?:just )?(?:saved|noted|stored|recorded|logged)|(?:saved|stored|logged) "
                               r"(?:it|this|that|to your|in your|under|as(?! (?:much|many|well|long|far|soon)))|noted (?:it|this|that) (?:down|in your)|(?:it's|it is) (?:saved|in your notes)"
                               r"|i(?:'ll| will) (?:remember (?:it|this|that)|keep (?:it|this|that) in mind))")),
    ('add_task', re.compile(r"(?i)\b(?:i(?:'ll| will) remind you|i(?:'ve| have) (?:added|set)(?: up)? (?:a |the )?(?:task|reminder)"
                            r"|added (?:it |this |that )?(?:as )?(?:a |to your )?task|(?:set|scheduled) (?:a|the) reminder"
                            r"|flag(?:ged)? (?:this|it) in (?:the|your) task list)")),
    ('send_message', re.compile(r"(?i)\b(?:i(?:'ve| have) (?:sent|emailed)|(?:has|have) been (?:sent|emailed)|(?:email|message) (?:was|is now) sent)\b")),
    ('make_payment', re.compile(r"(?i)\b(?:i(?:'ve| have) paid|(?:has|have) been paid|payment (?:is|was|has been) (?:made|completed|sent|done))\b")),
    # "queued for your approval" is true only when a tool in this turn was queued (done then holds 'queue')
    ('queue', re.compile(r"(?i)\bi(?:'ve| have) (?:just )?queued\b|\b(?:has|have) been queued\b|\b(?:is|it's|are|will be) (?:now )?queued\b"
                         r"|(?:^|[.!?:—–]\s*)queued\b|\bqueued (?:for your approval|as (?:a |message |payment |approval )?[A-Z]-\d+)"
                         r"|\b(?:is|it's|are|now) (?:waiting|pending) (?:for )?your approval\b|\bawaiting your approval\b")),
    ('complete_task', re.compile(r"(?i)\bi(?:'ve| have|'ll| will) (?:also |now |just |go ahead and )?mark(?:ed)?\b[^.!?\n]{0,40}?\b(?:done|complete(?:d)?|finished)\b"
                                 r"|\b(?:is|has been) (?:now )?marked (?:as )?(?:done|complete(?:d)?|finished)\b")),
    # Invoices go out only after the owner approves, so "I've sent the invoice" inside a turn is never true. Only the
    # first person is a claim: "invoice #0007 was sent on Monday" is a status the reply may report (checked with PayPal).
    ('create_invoice', re.compile(r"(?i)\bi(?:'ve| have) (?:just |now |already )?(?:sent|issued|created|emailed) (?:the |an |a |your |their )?"
                                  r"(?:\w+ )?invoice\b|\bi(?:'ve| have) (?:just |now |already )?billed\b")),
    ('send_reminder', re.compile(r"(?i)\bi(?:'ve| have) (?:just |now |already )?sent (?:a |the |them a |her a |him a )?(?:friendly |payment )?reminder\b")),
    ('record_payment', re.compile(r"(?i)\bi(?:'ve| have) (?:just |now |already )?(?:recorded|logged|entered|marked)\b[^.!?\n]{0,40}?"
                                  r"\b(?:payments?|cash|transfers?|deposits?|as paid)\b"
                                  r"|\b(?:payment|cash|transfer)\b[^.!?\n]{0,80}\b(?:has been|have been|is now) (?:recorded|logged|entered)\b"
                                  # "Recorded ¥9,000 from Ikoi Nursery …" (money words in the sentence: "Recorded best day: 412" is not one)
                                  r"|(?:^|[.!?:—–]\s*)(?:recorded|logged)\b(?=[^.!?\n]*(?:[¥$€£]\s?\d|\bpayment|\bpaid\b|\btransfer|\bcash\b|\binvoice))")),
]
ALWAYS_UNBACKED = {'send_message', 'make_payment', 'create_invoice', 'send_reminder', 'record_payment'}  # never done inside a turn


NOT_DONE = {'save_memory': 'nothing was saved — passwords and card numbers are never kept in your notes',
            'add_task': 'no task was added', 'send_message': 'nothing was sent — messages go out only after you approve them',
            'make_payment': 'nothing was paid — payments happen only after you approve them',
            'complete_task': 'no task was marked done',
            'queue': 'nothing was queued for your approval — ask again and it will wait in the approvals panel',
            'create_invoice': 'no invoice was sent — invoices go out through PayPal only after you approve them',
            'send_reminder': 'no reminder was sent — reminders go out only after you approve them',
            'record_payment': 'nothing was recorded in PayPal — a payment is recorded only after you approve it'}
INVOICE_CLAIMS = {'create_invoice', 'send_reminder', 'record_payment'}  # checked against the records (Money.claim_holds)
SENTENCE = re.compile(r'[^.!?\n]+[.!?]?')


def unbacked_claims(text: str, done: set[str], money_words: re.Pattern | None = None) -> list[str]:
    """Tools the reply claims were used that did not run (done = tools allowed and run in this turn, plus 'queue' when
    something was queued for the owner). money_words: sentences about invoices or customers are checked against PayPal
    instead (money.verify), so "invoice #0007 has been paid" there is not read as the owner paying someone."""
    found = []
    for sentence in (m.strip() for m in SENTENCE.findall(text or '')):
        hits = [tool for tool, rx in CLAIMS if (tool not in done or tool in ALWAYS_UNBACKED) and rx.search(sentence)
                and not (money_words and tool in ('send_message', 'make_payment') and money_words.search(sentence))]
        if 'record_payment' in hits:  # "I've recorded the ¥9,000 transfer" is about PayPal, not a note in memory
            hits = [t for t in hits if t != 'save_memory']
        found += [t for t in hits if t not in found]
    return [tool for tool, _ in CLAIMS if tool in found]


class Approvals:
    """The approval queue. Only the owner's controls (CLI or web UI) call decide(); no tool can."""

    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or utcnow

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else []

    def _save(self, items: list[dict]) -> None:
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.path)

    def request(self, tool: str, arguments: dict, reason: str, receipt: str) -> str:
        items = self._load()
        aid = f'A-{len(items) + 1:03d}'
        items.append(dict(id=aid, tool=tool, arguments=arguments, reason=reason, receipt=receipt, status='pending',
                          created=self.clock().isoformat(timespec='seconds')))
        self._save(items)
        return aid

    def list(self, status: str | None = 'pending') -> list[dict]:
        return [a for a in self._load() if status is None or a['status'] == status]

    def finish(self, aid: str, outcome: str, result: str) -> None:
        """What happened after an approval: outcome 'done' or 'failed', and the words the owner sees."""
        items = self._load()
        for a in items:
            if a['id'] == aid:
                a['outcome'], a['result'] = outcome, result[:500]
                self._save(items)
                return
        raise KeyError(aid)

    def decide(self, aid: str, approve: bool) -> dict:
        items = self._load()
        for a in items:
            if a['id'] == aid:
                if a['status'] != 'pending':
                    raise ValueError(f'{aid} was already {a["status"]}')
                a['status'] = 'approved' if approve else 'rejected'
                a['decided'] = self.clock().isoformat(timespec='seconds')
                self._save(items)
                return a
        raise KeyError(aid)


# ---------------------------------------------------------------- the guard

def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    text = json.dumps(messages, ensure_ascii=False)
    cjk = len(re.findall(r'[぀-ヿ㐀-鿿]', text))
    return int(cjk * 1.0 + (len(text) - cjk) / 3.5) + max_tokens


class Guard:
    def __init__(self, settings: Settings, ledger: UsageLedger, audit: Audit, approvals: Approvals, clock=None):
        self.settings, self.ledger, self.audit, self.approvals = settings, ledger, audit, approvals
        self.redactor = Redactor(settings.private_terms)
        self.clock = clock or utcnow

    def _day_start(self) -> datetime:
        now = self.clock().astimezone(ZoneInfo(self.settings.timezone))
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def before_model(self, messages: list[dict], vault: Vault, task: str, max_tokens: int) -> list[dict]:
        """Budget check, then a redacted copy of the messages. Raises Blocked when the daily budget is spent."""
        used_tokens, used_usd = self.ledger.totals_since(self._day_start())
        estimate = estimate_tokens(messages, max_tokens)
        model = MODELS[self.settings.routes.get(task, self.settings.routes['chat'])[0]]
        est_usd = estimate / 1e6 * max(model.usd_in_per_m, model.usd_out_per_m)
        if used_tokens + estimate > self.settings.daily_token_budget or used_usd + est_usd > self.settings.daily_usd_budget:
            reason = (f'daily budget reached: {used_tokens:,} tokens / ${used_usd:.4f} used, this call needs about '
                      f'{estimate:,} tokens (limits {self.settings.daily_token_budget:,} tokens / ${self.settings.daily_usd_budget:.2f})')
            receipt = self.audit.log('model_call', 'block', 'budget.daily', reason, task=task)
            raise Blocked('budget.daily', reason, receipt)

        found = Counter()
        safe = []
        for m in messages:
            m = dict(m)
            if isinstance(m.get('content'), str):
                m['content'], c = self.redactor.redact(m['content'], vault)
                found += c
            if m.get('tool_calls'):  # arguments of earlier tool calls go back to the model too
                calls = []
                for tc in m['tool_calls']:
                    fn = dict(tc['function'])
                    fn['arguments'], c = self.redactor.redact(fn.get('arguments') or '', vault)
                    found += c
                    calls.append({**tc, 'function': fn})
                m['tool_calls'] = calls
            safe.append(m)
        kinds = ', '.join(f'{k}×{n}' for k, n in sorted(found.items()))
        self.audit.log('model_call', 'allow', 'privacy.redact' if found else 'budget.ok',
                       f'redacted before sending: {kinds}' if found else 'within budget, nothing personal found',
                       task=task, model=model.id, redacted=dict(found), estimate_tokens=estimate)
        return safe

    def after_model(self, result: LLMResult, vault: Vault, withhold: bool = False) -> tuple[str, list[ToolCall]]:
        """Put the real values back for the owner's side (reply text and tool arguments)."""
        calls = [ToolCall(c.id, c.name, self.redactor.restore_obj(c.arguments, vault, withhold)) for c in result.tool_calls]
        return self.redactor.restore(result.content, vault, withhold), calls

    money = None  # kioku.money.Money when PayPal is connected (set by Kioku)

    def check_tool(self, call: ToolCall, redacted_args: dict, context: str = '') -> Decision:
        if call.name in INVOICE_TOOLS and '_unparsed' not in call.arguments:
            # decided on the owner's side with the real names: the customer book, the limits, PayPal's own status
            if self.money is None:
                action, rule, reason = 'block', 'paypal.off', 'PayPal is not connected'
            else:
                try:
                    action, rule, reason = self.money.check(call.name, call.arguments, context)
                except Exception as e:  # PayPal unreachable or refusing: nothing is queued on a guess
                    action, rule, reason = 'block', 'paypal.unreachable', f'could not check with PayPal ({str(e)[:160]})'
        else:
            action, rule, reason = check_tool_rules(ToolCall(call.id, call.name, redacted_args), self.settings.max_payment,
                                                    context)
        receipt = self.audit.log('tool_call', action, rule, reason, tool=call.name)
        return Decision(action, rule, reason, receipt)
