"""The architecture card for the video (section 3 of devpost/VIDEO-PLAN.md).

    python devpost/architecture_card.py

Writes devpost/architecture-card.png (1920x1080, the video frame) and
devpost/architecture-card-3x2.png (1800x1200, padded, for the Devpost gallery).

It draws with pygame and the console's own palette and font stack, so the card
cuts against console footage without looking like a different project. Amber is
used once, for the one claim that is the point of the whole design.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The console's palette (virta/console.py). Kept in sync by hand - this file is a
# poster, not a second renderer, and is not imported by anything.
BG = (6, 12, 8)
PANEL = (9, 18, 12)
FRAME = (32, 66, 44)
GRID = (20, 40, 27)
LABEL = (78, 120, 90)
TEXT_DIM = (96, 150, 110)
GREEN = (120, 240, 150)
GREEN_MID = (70, 190, 110)
AMBER = (232, 165, 50)

FACE = "consolas,dejavusansmono,liberationmono,couriernew,monospace"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", default="1920x1080", help="design size (default 1920x1080)")
    ap.add_argument("--out", default="devpost/architecture-card.png")
    ap.add_argument("--no-3x2", action="store_true", help="skip the padded gallery copy")
    args = ap.parse_args(argv)

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame as pg

    pg.init()
    pg.font.init()
    w, h = (int(v) for v in args.size.lower().split("x"))
    s = w / 1920.0

    def px(v: float) -> int:
        return int(round(v * s))

    f_huge = pg.font.SysFont(FACE, px(58), bold=True)
    f_box = pg.font.SysFont(FACE, px(30), bold=True)
    f_body = pg.font.SysFont(FACE, px(21))
    f_small = pg.font.SysFont(FACE, px(18))
    f_label = pg.font.SysFont(FACE, px(17), bold=True)
    f_foot = pg.font.SysFont(FACE, px(20), bold=True)

    surface = pg.Surface((w, h))
    surface.fill(BG)

    def text(font, string, colour, x, y, anchor="topleft"):
        img = font.render(string, True, colour)
        rect = img.get_rect()
        setattr(rect, anchor, (x, y))
        surface.blit(img, rect)
        return rect

    def box(rect, title, lines, foot=None, accent=GREEN):
        pg.draw.rect(surface, PANEL, rect)
        pg.draw.rect(surface, FRAME, rect, max(1, px(2)))
        pad = px(20)
        y = rect.y + pad
        text(f_box, title, accent, rect.x + pad, y)
        y += px(42)
        pg.draw.line(surface, GRID, (rect.x + pad, y), (rect.right - pad, y))
        y += px(16)
        for line, colour in lines:
            text(f_body, line, colour, rect.x + pad, y)
            y += px(30)
        if foot:
            text(f_label, foot, LABEL, rect.x + pad, rect.bottom - pad - px(17))

    def arrow_h(x1, x2, y, label=None, colour=GREEN_MID, label_colour=LABEL):
        head = px(13)
        pg.draw.line(surface, colour, (x1, y), (x2 - head, y), max(2, px(3)))
        pg.draw.polygon(surface, colour,
                        [(x2, y), (x2 - head, y - head // 2), (x2 - head, y + head // 2)])
        if label:
            text(f_label, label, label_colour, (x1 + x2) // 2, y - px(12), "midbottom")

    def arrow_v(x, y1, y2, label=None, colour=GREEN_MID, label_colour=LABEL):
        """y1 -> y2, either direction; the head lands on y2."""
        head = px(13) if y2 > y1 else -px(13)
        pg.draw.line(surface, colour, (x, y1), (x, y2 - head), max(2, px(3)))
        pg.draw.polygon(surface, colour,
                        [(x, y2), (x - abs(head) // 2, y2 - head), (x + abs(head) // 2, y2 - head)])
        if label:
            text(f_label, label, label_colour, x + px(16), (y1 + y2) // 2, "midleft")

    # --- heading ----------------------------------------------------------------
    m = px(70)
    text(f_huge, "VIRTA", GREEN, m, px(52))
    text(f_label, "CHEAP SENSOR, EXPENSIVE BRAIN", LABEL, m + px(4), px(126))
    text(f_label, "ONE METER  ·  ONE OLD PI  ·  NEMOTRON ON NEBIUS TOKEN FACTORY",
         LABEL, w - m, px(126), "topright")

    # --- row: meter -> pi -> cloud ----------------------------------------------
    top = px(210)
    meter = pg.Rect(m, top, px(330), px(250))
    pi = pg.Rect(px(520), top, px(530), px(360))
    cloud = pg.Rect(px(1250), top, px(600), px(250))

    box(meter, "SHELLY 3EM", [
        ("~60 EUR", GREEN_MID),
        ("one clamp meter,", TEXT_DIM),
        ("on the main supply", TEXT_DIM),
        ("3 phases, one number", TEXT_DIM),
    ], foot="THE GROUND TRUTH")

    box(pi, "RASPBERRY PI 3", [
        ("baseline   quiet-stretch base load", TEXT_DIM),
        ("detector   stacking step detection", TEXT_DIM),
        ("rhythms    loads too small to step", TEXT_DIM),
        ("scoring    time, darkness, pairs", TEXT_DIM),
        ("prices     Nordpool, slot by slot", TEXT_DIM),
        ("", TEXT_DIM),
        ("teach      a flick of the switch", GREEN_MID),
    ], foot="EVERYTHING NUMERIC. NO CLOUD. FREE.")

    box(cloud, "NEBIUS TOKEN FACTORY", [
        ("NEMOTRON 3 SUPER", GREEN),
        ("  the live line, naming loads", TEXT_DIM),
        ("NEMOTRON 3 ULTRA", GREEN),
        ("  the nightly pass, once", TEXT_DIM),
    ], foot="28 CALLS A DAY, NOT 5 760")

    arrow_h(meter.right + px(24), pi.x - px(10), top + px(110), "15 s")
    arrow_h(pi.right + px(24), cloud.x - px(10), top + px(110), "CONCLUSIONS")

    # --- bottom row: what stays, and what goes ----------------------------------
    archive = pg.Rect(pi.x, px(690), pi.w, px(250))
    box(archive, "data/archive/", [
        ("every settled day, frozen", TEXT_DIM),
        ("400 KB a day, kept forever", TEXT_DIM),
        ("past HA's 14-day purge", TEXT_DIM),
    ], foot="SO SLOW CHANGES STAY VISIBLE", accent=GREEN_MID)

    payload = pg.Rect(cloud.x, px(690), cloud.w, px(250))
    box(payload, "WHAT ACTUALLY LEAVES", [
        ("running loads + confidence", TEXT_DIM),
        ("the Nordpool price curve", TEXT_DIM),
        ("temperatures, darkness, time", TEXT_DIM),
        ("a digest you can open", TEXT_DIM),
    ], foot="NEVER A WATT-BY-WATT SERIES", accent=GREEN_MID)

    arrow_v(pi.centerx, pi.bottom + px(10), archive.y - px(10), "RAW POWER STAYS HERE", AMBER, AMBER)
    arrow_v(payload.centerx, payload.y - px(10), cloud.bottom + px(10))

    # --- the line the card exists for -------------------------------------------
    rule_y = h - px(118)
    pg.draw.line(surface, GRID, (m, rule_y), (w - m, rule_y))
    text(f_foot, "THE MODEL NEVER SEES THE POWER STREAM.", AMBER, m, rule_y + px(26))
    text(f_small, "The 15-second loop runs on the Pi, for nothing. 48 calls built the whole thing.",
         TEXT_DIM, m, rule_y + px(58))
    text(f_label, "github.com/apn201/Virta   ·   MIT", LABEL, w - m, rule_y + px(34), "topright")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pg.image.save(surface, str(out))
    print(f"card -> {out} ({w}x{h})")

    if not args.no_3x2:
        gw, gh = px(1800), px(1200)
        gallery = pg.Surface((gw, gh))
        gallery.fill(BG)
        scaled = pg.transform.smoothscale(surface, (gw, int(gw * h / w)))
        gallery.blit(scaled, (0, (gh - scaled.get_height()) // 2))
        g_out = out.with_name(out.stem + "-3x2" + out.suffix)
        pg.image.save(gallery, str(g_out))
        print(f"card -> {g_out} ({gw}x{gh}, padded 3:2 for the Devpost gallery)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
