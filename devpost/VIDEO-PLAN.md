# Virta — video plan

Target: **2:55**, hard ceiling 3:00. 1920x1080, 30 fps. Unlisted YouTube link for the
Devpost embed.

Judges watch a lot of these. The first 15 seconds have to say *cheap hardware, real
house*, or nothing else lands.

## The one sentence the video has to sell

A 50 EUR clamp meter and an old Raspberry Pi 3 see one number for the whole house.
Nemotron turns that one number into what a per-circuit install would tell you — and the
raw number never leaves the building.

---

## Prep, the day before

- [ ] `git pull` on the Pi, restart `virta-live` and `virta-console`. Console fullscreen on HDMI.
- [ ] Check `var/state.json` is fresh (no **NO SIGNAL**).
- [ ] Raise the headroom for filming: `--demo` gives 60 calls/h with the same daily cap.
      Check `var/cloud_usage.json` has room. Don't film on the last 20 calls.
- [ ] `profiles/hair_dryer.json` **deleted** — the teach scene needs it to be unknown.
      Keep a copy so you can re-shoot.
- [ ] Kettle filled, toaster plugged in, hair dryer on a phase the 3EM sees. Test that the
      step is over 120 W first, with `python -m virta.live --dry`.
- [ ] `VIRTA_AUTO` / `VIRTA_SWAP` set to the Big Bang → Rengasvalo 2 pair. Test the swap once.
- [ ] Speaker volume up, `VIRTA_QUIET` window not active.
- [ ] Last night's nightly pass actually ran — `var/insights.json` timestamped today.
- [ ] Clean the fuse box door. It will be on screen at 4K.

## Gear

- Phone on a tripod for the hardware and the light swap. Lock exposure before the lamp
  swap or auto-exposure will eat the shot.
- OBS on the PC for console capture. Run `python -m virta.mirror` so the PC console
  mirrors the Pi's state. Capture at 1280x720 and upscale — the console is drawn at that
  design scale and stays sharp.
- Record the voiceover separately. A phone mic in a room with curtains beats the camera mic.

## Fallbacks

Nothing here needs to be faked, but have these ready:

| If | Do |
|---|---|
| The house won't cooperate live | `python -m virta.demo` replays the published 16.09 day through the same detector, scoring and advisor |
| A cloud call fails mid-take | The console keeps the previous verdict on screen. Cut, retake |
| The price is cheap all day (it will be) | `echo 15 > var/demo_price`. The console marks **DEMO PRICE** in amber by itself. Say so in the voiceover |
| Teach-by-gesture misses the flick | Off for 3–5 s, then on. Under the detector's 10 s settle. Practice twice before rolling |

---

## Shot list

### 1. Hook — 0:00–0:12

**Shot:** Close on the Shelly 3EM in the fuse box, three clamps on the mains. Cut to the
Raspberry Pi 3, dusty, HDMI cable, the console glowing green next to it.

**VO:**
> This is a 50 euro clamp meter in the fuse box. This is a Raspberry Pi 3 from 2016.
> That's the hardware. No smart plugs, nothing on the appliances.

**On screen:** `Shelly 3EM ~50 EUR` · `Raspberry Pi 3 — already had it`

---

### 2. The problem — 0:12–0:30

**Shot:** The console, full frame. The trace moving. The DATA panel.

**VO:**
> One meter sees one number, the whole house. Something switches on, the number goes up.
> That part is easy. The hard part is what it means. A detector can tell you the kettle
> is on. It can't tell you the always-on load is three quarters of your bill.

---

### 3. Architecture card — 0:30–0:42

**Shot:** One still card. Ten seconds. Don't animate it.

```
  3EM  ──15 s──▶  Raspberry Pi 3  ──▶  detector · baseline · rhythms · price maths
                        │
                        ├── conclusions only ──▶  Nemotron on Nebius Token Factory
                        │                          Super (live) · Ultra (nightly)
                        │
                        └── raw power stays here ───────────▶  data/archive/
```

**VO:**
> So the meter stays cheap and the thinking goes to Nemotron on Nebius Token Factory.
> Raw power never leaves the house. The model gets conclusions — what's running, the
> price curve, a digest. Never the stream.

---

### 4. Live detection — 0:42–1:15

**Shot:** Hand switches the kettle on. Cut to the console. The trace steps. **KETTLE**
appears in the table with SURE, W, ON, COST. Then the toaster goes on and stacks on top.

**VO:**
> Kettle. Two thousand and sixteen watts on phase B, matched against the profile at 95
> percent. The cost column is the run so far at the real Nordpool price of every slot it
> has spanned. Toaster on top of it — every step is measured against what is already
> running, so loads stack.

**Shot:** The chat window. The newest line types itself out.

> The kettle and toaster have been on for ten minutes, suggesting breakfast is underway.

**VO:**
> That's Nemotron 3 Super. It gets today's kilowatt hours against yesterday at the same
> clock time, the base load trend, the weather, the rhythms. It talks about the house,
> and mentions the price only when the price is doing something.

---

### 5. Teach by gesture — 1:15–1:50

This is the scene people will remember. Give it room.

**Shot:** Hair dryer switched on. Console shows `HAIR DRYER? A`. The speaker says it out loud:

> Something new on A, eleven hundred eighty four watts, likely a hair dryer. Switch it
> off briefly and back on if I'm right.

**VO:**
> It doesn't know this one. Super guesses from the step size, the phase, the time of day,
> and what else is on.

**Shot:** Hand flicks the switch off — beat — on. The console renames the row to
**HAIR DRYER**. Speaker:

> Noted. The hair dryer it is — I shall know it next time.

**Shot:** Cut to the terminal, `cat profiles/hair_dryer.json`, two seconds.

**VO:**
> The flick is the yes. It's shorter than the detector's settle time, so the teacher
> watches the raw phase reading for a dip and a recovery. Then it writes the profile from
> the steps it measured. The light switch is the entire interface.

---

### 6. Price and agency — 1:50–2:15

**Shot:** Terminal, `echo 15 > var/demo_price`. The console flips to **DEMO PRICE** in amber.

**VO:**
> A genuinely expensive hour can't be booked for a video, so I override the price for this
> slot. The console says so itself. Everything else is real.

**Shot:** Console announcement and countdown. Speaker:

> Switching Big Bang off and Rengasvalo 2 on.

**Shot — the money shot:** wide on the room. The expensive lamp goes out, the cheap one
comes on. One continuous take, no cut.

**VO:**
> It announces first and waits fifteen seconds. N cancels. It only touches lights I listed
> by name, only turns them off, and only when the price is expensive. It never touches
> the car.

---

### 7. The long view — 2:15–2:42

**Shot:** The nightly insights on the console, rotating. Then two seconds of
`data/archive/` in a file listing.

**VO:**
> At half three in the morning it does one deep pass on Nemotron 3 Ultra. Base load 987
> watts — seventy three percent of everything the house used. The car charged at ten to
> nine in the morning and cost 23 cents. The cheapest window of the same length that night
> was five. About two euros a month.
>
> Every number on that screen was checked against the digest the model was given. A line
> with a number that isn't in it gets dropped.
>
> Home Assistant keeps 14 days. Virta freezes every day into its own archive and keeps it,
> 400 kilobytes a day, so anything slower than two weeks stays visible.

---

### 8. Cost, and out — 2:42–2:55

**Shot:** The ADVICE panel corner showing cloud calls used. Then the end card.

**VO:**
> The local loop reads the meter every fifteen seconds for nothing. A cloud call needs four
> gates to agree — kill switch, hard caps, a changed situation, debounce. On a simulated
> day that's 28 calls instead of 5,760. The whole build so far is 48 calls.

**End card:**

```
VIRTA
Shelly 3EM  +  Raspberry Pi 3  +  NVIDIA Nemotron on Nebius Token Factory
github.com/apn201/Virta                                              MIT
```

---

## B-roll to grab while you're there

Shoot these loose, they fill gaps in the edit:

- Fuse box door opening, clamps in focus.
- The Pi's power LED, macro.
- The console at an angle, phosphor green on a dark wall.
- Kettle steam with the console screen behind it.
- A hand on the light switch, nothing else in frame.
- Terminal scrolling `var/events_live.jsonl`.
- The price strip on the ADVICE panel, close.

## Editing notes

- Cut on the switch. Every appliance action lands on a cut to the console.
- No music under the spoken lines. Let the house talk.
- Subtitles on. The butler lines are the product and half the judges will watch muted.
- The console is phosphor green on near-black. Don't grade it warm.
- If you run long, cut section 2 to one sentence. Never cut section 5.

## Image gallery for Devpost (up to 15)

1. `var/pc_1280.png` — the full console. **First image**, cropped 3:2. This is the cover.
2. `var/console_demo.png` — the demo day.
3. `var/console_thinking.png` — a call in flight.
4. `var/pi_1024.png` — the console at the Pi's resolution.
5. Photo: the 3EM in the fuse box.
6. Photo: the Pi and the HDMI screen together.
7. Photo: the lamp swap, before and after side by side.
8. Screenshot: `labels/worklist.md` open in an editor — the human-in-the-loop part.
9. Screenshot: `profiles/hair_dryer.json`, written by the gesture.
10. Screenshot: `var/insights.json`, nightly output with `model_explanation` visible.
11. The architecture card from section 3.
