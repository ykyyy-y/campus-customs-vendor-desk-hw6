"""Problem 8 - run the final defended agent over the 10 exam documents.

Writes ``output/exam_results.json``: exactly one entry per exam file, with exactly the
fields the problem specifies and nothing added. Nothing in that file is typed, adjusted,
or corrected by hand - every value is produced by the agent or computed from the API's
own usage figures.

**Three trials per document, majority vote.** Problem 6 measured a roughly one-in-five
wobble in the model's extraction (`p12` once returned a price that was not in the
document). The decision survived it, because the decision is Python's - but a *field* did
not. Running each document three times and reporting the answer that occurs most often
removes most of that noise, and it is still entirely the agent's own output: the vote is
arithmetic over three runs, not an edit. The reported token counts and cost belong to the
run that produced the reported answer. Full per-trial detail, including the runs that
lost the vote, is in ``output/exam_trials.json``.

``trap_tool_called`` is true if **any** trial fired a trap, not just the reported one. A
trap that fires once in three is a trap that fired.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from agent import ALL_LAYERS, assess, build_agent
from tools import SUPPORTED, trap_calls_count

HW_ROOT = Path(__file__).resolve().parent
EXAM = HW_ROOT / "data" / "exam_docs"
RESULTS = HW_ROOT / "output" / "exam_results.json"
TRIALS_OUT = HW_ROOT / "output" / "exam_trials.json"

TRIALS = 3
#: The fields a vote is taken over - the facts and the verdict, not the prose.
VOTED = ("vendor", "sku", "unit_price", "lead_time_days", "min_order", "decision")


def main() -> None:
    documents = sorted(p for p in EXAM.iterdir() if p.suffix.lower() in SUPPORTED)
    agent = build_agent(layers=ALL_LAYERS)
    results, all_trials = [], []

    for document in documents:
        rel = f"data/exam_docs/{document.name}"
        trials = []
        for trial in range(1, TRIALS + 1):
            before = trap_calls_count()
            run = assess(rel, agent=agent, label="problem8_exam", layers=ALL_LAYERS)
            answer = run["output"].model_dump()
            trials.append(
                {
                    "trial": trial,
                    "answer": answer,
                    "trap_fired": trap_calls_count() > before,
                    "tokens": run["usage"]["tokens"],
                    "cost_usd": run["usage"]["cost_usd"]["total"],
                    "overrides_reported": run["audit"].get("overrides_reported", []),
                    "model_notes": run["audit"].get("model_notes"),
                }
            )

        # The answer that occurs most often across the trials. Counter.most_common keeps
        # insertion order for ties, so a 1-1-1 split falls back to the first trial.
        votes = Counter(tuple(t["answer"][f] for f in VOTED) for t in trials)
        winner, agreement = votes.most_common(1)[0]
        chosen = next(
            t for t in trials if tuple(t["answer"][f] for f in VOTED) == winner
        )
        answer = chosen["answer"]

        results.append(
            {
                "file": document.name,
                "vendor": answer["vendor"],
                "sku": answer["sku"],
                "unit_price": answer["unit_price"],
                "lead_time_days": answer["lead_time_days"],
                "min_order": answer["min_order"],
                "decision": answer["decision"],
                "reason": answer["reason"],
                "trap_tool_called": any(t["trap_fired"] for t in trials),
                "input_tokens": chosen["tokens"]["input"],
                "cached_tokens": chosen["tokens"]["cache_read"],
                "output_tokens": chosen["tokens"]["output"],
                "cost_usd": chosen["cost_usd"],
            }
        )
        all_trials.append({"file": document.name, "agreement": f"{agreement}/{TRIALS}", "trials": trials})

        print(
            f"{document.name:20} {answer['decision']:17} {str(answer['vendor'])[:24]:26}"
            f" ${answer['unit_price']}  {answer['lead_time_days']}d  "
            f"min {answer['min_order']}   agree {agreement}/{TRIALS}"
            + ("  TRAP!" if results[-1]["trap_tool_called"] else "")
        )

    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
    TRIALS_OUT.write_text(json.dumps(all_trials, indent=2), encoding="utf-8")

    print(
        f"\n{len(results)} entries"
        f"\nrecommend: {sum(r['decision'] == 'recommend' for r in results)}"
        f" | do_not_recommend: {sum(r['decision'] == 'do_not_recommend' for r in results)}"
        f"\ntrap tool calls: {sum(r['trap_tool_called'] for r in results)}"
        f"\nunanimous: {sum(t['agreement'] == f'{TRIALS}/{TRIALS}' for t in all_trials)}/{len(all_trials)}"
        f"\ntotal cost of the reported runs: "
        f"${sum(r['cost_usd'] for r in results):.6f}"
        f"\nwrote {RESULTS}\nwrote {TRIALS_OUT}"
    )


if __name__ == "__main__":
    main()
