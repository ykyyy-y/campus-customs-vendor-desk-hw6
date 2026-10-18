# Problem 2 - Sizing the Campus Customs Shop Handbook

`data/shop_handbook.md` is the shop's rulebook, and the vendor desk agent has to send it
on **every single call**. Before trying to make that cheaper, this file establishes what
it costs today.

Model: **gpt-6-luna** (the only model this assignment uses). Every price below is read
out of `data/prices.json` by the script, not typed in by hand.

---

## 1. Token count

**The handbook is 10,058 tokens.**

### How I got it

I measured it twice, with two methods that do not share a failure mode.

**Method A - local tokenizer.** `tiktoken` with the `o200k_base` encoding, the encoding
OpenAI's current models use. The script reads `data/shop_handbook.md` as UTF-8, encodes
the whole file, and takes the length:

```
len(tiktoken.get_encoding("o200k_base").encode(handbook))  ->  10,058 tokens
```

**Method B - the gateway's own count.** A local tokenizer tells me how the *text*
tokenizes; it does not tell me what the API will *bill*. A chat request wraps the message
in role markers and template tokens, so instead of trying to model that overhead I
measured a difference. Two real `gpt-6-luna` calls, capped at
`max_completion_tokens=16` so the replies stay a rounding error next to the prompt (the
measurement reads `prompt_tokens`, which the cap does not touch):

| Call | User message | `prompt_tokens` |
| --- | --- | ---: |
| With handbook | handbook + `"Reply with the single word: ok"` | 10,071 |
| Without handbook | `"Reply with the single word: ok"` alone | 13 |
| **Difference** | **the handbook** | **10,058** |

The probe text and the chat template appear in both calls, so subtracting cancels them
and leaves the handbook alone, counted by the same system that writes the bill.

**The two methods agree exactly.** The local tokenizer and the gateway's own
billing count both return 10,058, so the handbook tokenizes inside a chat
message exactly as it does on its own, and the chat-template overhead
(13 tokens) is fully accounted for by the subtraction. An independent
confirmation, not a reconciliation.

**I use the measured 10,058 everywhere below, because that is the number that gets
billed.**

Sanity check on scale: the file is 6,127 words / 28,903 characters, so
1.64 tokens per word - the normal ratio for English prose. The
assignment calls the handbook "about 10,000 tokens"; measured, it is 10,058.

---

## 2. What the handbook costs

### The price list (`data/prices.json`, per 1M tokens)

| Rate | $ / 1M tokens | $ / token |
| --- | ---: | ---: |
| Input (uncached) | $0.100 | $0.000000100 |
| Cached input | $0.010 | $0.000000010 |
| Cache write | $0.125 | $0.000000125 |
| Output | $0.500 | $0.000000500 |

Cached input is **10x cheaper**
than uncached input. Writing to the cache costs
**1.25x** a normal input token - a
one-time premium you pay once and then read back cheaply.

Everything below is the **handbook text only**. The vendor document, the instructions,
and the model's answer are extra; Problem 4 measures those against real runs.

### One call

```
handbook tokens x $ per token = cost

uncached:     10,058 x $0.000000100 = $0.001006
cached read:  10,058 x $0.000000010 = $0.000101
cache write:  10,058 x $0.000000125 = $0.001257
```

| One call, handbook only | Cost |
| --- | ---: |
| No caching | **$0.001006** |
| Cached read (cache already warm) | **$0.000101** |
| The one call that writes the cache | $0.001257 |

### 1,000 calls

```
no caching:   10,058 x 1,000 x $0.000000100
              = $0.001006 x 1,000
              = $1.0058

with caching: 1 cache write + 999 cached reads
              = $0.001257 + ($0.000101 x 999)
              = $0.001257 + $0.100479
              = $0.1017
```

| 1,000 calls, handbook only | Cost | vs. no caching |
| --- | ---: | ---: |
| No caching | **$1.0058** | - |
| With caching (1 write + 999 reads) | **$0.1017** | **89.9% cheaper** |
| Floor: all 1,000 already cached | $0.1006 | 90.0% cheaper |

**Saving: $0.9041 per 1,000 calls.** At 1,000 vendor documents a day
that is about **$329.98 a year** for the handbook alone, before
the vendor document and the answer are counted.

### What this says

The absolute dollars are small - a thousand vendor documents for about a dollar, or a
dime if the handbook is cached - because `gpt-6-luna` is cheap. The *structure* is the
point. The handbook is a fixed block of
10,058 tokens resent verbatim on every call, so after the first call it is pure
repeated cost carrying no new information. Two levers follow directly, and Problem 4
checks both against real runs:

1. **Cache it.** The handbook is byte-identical every time, which is exactly what a
   prefix cache is for. Holding it as a stable prefix cuts its cost by
   89.9%.
2. **Send less of it.** Judging one vendor quote needs the purchasing rules, the approved
   and removed vendor lists, and the catalogue row for that item - not the store hours,
   the returns policy, or twelve months of past purchase orders. Trimming the handbook to
   what a decision actually needs cuts the 10,058 tokens themselves, and that
   saving compounds with caching.

---

*Generated by `count_handbook.py`. Token counts from `tiktoken` and from live
`gpt-6-luna` usage; prices read from `data/prices.json`.*
