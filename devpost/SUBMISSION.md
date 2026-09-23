# Virta — Devpost submission, field by field

Nebius x NVIDIA Global AI Hackathon. Copy each block straight into the form.
Anything in `[brackets]` is a decision you have to make.

---

## Project name

```
Virta
```

Just the word. The gallery shows the name and the elevator pitch together, so a tagline
in the name field would say the same thing twice.

## Elevator pitch

```
A 60 EUR clamp meter and an old Raspberry Pi 3 watch one number for the whole house.
Nemotron on Nebius turns it into what every appliance is doing, and what it costs.
Raw power never leaves the building.
```

---

## About the project

The story lives in its own file so there is only one copy of it:
**[devpost/ABOUT.md](ABOUT.md)**. Paste the fenced block from there.

Rewritten 2026-09-23 with the apn201-voice and avoid-ai-writing skills. The earlier draft
read as a presentation of a finished thing rather than an account of building it, and it
closed about twelve paragraphs on a short reversal.

---

## Built with

*(up to 25 tags — paste one at a time)*

```
python
nvidia-nemotron
nemotron-3-super-120b
nemotron-3-ultra-550b
nebius-token-factory
openai-python
raspberry-pi
shelly-3em
home-assistant
nilm
edge-computing
energy-monitoring
nordpool
pygame-ce
rest-api
systemd
json
matplotlib
tts
iot
smart-home
prompt-engineering
mit-license
```

## Try it out links

```
https://github.com/apn201/Virta
```

Second link — the replay, so anyone can run the whole pipeline without a house.
Label it "Replay the published sample day":

```
https://github.com/apn201/Virta#demo-replay-and-sample-data
```

## Video demo link

```
https://youtu.be/raFAFtf1x2k
```

Project page, once it's live: https://devpost.com/software/virta

---

# Additional info (judges and organizers)

### Submitter Type

```
Individual
```

### Organization Name

```
N/A
```

### Submitter Country of Residence

```
Finland
```

### Canada province

```
N/A
```

### Which track

```
Physical AI
```

### New or existing prior to August 26, 2026

```
New
```

First commit 2026-09-18. Built during the hackathon.

### Public code repository

```
https://github.com/apn201/Virta
```

MIT licensed (`LICENSE`). The README has setup instructions, the model table per tier,
and where each Nemotron call lives in the code. `FEEDBACK.md` is the Token Factory
write-up.

### Working demo / hosted application

```
Not applicable — Physical AI. The hardware is a Shelly 3EM in a fuse box and a
Raspberry Pi 3. Anyone can run the whole pipeline without the house:

  pip install -r requirements.txt
  python -m virta.demo          # replays a real published day through the live pipeline
  python -m virta.console       # the instrument, second terminal

samples/ holds one real day from this house (per-phase power as recorded, that day's
Nordpool prices, and the insights it produced), time-shifted and published as-is.
profiles/ and labels/ ship with it as example data: the device profiles measured off
this house, and the real labelling worklist with its clusters and answers. So the replay
runs against the same profiles the live install uses, not against stubs.
```

---

### Which model(s) did you use, and why that size/variant?

```
Three, all on Nebius Token Factory, one endpoint, switched per job with an env var:

- nvidia/nemotron-3-super-120b-a12b — the live line (Tier 1) and naming unknown loads.
  Typical call 0.8-2k tokens, about 18 s. Chosen after Nano: Nano got the facts right
  but wrote flat instrument lines and would not take up the butler persona no matter how
  the prompt was written. Super took the voice and noticed more (a kettle running longer
  than usual, today against yesterday) at a similar token count.

- nvidia/Nemotron-3-Ultra-550b-a55b — the nightly deep pass (Tier 2). Once a night, about
  5.2k tokens. It is the only call in the system that looks at the whole house's history
  at once, so it gets the biggest model. It also turned out to be the cheapest of the
  candidates for the job: Ultra wrote better insights than Nemotron-3_5-Lightning AND used
  about half the tokens on the same digest (5.2k vs 10.8k). Bigger model, fewer tokens,
  better output.

- nvidia/Nemotron-3_5-Lightning and nvidia/Nemotron-3-Nano-30B-A3B — evaluated on real
  data, not shipped. Nano for the live tier (voice), Lightning for nightly (token cost and
  over-deliberation).

Right-sizing per tier is the whole architecture. Frequent calls get the mid model, the
once-a-night call gets the big one, and everything numeric happens on the Raspberry Pi
for free. Overridable with NEBIUS_MODEL_LIVE / _LABELS / _NIGHTLY.
```

### Nemotron output quality, 1–10

```
8

What worked: reasoning. Given a digest of the house's week, Ultra found the things that
matter — that the always-on load is 73% of consumption, that an EV session cost 0.23 EUR
when the cheapest same-length window that night was 0.05 — and explained each one with
the evidence it used. Instruction-following on JSON-only output was reliable enough that
defensive parsing was insurance, not the normal path. Domain knowledge was better than
expected: it connected cheap Nordic night prices to wind generation without being told.

Where it fell short:
- Over-deliberation. Asked for one short line in a dry voice, Lightning found a good line
  early and kept polishing past 8,000 tokens, ending truncated with no answer. Prompt
  folklore ("write the first good line and stop") fixed it. A reasoning-effort knob would
  have made it a parameter.
- Examples in the prompt get reproduced verbatim, including a price word that was wrong
  for the actual price. Placeholders and edge validation fixed it.
- Overreach on thin data: "the EV is overdue" after a single observed session, and a
  suggestion to shift halogen lights to the middle of the night. Fixed with explicit
  rules in the prompt and matching checks in code.
- Occasional factual slips in the free-text explanation (an EV session at 08:51-10:42
  described as "overnight") while the structured fields were correct. Which is why only
  the validated line reaches the screen.

Deducting 2 points for the last two, both of which needed code to catch rather than
prompting alone.
```

### Fine-tune, prompt-engineer, or out of the box?

```
Out of the box, prompt-engineered. No fine-tuning, no LoRA, no embeddings.

The approach was to move everything a model shouldn't be doing out of the model. The edge
does all the arithmetic and hands over facts: the cheapest 1 h and 3 h windows, the curve's
range, negative slots, each load's running cost right now. The model's job is judgement —
is it worth deferring, what about the deadline, how sure are we of what's actually running.
Not arithmetic.

The three things that made the difference:

1. Payload order. The live call kept drifting back to talking about the price until the
   house's own history was put FIRST in the payload and the prompt said explicitly when
   the price is worth mentioning.
2. Placeholders instead of examples. A literal example verdict came back verbatim at the
   wrong price. Now it is a <TIER> placeholder, and any price word that contradicts the
   real tier is corrected at the edge.
3. Validate at the edge, always. Every number in an output line is checked against the
   digest the model was given, and a line with a number that isn't in it is dropped. Same
   for the guardrail categories (no health, mood, relationships, visitors, sleep, or
   "the house is empty"). In the prompt AND in code, because the prompt alone leaks.

Structured output is "answer ONLY with JSON" plus parsing the last valid JSON block,
because there is no tool calling or response_format on these models through the OpenAI
wrapper.
```

### How did Nemotron compare to other models?

```
Compared within the family on real data, on the two jobs:

Nightly digest (same input, same prompt):
  Nemotron-3-Ultra-550b     5.2k tokens   best insights, correct groundings
  Nemotron-3_5-Lightning   ~10.8k tokens  weaker insights, and truncated at 4096

Live line (same situation):
  Nemotron-3-Nano-30B      ~1.3k tokens   facts right, voice flat, persona ignored
  nemotron-3-super-120b     0.8-2k tokens persona held, noticed more, ~18 s

Against the GPT-4-class and Claude-class models I normally use for this kind of
summarise-and-judge work: comparable quality on the reasoning itself. The differences
that mattered here were operational, not intellectual — no tool calling or JSON schema
support on the reasoning models, and the answer sometimes landing in reasoning_content
instead of content, which needed handling once in the client.

The thing Nemotron did better than I expected is hold a voice. A dry, slightly HAL-ish
butler that comments on the house without being creepy is a hard brief, and Super held it
across hundreds of calls without drifting into either a chatbot or a spreadsheet.

Reliability over the whole build: no failed requests. Every failure was mine.
```

### Which Nebius capabilities were most valuable?

```
Token Factory's OpenAI-compatible endpoint, and the model catalogue behind it.

Concretely:
- The standard `openai` Python client worked on the first call. Base URL, key, model id,
  nothing else. No Nebius SDK needed and none exists, which is the correct answer.
  requirements.txt on the Raspberry Pi 3 is two lines.
- /v1/models lists the catalogue, so right-sizing was a thing I could actually check
  rather than guess.
- One endpoint, three model sizes, switched by env var, meant the model comparison above
  was a config change and a re-run on real data instead of an integration project. That
  is the reason Virta ended up with three reasoning tiers at all — it was cheap to find
  out that Ultra beat Lightning on both quality and tokens.
- No GPU instances, no deployment, no scaling work. This is a Raspberry Pi 3 in a house in
  Finland making a few dozen API calls a day. Serverless inference is exactly the right
  shape for edge hardware: the Pi does the 15 second loop and Token Factory does the
  thinking, and there is nothing to keep warm in between.

Where Token Factory accelerated the workflow: model selection. Two afternoons of A/B on
the house's own data across four models, no infrastructure touched.
```

### Recommend Nemotron on Nebius, 1–10

```
9

Drop-in OpenAI compatibility plus a catalogue that spans 30B to 550B on one endpoint is
exactly what a project like this needs, and zero failed requests over the whole build.
Not a 10 only because per-token pricing isn't exposed through the API, so my spend ledger
can count calls and tokens but cannot compute money.
```

### Nemotron inference on Nebius vs previous environments, 1–10

```
8

Compared to running a local model on the same hardware: no contest. A Raspberry Pi 3
cannot run anything that writes these lines, and the whole point of the project is that
the hardware stays cheap.

Compared to other hosted inference I use: the same, minus tool calling and structured
output, plus a wider size range on one endpoint. Latency was fine for the job — about 18 s
for a live line on Super — because the 15 second local loop never waits on a call. It runs
in a background thread, and the previous verdict stays on screen until the new one lands.

The friction was all in token budgets: reasoning is billed and counted against max_tokens,
and with no separate budget or effort control, a call can spend its entire allowance
thinking and return an empty answer. That cost me a full afternoon before I understood it.
```

### What would have made the experience more effective?

```
1. A reasoning budget or effort control. max_reasoning_tokens, or reasoning_effort
   low/medium/high. Right now the only defence against a model polishing one console line
   past 8,000 tokens is prompt folklore plus a generous max_tokens, and a call that
   over-thinks returns finish_reason "length" with nothing usable.

2. A guaranteed answer/reasoning split. The same one-word prompt put the answer in
   content on all four models, but Lightning also wrote its thinking INTO content on
   longer tasks, and Nano exposes no reasoning_content at all. Documenting which models
   expose reasoning, and guaranteeing content holds only the answer, removes a whole
   class of defensive parsing.

3. response_format / JSON schema on the reasoning models. Everything structured in Virta
   is "answer ONLY with JSON" plus parsing the last valid JSON block plus validation.
   It works. It's also several hundred lines that wouldn't need to exist.

4. Cost in the usage object, or per-model pricing on /v1/models. Virta has a spend ceiling
   it cannot enforce in euros, because the app cannot find out what a token costs.

5. Prompting guidance per model family. The verbatim-example-copying behaviour is
   specific enough that it belongs in the docs.
```

### What do you most hope to see from the Nemotron team next?

```
Control over how much the model thinks, as a parameter rather than as prompt wording. That
is the single change that would most improve an application like this one, where the same
endpoint serves a 2k-token line every few minutes and a 5k-token analysis once a night.

Second: a small reasoning model that holds a voice. Nano got every fact right and could
not be persuaded to stop writing like a status readout. That is what pushed the live tier
from a 30B model to a 120B one. A Nano that keeps a persona would let a lot of this run
much cheaper, which matters when the whole premise is cheap hardware.
```

### Did you use Tavily?

```
No
```

Checked: no Tavily call anywhere in the repo. Answer no — claiming otherwise fails the
"functional runtime call" requirement.

### Builders & Brews IRL city

```
Leave unselected.
```

This one is only for people attending one of the 20 IRL hackathon events, and it is what
makes you eligible for the City Winner Award. If you did go to one, select that city —
not where you live. If you didn't, an entry here is wrong.

### The two checkboxes

Age of majority, and not an employee of the promotion entities. Both yours to tick.

---

# Before you submit

- [x] **`profiles/` and `labels/` ship as example data.** Decided. Both are already
      tracked in git, so nothing to do — but drop the README's open question about it
      (end of the Labelling section: "decide before the repo goes public whether it
      should be in it") and say they're published on purpose instead. An open question
      in a README reads as an oversight.
- [x] **Token Factory line added to the README.** Line 13 now links FEEDBACK.md as
      "where Token Factory sped the build up", which is what the form asks the repo to
      highlight. The demo video and Devpost links sit on the same line.
- [ ] Confirm `.env` is not in the repo and `.gitignore` still covers `data/`, `csv/`, `var/`.
- [ ] Push the current work. Last commit is 0e4993f, 2026-09-19.
- [ ] Repo public, MIT license file present, README setup steps verified on a clean clone.
- [x] Video uploaded: https://youtu.be/raFAFtf1x2k — still open it once in an incognito
      window. An accidentally-private link embeds as nothing on Devpost.
- [ ] First gallery image cropped 3:2 — it is the thumbnail everywhere.
