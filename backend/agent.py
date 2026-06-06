"""LLM agent. One per power.

Each call returns a structured payload:
- thought: free-form reasoning (private)
- scratchpad: updated long-term notes (private, persisted across turns)
- messages: outbound messages to specific other powers (negotiation only)
- commitments: optional binding declarations (orders only)
- orders: order strings (orders only)
"""

import json
import logging
import re

import litellm

logger = logging.getLogger(__name__)


def _extract_json_blob(text: str):
    """Pull the JSON object out of an LLM response (fenced or bare)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _slim_state(game_state):
    # Trim commitments_history to last 12 entries so the prompt stays bounded
    # but agents still see a useful track-record of recent kept/broken pledges.
    commitments_log = list(game_state.get("commitments_history", []))[-12:]
    return {
        "turn": game_state["turn"],
        "powers": {
            k: {"centers": v["centers"], "units": v["units"], "status": v["status"]}
            for k, v in game_state.get("powers", {}).items()
        },
        "units": game_state.get("units", []),
        "supply_centers": game_state.get("supply_centers", {}),
        "dislodged": game_state.get("dislodged", []),
        # Public knowledge: last phase's orders by power (so agents can see
        # exactly what every other power did, not just infer from positions).
        "last_phase": game_state.get("last_phase", ""),
        "last_phase_orders": game_state.get("last_phase_orders", {}),
        # Public knowledge: pending and historical commitments. Agents can
        # use these to gauge each other's trustworthiness over time.
        "commitments_active": game_state.get("commitments", []),
        "commitments_log": [
            {
                "power": c.get("power"),
                "text": c.get("text"),
                "phase": c.get("phase") or c.get("resolved_at"),
                "kept": c.get("kept"),
            }
            for c in commitments_log
        ],
    }


def _persona_block(persona: dict) -> str:
    """Persona snapshot baked into the agents_config at game-create time
    (rather than re-fetched per call) so editing/deleting the persona
    later doesn't perturb an in-flight game."""
    label = (persona or {}).get("label") or "Default"
    summary = (persona or {}).get("summary") or ""
    rules = list((persona or {}).get("rules") or [])
    if not rules:
        return (
            f"Persona: {label} — {summary}\n"
            "(No additional rules; play as you see fit.)"
        )
    rules_text = "\n".join(f"  - {r}" for r in rules)
    return (
        f"Persona: {label}\n"
        f"Summary: {summary}\n"
        f"Rules you MUST follow:\n{rules_text}"
    )


class Agent:
    def __init__(self, power, model, persona, api_key=None):
        """Per-power agent.

        Args:
            power: e.g. "ENGLAND". Stable for the life of the game.
            model: LiteLLM model string, e.g. "anthropic/claude-haiku-4-5-...".
            persona: snapshot dict { label, summary, rules: [str] } — already
                resolved from the user's persona collection at game-create.
            api_key: the user's plaintext provider key (decrypted on the
                server-side per call). None falls back to env-var auth so
                local dev still works without BYOK.
        """
        self.power = power
        self.model = model or "anthropic/claude-haiku-4-5-20251001"
        self.persona = persona or {}
        self.api_key = api_key

    async def _stream_and_parse(self, prompt, stream_callback, channel):
        logger.info("LLM call start power=%s channel=%s model=%s", self.power, channel, self.model)
        try:
            kwargs = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "max_tokens": 2500,
            }
            if self.api_key:
                # LiteLLM honours per-call api_key for all the providers we
                # care about (Anthropic, OpenAI, Gemini).
                kwargs["api_key"] = self.api_key
            response = await litellm.acompletion(**kwargs)
            full = ""
            async for chunk in response:
                content = chunk.choices[0].delta.content
                if not content:
                    continue
                full += content
                if stream_callback:
                    await stream_callback(self.power, content, channel)
            blob = _extract_json_blob(full) or {}
            if not blob:
                logger.warning(
                    "LLM returned no parseable JSON power=%s channel=%s raw_len=%d raw_head=%r",
                    self.power, channel, len(full), full[:300],
                )
            else:
                logger.info("LLM call ok power=%s channel=%s raw_len=%d keys=%s", self.power, channel, len(full), list(blob.keys()))
            return blob, full
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM call failed power=%s channel=%s model=%s", self.power, channel, self.model)
            err = f"[Error: {exc}]"
            if stream_callback:
                await stream_callback(self.power, err, channel)
            return {}, err

    async def negotiate(
        self,
        game_state,
        notebook,
        inbox,
        round_index,
        total_rounds,
        other_powers,
        stream_callback=None,
        calls_enabled=False,
        calls_remaining=0,
    ):
        """One round of negotiation. `inbox` = messages received since last round.

        `notebook` is a rendered text block of all saved notes for this power.
        """
        call_block = ""
        if calls_enabled and calls_remaining > 0:
            call_block = f"""

You may also OPTIONALLY initiate up to {calls_remaining} private back-and-forth CALL(S) this phase.
A call is a focused real-time exchange with one other power — use it when a topic needs
multiple turns of clarification or negotiation that a single letter can't capture.
Add calls to the response under "calls": [{{"to": "FRANCE", "topic": "Burgundy split"}}].
"""

        prompt = f"""You are playing classic Diplomacy as {self.power}.

{_persona_block(self.persona)}

Negotiation round {round_index + 1} of {total_rounds}.

Your private notebook (notes you have saved over time):
\"\"\"
{notebook}
\"\"\"

Current board state:
{json.dumps(_slim_state(game_state), indent=2)}

Field guide for the board state:
  - `last_phase_orders`: the full set of orders every power issued in the most recent
    movement phase. This is public knowledge — what actually happened, not what was
    claimed. Cross-reference against any promises made to/by each power.
  - `commitments_active`: public pledges declared this phase, not yet resolved.
  - `commitments_log`: recent past pledges with their `kept` outcome (true/false).
    Use this to gauge how trustworthy a given power has been over time.

Messages received since the last round:
{json.dumps(inbox, indent=2)}

Other powers you can write to: {", ".join(p for p in other_powers if p != self.power)}
{call_block}
Reply with ONE JSON object inside a ```json``` fence:
```json
{{
  "thought": "Your private reasoning for this round.",
  "notes_to_save": [
    "A short note worth remembering on future turns — e.g. a deal, a betrayal, an intent."
  ],
  "messages": [
    {{"to": "FRANCE", "content": "Your message text"}}
  ]{", \"calls\": [{\"to\": \"FRANCE\", \"topic\": \"short subject line\"}]" if calls_enabled and calls_remaining else ""}
}}
```

`notes_to_save` is APPEND-ONLY. Each note you save is kept (oldest dropped only when
the notebook overflows). Save concise, atomic facts — not whole strategy essays.
Examples of good notes:
  - "France committed S1901M: won't move to BUR. Verify in F1901M orders."
  - "Germany ignored my non-aggression proposal — treat as hostile."
  - "Plan: take BEL fall 1901 with F NTH support."
`messages` and `notes_to_save` may both be empty if you have nothing to add."""
        blob, _ = await self._stream_and_parse(prompt, stream_callback, "negotiate")
        return {
            "thought": blob.get("thought", ""),
            "notes_to_save": [n for n in (blob.get("notes_to_save") or []) if isinstance(n, str)],
            "messages": [m for m in (blob.get("messages") or []) if isinstance(m, dict)],
            "calls": [
                c for c in (blob.get("calls") or [])
                if isinstance(c, dict) and c.get("to") and c.get("topic")
            ] if calls_enabled else [],
        }

    async def respond_in_call(
        self,
        game_state,
        notebook,
        thread,
        other_party,
        topic,
        messages_remaining,
        stream_callback=None,
    ):
        """One turn inside an ongoing call."""
        transcript = "\n".join(
            f"{m['from']}: {m['content']}" for m in thread
        ) or "(no exchanges yet — you are the first to speak)"

        prompt = f"""You are playing classic Diplomacy as {self.power}.

{_persona_block(self.persona)}

You are in a PRIVATE CALL with {other_party}.
Topic: "{topic}"
You have at most {messages_remaining} more message(s) you can send in this call.

Conversation so far:
{transcript}

Your private notebook:
\"\"\"
{notebook}
\"\"\"

Current board state:
{json.dumps(_slim_state(game_state), indent=2)}

This is a quick back-and-forth — keep replies short (1–3 sentences). End the call when
the topic is resolved (agreement reached, deadlock, or you have nothing more to add).

Reply with ONE JSON object inside a ```json``` fence:
```json
{{
  "thought": "Brief private reasoning.",
  "notes_to_save": ["Concise notes worth remembering, if any. Append-only."],
  "reply": "Your message to {other_party}. Keep it short.",
  "end_call": false,
  "end_reason": null
}}
```

Set `end_call: true` when you want to close the call (and explain in `end_reason`)."""
        blob, _ = await self._stream_and_parse(prompt, stream_callback, "call")
        reply = (blob.get("reply") or "").strip()
        return {
            "thought": blob.get("thought", ""),
            "notes_to_save": [n for n in (blob.get("notes_to_save") or []) if isinstance(n, str)],
            "reply": reply,
            "end_call": bool(blob.get("end_call")) or len(reply) < 8,
            "end_reason": blob.get("end_reason"),
        }

    async def generate_orders(
        self,
        game_state,
        notebook,
        inbox,
        stream_callback=None,
    ):
        phase = game_state["turn"]["phase"]
        phase_type = game_state["turn"].get("type", "M")

        if phase_type == "A":
            adj = game_state.get("adjustments", {}).get(self.power, 0)
            coast_note = (
                " For multi-coast home centers, include the coast: 'F STP/NC B', "
                "'F STP/SC B', 'F SPA/NC B', 'F SPA/SC B', 'F BUL/EC B', 'F BUL/SC B'."
            )
            if adj > 0:
                action = (
                    f"You must BUILD {adj} new unit(s) in your unoccupied HOME supply centers. "
                    f"Use orders like 'A PAR B' or 'F LON B'.{coast_note}"
                )
            elif adj < 0:
                action = f"You must DISBAND {abs(adj)} of your units. Use orders like 'A PAR D'."
            else:
                action = "You have no builds or disbands. Use an empty orders list."
            phase_block = f"Phase: Winter Adjustments. {action}"
            example_orders = '["A PAR B"]' if adj > 0 else ('["A PAR D"]' if adj < 0 else "[]")
        elif phase_type == "R":
            mine = [d for d in game_state.get("dislodged", []) if d["power"] == self.power]
            if not mine:
                phase_block = "Phase: Retreats. No retreats for your power. Use an empty orders list."
                example_orders = "[]"
            else:
                opts = "\n".join(f"  {d['raw']} can retreat to: {d['options']}" for d in mine)
                phase_block = (
                    "Phase: Retreats. Issue retreat or disband orders for your dislodged units:\n"
                    f"{opts}\nUse 'A LON R YOR' or 'A LON D' (disband)."
                )
                example_orders = '["A LON R YOR"]'
        else:
            orderable = game_state.get("orderable", {}).get(self.power, [])
            phase_block = (
                f"Phase: {game_state['turn']['season']} Movement. Issue MOVE / HOLD / SUPPORT / CONVOY "
                f"orders for each of your units at: {orderable}."
            )
            example_orders = '["A PAR - BUR", "A MAR S A PAR - BUR", "F BRE H"]'

        prompt = f"""You are playing classic Diplomacy as {self.power}.

{_persona_block(self.persona)}

Your private notebook:
\"\"\"
{notebook}
\"\"\"

Current board state:
{json.dumps(_slim_state(game_state), indent=2)}

Messages exchanged between you and other powers this turn:
{json.dumps(inbox, indent=2)}

{phase_block}

Reply with ONE JSON object inside a ```json``` fence:
```json
{{
  "thought": "Your private reasoning for these orders.",
  "notes_to_save": ["Concise notes worth carrying forward. Append-only."],
  "commitments": [
    {{"text": "I will not move into BUR this turn", "type": "no_move", "target": "BUR"}}
  ],
  "orders": {example_orders}
}}
```

`commitments` is optional — declare them only when you want to be held accountable to a specific promise this turn. They will be checked against your actual orders."""
        blob, _ = await self._stream_and_parse(prompt, stream_callback, "orders")
        orders = blob.get("orders") or []
        commitments = blob.get("commitments") or []
        return {
            "thought": blob.get("thought", ""),
            "notes_to_save": [n for n in (blob.get("notes_to_save") or []) if isinstance(n, str)],
            "orders": [o for o in orders if isinstance(o, str)],
            "commitments": [c for c in commitments if isinstance(c, dict) and c.get("text")],
        }
