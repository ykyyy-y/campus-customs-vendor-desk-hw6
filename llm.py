"""The single place HW 6 names a model or opens a client.

Every AI call in this assignment is built here - ``build_model()`` for the PydanticAI
agent, ``client()`` for the raw token-counting calls - so there is exactly one line in
the project that names a model, and it names **gpt-6-luna**. Access is through the
Portkey gateway with ``PORTKEY_API_KEY``; the key is read into memory and is never
printed, logged, or written to any output file.

The rate card and the cost arithmetic live here too, next to the model they price, and
they are read from ``data/prices.json`` rather than typed in anywhere.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from portkey_ai import PORTKEY_GATEWAY_URL
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

#: The only model this assignment may use.
MODEL_NAME = "gpt-6-luna"

HW_ROOT = Path(__file__).resolve().parent
# A grader cloning this repo alone puts .env beside the homework; in the course repo it
# sits one level up, shared across assignments. Both work without editing anything.
ENV_CANDIDATES = (HW_ROOT / ".env", HW_ROOT.parent / ".env")
load_dotenv(next((p for p in ENV_CANDIDATES if p.exists()), ENV_CANDIDATES[0]))


def _require_key() -> str:
    api_key = os.getenv("PORTKEY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "PORTKEY_API_KEY is not set. Copy .env.example to .env and add your key. "
            "Looked in: " + ", ".join(str(p) for p in ENV_CANDIDATES)
        )
    return api_key


@lru_cache(maxsize=1)
def client() -> OpenAI:
    return OpenAI(api_key=_require_key(), base_url=PORTKEY_GATEWAY_URL)


@lru_cache(maxsize=1)
def build_model() -> OpenAIResponsesModel:
    """The PydanticAI model the vendor desk agent runs on.

    The Responses API, not chat/completions: function tools fail on Portkey's
    chat/completions path for this model, and this agent is tool-driven.
    """
    return OpenAIResponsesModel(
        MODEL_NAME,
        provider=OpenAIProvider(
            api_key=_require_key(), base_url=PORTKEY_GATEWAY_URL
        ),
    )


@lru_cache(maxsize=1)
def prices() -> dict[str, float]:
    """The ``gpt-6-luna`` rate card from ``data/prices.json``, per 1M tokens."""
    table = json.loads((HW_ROOT / "data" / "prices.json").read_text(encoding="utf-8"))
    return {k: v for k, v in table[MODEL_NAME].items() if isinstance(v, (int, float))}


def cost_usd(
    *,
    input_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
) -> dict[str, float]:
    """Price one run from the rate card, itemised.

    ``input_tokens`` is the whole prompt as the provider counts it, cached portion
    included, so the uncached remainder is what is left after the cached and
    cache-written tokens are taken out.
    """
    rate = {k: v / 1_000_000 for k, v in prices().items()}
    uncached = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)
    parts = {
        "uncached_input": uncached * rate["input_per_1m"],
        "cached_input": cache_read_tokens * rate["cached_input_per_1m"],
        "cache_write": cache_write_tokens * rate["cache_write_per_1m"],
        "output": output_tokens * rate["output_per_1m"],
    }
    parts = {k: round(v, 8) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 8)
    parts["uncached_input_tokens"] = uncached
    return parts
