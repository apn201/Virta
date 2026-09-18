"""Slice 5: the labelling pass - unknown loads -> named profiles (spec 5A, 7, 12).

Run:
    python -m virta.labeller              # cluster, act on answers, ask Nemotron
    python -m virta.labeller --no-llm     # everything except the cloud call
    python -m virta.labeller --refresh    # re-run the detector over the cached history first

One pass does, in order:
  1. Cluster recurring UNKNOWN loads from the event log (local statistics).
  2. Carry cluster ids forward by signature (labels/clusters.json), so an answer
     never drifts onto a different load overnight.
  3. Read the worklist (labels/worklist.md) and act on the human's answers:
       clean name -> promote to profiles/<name>.json (deterministic, no LLM)
       comment    -> send to Nemotron, which proposes a label (human confirms)
       ignore     -> rejected;  later -> needs-more-data
  4. Timeframe labels ("2026-09-16 07:42-07:44 kettle") -> find the step in the
     window; a new name becomes a draft profile.
  5. ONE batched Nemotron call for new guesses + comment replies, through the
     same cost gate as live advice (kill switch + caps). Skipped if nothing new.
  6. Rewrite the worklist: machine fields fresh, human input untouched.

Propose, then confirm: the LLM's guess is a hypothesis. Nothing becomes a
profile without the human writing its name.

Only cluster SUMMARIES go to the cloud - size, phase, duration, time of day,
count. Never raw power data (spec 11).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import worklist as wl
from .clustering import Cluster, assign_ids, cluster, occurrences_from_events
from .config import ConfigError, load_cost_config, load_nebius_config
from .cost_control import CloudCallGate
from .nebius_client import NebiusError, chat, extract_json
from .profiles import load_profiles

# Replay writes the first, the live loop appends to the second; both are mined.
EVENT_LOGS = ("var/events.jsonl", "var/events_live.jsonl")
REGISTRY = "labels/clusters.json"
WORKLIST = "labels/worklist.md"
PROFILES = "profiles"
USAGE_PROFILE = "usage_profile.txt"  # spec 3.4, optional hand-written patterns

REGUESS_AFTER_NEW_OCCURRENCES = 3
# Measured: 3 clusters took >4096 reasoning tokens before any JSON appeared
# (finish_reason=length). One call a day - budget generously, cap calls instead.
LLM_MAX_TOKENS = 12000

SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,40}$")
TIMEFRAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s+(.+?)\s*$")
IGNORE_WORDS = {"ignore", "reject", "rejected", "no", "none"}
LATER_WORDS = {"later", "more data", "wait", "?", "unsure", "dunno"}

SYSTEM_PROMPT = """\
You help label unknown electrical loads in a Finnish home, measured by one \
three-phase meter (Shelly 3EM, phases A/B/C, ~15 s readings). You only ever see \
compact summaries of RECURRING unknown loads - never raw data.

For each cluster, suggest the most likely appliance, in one short label, with a \
one-sentence reason and a confidence from 0 to 1. Use: size in watts, which \
single phase, how long it runs, time of day, how often, and whether it pulses \
(many short on/off hits in one session = a thermostat or element cycling). \
"unclear" is an honest and acceptable guess. Do not guess an appliance that is \
already identified unless the size clearly fits it.

Cluster fields: watts = typical step size of ONE occurrence; watts_spread = how \
much that size varies; duration_min = how long ONE occurrence stays on (median); \
occurrences = total times seen; sessions = groups of occurrences less than 2 h \
apart; per_session_max = most occurrences in one session; pulsing = true when \
occurrences bunch into few sessions; time_of_day = occurrences per part of day; \
during_ev = occurrences seen while the EV charged (sizes less reliable). Keep \
your reasoning brief - these are hypotheses the human will confirm.

A cluster with base_part_off: true is NOT a load switching on: it is a part of the always-on base load (about 1 kW: ventilation, fridge, freezer, servers, cryptominers, PCs) that was switched OFF for a while - its watts are negative. Guess which always-on device that is (e.g. a PC switched off overnight, a ventilation unit stepping down).

When a cluster carries a HUMAN COMMENT, read it as authoritative information \
from the person who lives there and propose a clean snake_case label for it, \
plus a one-sentence reply. The human will confirm; you never finalise.

Answer ONLY with JSON, no prose outside it:
{"guesses": [{"id": "...", "guess": "...", "why": "...", "confidence": 0.0}],
 "replies": [{"id": "...", "suggested_label": "...", "reply": "..."}]}"""


# --- registry ---------------------------------------------------------------
def load_registry(path: str = REGISTRY) -> dict:
    file = Path(path)
    if not file.is_file():
        return {"clusters": {}, "timeframes": {}}
    data = json.loads(file.read_text(encoding="utf-8"))
    data.setdefault("clusters", {})
    data.setdefault("timeframes", {})
    return data


def save_registry(data: dict, path: str = REGISTRY) -> None:
    from .fsutil import atomic_write_text

    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


# --- answers ----------------------------------------------------------------
def classify_answer(answer: str) -> tuple[str, str]:
    """-> (kind, value). The label-vs-comment fork (spec 5A)."""
    text = answer.strip()
    if not text:
        return "none", ""
    lowered = text.lower().strip(".! ")
    if lowered in IGNORE_WORDS:
        return "ignore", ""
    if lowered in LATER_WORDS:
        return "later", ""
    if SLUG_OK.match(text) and len(text.split()) <= 3:
        return "label", re.sub(r"[\s\-]+", "_", text.strip().lower())
    return "comment", text


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --- profiles ---------------------------------------------------------------
def _tolerance(watts: float, spread: float) -> float:
    return round(max(40.0, 3 * spread, 0.10 * watts))


def promote_cluster(slug: str, display: str, entry: dict) -> tuple[str | None, str]:
    """Write a DRAFT profile from a cluster's measured features. Never overwrites."""
    path = Path(PROFILES) / f"{slug}.json"
    if path.exists():
        return None, f"profile '{slug}' already exists - pick another name or edit {path} by hand"
    summary = entry["summary"]
    if len(summary["phases"]) != 1:
        return None, "multi-phase unknown loads can't be matched as a step yet - kept as a cluster"
    pulsing = bool(summary.get("pulsing"))
    profile = {
        "id": slug,
        "display_name": display,
        "tier": 2 if pulsing else 1,
        "draft": True,
        # a base part (PC, server, ventilation) is normally ON: the detector
        # matches its switch-OFF below the base load, and its return
        "base_component": bool(summary.get("base_part_off")),
        "signature": {
            "type": "step",
            "phase": summary["phases"],
            "step_w": abs(summary["watts"]),
            "step_tolerance_w": _tolerance(summary["watts"], summary.get("watts_spread", 0)),
            "cycling": (
                {"pulse_min": summary.get("duration_min"), "pulses_per_session": summary.get("per_session_max")}
                if pulsing else None
            ),
        },
        "measured": {
            "from_cluster": entry["id"],
            "occurrences": summary["occurrences"],
            "typical_duration_min": summary.get("duration_min"),
            "time_of_day": summary.get("time_of_day"),
            "first_seen": summary.get("first_seen"),
            "last_seen": summary.get("last_seen"),
        },
        "context_factors": [
            {"factor": "time_of_day", "typical": sorted(summary.get("time_of_day", {})), "strength": "medium"}
        ],
        "notes": (
            f"Draft from worklist cluster {entry['id']}, named by the builder "
            f"{datetime.now():%Y-%m-%d}. LLM had guessed: {entry.get('llm_guess') or 'n/a'}."
        ),
    }
    path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.as_posix(), ""


# --- timeframe labels -------------------------------------------------------
def process_timeframes(lines: list[str], registry: dict, events: list[dict]) -> list[str]:
    profiles = {p.id: p for p in load_profiles(PROFILES)}
    results = []
    for line in lines:
        cached = registry["timeframes"].get(line)
        if cached:
            results.append(cached)
            continue
        m = TIMEFRAME.match(line)
        if not m:
            results.append(f"  {line}  -> not understood (expected: YYYY-MM-DD HH:MM-HH:MM name)")
            continue
        day, t0, t1, name = m.groups()
        start = datetime.fromisoformat(f"{day}T{t0.zfill(5)}").astimezone()
        end = datetime.fromisoformat(f"{day}T{t1.zfill(5)}").astimezone()
        slug = re.sub(r"[\s\-]+", "_", name.strip().lower())
        window = [
            e for e in events
            if e["kind"] == "ON" and start - timedelta(seconds=90) <= datetime.fromisoformat(e["when"]) <= end + timedelta(seconds=30)
        ]
        if not window:
            result = f"  {line}  -> no visible step in that window: the 3EM did not see it"
        elif slug in profiles:
            hits = [e for e in window if e["load_id"] == slug]
            if hits:
                h = hits[0]
                result = (f"  {line}  -> recognised: {slug} at {datetime.fromisoformat(h['when']):%H:%M:%S}, "
                          f"{h['confidence']:.0%}")
            else:
                seen = ", ".join(f"{e['load_id']} {sum(e['delta_w'].values()):+.0f} W {e['phases']}" for e in window)
                result = f"  {line}  -> profile '{slug}' exists but was not matched here; saw: {seen}"
        else:
            unknowns = [e for e in window if e["state"] == "UNKNOWN"]
            if not unknowns:
                seen = ", ".join(e["load_id"] for e in window)
                result = f"  {line}  -> only known loads in that window ({seen}) - nothing new to learn"
            else:
                biggest = max(unknowns, key=lambda e: abs(sum(e["delta_w"].values())))
                watts = round(sum(biggest["delta_w"].values()))
                entry = {
                    "id": f"timeframe {line}",
                    "summary": {"phases": biggest["phases"], "watts": watts, "watts_spread": 0,
                                "occurrences": 1, "time_of_day": {}, "pulsing": False},
                }
                path, problem = promote_cluster(slug, name.strip(), entry)
                result = (f"  {line}  -> new draft profile {path}: {watts:+d} W on {biggest['phases']}"
                          if path else f"  {line}  -> {problem}")
        registry["timeframes"][line] = result
        results.append(result)
    return results


# --- the LLM half -----------------------------------------------------------
def _payload(clusters_needing_guess: list[dict], comments: list[dict]) -> str:
    profiles = load_profiles(PROFILES)
    known = [
        {"id": p.id, "phase": p.phase, "watts": p.step_w, "type": p.kind}
        for p in profiles if p.matchable
    ]
    unseen = [p.id for p in profiles if not p.matchable]
    from .advisor import read_usage_profile  # one reader: comment lines skipped

    usage = read_usage_profile(USAGE_PROFILE)
    body = {
        "house": {
            "country": "Finland",
            "month": datetime.now().strftime("%B"),
            "base_load_w": "about 1000 (static always-on load: ventilation, fridge, freezer, servers, cryptominers - about 1 kW, bundled, not itemised) - excluded from clusters",
            "smallest_step_detected_w": 120,
            "ev_charger": "balanced three-phase ~11 kW, already identified",
        },
        "already_identified": known,
        "owned_but_never_seen_by_the_meter": unseen,
        "usage_patterns": usage or "not provided",
        "clusters_to_guess": clusters_needing_guess,
        "clusters_with_human_comment": comments,
    }
    return json.dumps(body, indent=1, ensure_ascii=False)


def ask_nemotron(guess_for: list[dict], comments: list[dict], gate: CloudCallGate) -> tuple[dict, str]:
    """One batched call. Returns (parsed JSON or {}, status message)."""
    decision = gate.allow_housekeeping(f"{len(guess_for)} guesses, {len(comments)} replies")
    if not decision.should_call:
        return {}, f"skipped: {decision.reason}"
    try:
        config = load_nebius_config()
    except ConfigError as exc:
        return {}, f"skipped: {exc.args[0].splitlines()[0]}"
    gate.record_call({})  # counted when SENT - a crash or failure still counts
    try:
        result = chat(config, SYSTEM_PROMPT, _payload(guess_for, comments), max_tokens=LLM_MAX_TOKENS,
                      model=config.model_labels)
    except NebiusError as exc:
        return {}, f"failed: {str(exc).splitlines()[0]}"
    gate.add_usage(result.usage)
    # always keep the last raw reply: problems are debugged from evidence, not retried blind
    dump = Path("var/last_llm_reply.txt")
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(
        f"finish_reason: {result.finish_reason}\nusage: {result.usage}\n\n"
        f"--- content ---\n{result.content}\n\n--- reasoning_content ---\n{result.reasoning_content}\n",
        encoding="utf-8",
    )
    tokens = result.usage.get("completion_tokens", "?")
    if result.truncated:
        return {}, (f"reply cut off at max_tokens ({tokens} completion tokens) - raise "
                    f"LLM_MAX_TOKENS; raw reply in {dump}")
    parsed = extract_json(result)
    # shape check: a JSON fragment quoted inside the model's thinking is not an answer
    if not isinstance(parsed, dict) or not ({"guesses", "replies"} & set(parsed)):
        return {}, f"answered from {result.source}, but no usable JSON in it (raw reply in {dump})"
    return parsed, f"one call, answer from {result.source}, {tokens} completion tokens"


# --- the pass ---------------------------------------------------------------
def headline(summary: dict) -> str:
    dur = summary.get("duration_min")
    dur_txt = "open-ended" if dur is None else (f"~{dur * 60:.0f} s" if dur < 1.5 else f"~{dur:.0f} min")
    tod = ", ".join(f"{k.split(' ')[0]} {v}" for k, v in summary.get("time_of_day", {}).items())
    pulse = f"  PULSING (up to {summary['per_session_max']}/session)" if summary.get("pulsing") else ""
    sessions = summary["sessions"]
    if summary.get("base_part_off"):
        return (f"{summary['watts']} W  phase {summary['phases']}  BASE PART OFF for {dur_txt}  "
                f"{summary['occurrences']}x in {summary['span_days']:g} d "
                f"({sessions} session{'s' if sessions != 1 else ''})  {tod}")
    return (f"+{summary['watts']} W  phase {summary['phases']}  {dur_txt}  "
            f"{summary['occurrences']}x in {summary['span_days']:g} d "
            f"({sessions} session{'s' if sessions != 1 else ''})  {tod}{pulse}")


def run(*, use_llm: bool, verbose: bool = True, gate: CloudCallGate | None = None) -> int:
    """One labelling pass. `gate`: share the caller's gate (the nightly thread
    inside the live loop) so every call lands in one ledger."""
    say = print if verbose else (lambda *a, **k: None)
    logs = [p for p in EVENT_LOGS if Path(p).is_file()]
    if not logs:
        print(f"No event log ({' or '.join(EVENT_LOGS)}) - run: python -m virta.replay --fetch", file=sys.stderr)
        return 1

    events = [json.loads(l) for p in logs for l in Path(p).read_text(encoding="utf-8").splitlines() if l.strip()]
    # pair ON/OFF within each log (load keys restart per log), then drop overlap
    # where a replay and the live loop saw the same switch
    occurrences, seen_keys = [], set()
    for p in logs:
        for occ in occurrences_from_events(p):
            key = (occ.start.replace(microsecond=0), occ.phases)
            if key not in seen_keys:
                seen_keys.add(key)
                occurrences.append(occ)
    registry = load_registry()
    parsed = wl.parse(WORKLIST)

    # 1-2. cluster and carry ids forward
    found: list[Cluster] = cluster(occurrences)
    assign_ids(found, registry["clusters"])
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for c in found:
        entry = registry["clusters"].setdefault(
            c.id, {"id": c.id, "status": "proposed", "first_proposed": now}
        )
        entry.update({"phases": c.phases, "watts": c.watts, "duration_s": c.duration_s,
                      "summary": c.summary(), "last_updated": now})
        entry["summary"]["id"] = c.id
    say(f"CLUSTERS   {len(found)} recurring unknown loads in the event log "
        f"({len(registry['clusters'])} known to the registry)")

    # 3. act on the human's answers
    comments: list[dict] = []
    for cid, entry in registry["clusters"].items():
        human = parsed.blocks.get(cid)
        kind, value = classify_answer(human.answer if human else "")
        if kind == "none" or entry["status"] == "confirmed":
            continue
        if kind == "ignore":
            entry["status"] = "rejected"
            say(f"ANSWER     {cid}: ignore -> rejected")
        elif kind == "later":
            entry["status"] = "needs-more-data"
            say(f"ANSWER     {cid}: later -> needs-more-data")
        elif kind == "label":
            display = human.answer.strip()
            path, problem = promote_cluster(value, display, entry)
            if path:
                entry.update(status="confirmed", profile=path, pass_note="")
                say(f"ANSWER     {cid}: '{display}' -> promoted to {path}")
            else:
                entry["pass_note"] = problem
                say(f"ANSWER     {cid}: '{display}' -> not promoted: {problem}")
        elif kind == "comment":
            if entry.get("replied_hash") != _hash(value):
                comments.append({"id": cid, "summary": entry["summary"], "comment": value,
                                 "previous_guess": entry.get("llm_guess", "")})
                say(f"ANSWER     {cid}: comment -> will ask Nemotron to propose a label")

    # 4. timeframe labels
    tf_results = process_timeframes(wl.timeframe_lines(parsed), registry, events)
    for line in tf_results:
        say(f"TIMEFRAME {line}")

    # 5. one batched LLM call, only if there is something new to ask
    guess_for = []
    commented = {c["id"] for c in comments}
    for cid, entry in registry["clusters"].items():
        if entry["status"] not in ("proposed", "needs-more-data"):
            continue
        if cid in commented or entry.get("llm_reply"):
            continue  # the human already said what it is - a blind guess adds nothing
        n = entry["summary"]["occurrences"]
        if not entry.get("llm_guess") or n >= entry.get("guessed_at_n", 0) + REGUESS_AFTER_NEW_OCCURRENCES:
            guess_for.append(entry["summary"])

    if not (guess_for or comments):
        say("NEMOTRON   nothing new to ask - no call made")
    elif not use_llm:
        say(f"NEMOTRON   --no-llm: {len(guess_for)} guesses and {len(comments)} replies left pending")
        for s in guess_for:
            registry["clusters"][s["id"]].setdefault("llm_guess", "")
    else:
        answer, status = ask_nemotron(guess_for, comments, gate or CloudCallGate(load_cost_config()))
        say(f"NEMOTRON   {status}")
        if answer:
            got_g = {str(g.get("id")) for g in answer.get("guesses", []) if isinstance(g, dict)}
            got_r = {str(r.get("id")) for r in answer.get("replies", []) if isinstance(r, dict)}
            want_g = {s["id"] for s in guess_for}
            want_r = {c["id"] for c in comments}
            say(f"           matched {len(got_g & want_g)}/{len(want_g)} guesses, "
                f"{len(got_r & want_r)}/{len(want_r)} replies"
                + (f"; unexpected keys {sorted(answer)}" if not (got_g or got_r) else ""))
        for g in answer.get("guesses", []) if isinstance(answer.get("guesses"), list) else []:
            entry = registry["clusters"].get(str(g.get("id", "")))
            if not entry:
                continue
            conf = g.get("confidence")
            conf_txt = f" ({float(conf):.0%})" if isinstance(conf, (int, float)) else ""
            entry["llm_guess"] = f"{g.get('guess', 'unclear')}{conf_txt}"
            entry["llm_why"] = str(g.get("why", ""))
            entry["guessed_at_n"] = entry["summary"]["occurrences"]
        for r in answer.get("replies", []) if isinstance(answer.get("replies"), list) else []:
            entry = registry["clusters"].get(str(r.get("id", "")))
            if not entry:
                continue
            match = next((c for c in comments if c["id"] == entry["id"]), None)
            label = str(r.get("suggested_label", "")).strip()
            entry["llm_reply"] = (f"{r.get('reply', '')} Suggested label: {label}. "
                                  f"Confirm by replacing your comment with: {label}")
            if match:
                entry["replied_hash"] = _hash(match["comment"])

    # 6. rewrite the worklist - human input untouched
    order = {"proposed": 0, "needs-more-data": 1, "confirmed": 2, "rejected": 3}
    entries = []
    for cid, entry in sorted(registry["clusters"].items(),
                             key=lambda kv: (order.get(kv[1]["status"], 9), -kv[1]["summary"]["occurrences"])):
        s = entry["summary"]
        seen = ", ".join(s["examples"]) + (" ..." if s["occurrences"] > len(s["examples"]) else "")
        if s.get("during_ev"):
            seen += f"  ({s['during_ev']} during EV charging - sizes unreliable)"
        entries.append({
            "id": cid,
            "headline": headline(s),
            "seen": seen,
            "llm_guess": entry.get("llm_guess") or (
                "(pending)" if entry["status"] == "proposed" and not entry.get("llm_reply") else ""
            ),
            "llm_why": entry.get("llm_why", ""),
            "llm_reply": entry.get("llm_reply", ""),
            "pass_note": entry.get("pass_note", ""),
            "profile": entry.get("profile", ""),
            "status": entry["status"],
        })
    wl.write(WORKLIST, wl.render(parsed, entries, tf_results))
    save_registry(registry)
    say(f"WORKLIST   {WORKLIST} written ({len(entries)} clusters); registry {REGISTRY}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-llm", action="store_true", help="skip the Nemotron call")
    parser.add_argument("--refresh", action="store_true", help="re-run the detector over cached history first")
    args = parser.parse_args(argv)
    if args.refresh:
        from . import replay

        with contextlib.redirect_stdout(io.StringIO()):
            code = replay.main([])
        if code:
            print("replay failed - run python -m virta.replay to see why", file=sys.stderr)
            return code
        print("REFRESH    detector re-run over cached history -> var/events.jsonl")
    return run(use_llm=not args.no_llm)


if __name__ == "__main__":
    raise SystemExit(main())
