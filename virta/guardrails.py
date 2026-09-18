"""Checks every line Nemotron writes must pass before it reaches the console.

Shared by the live observation voice (advisor.py) and the nightly insights
(nightly.py), so both voices obey the same two rules:

  1. Personal, never creepy (builder decision 2026-09-18, easing spec 6A).
     Playful guesses anchored in a device are welcome - "EV AT FULL TILT.
     DRIVING FERRARI TODAY?", like the scan in the game Another World. What is
     blocked in code is the creepy set: health, mood, relationships, visitors,
     sleep, the bathroom - and anything saying the house is empty, which is
     also a security leak on a screen that can be seen through a window.
  2. Every number on a console line is real: it must appear in the data the
     model was given (small counts excepted). A line that invents a figure is
     dropped, however well it reads.
"""

from __future__ import annotations

import re
from typing import Any

CREEPY = re.compile(
    # the house is empty / who is or isn't there - creepy, and a burglar's notice board
    r"\b((?:you(?:'re| are| were)?|nobody|no one|everyone|anyone)(?: is| was)? (?:home|away|out|gone)"
    r"|home (?:more|less)|empty house|house (?:is |was )?empty|vacation|holiday|travel\w*"
    # sleep, the body, the bathroom
    r"|asleep|awake|sleep\w*|slept|woke|wake up|insomnia|in bed|shower\w*|bath(?:ing|room)?\b|toilet"
    r"|naked|drunk|hangover"
    # health and mood
    r"|health|sick|ill\b|illness|disease|mood|sad|depress\w*|lonely|alone|stress\w*"
    # other people
    r"|guests?|visitors?|family|kids?|partner|wife|husband|girlfriend|boyfriend)\b",
    re.IGNORECASE,
)
PERSON_TALK = CREEPY  # older name, same list
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def about_a_person(*texts: str) -> bool:
    """True when a line crosses from personal into creepy (see CREEPY)."""
    return any(CREEPY.search(t or "") for t in texts)


def known_numbers(data: Any) -> set[str]:
    """Every number in the data the model saw, in the forms a line might print it."""
    found: set[str] = set()

    def add(value: float) -> None:
        for digits in (0, 1, 2, 3):
            found.add(f"{round(value, digits):g}")
        found.add(str(int(value)))

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add(float(node))
        elif isinstance(node, str):
            for n in NUMBER.findall(node):
                add(float(n.replace(",", ".")))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(data)
    return found


def ungrounded_numbers(line: str, grounded: set[str]) -> list[str]:
    """Numbers on a line that the data does not contain (counts up to 12 excepted)."""
    missing = []
    for n in NUMBER.findall(line):
        value = float(n.replace(",", "."))
        if value <= 12 and value == int(value):
            continue  # "3 RUNS", "2ND BOIL", "10 TIMES" - counts and small ordinals
        if f"{value:g}" not in grounded and n not in grounded:
            missing.append(n)
    return missing
