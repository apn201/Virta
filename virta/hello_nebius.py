"""Slice 1: prove ONE Nemotron call works on Nebius Token Factory.

Run:  python -m virta.hello_nebius

Sends a single trivial completion and prints what came back AND which response
field it came from (`content` vs `reasoning_content`) - so the reasoning-model
gotcha is visibly handled from the very first call.

This is deliberately the whole program. No CSV parsing, no detection, no Home
Assistant, no M5 console, no energy-advice prompt, and no loop: exactly one
request per run.
"""

from __future__ import annotations

import sys

from .config import ConfigError, load_nebius_config
from .nebius_client import ChatResult, NebiusError, chat

SYSTEM_PROMPT = "You are a terse connectivity check. Answer with exactly what is asked, nothing else."
USER_PROMPT = "Reply with the single word: ONLINE"


def _preview(value: str, limit: int = 400) -> str:
    if not value:
        return "<empty>"
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + f"... [{len(collapsed)} chars total]"


def report(result: ChatResult) -> None:
    print("\n--- response ---")
    print(f"  answered from   : {result.source}")
    print(f"  text            : {_preview(result.text)}")
    print(f"  model reported  : {result.model}")
    if result.usage:
        usage = ", ".join(f"{k}={v}" for k, v in result.usage.items() if isinstance(v, int))
        print(f"  usage           : {usage}")

    print("\n--- raw fields (the gotcha, visible) ---")
    print(f"  message.content           : {_preview(result.content, 200)}")
    print(f"  message.reasoning_content : {_preview(result.reasoning_content, 200)}")

    print()
    if result.source == "reasoning_content":
        print("GOTCHA CONFIRMED: content was empty, answer taken from reasoning_content.")
    elif result.source == "content":
        print("This model filled content normally; the reasoning_content fallback stays in place.")
    else:
        print(
            "WARNING: both content and reasoning_content were empty.\n"
            "  The call succeeded but the model returned nothing usable. Try raising\n"
            "  NEBIUS_MAX_TOKENS (reasoning models spend tokens thinking before answering)."
        )


def main() -> int:
    try:
        config = load_nebius_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    if len(sys.argv) > 1:  # python -m virta.hello_nebius <model id>: check one tier's model
        from dataclasses import replace

        config = replace(config, model_id=sys.argv[1])
    print(config.describe(), flush=True)
    print(f"\nSending one completion: {USER_PROMPT!r}", flush=True)

    try:
        result = chat(config, SYSTEM_PROMPT, USER_PROMPT)
    except NebiusError as exc:
        print(f"\nNEBIUS ERROR\n{exc}", file=sys.stderr)
        return 1

    report(result)
    return 0 if result.text else 3


if __name__ == "__main__":
    raise SystemExit(main())
