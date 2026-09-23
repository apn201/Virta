# LinkedIn post

LinkedIn shows about 200 characters before "…see more", so the first two lines carry it.
No markdown renders there — asterisks show up as asterisks. Paste as plain text, keep
the blank lines.

Roughly 1,800 characters of the 3,000 allowed.

---

```
One clamp meter in the fuse box, about 60 euros. A Raspberry Pi 3 from 2016 that was in a drawer. That is the hardware.

I wanted to know what the always-on load in this house actually is. The proper way is a meter on every circuit, which means an electrician and a few hundred euros of hardware. So I put one meter on the mains instead and wrote a step detector.

It worked. And it was useless. It could tell me the kettle was on, which I knew, because I had just switched it on.

What it could not tell me: the always-on load is 73% of everything the house uses. Or that the car charged at nine in the morning for 23 cents, when the same charge that night would have cost 5.

Those two sentences are the product. A detector cannot write them. It has no memory and no idea what "usual" is.

So the meter stays cheap and the thinking goes to NVIDIA Nemotron on Nebius Token Factory. Nemotron 3 Super writes the live line. Once a night, Nemotron 3 Ultra reads the whole archive and reports what it found. Everything numeric — base load, step detection, the price arithmetic — happens on the Pi, for nothing.

Raw power never leaves the house. The model gets conclusions, never the stream.

It learns new appliances by gesture. Something unknown switches on, Super guesses what it is from the step size, the phase and the time of day, and the house says it out loud. You flick the switch off and back on. That is the yes. It writes the profile and knows it next time. The light switch is the whole interface.

A cloud call needs four gates to agree before it is allowed, so a day comes to 28 calls instead of 5,760. The whole build so far has cost 48 calls.

Video: https://youtu.be/raFAFtf1x2k
Code, MIT: https://github.com/apn201/Virta
Devpost: https://devpost.com/software/virta

Built for the Nebius x NVIDIA Global AI Hackathon, Physical AI track.

#nemotron #raspberrypi #energy
```

---

## Notes

- The first line is the hook and it is just the bill of materials. Don't move the
  hardware further down to make room for a framing sentence.
- LinkedIn eats links in the body less than it used to, but the post will still travel
  further if the video link goes in the first comment instead. Worth testing: post it
  with the links, and if reach looks flat, next time put them below.
- Three hashtags. More reads as fishing.
- If you want a shorter version, cut the gesture paragraph. It is the best part, but the
  post works without it and nothing else can go.
- Pair it with one image: the console photo, or the fuse box with the clamps. Not the
  architecture card — too small to read in a feed.
