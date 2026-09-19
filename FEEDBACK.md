# Feedback: Nebius Token Factory + NVIDIA Nemotron

Written while building Virta (Sept 2026). Everything below was measured in this project,
not guessed; the numbers come from real calls logged in `var/`.

## What worked well

- **Drop-in OpenAI compatibility.** The standard `openai` Python client worked on the first
  call - change the base URL, key and model id, nothing else. `/v1/models` lists the
  catalogue, which made right-sizing models trivial to check.
- **Right-sized models are real.** Virta runs its reasoning tiers on models from the same
  endpoint: Super for the frequent live line and for labelling unknown loads, and
  Nemotron 3 Ultra for the deep nightly analysis. Ultra produced the most accurate nightly
  insights *and* used about half the tokens Lightning needed for the same digest (5.2k vs
  ~10.8k). Switching tiers is one env var, so we could test the candidates on real data.
- **Size shows in the voice, not just the facts.** Nano (~1.3k tokens) wrote correct but
  flat lines ("BASE LOAD ~1KW, PRICE 2.95 C/KWH"). Given a butler persona, it kept writing
  instrument lines. Super took the persona and noticed more (a kettle running longer than
  usual, today against yesterday) at a similar token count (0.8-2k), ~18 s per call.
  Both kept drifting back to the price until the prompt put the house's own history
  first in the payload and said explicitly when the price is worth mentioning.
- **Reliable.** No failed requests over the build; failures we did see were all our own
  (token budgets, see below).

## Friction, with evidence

### 1. Where the answer lives differs per model
The same one-word prompt ("Reply with the single word: ONLINE"):

| Model | completion tokens | `content` | `reasoning_content` |
|---|---|---|---|
| Nemotron-3_5-Lightning | 165 | ONLINE | full chain of thought |
| Nemotron-3-Nano-30B-A3B | 55 | ONLINE | **empty** (reasoning not exposed) |
| nemotron-3-super-120b-a12b | 17 | ONLINE | one line |
| Nemotron-3-Ultra-550b-a55b | 20 | ONLINE | one line |

On a longer task Lightning also wrote its thinking *into `content`* ("Here's a thinking
process: ..."), so `content` could not be trusted to hold only the answer. We parse the
last valid JSON block defensively. **Suggestion:** guarantee the answer/reasoning split
across all Nemotron models, and document which models expose reasoning.

### 2. Reasoning can consume the whole budget and return no answer
With `max_tokens=4096`, a labelling call came back `finish_reason: "length"` with
`reasoning_tokens: 4096` of 4096 - all thinking, no answer. Raising the budget worked
(the same call finished at ~10.8k). **Suggestion:** a separate reasoning budget or
effort knob (e.g. `reasoning_effort` / `max_reasoning_tokens`), so an app can cap
thinking without risking an empty answer.

### 3. Over-deliberation on style
Asked for one short console line in a dry voice, Lightning found a good line early and
then kept polishing it past 8,000 tokens (truncated, no answer). Adding "write the first
good line and stop" to the prompt brought it to ~4.4k. A reasoning-effort control would
make this a parameter instead of prompt folklore.

### 4. Prompt examples get copied verbatim
Two cases: an example verdict "PRICE NORMAL" was reproduced at a price that was
EXPENSIVE, and an example observation ("TOASTER AND KETTLE ON B, TOGETHER AGAIN.") came
back word for word. We now use placeholders and validate facts at the edge. Worth
mentioning in prompting guidance for these models.

### 5. No tool calling / structured output for reasoning models
Structured output comes from "answer ONLY with JSON" plus defensive parsing and
validation. It works, but `response_format` / JSON-schema support on the reasoning
models would remove a whole class of parsing code and failure modes.

### 6. Pricing isn't visible to the app
Virta has a spend ceiling, but per-token prices aren't exposed through the API, so the
ledger counts calls and tokens and cannot compute money. **Suggestion:** return cost in
`usage`, or expose per-model pricing via `/v1/models`.

## Summary

Token Factory made "one reasoning endpoint, three model sizes" genuinely easy - that is
the architecture Virta is built on. The biggest improvement would be control over
*how much* a Nemotron model thinks, and a guaranteed place where the answer lands.
