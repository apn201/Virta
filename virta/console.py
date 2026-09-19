"""Slice 8: the console - a fixed single-screen scientific instrument (spec 9).

Run (reads var/state.json, which the live loop writes):
    python -m virta.console                      # a window on the PC (dev)
    python -m virta.console --fullscreen         # the Pi's HDMI screen (deploy)
    python -m virta.console --size 800x480       # try the Pi display's resolution
    python -m virta.console --snapshot out.png   # render one frame to a file, no window

One renderer draws Virta's state onto a surface; only the transport differs.
The window here, the Pi's framebuffer later, an off-screen surface for the M5
panel after that (spec 10) - the draw code is written once.

Design rules from the spec, enforced here:
  - MOTION = INFORMATION. The trace scrolls because time passes and power
    changes; the Ferraris disc turns at a speed proportional to the watts the
    house is drawing. Nothing else moves. No idle animation, no pulsing.
  - Three self-contained panels with hard margins, so a printed bezel can frame
    each as its own instrument: DYNAMIC (trace + disc), DATA (readouts +
    detections), ADVICE (live verdict, nightly insight, price strip).
  - Palette: near-black, phosphor greens for data, dim grey-green for grids
    and labels, ONE warm accent (amber) reserved for alerts and expensive
    prices. Colour as signal, never decoration.
  - Two voices (spec 6A): the live verdict is the clean instrument line; the
    nightly insight is the dry commentary line beneath it.

The console only READS state.json. It never talks to HA or Nebius, so a crash
here can't touch detection or spending, and the loop keeps running without it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

STATE_PATH = "var/state.json"
STALE_AFTER_S = 45  # the live loop writes every ~15 s
BASE_W, BASE_H = 1280, 720  # design size; everything scales from here
TRACE_MINUTES = 20
DISC_REV_PER_KW = 0.12  # disc speed: 1 kW = one turn per ~8 s; the EV makes it race

# palette (spec 9)
BG = (6, 12, 8)
PANEL = (9, 18, 12)
FRAME = (32, 66, 44)
GRID = (20, 40, 27)
BAR = (40, 88, 58)  # future price slots: must read on a cheap LCD, not just a good monitor
BAR_PAST = (26, 56, 37)
LABEL = (78, 120, 90)
TEXT_DIM = (96, 150, 110)
GREEN = (120, 240, 150)  # live data, primary
GREEN_MID = (70, 190, 110)
PHASE = {"A": (170, 245, 190), "B": (90, 210, 130), "C": (40, 160, 95)}  # three shades, direct-labelled
AMBER = (232, 165, 50)  # alerts + expensive price ONLY
AMBER_DIM = (120, 85, 30)

LAMP_MOODS = {  # the real lamp's colours (actions.MOODS), echoed on screen
    "wait": ((255, 70, 30), "WAIT"), "run": ((40, 230, 90), "RUN NOW"),
    "noticed": ((170, 60, 255), "NOTICED"), "calm": ((50, 100, 255), "CALM"),
}

GLYPH = [  # single-letter load glyphs, terminal idiom (spec 9)
    ("ev", "E"), ("kettle", "K"), ("sauna", "S"), ("oven", "O"), ("halogen", "H"),
    ("coffee", "C"), ("floor", "F"), ("toaster", "T"), ("fridge", "R"),
]


def glyph_for(load: dict) -> str:
    if load.get("base_part_off"):
        return "-"
    if load.get("state") == "UNKNOWN":
        return "U"
    lid = str(load.get("id", ""))
    for prefix, letter in GLYPH:
        if lid.startswith(prefix):
            return letter
    return lid[:1].upper() or "?"


def nice_ceiling(value_kw: float) -> float:
    """Round an axis maximum up to 1/2/5 x 10^n."""
    if value_kw <= 0:
        return 1.0
    exp = math.floor(math.log10(value_kw))
    for m in (1, 2, 5, 10):
        if m * 10 ** exp >= value_kw:
            return m * 10 ** exp
    return 10 ** (exp + 1)


class ConsoleRenderer:
    """Draws one frame of the instrument onto any pygame Surface."""

    def __init__(self, size: tuple[int, int], bezel: int = 40) -> None:
        import pygame

        self.pg = pygame
        self.bezel = bezel  # design-size px between and around panels, for a printed bezel
        self.resize(size)
        self.disc_angle = 0.0
        self.disc_speed = 0.0  # rev/s, eased toward the reading (a real disc has inertia)
        self.own_trace: list = []  # built from successive readings if state.json carries no trace
        self._last_when = None
        self._text_cache: dict = {}
        self._chat_seen: dict = {}  # line key -> when this console first showed it
        self._chat_primed = False  # lines present at start are not typed out again

    # --- layout ---------------------------------------------------------------
    def resize(self, size: tuple[int, int]) -> None:
        pg = self.pg
        self.w, self.h = size
        self.s = min(self.w / BASE_W, self.h / BASE_H)
        face = "consolas,dejavusansmono,liberationmono,couriernew,monospace"
        self.f_tiny = pg.font.SysFont(face, max(12, int(12 * self.s)))
        self.f_small = pg.font.SysFont(face, max(13, int(15 * self.s)))
        self.f_mid = pg.font.SysFont(face, max(16, int(20 * self.s)))
        self.f_verdict = pg.font.SysFont(face, max(20, int(24 * self.s)), bold=True)
        self.f_verdict_big = pg.font.SysFont(face, max(25, int(30 * self.s)), bold=True)
        self.f_small_bold = pg.font.SysFont(face, max(13, int(15 * self.s)), bold=True)
        self.f_comment = pg.font.SysFont(face, max(14, int(17 * self.s)))
        self.f_big = pg.font.SysFont(face, max(28, int(34 * self.s)), bold=True)
        self.f_chat = pg.font.SysFont(face, max(17, int(21 * self.s)), bold=True)  # Virta's newest line
        # floors: below the 1280x720 design size (the Pi's 7" 1024x600) text keeps a
        # readable pixel size instead of shrinking with the layout
        self._text_cache = {}
        m = g = int(self.bezel * self.s)  # the bezel covers these margins
        # Nemotron across the top, read first (spec 0: lead with the reasoning);
        # the sensing below it - the trace + disc, and the readings + detections
        top_h = int((self.h - 2 * m - g) * 0.46)  # the conversation needs room to scroll
        bottom_y = m + top_h + g
        bottom_h = self.h - bottom_y - m
        left_w = int((self.w - 2 * m - g) * 0.60)
        self.p_mind = pg.Rect(m, m, self.w - 2 * m, top_h)
        self.p_dynamic = pg.Rect(m, bottom_y, left_w, bottom_h)
        self.p_data = pg.Rect(m + left_w + g, bottom_y, self.w - 2 * m - g - left_w, bottom_h)

    # --- primitives -------------------------------------------------------------
    def text(self, font, s: str, color) -> "pygame.Surface":
        key = (id(font), s, color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 600:
                self._text_cache.clear()
            surf = self._text_cache[key] = font.render(s, True, color)
        return surf

    def blit(self, surface, font, s: str, color, x: int, y: int, anchor: str = "topleft") -> "pygame.Rect":
        img = self.text(font, s, color)
        rect = img.get_rect(**{anchor: (x, y)})
        surface.blit(img, rect)
        return rect

    def fit(self, font, s: str, width: int) -> str:
        """Clip a string to a pixel width with an ellipsis - columns never collide."""
        if font.size(s)[0] <= width:
            return s
        while s and font.size(s + "..")[0] > width:
            s = s[:-1]
        return s.rstrip() + ".."

    def wrap(self, font, s: str, width: int) -> list[str]:
        words, lines, line = s.split(), [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if font.size(trial)[0] <= width:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    def panel(self, surface, rect, title: str, right: str = "") -> None:
        """A framed instrument. Legends sit just INSIDE the frame: the margins
        outside belong to the printed bezel and anything there gets covered."""
        pg, s = self.pg, self.s
        pg.draw.rect(surface, PANEL, rect)
        pg.draw.rect(surface, FRAME, rect, width=max(1, int(2 * s)))
        if title:
            self.blit(surface, self.f_tiny, title, LABEL, rect.x + int(12 * s), rect.y + int(7 * s))
        if right:
            self.blit(surface, self.f_tiny, right, LABEL, rect.right - int(12 * s), rect.y + int(7 * s), "topright")

    # --- the frame --------------------------------------------------------------
    def draw(self, surface, state: dict, now: float, dt: float) -> None:
        if state and not state.get("trace"):
            state = {**state, "trace": list(self.own_trace)}  # older loop: draw from what we've seen
        surface.fill(BG)
        age = now - _epoch(state.get("when")) if state else None
        stale = age is None or age > STALE_AFTER_S
        self.draw_dynamic(surface, state, now, dt, stale)
        self.draw_data(surface, state, stale)
        self.draw_mind(surface, state, now)
        if stale:
            msg = "NO SIGNAL - LIVE LOOP NOT RUNNING" if age is None else f"NO SIGNAL {age:.0f} S - LIVE LOOP NOT UPDATING"
            self.blit(surface, self.f_small, msg, AMBER, self.p_dynamic.right - int(14 * self.s),
                      self.p_dynamic.y + int(7 * self.s), "topright")

    def observe(self, state: dict) -> None:
        """Remember each new reading, so a trace exists even without one in state.json."""
        when, power = state.get("when"), state.get("power_w") or {}
        if not when or when == self._last_when or "total" not in power:
            return
        self._last_when = when
        self.own_trace.append([_epoch(when), power.get("total", 0), power.get("A", 0), power.get("B", 0),
                               power.get("C", 0)])
        cutoff = time.time() - TRACE_MINUTES * 60 - 60
        while self.own_trace and self.own_trace[0][0] < cutoff:
            self.own_trace.pop(0)

    # --- panel 1: DYNAMIC ---------------------------------------------------------
    def draw_dynamic(self, surface, state: dict, now: float, dt: float, stale: bool) -> None:
        pg, s = self.pg, self.s
        rect = self.p_dynamic
        self.panel(surface, rect, "LOAD  kW  LAST 20 MIN")
        pad = int(14 * s)
        disc_w = int(rect.w * 0.26)
        plot = pg.Rect(rect.x + pad + int(40 * s), rect.y + pad + int(20 * s),
                       rect.w - disc_w - 2 * pad - int(56 * s), rect.h - 2 * pad - int(38 * s))

        trace = state.get("trace") or []
        t0 = now - TRACE_MINUTES * 60
        visible = [p for p in trace if p[0] >= t0 - 60]
        peak = max((p[1] for p in visible), default=1000) / 1000
        step = nice_ceiling(max(2.0, peak * 1.15) / 4)  # gridlines on 1/2/5 steps, never 1.25
        ymax = step * 4

        def xy(t: float, w: float) -> tuple[int, int]:
            x = plot.x + (t - t0) / (TRACE_MINUTES * 60) * plot.w
            y = plot.bottom - (w / 1000) / ymax * plot.h
            return int(min(max(x, plot.x), plot.right)), int(y)

        # grid: kW rows, 5-minute columns
        for i in range(5):
            y = plot.bottom - int(i * step / ymax * plot.h)
            pg.draw.line(surface, GRID, (plot.x, y), (plot.right, y))
            self.blit(surface, self.f_tiny, f"{i * step:g}", LABEL, plot.x - int(8 * s), y, "midright")
        for minutes in range(0, TRACE_MINUTES + 1, 5):
            x = plot.x + int(minutes / TRACE_MINUTES * plot.w)
            pg.draw.line(surface, GRID, (x, plot.y), (x, plot.bottom))
            ago = TRACE_MINUTES - minutes
            self.blit(surface, self.f_tiny, "NOW" if ago == 0 else f"-{ago}m", LABEL, x,
                      plot.bottom + int(6 * s), "midtop")

        # base load reference (dashed): everything normally on
        base = (state.get("base_load_w") or {}).get("total")
        if base:
            y = xy(now, base)[1]
            for x in range(plot.x, plot.right, int(10 * s) or 10):
                pg.draw.line(surface, FRAME, (x, y), (min(x + int(5 * s), plot.right), y))
            self.blit(surface, self.f_tiny, "BASE", LABEL, plot.x + int(4 * s), y - int(2 * s), "bottomleft")

        # step-drawn lines: a reading holds until the next one (HA records changes)
        hold_until = now if not stale else (visible[-1][0] if visible else now)

        def steps(idx: int) -> list[tuple[int, int]]:
            pts = []
            for a, b in zip(visible, visible[1:]):
                pts.append(xy(a[0], a[idx]))
                pts.append(xy(b[0], a[idx]))
            if visible:
                pts.append(xy(visible[-1][0], visible[-1][idx]))
                pts.append(xy(hold_until, visible[-1][idx]))
            return pts

        labels = []  # (y, text, colour) - placed after, pushed apart so they never collide
        for idx, phase in ((2, "A"), (3, "B"), (4, "C")):
            pts = steps(idx)
            if len(pts) > 1:
                pg.draw.lines(surface, PHASE[phase], False, pts, 1)
                labels.append([pts[-1][1], phase, PHASE[phase]])
        pts = steps(1)
        if len(pts) > 1:
            pg.draw.lines(surface, GREEN, False, pts, max(2, int(2 * s)))
            labels.append([pts[-1][1], "TOTAL", GREEN])
        self.draw_callouts(surface, state, plot, t0, now, xy, visible)
        labels.sort(key=lambda item: item[0])
        gap = self.f_tiny.get_height()
        for prev, item in zip(labels, labels[1:]):
            item[0] = max(item[0], prev[0] + gap)
        for y, text, colour in labels:
            self.blit(surface, self.f_tiny, text, colour, plot.right + int(5 * s), y, "midleft")

        # the Ferraris disc: speed IS the reading (spec 9)
        total = (state.get("power_w") or {}).get("total") or 0.0
        target = 0.0 if stale else total / 1000 * DISC_REV_PER_KW
        self.disc_speed += (target - self.disc_speed) * min(1.0, dt / 1.2)
        self.disc_angle = (self.disc_angle + self.disc_speed * dt * 2 * math.pi) % (2 * math.pi)
        cx = rect.right - disc_w // 2 - pad
        cy = rect.y + rect.h // 2 - int(16 * s)
        r = int(min(disc_w * 0.42, rect.h * 0.36))
        self.draw_ferraris(surface, cx, cy, r, stale)
        kw = f"{total / 1000:.2f} kW" if not stale else "-- kW"
        self.blit(surface, self.f_big, kw, AMBER if stale else GREEN, cx, cy + int(r * 0.55), "midtop")

    def draw_callouts(self, surface, state: dict, plot, t0: float, now: float, xy, visible: list) -> None:
        """Tie the trace to the detection table: the same glyph, on the step itself.

        Each detected session gets its glyph box just above the total line where
        it switched on, a thin leader down to the step, and a bracket running to
        where it ended (or to now). Rhythms get a lane of tick marks along the top
        of the plot, one tick per pulse, tagged "~" + phase like their table row.
        """
        pg, s = self.pg, self.s
        box_w = int(14 * s)

        def total_at(t: float) -> float:
            value = visible[0][1] if visible else 0.0
            for point in visible:
                if point[0] > t:
                    break
                value = point[1]
            return value

        sessions = [(load, False) for load in state.get("loads") or []] + \
                   [(load, True) for load in state.get("recent") or []]
        lanes: list[int] = []  # right edge of the last box in each vertical lane
        for load, finished in sorted(sessions, key=lambda item: _epoch(item[0].get("since"))):
            start = _epoch(load.get("since"))
            end = _epoch(load.get("ended")) if finished else now
            if not start or end < t0:
                continue
            x0 = xy(max(start, t0), 0)[0]
            x1 = xy(min(end, now), 0)[0]
            lane = next((i for i, right in enumerate(lanes) if right < x0 - int(4 * s)), len(lanes))
            if lane == len(lanes):
                lanes.append(0)
            lanes[lane] = x0 + box_w
            y_line = xy(start, total_at(start + 1))[1]
            y_box = max(plot.y + int(22 * s), y_line - int(26 * s) - lane * int(20 * s))
            ink = TEXT_DIM if finished or load.get("base_part_off") else (
                GREEN if load.get("state") == "MATCHED" else GREEN_MID)
            box = pg.Rect(x0 - box_w // 2, y_box, box_w, box_w)
            pg.draw.line(surface, ink, (x0, box.bottom), (x0, y_line), 1)  # leader to the step
            if x1 > x0 + box_w:
                pg.draw.line(surface, ink, (box.right, box.centery), (x1, box.centery), 1)  # how long it ran
                pg.draw.line(surface, ink, (x1, box.centery - int(3 * s)), (x1, box.centery + int(3 * s)), 1)
            pg.draw.rect(surface, PANEL, box)
            pg.draw.rect(surface, ink, box, 1)
            self.blit(surface, self.f_tiny, glyph_for(load), ink, box.centerx, box.centery, "center")

        # rhythm lanes along the top of the plot
        for i, rhythm in enumerate((state.get("rhythms") or [])[:3]):
            y = plot.y + int(6 * s) + i * int(12 * s)
            ink = GREEN_MID if rhythm.get("name") else TEXT_DIM
            sign = "-" if rhythm.get("kind") == "briefly_off" else "+"  # matches the table's -35 W / +20 W
            self.blit(surface, self.f_tiny, f"~{rhythm.get('phase')}{sign}", ink, plot.x + int(4 * s), y, "midleft")
            for t in rhythm.get("recent_pulses") or []:
                if t0 <= t <= now:
                    x = xy(t, 0)[0]
                    pg.draw.line(surface, ink, (x, y - int(3 * s)), (x, y + int(3 * s)), 1)

    def draw_ferraris(self, surface, cx: int, cy: int, r: int, stale: bool) -> None:
        """The disc of an old kWh meter, seen edge-on through the meter's window.

        Graduation ticks slide along the disc's front rim and one marker comes
        round once per turn - the speed is the reading. Only the front half of
        the rim is visible, as on the real thing; the ticks bunch up toward the
        edges of the window because the disc is round.
        """
        pg, s = self.pg, self.s
        rx, ry = r, max(4, int(r * 0.2))  # perspective: a flat disc seen almost edge-on
        rim = max(3, int(r * 0.09))  # the disc's thickness
        window = pg.Rect(0, 0, int(rx * 2.2), int((ry + rim) * 2 + r * 0.35))
        window.center = (cx, cy)
        pg.draw.rect(surface, BG, window)
        pg.draw.rect(surface, FRAME, window, 1)
        self.blit(surface, self.f_tiny, "kWh", LABEL, window.x + int(6 * s), window.y + int(4 * s))
        # the spindle through the disc
        pg.draw.line(surface, GRID, (cx, window.y + int(4 * s)), (cx, window.bottom - int(4 * s)), 1)
        # the disc: top face outline, then the lit front rim band
        top = pg.Rect(cx - rx, cy - ry, 2 * rx, 2 * ry)
        pg.draw.ellipse(surface, PANEL, top)
        pg.draw.ellipse(surface, FRAME, top, 1)
        front = [(cx + rx * math.cos(a / 40 * math.pi), cy + ry * math.sin(a / 40 * math.pi)) for a in range(0, 41)]
        pg.draw.lines(surface, FRAME, False, [(x, y + rim) for x, y in front], 1)
        pg.draw.line(surface, FRAME, (cx - rx, cy), (cx - rx, cy + rim))
        pg.draw.line(surface, FRAME, (cx + rx, cy), (cx + rx, cy + rim))
        # graduations on the rim; the one marker is the bright one
        ticks = 48
        tick_ink = LABEL if not stale else AMBER_DIM
        for i in range(ticks):
            a = self.disc_angle + i * 2 * math.pi / ticks
            if math.sin(a) <= 0.05:  # the back half is hidden behind the disc
                continue
            x = cx + rx * math.cos(a)
            y = cy + ry * math.sin(a)
            if i == 0:
                pg.draw.line(surface, AMBER if stale else GREEN, (x, y - 1), (x, y + rim + 1), max(3, int(3 * s)))
            else:
                pg.draw.line(surface, tick_ink, (x, y + 1), (x, y + rim - 1), 1)

    # --- panel 2: DATA ------------------------------------------------------------
    def draw_data(self, surface, state: dict, stale: bool) -> None:
        pg, s = self.pg, self.s
        rect = self.p_data
        self.panel(surface, rect, "READINGS  +  DETECTION")
        x, y = rect.x + int(16 * s), rect.y + int(28 * s)
        p = state.get("power_w") or {}
        colour = AMBER if stale else GREEN

        self.blit(surface, self.f_tiny, "TOTAL", LABEL, x, y)
        self.blit(surface, self.f_big, f"{p.get('total', 0):,.0f} W".replace(",", " "), colour, x, y + int(14 * s))
        base = (state.get("base_load_w") or {}).get("total")
        if base:
            self.blit(surface, self.f_small, f"BASE {base:,.0f} W".replace(",", " "), TEXT_DIM,
                      rect.right - int(16 * s), y + int(4 * s), "topright")

        # phases: colour swatch carries identity, the text stays in text ink
        y += int(58 * s)
        col = (rect.w - int(32 * s)) // 3
        for i, ph in enumerate("ABC"):
            px = x + i * col
            pg.draw.rect(surface, PHASE[ph], (px, y + int(5 * s), int(8 * s), int(8 * s)))
            self.blit(surface, self.f_small, f"{ph} {p.get(ph, 0):,.0f} W".replace(",", " "), GREEN_MID,
                      px + int(14 * s), y)

        # price + tier: amber only when it costs (spec 9 palette discipline)
        y += int(30 * s)
        price = state.get("price") or {}
        c = price.get("c_kwh")
        tier = (price.get("tier") or "").upper()
        demo = bool(price.get("demo_override"))
        label = (f"PRICE {c:.2f} c/kWh" if c is not None else "PRICE --") + ("  DEMO PRICE" if demo else "")
        self.blit(surface, self.f_small, label, AMBER if demo else GREEN_MID, x, y)
        if tier:
            self.blit(surface, self.f_small, tier, AMBER if tier in ("EXPENSIVE", "PEAK") else GREEN,
                      rect.right - int(16 * s), y, "topright")
        ctx = state.get("context") or {}
        y += int(22 * s)
        self.blit(surface, self.f_tiny, str(ctx.get("part_of_day", "")).upper(), LABEL, x, y)

        # detection table: what is on, how sure, how big, how long, what it has cost
        y += int(24 * s)
        right = rect.right - int(16 * s)
        # right-aligned column edges: SURE | W | ON | COST
        cols = {"SURE": right - int(230 * s), "W": right - int(160 * s), "ON": right - int(80 * s), "COST": right}
        self.blit(surface, self.f_tiny, "LOAD", LABEL, x, y)
        for name, cx in cols.items():
            self.blit(surface, self.f_tiny, name, LABEL, cx, y, "topright")
        y += self.f_tiny.get_linesize() + 1
        pg.draw.line(surface, GRID, (x, y), (right, y))
        y += int(6 * s)
        if state.get("ev_active"):
            self.blit(surface, self.f_tiny, "EV CHARGING - OTHER DETECTIONS UNRELIABLE", AMBER, x, y)
            y += int(18 * s)
        loads = state.get("loads") or []
        recent = state.get("recent") or []
        row_h = max(int(22 * s), self.f_small.get_linesize() + 3)
        head_h = int(4 * s) + self.f_tiny.get_linesize() + 1  # a section label (RHYTHMS, ENDED)
        limit = rect.bottom - int(8 * s) - self.f_tiny.get_linesize() - 2  # the footer note sits below
        rows_fit = max(1, (limit - y) // row_h)
        if not loads:
            self.blit(surface, self.f_small, "NOTHING ABOVE THE BASE LOAD", TEXT_DIM, x, y)
            y += row_h
            rows_fit -= 1
        for load in loads[:rows_fit]:
            self.draw_load_row(surface, load, x, y, cols, finished=False)
            y += row_h
        rows_fit -= len(loads[:rows_fit])
        # what ended (and what it cost) comes next; the rhythms sit at the bottom and
        # give up their rows as soon as there is something more interesting to show
        if recent and y + head_h + row_h <= limit:
            y += int(4 * s)
            self.blit(surface, self.f_tiny, "ENDED", LABEL, x, y)
            y += head_h - int(4 * s)
            for session in recent:
                if y + row_h > limit:
                    break
                self.draw_load_row(surface, session, x, y, cols, finished=True)
                y += row_h
        rhythms = state.get("rhythms") or []
        fits = min(len(rhythms), max(0, (limit - y - head_h) // row_h))
        if fits:
            ry = limit - fits * row_h - head_h + int(4 * s)  # anchored to the bottom, above the footer
            self.blit(surface, self.f_tiny, "RHYTHMS  -  SMALL LOADS FOUND BY THEIR PATTERN", LABEL, x, ry)
            ry += head_h - int(4 * s)
            for r in rhythms[:fits]:
                self.draw_rhythm_row(surface, r, x, ry, right)
                ry += row_h
        note = self.fit(self.f_tiny, "SURE = HOW WELL SIZE, PHASE, TIME FIT THE PROFILE", right - x)
        self.blit(surface, self.f_tiny, note, LABEL, x, rect.bottom - int(8 * s), "bottomleft")

    def draw_rhythm_row(self, surface, r: dict, x: int, y: int, right: int) -> None:
        """A load known by its rhythm: phase, size, how long each pulse, how often."""
        pg, s = self.pg, self.s
        ink = GREEN_MID if r.get("name") else TEXT_DIM
        box = pg.Rect(x, y + int(1 * s), int(16 * s), int(16 * s))
        pg.draw.rect(surface, ink, box, 1)
        self.blit(surface, self.f_tiny, "~", ink, box.centerx, box.centery, "center")
        off = r.get("kind") == "briefly_off"
        name = str(r.get("name") or f"{'DIPS' if off else 'PULSES'} ON {r.get('phase')}").upper()
        every = fmt_duration((r.get("every_s") or 0) / 60) if r.get("every_s") else "-"
        pulse = r.get("pulse_s") or 0
        pulse_txt = f"{pulse:.0f}S" if pulse < 90 else fmt_duration(pulse / 60)
        # compact: the Pi's wider font must still leave room for the name
        detail = f"{'-' if off else '+'}{r.get('amplitude_w')}W {pulse_txt}  EVERY {every}  {r.get('per_hour'):.0f}/H"
        detail_img = self.blit(surface, self.f_small, detail, ink, right, y, "topright")
        room = detail_img.x - (x + int(24 * s)) - int(10 * s)
        self.blit(surface, self.f_small, self.fit(self.f_small, name, room), ink, x + int(24 * s), y)

    def draw_load_row(self, surface, load: dict, x: int, y: int, cols: dict, *, finished: bool) -> None:
        pg, s = self.pg, self.s
        conf = load.get("confidence") or 0.0
        matched = load.get("state") == "MATCHED"
        unsure = matched and (conf < 0.6 or load.get("unusual"))
        if finished or load.get("base_part_off"):
            ink = TEXT_DIM
        elif not matched:
            ink = GREEN_MID
        else:
            ink = TEXT_DIM if (unsure or load.get("unreliable")) else GREEN
        box = pg.Rect(x, y + int(1 * s), int(16 * s), int(16 * s))
        pg.draw.rect(surface, ink, box, 1)
        self.blit(surface, self.f_tiny, glyph_for(load), ink, box.centerx, box.centery, "center")
        name = row_name(load)
        if finished:
            name = f"{str(load.get('ended', ''))[11:16]} {name}"  # when it ended, then what
        room = cols["SURE"] - self.f_small.size("100%")[0] - int(12 * s) - (x + int(24 * s))
        self.blit(surface, self.f_small, self.fit(self.f_small, name, room), ink, x + int(24 * s), y)
        sure = f"{conf:.0%}{'?' if unsure else ''}" if matched and not load.get("base_part_off") else "-"
        watts = load.get("watts")
        values = {
            "SURE": sure,
            "W": "-" if watts is None else f"{'-' if load.get('base_part_off') else ''}{watts:,.0f}".replace(",", " "),
            "ON": fmt_duration(load.get("minutes")),
            "COST": fmt_cost(load.get("cost_eur"), saving=bool(load.get("saving"))),
        }
        for name, cx in cols.items():
            self.blit(surface, self.f_small, values[name], ink, cx, y, "topright")

    # --- panel 3: ADVICE ----------------------------------------------------------
    def draw_mind(self, surface, state: dict, now: float) -> None:
        """Panel 1, read first: what Nemotron concluded, WHY, and the evidence.

        Spec 0: lead with the reasoning, not the detection. The verdict is the
        biggest type on the screen; its one-sentence reason sits under it (the
        line a detector can never produce); the real price curve it reasoned
        over sits beside it; last night's insight runs along the bottom. The
        header shows the reasoning itself - when it last reasoned and at what
        cost in tokens, or that it is reasoning right now and about what.
        Static text only: a call in flight is information, not an animation.
        """
        pg, s = self.pg, self.s
        rect = self.p_mind
        advice = state.get("advice") or {}
        reasoning = state.get("reasoning") or {}
        gate = (state.get("gate") or {}).get("status", "")
        calls = ""
        if gate:  # "calls: 5/20 this hour | 14/150 today | ..." -> "CLOUD 5/20 H  14/150 D"
            bits = [b.strip() for b in gate.split("|")]
            try:
                calls = f"CLOUD {bits[0].split()[1]} H  {bits[1].split()[0]} D"
            except IndexError:
                calls = ""
        self.panel(surface, rect, "", calls)
        pad = int(16 * s)
        x = rect.x + pad

        # header: who is thinking, and what it is doing
        name = self.blit(surface, self.f_small_bold, "NEMOTRON", GREEN, x, rect.y + int(6 * s))
        self.blit(surface, self.f_tiny, "  REASONING ON NEBIUS TOKEN FACTORY" + ("  -  DEMO" if reasoning.get("demo") else ""),
                  LABEL, name.right, name.centery, "midleft")
        if reasoning.get("in_flight"):
            since = str(reasoning.get("since", ""))[11:16]
            why = str(reasoning.get("why", "")).split(":")[0]
            status, ink = f"REASONING NOW  -  SINCE {since}  -  {why}".upper(), GREEN
        elif advice.get("at"):
            tokens = advice.get("tokens")
            tok = f"  -  {tokens:,} TOKENS".replace(",", " ") if isinstance(tokens, int) else ""
            status, ink = f"LAST REASONED {str(advice['at'])[11:16]}{tok}", LABEL
        else:
            status, ink = "NOT YET ASKED", LABEL
        right_edge = rect.right - pad - (self.f_tiny.size(calls)[0] + int(24 * s) if calls else 0)
        status_rect = self.blit(surface, self.f_tiny, status, ink, right_edge, name.centery, "midright")
        # the Virta lamp: its colour here matches the real lamp in the room
        lamp = state.get("lamp") or {}
        if lamp.get("mood"):
            rgb, word = LAMP_MOODS.get(lamp["mood"], (LABEL, lamp["mood"]))
            text = self.blit(surface, self.f_tiny, f"LAMP {word}", LABEL, status_rect.x - int(18 * s),
                             name.centery, "midright")
            pg.draw.circle(surface, rgb, (text.x - int(9 * s), name.centery), max(3, int(5 * s)))

        # left: the conversation - what happened, what Virta said, what it did.
        # Newest at the bottom; older lines fade and scroll out of the top.
        split = rect.x + int(rect.w * 0.62)
        width = split - x - pad
        top = rect.y + int(30 * s)
        bottom = rect.bottom - int(52 * s) - int(14 * s)  # above LAST NIGHT

        # ACT: a proposal waiting for a human (amber - the one alert on screen), pinned at the bottom
        proposal = state.get("proposal")
        if proposal:
            name = str(proposal.get("name", "")).upper()
            swap = f" -> {str(proposal.get('swap_name')).upper()} ON" if proposal.get("swap_name") else ""
            if proposal.get("policy") == "auto":
                left = max(0, int(_epoch(proposal.get("act_at")) - now))
                msg = f"SWITCHING {name} OFF{swap} IN {left} S   [N] CANCEL"
            else:
                msg = f"PROPOSED: TURN OFF {name}{swap}   [Y] YES   [N] NO"
            r = self.blit(surface, self.f_small_bold, self.fit(self.f_small_bold, msg, width), AMBER, x,
                          bottom, "bottomleft")
            bottom = r.y - int(6 * s)

        chat = list(state.get("chat") or [])
        if not chat and state.get("verdict"):  # an older loop without a chat log
            chat = [{"at": (state.get("advice") or {}).get("at") or "", "kind": "virta", "text": state["verdict"],
                     "why": str(((state.get("advice") or {}).get("action") or {}).get("reason", ""))}]
        if not chat:
            self.blit(surface, self.f_verdict_big, "AWAITING FIRST WORD", TEXT_DIM, x, top)
        else:
            self.draw_chat(surface, chat, pg.Rect(x, top, width, bottom - top), now)

        # right: the evidence it reasoned over - the real price curve
        strip_x = split + pad
        strip = pg.Rect(strip_x, rect.y + int(52 * s), rect.right - pad - strip_x, int(rect.h * 0.40))
        self.draw_price_strip(surface, strip, state, now)

        # bottom: last night's understanding (the dry voice) + what it rests on
        base_y = rect.bottom - int(52 * s)
        pg.draw.line(surface, GRID, (x, base_y - int(8 * s)), (rect.right - pad, base_y - int(8 * s)))
        commentary = state.get("commentary") or {}
        tag = self.blit(surface, self.f_tiny, "LAST NIGHT", LABEL, x, base_y + int(4 * s))
        line_x = x + int(96 * s)
        if commentary:
            self.blit(surface, self.f_tiny, f"{commentary.get('index')}/{commentary.get('count')}", LABEL,
                      tag.x, tag.bottom + int(3 * s))
            text = self.wrap(self.f_comment, commentary.get("line", ""), rect.right - pad - line_x)
            self.blit(surface, self.f_comment, text[0] if text else "", GREEN_MID, line_x, base_y)
            evidence = str(commentary.get("evidence") or "")
            if evidence:
                width = rect.right - pad - line_x
                ev = self.wrap(self.f_tiny, f"FROM: {evidence}".replace("_", " ").upper(), width)
                if len(ev) > 1:
                    ev = self.wrap(self.f_tiny, " ".join(ev), width - self.f_tiny.size(" ...")[0])
                self.blit(surface, self.f_tiny, ev[0] + (" ..." if len(ev) > 1 else ""), LABEL, line_x,
                          base_y + int(24 * s))
        else:
            self.blit(surface, self.f_comment, "NO INSIGHTS YET - THE NIGHTLY PASS RUNS AT 03:30", TEXT_DIM,
                      line_x, base_y)

    def draw_chat(self, surface, chat: list[dict], area, now: float) -> None:
        """A chat window, bottom-up. Virta's newest line is the big one and types
        itself out; the house's events are small time-stamped lines between; what
        Virta did is amber. New lines push the old ones up and out of the frame."""
        pg, s = self.pg, self.s
        first = self._chat_seen
        for c in chat:  # when did THIS console first see each line (restarts don't re-type old ones)
            first.setdefault(c.get("at", "") + c.get("text", "")[:20], now if self._chat_primed else 0.0)
        self._chat_primed = True
        newest_virta = max((i for i, c in enumerate(chat) if c.get("kind") == "virta"), default=-1)
        clock_w = self.f_tiny.size("00:00  ")[0]
        text_x, text_w = area.x + clock_w, area.w - clock_w

        blocks = []  # (entry, font, lines, colour, height) newest last
        for i, c in enumerate(chat):
            kind, text = c.get("kind"), str(c.get("text", ""))
            if kind == "virta" and i == newest_virta:
                font, ink = self.f_chat, GREEN
            elif kind == "virta":
                font, ink = self.f_comment, GREEN_MID
            elif kind == "act":
                font, ink = self.f_small, AMBER if i >= len(chat) - 3 else AMBER_DIM
            else:
                font, ink = self.f_small, TEXT_DIM
            lines = self.wrap(font, text, text_w)[:3]
            height = len(lines) * font.get_linesize()
            why = str(c.get("why") or "").strip() if i == newest_virta else ""
            why_lines = self.wrap(self.f_tiny, "WHY  " + why.upper(), text_w)[:2] if why else []
            height += len(why_lines) * self.f_tiny.get_linesize() + int(6 * s)
            blocks.append((c, font, lines, ink, why_lines, height))

        # the newest line slides in from below
        key = chat[-1].get("at", "") + chat[-1].get("text", "")[:20]
        age = now - first.get(key, 0.0)
        slide = int(blocks[-1][5] * max(0.0, 1 - age / 0.45)) if blocks else 0

        clip = surface.get_clip()
        surface.set_clip(area)
        y = area.bottom + slide
        for depth, (c, font, lines, ink, why_lines, height) in enumerate(reversed(blocks)):
            y -= height
            if y + height < area.y:
                break
            fade = max(0.3, 1.0 - 0.13 * depth)  # older = dimmer, like ink drying
            colour = tuple(int(PANEL[k] + (ink[k] - PANEL[k]) * fade) for k in range(3))
            stamp = str(c.get("at", ""))[11:16]
            self.blit(surface, self.f_tiny, stamp, LABEL, area.x, y + max(0, font.get_linesize() - self.f_tiny.get_linesize()) // 2)
            seen = first.get(c.get("at", "") + c.get("text", "")[:20], 0.0)
            reveal = int((now - seen) * 45) if c.get("kind") == "virta" else 10_000  # typewriter, 45 chars/s
            ly = y
            for line in lines:
                shown = line[:max(0, reveal)]
                reveal -= len(line) + 1
                if shown:
                    self.blit(surface, font, shown, colour, text_x, ly)
                ly += font.get_linesize()
            if why_lines and reveal > 0:
                for line in why_lines:
                    self.blit(surface, self.f_tiny, line, LABEL, text_x, ly)
                    ly += self.f_tiny.get_linesize()
        surface.set_clip(clip)
        # a soft fade at the top edge, so lines scroll OUT rather than get cut
        fade_h = int(18 * s)
        veil = pg.Surface((area.w, fade_h), pg.SRCALPHA)
        for k in range(fade_h):
            veil.fill((*PANEL, int(255 * (1 - k / fade_h))), pg.Rect(0, k, area.w, 1))
        surface.blit(veil, (area.x, area.y))

    def draw_price_strip(self, surface, strip, state: dict, now: float) -> None:
        """Today's real Nordpool slots (+ tomorrow once published), the current one lit."""
        pg, s = self.pg, self.s
        curve = state.get("price_curve") or []
        bounds = state.get("price_bounds_c_kwh") or {}
        price = state.get("price") or {}
        tier = (price.get("tier") or "").upper()
        self.blit(surface, self.f_tiny, "PRICE c/kWh  -  WHAT IT REASONED OVER", LABEL, strip.x,
                  strip.y - int(18 * s))
        if tier:
            self.blit(surface, self.f_tiny, tier, AMBER if tier in ("EXPENSIVE", "PEAK") else GREEN,
                      strip.right, strip.y - int(18 * s), "topright")
        if not curve:
            self.blit(surface, self.f_small, "NO PRICE CURVE YET", TEXT_DIM, strip.x, strip.y)
            return
        lo = min(0.0, min(v for _, _, v in curve))
        hi = max(1.0, max(v for _, _, v in curve))
        span_t = curve[-1][1] - curve[0][0]
        zero_y = strip.bottom - int((0 - lo) / (hi - lo) * strip.h)
        for start, end, value in curve:
            x0 = strip.x + int((start - curve[0][0]) / span_t * strip.w)
            x1 = strip.x + int((end - curve[0][0]) / span_t * strip.w)
            top = strip.bottom - int((value - lo) / (hi - lo) * strip.h)
            is_now = start <= now < end
            expensive = value >= bounds.get("expensive", 1e9)
            colour = (AMBER if expensive else GREEN) if is_now else (
                AMBER_DIM if expensive else (BAR_PAST if end <= now else BAR))
            bar = pg.Rect(x0, min(top, zero_y), max(1, x1 - x0 - 1), max(1, abs(zero_y - top)))
            pg.draw.rect(surface, colour, bar)
            if is_now:
                self.blit(surface, self.f_tiny, f"{value:.2f}", GREEN, x0, bar.y - int(2 * s), "midbottom")
        self.blit(surface, self.f_tiny, f"MAX {hi:.1f}", LABEL, strip.right, strip.bottom + int(4 * s), "topright")
        for start, _, _ in curve:
            hour = datetime.fromtimestamp(start)
            if hour.minute == 0 and hour.hour % 6 == 0:
                tx = strip.x + int((start - curve[0][0]) / span_t * strip.w)
                self.blit(surface, self.f_tiny, hour.strftime("%H" if hour.hour else "%a").upper(), LABEL,
                          tx, strip.bottom + int(4 * s), "midtop")


def row_name(load: dict) -> str:
    """The load's name for the table - the confidence has its own column."""
    phases = load.get("phases") or ""
    if load.get("base_part_off"):
        who = "BASE PART" if load.get("state") == "UNKNOWN" else str(load.get("name") or load.get("id", "")).upper()
        return f"{who} OFF {phases}".strip()
    if load.get("state") == "UNKNOWN":
        if load.get("guess"):  # Nemotron's guess, waiting for the off-and-on gesture
            return f"{str(load['guess']).upper()}? {phases}".strip()
        return f"UNKNOWN {phases}".strip()
    name = load.get("name") or load.get("id") or ""
    if not load.get("name") and load.get("label"):  # finished sessions carry the label
        name = str(load["label"]).rsplit(" ", 1)[0]
    return str(name).upper()


def fmt_duration(minutes: float | None) -> str:
    if minutes is None:
        return "-"
    if minutes < 60:
        m, sec = divmod(int(round(minutes * 60)), 60)
        return f"{m}:{sec:02d}"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}"


def fmt_cost(eur: float | None, *, saving: bool = False) -> str:
    """Real money so far, at the Nordpool price of every slot the session spanned."""
    if eur is None:
        return "-"
    sign = "-" if saving else ""
    cents = eur * 100
    if cents < 1:
        return f"{sign}{cents:.2f} c"
    if cents < 100:
        return f"{sign}{cents:.1f} c"
    return f"{sign}{eur:.2f} EUR"


def _epoch(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 0.0


def read_state(path: Path, previous: dict) -> dict:
    """The live loop writes atomically, so a read is whole - but keep the last
    good state if the file is missing or momentarily unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fullscreen", action="store_true", help="kiosk mode (the Pi's HDMI screen)")
    parser.add_argument("--size", default="1280x720", help="window size WxH (default 1280x720)")
    parser.add_argument("--fps", type=float, default=20, help="redraw rate (the Pi3 is fine at 15-20)")
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--snapshot", help="render one frame to this PNG and exit (no window)")
    parser.add_argument("--bezel", type=int, default=40,
                        help="margin between and around panels, in 1280x720 design pixels (default 40)")
    args = parser.parse_args(argv)

    if args.snapshot:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    pygame.init()
    pygame.font.init()
    w, h = (int(v) for v in args.size.lower().split("x"))
    state_path = Path(args.state)
    state = read_state(state_path, {})

    if args.snapshot:
        surface = pygame.Surface((w, h))
        renderer = ConsoleRenderer((w, h), bezel=args.bezel)
        renderer.draw(surface, state, time.time(), 0.0)
        pygame.image.save(surface, args.snapshot)
        print(f"snapshot -> {args.snapshot} ({w}x{h}), state from {state.get('when', 'nowhere')}")
        return 0

    if args.fullscreen:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    else:
        screen = pygame.display.set_mode((w, h), pygame.RESIZABLE)
    pygame.display.set_caption("Virta")
    renderer = ConsoleRenderer(screen.get_size(), bezel=args.bezel)
    clock = pygame.time.Clock()
    last_read = 0.0
    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q)):
                    return 0
                if event.type == pygame.KEYDOWN and event.key in (pygame.K_y, pygame.K_n) and state.get("proposal"):
                    # the human's answer; the live loop acts on it (the console itself never calls HA)
                    answer = {"id": state["proposal"]["id"], "answer": "yes" if event.key == pygame.K_y else "no",
                              "at": datetime.now().astimezone().isoformat(timespec="seconds")}
                    Path("var/answer.json").write_text(json.dumps(answer), encoding="utf-8")
                if event.type == pygame.VIDEORESIZE and not args.fullscreen:
                    screen = pygame.display.set_mode(event.size, pygame.RESIZABLE)
                    renderer.resize(screen.get_size())
            now = time.time()
            if now - last_read >= 1.0:  # the loop writes every ~15 s; 1 Hz is plenty
                state = read_state(state_path, state)
                renderer.observe(state)
                last_read = now
            dt = clock.tick(args.fps) / 1000
            renderer.draw(screen, state, now, dt)
            pygame.display.flip()
    finally:
        pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
