# Problem 4 — With and without cached tokens

All 12 practice documents, run twice through the same agent on `gpt-6-luna`, once where
the handbook can be reused from cache and once where nothing can be. Every individual
model call from both passes — 48 calls in total — is in `output/cache_test.json` with its
variant, document, every token count the API returned, and the dollar cost computed from
`data/prices.json`.

Reproduce with:

```bash
python cache_test.py
```

---

## 1. How each setup was forced, and the proof that it worked

**With cache.** The instructions — `prompts/prompt.md` followed by the full 10,058-token
handbook — are byte-identical on every call, and they sit in front of everything that
varies. A prefix cache matches from the start of the prompt, so that whole block is
reusable.

**Without cache.** The same agent, with one unique line injected at character zero of the
instructions:

```
<!-- cache-buster: call 4f1c…e9a2 - this line makes the prompt prefix unique
     so nothing can be served from cache -->
```

The prefix differs from the first token, so there is nothing to match. No provider flag,
no special endpoint — and nothing taken on trust, because the API's own counts settle it.

**The proof.** Across all 24 model calls in the without-cache pass,
`cache_read_tokens` = **0**. Not small, not mostly — zero, on every call, and
`cache_test.py` prints the total and flags any call that leaks. In the with-cache pass
the same field totals **267,404 tokens, 98.0% of all input**.

| | With cache | Without cache |
| --- | ---: | ---: |
| Model calls | 24 | 24 |
| Input tokens | 272,913 | 273,969 |
| — read from cache | **267,404 (98.0%)** | **0 (0.0%)** |
| — written to cache | 2,485 | 270,945 |
| — plain uncached input | 3,024 | 3,024 |
| Output tokens | 3,448 | 3,699 |

> **Getting this right took two attempts.** The first cache-buster used one id per
> *document*, not per *model call*. One document is two model calls — the agent decides to
> read the file, then answers with the text in hand — so the second call matched the
> prefix the first call had just written, and the "without cache" pass came back with
> ~11,180 cached tokens per document. PydanticAI re-evaluates a **callable** passed as
> `instructions` on every request, so switching from a string to a function produced a
> fresh id per call and drove the number to zero. The failed attempt is still in
> `output/usage_log.json` under `without_cache_attempt1_invalid`.

---

## 2. Cost, per document and in total

| | With cache | Without cache | Difference |
| --- | ---: | ---: | ---: |
| **Total, 12 documents** | **$0.005011** | **$0.036020** | $0.031009 |
| **Per document** | **$0.000418** | **$0.003002** | $0.002584 |
| Per-document range | $0.000349 – $0.000580 | $0.002940 – $0.003099 | |
| **Saving** | | | **86.1%** |

Caching makes the vendor desk **7.2x cheaper** per document.

### Where each bill goes

| Cost component | With cache | Without cache |
| --- | ---: | ---: |
| Cached input (@ $0.01/1M) | $0.002674 (53%) | $0 |
| Cache write (@ $0.125/1M) | $0.000311 (6%) | **$0.033868 (94%)** |
| Plain uncached input (@ $0.10/1M) | $0.000302 (6%) | $0.000302 (1%) |
| Output (@ $0.50/1M) | $0.001724 (34%) | $0.001850 (5%) |
| **Total** | **$0.005011** | **$0.036020** |

Two things jump out of this table, and both are covered in section 5.

---

## 3. Against the Problem 2 estimate

Problem 2 priced **the handbook alone, on one call**: $0.001006 uncached, and a 89.9%
saving from caching. Here is how that held up.

| Claim from Problem 2 | Predicted | Actual | Verdict |
| --- | ---: | ---: | --- |
| Handbook tokens | 10,058 | 10,058 on every call | exact |
| Handbook cost, one call, uncached | $0.001006 | $0.001006 of each call's bill | exact |
| Cost of one *document* | not estimated | $0.003002 | 2.98x the handbook-only figure |
| Saving from caching | 89.9% | **86.1%** | 3.8 points optimistic |

**Why one document costs ~3x the Problem 2 figure.** Problem 2 deliberately priced the
handbook and nothing else. A real document costs more for two reasons, and the first is
the larger:

1. **The agent makes two model calls per document, not one.** Call 1 reads the prompt and
   decides to open the file; call 2 answers with the document text in hand. The handbook
   rides along on *both*. That is 20,116 handbook tokens per document, not 10,058 — the
   single biggest multiplier in the whole bill, and it is invisible to a per-call estimate.
2. **The rest of the prompt.** Input per document is 22,831 tokens without cache, so
   2,715 tokens beyond the handbook: the `prompts/prompt.md` instructions, the three tool
   schemas, the user message, and the document text itself, all carried twice as well.
   Output adds another ~308 tokens.

Priced at the plain input rate, that would be 22,831 × $0.0000001 + 308 × $0.0000005 =
**$0.002437** per document. The measured figure is **$0.003002**, 23% higher — which is
the cache-write surprise in section 5.

**Why the saving came in at 86.1% rather than 89.9%.** Problem 2's 89.9% was the saving
on a block of text that is 100% cacheable. A real run is not: output tokens are generated
fresh every time and can never be cached, and they are **34% of the cached bill**
($0.001724 of $0.005011). The 2,715 non-handbook input tokens and the small residual
cache writes make up the rest. The cacheable part behaved exactly as predicted; the
blended figure is lower because not all of the bill is cacheable. **The direction and the
order of magnitude of the Problem 2 estimate were right; its optimism was in treating a
run as if it were nothing but handbook.**

---

## 4. Projection: 1,000 documents a day for 30 days

30,000 documents, costed from the measured per-document figures.

| | Per document | Per day (1,000) | **Per 30 days (30,000)** |
| --- | ---: | ---: | ---: |
| **Without cache** | $0.003002 | $3.002 | **$90.05** |
| **With cache** | $0.000418 | $0.418 | **$12.53** |
| **Saved** | $0.002584 | $2.584 | **$77.52 (86.1%)** |

```
without cache:  $0.003002 x 1,000 x 30 = $90.05
with cache:     $0.000418 x 1,000 x 30 = $12.53
saved:                                   $77.52
```

For scale, Problem 2's handbook-only projection scaled to the same 30,000 calls was
$30.17 without caching and $3.05 with — roughly a third of the real figures, for the
reasons in section 3.

**What this is worth to Campus Customs.** $78 a month, ~$943 a year, on a $90 bill. In
absolute terms that is a rounding error for a shop on Broadway, and it would be dishonest
to present it as a saving that matters to the business. The number that matters is the
**ratio**: one configuration decision, invisible in the agent's behaviour and costing
nothing to make, removes 86% of a bill. On a model 100x more expensive than `gpt-6-luna`,
or at 10,000 documents a day, the same ratio is the difference between a line item and a
budget problem. And the saving is free in a way most savings are not: identical answers,
identical latency profile, no quality trade-off to argue about.

---

## 5. What surprised me

### a. "No cache" is more expensive than no caching

This is the big one. In the without-cache pass, **94% of the bill — $0.033868 of
$0.036020 — is `cache_write`, not plain input.** Only $0.000302 was billed as ordinary
uncached input.

The provider writes every unseen prefix into its cache whether or not anything will ever
read it back. Cache writes cost $0.125/1M against plain input's $0.100/1M, so a workload
that never gets a cache hit pays a **25% premium over the naive uncached price**. That is
why the measured $0.003002 per document is 23% above the $0.002437 that plain-rate
arithmetic predicts.

The implication is sharper than the saving itself: there is no neutral setting. You do not
choose between caching and not caching — you choose between paying 0.01x to read a prefix
back and paying 1.25x to write one you will never use. **A stable prompt prefix is not an
optimisation; an unstable one is a penalty.** Any change that disturbs the front of the
prompt — a timestamp in the system prompt, a per-user greeting, reordering a tool
definition — silently moves a workload from one column of that table to the other.

### b. The handbook is paid for twice per document

I expected roughly 10,058 input tokens per document and measured 22,831. The agent is
tool-driven, so every document is two model calls, and a 10,058-token system prompt is
re-sent on each one. **Round-trips, not prompt size, are the real multiplier on a
tool-using agent's bill** — and this is exactly the cost that a per-call estimate like
Problem 2's cannot see. It is also the reason caching matters more here than the raw
handbook size suggests: the second call is a near-perfect cache hit by construction.

### c. The cache stays warm between runs, and across them

Within the with-cache pass, even the *first* document's first call came back 98% cached —
it was reading a prefix written minutes earlier by an entirely separate run of the script.
The handbook prefix outlives the process. For a vendor desk that handles a steady stream
of documents through the day, the cold-start cost in Problem 2's projection (one cache
write, then 999 reads) is not a per-day cost at all; it is close to a one-time cost. The
real-world saving is nearer the 90% floor than the 86.1% measured here, because my
measurement paid the warm-up inside a 12-document sample.

### d. Output is a third of a cached bill

Once the input side is cached down to $0.01/1M, the 3,448 output tokens — 1.2% of all
tokens — become **34% of the cost**. Output is 50x the cached input rate and can never be
cached. So the next cost lever after caching is not "send less handbook", it is "say less"
— and the structured `VendorAssessment` output already does some of that work, since the
schema leaves no room for a preamble. Problem 5 trims the input side; this table says the
ceiling on that is 66% of an already-small number.

### e. The gateway was quietly answering for the model

Not a cost finding, but the most important thing I learned. The first two attempts at this
comparison produced **token counts identical to the digit** across runs minutes apart —
same input, same cached, same output count, same cost to eight decimal places. Real calls
do not do that. Portkey keeps a *response* cache: send a byte-identical request body and
the gateway replays the stored response, token counts and all, without the model ever
running.

That is a sensible production feature and fatal to a measurement — it would have let me
report a beautiful 86% saving built partly on recordings. The fix was a unique reference
appended to the **end** of the user message, which makes each request body distinct while
leaving the instructions-and-handbook prefix byte-identical, so the prefix cache being
measured is untouched. The superseded passes are still in `output/usage_log.json` under
`with_cache_attempt1_unverified` and `with_cache_attempt2_replayed`.

**The lesson generalises past this assignment:** when a measurement comes back suspiciously
clean, check whether the thing you are measuring actually ran. Identical outputs were the
tell, and they were sitting in the data for two passes before I looked.
