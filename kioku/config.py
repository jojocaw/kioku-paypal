"""Settings and the model catalogue.

Everything is read from environment variables (or a local .env file). The API key is never printed:
it is excluded from repr() and never written to any log.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Model:
    id: str
    usd_in_per_m: float   # price per 1M input tokens (Token Factory public catalogue, 2026-10-04)
    usd_out_per_m: float  # price per 1M output tokens
    thinking_switch: bool  # accepts chat_template_kwargs.enable_thinking (thinking otherwise leaks into content)


MODELS = {
    'lightning': Model('nvidia/Nemotron-3_5-Lightning', 0.06, 0.24, thinking_switch=True),
    'super': Model('nvidia/nemotron-3-super-120b-a12b', 0.30, 0.90, thinking_switch=False),
    'ultra': Model('nvidia/Nemotron-3-Ultra-550b-a55b', 1.00, 3.00, thinking_switch=True),
}

# Which model does which job. Lightning (thinking off) handles every everyday turn and tool call;
# Super handles the reviews that need to read a whole week or month and reason about it.
ROUTES = {
    'chat': ('lightning', False),
    'journal': ('lightning', False),
    'briefing': ('lightning', False),
    'weekly_review': ('super', True),
    'monthly_review': ('super', True),
}


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE per line). Variables already set in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():  # -sig: files saved by Notepad start with a BOM
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str = 'https://api.tokenfactory.nebius.com/v1'
    data_dir: Path = Path('data/default')
    timezone: str = 'UTC'
    daily_token_budget: int = 400_000   # hard stop per profile per day (input + output tokens)
    daily_usd_budget: float = 0.50      # hard stop per profile per day
    private_terms: tuple[str, ...] = ()  # names etc. that must never reach a model (redacted before every call)
    max_payment: float = 200            # payments above this are refused outright (in the owner's currency)
    routes: dict = field(default_factory=lambda: dict(ROUTES))
    # PayPal (Invoicing v2). Without keys the invoice tools are not offered at all.
    paypal_client_id: str = field(default='', repr=False)
    paypal_secret: str = field(default='', repr=False)
    paypal_base_url: str = 'https://api-m.sandbox.paypal.com'
    currency: str = 'JPY'
    max_invoice: float = 300_000        # invoices above this are refused outright

    @classmethod
    def from_env(cls, root: Path | None = None, **overrides) -> 'Settings':
        root = root or Path.cwd()
        load_dotenv(root / '.env')
        load_dotenv(root / 'paypal.env')
        env = os.environ
        values = dict(
            api_key=env.get('NEBIUS_API_KEY', ''),
            base_url=env.get('KIOKU_BASE_URL', cls.base_url),
            data_dir=Path(env.get('KIOKU_DATA_DIR', root / 'data' / 'default')),
            timezone=env.get('KIOKU_TIMEZONE', 'UTC'),
            daily_token_budget=int(env.get('KIOKU_DAILY_TOKENS', cls.daily_token_budget)),
            daily_usd_budget=float(env.get('KIOKU_DAILY_USD', cls.daily_usd_budget)),
            private_terms=tuple(t.strip() for t in env.get('KIOKU_PRIVATE_TERMS', '').split(',') if t.strip()),
            max_payment=float(env.get('KIOKU_MAX_PAYMENT', cls.max_payment)),
            paypal_client_id=env.get('PAYPAL_CLIENT_ID', ''),
            paypal_secret=env.get('PAYPAL_CLIENT_SECRET', ''),
            paypal_base_url=env.get('PAYPAL_BASE_URL', cls.paypal_base_url),
            currency=env.get('KIOKU_CURRENCY', cls.currency).upper(),
            max_invoice=float(env.get('KIOKU_MAX_INVOICE', cls.max_invoice)),
        )
        values.update(overrides)
        return cls(**values)
