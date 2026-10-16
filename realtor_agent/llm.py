"""Optional: turn a free-text buyer request into Criteria with Claude.

Needs `pip install anthropic` and ANTHROPIC_API_KEY. The rest of the project
does not depend on this module.
"""
from __future__ import annotations

import json

from .matcher import Criteria

SCHEMA = {
    "type": "object",
    "properties": {
        "budget_usd": {"type": ["integer", "null"]},
        "district": {"type": ["string", "null"]},
        "rooms": {"type": ["integer", "null"]},
        "must_have": {"type": "array", "items": {"type": "string"}},
        "max_metro_minutes": {"type": ["integer", "null"]},
    },
    "required": ["budget_usd", "district", "rooms", "must_have", "max_metro_minutes"],
    "additionalProperties": False,
}


def parse_request(text: str, model: str = "claude-opus-5") -> Criteria:
    import anthropic  # imported lazily so the core stays dependency-free

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system="Extract a property search from the buyer's message. Use null when not stated. Never invent a budget.",
        messages=[{"role": "user", "content": text}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return Criteria(**data)
