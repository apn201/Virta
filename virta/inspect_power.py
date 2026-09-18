"""Slice 3: load real power data and plot it to eyeball signatures (spec 12).

Run (live HA history, the default):
    python -m virta.inspect_power
    python -m virta.inspect_power --days 14
Run against an export instead:
    python -m virta.inspect_power --csv csv/3EM_Power_Raw_History.csv

Read-only against HA; no cloud calls. PC-only (needs matplotlib from
requirements-dev.txt) - this is offline signature work, not Pi runtime.

Outputs:
  data/power_history.csv  - the pulled history, cached in export format
  var/plots/power_<date>.png - one figure per day: total + per-phase, EV shaded
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ConfigError, load_ha_config
from .ev_signature import UNRELIABLE_NOTE, Baseline, EVWindow, find_ev_windows, rough_baseline
from .ha_client import HAClient, HAError
from .history import (
    Frame,
    LoadReport,
    Series,
    align_phases,
    fetch_history,
    interval_stats,
    load_csv,
    save_csv,
    span,
)

# Validated categorical slots 1-3 (dataviz reference palette, light mode,
# all-pairs PASS). Aqua is below 3:1 on the surface, so every phase line is
# also direct-labelled - identity is never colour alone.
PHASE_COLOURS = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"
EV_BAND = "#d9d8d3"  # neutral state band, deliberately not a status colour


def _report(series: Series, report: LoadReport, source: str) -> None:
    print(f"\nSOURCE  {source}")
    print(report.describe())
    print("\nSAMPLE SPACING (real, irregular - spec 3.1)")
    for entity_id, samples in sorted(series.items()):
        stats = interval_stats(samples)
        if not stats:
            continue
        print(
            f"  {entity_id:32s} span {str(span(samples)).split('.')[0]:>16s}   "
            f"p05 {stats['p05']:5.1f}s  median {stats['median']:5.1f}s  "
            f"p95 {stats['p95']:5.1f}s  max {stats['max']:6.0f}s"
        )


def _plot_day(day: date, frames: list[Frame], windows: list[EVWindow], base: Baseline, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    tz = frames[0].when.tzinfo
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    fig, (ax_total, ax_phase) = plt.subplots(
        2, 1, figsize=(15, 7.5), sharex=True, gridspec_kw={"height_ratios": [1, 1]}
    )
    fig.patch.set_facecolor(SURFACE)

    times = [f.when for f in frames]

    # Step drawing is the honest shape: HA records CHANGES, so a value holds
    # until the next row. A straight line between samples would invent ramps.
    ax_total.step(times, [f.total / 1000 for f in frames], where="post", color=INK, linewidth=1.0)
    ax_total.axhline(base.total / 1000, color=INK_SECONDARY, linewidth=0.8, linestyle=(0, (4, 3)))
    ax_total.text(
        day_start + timedelta(minutes=10), base.total / 1000, f" rough floor {base.total:.0f} W",
        va="bottom", ha="left", fontsize=8, color=INK_SECONDARY,
    )
    ax_total.set_ylabel("Total (kW)", color=INK_SECONDARY)

    for phase, attr in (("A", "a"), ("B", "b"), ("C", "c")):
        pts = [(f.when, getattr(f, attr) / 1000) for f in frames if getattr(f, attr) is not None]
        if not pts:
            continue
        ax_phase.step([p[0] for p in pts], [p[1] for p in pts], where="post",
                      color=PHASE_COLOURS[phase], linewidth=1.0, label=f"Phase {phase}")
        # direct label at the right edge, in text ink next to a coloured mark
        last_t, last_v = pts[-1]
        ax_phase.annotate(
            f"  {phase}", xy=(min(last_t, day_end), last_v), fontsize=9, color=INK,
            va="center", ha="left", annotation_clip=False,
        )
    ax_phase.set_ylabel("Per phase (kW)", color=INK_SECONDARY)
    ax_phase.legend(loc="upper left", frameon=False, fontsize=8, ncol=3)

    for window in windows:
        if window.end < day_start or window.start > day_end:
            continue
        for ax in (ax_total, ax_phase):
            ax.axvspan(max(window.start, day_start), min(window.end, day_end),
                       color=EV_BAND, alpha=0.55, linewidth=0, zorder=0)
        ax_total.text(
            max(window.start, day_start) + timedelta(minutes=4), ax_total.get_ylim()[1] * 0.97,
            "EV · load control\nothers unreliable", va="top", ha="left", fontsize=8, color=INK_SECONDARY,
        )

    for ax in (ax_total, ax_phase):
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=INK_SECONDARY, labelsize=8)
        ax.set_ylim(bottom=0)
    ax_phase.set_xlim(day_start, day_end)
    ax_phase.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=tz))
    ax_phase.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))

    covered = f"{frames[0].when:%H:%M}–{frames[-1].when:%H:%M}"
    fig.suptitle(f"House power, {day:%a %d.%m.%Y}   (data {covered}, step-drawn as recorded)",
                 x=0.06, ha="left", fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0, 0.98, 0.96))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=float, default=14, help="history to request from HA (default 14 = recorder window)")
    parser.add_argument("--csv", help="read a states export instead of HA")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args(argv)

    try:
        ha_config = load_ha_config(require_token=not args.csv)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    total_id = ha_config.entity("SENSOR_TOTAL")
    phase_ids = (ha_config.entity("SENSOR_PHASE_A"), ha_config.entity("SENSOR_PHASE_B"),
                 ha_config.entity("SENSOR_PHASE_C"))

    if args.csv:
        series, report = load_csv(args.csv, ha_config.power_entities)
        source = args.csv
    else:
        end = datetime.now().astimezone()
        start = end - timedelta(days=args.days)
        try:
            series, report = fetch_history(HAClient(ha_config, timeout_s=120), ha_config.power_entities, start, end)
        except HAError as exc:
            print(f"HA ERROR\n{exc}", file=sys.stderr)
            return 1
        source = f"HA history, requested {args.days:g} days (HA returns what the recorder holds)"
        save_csv(series, "data/power_history.csv")

    if total_id not in series:
        print(f"No usable samples for {total_id} - nothing to inspect.", file=sys.stderr)
        return 1

    _report(series, report, source)

    frames = align_phases(series, total_id, phase_ids)
    raw_total = len(series[total_id])
    spacing = interval_stats([(f.when, f.total) for f in frames])
    stale = [f for f in frames if None not in (f.age_a, f.age_b, f.age_c) and max(f.age_a, f.age_b, f.age_c) > 30]
    print(f"\nREADINGS  {raw_total} raw total rows -> {len(frames)} real readings after burst collapse "
          f"(total fires once per phase update; partial sums dropped)")
    print(f"  spacing  p05 {spacing['p05']:.1f}s  median {spacing['median']:.1f}s  "
          f"p95 {spacing['p95']:.1f}s  max {spacing['max']:.0f}s")
    print(f"  {100 * len(stale) / max(1, len(frames)):.1f}% of readings carry a phase value older than 30s")

    base = rough_baseline(frames)
    print(f"\nROUGH FLOOR (p10, inspection only - spec 5.1 baseline is slice 4)\n  {base.describe()}")

    windows = find_ev_windows(frames, base)
    print(f"\nEV CHARGE SESSIONS by signature ({len(windows)})  - {UNRELIABLE_NOTE}")
    for window in windows:
        print(f"  {window.describe()}")
    if not windows:
        print("  none found in this window")
    ev_time = sum((w.duration for w in windows), timedelta())
    covered = frames[-1].when - frames[0].when
    if covered.total_seconds() > 0:
        print(f"  EV active {100 * ev_time / covered:.1f}% of the covered time - "
              "detections in those stretches must be flagged unreliable")

    if not args.no_plots:
        by_day: dict[date, list[Frame]] = defaultdict(list)
        for frame in frames:
            by_day[frame.when.date()].append(frame)
        print("\nPLOTS")
        for day, day_frames in sorted(by_day.items()):
            out = Path("var/plots") / f"power_{day:%Y-%m-%d}.png"
            _plot_day(day, day_frames, windows, base, out)
            print(f"  {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
