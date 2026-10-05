"""The habits that make memory grow: day → week → month → topic notes → index.

- daily_journal   (nightly, Lightning)   the day's conversation + saved notes → daily note; topic histories
- weekly_review   (Sunday, Super)        the week's daily notes → weekly note; topic "current state"; index facts
- monthly_review  (1st of month, Super)  the month's weekly notes → monthly note
- morning_briefing (morning, Lightning)  yesterday's open threads + tasks + on-this-day → a short briefing

Each skill asks for JSON and renders the Markdown itself, so the notes keep the same shape every day.
All model calls go through the guard (budget, redaction, receipts).
"""
from __future__ import annotations

from datetime import date, timedelta

from .memory import (Memory, add_to_section, new_topic, real_topic, section, set_section, slugify, tokens, week_days,
                     week_id)


def bullets(items, prefix: str = '- ') -> str:
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    return '\n'.join(prefix + x for x in items) if items else '- (none)'


def topic_title(memory: Memory, slug: str) -> str:
    first = memory.read(f'topics/{slug}.md').splitlines()[:1]
    return first[0].lstrip('# ').strip() if first and first[0].startswith('#') else slug


def similar(a: str, b: str) -> bool:
    """Roughly the same line (word overlap of at least half): used to avoid saying one thing twice in a topic."""
    ta, tb = set(tokens(a)), set(tokens(b))
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


def topic_names(memory: Memory) -> str:
    return ', '.join(topic_title(memory, s) for s in memory.topics()) or '(none yet)'


# ------------------------------------------------------------------ daily

JOURNAL_SYSTEM = """You write the owner's daily journal from what the owner said today, the notes saved today, and the day's records
(approvals and what the guard stopped). The secretary's replies are left out on purpose: memory is built from the owner's
own words and from what actually happened, never from the assistant's prose.
Write in the owner's language and speak to the owner as "you" (never "he", "she" or "the owner"). Be concrete: things, numbers, people, decisions. Do not add anything that is not in the input.
Return only a JSON object with these keys:
"key_points": 1-3 short sentences, the most important things today
"what_happened": what happened in the owner's day, short lines in time order, each starting with "HH:MM " (the owner's events, not the secretary's replies)
"owner_words": 0-3 lines worth keeping in the owner's own words — feelings, values, decisions — copied exactly; not routine numbers
"insights": things learned today (list, may be empty)
"open_questions": things the owner still has to decide or solve (list, may be empty)
"tomorrow": next steps (list, may be empty)
"topics": at most 3 items {{"topic": "1-3 word subject name", "update": "one line on what changed today"}} for subjects worth following over time. Prefer broad, lasting subjects (e.g. sales, staff, ovens, flour, a product) and reuse an existing topic whenever the subject matches — daily numbers go to the existing sales topic; create a new one only for a new subject that will come up again; never a one-day event or a catch-all such as "daily notes". Existing topics: {topics}
"mood": one or two words for the owner's mood today, or "" if unclear"""

JOURNAL_KEYS = ['key_points', 'what_happened', 'owner_words', 'insights', 'open_questions', 'tomorrow', 'topics', 'mood']


def daily_journal(k, day: date) -> str | None:
    """Write daily/<day>.md. Keeps the notes saved during the day. Returns the path, or None if nothing happened."""
    m = k.memory
    rel = m.daily(day)
    existing = m.read(rel)
    notes = section(existing, 'Notes')
    turns = [t for t in m.log_for(day) if t.get('role') == 'owner']
    if not turns and not notes:
        return None
    said = '\n'.join(f'{t["ts"][11:16]} {t["text"][:800]}' for t in turns)[-12000:]
    user = (f'Date: {day.isoformat()} ({day.strftime("%A")})\n\nWhat the owner said today:\n{said or "(nothing)"}\n\n'
            f'Notes saved today:\n{notes or "(none)"}\n\nRecords today:\n{records_for(k, day) or "(none)"}')
    data, _ = k.ask_json(task='journal', system=JOURNAL_SYSTEM.format(topics=topic_names(m)), user=user, keys=JOURNAL_KEYS)

    topics = [dict(t, topic=m.resolve_topic(str(t['topic']).strip()))
              for t in data.get('topics') or [] if isinstance(t, dict) and real_topic(t.get('topic'))]
    body = (f'# {day.isoformat()} ({day.strftime("%A")})\n\n'
            f'## Key points\n{bullets(data.get("key_points"))}\n\n'
            f'## What happened\n{bullets(data.get("what_happened"))}\n\n'
            f'## Owner\'s words\n{bullets(data.get("owner_words"), "> ")}\n\n'
            f'## Insights\n{bullets(data.get("insights"))}\n\n'
            f'## Open questions\n{bullets(data.get("open_questions"))}\n\n'
            f'## Tomorrow\n{bullets(data.get("tomorrow"))}\n\n'
            f'## Topics\n{bullets([f"[[{slugify(t["topic"])}]] {t.get("update", "")}" for t in topics])}\n')
    if str(data.get('mood', '')).strip():
        body += f'\n## Mood\n- {str(data["mood"]).strip()}\n'
    for keep in ('Morning briefing', 'Notes'):
        if section(existing, keep):
            body = set_section(body, keep, section(existing, keep))
    m.write(rel, body)

    for t in topics:
        trel = m.topic(t['topic'])
        tbody = m.read(trel) or new_topic(str(t['topic']).strip())
        update = str(t.get('update', '')).strip()
        same_day = [s.split(']]', 1)[-1] for s in section(tbody, 'History').splitlines() if f'[[{day.isoformat()}]]' in s]
        if any(similar(update, s) for s in same_day):  # a note saved during the day already says it
            continue
        m.write(trel, add_to_section(tbody, 'History', f'- [[{day.isoformat()}]] {update}'))
    return rel


def records_for(k, day: date) -> str:
    """The day's facts from the receipts: approvals asked for and decided, and what the guard refused."""
    d, lines = day.isoformat(), []
    for a in k.approvals.list(None):
        what = ', '.join(f'{key}={a["arguments"][key]}' for key in ('what', 'amount', 'currency', 'payee', 'to', 'subject')
                         if key in a['arguments'])
        if a.get('created', '')[:10] == d:
            lines.append(f'{a["created"][11:16]} {a["tool"]} {a["id"]} waited for the owner ({what})')
        if (a.get('decided') or '')[:10] == d:
            lines.append(f'{a["decided"][11:16]} the owner {a["status"]} {a["id"]}')
    for r in k.audit.rows():
        if r.get('ts', '')[:10] == d and r.get('event') == 'tool_call' and r.get('decision') == 'block':
            lines.append(f'{r["ts"][11:16]} Kioku Guard refused {r.get("tool")}: {r.get("rule")} — {r.get("reason", "")[:120]}')
    return '\n'.join(sorted(lines))


# ------------------------------------------------------------------ weekly

WEEKLY_SYSTEM = """You write the owner's weekly review from one week of daily journals. Find what matters: progress, problems,
patterns across days, decisions, and what to do next week. Copy numbers, names and days exactly as the journals give
them; do not turn one day's detail into a rule, and do not guess causes the journals do not state. Do not invent.
Write in the owner's language and speak to the owner as "you" (never "he", "she" or "the owner"). Return only a JSON object with these keys:
"summary": 2-3 sentences
"highlights": list
"patterns": things that recur across days (list)
"decisions": choices the owner actually stated this week (e.g. "we'll go back to the old sugar") — not numbers, routines, plans or things still waiting (list)
"didnt_work": things tried that did not work (list)
"next_week": 3-5 things that matter next week — not routine chores (list)
"topics": list of {"topic": "existing topic name", "current_state": ["1-3 lines on where it stands now"], "decision": "only a decision the owner actually made, else empty", "didnt_work": "only if something was tried and failed, else empty"}
"merge_topics": topics that turned out to be the same subject, as [{"into": "slug to keep", "from": ["slugs to fold into it"]}] — only when clearly the same subject; may be empty
"index_facts": the full, updated list of lasting facts — things still true and useful months from now: people and their roles and days, suppliers and prices, schedules, products and prices, records and milestones (best day, best month, with dates), rules the owner set. Keep what is still true, replace what changed, add new ones; no one-off events or moods; each under 20 words, written as a plain fact without pronouns (e.g. "Aiko works Tue, Wed, Thu and Sat from 1 Sep"); at most 15"""

WEEKLY_KEYS = ['summary', 'highlights', 'patterns', 'decisions', 'didnt_work', 'next_week', 'topics', 'merge_topics', 'index_facts']


def weekly_review(k, wid: str) -> str | None:
    m = k.memory
    days = [d for d in week_days(wid) if m.exists(m.daily(d))]
    if not days:
        return None
    journals = '\n\n'.join(f'=== {d.isoformat()} ===\n{m.read(m.daily(d))[:2500]}' for d in days)
    topics = '\n'.join(f'- {s} ({topic_title(m, s)}): {section(m.read(f"topics/{s}.md"), "Current state")[:300]}'
                       for s in m.topics())
    facts = section(m.read('index.md'), 'Lasting facts')
    user = (f'Week {wid} ({days[0].isoformat()} to {days[-1].isoformat()})\n\nCurrent lasting facts:\n{facts or "(none)"}\n\n'
            f'Topics and their current state:\n{topics or "(none)"}\n\nDaily journals:\n{journals}')
    money = money_block(k)
    has_money = money.startswith(('Overdue:', 'Not due yet:'))
    if has_money:  # what customers still owe, so "next week" can include chasing it
        user += '\n\nInvoices still open (read from PayPal now; for next_week, do not copy into lasting facts):\n' + money
    data, _ = k.ask_json(task='weekly_review', system=WEEKLY_SYSTEM, user=user, keys=WEEKLY_KEYS, max_tokens=8000)
    merged = []
    for g in (data.get('merge_topics') or [])[:10]:
        if isinstance(g, dict) and isinstance(g.get('from'), list):
            into = slugify(str(g.get('into', '')))
            merged += [f'{src} → [[{into}]]' for src in m.merge_topics(into, [str(x) for x in g['from']][:6])]

    rel = m.weekly(wid)
    m.write(rel, (f'# {wid}\n\n## Summary\n{data.get("summary", "").strip() or "(none)"}\n\n'
                  f'## Highlights\n{bullets(data.get("highlights"))}\n\n## Patterns\n{bullets(data.get("patterns"))}\n\n'
                  f'## Decisions\n{bullets(data.get("decisions"))}\n\n## What didn\'t work\n{bullets(data.get("didnt_work"))}\n\n'
                  f'## Next week\n{bullets(data.get("next_week"))}\n\n'
                  + (f'## Money (as PayPal stated it at the review)\n{money}\n\n' if has_money else '')
                  + (f'## Topics merged\n{bullets(merged)}\n\n' if merged else '')
                  + f'## Days\n{bullets([f"[[{d.isoformat()}]]" for d in days])}\n'))
    end = days[-1].isoformat()
    for t in data.get('topics') or []:
        if not isinstance(t, dict) or not real_topic(t.get('topic')):
            continue
        t = dict(t, topic=m.resolve_topic(str(t['topic']).strip()))
        trel = m.topic(t['topic'])
        tbody = m.read(trel) or new_topic(str(t['topic']).strip())
        state = t.get('current_state') or []
        tbody = set_section(tbody, 'Current state', bullets(state if isinstance(state, list) else [state]) + f'\n- (as of {end})')
        for key, sec in (('decision', 'Decisions'), ('didnt_work', "What didn't work")):
            if str(t.get(key, '') or '').strip():
                tbody = add_to_section(tbody, sec, f'- [[{end}]] {str(t[key]).strip()}')
        m.write(trel, tbody)
    refresh_index(m, facts=data.get('index_facts'), note=f'weekly review {wid}')
    return rel


# ------------------------------------------------------------------ monthly

MONTHLY_SYSTEM = """You write the owner's monthly review from the month's weekly reviews (plus the key points of days that no weekly
review covers yet). Show the shape of the month: what moved, what the numbers did, what kept coming back, lessons,
and what to focus on next month. Copy numbers exactly as the notes give them. Do not invent.
Write in the owner's language and speak to the owner as "you" (never "he", "she" or "the owner"). Return only a JSON object with these keys:
"summary": 3-4 sentences, starting with the month's headline numbers if the notes give them
"numbers": the month's key figures exactly as given — sales, records, prices, costs (list, may be empty)
"trends": list
"decisions": only choices the owner actually stated — not numbers, checks, tasks or plans (list)
"lessons": lessons this month's events actually taught, not general advice (list)
"next_month": 3-5 things to focus on (list)"""


def monthly_review(k, year: int, month: int) -> str | None:
    m = k.memory
    days = [date(year, month, d) for d in range(1, 32) if valid(year, month, d)]
    weeks = sorted({week_id(d) for d in days})
    notes = [(w, m.read(m.weekly(w))) for w in weeks if m.exists(m.weekly(w))]
    covered = {d for w, _ in notes for d in week_days(w)}
    loose = [(d, section(m.read(m.daily(d)), 'Key points')) for d in days if d not in covered and m.exists(m.daily(d))]
    loose = [(d, kp) for d, kp in loose if kp and kp != '- (none)']
    if not notes and not loose:
        return None
    user = f'Month {year}-{month:02d}\n\n' + '\n\n'.join(f'=== {w} ===\n{body[:3000]}' for w, body in notes)
    if loose:  # e.g. the last days of the month, before Sunday's weekly review has run
        user += '\n\n=== Days not yet in a weekly review ===\n' + '\n'.join(f'{d.isoformat()}:\n{kp}' for d, kp in loose)
    data, _ = k.ask_json(task='monthly_review', system=MONTHLY_SYSTEM, user=user,
                         keys=['summary', 'numbers', 'trends', 'decisions', 'lessons', 'next_month'], max_tokens=6000)
    rel = m.monthly(year, month)
    m.write(rel, (f'# {year}-{month:02d}\n\n## Summary\n{data.get("summary", "").strip() or "(none)"}\n\n'
                  f'## Numbers\n{bullets(data.get("numbers"))}\n\n## Trends\n{bullets(data.get("trends"))}\n\n## Decisions\n{bullets(data.get("decisions"))}\n\n'
                  f'## Lessons\n{bullets(data.get("lessons"))}\n\n## Next month\n{bullets(data.get("next_month"))}\n\n'
                  f'## Weeks\n{bullets([f"[[{w}]]" for w, _ in notes])}\n'))
    return rel


def valid(y: int, mth: int, d: int) -> bool:
    try:
        date(y, mth, d)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ morning

BRIEFING_SYSTEM = """You are Kioku, the owner's secretary. Write this morning's briefing in the owner's language, speaking to the owner as "you": at most 5 short
lines — what is still open from the last journal, open tasks, overdue invoices (if given, with the amounts exactly as PayPal
states them), and (if given) one thing from the same day in the past.
Use only the facts below. Never invent tasks, meetings, numbers or advice. If little is open, say so in one line.
Plain text, no headings."""


def money_block(k) -> str:
    """The open invoices as PayPal states them right now (read live; payments found on the way go into memory).
    '' without PayPal. Used by the briefing and the weekly review, so neither works from an old copy."""
    if not k.money.on:
        return ''
    try:
        k.check_invoices({'which': 'open'}, k.clock())  # refreshes the ledger from PayPal (and notes new payments)
    except Exception as e:  # noqa: BLE001 — a briefing without the money line beats no briefing
        return f'PayPal could not be read just now ({str(e)[:80]}).'
    return k.money.open_summary()


def morning_briefing(k, day: date) -> str:
    m = k.memory
    # the last journal within three days (after a day off, "yesterday" has no note)
    last = next((d for d in (day - timedelta(days=i) for i in (1, 2, 3)) if section(m.read(m.daily(d)), 'Key points')), None)
    body = m.read(m.daily(last)) if last else ''
    label = f'Last journal ({last.isoformat()}, {last.strftime("%A")})' if last else 'Last journal'
    tasks, past = m.open_tasks()[:10], m.on_this_day(day)
    money = money_block(k)
    open_money = money if money.startswith(('Overdue:', 'Not due yet:')) else ''
    if not body and not tasks and not past and not open_money:  # nothing to brief on: no model call, nothing made up
        text = 'Nothing carried over and no open tasks. Have a good day.'
    else:
        parts = [f'Today: {day.isoformat()} ({day.strftime("%A")})',
                 f'About the owner:\n{section(m.read("index.md"), "About the owner")[:1200] or "(no profile)"}',
                 f'{label} — key points:\n{section(body, "Key points") or "(none)"}',
                 f'{label} — next steps:\n{section(body, "Tomorrow") or "(none)"}',
                 f'{label} — open questions:\n{section(body, "Open questions") or "(none)"}',
                 'Open tasks:\n' + ('\n'.join(f'- {t}' for t in tasks) or '(none)')]
        if past:
            parts.append('On this day:\n' + '\n'.join(f'- {when}: {note[:300]}' for when, note in past))
        if open_money:
            parts.append('Invoices still open (read from PayPal just now):\n' + open_money)
        text, _, _ = k.ask([{'role': 'system', 'content': BRIEFING_SYSTEM}, {'role': 'user', 'content': '\n\n'.join(parts)}],
                           task='briefing', max_tokens=400, temperature=0.2, for_disk=True)
    rel = m.daily(day)
    m.write(rel, set_section(m.read(rel) or f'# {day.isoformat()} ({day.strftime("%A")})\n', 'Morning briefing', text))
    return text


# ------------------------------------------------------------------ index

def refresh_index(m: Memory, facts=None, note: str = '') -> str:
    """index.md = the owner's own profile (never edited by Kioku) + lasting facts + topics + recent weeks."""
    old = m.read('index.md')
    unlabel = lambda text: '\n'.join(s for s in text.splitlines() if not s.startswith('_')).strip()  # noqa: E731
    profile = unlabel(section(old, 'About the owner')) or m.read('profile.md').strip() or '- (the owner has not written a profile yet)'
    if facts is None:
        facts_text = unlabel(section(old, 'Lasting facts')) or '- (none yet)'
    else:
        facts_text = bullets([f for f in facts if str(f).strip()][:15])
    topic_lines = []
    for slug in m.topics():
        body = m.read(f'topics/{slug}.md')
        state = [s[2:] for s in section(body, 'Current state').splitlines() if s.startswith('- ') and not s.startswith('- (as of')]
        line = state[0] if state else '(not reviewed yet)'
        topic_lines.append(f'[[{slug}]] — {line[:110] + "…" if len(line) > 110 else line}')
    weeks = sorted(m.files('weekly'))[-4:]
    week_lines = [f'[[{w[7:-3]}]] — {section(m.read(w), "Summary")[:160]}' for w in reversed(weeks)]
    body = (f'# Index\n_Updated by the {note or "index refresh"}._\n\n'
            f'## Lasting facts\n_Kept current by the weekly review — newer than the profile below._\n{facts_text}\n\n'
            f'## About the owner\n_Written by the owner; later changes are in the lasting facts._\n{profile}\n\n'
            f'## Topics\n{bullets(topic_lines)}\n\n## Recent weeks\n{bullets(week_lines)}\n')
    m.write('index.md', body)
    return body
