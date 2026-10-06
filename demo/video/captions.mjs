// Captions of the PayPal video that depend on what happened in the take, kept apart from record-paypal.mjs (which needs
// Playwright and a browser) so that tests/test_video.py can check them against the running app with a scripted model.
// A reply is "right" only when the guard checked it against PayPal and every claim matched — never because of how it
// sounds. A slip is called a slip, with whether the guard caught it.

const yen = n => `¥${Math.round(Number(n) || 0).toLocaleString('en-US')}`;

// What the guard did with a reply's claims about money (the events /api/chat returns): receipts for claims that matched
// PayPal, and the correction it added for the ones that did not. number: one invoice to ask about ("0006").
export function moneyChecks(events = [], number = '') {
  const about = e => !number || String(e.detail || '').includes(`#${number}`);
  const checked = events.filter(e => e.rule === 'honesty.paypal_checked');
  const corrected = events.filter(e => e.rule === 'honesty.paypal_corrected');
  return { checked: checked.length, corrected: corrected.length > 0, checkedThis: checked.some(about), correctedThis: corrected.some(about) };
}

// Does a reply sound as if the customer has paid? Only a safety net for a slip the guard did not read; it never makes a
// reply right.
const SOUNDS_PAID = /\b(?:paid|settled|cleared|received|in full)\b|\bnothing\s+(?:is\s+)?(?:(?:still|remaining|left)\s+)*(?:due|owed|outstanding)\b|\bno\s+(?:outstanding\s+|remaining\s+)?balance\b|\bowes?\s+(?:you\s+)?nothing\b|支払い?済|入金済|精算済|完済/i;
const SOUNDS_UNPAID = /\b(?:not|never|unpaid|outstanding|overdue|owes?|owing)\b|n['’]t\b|\bstill\s+(?:due|has|open)\b|未払い|未入金|まだ/i;
export const soundsPaid = text => SOUNDS_PAID.test(text || '') && !SOUNDS_UNPAID.test(text || '');

// The opening scene: the owner says a customer paid, and PayPal shows that customer's invoice still due (invoice: the
// row /api/invoices gave for it before the take). Returns { verdict, caption }; verdict is 'caught' (a slip, and the guard
// corrected it), 'right' (the guard checked the reply against PayPal and everything matched), 'missed' (the reply says
// they paid and the guard did nothing — that take is not for the video), or 'unchecked'.
export function sakuraCaption({ text = '', events = [], invoice = {} } = {}) {
  const who = invoice.customer || 'Hotel Sakura';
  const late = Number(invoice.late) || 0;
  const owed = `${yen(invoice.due ?? invoice.total)} still due${late ? `, ${late} day${late === 1 ? '' : 's'} late` : ''}`;
  const g = moneyChecks(events, invoice.number || '');
  if (g.correctedThis) {
    return { verdict: 'caught', caption: `Here the model slipped: PayPal shows ${who} with ${owed}. The guard caught it and put PayPal’s status in front of the owner.` };
  }
  if (g.corrected) {
    return { verdict: 'caught', caption: 'Here the model slipped. The guard caught it and put PayPal’s status in front of the owner.' };
  }
  if (g.checkedThis) {
    return { verdict: 'right', caption: `This time the model got it right — the guard checked the reply against PayPal: ${owed}. The receipt sits under the reply.` };
  }
  if (soundsPaid(text)) {
    return { verdict: 'missed', caption: `The model said ${who} had paid — wrong: PayPal shows ${owed}. This time the guard did not catch it.` };
  }
  const readPayPal = events.some(e => e.tool === 'check_invoices' || e.rule === 'money.check_first');
  return { verdict: 'unchecked', caption: readPayPal
    ? `Kioku read ${who}’s invoice in PayPal before answering; the guard found no claim about paying in the reply to check.`
    : 'This time the reply made no claim about who paid, so there was nothing for the guard to check.' };
}

// "Who still owes me money?": what the guard did with the answer. kind 'slip' | 'asked_again' | 'checked' | ''.
export function owesCaption(events = []) {
  const note = events.find(e => e.rule === 'honesty.paypal_corrected');
  if (note) {
    const n = (String(note.detail || '').match(/PayPal shows/g) || []).length || 1;
    return { kind: 'slip', caption: `The model slipped on ${n === 1 ? 'one invoice' : `${n} invoices`} — the guard added PayPal’s own status before the owner read it.` };
  }
  if (events.some(e => e.rule === 'money.check_first')) {
    return { kind: 'asked_again', caption: 'The model first answered without reading PayPal — so Kioku read PayPal itself and had it answer from PayPal’s numbers.' };
  }
  const { checked } = moneyChecks(events);
  if (checked) {
    return { kind: 'checked', caption: checked === 1 ? 'The guard checked the answer’s claim against PayPal, and it matches; the receipt sits under the reply.'
      : `The guard checked all ${checked} claims in the answer against PayPal, and they match; the receipts sit under the reply.` };
  }
  return { kind: '', caption: '' };
}

// A refusal, in one short line that fits the scene: no rule names (the receipt under the reply has them).
const REFUSED = {
  'invoice.note_contact': 'The guard refused it: no address goes into an invoice note.',
  'invoice.unknown_customer': 'The guard refused it: that customer isn’t in the book.',
  'invoice.which_customer': 'The guard refused it: more than one customer matches.',
  'invoice.limit': 'The guard refused it: the total is over the invoice limit.',
  'invoice.bad_items': 'The guard refused it: the items don’t make a valid bill.',
  'invoice.bad_amount': 'The guard refused it: that amount doesn’t fit the invoice.',
  'invoice.already_paid': 'The guard refused it: PayPal shows the invoice as paid.',
  'invoice.closed': 'The guard refused it: PayPal shows the invoice as closed.',
  'invoice.which': 'The guard refused it: it isn’t clear which invoice.',
  'invoice.unknown': 'The guard refused it: no invoice matches that.',
  'demo.shared_invoice': 'The guard refused it: that demo invoice is shared.',
  'action.not_asked': 'The guard refused it: the owner didn’t ask for that.',
  'paypal.unreachable': 'The guard refused it: PayPal couldn’t be checked.',
  'paypal.off': 'The guard refused it: PayPal isn’t connected.',
};
export const refusalCaption = event => REFUSED[event?.rule] || 'The guard refused it — nothing ran in PayPal.';
export const refusalRules = () => Object.keys(REFUSED);

// The bar above the screenshot of PayPal's payment page: never the page's address (anyone could open it), just what it is.
export const shotLabel = () => 'PayPal checkout link';
