# Virta - Claude Code Kickoff Prompt

Paste everything below the line into Claude Code as the first message, with `virta-nebius-spec-v3.md` in the working directory. Develop on the PC; the finished code deploys to the Pi3 later (spec V3.2). Do the account/token steps yourself - Claude Code can't create the Nebius key, the HA token, or run OpenClaw.

Have ready before starting:
- `NEBIUS_API_KEY` set in your env (you have the key stored).
- A chosen Nemotron `MODEL_ID` from the Token Factory catalog - grab one and test it in the Token Factory Playground before this, so you know it responds.
- The real data CSVs in the working dir (`3EM_Power_Raw_History.csv`, `ha_3em_power_20min_history.csv`).

---

You are building **Virta**, a NILM power-monitoring + price-aware energy console for the Nebius x NVIDIA Physical AI hackathon. The full spec is `virta-nebius-spec-v3.md` in this directory - read it fully before writing any code, including the v3, v3.2 addenda. It is the source of truth. Where it gives a default or marks something TUNABLE, follow the default and move on.

## Non-negotiables (from the spec)

- **Reasoning runs on Nebius Token Factory** (OpenAI-compatible). Base URL `https://api.tokenfactory.nebius.com/v1/`, key from env `NEBIUS_API_KEY`, model id from env/config. Standard `openai` Python client.
- **Nemotron models are REASONING models: the answer may be in `reasoning_content`, and `choices[0].message.content` may be EMPTY.** Handle this from the very first call. Also: no function/tool calling via the wrapper - structured output comes from prompting for JSON and parsing it, never tools.
- **Server-authoritative / edge-local split:** the Pi (later) polls HA locally and only sends abstracted events to Nebius - never raw power data. Build with that boundary in mind even in dev.
- **Config-driven everything:** Nebius base URL, model id, key; HA base URL + token - all from env/config, never hardcoded. The same code must run on the PC (dev) and the Pi3 (deploy) by changing env only.
- **Real data parsing rules (spec 2.4):** values may be `unavailable`/`unknown`/empty - SKIP, don't `float()` them. Timestamps are irregular (~10-15s, sometimes sub-second) - use actual elapsed time, never assume a fixed interval.

## Build Slice 1 ONLY: Nebius hello-world

Before any detection, HA, or console code, prove the reasoning endpoint works. Build the smallest thing:

1. A config module reading `NEBIUS_API_KEY`, `NEBIUS_BASE_URL` (default `https://api.tokenfactory.nebius.com/v1/`), and `NEBIUS_MODEL_ID` from env. Echo on startup which model and base URL are loaded (not the key).
2. A single function that sends one chat completion to the model with a trivial system+user message and returns the text, **correctly reading `reasoning_content` when `content` is empty** (`resp.choices[0].message.content or getattr(resp.choices[0].message, "reasoning_content", "")`).
3. A tiny test: send "Reply with the single word: ONLINE" and print what comes back, plus which field (`content` vs `reasoning_content`) it came from - so I can see the gotcha handling working.
4. Clear failure messages if the key is missing, the model id is wrong/unavailable, or the endpoint refuses - these are the #1 setup snags.
5. Tell me exactly which env vars to set and how to run it.

Do NOT build in Slice 1: the CSV parser, event detection, signature matching, HA polling, the M5 UDP console, or the reasoning prompt for energy advice. Just prove one Nemotron call works and the response-field handling is correct. Once it runs for me, we do Slice 2 (parse the real CSVs and plot/inspect the power data), then Slice 3+ per spec build order (section 9).

## What NOT to do

- Do not hardcode the key, base URL, or model id.
- Do not build a loop that calls the model repeatedly yet - one call in Slice 1.
- Do not touch HA, the Pi, or the M5 console yet.
- Do not use tool/function calling for structured output - prompt for JSON later, parse it.
- Do not ask me to decide things the spec already decided.

Start by reading `virta-nebius-spec-v3.md`, confirm in three or four sentences your understanding of Slice 1 and the two key gotchas (`reasoning_content`, no tool-calling), then build Slice 1.
