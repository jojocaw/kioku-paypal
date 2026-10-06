import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kioku.agent import Kioku
from kioku.config import Settings
from kioku.llm import LLMResult, ToolCall

TZ = ZoneInfo('Asia/Tokyo')


class FakeLLM:
    """A scripted model. Each chat() takes the next response: a string, (text, [(tool, args), ...]), or a function
    of the messages. Everything it was sent is kept in .sent so tests can check what the model saw."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def chat(self, messages, *, task, tools=None, tool_choice='auto', max_tokens=1024, temperature=None):
        self.sent.append(dict(messages=json.loads(json.dumps(messages)), task=task, tool_choice=tool_choice, tools=bool(tools),
                              tool_names=[t['function']['name'] for t in tools or []]))
        r = self.responses.pop(0)
        if callable(r):
            r = r(messages)
        text, calls = r if isinstance(r, tuple) else (r, [])
        return LLMResult(content=text, tool_calls=[ToolCall(f'call_{i}', n, a) for i, (n, a) in enumerate(calls)],
                         model='fake-model', task=task, prompt_tokens=120, completion_tokens=30, cost_usd=0.0, latency_ms=5)

    def seen_text(self) -> str:
        return json.dumps(self.sent, ensure_ascii=False)


@pytest.fixture
def make_kioku(tmp_path: Path):
    def make(*responses, now=datetime(2026, 10, 5, 9, 30, tzinfo=TZ), **settings):
        s = Settings(api_key='test-key', data_dir=tmp_path / 'data', timezone='Asia/Tokyo',
                     private_terms=('Aiko Tanaka', 'Ren', '花子'), **settings)
        fake = FakeLLM(*responses)
        k = Kioku(s, llm=fake, clock=lambda: now)
        return k, fake
    return make
