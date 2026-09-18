"""The human-readable worklist file (spec 5A): async conversation over a text file.

The labelling pass writes what it found; the human answers on `your_label:`
whenever they like; the next pass reads the answer and acts. No UI, survives
restarts, fits a keyboard workflow, never interrupts the live console.

HUMAN INPUT IS SACRED. The pass owns a fixed set of machine fields and
regenerates only those. Everything else in a cluster block - `your_label:`
including multi-line prose, and any extra line the human adds - is carried
through verbatim. The timeframe-labels section is carried through verbatim in
full. The file is written atomically with the previous version kept as .bak.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Machine-owned field names are deliberately unlikely words for a human to type,
# so nothing the human writes is ever mistaken for a field the pass may rewrite.
MACHINE_KEYS = ("seen", "llm_guess", "llm_why", "llm_reply", "pass_note", "profile", "status")
BLOCK_HEADER = re.compile(r"^\[([A-Za-z0-9_\-+]+)\]")
FIELD = re.compile(r"^  ([a-z_]+):(.*)$")

TIMEFRAME_HEADING = "## Timeframe labels"
CLUSTERS_HEADING = "## Recurring unknown loads"
RESULTS_HEADING = "## Timeframe results"

PREAMBLE = """\
# Virta - labelling worklist
#
# The labelling pass writes here; you answer whenever you like; the next pass
# acts on your answers. It never changes anything you write.
#
# Answer on the `your_label:` line of a cluster:
#   your_label: fridge          -> a clean name: promoted to profiles/fridge.json
#   your_label: it's the dryer but only the hot cycle, ...
#                               -> a comment: sent to Nemotron, which proposes a
#                                  label in `llm_reply`; confirm with a clean name
#   your_label: ignore          -> rejected, never proposed again
#   your_label: later           -> needs more data, kept open
#
# Nothing here is ever acted on without your answer (propose, then confirm).
"""

TIMEFRAME_HELP = """\
# Ran something on purpose? One line each, the pass finds the step it made:
#   2026-09-16 07:42-07:44 kettle
"""


@dataclass
class HumanBlock:
    """What the human owns inside one cluster block."""

    your_label: list[str] = field(default_factory=list)  # raw lines, verbatim
    extra: list[str] = field(default_factory=list)  # any other human line, verbatim

    @property
    def answer(self) -> str:
        """The answer as one string (continuation lines joined), stripped."""
        if not self.your_label:
            return ""
        first = self.your_label[0].split(":", 1)[1] if ":" in self.your_label[0] else ""
        rest = [line.strip() for line in self.your_label[1:]]
        return " ".join([first.strip(), *rest]).strip()


@dataclass
class ParsedWorklist:
    timeframe_body: list[str]  # verbatim, the whole section
    blocks: dict[str, HumanBlock]


def parse(path: str | Path) -> ParsedWorklist:
    file = Path(path)
    if not file.is_file():
        return ParsedWorklist(timeframe_body=[], blocks={})

    lines = file.read_text(encoding="utf-8").splitlines()
    timeframe: list[str] = []
    blocks: dict[str, HumanBlock] = {}
    section = ""
    current: HumanBlock | None = None
    in_label = False

    for line in lines:
        if line.startswith("## "):
            section = line.strip()
            current, in_label = None, False
            continue
        if section == TIMEFRAME_HEADING:
            timeframe.append(line)
            continue
        if section != CLUSTERS_HEADING:
            continue

        header = BLOCK_HEADER.match(line)
        if header:
            current = blocks.setdefault(header.group(1), HumanBlock())
            in_label = False
            continue
        if current is None:
            continue

        field_match = FIELD.match(line)
        if field_match:
            key = field_match.group(1)
            if key == "your_label":
                current.your_label = [line]
                in_label = True
            elif key in MACHINE_KEYS:
                in_label = False
            else:
                current.extra.append(line)  # a field the human invented: keep it
                in_label = False
            continue
        if in_label and line.strip():
            current.your_label.append(line)  # prose continuation of the answer
        elif line.strip():
            current.extra.append(line)  # anything else the human wrote: keep it

    # drop the help comment lines we write ourselves; keep everything else
    help_lines = set(TIMEFRAME_HELP.splitlines())
    body = [l for l in timeframe if l not in help_lines]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return ParsedWorklist(timeframe_body=body, blocks=blocks)


def timeframe_lines(parsed: ParsedWorklist) -> list[str]:
    return [l.strip() for l in parsed.timeframe_body if l.strip() and not l.strip().startswith("#")]


def render(
    parsed: ParsedWorklist,
    entries: list[dict],
    timeframe_results: list[str],
) -> str:
    """Rebuild the file: machine fields fresh, human content exactly as found."""
    out = [PREAMBLE, TIMEFRAME_HEADING, TIMEFRAME_HELP.rstrip("\n")]
    out.extend(parsed.timeframe_body or [""])
    out.append("")
    out.append(CLUSTERS_HEADING)
    out.append("")
    if not entries:
        out.append("(nothing recurring yet - clusters appear after 3 similar unknown loads)")
        out.append("")
    for entry in entries:
        human = parsed.blocks.get(entry["id"], HumanBlock())
        out.append(f"[{entry['id']}]  {entry['headline']}")
        for key in ("seen", "llm_guess", "llm_why", "llm_reply", "pass_note", "profile"):
            if entry.get(key):
                value = " ".join(str(entry[key]).split())  # machine fields stay one line
                out.append(f"  {key}: {value}")
        out.extend(human.your_label or ["  your_label: "])
        out.extend(human.extra)
        out.append(f"  status: {entry['status']}")
        out.append("")
    out.append(RESULTS_HEADING)
    out.append("# written by the pass - one line per timeframe label above")
    out.extend(timeframe_results or ["(none yet)"])
    out.append("")
    return "\n".join(out)


def write(path: str | Path, text: str) -> None:
    """Atomic replace, previous version kept as .bak - a crash never eats an answer."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.is_file():
        backup = file.with_suffix(file.suffix + ".bak")
        backup.write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    from .fsutil import atomic_write_text

    atomic_write_text(file, text)
