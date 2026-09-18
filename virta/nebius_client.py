"""Minimal Nebius Token Factory client for Virta.

Two spec gotchas are handled here, once, so nothing downstream has to think
about them (spec 0, section 5, risk list 11):

1. Nemotron models on Nebius are REASONING models. `choices[0].message.content`
   may be EMPTY and the actual answer arrives in the non-standard
   `reasoning_content` field. Every read goes through `_extract_text`, which
   reports WHICH field the text came from.
2. These models do not support function/tool calling through the OpenAI
   wrapper. `chat()` therefore takes no tools/response_format and never passes
   any - structured output will come later from prompting for JSON and parsing
   it (spec 5).

Only abstracted, already-aggregated information is ever sent through here;
raw power data stays on the edge (spec V3.2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import NebiusConfig

try:
    import openai
    from openai import OpenAI
except ModuleNotFoundError as exc:  # pragma: no cover - setup snag, not logic
    raise SystemExit(
        "The 'openai' package is not installed (Nebius Token Factory is\n"
        "OpenAI-compatible, so Virta uses the standard client).\n"
        "  Install it with:  python -m pip install -r requirements.txt"
    ) from exc


class NebiusError(RuntimeError):
    """A call to Nebius failed, with a message aimed at fixing the setup."""


@dataclass(frozen=True)
class ChatResult:
    """One completion, plus where its text actually came from."""

    text: str
    source: str  # "content" | "reasoning_content" | "empty"
    content: str  # raw message.content, as returned
    reasoning_content: str  # raw message.reasoning_content, as returned
    model: str
    usage: dict[str, Any]
    finish_reason: str = ""  # "length" = cut off at max_tokens, usually mid-thought

    @property
    def used_reasoning_field(self) -> bool:
        return self.source == "reasoning_content"

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def _extract_text(message: Any) -> tuple[str, str, str]:
    """Return (text, source, (content, reasoning_content)) flattened as a 3-tuple.

    THE GOTCHA, in one place: prefer `content`, fall back to
    `reasoning_content` when content is empty/missing.
    """
    content = (getattr(message, "content", None) or "").strip()
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is None:
        # Older/newer SDK versions may park unknown fields in model_extra
        # rather than exposing them as attributes.
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content")
    reasoning = (reasoning or "").strip()

    text = content or reasoning
    if content:
        source = "content"
    elif reasoning:
        source = "reasoning_content"
    else:
        source = "empty"
    return text, source, (content, reasoning)


def make_client(config: NebiusConfig) -> OpenAI:
    """Standard OpenAI client pointed at Nebius Token Factory."""
    return OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout_s,
    )


def chat(
    config: NebiusConfig,
    system: str,
    user: str,
    *,
    client: OpenAI | None = None,
    max_tokens: int | None = None,
) -> ChatResult:
    """Send ONE chat completion and return the answer text plus its source field.

    No tools, no response_format - reasoning models on Nebius do not support
    function calling through this wrapper (spec 0).
    """
    client = client or make_client(config)

    try:
        resp = client.chat.completions.create(
            model=config.model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens or config.max_tokens,
        )
    except openai.AuthenticationError as exc:
        raise NebiusError(
            f"Nebius rejected the API key (HTTP 401).\n"
            f"  base_url: {config.base_url}\n"
            f"  key     : {config.redacted_key}\n"
            "  Check NEBIUS_API_KEY against the Token Factory console - a stale,\n"
            "  truncated, or whitespace-padded key is the usual cause.\n"
            f"  Server said: {exc}"
        ) from exc
    except openai.NotFoundError as exc:
        raise NebiusError(
            f"Nebius does not know model id {config.model_id!r} (HTTP 404).\n"
            f"  base_url: {config.base_url}\n"
            "  Copy the exact id from the Token Factory catalog/Playground into\n"
            "  NEBIUS_MODEL_ID. Also check NEBIUS_BASE_URL still ends in /v1/.\n"
            f"  Server said: {exc}"
        ) from exc
    except openai.PermissionDeniedError as exc:
        raise NebiusError(
            f"Nebius refused access to {config.model_id!r} (HTTP 403).\n"
            "  The key is valid but not entitled to this model, or the account\n"
            "  is out of credits - check the Token Factory console and request\n"
            "  hackathon participant credits if you have not yet.\n"
            f"  Server said: {exc}"
        ) from exc
    except openai.RateLimitError as exc:
        raise NebiusError(
            "Nebius rate-limited or billed-out the request (HTTP 429).\n"
            "  Wait and retry, or check remaining credits in the console.\n"
            f"  Server said: {exc}"
        ) from exc
    except openai.APIConnectionError as exc:
        raise NebiusError(
            f"Could not reach Nebius at {config.base_url}.\n"
            "  Check the URL (it should end in /v1/), your network, and any\n"
            "  proxy/firewall between this machine and the internet.\n"
            f"  Underlying error: {exc}"
        ) from exc
    except openai.APIStatusError as exc:
        raise NebiusError(
            f"Nebius returned HTTP {exc.status_code} for model "
            f"{config.model_id!r}.\n  Server said: {exc}"
        ) from exc

    if not resp.choices:
        raise NebiusError(
            f"Nebius returned no choices for model {config.model_id!r}. "
            "Response was well-formed but empty."
        )

    text, source, (content, reasoning) = _extract_text(resp.choices[0].message)
    usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}

    return ChatResult(
        text=text,
        source=source,
        content=content,
        reasoning_content=reasoning,
        model=getattr(resp, "model", config.model_id),
        usage=usage,
        finish_reason=str(getattr(resp.choices[0], "finish_reason", "") or ""),
    )


def extract_json(result: ChatResult) -> Any:
    """Structured output without tool calling (spec 2, 6): find the JSON in the reply.

    Parse `content` first. Only if it holds no JSON, fall back to
    `reasoning_content` - where the model's thinking surrounds the answer with
    prose, so the LAST complete JSON block is the one that counts. Returns None
    when nothing parses; callers must treat that as "no answer", never guess.
    """
    import json

    for text in (result.content, result.reasoning_content):
        if not text:
            continue
        cleaned = text.replace("```json", "```")
        # try every opening bracket from the end backwards: the last complete
        # block wins, and prose before it is skipped
        for start in sorted({i for i, ch in enumerate(cleaned) if ch in "[{"}, reverse=True):
            decoder = json.JSONDecoder()
            try:
                value, _ = decoder.raw_decode(cleaned[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, (list, dict)) and value:
                # prefer the outermost block that starts here; skip tiny inner fragments
                outer = _outermost(cleaned, start, decoder)
                return outer if outer is not None else value
    return None


def _outermost(text: str, start: int, decoder: Any) -> Any:
    """If the block at `start` sits inside a larger valid block, return the larger one."""
    import json

    best = None
    for i in range(start, -1, -1):
        if text[i] not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if i + end > start:
            best = value
    return best
