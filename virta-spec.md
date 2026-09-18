# VIRTA - Load Disaggregation & Price-Aware Energy Console

Working title: **Virta** (Finnish: electric current / flow). Yours to change.
Target: Nebius x NVIDIA Global AI Hackathon, **Physical AI Track**. Deadline: Oct 30, 2026, 10:00am PDT. Online, solo, Finland eligible. $50k pool; every track winner also gets an NVIDIA Jetson Orin Nano.

This is the single source of truth. All data values here are confirmed against the builder's real Home Assistant system unless explicitly marked TBD or TUNABLE. Build against these.

---

## 0. Positioning (read first - this is what Virta IS)

**NILM is a solved problem and others do the SENSING better** - HA NILM projects and commercial disaggregators have more accurate sensors, more data points, load-type libraries, frequency analysis. Virta does NOT compete on sensing and concedes this openly.

**Virta is a reasoning layer that happens to sit on top of NILM.** The thesis: cheap sensor, expensive brain. A ~50€ Shelly 3EM plus a reasoning model at the edge extracts UNDERSTANDING from a crude signal that better sensors with no brain never reach - because the value was never in the resolution, it's in what you CONCLUDE from it. Disaggregation is table stakes (do it so there's something to reason about); the product is the reasoning: disambiguating the ambiguous, discovering the unknown, finding behaviors and inefficiencies, and advising with judgment.

**Same architecture scales house -> business.** You can't instrument a legacy factory to every socket, but you can put a crude meter on the main and a reasoning model behind it and get actionable insight. This is the builder's OT/energy day-job pitch. "For humans" scales to "for businesses."

**Private by design (DLP by design).** No raw power data leaves the premises - only abstracted events go to the cloud for reasoning. A real enterprise selling point, not an afterthought.

**Do NOT publish this as "yet another HA NILM."** Lead every piece of copy, and the demo, with the REASONING, not the detection. The existing HA NILM projects are Virta's foundation layer, not its competition. The demo must contain at least one undeniable moment of UNDERSTANDING a detector could never produce (the discovery loop + one efficiency nudge + one dry anomaly observation) - or the vision is asserted, not demonstrated. If the video's climax is "look, a nice gauge," the entry has failed; if it's "watch it work out an appliance it was never told about, then suggest I'd save 5€ with a microwave," it has won.

---

## 1. What this is

A power-monitoring console that reads the whole-house 3-phase draw, detects which major appliances are running from their load behavior, and REASONS over that plus the live Nordpool electricity price - to advise on deferring loads AND, in a deeper nightly pass, to find behaviors, inefficiencies, and anomalies in how the home uses power. Live example: "Sauna and an oven are on, price is high for the next 90 min then drops - defer the sauna, ~2.4 c/kWh cheaper, no comfort lost." Nightly example: "You've boiled the kettle 47 times this fortnight; for your typical volume a microwave would use less energy - about 5€/month at current prices."

The detection (NILM) is the sensing layer. The reasoning (Nemotron) is the point (section 0).

The console displays on the Pi's attached HDMI screen (primary, section 9). An M5 WiFi/UDP hardware panel is an optional later upgrade sharing the same renderer (section 10). The edge does signal work locally; Nemotron on Nebius does the reasoning.

### Why it fits the Physical AI track
- Embodied/edge agent that senses (real mains current) and acts (advice, optional HA control) in the real world.
- Edge genuinely matters: you cannot stream high-frequency 3-phase current to a datacenter, and it's your own home's power data - so the edge extracts events locally and sends only a compact abstraction upstream. Real edge computing, private by design.
- The AI role is the PRODUCT, not garnish (section 0): a rules engine could compare a price to a threshold; only a reasoning model does contextual disambiguation, unknown-load discovery, behavioral/efficiency analysis, and judgment-based advice. If Nemotron's only job were "print status," it would be decoration - it is not (section 6A).
- Impact scales up (house PoC -> industrial energy management); Finland's volatile spot pricing makes the house version genuinely useful.
- Physical-AI submissions need a 1-min clip of hardware operating: turn on the EV/kettle on camera, the console detects it, advice appears citing the real Nordpool price - and the demo also shows a nightly insight (the differentiators).

---

## 2. If you're new to Nebius/Nemotron

Nebius Token Factory = an OpenAI-compatible endpoint. Same `openai` Python client you'd use for GPT; change base URL, key, model id. No separate SDK.

- Base URL: `https://api.tokenfactory.nebius.com/v1/`
- Key: `NEBIUS_API_KEY` env var (account + credits already claimed).
- Model id: exact string from the Token Factory catalog (an `nvidia/nemotron-*` id). Test one call in the Playground before writing loop code.

Two hard gotchas, both handled from the first call:
- **Nemotron models are REASONING models: the answer may be in `reasoning_content`, and `choices[0].message.content` may be EMPTY.** Read `reasoning_content` when content is empty:
  `out = resp.choices[0].message.content or getattr(resp.choices[0].message, "reasoning_content", "")`
- **No function/tool calling via the wrapper.** Get structured output by asking for JSON in the prompt and parsing it - never tools.

Requirement: at least one NVIDIA open-source model must be used - Nemotron satisfies it.

---

## 3. The real data - inputs, exactly as they exist

### 3.1 Power input: Shelly 3EM (native Shelly HA integration)
Confirmed live entities, recording:
- `sensor.3em_channel_a_power` - phase A active power (W)
- `sensor.3em_channel_b_power` - phase B active power (W)
- `sensor.3em_channel_c_power` - phase C active power (W)
- `sensor.3em_total_power` - summed total power (W), a user-created template sensor
- `sensor.3em_channel_a_energy` / `_b` / `_c` - cumulative energy (kWh), ever-increasing (fallback only)

Recorder: the power sensors were excluded from the HA recorder; the exclusions have been removed and power is now recorded. 14 days of raw history (`purge_keep_days: 14`); permanent hourly long-term statistics (sensors are `state_class: measurement`).

**Sample rate: ~10-15s steady, faster (sometimes sub-second) on change.** The Shelly native CoIoT integration pushes an update the moment the value changes enough, then settles to ~15s when stable. Good for step detection and cycling; NOT for sub-second inrush fingerprinting. Detector design is settled: step + steady-state + cycling, no transient-shape fingerprinting.

Power-from-energy fallback (only if a window has energy but not power): `W = (E[i]-E[i-1]) / (t[i]-t[i-1] seconds) * 3600 * 1000`, guarding `0 < Δt < 600` and `ΔE >= 0`. Prefer the real `_power` sensors.

### 3.2 Price input: Nordpool (HACS `custom-components/nordpool`)
Sensor: `sensor.nordpool_kwh_fi_eur_3_10_0255` (FI region, name decodes as 3 decimals / 10% VAT included / 0.0255 fixed adder - so the value is an all-in, VAT-inclusive price, the honest number for savings math).
- **State** = current price. **Unit: c/kWh** (cents). Keep all savings math in c/kWh; tell Nemotron "prices in c/kWh".
- **Curve is in the attributes** (read via `state_attr`, not separate entities):
  - `raw_today` - array of `{start, end, value}`. `start`/`end` are ISO timestamps WITH local offset (`+03:00` EEST); `value` in c/kWh. Use `end` for slot duration - don't infer it from the gap. Example entry: `{"start":"2026-09-16T00:00:00+03:00","end":"2026-09-16T00:15:00+03:00","value":0.665}`.
  - `raw_tomorrow` - same shape; EMPTY until tomorrow's prices publish (~13:00 CET); `tomorrow_valid` is false until then. The reasoning MUST handle the short look-ahead gracefully (only today's remaining curve before the publish).
- **96 slots, 15-minute resolution.** Read the array length at runtime (don't hardcode), but it is 96, not 24 hourly.
- Parse to timezone-aware datetimes and keep the local offset; reasoning about clock times ("defer to 21:00") stays in local time, no UTC conversion.
- **Negative prices occur** (Finland, wind oversupply). Treat negative price as "run loads now / you're paid to consume." Do not assume price > 0. Night slots run well under 1 c/kWh, afternoon peaks far higher - real deferral savings, not rounding error.
- A downstream helper `sensor.cheapest_hours_energy_tomorrow` exists (built on Nordpool) - a hint only; the reasoning consumes `raw_today`/`raw_tomorrow` directly.

### 3.3 Context inputs (pull from HA like the price/power)
- Outdoor temperature: `sensor.current_temperature` (NOTE: outdoor, despite the generic name).
- Indoor temperature: `sensor.temperature_average`.
- Darkness: HA `sun.sun` (`below_horizon`) gives a free `is_dark` flag (used by the profile system, section 7).
- Time/day: local clock (prices already local time).

Use the real driver, not a proxy: floor heating correlates with cold (outdoor temp), not the calendar; a cold snap should raise floor-heating probability before the date would.

### 3.4 Usage patterns / preferences (builder-supplied text)
A short profile fed into reasoning, e.g. "sauna usually 20:00-22:00; EV must be charged by 07:00; ovens are dinner-time; floor heating is comfort, deferrable within reason." Hand-written to start.

### 3.5 Offline signature-building data (historical CSV exports)
Live data drives the running system; CSV exports are only for building the initial signature library offline. States-history CSVs are long format `entity_id,last_updated,state`. Parsing rules to bake into code:
- Values may be `unavailable`/`unknown`/empty - SKIP those rows, don't `float()` them (crashes naive code).
- Timestamps are irregular (~10-15s, sometimes sub-second) - use ACTUAL elapsed time, never assume a fixed interval.
- Drop `_energy_cost` entities entirely - dummy data (running total off a fixed rate), superseded by the live Nordpool price. Purge from any working copy so they don't confuse the pipeline.
- The light-control Shellys (`switch_a..u`) are lights, not interesting loads - ignore them.

---

## 4. What the first real capture showed (signatures + the load-control finding)

A deliberate-run capture (Sept 16 morning) established the real signatures:

- **EV charge:** total steps up by ~8-11 kW, BALANCED across all three phases (each +~3000-3900W); captured runs reached ~9-11.8 kW active vs a ~950-1000W baseline. The only balanced three-phase load in the house. Trivially detectable, ~100% confidence on power alone. This is the demo hero.
- **Kettle:** ~+1200W on phase B, flat, short, non-cycling. Easy.
- **Toaster:** ~+880W on phase B, flat, short. Easy.
- **Halogen1 / Halogen2:** ~+260W each on phase C, flat, single-phase. Detectable.
- **Sauna, oven, coffee maker:** effectively INVISIBLE at switch-on in this capture - they don't pull full power immediately (sauna ramps then thermostat-cycles; oven cycles; coffee pulses). They need longer-window / cycling detection + context, not instant-step.

Two structural findings this produced:
- **Phase assignment is a strong, free discriminator.** Loads spread across phases (kettle/toaster on B, halogens on C, EV all three). "Which phase moved and by how much" does half the identification. Phase C tends to run higher than A/B even under EV load - C likely carries more always-on base load.
- **The high baseline (~1kW) is real and explained:** always-on servers, cryptominers, etc. The detection floor sits above a kilowatt, so sub-~200W loads are genuinely hard to separate from baseline drift and are OUT OF SCOPE by design. State this in the demo ("detection floor above ~1kW of always-on compute; sub-200W loads deliberately out of scope") - it turns a limit into a credibility signal.

**EV load-control finding:** the Eve charger has dynamic load control - it throttles its own draw when household consumption rises, to protect the main fuses. So while charging, another appliance switching on can be MASKED (the charger backs off by roughly the same amount, total stays flat). This breaks naive step-detection during active charging. Mitigations: (a) the EV mostly charges at NIGHT, so this rarely collides with daytime detection; (b) if the Eve/Plugit exposes power or load-control state via an API, read it directly rather than inferring (not yet checked - parked, non-blocking); (c) treat any sustained ~9-12 kW balanced draw as "EV active, load-control possible, secondary-load confidence reduced" and let the reasoning layer narrate around it.

---

## 5. Detection: two tiers

**Tier 1 - instant-step (DEMO-READY NOW).** Loud, clean loads identified by a single step's magnitude + which phase moved: EV (3-phase balanced ~9-11kW), kettle (~1200W/B), toaster (~880W/B), halogens (~260W/C). Build the demo on these. The EV alone is a killer demo - defer-to-cheap-hours advice on a real ~10kW event, zero fine-tuning.

**Tier 2 - pattern-over-time (ROADMAP, data collection underway).** Slow/cycling loads identified by their thermostat rhythm over a window, not their switch-on: sauna (bang-bang thermostat, characteristic period + amplitude once hot), oven (lower-amplitude cycling), floor heating (winter-only, slow cycle, temperature-gated). These need a monitoring window and cycling characterization. Frame as the harder tier the system is learning - the honest NILM arc, more credible than a perfect demo, and recognizing a sauna by its thermostat heartbeat is the impressive part once it lands.

**Cycling is both a fingerprint AND the overlap-separator.** In a single instantaneous total, two concurrent loads blur into one number and can't be split by magnitude. But two cyclers at different periods (sauna's rhythm vs floor heating's ~20-min rhythm) appear as superimposed square waves at distinct frequencies - separable in the time domain even when inseparable by magnitude. So characterize each cycling load by period + amplitude + phase, and decompose a busy window by the distinct rhythms present. This is the core technique for concurrency and why longer windows identify loads instant-step misses.

The reasoning layer is tier-agnostic: it receives "appliance + confidence + supporting evidence" and reasons over price identically whether Tier 1 or Tier 2 detected it. Tier 2 can improve after the demo without touching the reasoning half.

**Demo strategy (locked):** build and demo on Tier 1 (EV = hero). Present Tier 2 as the roadmap already in progress, with real collected data shown if available. Do NOT spend the weeks chasing full slow-cycler accuracy - prove the point on the easy loads, be honest about the hard tier.

### Starting signature library (seed values; refine with more data)
```
EV_charger:    ~+8-11kW total, BALANCED 3-phase (~3000-3900W each over baseline), non-cycling while charging. Tier 1.
kettle:        ~+1200W on phase B, flat, short, non-cycling. Tier 1.
toaster:       ~+880W on phase B, flat, short. Tier 1.
halogen1:      ~+260W on phase C, flat. Tier 1.
halogen2:      ~+260W on phase C, flat. Tier 1.
sauna:         TBD - ramp then thermostat cycling; capture a full hot run. Tier 2. Large amplitude, distinct period expected.
oven:          TBD - lower-amplitude cycling. Tier 2.
floor_heating: TBD - winter-only, slow cycle, temp-gated. Tier 2.
coffee_maker:  TBD - pulsing, small; may be near baseline floor. Tier 2.
```
Phase map so far: B carries kettle+toaster; C carries halogens; EV spans all three. Confirm each appliance's phase as more data arrives - phase is a top discriminator.

### 5.1 Baseline calibration (calibrate from quiet, not from the clock)
The house has a large static floor (~1kW: always-on servers, miners, fridge, standby). Detection works against this floor, so it must be known and current.
- **Calibrate from QUIET, not a fixed hour.** Overnight is usually quiet, but the EV often charges at night - so do NOT calibrate from a fixed 01-05 window. Instead find the longest STABLE, LOW stretch (total below a sane ceiling, low variance, sustained) and take the baseline from that. Quiet is the signal, not the hour.
- **Per-phase baselines.** Each phase carries a different static load (C runs higher than A/B). Maintain a separate floor per phase.
- **Rolling, not a hard nightly reset.** Take each night's quiet-stretch median and roll it into a moving per-phase baseline - mostly stable, adapts over days, immune to one weird night. A single bad night must not throw the reference.
- The overnight quiet-stretch recalibration also clears accumulated stacking drift (5.2).

### 5.2 Stacking detection model (the core engine)
The floor is NOT static during the day - it is the static baseline PLUS everything currently known to be on. Detect each load as a step against the CURRENT accounted total, not against the cold baseline.

Maintain, per phase:
```
accounted_total[phase] = static_baseline[phase]
                       + sum(active known loads on phase)
                       + sum(active unknown loads on phase)
```
Every deviation of the ACTUAL phase power from `accounted_total[phase]` is an event:
- **Step UP** = a new load switched on. Measure the delta against the current accounted total (not baseline). Match it to a signature -> named load; or, if clearly above floor but matching nothing, mark it UNKNOWN. Either way, ADD its measured magnitude to the accounted total. The floor for the NEXT event is now higher.
- **Step DOWN** = a load ended. Match the drop to an active load's known draw, remove it from the stack; the floor drops back.

Worked example: baseline 1kW + an unknown 1kW already running = accounted 2kW. Total jumps to 2800W on phase B -> that's a +800W step against the 2kW floor -> matches the toaster. You did NOT need to identify the unknown 1kW to detect the toaster on top of it. **Unknowns participate in the stack by their magnitude even without an identity** - that is what makes this robust; you are never blocked by the thing you can't name.

This turns "many things on at once" into a sequence of clean single-step events. It handles SEQUENTIAL concurrency (loads switching one at a time), which is the common case.

Supporting rules:
- **Per-phase stacking:** a step stacks on the phase it landed on (toaster's 800W on B stacks on B's floor, not the aggregate). Phase narrows which stack an event belongs to - another reason phase is a top discriminator.
- **Reconciliation / drift:** over a long session, accounted vs actual drift apart (loads changing draw slightly, missed events, cycling wobble). Periodically compare accounted-total to actual-total; the residual is "unexplained load." Small residual: ignore. Large persistent residual: you missed an event - surface it as UNKNOWN or flag the model out of sync. The overnight quiet recalibration (5.1) is the daily hard reset that clears drift.
- **Load-control interaction:** while the Eve charger throttles (section 4), it silently changes its own draw, so accounted won't match actual - this shows up as residual drift. Treat "EV active" as a known-but-variable load (widen its tolerance while charging) so the stack doesn't go haywire. Night charging keeps the daytime stack clean.
- **Simultaneous switches are the honest limit:** two loads switching within the same ~15s sample look like one combined step and can't be split by magnitude alone. Don't pretend the stack solves this - report UNKNOWN +Xw rather than guessing; cycling decomposition (section 5) is the eventual help for the cycling case.

### 5.3 Three detection states (surface all three honestly)
Every bit of load is in one of three states, all shown:
- **MATCHED** - above floor, matches a signature -> named load + confidence + reason string.
- **UNKNOWN** - clearly above floor but matches no signature (confidence below match threshold but clearly above baseline). Shown honestly as e.g. "UNKNOWN +1200W B". Not hidden, not forced into a wrong bucket. Unknown loads are also the labeling pipeline - they are the candidates you later teach the system.
- **BASELINE** - the static floor itself (5.1).
An honest "unknown" beats a confident wrong guess and reads as rigor to judges.

---

## 6. Reasoning: Nemotron on Nebius

Client shape:
```python
from openai import OpenAI
client = OpenAI(base_url="https://api.tokenfactory.nebius.com/v1/",
                api_key=os.environ["NEBIUS_API_KEY"])
resp = client.chat.completions.create(
    model="<nvidia/nemotron-* id from catalog>",
    messages=[{"role":"system","content": SYS}, {"role":"user","content": payload}],
)
out = resp.choices[0].message.content or getattr(resp.choices[0].message, "reasoning_content", "")
```

System prompt (dry): "You are a household energy advisor. Given the appliances currently running, the Nordpool electricity price curve (c/kWh, VAT included; values can be negative meaning you are paid to consume), the outdoor/indoor temperature, whether it is dark, and the user's usage patterns, decide whether any running or imminent load should be deferred to save money without hurting comfort or hard deadlines (e.g. EV charged by 07:00). Prices are 15-minute slots in local time - read the timestamps. Be specific and brief; never nag about trivial savings; if nothing is worth changing, say so in one line. Output one line for the console display, then a JSON block {action: none|suggest_defer, appliance, defer_until, est_saving_c_kwh, reason}."

Feed the payload as text/JSON: current active appliances (+confidence), the `raw_today` (+`raw_tomorrow` if valid) curve, temps, is_dark, the usage profile, and current time. Parse the JSON for the console/action; show the one-liner as the verdict. No tool calling - JSON via prompt, parsed in code.

---

## 6A. Three reasoning tiers + voice (this is the differentiation)

Virta's magic is layered reasoning at three cost/depth tiers. The trick that makes it "appear to think deeply in real time" without per-call cost: the EXPENSIVE cognition is amortised into the quiet nightly hours and its conclusions are CACHED and served cheaply all day. It thought deeply last night; the console is just remembering.

### Tier 1 - LIVE (cheap, reactive, capped)
Event-driven, tightly capped (section 8). Quick reasoning on a situation change -> clean instrument advice ("DEFER SAUNA -> 21:15, SAVE 2.4c"). Necessary, not the magic. This is section 6's advisor call.

### Tier 2 - NIGHTLY (deep, cheap-because-once)
At night nothing competes and latency doesn't matter, so afford ONE big deep call over the day's/fortnight's EVENT LOG (compact structured history, not raw stream - section 5A). Output is UNDERSTANDING that gets cached and surfaced cheaply all day. The nightly job list is bigger than "discover loads" (section 5A) - it is "understand the household":
- **New load discovery** (section 5A) - cluster unknowns, propose labels via the worklist file.
- **Behavioral patterns** - "you sauna every night except Sundays"; "baseline crept up 200W three weeks ago and never came back - something new is always-on"; "the dishwasher and the EV both want the expensive evening slot." Observations about how the house is USED - a level above what's on. No detector does this (no memory, no notion of 'usual').
- **Efficiency findings** - analysis over behavior + appliance physics (LLM world knowledge) + real price. E.g. kettle-vs-microwave: knows your habit, knows relative efficiency, does the arithmetic against Nordpool. A detector cannot produce this sentence.
- **Anomalies / life-events** - "you haven't charged the car in three weeks - did you sell it, or is something wrong?" Reasoning over time; whimsy and utility are the same capability (a missing routine load or a baseline jump can mean a fault, a leak, a forgotten device).
- **Cost->carbon translation (honest, not preachy)** - where true in the Nordic grid, cheap hours are often high-wind, so "deferring to the cheap slot also shifts you to lower-carbon wind" is a FACTUAL observation the LLM makes, not a slogan. Green is a consequence Virta points out, not its banner. (Aside: the dashboard is literally green - note the joke once, move on.)

### Tier 3 - SURFACE (free)
The console serves Tier-2 cached insights all day + Tier-1 live advice. Displaying already-computed conclusions costs nothing. This is why it appears to reason continuously while spending almost nothing.

### Two voices (resolve the trust/character tension)
Personality is the wow factor but it can undercut trust - you must BELIEVE the advice. So split registers:
- **Live advice = clean, terse, instrument-like, trustworthy.** "DEFER SAUNA -> 21:15, SAVE 2.4c." No jokes here.
- **Nightly commentary = dry, observant, a little HAL.** "You've boiled that kettle 47 times this fortnight; a microwave would've saved you a coffee's worth of money." "I see you didn't charge the car for three weeks. Did you sell it?"
Instrument by default; character in the reflective layer. They don't fight.

### The one guardrail: watch the METER, not the PERSON
The HAL vibe stays charming only if it comments on ENERGY and APPLIANCES, drily - never on the human's life. "You didn't charge the car in three weeks" (about a device) is fine. "You're home more than usual" (about you) is creepy - never do it. The wit comes from how precisely it noticed the DEVICE pattern, not from surveilling the person. On the meter, not the person. This is also the honest privacy posture (DLP by design, section 0).

### Demo requirement
The demo MUST show at least one Tier-2 insight (the discovery loop) + one efficiency nudge + one dry anomaly line - not just live detection - or the differentiation is asserted, not shown (section 0). That trio on camera proves the whole thesis; everything else is credible future work.

---

## 7. Device-profile system (identification factors)

Identification = P(appliance | power event AND context), not power alone. Do NOT build a learned Bayesian engine - with ~2 weeks of sparse history and one example of most appliances, weight-fitting is a research project that eats the timeline. Instead: profiles are structured, human-editable records; the edge does simple deterministic first-pass scoring; Nemotron does the fuzzy multi-factor and correlation reasoning in language.

### Profile schema (human-editable JSON)
```json
{
  "id": "kitchen_halogen",
  "display_name": "Big-bang halogen (kitchen)",
  "tier": 1,
  "signature": {
    "type": "step",
    "phase": "C",
    "step_w": 260, "step_tolerance_w": 80,
    "cycling": null
  },
  "context_factors": [
    {"factor": "time_of_day", "typical": ["morning","evening"], "strength": "medium"},
    {"factor": "correlates_with", "devices": ["coffee_maker","kettle","toaster"],
     "note": "kitchen cluster", "strength": "medium", "stronger_when": "dark"},
    {"factor": "season", "note": "year-round", "strength": "weak"}
  ],
  "notes": "In kitchen. When on, kitchen appliances more likely - especially after dark."
}
```
Factors are statements, not tuned coefficients. `strength` is coarse (weak/medium/strong). Conditional modifiers like `stronger_when: dark` are plain flags the reasoning layer interprets.

### The split
- **Edge, deterministic first pass:** score a detected event against each profile's hard signature (phase match + magnitude within tolerance) and cheap context (time-of-day bucket, is_dark). First-pass candidate + confidence, no model call.
- **Nemotron, fuzzy reasoning:** given candidate profiles + the current situation (what else is on, time, temp, dark/light, unexplained residual loads), do the soft inference - "kitchen halogen is on and it's dark, so that unexplained ~1200W on phase B is probably the kettle." Correlation-stronger-when-dark is a sentence it reads, not a coefficient.

### Authoring
- The labeling tool outputs a DRAFT profile from a labeled capture (fills signature; stubs context_factors to edit).
- Profiles are plain JSON files - hand-edit or a simple form. No training pipeline. Both the edge scorer and the Nemotron prompt read the same files.

### Confidence scoring (explicit, surfaced)
Every detection carries an explicit confidence + a short reason string ("sauna 92% - power+phase match, matches evening pattern, pump also active" / "looks like sauna but 03:00 on a workday - low confidence, flagged unusual"). Surface it. Low-confidence detections produce suggestions, never automatic actions. Context can DEMOTE a power match (a sauna-shaped signature at an impossible hour lowers confidence and flags "unusual" rather than reporting "sauna"), which also turns genuinely unexpected events into a surfaced feature instead of a misclassification.

### Discipline
Start with signature + 2-3 context factors (time, phase, one correlation, is_dark). Get it working end to end on Tier-1 devices. Add darkness-conditional correlation, inter-device clusters, seasonal gating as data justifies. A profile system you can extend beats a complete one you can't finish.

---

## 8. Cadence & cost control (how often to call Nebius)

Decouple the two clocks: read HA fast and free, call the cloud rarely and only on meaningful change. Naive "call every 15s tick" = ~5,760 calls/day of identical advice. Smart triggering cuts that ~99% and is BETTER behavior (advice changes only when the situation changes).

- **Local loop (~every 10-15s, FREE):** poll HA, run edge detection, update the console. No cloud call - just HTTP + arithmetic.
- **Cloud loop (event-driven, PAID):** call Nemotron only when the situation changed in a way that could change the advice.

**Situation hash (single trigger mechanism):**
```
situation = ( frozenset(active_appliance_ids), price_tier, is_dark )
```
`price_tier` is a COARSE bucket (cheap/normal/expensive, thresholds in config), NOT the raw c/kWh - so tiny price wiggles don't trigger calls. Call only when the hash changes. Cache the last situation + last verdict; unchanged hash -> do nothing, cached verdict holds.

Special triggers beyond a hash change:
- New price data published (~13:00 CET, `tomorrow_valid` flips true): one call - horizon grew.
- Optional heartbeat: at most one call/hour even if nothing changed, to avoid a stale verdict. Optional.

**Debounce:** appliance switch-ons produce a flurry (settling, thermostat, load-control reactions). On a detected change, wait a short settle window (TUNABLE, ~5-10s), then evaluate the hash and call once for the settled state. One real event = one call.

**Verdict caching:** the console updates load bars/trace every ~15s from local data (free); the advice line refreshes only when a cloud call fires, showing the last cached verdict between calls. Fast local UI, slow cloud advice.

**One call reasons over the whole horizon:** send the full relevant price curve in that one call; the model returns "defer until 21:15" in one shot. Don't re-ask until the situation changes.

**Hard-cap backstop (enforced in code, checked before every call):** `MAX_CLOUD_CALLS_PER_HOUR` (~20), `MAX_CLOUD_CALLS_PER_DAY` (~150), optional `DAILY_SPEND_CEILING`. On cap hit: STOP calling, keep serving the cached verdict, log it. Fail closed - a stuck sensor flapping every 15s must not drain the budget. Caps in config, echoed on startup.

**Cheap model, tight prompt:** cheapest Nemotron that reasons adequately; prompt = current loads + price curve + profile + temps + is_dark + time, nothing else. Expected normal day ~50 calls (20-40 appliance events + a few price-tier crossings + one publish) - far under any sane ceiling.

---

## 9. Console: HDMI display (PRIMARY)

The Pi3 has a small HDMI display attached - this is the primary console and the shippable demo surface. A fullscreen app on it is a complete, filmable Physical AI console with zero network protocol. The M5 UDP panel (section 10) is an optional upgrade, NOT a dependency.

Build the console as a **renderer that draws Virta's current state to a surface** ("what to draw", framework-agnostic). The HDMI path draws it to the Pi's fullscreen framebuffer (v1, ships first); the M5 path later encodes the SAME surface into UDP FRAME packets. Write the draw logic once; only the transport differs.

### Aesthetic: fixed single-screen scientific instrument
Retro-scientific, vector-ish, restrained. Muted palette, phosphor-green terminal glow on near-black, coarse-but-clean linework - a workstation in a dim room. Instrument, not dashboard; measuring, not decorating.

Governing principle: **motion = information only.** The only things that move are things the data actually does. The trace scrolls because power changes; a number ticks because a reading arrived; the meter spins faster because consumption rose. No idle animations, no decorative pulsing. This restraint is what reads as scientific rather than screensaver.

Fixed single screen - everything visible at once, no modes, no menus, no interaction. It should look like something that has been running for hours and will keep running.

**Panelized layout (design for a printed bezel from day one).** Build the screen as 2-3 self-contained panels in a grid, each reading as its own instrument, with quiet margins a printed frame/bezel can cover to make them look like separate physical screens. This costs nothing if designed in from the start and is painful to retrofit. Natural three-block split: (1) DYNAMIC - live trace + analog meter; (2) DATA - numeric readouts + detection panel; (3) ADVICE - verdict line + price sparkline. Give each panel hard internal boundaries and margin so a bezel lands cleanly. Bonus: because each panel is already a discrete render unit, sending one panel to an M5 later (section 10) is trivial.

Elements (laid out like an instrument, trace dominant):
- **Live power trace (hero):** total draw, last several minutes, scrolling right-to-left. Thin vector line, phosphor green on near-black, faint grid. Where you SEE an appliance switch on as a real-time step.
- **Three-phase readout:** A/B/C as thin traces or a small stacked readout in slightly different shades, so balance is visible (EV = all three rise; kettle = one jumps).
- **Analog consumption meter (subtle motion):** a rotating disc with a single radial reference mark (Ferraris power-meter idiom), rotating at a rate PROPORTIONAL TO CURRENT TOTAL WATTS - idle-slow at baseline, visibly faster under load, racing when EV+sauna are on. The speed IS the reading: instantaneous "how hard is the house pulling" at a glance, which numbers don't convey viscerally. Thin vector circle + one mark, one rotating line per frame - cheap on the Pi3. Smooth, no blur, no flourish; speed maps to watts continuously (not a fixed spin). Rotation shows FLOW; the numbers show LEVEL - both, deliberately.
- **Numeric readouts (monospace):** current total W, per-phase W, current price c/kWh, price tier as a word. Numbers tick as readings arrive.
- **Detection panel:** what's believed ON + confidence, terse uppercase ("EV CHARGING 98%", "KETTLE 71%"), plus honest UNKNOWN entries ("UNKNOWN +1200W B", section 5.3). Low-confidence shown dimmer or with "?" so uncertainty is visible. Prefix each with a single-letter LOAD GLYPH in the terminal idiom, not pictographic icons: E=EV, K=kettle, S=sauna, O=oven, H=halogen, C=coffee, F=floor heating, U/?=unknown. Letters read like instrument labels and fit the aesthetic; a detailed icon set would fight it.
- **Verdict line (live advice, clean register):** the Nemotron Tier-1 advice, one line, terminal voice - clipped, technical, no pleasantries ("PRICE HIGH. DEFER SAUNA -> 21:15. EST 2.4 c/kWh CHEAPER."). Trustworthy instrument voice; no jokes here (section 6A).
- **Commentary line (nightly insight, dry register):** surfaces the cached Tier-2 observation of the day - dry, HAL-ish, on the meter not the person ("47 KETTLE BOILS THIS FORTNIGHT. A MICROWAVE SAVES ~5€/MO."). This is the differentiator made visible. Rotate through the night's findings; one at a time, unobtrusive.
- **Price sparkline (optional):** the day's 96 slots as a tiny bar strip with "now" marked. Updates each slot.

Palette discipline (where it lives or dies): near-black background; one or two greens for live data; dim grey-green for grids/labels; ONE warm accent (amber or muted red) reserved ONLY for alerts / expensive-price state. Colour as signal, not decoration.

Rendering: **pygame**, fullscreen kiosk on the Pi3 - draws line strips, text, and the rotating meter fast, no browser overhead; vector-ish lines scale to the HDMI resolution and are cheaper than pixel-art. Monospace TTF/bitmap font. Optional VERY subtle scanline/bloom if leaning into the retro reference, but do NOT overdo CRT effects (heavy scanlines tip it into pastiche). Redraw from local data every ~15s (or interpolate the trace/meter smoothly between readings); advice line refreshes only on a cloud call (section 8).

**Safety:** any real load control is suggest-and-confirm, never autonomous. Advice by default; action only on explicit OK. Never let it silently switch the EV charger.

---

## 10. Console: M5 hardware panel (OPTIONAL later upgrade)

Optional upgrade after the HDMI console works. Reuses the existing micro_arcade wire protocol; the M5 stays a dumb display, the Pi is the host and renders Virta's console view into FRAME packets instead of a game screen. Same shared renderer as section 9 - only the transport differs.

Transport: **WiFi UDP on port 20002, little-endian - NO cables/serial.**
```
common header:  magic 0x4D ('M') u8 | type u8 | session u16
HELLO   0x01:   ver u8 | caps u8 | width u16 | height u16 | token_len u8 | token
INPUT   0x02:   seq u32 | buttons u16 | ax i16 | ay i16 | az i16 | last_frame_id u16
FRAME   0x82:   frame_id u16 | frag_idx u8 | frag_count u8 | flags u8 | length u16
  update:       rect_count u8 | game_state u8 | pad u16
  rect:         x u16 | y u16 | w u16 | h u16 | encoding u8 | length u16 | payload
```
- The Pi host sends FRAME packets rendering the console UI into rects (load bars, verdict, confidence). Fragmentation handles frames larger than one datagram.
- INPUT packets (buttons, accelerometer) come back - use buttons as the "confirm" path for suggest-and-confirm actions.
- HELLO handshake establishes session, caps, and the M5's width/height.
- **Minimal M5 role (recommended if pursued):** the M5 shows just the analog meter (the Ferraris disc) + total watts - the one element legible as pure motion from across a room, needing no text parsing. HDMI is the workstation; the M5 is the glanceable ambient gauge ("how hard is the house pulling right now"). This is a tiny payload - a rotation value + a number, not a full rendered frame - far easier than streaming the whole console.
- Multi-device (if pursued further): each M5 (e.g. SENSE/REASON/ACT roles, or one panel each from the panelized layout in section 9) runs the protocol; the host addresses each by UDP endpoint and sends its own view. Simplest first: one M5 showing the meter + total.
- The micro_arcade host code already implements this protocol (encoding rects, fragmenting frames, HELLO/INPUT loop) - lift the host-side transport wholesale; only the rendered content changes.

---

## 11. Deployment topology & how the Pi talks to HA

- **Development: on the builder's PC.** Claude Code develops the Python here. Offline signature analysis on CSV exports (plotting, labeling tool, cycling characterization) also on the PC. (No external dev server.)
- **Runtime: Raspberry Pi 3, same LAN as HAOS.** Runs the edge layer, signature matcher, confidence scoring, signature library, the Nebius reasoning client, and the HDMI console. Old ARM core - keep the edge code lightweight (polling, step detection, simple matching, occasional API calls; no GPU needed - reasoning is Nebius's job). The Pi runs other projects; pause them during Virta runs. Deploy = copy the finished Python from PC to Pi.
- **Cloud: Nebius Token Factory.** Nemotron reasoning, one OUTBOUND HTTPS call from the Pi. Only abstracted events leave the house ("these appliances are on, price curve, advise") - never raw power data. Good privacy line for the writeup.
- **OpenClaw (access to both networks): the hands inside HA.** All HA config changes and data exports go through OpenClaw, driven by prompts Claude Code writes and the builder runs. Claude Code never touches HA directly.
- **Tailscale / Nabu Casa: remote access only, NOT in the runtime data path.** The Pi is already on the LAN with HA.

Physical-AI framing: "runs on a Raspberry Pi on my home network, reasoning served from the cloud, no raw power data leaves the house." Real edge hardware, on-prem, behind firewall+NAT; one thin outbound call for cognition.

### HA communication (confirmed)
- Endpoint: `http://192.168.86.33:8123/api/` (REST enabled and responding on the LAN).
- Auth: a long-lived access token (HA UI -> Profile -> Security -> Long-Lived Access Tokens). Store in env on the Pi; never commit.
- Read via `GET /api/states/<entity_id>`: the four power sensors, the Nordpool sensor (state + `raw_today`/`raw_tomorrow` attributes), the two temperature sensors, `sun.sun`.
- Cadence: poll ~every 10-15s (matches the 3EM). Simple REST polling is fine; websocket (`/api/websocket`) is an optional push upgrade, not needed for v1.
- The runtime READS over REST. Any HA config CHANGE or bulk history EXPORT goes through OpenClaw, not the runtime code.

### OpenClaw workflow
Claude Code PROPOSES ready-to-paste OpenClaw prompts for any HA data pull or config change; the builder runs them. Pattern: investigate and show before changing, show the diff, wait for approval, note if a restart is needed. Already done this way: the recorder fix, entity discovery, the power/temperature/Nordpool confirmations.

### Config block (real values)
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
SENSOR_TEMP_OUT = sensor.current_temperature      # outdoor, despite the generic name
SENSOR_TEMP_IN  = sensor.temperature_average       # indoor average
SENSOR_SUN      = sun.sun                           # is_dark from below_horizon
```

---

## 12. Build order (Oct 16-30, after Apertus; tight)

Develop on PC, deploy to Pi. One slice at a time.

1. **Nebius hello-world:** one Nemotron call, config-driven, handle `reasoning_content`, print a verdict. Playground-test the model id first.
2. **Cost safeguards + config:** the situation-hash trigger, debounce, hard caps, kill switch, spend tracking (section 8). Guardrails before anything loops on a paid model.
3. **Parse real data:** load the states CSVs with the skip-unavailable / irregular-timestamp / energy->power rules; or read live `_power` sensors. Plot to eyeball signatures.
4. **Baseline + stacking detector** (sections 5.1-5.3): per-phase quiet-stretch rolling baseline; the accounted-total stacking model (steps measured against the running sum of active loads, not the cold floor); step-up/step-down events; matched/unknown/baseline states; feature extraction (step W, phase, duration, cycling). This is the core engine.
5. **Labeling tool** (event + timeframe labeling) -> signature library + draft profiles JSON.
6. **Live matcher + profile scoring** -> active-appliance state with confidence + reason strings.
7. **Reasoning integration - Tier 1 LIVE (the AI centerpiece):** assemble appliances + Nordpool curve + temps + is_dark + profile -> Nemotron -> parsed verdict + JSON action. Clean instrument advice (section 6/6A). CURRENT SLICE.
7b. **Reasoning - Tier 2 NIGHTLY:** the deep once-a-night pass over the event log producing behaviors, efficiency findings, anomalies, cost->carbon notes, and new-load discovery (sections 5A, 6A); output cached for cheap all-day surfacing; dry-register commentary. This is the differentiation - not optional for the pitch, though it can be lightly implemented for the demo (one real behavioral insight + one efficiency nudge + one anomaly line is the minimum that proves the thesis, section 0).
8. **HDMI console (PRIMARY):** the fixed single-screen instrument (section 9) as soon as live detection works. This is the demo backbone.
9. **HA action path (optional):** suggest-and-confirm defer/notify.
10. **M5 UDP panel (optional):** shared renderer over the micro_arcade protocol (section 10).
11. **Submission:** 1-min hardware clip + <=3-min demo video (audio MUST explain Token Factory + NVIDIA model use) + public repo (OSI license at top, README highlighting where Nemotron/Token Factory are used) + Devpost writeup + Nebius/NVIDIA feedback.

Slices 1-8 = a working, filmable system. 9-10 = optional upgrades. If time collapses, ship 1-8 solid.

---

## 13. Submission checklist (Physical AI track)
- NVIDIA open-source model (Nemotron) on Nebius Token Factory. Required.
- Track: Physical AI. Working-demo URL NOT required for this track.
- Video <=3 min, public YouTube, audio explains Token Factory + NVIDIA model use, >=1 min of hardware operating.
- Public repo with an OSI license visible at top + README with setup/run.
- Feedback on Nebius/NVIDIA tools.
- If the project predates the submission period, note what was significantly built during it (the signature layer, reasoning, profiles, console are new).

---

## 13A. Architecture generalization (real future work - state in the writeup, not a footnote)

These are not throwaway footnotes; they show the reasoning-layer approach is bigger than the demo, and they belong as a paragraph in the writeup. The demo runs on the single 3EM aggregate; the architecture generalizes:

- **Any HA power sensor as input.** Virta is not "a 3EM disaggregator" - it is a reasoning layer that can ingest ANY power signal HA exposes: the 3EM aggregate, an individual smart plug, a sub-meter, a solar inverter. Most NILM assumes one aggregate input; reasoning across a heterogeneous mix of aggregate and per-device sensors is architecturally more ambitious and is a natural extension of the config-driven sensor list.
- **Cross-source overlap reconciliation.** If a device has its OWN reading AND is inside the phase-accumulated total, that's double-counting - the reasoning subtracts known per-device readings from the aggregate to sharpen what's left ("total says 3kW, the dishwasher plug says 800W, so the remaining 2.2kW is other stuff"). This is the stacking model (section 5.2) generalized: a known per-device sensor is just a load whose magnitude you're TOLD rather than inferring, which makes everything stacked on top cleaner. It's honestly a better NILM than any HA blueprint, and it proves the approach scales.
- **Better sensing later.** The demo concedes crude sensing (section 0). Higher sample rates, frequency/harmonic analysis, and load-type libraries are all future inputs the same reasoning layer would exploit - the point is the brain is portable across sensors, not tied to this one.

---

## 14. Risks / open items
- **EV load-control masking** (section 4) - rare in practice (night charging); handle via reduced secondary-load confidence, or read charger state if its API is checked and available (parked).
- **`reasoning_content` + no tool-calling** - handled from call one (section 2/6).
- **`raw_tomorrow` empty before ~13:00 CET** - reasoning handles short look-ahead; hits every morning.
- **Overlapping similar loads** (2 saunas, 3 ovens) - honest hard case; demo clean separators, report "uncertain"; cycling decomposition (section 5) is the eventual answer.
- **Credits/cost** - event-driven calls + hard caps (section 8); credits already claimed.
- **Slow-cycler signatures (sauna/oven/floor heating) not yet captured** - Tier 2, data collection underway; demo on Tier 1 regardless.
- **October fatigue** - third October project (after Palsta, Apertus). Deadline Oct 30 gives room after Apertus (Oct 16). Highest-reuse project on the board (existing sensing, Nordpool, devices, terminal protocol) - the argument for doing it, if evenings allow.

## 15. Open decisions (human)
- Name (Virta = suggestion).
- Nemotron model id (from catalog) + confirm behavior in Playground.
- Usage-profile text (section 3.4) - write your real patterns.
- Price-tier thresholds for the situation hash (section 8) - cheap/normal/expensive in c/kWh.
- Whether to pursue the M5 panel and/or the HA action path (both optional).
- Suggest-only vs confirmed-action for HA control - recommend suggest-and-confirm only.

---

## 5A. Retrospective learning (unknown-load discovery over history)

A periodic housekeeping pass that does what the live edge detector structurally can't: discover NEW recurring loads from accumulated history. The live loop asks "what is this load now?" against known signatures; this pass asks "are there recurring shapes in my history that should BECOME known signatures?" - pattern discovery across time, which is a reasoning job (Nemotron), not a step-detection job.

Mechanism (unsupervised load discovery by clustering unknowns): an UNKNOWN load (section 5.3) that recurs with consistent magnitude + phase + duration + time-of-day is almost certainly one real unlabeled appliance. Three "UNKNOWN +2100W phase A, ~45min, weekday evenings" events across two weeks are one washing machine seen three times.

### Split (cheap clustering local, fuzzy interpretation in the cloud)
- **Local, cheap:** cluster the accumulated UNKNOWN events by similar magnitude / phase / duration / time. Simple statistics - no LLM needed to notice three events share a shape.
- **Nemotron, interpret:** hand it the CLUSTERS, not raw data - e.g. "recurring unknown: 2100W, phase A, ~45min, appeared Mon/Wed/Sat evenings, 6x in 14 days." Ask it to reason about what it might be and whether it correlates with known patterns, the usage profile, or the timing of known loads.

### Input: the EVENT LOG, not the raw stream
Do NOT send 14 days of raw power to the LLM (huge, mostly baseline, blows context + budget). Send the compact EVENT LOG the edge already produced during live detection: detected events (matched + unknown) with features - magnitude, phase, duration, timestamp, cycling summary. Two weeks of events is a few hundred rows. One occasional call over compact structured history.

### Correlation-to-label (closing the unknown->known loop)
If a recurring unknown lines up in time with something known - the user ran the washing machine those evenings, another sensor confirms it, or the usage profile says so - the pass PROPOSES a label: "that recurring 2100W/A/45min unknown is probably the washing machine." This promotes unknowns toward named profiles without hand-labeling every event.

### Propose-then-confirm (never auto-label)
The pass PROPOSES; it does not silently create profiles. It surfaces "unlabeled recurring load: 2100W/A/45min/weekday-evenings - possibly a washing machine, label it?" The user confirms or corrects, then it promotes to a named profile (section 7) with the user's label. An LLM guessing "washing machine" from a signature is a hypothesis, not a fact; a wrong auto-label would pollute the signature library and every future detection. Same suggest-and-confirm discipline as HA control.

### The feedback interface: a human-readable worklist file (async)
The nightly pass and the human communicate through a persistent, human-readable file - no UI, survives restarts, fits a keyboard-driven workflow. The pass writes discovered clusters with its evidence and guess plus a blank answer field; the user fills answers whenever they like; the NEXT nightly run reads the answers, acts, and updates the file. Fully asynchronous - propose, reply on your own schedule, picked up next cycle. Nothing blocks; it never interrupts the live console.

Entry shape (illustrative):
```
[cluster_07]  2100W  phase A  ~45min  Mon/Wed/Sat evenings  6x/14d
  llm_guess: probably washing machine - evening recurrence, magnitude+duration
             fit a heated wash cycle, matches stated laundry days
  your_label: ________
  status: proposed
```

**Label vs comment fork (build BOTH paths):** the `your_label` field accepts either -
- A clean label ("washing_machine") -> the pass promotes it DIRECTLY next run: build the profile from the cluster's measured features + the user's name. No LLM needed - cheap, deterministic.
- A freeform COMMENT ("it's the washing machine but only the heated cycle; the cold one is smaller and separate") -> the pass sends the comment back through the LLM next run to build a smarter/split profile.
The pass decides which path by whether the answer is a simple label or prose.

**Status lifecycle (load-bearing):** each entry has a state - `proposed` / `confirmed` / `rejected` / `needs-more-data` - so the pass knows what to leave alone. A confirmed+promoted load is not re-proposed or re-asked. `your_label: ignore` (an LLM hiccup or a one-off) -> `rejected`, stops surfacing. Without status, the file re-proposes everything nightly and becomes noise.

**Never clobber human input:** the pass owns the file and updates its OWN fields (guess, status), appends new discoveries, but NEVER overwrites `your_label`. Safe pattern: read whole file, preserve every human field verbatim, touch only machine-owned fields, write back - OR keep human answers in a separate file the pass only reads. Human input is sacred; the pass reads it, never rewrites it.

**Stable cluster IDs across runs:** match clusters between nights by their SIGNATURE (magnitude/phase/duration/timing), not by file position, so last night's `cluster_07` is the same entry tonight after re-clustering. Otherwise an answer drifts onto the wrong load overnight - a maddening bug. Assign a stable id per recurring signature and carry it forward.

The file doubles as demo material: the LLM's guesses, the user's one-word confirmations, and a growing list of learned appliances is a concrete artifact of the system learning over time - more convincing than a claim.

### Cadence
Not the live loop - periodic housekeeping. Once a day (natural: overnight, right after the quiet-stretch baseline recalibration, section 5.1, when nothing competes) or on demand. One retrospective call/day is negligible against the budget, and pattern discovery doesn't need real-time - a weekly appliance reveals itself over weeks regardless.

### Status: Tier-2 / roadmap, not Day-1
The live three-state detection (5.3) is the core that must work for the demo. This learning layer is the "and it gets smarter from its own data" capability on top - a strong writeup beat ("it flagged a recurring unknown and, over two weeks, worked out it was the washing machine"). Build it if the core is solid and time allows; frame it as the learning loop even if lightly implemented. Do not let discovery/clustering/promotion eat the weeks the live engine needs.
