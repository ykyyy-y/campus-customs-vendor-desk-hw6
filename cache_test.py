"""Problem 4 - what prefix caching is actually worth on this agent.

Runs all 12 practice documents twice:

* **with_cache** - the instructions are byte-identical on every call, so the 10,058-token
  handbook sits at a stable prefix the provider can reuse.
* **without_cache** - the same agent, except a unique run id is injected at character
  zero of the instructions. The prefix differs from the first token, so there is nothing
  for the cache to match and ``cache_read_tokens`` must come back 0 on every call. That
  is the claim this script proves rather than asserts.

Every individual model call from both passes is written to ``output/cache_test.json``:
the variant, the document, every token count the API reported, and the dollar cost from
``data/prices.json``. One document makes more than one model call - the agent decides to
read the file, then answers with the text in hand - so the per-call records are kept
separately instead of being flattened into a run total.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic_ai.messages import ModelResponse

from agent import assess, build_agent, system_prompt
from llm import MODEL_NAME, cost_usd
from tools import SUPPORTED

HW_ROOT = Path(__file__).resolve().parent
PRACTICE = HW_ROOT / "data" / "practice_docs"
OUT_JSON = HW_ROOT / "output" / "cache_test.json"

WITH_CACHE = "with_cache"
WITHOUT_CACHE = "without_cache"


def documents() -> list[Path]:
    """The 12 practice documents, in a stable order so passes are comparable."""
    return sorted(p for p in PRACTICE.iterdir() if p.suffix.lower() in SUPPORTED)


def cache_busting_instructions() -> str:
    """The same instructions, made unique at character zero.

    A prefix cache matches from the very start of the prompt, so one unique line in front
    of everything else is enough to guarantee a miss - no provider flag required, and the
    proof is in the returned token counts.
    """
    return (
        f"<!-- cache-buster: run {uuid.uuid4()} - this line makes the prompt prefix "
        "unique so nothing can be served from cache -->\n\n" + system_prompt()
    )


def call_records(result, variant: str, doc: str) -> list[dict]:
    """One record per model call in a run, priced from the rate card."""
    records = []
    index = 0
    for message in result.all_messages():
        if not isinstance(message, ModelResponse):
            continue
        usage = message.usage
        index += 1
        cost = cost_usd(
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
        )
        records.append(
            {
                "variant": variant,
                "document": doc,
                "model": MODEL_NAME,
                "call_index": index,
                "tokens": {
                    "input": usage.input_tokens,
                    "uncached_input": cost["uncached_input_tokens"],
                    "cache_read": usage.cache_read_tokens,
                    "cache_write": usage.cache_write_tokens,
                    "output": usage.output_tokens,
                    "total": usage.input_tokens + usage.output_tokens,
                },
                "api_details": dict(usage.details or {}),
                "cost_usd": {
                    k: cost[k]
                    for k in (
                        "uncached_input",
                        "cached_input",
                        "cache_write",
                        "output",
                        "total",
                    )
                },
            }
        )
    return records


def main() -> None:
    docs = documents()
    print(f"{len(docs)} practice documents, two passes each\n")
    records: list[dict] = []

    # Pass 1: identical instructions every time. The first document pays to warm the
    # cache; the rest should read the handbook back at the cached rate.
    cached_agent = build_agent()
    for doc in docs:
        rel = doc.relative_to(HW_ROOT).as_posix()
        run = assess(rel, agent=cached_agent, label=WITH_CACHE, run_tag=str(uuid.uuid4()))
        rows = call_records(run["result"], WITH_CACHE, rel)
        records.extend(rows)
        read = sum(r["tokens"]["cache_read"] for r in rows)
        print(
            f"  {WITH_CACHE:14} {doc.name:32} "
            f"${sum(r['cost_usd']['total'] for r in rows):.6f}  cached {read:,}"
        )

    print()

    # Pass 2: a fresh unique prefix per document, so no call can hit the cache.
    for doc in docs:
        rel = doc.relative_to(HW_ROOT).as_posix()
        agent = build_agent(instructions=cache_busting_instructions)
        run = assess(rel, agent=agent, label=WITHOUT_CACHE, run_tag=str(uuid.uuid4()))
        rows = call_records(run["result"], WITHOUT_CACHE, rel)
        records.extend(rows)
        read = sum(r["tokens"]["cache_read"] for r in rows)
        flag = "" if read == 0 else "  <-- LEAKED CACHE, run invalid"
        print(
            f"  {WITHOUT_CACHE:14} {doc.name:32} "
            f"${sum(r['cost_usd']['total'] for r in rows):.6f}  cached {read:,}{flag}"
        )

    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def total(variant: str, field: str) -> float:
        return sum(
            r["cost_usd"]["total"] if field == "cost" else r["tokens"][field]
            for r in records
            if r["variant"] == variant
        )

    leaked = total(WITHOUT_CACHE, "cache_read")
    print(f"\nwrote {len(records)} model calls to {OUT_JSON}")
    print(f"  with_cache    total ${total(WITH_CACHE, 'cost'):.6f}")
    print(f"  without_cache total ${total(WITHOUT_CACHE, 'cost'):.6f}")
    print(f"  cache_read tokens in the without_cache pass: {leaked:,.0f} (must be 0)")


if __name__ == "__main__":
    main()
