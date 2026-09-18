# OpenClaw prompt 02 — does the Eve EV charger expose its own power / load-control state?

**Why Virta needs this:** spec §4 records that the Eve charger throttles its own draw
when household consumption rises, which can MASK another appliance switching on — the
charger backs off by roughly the same amount and the total stays flat. That breaks naive
step detection during charging. The spec parks this as non-blocking (the EV mostly
charges at night) but notes the clean fix: if the charger exposes power or load-control
state via an API, read it directly instead of inferring it.

**Cost of knowing:** the stacking detector (§5.2) can treat the EV as a known-but-variable
load with a real number instead of a widened tolerance. Cheap to check, meaningful payoff.

**When to run:** any time. Read-only investigation, no config changes.

---

Paste into OpenClaw:

> Read-only investigation, please change nothing.
>
> I have an Eve / Plugit EV charger. I want to know whether Home Assistant can see its
> actual instantaneous power draw and/or its dynamic load-control state, as opposed to me
> inferring the draw from the house's 3-phase meter.
>
> Please:
> 1. List every entity in HA whose name or device relates to the EV charger (Eve, Plugit,
>    charger, EVSE, wallbox — search broadly). For each, show the entity_id, its current
>    state, its unit, and its device class.
> 2. Say which integration provides them, and whether that integration is local-polling,
>    local-push, or cloud.
> 3. Flag specifically whether any of them reports: instantaneous power (W), current (A)
>    per phase, a charging/idle status, or a dynamic-load-limit / current-limit value.
> 4. If nothing exists in HA, say whether the charger exposes a local API (Modbus, REST,
>    OCPP) that could be added later — I'm only asking whether the data is reachable in
>    principle, I'm not asking you to set anything up.
>
> Report what you find; don't add or modify any integration or entity.

---

**If the answer is "yes, it reports power":** I'll read that entity alongside the 3EM
sensors and subtract it from the accounted total, so the stack stays accurate while the
car charges.

**If the answer is "no":** the spec's fallback stands — treat any sustained ~9–12 kW
balanced three-phase draw as "EV active, load-control possible, secondary-load confidence
reduced", and let the reasoning layer narrate the uncertainty rather than hide it.
