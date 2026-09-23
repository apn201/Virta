# Devpost — About the project

The project story, for the "About the project" field. Paste as markdown.

The 3EM price says 60 euros, matching what is already on the live page.

---

```markdown
## What it is

Virta is an energy advisor that knows what every appliance in the house is doing. Actually
it is one Shelly 3EM on the main supply, about 60 euros, and a Raspberry Pi 3 that was
already in a drawer. No smart plugs, no per-circuit clamps, nothing attached to any
appliance. The detector on the pi is maybe 700 lines and the thinking is done by NVIDIA
Nemotron on Nebius Token Factory.

The meter sees one number, the whole house. Everything else is worked out from that number.

## Why

I wanted to know what the always-on load in this house actually is. Ventilation, fridge,
freezer, couple of servers, the miners. The proper way is a meter on every circuit, which
means an electrician and a few hundred euros of hardware.

So I put one clamp meter on the mains instead and wrote a step detector. That part was
easy, mostly vibecoding, and it worked on the first real day. It could tell me the kettle
was on. Which I knew, because I had just switched it on.

What it could not tell me is that the always-on load is 73% of everything the house uses
(59 of 81 kWh over two and a half days, about 21.6 euros a month). Or that the car charged
at 08:51 on a Wednesday and paid 0.23 EUR, when the same charge starting 03:45 that night
would have cost 0.05. A detector has no memory and no idea what is usual, so it cannot say
either of those. That is the part Nemotron does.

## How it works

Three tiers, because the jobs are different sizes.

Live: the pi reads Home Assistant every 15 seconds over the lan. Base load from quiet
stretches, stacking step detection, rhythms, context scoring, and all the price arithmetic
happen there, for free. When the situation actually changes, Nemotron 3 Super gets a JSON
summary of what is running, the Nordpool curve in 15 minute slots, temperatures, darkness,
and the house's own recent numbers, and writes one line about it.

Nightly: at 03:30 the archive is replayed and one deep call goes to Nemotron 3 Ultra, which
writes up to six insights. This is where the 73% line came from.

Surfacing: the console rotates last night's conclusions all day. Costs nothing.

Teaching: something switches on that matches no profile, and after 20 seconds Super guesses
what it is from the step size, the phase, the time and what else is running. The speaker
says it out loud - "Something new on A, 1184 W, likely a hair dryer. Switch it off briefly
and back on if I'm right." You flick the switch off and on, and that is the yes. The flick
is shorter than the detector's 10 second settle, so the teacher watches the raw phase
reading for a dip that recovers instead. Then it writes profiles/hair_dryer.json from the
steps it measured. Light switch is the whole interface, which I liked, and it also meant I
did not have to build an app.

It can act too. Nemotron's reply may carry a sentence for the speaker and one light switch
action, and code decides whether they happen. Only lights I listed by name, only turn_off,
only when the price is expensive, announced first with 15 seconds to cancel. It never
touches the car.

## The edge part

Raw power never leaves the house. Not to Nebius, not to a vendor cloud, not into the repo.
The model gets abstractions: which loads are running, the price curve, a nightly digest
that is a file on disk you can open (var/digest.json). data/ is git-ignored.

This is also why Virta keeps its own archive. Home Assistant's recorder purges at 14 days,
so every settled day is frozen into data/archive/ as gzipped CSV, about 400 KB a day, and
kept. A base load creeping up over a month is invisible otherwise.

profiles/ and labels/ are in the repo on purpose, as example data. Those are the real
device profiles measured off this house and the real labelling worklist with its clusters
and my answers. The power stream they came from stays here.

## Tokens

The local loop runs at 15 seconds and costs nothing. A cloud call needs four things to
agree first: the kill switch is off, the hourly and daily caps have room, the situation
hash (active appliances, price tier, is_dark) actually changed, and the debounce window
has settled. Anything unclear serves the cached verdict instead of calling.

On a simulated day at the real 15 second rate that comes to 28 calls instead of 5,760.
The whole build - hello world, four model comparisons, labelling passes, nightly passes,
filming - came to 48 calls, 108k input tokens and 161k output tokens on the ledger.

## What the real data forced

None of these were in the plan.

The 3EM's total sensor lies three times per push. All three phases arrive within about 5 ms
and the total is recomputed after each one, so two of every three totals are partial sums.
An EV switch-on shows three fake stair-steps in 10 milliseconds. Collapsing the bursts took
51k raw rows down to 16k real readings.

The kettle is 2016 W, not the 1200 W I put in my own spec. The spec number came from
one-minute averages of a 73 second event.

One device cannot be on twice. If a profile matches while it is already running its OFF was
missed, and the earlier one has to be closed, otherwise the stack never empties and
recalibration is blocked forever.

OFF steps have to match more loosely than ON steps. A halogen measured +230 W on and -194 W
off.

While the car charges everything else is unreliable, because the charger's load control
absorbs other loads. Not total masking though - a +450 W load on phase C went on mid-session
and the total rose with it - but enough that other detections get a flag.

Parts of the base load switch off too. A drop nothing explains means a server or a fan
stepped down, so it joins the stack as a negative unknown load and pairs with the step back
up later.

On the labelled test run every load was detected at the right time: halogen 1 87%, halogen 2
87%, kettle 95%, toaster 89%, EV 98%. 29 ON and 29 OFF events, all paired, stack ends empty.

## What went wrong

Reasoning is billed and counted against max_tokens, which I did not understand for most of
an afternoon. A labelling call at 4096 came back finish_reason "length" with reasoning_tokens
4096 of 4096. All thinking, no answer. The same call finishes around 10.8k.

Asked for one short line in a dry voice, Lightning found a good line early and then kept
polishing it past 8,000 tokens and returned nothing. Adding "write the first good line and
stop" to the prompt brought it to 4.4k. A reasoning effort setting would make that a
parameter instead of prompt folklore.

Examples in a prompt come back verbatim. An example verdict "PRICE NORMAL" was reproduced at
a price that was EXPENSIVE, and an example observation came back word for word. They are
placeholders now and every fact is checked at the edge before it reaches the screen.

Early nightly drafts overreached on thin data. One called the EV "overdue" after one
observed session and one suggested shifting halogen lights to the middle of the night. So
profiles carry a shiftable flag, efficiency suggestions need at least 0.50 EUR/month, and
"something stopped" needs an established routine. In the prompt and also checked in code,
because the prompt alone leaks.

Every number on a console line is checked against the digest the model was given and a line
with a number that is not in it gets dropped. Same for the creepy categories - health, mood,
relationships, visitors, sleep, and saying the house is empty. It may be personal about a
device ("EV at full tilt, driving a Ferrari today?"), not about a person. That is in
guardrails.py, not only in the prompt.

## Models

Super (nvidia/nemotron-3-super-120b-a12b) for the live line and for naming unknown loads,
Ultra (nvidia/Nemotron-3-Ultra-550b-a55b) for the nightly pass. Nano got the facts right but
wrote flat instrument lines and would not take the butler voice however I wrote the prompt,
so the live tier went up a size. Ultra wrote better nightly insights than Lightning and used
about half the tokens doing it, 5.2k against 10.8k, which I did not expect from the bigger
model.

Switching tier is one env var on the same endpoint, which is the only reason I tested four
models on real data at all.

## Honest limits

It only sees what the 3EM sees. The ovens, the microwave, the air fryer and the sauna are
invisible on this install and Virta says so instead of guessing. Steps under about 120 W are
not detected, so small always-on things stay inside the base load bundle, which is measured
as one lump on purpose. The Eve charger sends nothing to Home Assistant, so the car is
detected purely by signature, being the only balanced three-phase load in the house.

Per-run prices are real, costed slot by slot at the price in force while the thing ran.
Monthly figures are estimates and labelled as such. There were only two and a half days of
history when the first nightly pass ran, and the digest says so.

In the video the expensive price is an override, because a real expensive hour cannot be
booked for a shoot. The console marks it DEMO PRICE by itself.

It is a box in a fuse room reading a real house and switching real lights, so I think it
belongs in the physical AI track. The M5 panel is still in the drawer, I ran out of days.
```
