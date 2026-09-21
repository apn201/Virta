# YouTube — title, description, tags

For the demo video in `devpost/VIDEO-PLAN.md`. Upload **unlisted**, then paste the watch
URL into the Devpost video field.

Timestamps below match the shot list. If the edit comes out at a different length, fix
them before publishing — a chapter list that doesn't line up is worse than none.

---

## Title

100 character limit. This is 81.

```
Virta: whole-house NILM with one Shelly 3EM, a Raspberry Pi 3 and NVIDIA Nemotron
```

Alternates:

```
One 50 EUR meter and an old Raspberry Pi work out what every appliance in the house is doing
```

```
Virta — the house explains itself: Shelly 3EM + Raspberry Pi 3 + NVIDIA Nemotron
```

The first one front-loads the words people actually search for. The second reads better
and will be found by nobody.

---

## Description

Roughly 157 characters show above the fold, so the first two lines carry it.

```
One 50 EUR clamp meter on the main supply and a Raspberry Pi 3 from 2016. No smart plugs,
nothing attached to any appliance. Virta works out what every load in the house is doing,
what it costs at the real Nordpool price, and what the house does with its week.

The detection is the easy part. A step detector can tell you the kettle is on, which you
knew, because you just switched it on. What it can't tell you is that the always-on load
is 73% of everything the house uses, or that the car charged during the expensive stretch
when four hours earlier would have cost a quarter as much. That's the reasoning tier, and
it runs on NVIDIA Nemotron through Nebius Token Factory.

Raw power never leaves the building. The model gets conclusions — what's running, the
price curve, a nightly digest that is a file on disk you can open. Never the stream.

CHAPTERS
0:00 One meter, one old Pi
0:12 Why a detector isn't enough
0:30 How it works
0:42 Kettle and toaster, live
1:15 Teaching it a new appliance
1:50 Swapping a light when the price turns
2:15 What the nightly pass found
2:42 What it costs to run

THE HARDWARE
Shelly 3EM, about 50 EUR, three clamps on the mains
Raspberry Pi 3, already in a drawer
An HDMI screen for the console
Home Assistant on the LAN, read over REST

WHAT RUNS WHERE
On the Pi, every 15 seconds, for nothing: base load from quiet stretches, a stacking step
detector, rhythm detection for loads too small to make a step, context scoring, and all
the price arithmetic.

On Nebius Token Factory, when the situation actually changes: Nemotron 3 Super writes the
live line and names unknown loads. Once a night, Nemotron 3 Ultra does the deep pass over
the whole archive.

A cloud call needs four gates to agree — kill switch, hard caps, a changed situation,
debounce. On a simulated day that's 28 calls instead of 5,760. The whole build so far has
cost 48 calls.

TEACHING IT AN APPLIANCE
Something unknown switches on. Super guesses from the step size, the phase, the time and
what else is running, and the house says it out loud. You flick the switch off and back
on. That's the yes. It writes the profile from the steps it measured and knows it next
time. The light switch is the entire interface.

HONEST LIMITS
It only sees what the 3EM sees. The ovens, the microwave, the air fryer and the sauna are
invisible on this install, and it says so rather than guessing. Steps under about 120 W
aren't detected at all. Monthly figures are estimates and are labelled as such; every
per-run price is real, costed slot by slot at the price in force while it ran.

Every number on screen is checked against the digest the model was given. A line with a
number that isn't in it gets dropped.

The expensive price in the light-swap scene is an override — a real expensive hour can't
be booked for a video. The console marks it DEMO PRICE by itself. Everything else is the
house.

CODE
https://github.com/apn201/Virta
MIT. The repo has the setup, the device profiles measured off this house, the labelling
worklist, and one real day published so you can run the whole pipeline without a house.

Built for the Nebius x NVIDIA Global AI Hackathon, Physical AI track.

#nemotron #raspberrypi #smarthome
```

---

## Tags

YouTube's tag field caps at **500 characters including the commas**. These 30 tags come
to 447, so there's room for two or three more. Paste as one line.

```
nilm, non-intrusive load monitoring, load disaggregation, energy monitoring, home energy monitor, electricity monitoring, shelly 3em, raspberry pi, raspberry pi 3, home assistant, nvidia nemotron, nemotron, nebius, nebius token factory, edge ai, edge computing, local first, smart home, diy smart home, home automation, nordpool, energy saving, electricity price, ev charging, three phase power, python, llm, ai agent, reasoning model, physical ai
```

Order matters a little — YouTube weights the first few. The first three are what this
actually is, the next block is what people search when they want to build one.

## Hashtags

The first three show above the title. Don't use more than three; past that YouTube
ignores all of them.

```
#nemotron #raspberrypi #smarthome
```

## Thumbnail

Use the console shot, not the architecture card — the card is unreadable at thumbnail
size. `var/pc_1280.png` cropped to the DATA panel with the detection table visible, or a
photo of the Pi and the screen together. Add three or four words at most, big:
`ONE METER. WHOLE HOUSE.`

## Pinned comment

Worth posting, it answers the first question people ask:

```
The 3EM only sees what's on the phases it measures, so the ovens, microwave, air fryer and
sauna are invisible on this install. Virta says so rather than guessing. Detection limit is
about 120 W. Code and the measured device profiles: https://github.com/apn201/Virta
```

## Before you hit publish

- [ ] Unlisted, not private. Private links don't embed on Devpost.
- [ ] Check the chapter timestamps against the finished cut.
- [ ] Watch it once in an incognito window to confirm the link works.
- [ ] Subtitles: upload the VO script as a transcript. The butler lines are the product
      and half the judges will watch muted.
