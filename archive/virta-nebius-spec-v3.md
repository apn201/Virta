# VIRTA - Load Disaggregation & Price-Aware Energy Console - Spec v2

Working title: **Virta** (Finnish: electric current / flow). Yours to change.
Target: Nebius x NVIDIA Global AI Hackathon, **Physical AI Track**. Deadline: Oct 30, 2026, 10:00am PDT. Online, solo, Finland eligible. $50k pool; every track winner also gets an NVIDIA Jetson Orin Nano.

This v2 replaces the assumption-based data sections of v1 with the builder's REAL Home Assistant system: real sensor names, the real Nordpool attribute shape, the real recorder situation, and an OpenClaw-driven workflow for HA data/config. Everything in v1 about architecture, the three-device console, the Physical-AI framing, and the honest-scope NILM caveats still holds - this document is the data-and-integration source of truth that makes it build-ready.

## 0. If you're new to Nebius/Nemotron (unchanged from v1, summarized)

Nebius Token Factory = an OpenAI-compatible endpoint. Same `openai` Python client you used for Claude/GPT; change base URL, key, model id. No separate SDK.
- Base URL: `https://api.tokenfactory.nebius.com/v1/` (confirm in the Token Factory console).
- Key: `NEBIUS_API_KEY` env var. Get it from the Token Factory console; request participant credits from the hackathon Resources tab early.
- Model id: exact string from the Token Factory catalog, e.g. an `nvidia/nemotron-*` id. Confirm current id in the Playground.
- **GOTCHA:** Nemotron models on Nebius are REASONING models - the answer comes in `reasoning_content`, and `choices[0].message.content` may be EMPTY. Read `reasoning_content` when content is empty. Also: these reasoning models don't support function/tool calling via the OpenAI wrapper - get structured output by asking for JSON in the prompt and parsing it, not via tools. Requirement: at least one NVIDIA open-source model must be used - Nemotron satisfies it. Test one call in the Playground before writing loop code.

## 1. What this is (unchanged)

A power-monitoring console that reads the whole-house 3-phase draw, detects which major appliances are running from their load behavior, and REASONS over that plus the live Nordpool price to advise on deferring loads. "Sauna + an oven are on, price is high for the next 90 min then drops - defer the sauna, ~X snt saved, no comfort lost." NILM at house scale as a PoC, with an industrial scale-up story from the builder's OT/energy day job. Housed in a 3D-printed spaceship-console holding three M5 devices (sense / reason / act-display). Edge does signal work locally; Nemotron on Nebius does the reasoning.

## 2. THE REAL DATA - inputs, exactly as they exist

### 2.1 Power input: Shelly 3EM via native Shelly HA integration
Confirmed live entities (native Shelly integration, already working):
- `sensor.3em_channel_a_power` - phase A active power (W)
- `sensor.3em_channel_b_power` - phase B active power (W)
- `sensor.3em_channel_c_power` - phase C active power (W)
- `sensor.3em_total_power` - summed total power (W), a user-created min_max/template sensor
- `sensor.3em_channel_a_energy` / `_b` / `_c` - cumulative energy (kWh), ever-increasing

**Recorder situation (important):** the `_power` sensors EXIST and work live but were EXCLUDED from the HA recorder via `entity_globs: sensor.3em_*_power` and `sensor.3em_total_power` in the entities exclude list. The fix (being applied via OpenClaw) removes those exclusions so power is recorded. After the change:
- 14 days of raw power history retained (`purge_keep_days: 14`).
- Long-term statistics (hourly min/max/mean) kept indefinitely because these sensors are `state_class: measurement`.

**Sample rate: UNCONFIRMED, must be measured after recording is enabled.** The earlier energy export logged ~1 sample/minute. The Shelly native integration MAY push power every 1-2s or may be throttled to ~30-60s. This decides the detector design:
- If ~1-2s: real switch-on transients/inrush are visible -> transient-shape fingerprinting is possible.
- If ~30-60s: step-detection + steady-state signatures only (still fine for big loads).
Build the detector to work at the coarse rate first; treat transient fingerprinting as an upgrade only if the real rate supports it.

**Power reconstruction fallback:** if for any window only energy (kWh) is available and not power, power = Δenergy/Δt (see `states_history` parsing rules, 2.4). Prefer the real `_power` sensors once recorded.

### 2.2 Price input: Nordpool via HACS `custom-components/nordpool`
Sensor: `sensor.nordpool_kwh_fi_eur_3_10_0255`
- Region FI, EUR, reported in **snt/kWh** (cents), name decodes as 3 decimals / 10% VAT included / 0.0255 fixed adder. So the value is an all-in, VAT-inclusive price - the honest number for savings math.
- **State** = current price (snt/kWh).
- **Attributes carry the curve** (read via `state_attr`, NOT separate entities):
  - `raw_today` - array of `{ "start": <ISO timestamp>, "value": <price snt/kWh> }`
  - `raw_tomorrow` - same shape; EMPTY until tomorrow's prices publish (~13:00 CET). Also `tomorrow_valid` boolean.
  - (also plain `today`/`tomorrow` value arrays, plus min/max/avg attrs - prefer `raw_*` because each price carries its own timestamp.)
- **Slot count: read the array LENGTH at runtime.** Nord Pool moved to 15-minute settlement periods; the array may be 96 (15-min) or 24 (hourly) depending on integration version. Never hardcode 24.
- **Negative prices occur** (Finland, wind oversupply). Reasoning must treat negative price as "run loads now / you're paid to consume." Do not assume price > 0.
- Downstream helper already present: `sensor.cheapest_hours_energy_tomorrow` (built on the Nordpool data). Can be used as a hint, but the reasoning should consume `raw_today`/`raw_tomorrow` directly.

Reference chart the builder uses (confirms shape):
```
entity.attributes.raw_today.map((e) => [new Date(e["start"]).getTime(), e["value"]])
entity.attributes.raw_tomorrow.map((e) => [new Date(e["start"]).getTime(), e["value"]])
```

### 2.3 Usage patterns / preferences (builder-supplied, static text for now)
A short profile fed into reasoning, e.g. "sauna usually 20:00-22:00; EV must be charged by 07:00; ovens are dinner-time; floor heating is comfort, deferrable within reason." Hand-written to start; could later be learned from history.

### 2.4 The historical export files (for OFFLINE signature-building only)
Two CSVs exist from HA. Live data drives the running system; these are only to build the initial signature library offline.
- `..._States_20260915.csv` - short-term states history, Sept 1-15. Long format: `entity_id,last_updated,state`. ~1 sample/min on the 3EM. **Only the ENERGY and (once recorded) POWER rows are useful.**
- `..._20260915.csv` - hourly long-term statistics, 5 months. Too coarse (hourly) for NILM; ignore for detection, usable only for coarse usage-pattern context.

**Parsing rules for the states CSV (bake into code):**
- Values may be the strings `unavailable`, `unknown`, or empty - SKIP these rows, don't `float()` them (this crashes naive code).
- Timestamps are irregular (48-74s apart) - compute power from ACTUAL elapsed time between samples, never assume a fixed interval.
- Power from energy: `W = (E[i]-E[i-1]) / (t[i]-t[i-1] in seconds) * 3600 * 1000`, guarding `0 < Δt < 600` and `ΔE >= 0`.
- **DROP the `_energy_cost` entities entirely** - they are dummy data (a running total off a fixed rate), superseded by the live Nordpool price. They serve no purpose; purge them from any working copy so they don't confuse the pipeline.

## 3. Architecture (unchanged from v1, restated with real names)

```
Shelly 3EM (native HA integration): sensor.3em_channel_{a,b,c}_power + sensor.3em_total_power
Nordpool (HACS): sensor.nordpool_kwh_fi_eur_3_10_0255 (state + raw_today/raw_tomorrow)
        |
        v  (read from HA - live via API/websocket; history via export for training)
  EDGE LAYER (local: Pi or PC near HA)
   - ingest total power (primary) + per-phase power (disambiguation)
   - detect switching events (step changes) on total_power
   - extract signature features per event (step W, phase, duration, cycling pattern, [transient if rate allows])
   - match against learned signature library -> appliance ON/OFF + confidence ("unknown" below threshold)
   - emit compact active-appliance state
        |
        v
  CLOUD REASONING (Nemotron on Nebius Token Factory)
   - input: active appliances + Nordpool raw_today/raw_tomorrow curve + usage profile
   - output: one-line console verdict + JSON action {none|suggest_defer, appliance, defer_until, est_saving, reason}
        |
        +--> CONSOLE (3x M5 in 3D-printed panel): SENSE (live load as light), REASON (HAL/Echo shows verdict), ACT (Atom S3R terminal, existing protocol; suggest-and-confirm)
        +--> Home Assistant (optional action: notify / defer, CONFIRMED not autonomous)
```

Already done: 3EM sensing, Nordpool feed, the micro-doom terminal protocol (console nervous system), the three M5 devices. New work: recorder fix (in progress), edge signature layer, Nebius reasoning client, console roles, optional HA action path.

## 4. How the code talks to Home Assistant

Two access modes:
- **Live (runtime):** HA long-lived access token + REST API (`/api/states/<entity_id>`) or the websocket API for push. Read the 3EM power sensors and the Nordpool sensor (state + attributes). Base URL + token in env/config. This is how the running console gets current data.
- **History (training):** the exported CSVs for offline signature-building. Later, HA's history/statistics API could pull training windows directly, but CSV export is fine for v1.

Claude Code builds against config-driven HA base URL + token; you create the token in HA (Profile -> Long-lived access tokens) and set it in env. Claude Code cannot create the token or edit HA config itself - see the OpenClaw workflow (section 8).

## 5. Nebius/Nemotron reasoning (real integration)

Client shape:
```python
from openai import OpenAI
client = OpenAI(base_url="https://api.tokenfactory.nebius.com/v1/",
                api_key=os.environ["NEBIUS_API_KEY"])
resp = client.chat.completions.create(
    model="<nvidia/nemotron-* exact id from catalog>",
    messages=[{"role":"system","content": SYS}, {"role":"user","content": payload}],
)
out = resp.choices[0].message.content or getattr(resp.choices[0].message, "reasoning_content", "")
```
- Handle empty `content` -> `reasoning_content` (the gotcha).
- No tool calling; get JSON by prompting for it and parsing.
- Call event-driven (on appliance switch or price-tier change), not on a tight timer. Cheap on credits; matches "advise when it matters, don't nag."

System prompt (dry): "You are a household energy advisor. Given the appliances currently running, the Nordpool electricity price curve (snt/kWh, VAT included; values can be negative meaning you are paid to consume), and the user's usage patterns, decide whether any running or imminent load should be deferred to save money without hurting comfort or hard deadlines (e.g. EV charged by 07:00). Prices are in 15-minute or hourly slots - read the timestamps. Be specific and brief; never nag about trivial savings; if nothing is worth changing, say so in one line. Output: one line for the console display, then a JSON block {action: none|suggest_defer, appliance, defer_until, est_saving_snt, reason}."

Feed the price curve as the `raw_today` (+`raw_tomorrow` if valid) array, each `{start,value}`. Include current time so it knows 'now'. Parse the JSON for the console/HA action; show the one-liner on the REASON device.

## 6. Signature-learning workflow (real, offline-first)

1. **Get real power history:** after the recorder fix, let it collect >=1-2 days including deliberate appliance runs; export the states history for the four power sensors. (Earlier energy-only export works via reconstruction meanwhile.)
2. **Measure the real sample rate** (2.1) - decides detector capability.
3. **Event detection on `sensor.3em_total_power`:** step/change-point detection. Each event -> features: step magnitude (W), which phase rose (from per-phase sensors), duration, post-switch cycling pattern, [transient shape if rate allows].
4. **Labeling:** tool presents detected events; builder labels ("sauna", "oven 1", "EV", "IR heater") AND can label by timeframe ("21:00-21:40 sauna"), auto-labeling events in that window.
5. **Signature library:** aggregate labeled events into per-appliance profiles (step range, phase, duration, cycling fingerprint). Store as JSON.
6. **Live matching:** new event -> nearest signature above confidence threshold -> appliance ON/OFF; below -> "unknown", reported honestly.

Builder's high-value, high-signature loads: 3 ovens, 2 saunas, EV charger, electric floor heating, IR heater, coffee machine. Demo the clean separators (sauna ~3.8kW hold, EV, oven); be openly honest that two-saunas / three-ovens overlap is the hard case and the console says "uncertain." Simple event-matching beats a black box for an explainable demo; the scored intelligence lives in the Nemotron reasoning layer, not the detector.

Note: the light-control Shellys (`switch_a..u`) are NOT interesting loads - ignore them.

## 7. Console (3 M5 devices, unchanged from v1)

- SENSE (Matrix - light): live per-phase / total load as light/bars.
- REASON (Echo/HAL - theatrical heart): terse Nemotron verdict ("SAUNA + OVEN 2 ACTIVE. PRICE HIGH 90MIN. DEFER SAUNA -> 21:15. EST 2.4 snt/kWh CHEAPER.").
- ACT/ALERT (Atom S3R - existing PC-relay protocol): signals you; with confirmation triggers an HA action.
- Devices talk to the host over the existing micro-doom serial/terminal protocol.
- **Safety:** any real load control is suggest-and-confirm, never autonomous. Advice by default; action only on explicit OK. Never let it silently switch the EV charger.

## 8. OpenClaw workflow for HA (Claude Code proposes; you run via OpenClaw)

You have OpenClaw wired to your HA. Claude Code should PROPOSE OpenClaw prompts for any HA data pull or config change, and you run them - because Claude Code can't touch HA directly and OpenClaw can inspect/edit safely with your approval. Pattern for every such prompt: investigate and show before changing, show the diff, wait for approval, note if a restart is needed.

Already done this way: the recorder fix (removing `sensor.3em_*_power` and `sensor.3em_total_power` from the exclude lists). Still useful to request via OpenClaw:
- Confirm the four power sensors are recording after restart, and REPORT THE REAL SAMPLE INTERVAL of `sensor.3em_channel_a_power` (1-2s vs 30-60s).
- Export a fresh states history for the four power sensors over a window containing deliberate sauna/EV/oven runs (for signature training).
- Create/read a long-lived access token for the runtime HA API (you do this in HA UI; OpenClaw can guide).
- Optionally purge the dummy `sensor.3em_*_energy_cost` entities from recorder (they're useless; excluding them keeps the DB clean).

Claude Code: when you need HA data or a config change, output a ready-to-paste OpenClaw prompt following that safe pattern, rather than assuming access.

## 9. Build order (Oct 16-30, after Apertus; tight)

1. **Recorder fix + confirm power recording + measure sample rate** (via OpenClaw). Input side unlocked.
2. **Nebius hello-world:** one Nemotron call, handle `reasoning_content`, print a verdict. Playground first.
3. **Parse real data:** load the states CSV with the skip-unavailable / irregular-timestamp / energy->power rules; or read live `_power` sensors. Plot to eyeball signatures.
4. **Event detector** on total power; feature extraction.
5. **Labeling tool** (event + timeframe labeling) -> signature library JSON.
6. **Live matcher** -> active-appliance state with confidence.
7. **Reasoning integration:** assemble appliances + Nordpool `raw_*` curve + profile -> Nemotron -> parsed verdict + JSON action. The AI centerpiece.
8. **Console:** wire 3 M5 devices via existing protocol; SENSE/REASON/ACT. 3D print in parallel (wall-clock time - start early).
9. **HA action path** (optional): suggest-and-confirm defer/notify.
10. **1-min hardware clip + <=3-min demo video (audio MUST explain Token Factory + NVIDIA model use) + public repo (OSI license at top, README) + Devpost writeup + Nebius/NVIDIA feedback.**

Slices 1-7 = working system + findings. 8 = the physical demo the track requires. 9 = bonus impact.

## 10. Submission checklist (from rules, unchanged)
- NVIDIA open-source model (Nemotron) on Nebius Token Factory. Required.
- Track: Physical AI. Working-demo URL NOT required for Physical AI.
- Video <=3 min, public YouTube, audio explains Token Factory + NVIDIA model use, >=1 min of hardware operating.
- Public repo with OSI license visible at top + README with setup/run.
- Feedback on Nebius/NVIDIA tools.
- If project predates submission period, note what was significantly built during it (the signature layer, reasoning, console are new).

## 11. Risks / open items
- **Sample rate unknown until recording is on** - decides transient fingerprinting yes/no. Measure first (slice 1).
- **`reasoning_content` gotcha + no tool-calling** - handle both (section 5).
- **Nordpool `raw_tomorrow` empty before ~13:00 CET** - reasoning handles short look-ahead gracefully.
- **Slot count 24 vs 96** - read array length, never hardcode.
- **Negative prices** - handle as "consume now".
- **Overlapping similar loads** (2 saunas, 3 ovens) - honest hard case; demo clean separators, report "uncertain".
- **Credits/cost** - request participant credits early; event-driven reasoning calls.
- **October fatigue** - third October hackathon (after Palsta, Apertus). Deadline Oct 30 gives breathing room after Apertus (Oct 16). Highest-reuse project on the board (existing sensing, Nordpool, devices, terminal protocol) - the argument for doing it, if evenings allow.

## 12. Open decisions (human)
- Name (Virta = suggestion).
- Device-to-role mapping (Matrix=SENSE, Echo/HAL=REASON, Atom S3R=ACT) - adjust to each device's I/O.
- Nemotron model id (from catalog) + confirm `reasoning_content` behavior in Playground.
- Usage-profile text (2.3) - write your real patterns.
- Suggest-only vs confirmed-action for HA control - recommend suggest-and-confirm only.

---

# ADDENDUM (v3) - Real signature findings, detection tiers, context features

Added after a first real capture (Sept 15-16 morning, deliberate appliance runs). This is what the actual 3EM data taught us. It refines sections 2, 6 and the honest-scope discussion; nothing above is wrong, this makes it concrete and adds the two-tier detector and the context-feature model. Update again as more data (especially slow-cycler runs) arrives.

## V3.1 What the first real capture showed

Sample rate confirmed: ~10-15s steady, faster (sometimes sub-second) on change - change-driven CoIoT pushes fire right at switch-on. Good for step detection, not for sub-second inrush fingerprinting. Detector design is therefore step + steady-state + cycling, NOT transient-shape.

Per-phase averages during labeled windows (baseline total ~1000W - see V3.4):
- **EV charge:** total ~950W -> ~9060W, BALANCED across all three phases (A/B/C ~3000W each). The only three-phase load in the house. Trivially detectable, ~100% confidence on power alone.
- **Kettle:** +~1200W step on **phase B** (B 170 -> 1413). Clean, flat, non-cycling. Easy.
- **Toaster:** +~880W on **phase B** (B -> 1052). Clean. Easy.
- **Halogen1 / Halogen2:** +~260W each on **phase C** (C 440 -> ~700 / ~615). Small but distinct single-phase steps. Detectable.
- **Sauna1, Oven1, Coffee maker:** effectively INVISIBLE in this capture - their labeled windows read within noise of baseline. Reason (confirmed by user): these loads don't pull full power at switch-on. A sauna ramps and then cycles on its thermostat; an oven cycles; a coffee maker pulses. The instantaneous step at the logged minute isn't there. They need longer-window / cycling detection + context, not instant-step. (Also the log times were approximate.)

Takeaway: **power-only detection cleanly handles the loud loads and misses the slow/cycling ones.** This is exactly why context + confidence scoring is load-bearing, not decoration.

## V3.2 Two detection tiers

**Tier 1 - instant-step (DEMO-READY NOW):** loud, clean loads identified by a single step's magnitude + which phase moved. Members: EV charger (3-phase balanced ~9kW), kettle (~1200W/B), toaster (~880W/B), halogens (~260W/C). Build the demo on these. Phase assignment is a free, strong discriminator - "which phase moved and by how much" does half the identification before anything else.

**Tier 2 - pattern-over-time (ROADMAP, data collection underway):** slow/cycling loads identified by their thermostat rhythm over a window, not their switch-on. Members: sauna (bang-bang thermostat, characteristic period + amplitude once hot), oven (lower-amplitude cycling), floor heating (winter-only, slow cycle). These need a monitoring window and cycling characterization. Frame in the demo as the harder tier the system is learning - honest NILM arc, more credible than a perfect demo, and recognizing a sauna by its thermostat heartbeat is the impressive part once it lands.

The reasoning (Nemotron) layer is tier-agnostic: it receives "appliance + confidence + supporting evidence" and reasons over price identically whether Tier 1 or Tier 2 detected it. Tier 2 can improve after the demo without touching the reasoning half.

## V3.3 Cycling as fingerprint AND overlap-separator (key)

Cycling is not just a per-appliance fingerprint. It is the mechanism that makes SIMULTANEOUS-load detection tractable. In a single instantaneous total, two concurrent loads blur into one number and can't be split by magnitude. But two cyclers at different periods (e.g. sauna ~seconds-to-minutes rhythm vs floor heating ~20-min rhythm) appear as superimposed square waves at distinct frequencies - separable in the time/frequency domain even when inseparable by magnitude. So:
- Characterize each cycling load by period + amplitude + phase.
- When multiple loads are on, decompose the window by distinct rhythms present, not by trying to split one instantaneous total.
- This is the core technique for the concurrency problem and should drive the Tier 2 detector design. It is why longer monitoring windows identify loads that instant-step detection misses.

## V3.4 The high baseline (scope it, state it)

Constant ~1000W floor is real and explained: always-on servers, cryptominers, etc. Consequence: the detection floor sits above a kilowatt, so sub-~200W loads are genuinely hard to separate from baseline drift and are OUT OF SCOPE by design. State this openly in the demo ("detection floor sits above ~1kW of always-on compute; sub-200W loads are deliberately out of scope"). It turns a limitation into a credibility signal and it's true. Baseline also wanders, so treat anything near it as low-confidence unless context strongly agrees.

## V3.5 Context features as first-class inputs (not tiebreakers)

Identification = P(appliance | power event AND full context), not power alone. For habitual loads, context can be the stronger signal (a 2kW step at 07:12 weekday ~= coffee; the same step at 02:00 is something else). Context features to feed the confidence model / reasoning layer:

- **Time of day, day of week** - habitual timing (coffee morning, sauna evening). Strong for routine loads.
- **Temperature (inside + outside), from HA sensors** - use the REAL driver, not the calendar proxy. Floor heating correlates with cold, not with the date; a cold October snap should raise floor-heating probability before the calendar would. Outdoor temp gates seasonal loads (sauna far more likely than heating in July; floor heating near-impossible in July). Pull inside/outside temp entities from HA (propose an OpenClaw prompt to identify the exact entity_ids).
- **Season/month** - coarse backdrop, subordinate to actual temperature.
- **Co-occurrence** - what else is already on (sauna + its pump; EV only when car home; oven co-occurs with stove at dinner).
- **Duration + cycling rhythm so far** - how long it's run and its pattern (V3.3).
- **Recency** - when that appliance last ran.

Each feature shifts the odds up or down. Crucially, context can DEMOTE a power match: a sauna-shaped signature at 03:00 on a workday should lower confidence and flag "unusual," not blindly report "sauna." Unusual-context events are surfaced as a feature ("high draw at an odd hour"), not silently misclassified - this also handles the genuinely unexpected events gracefully.

## V3.6 Confidence scoring (explicit, surfaced)

Every detection carries an explicit confidence and a short reason string, e.g. "sauna 92% - power+phase match, matches evening pattern, pump also active" or "looks like sauna but 03:00 on a workday - low confidence, flagged unusual". Surface it; don't hide it. Low-confidence detections produce suggestions, never automatic actions. The reason string is strong demo material - it shows contextual reasoning, not threshold-matching, which is the whole difference from an off-the-shelf energy monitor.

## V3.7 Where the context reasoning lives (design split)

- **Edge:** cheap statistical priors from labeled history (per-appliance distributions over time-of-day, day-of-week, temp band, co-occurrence). Fast first-pass confidence, no model call per event. Plus cycling decomposition (V3.3).
- **Nemotron (Nebius):** the harder contextual calls, the anomaly framing, and the advice - fed the full situation (event + phase + time + temp + what-else-is-on + usual patterns) in language, which a reasoning model handles well. Edge says "78% coffee by signature+timing"; cloud says "agreed, usual morning pattern, nothing to advise" or "signature says sauna but wrong hour - flagging, not acting."

Data note: context priors need HISTORY to learn from (weeks, ideally across seasons for temp-driven loads). 14-day raw window starts it; permanent hourly long-term statistics give the seasonal backdrop. The longer it records before the demo, the stronger the context layer - another reason to keep it recording now.

## V3.8 Demo strategy (locked)

Build and demo on Tier 1 (EV is the hero: ~9kW, 3-phase, "defer your charge, save X at current Nordpool price" - one legible real event, works today, zero fine-tuning). Present Tier 2 (sauna/oven/floor-heating by cycling + context) as the roadmap already in progress, with real collected data shown if available by then. Do NOT spend the five weeks chasing full slow-cycler accuracy - prove the point on the easy loads, be honest about the hard tier. The honest arc beats a suspicious perfect demo.

## V3.9 Starting signature library (from first capture - seed values, refine with more data)

```
EV_charger:   ~+8000W total, BALANCED 3-phase (~2700W each over baseline), non-cycling while charging. Tier 1.
kettle:       ~+1200W on phase B, flat, short duration, non-cycling. Tier 1.
toaster:      ~+880W on phase B, flat, short. Tier 1.
halogen1:     ~+260W on phase C, flat. Tier 1.
halogen2:     ~+260W on phase C, flat. Tier 1.
sauna:        TBD - ramp then thermostat cycling; capture a full hot run. Tier 2. Expect large amplitude, distinct period.
oven:         TBD - lower-amplitude cycling. Tier 2.
floor_heating: TBD - winter-only, slow cycle, temp-gated. Tier 2.
coffee_maker: TBD - pulsing pattern, small. Tier 2 / may be near baseline floor.
```
Phase map so far: B carries kettle+toaster; C carries halogens; EV spans all three. Confirm each appliance's phase as more data arrives - phase is a top discriminator.

---

# ADDENDUM (v3.2) - Deployment topology, HA communication, M5 console transport

Placement decided against the real network. This is where each piece runs and how they talk. Claude Code and OpenClaw both need this before starting.

## V3.2.1 Where things run

- **Development: directly on the user's PC.** Claude Code develops the Python here. No external dev server (helppox is NOT used for this). Offline signature analysis on exported CSVs (plotting, labeling tool, cycling characterization) also runs on the PC.
- **Runtime: Raspberry Pi 3, on the same LAN as HAOS.** The edge layer, signature matcher, confidence scoring, signature library, the Nebius reasoning client, and the M5 console host all run here. Old ARM core - keep the edge code lightweight (polling, step detection, simple matching, occasional API calls; no GPU, no heavy CPU - the reasoning is Nebius's job). The Pi already runs other projects; the user pauses them during Virta runs. Deploy = copy the finished Python from PC to Pi.
- **Cloud: Nebius Token Factory.** Nemotron reasoning, reached by one OUTBOUND HTTPS call from the Pi. Base URL `https://api.tokenfactory.nebius.com/v1/`, key in `NEBIUS_API_KEY`, standard OpenAI client. Only abstracted events leave the house ("these appliances are on, price curve, advise") - never raw power data. Good privacy line for the writeup.
- **OpenClaw (access to both networks): the hands inside HA.** All HA config changes and data exports go through OpenClaw, driven by prompts Claude Code writes and the user runs. Claude Code never touches HA directly.
- **Tailscale / Nabu Casa: remote access only, NOT in the runtime data path.** The Pi is already on the LAN with HA, so runtime uses local networking. Tailscale/Nabu Casa optional only if the user later wants to reach the Pi's console/dashboard from outside.

Physical AI framing bonus: "runs on a Raspberry Pi on my home network, reasoning served from the cloud, no raw power data leaves the house." Real edge hardware, on-prem, behind firewall+NAT; one thin outbound call for cognition.

## V3.2.2 How the Pi talks to Home Assistant

Local REST over the LAN. No Tailscale, no Nabu Casa, no cloud round-trip.
- Endpoint: `http://<haos-lan-ip>:8123/api/`
- Auth: a long-lived access token (HA UI -> Profile -> Long-lived access tokens). Store in env/config on the Pi; never commit.
- Read power sensors and Nordpool via `GET /api/states/<entity_id>`:
  - `sensor.3em_channel_a_power`, `_b`, `_c`, `sensor.3em_total_power`
  - `sensor.nordpool_kwh_fi_eur_3_10_0255` (state + `raw_today`/`raw_tomorrow` attributes)
  - inside/outside temperature entities (identify exact ids via OpenClaw - V3.5)
- Cadence: poll ~every 10-15s (matches the 3EM update rate). Simple REST polling is fine and easier than websocket; start there. Websocket (`/api/websocket`) is an optional upgrade for push instead of poll - not needed for v1.
- Config-driven: HA base URL + token in env, so the same code runs pointing at any HA instance.

Note: the runtime READS from HA over REST. Any HA config CHANGE or bulk history EXPORT still goes through OpenClaw (V3.2.1), not the runtime code.

## V3.2.3 M5 console transport - WiFi UDP (reuse micro_arcade protocol)

The M5 console talks over **WiFi UDP on port 20002, little-endian - NO cables/serial.** It reuses the existing micro_arcade wire protocol. The M5 stays a dumb display exactly as in micro_arcade; the Pi is the host and renders Virta's console view (load bars, reasoning verdict text, confidence) into FRAME packets instead of a game screen. Same transport and framing, Virta-specific payload.

Wire protocol (from micro_arcade, authoritative):
```
UDP port 20002, little-endian. Every packet starts with the 4-byte common header.

common header:  magic 0x4D ('M') u8 | type u8 | session u16

HELLO   0x01:   ver u8 | caps u8 | width u16 | height u16 | token_len u8 | token
INPUT   0x02:   seq u32 | buttons u16 | ax i16 | ay i16 | az i16 | last_frame_id u16
FRAME   0x82:   frame_id u16 | frag_idx u8 | frag_count u8 | flags u8 | length u16
  update:       rect_count u8 | game_state u8 | pad u16
  rect:         x u16 | y u16 | w u16 | h u16 | encoding u8 | length u16 | payload
```

For Virta:
- The Pi host sends FRAME packets rendering the console UI (Virta draws into rects: per-phase/total load bars, the Nemotron one-line verdict, confidence/reason). Fragmentation (frag_idx/frag_count) handles frames larger than one UDP datagram, as in micro_arcade.
- INPUT packets (buttons, accelerometer) come back from the M5 - use them for console interaction (e.g. acknowledge/confirm a suggested action, cycle views). Buttons are the "confirm" path for suggest-and-confirm actions (V3 / section 7).
- HELLO handshake establishes session, caps, and the M5's width/height so the host renders to the right resolution.
- Multi-device console: if the three M5 devices (SENSE/REASON/ACT) each run this protocol, the host addresses each by its UDP endpoint/session. Reuse the same renderer, send each device its own view. (Simplest v1: one M5 showing the combined console; split to three once one works.)

Reuse note: the micro_arcade host code already implements this protocol (encoding rects, fragmenting frames, the HELLO/INPUT loop). Lift that host-side transport wholesale; only the *content* being rendered changes from game to console. This is direct reuse, not new plumbing.

## V3.2.4 Deployment flow

1. Develop + test on PC (edge logic against exported CSVs and, when reachable, live HA over LAN).
2. Generate the HA long-lived token (OpenClaw-guided or in HA UI); put it + Nebius key in env on the Pi.
3. Copy the finished Python to the Pi3; pause the Pi's other projects.
4. Run on the Pi: it polls HA locally, calls Nebius outbound, drives the M5 console over UDP 20002.
5. First OpenClaw prompt needed up front: identify inside/outside temperature entity_ids and confirm the Pi's LAN IP can reach HA's REST API, and guide long-lived-token creation.

---

# ADDENDUM (v3.3) - Confirmed live values from HA (OpenClaw report)

All previously-open data values are now confirmed against the real HA instance. Use these; they replace any placeholder.

## Config block (real values)
```
NEBIUS_BASE_URL = https://api.tokenfactory.nebius.com/v1/
NEBIUS_MODEL_ID = <chosen Nemotron id from Token Factory catalog>
NEBIUS_API_KEY  = <stored>
HA_BASE_URL     = http://192.168.86.33:8123/api/
HA_TOKEN        = <long-lived token, user-created, stored>
SENSOR_PHASE_A  = sensor.3em_channel_a_power
SENSOR_PHASE_B  = sensor.3em_channel_b_power
SENSOR_PHASE_C  = sensor.3em_channel_c_power
SENSOR_TOTAL    = sensor.3em_total_power
SENSOR_NORDPOOL = sensor.nordpool_kwh_fi_eur_3_10_0255
SENSOR_TEMP_OUT = sensor.current_temperature      # NOTE: outdoor temp, despite the generic name
SENSOR_TEMP_IN  = sensor.temperature_average       # indoor average
```

## Confirmed facts
- **HA REST API** at `http://192.168.86.33:8123/api/`, enabled and responding. Pi on same LAN reaches it directly. Auth via long-lived token (user creates in HA -> Profile -> Security -> Long-Lived Access Tokens).
- **Nordpool: 96 slots, 15-minute resolution** (not 24 hourly). Unit **c/kWh** (cents). Keep all savings math in c/kWh; tell Nemotron "prices in c/kWh". Read array length at runtime (confirmed 96, but don't hardcode).
- **`raw_tomorrow` is empty until tomorrow's prices publish (~13:00 CET); `tomorrow_valid` is false until then.** Confirmed real daily state. Reasoning MUST handle short look-ahead gracefully (only today's remaining curve available before the publish).
- **`raw_today` / `raw_tomorrow`** entries have `{start, end, value}` shape - CONFIRMED. Each entry: `start` and `end` are ISO timestamps WITH local offset (`+03:00` EEST), `value` in c/kWh. Use `end` directly for slot duration - do NOT infer it from the gap. Parse to timezone-aware datetimes and keep the local offset; reasoning about clock times ('defer to 21:00') stays in local time, no UTC conversion needed. Example: `{"start":"2026-09-16T00:00:00+03:00","end":"2026-09-16T00:15:00+03:00","value":0.665}`. Night slots run well under 1 c/kWh; afternoon peaks far higher - real deferral savings, not rounding error.
- **Temperature entities confirmed:** outdoor `sensor.current_temperature`, indoor `sensor.temperature_average`. (Also exists `sensor.indoor_max_temp` - not primary.)
- **Live observation:** daytime EV charging shows ~11,800W total, balanced ~3800-4400 per phase. Phase C runs somewhat higher than A/B even under EV load - C likely carries more always-on base load. Useful prior for phase-based disambiguation.
