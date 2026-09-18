# OpenClaw prompt 01 — export power history for Tier-2 signature building

**Why Virta needs this:** the current CSVs cover ~9h42m and contain only Tier-1 loads
(EV, kettle, toaster, halogens). Slices 4–6 (baseline, stacking detector, signature
library) need the Tier-2 cyclers — sauna, oven, floor heating — captured over a full
run so their thermostat rhythm is visible. Spec §5 / §5.3 / §12.

**When to run:** after a day or two that includes at least one full hot sauna run and
one oven run. Read-only — this prompt exports data, it changes nothing.

---

Paste into OpenClaw:

> I need a states-history export from Home Assistant for a load-disaggregation project.
> This is read-only — do not change any configuration.
>
> Please export the recorder's states history for these four entities, for the last 7
> days (or as far back as `purge_keep_days` allows, whichever is shorter):
>
> - `sensor.3em_channel_a_power`
> - `sensor.3em_channel_b_power`
> - `sensor.3em_channel_c_power`
> - `sensor.3em_total_power`
>
> Format: long CSV with the columns `entity_id,last_updated,state` — the same shape as
> the earlier export. Keep every sample; do not downsample, round, or fill gaps, and do
> not drop rows whose state is `unavailable` or `unknown` (I handle those myself and I
> want to see where they occur). Timestamps in local time with the offset if possible.
>
> Before exporting, tell me:
> 1. The actual time range and row count you're about to export.
> 2. Whether `sensor.3em_total_power` is recorded at a different rate than the three
>    channel sensors — in the earlier export it had roughly 3× as many rows, which I
>    think means the template sensor updates whenever ANY phase updates. Show me its
>    template definition so I can confirm that, but do not modify it.
> 3. Whether any of the four had gaps or `unavailable` stretches in the window.
>
> Then wait for my go-ahead before writing the file.

---

**What I do with it:** feeds the baseline calibration (§5.1, quiet-stretch detection),
the stacking detector (§5.2), and the cycling characterisation that Tier 2 depends on.

**Also useful, same session:** if you ran appliances deliberately, note the approximate
windows ("21:05–21:50 sauna, 18:20–19:10 oven 1"). Spec §6 labelling accepts timeframe
labels, so approximate times are enough — I don't need exact switch-on instants.
