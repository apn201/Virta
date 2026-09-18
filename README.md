# Virta

**A cheap meter and a reasoning model: cheap sensor, expensive brain.**

Virta turns one ~50 EUR Shelly 3EM on the main supply into an energy advisor that
*understands* the house. Better sensors exist and so do better load disaggregators;
Virta doesn't compete with them. It concedes the sensing and puts the value in what you
**conclude** from a crude signal. NVIDIA **Nemotron** on **Nebius Token Factory** does
that reasoning. Detection (NILM) is only the foundation it reasons over.

Nebius x NVIDIA Global AI Hackathon, Physical AI track. Spec: `virta-spec.md`.

## What it does - three tiers of reasoning (spec §6A)

| Tier | When | What Nemotron does | Voice |
|---|---|---|---|
| **1 - Live** | on a situation change, capped | what's running + the real Nordpool curve -> defer or not | clean instrument |
| **2 - Nightly** | once a night, one deep call | the house's history -> behaviours, efficiency, anomalies, discovery | dry, a little HAL |
| **3 - Surface** | all day, free | the console serves last night's conclusions | - |

Real output from this house, 18.09.2026:

```
LIVE     PRICE HIGH. DEFER EV -> 14:30. EST 7.3 C/KWH CHEAPER.
LIVE     NOTHING TO SHIFT. PRICE EXPENSIVE.            (kettle + toaster: never nag)
NIGHTLY  BASE LOAD 987W CONSTANT, 73% OF DAILY ENERGY
NIGHTLY  SHIFT EV CHARGER TO 03:45-05:36 SAVING ~2.08 EUR/MONTH
NIGHTLY  1532W PULSED LOAD DETECTED 10 TIMES, EVENING-BIASED
DISCOVER C-440W-36m: +443 W on C, 36 min, mornings + evenings -> "infrared heat lamp (50%)"?
```

A detector knows what is on now. It has no memory and no idea of "usual", so it can
never say that the always-on base load is three quarters of the bill, or that a
charge would have cost a quarter as much 5 hours earlier. That sentence is the product.

**Where Nemotron is used** (all through Nebius Token Factory, `virta/nebius_client.py`):
live advice (`virta/advisor.py`), nightly insights (`virta/nightly.py`), and load
discovery / labelling (`virta/labeller.py`).

## Honest by construction

- **Every price is real.** Each run is costed slot by slot at the Nordpool price in
  force while it ran; "could have cost" uses the cheapest real window of the same length
  that day. Only per-month figures are estimates, and they're labelled as such.
- **Every number on a console line is checked** against the digest the model was given;
  a line with a number that isn't in it is dropped.
- **The meter, not the person.** Insights about presence, sleep, guests or health are
  dropped in code, not just discouraged in the prompt.
- **Propose, then confirm.** Nemotron's guesses about unknown loads are hypotheses; nothing
  becomes a profile until you name it (`labels/worklist.md`). Advice never switches anything.
- **Private by design.** Raw power never leaves the house. Nemotron sees abstractions only:
  running loads, the price curve, a compact nightly digest (`var/digest.json` - always
  inspectable).
- **Local memory beyond HA's 14 days.** Power and price history is archived daily into
  `data/archive/` and kept forever, so patterns longer than the recorder window stay visible.

## Status

Build order per spec §12:

1. **Nebius hello-world** - one Nemotron call, `reasoning_content` handled.
2. **Cost safeguards + config** - situation hash, debounce, hard caps, kill switch, one
   shared ledger.
3. **Real data** - HA history + CSV exports, spec §3.5 parsing rules, plots.
4. **Baseline + stacking detector** - the sensing layer: quiet-stretch base load, stacked
   step detection, MATCHED / UNKNOWN / BASELINE.
5. **Labelling** - recurring unknowns clustered locally, Nemotron proposes, you confirm.
6. **Live loop + context scoring** - HA every 15 s, time-of-day / darkness / co-occurrence.
7. **Tier 1 live advice.**
7b. **Tier 2 nightly understanding** + the local archive + Tier 3 surfacing.
8. **Console** - the single-screen instrument, windowed on the PC for development;
   fullscreen HDMI on the Pi is the same code.

Next: deploy to the Raspberry Pi 3. Optional after that: HA action path, M5 panel.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set three env vars (or copy `.env.example` to `.env` and fill it in — real env vars win over `.env`):

| Variable | Required | Default |
|---|---|---|
| `NEBIUS_API_KEY` | yes | — |
| `NEBIUS_MODEL_ID` | yes | — (exact id from the Token Factory catalog) |
| `NEBIUS_BASE_URL` | no | `https://api.tokenfactory.nebius.com/v1/` |
| `NEBIUS_TIMEOUT_S` | no | `60` |
| `NEBIUS_MAX_TOKENS` | no | `2048` |

```powershell
$env:NEBIUS_API_KEY  = "<your Token Factory key>"
$env:NEBIUS_MODEL_ID = "nvidia/<exact-catalog-id>"
```

## Run

| Command | What it does | Costs money |
|---|---|---|
| `python -m virta.hello_nebius` | one Nemotron call; prints the answer and which field it came from | one call |
| `python -m virta.check_safeguards` | simulates a day of ticks, proves the guardrails hold | no |
| `python -m virta.check_ha` | reads every spec §11 entity live off the LAN | no |
| `python -m virta.inspect_power` | pulls power history from HA, reports it, plots each day | no |
| `python -m virta.inspect_power --csv <file>` | same, from a states export | no |
| `python -m virta.replay [--fetch] [--day YYYY-MM-DD]` | runs history through the detector, prints events + end state | no |
| `python -m virta.check_detector` | synthetic §5.2 cases + the labelled 16.09 test run | no |
| `python -m virta.labeller [--refresh] [--no-llm]` | clusters unknowns, acts on answers, updates `labels/worklist.md` | at most one call |
| `python -m virta.live [--minutes N] [--once] [--quiet] [--dry] [--no-nightly]` | the live loop: advice on situation change, nightly pass at 03:30 | gated calls |
| `python -m virta.nightly [--no-llm] [--show-digest]` | archive + discovery + digest + one deep insight call | up to two calls |
| `python -m virta.console [--size WxH] [--fullscreen]` | the instrument, drawn from `var/state.json` | no |
| `python -m virta.console --snapshot out.png` | render one frame to a file, no window | no |
| `python -m virta.advisor [--scenario ev\|kitchen\|idle] [--show-payload]` | one advice call, from `state.json` or a scenario on the real price curve | one call |

`inspect_power` needs matplotlib: `pip install -r requirements-dev.txt` (PC only - the Pi
never plots). It caches the pull to `data/power_history.csv` and writes
`var/plots/power_<date>.png`.

Exit codes: `0` ok · `1` call failed · `2` config problem · `3` model returned nothing.

## Cost control (spec §8)

The local loop polls HA every ~15s for free; the cloud loop is gated. A cloud call needs
all four layers to agree:

1. **Kill switch** — `VIRTA_CLOUD_ENABLED=0` or the file at `CLOUD_KILL_SWITCH_PATH`.
   The file form stops a running Pi without touching its environment.
2. **Hard caps** — per-hour and per-day call counts plus an optional spend ceiling,
   checked before every call and persisted to `var/cloud_usage.json`, so a restart
   cannot hand a crash-loop a fresh budget.
3. **Trigger** — the situation hash `(active appliances, price tier, is_dark)` changed,
   or `tomorrow_valid` flipped true, or the heartbeat is due.
4. **Debounce** — a settle window, so one real event produces one call.

Everything fails closed: any doubt serves the cached verdict instead of calling.

Measured on a simulated day at the real 15s rate: **28 cloud calls instead of 5,760**, a
99.5% reduction. A sensor flapping faster than the settle window is absorbed by debounce;
one flapping slower gets exactly `MAX_CLOUD_CALLS_PER_HOUR` calls and is then held.

Price tiers are coarse on purpose, so small price wiggles don't trigger calls. Defaults
are 5 / 8 / 12 c/kWh, and negative prices are their own tier because "you are paid to
consume" is advice-changing on its own (spec §3.2). HA itself uses a moving-average
threshold, so treat the fixed numbers as a starting point.

## What the real data showed (slice 3)

- **`sensor.3em_total_power` is exactly A+B+C, recomputed after each phase update.** The
  3EM pushes all three phases within ~5 ms, so total fires three times per push and the
  first two are partial sums (new A + stale B + stale C). `history.collapse_bursts` keeps
  only the last. Without that, every event is triple-counted and an EV switch-on shows
  three fake stair-steps in 10 ms. Raw rows 51k -> 16k real readings.
- **Real spacing:** median 15 s, p05 2 s, max 103 s. Long gaps are held values - HA
  records changes, so plots are step-drawn rather than interpolated.
- **Base load ~965 W** (A 357 / B 168 / C 438), matching spec §4: ventilation, fridge, freezer,
  servers, cryptominers - always on, measured as one bundle. Phase A has a ~50 W dip
  roughly every 3.5 h - a background cycler inside the base load itself, below the detection
  scope but relevant to slice 4's baseline drift.
- **EV** (Wed 16.09 08:51-10:42): 11.5 kW mean, rise +3484 / +3433 / +3657 W, clean and
  balanced. The only session in the ~2.4 days recorded so far.
- **Load control is partial, not total masking.** During that session a ~+450 W load on
  phase C switched on and the total rose with it (11.5 -> 11.9 kW). So "other detections
  unreliable while EV charges" is the right flag, but loads aren't necessarily invisible.
- **Recurring ~+450 W flat steps on phase C**, 35-40 min each (16.09 20:50, 17.09 07:45
  and 18:35). Too big for one halogen (~260 W); the likely candidates are both halogens
  together, or an unlabelled C-phase light. Needs a label.
- **Cycling on phase B, 16.09 16:50-17:55:** about eight short ~+1.5 kW pulses. A
  thermostat pattern - an oven is the obvious suspect, which would make this the first
  Tier-2 signature in the data.

## Seed profiles (builder test run, Wed 16.09 morning)

`profiles/*.json`, spec §7 schema, measured from the burst-collapsed readings:

| Load | Phase | On | Off | Notes |
|---|---|---|---|---|
| EV charger | A+B+C | +3.4 kW per phase, in two stages 2 s apart | one step | balanced to ~2% |
| Kettle | B | +2016 W | -1984 W | spec's ~1200 W was a 1-min average; real step is 2 kW |
| Toaster | B | +931 W | -847 W | smaller off-step once the element is hot |
| Halogen 1 | C | +271 W | -257 W | |
| Halogen 2 | C | +197 W | -188 W | spec said ~260 W; tight tolerances keep the two apart |
| Sauna 1, Oven 1, Coffee maker | - | nothing | - | no step >100 W on any phase, in or after the window |

Coffee's full cycle at 09:26 fell inside the EV session and still showed nothing. Masking
by load control would have left an imbalance (the coffee's phase up, the others down),
and there wasn't one.

## Detector (slice 4, spec §5.1-5.3)

`virta/baseline.py` finds the base load from **quiet stretches**, not from the clock: at least
20 minutes of low, stable readings with nothing on the stack, one best stretch per day,
and the base load is the median of the last 5 days. Across the three recorded days it
held at 986-995 W, with each phase varying by only a few watts.

`virta/detector.py` keeps `accounted[phase] = base load + drift + active loads` and treats
every settled deviation as an event, measured against what is already on. So a toaster
is recognised on top of an unknown load. Both replay and the live loop feed it one
reading at a time.

On the recorded history, every load from the 16.09 test run was detected at the right
time (halogen 1 87%, halogen 2 87%, kettle 95%, toaster 89%, EV 98%). The 29 ON and 29 OFF
events all pair up, there are no unexplained drops, and the stack ends empty with the
residual within 1 W.

Rules the real data forced:
- **One device can't be on twice.** If a profile matches while it's already on, its OFF was
  missed; the earlier instance is closed as `OFF_RECONCILED`, otherwise it would keep the
  stack non-empty and block every later recalibration.
- **OFF steps match more loosely than ON steps.** They're only compared against loads that
  are on, and real draws drift: a halogen measured +230 W on and -194 W off.
- **While the EV charges, anything the three phases do together is the charger**, even if
  only one phase crossed the threshold (on 16.09 at 09:32 it drifted +123/+95/+90 W).
  When the EV switches off, anything beyond its own draw is matched as other loads ending.

**Base parts switched off.** A drop that no running load explains means part of the
base load itself went off (a PC, a server, ventilation stepping down). It joins the
stack as a **negative** unknown load (`BASE_OFF`, e.g. `BASE PART OFF -150W A`) and
pairs with the matching step back up (`BASE_ON`, with its off-time). It's clustered
and labelled like any unknown, and a named one gets a profile marked
`base_component: true`. While a part is off, calibration adds it back, so the base load
keeps meaning "everything normally on". A part off for over 24 h becomes `BASE_SHIFT`,
a lasting drop of the base load, which the nightly pass can report. The nightly digest
lists these parts with how long they were off and what that saved at real prices.
Switching base parts off never triggers live advice calls.

Recurring unknowns, waiting for §5A labelling:
- **+~440 W on C, lasting 36-37 min every time**, seen 5 times (mornings ~07:45 and
  evenings). Such a consistent length suggests a timer.
- **+~160 W on B, 2-8 min**, mostly evenings and at night.
- **~1.5 kW pulses on B, 20-60 s each**, 16.09 16:49-17:56. Looks like a thermostat cycling.

## Labelling (slice 5, spec §5A)

You and the system talk through **`labels/worklist.md`**. The pass writes what it found;
you answer on `your_label:` whenever you like; the next pass acts on the answer.

- **Clusters** are built locally from the event log: at least 3 unknown loads of similar
  size, phase and duration. IDs like `C-440W-36m` are carried forward by signature in
  `labels/clusters.json`, so an answer can't drift onto a different load overnight.
- **Answers:** a clean name (`fridge`) is promoted straight to `profiles/fridge.json`
  (a draft, no LLM involved) and the detector recognises it from then on. A comment in
  prose goes to Nemotron, which proposes a label that you confirm. `ignore` rejects the
  cluster; `later` keeps it open.
- **Timeframe labels:** a line like `2026-09-16 07:42-07:44 kettle` finds the step in
  that window. A known load is checked, a new name becomes a draft profile, and a window
  with nothing in it says so ("the 3EM did not see it").
- **Nemotron** is called once per pass, only when there's something new, through the
  same cost gate as live advice. Only cluster summaries are sent - never raw power.
- **Your input is never overwritten.** The pass regenerates only its own fields
  (`seen`, `llm_guess`, `llm_why`, `llm_reply`, `pass_note`, `profile`, `status`), keeps
  everything else byte-for-byte, writes atomically, and keeps a `.bak`. Tested with
  multi-line comments and extra lines added by hand; running it twice gives an
  identical file.

Measured: three clusters took **~10,800 reasoning tokens** in one call. At 4,096 the
model ran out mid-thought (`finish_reason: length`) and never produced its JSON, so the
labelling call gets 12,000 and the client now reports truncation explicitly. The last
raw reply is always kept in `var/last_llm_reply.txt` for debugging.

`labels/` describes household routines (what runs when). It's useful demo material, but
decide before the repo goes public whether it should be in it.

## Live loop (slice 6)

`python -m virta.live` is what the Pi will run. Every 15 s it reads the three phase
sensors over the LAN and feeds the same detector and baseline code the replay uses.
Total power is A+B+C computed locally; the HA total sensor is the same sum, but it can be
read mid-update. Price, darkness and temperatures refresh every 60 s.

- **Base load:** on first start it's calibrated from quiet stretches in the last 24 h of
  history, then saved to `var/baseline.json`; later starts resume from that file.
- **Context scoring** (`virta/scoring.py`, spec §7) adjusts the signature confidence
  using each profile's plain-language factors. A usual time of day adds a little and an
  unusual one takes some away (a kettle at 03:30 drops from 95% to 85%, with the reason
  given). A correlated device already on adds a little, twice as much when the profile
  says "stronger in the dark". A context-demoted match is flagged unusual: suggestion
  only.
- **Cost gate:** runs on every tick; when it says call, the advice call (slice 7) runs in
  a background thread so the 15 s loop never waits. `--dry` logs `WOULD CALL NEMOTRON`
  instead.
- **Output:** `var/state.json` (current loads, base load, price, context, live verdict, nightly commentary - what the HDMI
  console will draw) and `var/events_live.jsonl` (mined by the labelling pass).
- **HA outages** are logged and retried with backoff, never fatal.

## Advice (slice 7, spec §6)

`virta/advisor.py`. Nemotron gets the situation as JSON: running loads with confidence,
unusual and EV-unreliable flags; unknown loads by size and phase; the Nordpool curve in
15-minute slots from now on (plus tomorrow once published); temperatures, darkness, the
time; and `usage_profile.txt`. **Never the raw power stream.**

The edge does the price arithmetic first and hands it over as facts: cheapest 1 h and
3 h windows, the curve's range, negative slots, and each load's running cost now. The
model's job is judgement - is it worth it, what about deadlines, how sure are we of
what's running - not arithmetic.

It returns one console line and a JSON action, which is parsed and validated. The
appliance must be running and `defer_until` must be HH:MM; a defer without an appliance
becomes `none`. A missing verdict line is rebuilt from the JSON. Advice only: nothing is
ever switched.

Measured against the real curve on 18.09 (price 10.12 c/kWh, EXPENSIVE, tomorrow not
published yet):

| Scenario | Verdict | Tokens |
|---|---|---|
| EV charging now | `PRICE HIGH. DEFER EV -> 14:30. EST 7.3 C/KWH CHEAPER.` | 4.4k |
| kettle + toaster | `NOTHING TO SHIFT.` - "short loads, never worth deferring" | 3.0k |
| live, house idle | `NOTHING TO SHIFT. PRICE EXPENSIVE.` | 2.1k |

The EV's 7.3 is correct: 10.12 now minus the 2.79 average of the cheapest 3 h window.

Fixed along the way: with a literal example in the prompt, the model once wrote "PRICE
NORMAL" at 10.1 c/kWh (EXPENSIVE). The prompt now uses a `<TIER>` placeholder, and any
price word in the verdict that's wrong for the real tier is corrected at the edge.

**Calls are counted when sent** (tokens added when the answer arrives), so a crash
mid-call can't hand a restart a fresh budget. Only one call is in flight at a time, and
a failed call keeps the previous verdict on screen.

`usage_profile.txt` is **starter text taken from the spec's example**. Rewrite it with
your real habits: Nemotron reads it on every call.

## Nightly understanding (Tier 2, spec §6A, slice 7b)

`python -m virta.nightly` - and the live loop runs it itself once a night at `NIGHTLY_AT`
(default 03:30), in a background thread, on the same cost gate.

1. **Archive** (`virta/archive.py`): copy every day HA still has into
   `data/archive/{power,prices}/YYYY-MM-DD.csv.gz`. Days are frozen once settled, so they
   survive HA's 14-day purge. About 400 KB a day.
2. **Replay** the window through the same detector -> events and daily base loads.
3. **Discovery**: the labelling pass (clusters, your answers, Nemotron's proposals).
4. **Digest**, computed at the edge. It has:
   - the energy split: base load / identified / unknown / other
   - each run's real cost, and the cost of the cheapest real same-length window that
     day (for shiftable loads)
   - habits by part of day, and days since each load last ran
   - one row per archived day (`data/archive/daily.json`, kept forever)
5. **One deep Nemotron call** -> up to 6 insights in the dry voice. It's validated at the
   edge (numbers grounded, meter-not-person, shiftable-only suggestions, at least 0.50
   EUR/month) and cached in `var/insights.json`.
6. **Surface**: the live loop rotates one insight into `state.json` every 3 minutes, free.

The **base load** is the static always-on load (ventilation, fridge, freezer, servers,
cryptominers, about 1 kW), bundled on purpose. It's measured, not itemised; any part
that later switches on and off will show up as an unknown load. It is **not** floor
heating.

What the first real pass showed, and what it forced:

- **The base load is 73% of all energy** (59 of 81 kWh over 2.5 days) and EUR 0.71 of the
  EUR 0.82 daily spend. That's the headline no detector produces.
- **Early drafts overreached.** One called the EV "overdue" (after one session) and one
  suggested shifting halogen lights to night. Fix: profiles carry `shiftable`, efficiency
  needs at least 0.50 EUR/month, and "something stopped" needs an established routine.
  In the prompt *and* checked at the edge.
- **A 15-minute shortcut isn't a real price.** Runs are now costed slot by slot, and the
  alternative is the cheapest real window of the run's own length.
- **The model's explanations can still slip** (one said the EV charged "overnight"; it
  charged 08:51-10:42). The console shows only the checked line; the explanation is kept
  in the file labelled `model_explanation`.

## Console (slice 8, spec §9)

`python -m virta.console` opens the instrument in a window; `--fullscreen` is the Pi's
HDMI screen. It only **reads** `var/state.json`, which the live loop writes, so run both:

```
python -m virta.live        # terminal 1: the loop (writes var/state.json)
python -m virta.console     # terminal 2: the instrument (reads it)
```

The two run independently, in any order. The console reads the file once a second and
builds its own trace if a state file lacks one.

One renderer (`ConsoleRenderer`) draws onto any surface: a window here, the framebuffer
on the Pi, and later an off-screen surface for the M5 panel. Layout: three framed panels
with 40 px margins (`--bezel N`, at the 1280x720 design scale) for a 3D-printed bezel.
Legends sit inside the frames, so the bezel covers nothing.

- **DYNAMIC** - a 20-minute step-drawn trace (total + phases A/B/C, direct-labelled, the
  base load dashed) and a Ferraris meter disc, seen edge-on through its window. Its rim
  graduations slide past at a speed proportional to the watts.
- **DATA** - total, phases, base load, price and tier, context, and a detection table:
  **LOAD · SURE · W · ON · COST**. SURE is how well size, phase and time of day match the
  profile. COST is the session so far at the real Nordpool price of every slot it has
  spanned, and a base part switched off shows the money it's saving. Sessions that ended
  in the last 30 min stay listed under ENDED with their final cost.
- **ADVICE** - the live verdict (instrument voice), last night's rotating insight (dry
  voice), today's real price strip with the current slot marked, and cloud calls used.

Rules from the spec: **motion = information** (only the trace and the disc move, and only
because the data does); phosphor green on near-black; **amber only for alerts and
expensive prices**. If `state.json` is more than 45 s old the console says **NO SIGNAL**
in amber and the disc stops, so a dead loop can't pass for a quiet house.

`--snapshot` renders a frame without a window (for checking layout, e.g. at the Pi's
resolution with `--size 800x480`). `python -m virta.live --state-out <file>` lets a test
run write elsewhere than the running loop.

## EV rule (builder decision)

The Eve charger sends nothing to HA. The EV is detected purely by its signature - the
only balanced three-phase load, ~8-12 kW - in `virta/ev_signature.py`, with hysteresis so
a throttled charger stays "charging". **While the EV charges, every other detection is
flagged unreliable**, because the charger's load control can absorb other loads.

## Layout

- `virta/config.py` — env-driven config: Nebius, Home Assistant, cost control.
- `virta/nebius_client.py` — OpenAI client pointed at Nebius; `reasoning_content` handling.
- `virta/cost_control.py` — price tiers, situation hash, debounce, caps, spend ledger.
- `virta/ha_client.py` — read-only HA REST client (stdlib only, Pi-friendly).
- `virta/history.py` — CSV + HA history loader, spec §3.5 rules, burst collapse, phase alignment.
- `virta/ev_signature.py` — EV charge sessions by signature; the "others unreliable" flag.
- `virta/inspect_power.py` — slice 3 report and plots.
- `virta/baseline.py` — quiet-stretch, rolling, per-phase base load (streaming, persists to JSON).
- `virta/detector.py` — the stacking detector and event log.
- `virta/profiles.py` — loads `profiles/*.json`.
- `virta/replay.py`, `virta/check_detector.py` — slice 4 runners.
- `virta/clustering.py` — recurring-unknown clustering, stable signature IDs.
- `virta/worklist.py` — the worklist file: parse, render, never overwrite human input.
- `virta/labeller.py` — slice 5 pass.
- `virta/scoring.py` — context scoring (time of day, darkness, correlations).
- `virta/live.py` — slice 6 live loop.
- `virta/advisor.py` — Tier 1 live advice: payload, prompt, validated parsing.
- `virta/nightly.py` — Tier 2 nightly: digest, deep call, grounding + guardrail checks.
- `virta/archive.py` — the local power/price archive, kept forever.
- `virta/console.py` — the instrument: one renderer, windowed or fullscreen.
- `virta/fsutil.py` — atomic writes that retry through a briefly locked file (Dropbox on the dev PC).
- `usage_profile.txt` — your habits, in plain sentences (spec §3.4).
- `labels/` — `worklist.md` (yours to answer) and `clusters.json` (the pass's memory).
- `profiles/` — device profiles (spec §7), hand-editable JSON, read by the detector and the prompt.
- `virta/hello_nebius.py`, `virta/check_safeguards.py`, `virta/check_ha.py` — the runners.
- `archive/openclaw-prompts/` — unused; HA is now read directly over REST.
- `csv/`, `data/` — household power data. Git-ignored: raw power never leaves the house (spec §11).
- `archive/` — superseded documents.

## Slice 1 result (confirmed against the real endpoint)

`nvidia/Nemotron-3_5-Lightning` returns **both** fields: the answer in `content`, the chain of
thought in `reasoning_content`. So `content or reasoning_content` is the right precedence — the
fallback is insurance, not the normal path, for this model.

It spent **165 completion tokens to answer "ONLINE"** — the thinking is billed and counted
against `max_tokens`. If `max_tokens` is set too low, the model can exhaust the budget mid-thought
and return empty `content`, which surfaces as exit 3. Hence the `2048` default.

Consequence for later slices: when parsing a JSON verdict, parse `content` first. Only fall back
to `reasoning_content`, and when you do, expect prose around the JSON — extract the last JSON
block rather than trusting the whole string.

## The two Nemotron gotchas (spec §2, §6)

1. **Reasoning models may leave `content` empty and put the answer in `reasoning_content`.**
   Handled once, in `_extract_text`, which also reports the source field.
2. **No function/tool calling through the OpenAI wrapper.** `chat()` never passes `tools=` or
   `response_format=`; structured output will come from prompting for JSON and parsing it.
