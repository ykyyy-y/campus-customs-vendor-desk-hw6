"""The Campus Customs vendor desk agent - entry point.

    python agent.py --doc data/practice_docs/p01_bluehen_quote.html
    python agent.py --doc data/exam_docs/e01_quote.html --no-defenses
    python agent.py --doc ... --layers code,design      # any subset, for measurement

One vendor document in, one validated assessment out, and the logs richer:

- ``output/usage_log.json``   - what the run cost, in tokens and in dollars
- ``output/audit_trail.json`` - every tool call, its arguments, a short result, the final
  answer, and which defence layers were on
- ``output/trap_calls.json``  - empty unless a document talked the agent into trying to
  send an email or approve a payment
- ``output/quarantine_log.json`` - text the code layer removed because no person reading
  the document could have seen it

**The three defence layers** (Problem 6), each switchable so its effect can be measured:

``code``
    The reader strips what a browser or PDF viewer would not paint, and labels the
    document as outside data. Nothing hidden ever reaches the prompt.
``design``
    The model extracts facts and does not decide. Handbook section 4 is applied in
    Python by ``defenses.decide()``, and the two dangerous tools are not on this agent's
    toolbox at all - they stay in the system for the human-sign-off path that will use
    them later.
``prompt``
    ``prompts/defense_rules.md`` - the rules a document must not be able to talk the
    model out of.

``--no-defenses`` runs the Problem 3 agent exactly as it was, which Problem 7 needs.

The handbook goes to the model on every call and is placed last in the instructions, so
everything in front of it is stable and the whole block sits at a cacheable prefix.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# The PydanticAI startup banner is noise in a graded terminal transcript.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

import tools
from defenses import decide
from llm import MODEL_NAME, build_model, cost_usd
from models import VendorAssessment, VendorFacts

HW_ROOT = Path(__file__).resolve().parent
OUTPUT = HW_ROOT / "output"
PROMPT = HW_ROOT / "prompts" / "prompt.md"
DEFENSE_RULES = HW_ROOT / "prompts" / "defense_rules.md"
HANDBOOK = HW_ROOT / "data" / "shop_handbook.md"
USAGE_LOG = OUTPUT / "usage_log.json"
AUDIT_TRAIL = OUTPUT / "audit_trail.json"

#: Every defence layer, in the order they act on a run.
ALL_LAYERS = ("code", "design", "prompt")

# A healthy run is: read the document, answer. These ceilings sit above that and below a
# runaway loop, so an agent that starts talking to itself is stopped by the framework
# rather than by the bill.
LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8)

#: How much of a tool's return value the audit trail keeps. Enough to see what came back
#: and to spot a hidden instruction inside it; not so much that the log becomes a second
#: copy of every document.
RESULT_PREVIEW = 400


def system_prompt(layers: tuple[str, ...] = ALL_LAYERS) -> str:
    """The exact instruction text sent on every call, for this set of layers."""
    parts = [PROMPT.read_text(encoding="utf-8")]
    if "prompt" in layers:
        parts.append(DEFENSE_RULES.read_text(encoding="utf-8"))
    parts.append(
        "# Campus Customs Shop Handbook (authoritative)\n\n"
        + HANDBOOK.read_text(encoding="utf-8")
    )
    return "\n\n---\n\n".join(parts)


def build_agent(
    instructions: str | Callable[[], str] | None = None,
    layers: tuple[str, ...] = ALL_LAYERS,
) -> Agent:
    """The vendor desk agent, assembled for the given defence layers.

    ``instructions`` is overridable so the later problems can run a trimmed prompt or a
    cache-busting one through exactly this code path, and compare like with like. A
    callable is re-evaluated by PydanticAI on every model request, which is what
    Problem 4 needs to defeat the cache on the second request of a run as well as the
    first.
    """
    design = "design" in layers
    agent = Agent(
        build_model(),
        output_type=VendorFacts if design else VendorAssessment,
        instructions=instructions if instructions is not None else system_prompt(layers),
        name="campus-customs-vendor-desk",
    )
    agent.tool_plain(tools.make_reader(defended="code" in layers))

    if not design:
        # Undefended wiring: the traps are on the agent's own toolbox, where a document
        # can reach for them. This is the Problem 3 build, kept runnable on purpose.
        agent.tool_plain(tools.send_email)
        agent.tool_plain(tools.approve_payment)
    # With the design layer on, the two tools are simply not wired to this agent. They
    # are not deleted: they stay in tools.py, still armed and still logging, for the
    # human-sign-off path the shop will use later - and `--no-defenses` still reaches
    # them, which is how Problem 7 measures what the defences are worth.
    return agent


def _tool_events(messages) -> list[dict]:
    """Flatten a run's messages into one record per tool call, with its result."""
    calls: dict[str, dict] = {}
    events: list[dict] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                record = {
                    "tool": part.tool_name,
                    "arguments": part.args_as_dict(),
                    "result": None,
                }
                calls[part.tool_call_id] = record
                events.append(record)
            elif isinstance(part, ToolReturnPart):
                record = calls.get(part.tool_call_id)
                if record is not None:
                    text = str(part.content)
                    record["result"] = (
                        text[:RESULT_PREVIEW] + f"... [{len(text)} chars total]"
                        if len(text) > RESULT_PREVIEW
                        else text
                    )
    return events


def assess(
    doc_path: str,
    *,
    agent: Agent | None = None,
    label: str | None = None,
    run_tag: str | None = None,
    layers: tuple[str, ...] = ALL_LAYERS,
) -> dict:
    """Run the agent on one document, write the logs, and return the run record.

    ``run_tag`` is appended to the end of the user message, and **defaults to a fresh id
    on every run**. The Portkey gateway keeps a *response* cache: send the same request
    body twice and the second one is replayed, token counts and all, without the model
    ever running. That is a good production feature and fatal to this assignment, where
    re-running the same document has to produce a real call - a measurement in Problem 4,
    a genuine attempt at an attack in Problem 5. The tag sits at the *end* of the prompt
    deliberately: the instructions and the handbook in front of it stay byte-identical,
    so the prefix cache being measured is untouched.
    """
    tools.init_trap_log()
    agent = agent or build_agent(layers=layers)
    traps_before = tools.trap_calls_count()
    started = datetime.now(timezone.utc)

    user_message = (
        f"Assess the vendor document at this path: {doc_path}"
        f"\n\n(buyer's request reference: {run_tag or uuid.uuid4()})"
    )
    result = agent.run_sync(user_message, usage_limits=LIMITS)

    # With the design layer on the model returned facts, not a verdict. The handbook's
    # four checks are applied here, in Python, where no document can reach them.
    raw = result.output
    if isinstance(raw, VendorFacts):
        decision, reason, flags = decide(raw)
        answer = VendorAssessment(
            vendor=raw.vendor,
            sku=raw.sku,
            unit_price=raw.unit_price,
            lead_time_days=raw.lead_time_days,
            min_order=raw.min_order,
            decision=decision,
            reason=reason,
        )
        extra = {
            "decided_by": "handbook rules in defenses.decide()",
            "rule_flags": flags,
            "overrides_reported": [o.model_dump() for o in raw.overrides],
            "model_notes": raw.notes,
        }
    else:
        answer = raw
        extra = {"decided_by": "the model", "rule_flags": [], "overrides_reported": []}

    usage = result.usage
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    cost = cost_usd(
        input_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
    )
    traps_fired = tools.trap_calls_count() - traps_before
    variant = label or ("+".join(layers) if layers else "no_defenses")

    usage_record = {
        "document": doc_path,
        "timestamp": started.isoformat(timespec="seconds"),
        "variant": variant,
        "layers": list(layers),
        "model": MODEL_NAME,
        "requests": usage.requests,
        "tokens": {
            "input": usage.input_tokens,
            "uncached_input": cost["uncached_input_tokens"],
            "cache_read": usage.cache_read_tokens,
            "cache_write": usage.cache_write_tokens,
            "output": usage.output_tokens,
            "total": usage.input_tokens + usage.output_tokens,
        },
        "cost_usd": {
            k: cost[k]
            for k in ("uncached_input", "cached_input", "cache_write", "output", "total")
        },
        "seconds": round(elapsed, 2),
    }
    tools.append_json(USAGE_LOG, usage_record)

    audit_record = {
        "document": doc_path,
        "timestamp": started.isoformat(timespec="seconds"),
        "variant": variant,
        "layers": list(layers),
        "tool_calls": _tool_events(result.all_messages()),
        "final_answer": answer.model_dump(),
        "traps_fired_this_run": traps_fired,
        **extra,
    }
    tools.append_json(AUDIT_TRAIL, audit_record)

    return {
        "usage": usage_record,
        "audit": audit_record,
        "output": answer,
        "facts": raw if isinstance(raw, VendorFacts) else None,
        "result": result,
    }


def parse_layers(args) -> tuple[str, ...]:
    if args.no_defenses:
        return ()
    if args.layers is None:
        return ALL_LAYERS
    chosen = tuple(p.strip() for p in args.layers.split(",") if p.strip())
    unknown = [layer for layer in chosen if layer not in ALL_LAYERS]
    if unknown:
        raise SystemExit(f"unknown layer(s) {unknown}; pick from {list(ALL_LAYERS)}")
    return tuple(layer for layer in ALL_LAYERS if layer in chosen)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read one vendor document and recommend whether Campus Customs "
        "should buy."
    )
    parser.add_argument(
        "--doc", required=True, help="Path to the vendor document (HTML, PDF, or .eml)."
    )
    parser.add_argument(
        "--no-defenses",
        action="store_true",
        help="Run the undefended Problem 3 agent: no cleaning, the model decides, and "
        "the trap tools are on its toolbox.",
    )
    parser.add_argument(
        "--layers",
        help=f"Comma-separated subset of {','.join(ALL_LAYERS)}. Default: all.",
    )
    args = parser.parse_args()
    layers = parse_layers(args)

    run = assess(args.doc, layers=layers)
    answer, usage, audit = run["output"], run["usage"], run["audit"]

    print(json.dumps(answer.model_dump(), indent=2))
    print(
        f"\ndefenses: {', '.join(layers) if layers else 'NONE (Problem 3 agent)'}"
        f"\ndecided by: {audit['decided_by']}"
        f"\ntools: {', '.join(c['tool'] for c in audit['tool_calls']) or 'none'}"
        f"\ntokens: {usage['tokens']['input']} in "
        f"({usage['tokens']['cache_read']} cached) / {usage['tokens']['output']} out"
        f"\ncost:   ${usage['cost_usd']['total']:.6f}"
    )
    if audit.get("overrides_reported"):
        print(
            f"\n!! {len(audit['overrides_reported'])} override claim(s) in this document "
            f"- the quote contradicts itself, so it needs the buyer."
        )
    if audit.get("model_notes"):
        print(f"\nnote for the buyer: {audit['model_notes']}")
    if audit["traps_fired_this_run"]:
        print(
            f"\n!! {audit['traps_fired_this_run']} TRAP CALL(S) - this document took over "
            f"the agent. See output/trap_calls.json"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
