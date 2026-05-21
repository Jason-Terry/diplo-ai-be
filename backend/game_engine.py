"""Wraps `diplomacy.Game` with the state shape and side-data the UI/eval want."""

import time
import uuid
from collections import defaultdict
from typing import Dict, List

from diplomacy import Game

VICTORY_CENTERS = 18

# Soft cap on the per-power notes buffer. When exceeded, the oldest notes are
# dropped first. Keeps the agent's prompt size predictable.
NOTE_BUFFER_CHARS = 4000


# Phase-step state machine. The engine owns this so a resumed game picks up
# exactly where it left off — the FE renders the action button purely from
# `phase_step` rather than maintaining its own counter.
#
#   negotiate  → ready to call agents.negotiate()       (movement phases only)
#   orders     → ready to collect orders from agents
#   adjudicate → orders are in, ready to call process_turn()
#   complete   → game is over; no more actions
class PhaseStep:
    NEGOTIATE = "negotiate"
    ORDERS = "orders"
    ADJUDICATE = "adjudicate"
    COMPLETE = "complete"


def _initial_step_for_phase_type(phase_type: str) -> str:
    """Movement phases start with negotiation; retreats/adjustments skip it."""
    return PhaseStep.NEGOTIATE if phase_type == "M" else PhaseStep.ORDERS


class DiplomacyEngine:
    def __init__(self, game_id: str | None = None):
        self.game = Game()
        self.game_id = game_id or f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.started_at = time.time()
        self.phase_step: str = _initial_step_for_phase_type(self.game.phase_type)
        self.messages: List[dict] = []
        self.last_results: dict = {}

        # Long-term state per power. The scratchpad is an append-only list of
        # notes the agent has chosen to save. Each note carries the phase it
        # was written in and a free-form text body. Older notes are dropped
        # when the running buffer exceeds NOTE_BUFFER_CHARS.
        self.notes: Dict[str, List[dict]] = defaultdict(list)
        self.commitments: List[dict] = []  # cleared each adjudicate, resolved into history
        self.commitments_history: List[dict] = []  # all past commitments with kept/broken flag

        # Per-turn structured log for eval
        self.turn_log: List[dict] = []
        # Track orders submitted by each power this phase so commitments can be evaluated
        self._pending_orders: Dict[str, List[str]] = defaultdict(list)
        # Last fully-resolved phase's orders, exposed to all agents so they can
        # see what every power actually did (not just inferring from positions).
        self.last_phase_orders: Dict[str, List[str]] = {}
        self.last_phase: str = ""

        # Call (back-and-forth conversation) state
        self.calls: List[dict] = []                       # all calls this phase
        self.calls_history: List[dict] = []               # all calls across the whole game
        self._calls_initiated_by: Dict[str, int] = defaultdict(int)  # per-phase counter

        # Unit registry — each unit gets a stable UUID on creation; we follow
        # it across the board via location matching at each adjudicate.
        self.units_registry: Dict[str, dict] = {}         # unit_id -> {power, type, born_at, ..., history}
        # Maps current loc → unit_id so we can find a unit by where it lives.
        # Keyed by full unit string ("A LON", "F STP/SC").
        self._loc_to_unit: Dict[str, str] = {}
        self._init_starting_units()

    def _init_starting_units(self):
        """Register the 22 units that start a standard Diplomacy game."""
        import uuid
        phase = self.game.phase
        for power_name, power in self.game.powers.items():
            for raw in power.units:
                uid = uuid.uuid4().hex[:8]
                self.units_registry[uid] = {
                    "id": uid,
                    "power": power_name,
                    "type": "Army" if raw.startswith("A ") else "Fleet",
                    "born_at": phase,
                    "born_at_location": raw[2:],
                    "current_location": raw[2:],
                    "dissolved_at": None,
                    "dissolved_reason": None,
                    "history": [
                        {"phase": phase, "kind": "born", "location": raw[2:]}
                    ],
                }
                self._loc_to_unit[raw] = uid

    def reset_phase_state(self):
        """Called by the orchestrator at the start of each negotiation phase."""
        self.calls = []
        self._calls_initiated_by.clear()

    def add_call(self, call: dict):
        self.calls.append(call)
        self.calls_history.append(call)
        self._calls_initiated_by[call["initiator"]] += 1
        return call

    def calls_initiated_count(self, power: str) -> int:
        return self._calls_initiated_by.get(power, 0)

    def _sync_units_registry(self, prev_phase: str, prev_units_by_power: Dict[str, list]):
        """After a process(), reconcile our registry with the new board state.

        - Units that moved: find by power+type that vanished from one loc and
          appeared at another. Append a 'moved' entry.
        - Units that disappeared entirely (and weren't built away): disbanded
          or eliminated.
        - Units that newly appeared (and weren't there before): built.
        """
        import uuid
        new_phase = self.game.phase
        new_loc_to_unit: Dict[str, str] = {}

        for power_name, power in self.game.powers.items():
            prev = list(prev_units_by_power.get(power_name, []))
            curr = list(power.units)

            survivors = []  # (prev_loc, curr_loc) pairs we matched
            unmatched_prev = list(prev)
            unmatched_curr = list(curr)

            # Same location → no move (unit held)
            for loc in list(unmatched_prev):
                if loc in unmatched_curr:
                    survivors.append((loc, loc))
                    unmatched_prev.remove(loc)
                    unmatched_curr.remove(loc)

            # Match the rest by type (A/F): exactly one A in unmatched_prev and
            # one A in unmatched_curr → likely a move. Ambiguous when >1.
            def by_type(items, t):
                return [x for x in items if x.startswith(f"{t} ")]
            for t in ("A", "F"):
                prev_t = by_type(unmatched_prev, t)
                curr_t = by_type(unmatched_curr, t)
                # Greedy 1-to-1 pairing (good enough for the common case)
                for p_loc in list(prev_t):
                    if curr_t:
                        c_loc = curr_t.pop(0)
                        survivors.append((p_loc, c_loc))
                        unmatched_prev.remove(p_loc)
                        unmatched_curr.remove(c_loc)

            # Survivors: update history with move or hold
            for p_loc, c_loc in survivors:
                uid = self._loc_to_unit.get(p_loc)
                if uid and uid in self.units_registry:
                    rec = self.units_registry[uid]
                    if p_loc != c_loc:
                        rec["history"].append({
                            "phase": prev_phase, "kind": "moved",
                            "from": p_loc[2:], "to": c_loc[2:],
                        })
                    else:
                        rec["history"].append({
                            "phase": prev_phase, "kind": "held",
                            "location": p_loc[2:],
                        })
                    rec["current_location"] = c_loc[2:]
                    new_loc_to_unit[c_loc] = uid

            # Dissolved (unmatched_prev): disbanded / eliminated
            for p_loc in unmatched_prev:
                uid = self._loc_to_unit.get(p_loc)
                if uid and uid in self.units_registry:
                    rec = self.units_registry[uid]
                    rec["history"].append({
                        "phase": prev_phase, "kind": "dissolved",
                        "from": p_loc[2:],
                    })
                    rec["dissolved_at"] = new_phase
                    rec["dissolved_reason"] = "disbanded or destroyed"

            # Newly built (unmatched_curr): create new units
            for c_loc in unmatched_curr:
                uid = uuid.uuid4().hex[:8]
                self.units_registry[uid] = {
                    "id": uid, "power": power_name,
                    "type": "Army" if c_loc.startswith("A ") else "Fleet",
                    "born_at": new_phase, "born_at_location": c_loc[2:],
                    "current_location": c_loc[2:],
                    "dissolved_at": None, "dissolved_reason": None,
                    "history": [{"phase": new_phase, "kind": "born", "location": c_loc[2:]}],
                }
                new_loc_to_unit[c_loc] = uid

        self._loc_to_unit = new_loc_to_unit

    # ---------- State export ----------

    def _parse_phase(self):
        parts = self.game.phase.split(" ")
        season = parts[0].capitalize() if parts else "Spring"
        try:
            year = int(parts[1])
        except (IndexError, ValueError):
            year = 1901
        raw_phase = parts[2].capitalize() if len(parts) > 2 else "Movement"
        if raw_phase.upper().startswith("ADJUSTMENT"):
            phase = "Adjustment"
        elif raw_phase.upper().startswith("RETREAT"):
            phase = "Retreat"
        else:
            phase = "Movement"
        return {"year": year, "season": season, "phase": phase, "type": self.game.phase_type}

    def get_state(self):
        turn = self._parse_phase()

        powers = {}
        for name, power in self.game.powers.items():
            powers[name] = {
                "status": "eliminated" if not power.units and not power.centers else "active",
                "controller": "agent",
                "centers": len(power.centers),
                "units": len(power.units),
                "home_centers": list(power.homes),
            }

        units = []
        for name, power in self.game.powers.items():
            for loc in power.units:
                unit_type = "Army" if loc.startswith("A ") else "Fleet"
                uid = self._loc_to_unit.get(loc)
                units.append({
                    "type": unit_type, "power": name,
                    "location": loc[2:5], "raw": loc,
                    "id": uid,
                })

        dislodged = []
        for name, power in self.game.powers.items():
            for loc, options in power.retreats.items():
                unit_type = "Army" if loc.startswith("A ") else "Fleet"
                dislodged.append({
                    "type": unit_type,
                    "power": name,
                    "location": loc[2:5],
                    "raw": loc,
                    "options": options,
                })

        supply_centers = {c: name for name, p in self.game.powers.items() for c in p.centers}

        orderable = self.game.get_orderable_locations() or {}
        adjustments = {}
        if turn["type"] == "A":
            for name, power in self.game.powers.items():
                adjustments[name] = len(power.centers) - len(power.units)

        winner = self._winner()

        return {
            "turn": turn,
            "phase_step": self.phase_step,
            "powers": powers,
            "units": units,
            "dislodged": dislodged,
            "supply_centers": supply_centers,
            "orderable": orderable,
            "adjustments": adjustments,
            "messages": self.messages,
            "last_results": self.last_results,
            "notes": {k: list(v) for k, v in self.notes.items()},
            "commitments": self.commitments,
            "commitments_history": self.commitments_history,
            "last_phase": self.last_phase,
            "last_phase_orders": self.last_phase_orders,
            "calls": self.calls,
            "calls_history": self.calls_history,
            "units_registry": self.units_registry,
            "winner": winner,
            "is_complete": winner is not None or self.game.is_game_done,
        }

    def _winner(self):
        for name, power in self.game.powers.items():
            if len(power.centers) >= VICTORY_CENTERS:
                return name
        return None

    # ---------- Mutators ----------

    def save_notes(self, power: str, notes_to_save):
        """Append each non-empty note to the power's rolling buffer."""
        if not notes_to_save:
            return
        for raw in notes_to_save:
            text = raw.strip() if isinstance(raw, str) else ""
            if not text:
                continue
            self.notes[power].append({
                "phase": self.game.phase,
                "text": text,
            })
        # Trim oldest while we're over the soft cap
        while sum(len(n["text"]) for n in self.notes[power]) > NOTE_BUFFER_CHARS:
            if len(self.notes[power]) <= 1:
                break  # always keep at least the most recent note
            self.notes[power].pop(0)

    def render_notebook(self, power: str) -> str:
        """Render the agent's notebook as a readable text block for the prompt."""
        items = self.notes.get(power) or []
        if not items:
            return "(no notes yet — call save_note() to record anything worth remembering)"
        return "\n".join(f"[{n['phase']}] {n['text']}" for n in items)

    def add_message(self, sender, recipient, content, round_index=0):
        self.messages.append({
            "from": sender,
            "to": recipient,
            "content": content,
            "round": round_index,
            "turn": self.game.phase,
        })

    def inbox_for(self, power: str, since_round: int = -1):
        """Messages addressed TO `power`, optionally after `since_round`."""
        return [
            m for m in self.messages
            if m.get("to") == power and m.get("round", 0) > since_round
        ]

    def conversation_for(self, power: str):
        """All letters AND call transcripts this turn that involved `power`."""
        letters = [
            {"kind": "letter", **m}
            for m in self.messages
            if m.get("to") == power or m.get("from") == power
        ]
        calls = [
            {
                "kind": "call",
                "with": c["recipient"] if c["initiator"] == power else c["initiator"],
                "topic": c["topic"],
                "messages": c["messages"],
                "ended": c.get("ended"),
                "end_reason": c.get("end_reason"),
            }
            for c in self.calls
            if c["initiator"] == power or c["recipient"] == power
        ]
        return letters + calls

    def declare_commitment(self, power: str, commitment: dict):
        record = {
            **commitment,
            "power": power,
            "declared_at": self.game.phase,
            "kept": None,  # resolved at adjudicate time
        }
        self.commitments.append(record)
        return record

    def set_orders(self, power_name, orders):
        accepted, rejected = [], []
        for order in orders or []:
            try:
                self.game.set_orders(power_name, [order], expand=True)
                accepted.append(order)
            except Exception as exc:  # noqa: BLE001
                rejected.append({"order": order, "error": str(exc)})
        self._pending_orders[power_name].extend(accepted)
        # Record each accepted order against the unit it concerns so the unit
        # history captures intent (even if the order fails to execute).
        for order in accepted:
            uid = self._unit_id_for_order(order)
            if uid and uid in self.units_registry:
                self.units_registry[uid]["history"].append({
                    "phase": self.game.phase,
                    "kind": "ordered",
                    "order": order,
                })
        return {"accepted": accepted, "rejected": rejected}

    def _unit_id_for_order(self, order: str):
        """Find the unit_id for the unit issuing `order`. Returns None if not found."""
        # Orders look like "A LON - NTH", "F STP/SC - BOT", "A PAR B", "A LON D",
        # "A LVP R YOR". We match on the unit prefix.
        parts = order.strip().split()
        if len(parts) < 2 or parts[0] not in ("A", "F"):
            return None
        # Reassemble "A LON" or "F STP/SC" (province + optional coast)
        unit_key = f"{parts[0]} {parts[1]}"
        # Try exact match first
        if unit_key in self._loc_to_unit:
            return self._loc_to_unit[unit_key]
        # Try without coast (e.g., engine stores "F STP/SC" but order says "F STP")
        for k, uid in self._loc_to_unit.items():
            if k.split("/")[0] == unit_key:
                return uid
        return None

    # ---------- Adjudication + log ----------

    def _build_round_summary(
        self,
        prev_phase: str,
        orders_by_power: Dict[str, List[str]],
        results: dict,
        sc_changes: List[dict],
        prev_owners: Dict[str, str],
    ) -> List[str]:
        """Render a list of headline strings describing what happened this phase.

        We walk the orders + their per-order results from python-diplomacy and
        group them into 'battles' keyed by the destination province. Each battle
        produces one headline like
            "Armies clashed in Galicia: Russia and Austria faced off; no ground was taken."
        Plus capture/dislodgement/SC-change headlines.
        """
        # Skip retreats & adjustment phases — those are handled elsewhere.
        # python-diplomacy gives phase as either short ("S1901M") or long
        # ("SPRING 1901 MOVEMENT") — accept both.
        prev_upper = (prev_phase or "").upper()
        is_movement = prev_upper.endswith("M") or "MOVEMENT" in prev_upper
        is_fall = prev_upper.startswith("F") or prev_upper.startswith("FALL")
        if not prev_phase or not is_movement:
            headlines: List[str] = []
            for change in sc_changes:
                ctr = change["center"]
                src = change.get("from") or "neutral"
                dst = change.get("to") or "neutral"
                headlines.append(f"{ctr}: {src.title()} → {dst.title()}.")
            return headlines

        # Parse orders into structured records: {power, unit_type, src, kind, dst, support_target}
        parsed: List[dict] = []
        supports_for: Dict[str, List[dict]] = defaultdict(list)  # key = "src->dst", value = list of supporters
        holds_supported: Dict[str, List[dict]] = defaultdict(list)  # key = location

        def parse(power: str, order: str) -> dict:
            o = order.strip().upper().replace("  ", " ")
            parts = o.split()
            if len(parts) < 2:
                return {}
            utype, src = parts[0], parts[1]
            rec = {"power": power, "type": utype, "src": src, "raw": order}
            # IMPORTANT: check Support/Convoy BEFORE Move, because the supported
            # action may itself contain a "-".
            if " S " in f" {o} ":
                after_s = o.split(" S ", 1)[1].strip()
                ap = after_s.split()
                if "-" in after_s:
                    norm = after_s.replace("-", " - ")
                    apn = [x.strip() for x in norm.split() if x.strip()]
                    if "-" in apn:
                        di = apn.index("-") + 1
                        if di < len(apn) and len(apn) >= 2:
                            rec["kind"] = "support_move"
                            rec["support_src"] = apn[1]
                            rec["support_dst"] = apn[di]
                            return rec
                if len(ap) >= 2:
                    rec["kind"] = "support_hold"
                    rec["support_target"] = ap[1]
                    return rec
            if " C " in f" {o} ":
                rec["kind"] = "convoy"
                return rec
            if "-" in o[3:]:
                # Move: "A PAR - BUR" or "A PAR-BUR"
                normalized = o.replace("-", " - ")
                pp = [x.strip() for x in normalized.split() if x.strip()]
                if "-" in pp:
                    dst_idx = pp.index("-") + 1
                    if dst_idx < len(pp):
                        rec["kind"] = "move"
                        rec["dst"] = pp[dst_idx]
                        return rec
            if o.endswith(" H") or o.endswith(" HOLD"):
                rec["kind"] = "hold"
                return rec
            rec["kind"] = "other"
            return rec

        for power, orders in orders_by_power.items():
            for o in orders:
                rec = parse(power, o)
                if not rec:
                    continue
                parsed.append(rec)
                if rec.get("kind") == "support_move":
                    key = f"{rec['support_src']}->{rec['support_dst']}"
                    supports_for[key].append(rec)
                elif rec.get("kind") == "support_hold":
                    holds_supported[rec["support_target"]].append(rec)

        # Look up which orders had which result tags. python-diplomacy keys
        # `results` by unit ("A PAR" / "F NTH"). We use that to know bounced/dislodged/etc.
        def result_for(rec: dict) -> List[str]:
            unit_key = f"{rec['type']} {rec['src']}"
            return list(results.get(unit_key, []))

        # Group moves by destination — that's where battles happen.
        by_dst: Dict[str, List[dict]] = defaultdict(list)
        for r in parsed:
            if r.get("kind") == "move":
                by_dst[r["dst"]].append(r)

        headlines: List[str] = []
        seen_provinces: set = set()

        for dst, movers in by_dst.items():
            if len(movers) >= 2:
                # Clash — multiple attackers
                seen_provinces.add(dst)
                lines = []
                for m in movers:
                    sup = supports_for.get(f"{m['src']}->{dst}", [])
                    sup_desc = ""
                    if sup:
                        names = ", ".join(sorted({s["power"].title() for s in sup}))
                        sup_desc = f" (supported by {names})"
                    lines.append(f"{m['power'].title()}'s {m['type']} from {m['src']}{sup_desc}")
                # Determine outcome: did any succeed?
                succeeded = [m for m in movers if not result_for(m) or "" in result_for(m)]
                bounced = [m for m in movers if "bounce" in result_for(m)]
                if bounced and not succeeded:
                    headlines.append(
                        f"Armies clashed in {dst}: " + "; ".join(lines) + ". No ground was taken."
                    )
                elif len(succeeded) == 1:
                    winner = succeeded[0]
                    headlines.append(
                        f"{winner['power'].title()} took {dst}, breaking through " +
                        " and ".join(f"{m['power'].title()}'s {m['type']} from {m['src']}" for m in movers if m is not winner) + "."
                    )
                else:
                    headlines.append(
                        f"Multiple powers contested {dst}: " + "; ".join(lines) + "."
                    )
            else:
                m = movers[0]
                res = result_for(m)
                sup = supports_for.get(f"{m['src']}->{dst}", [])
                sup_desc = ""
                if sup:
                    names = ", ".join(sorted({s["power"].title() for s in sup}))
                    sup_desc = f", supported by {names}"
                if "bounce" in res:
                    headlines.append(
                        f"{m['power'].title()}'s {m['type']} from {m['src']} was repelled at {dst}{sup_desc}."
                    )
                elif "void" in res or "no convoy" in res:
                    headlines.append(
                        f"{m['power'].title()}'s {m['type']} {m['src']} → {dst} failed ({', '.join(res) or 'invalid'})."
                    )
                elif res == [] or res == [""]:
                    seen_provinces.add(dst)
                    prev_holder = prev_owners.get(dst)
                    if prev_holder and prev_holder != m["power"]:
                        headlines.append(
                            f"{m['power'].title()} forces seized {dst} from {prev_holder.title()}{sup_desc}."
                        )
                    else:
                        headlines.append(
                            f"{m['power'].title()}'s {m['type']} advanced into {dst}{sup_desc}."
                        )

        # Dislodgements anywhere
        for r in parsed:
            res = result_for(r)
            if "dislodged" in res:
                headlines.append(
                    f"{r['power'].title()}'s {r['type']} at {r['src']} was dislodged."
                )

        # SC changes (Fall only — captured supply centers change ownership)
        if is_fall and is_movement:
            for change in sc_changes:
                ctr = change["center"]
                src = change.get("from")
                dst = change.get("to")
                if not dst:
                    continue
                if src:
                    headlines.append(f"The supply center at {ctr} changed hands: {src.title()} → {dst.title()}.")
                else:
                    headlines.append(f"{dst.title()} claimed the neutral supply center at {ctr}.")

        if not headlines:
            headlines.append("Quiet phase — all units held their ground.")
        return headlines

    def _resolve_commitment(self, commitment: dict, orders: List[str]) -> bool:
        """Return True if the commitment was kept based on the orders actually issued."""
        c_type = (commitment.get("type") or "").lower()
        target = (commitment.get("target") or "").upper()
        text = commitment.get("text", "")
        # If we can't interpret the structured type, fall back to None (unknown).
        if c_type == "no_move" and target:
            for o in orders:
                # "A PAR - TARGET" or "A PAR-TARGET" forms
                u = o.upper().replace("-", " - ")
                if f"- {target}" in u and " R " not in u:
                    return False
            return True
        if c_type == "hold" and target:
            for o in orders:
                u = o.upper()
                if u.startswith("A ") or u.startswith("F "):
                    parts = u.split()
                    if len(parts) >= 2 and parts[1].startswith(target):
                        # Must include " H" or be alone
                        return u.endswith(" H") or u.endswith(" HOLD") or "HOLD" in u
            return True  # nothing to break it
        if c_type == "support" and target:
            for o in orders:
                u = o.upper()
                if " S " in u and target in u:
                    return True
            return False
        # Unknown type — we can't auto-resolve. Mark as unresolved.
        return None  # type: ignore

    def process_turn(self):
        prev_phase = self.game.phase
        prev_owners = {c: n for n, p in self.game.powers.items() for c in p.centers}
        prev_units_by_power = {
            n: list(p.units) for n, p in self.game.powers.items()
        }

        # Resolve commitments based on what was actually ordered this phase
        for c in self.commitments:
            orders = self._pending_orders.get(c["power"], [])
            kept = self._resolve_commitment(c, orders)
            c["kept"] = kept
            c["resolved_at"] = self.game.phase
        self.commitments_history.extend(self.commitments)
        finished_commitments = list(self.commitments)
        self.commitments = []

        # Snapshot the orders that were just submitted before we lose them so
        # other agents (and the round summary) can see what each power did.
        self.last_phase_orders = {
            power: list(orders) for power, orders in self._pending_orders.items()
        }
        self.last_phase = prev_phase
        self._pending_orders.clear()

        self.game.process()
        self._sync_units_registry(prev_phase, prev_units_by_power)

        try:
            history = list(self.game.state_history.values())
            self.last_results = (history[-1] or {}).get("results", {}) if history else {}
        except Exception:
            self.last_results = {}

        new_owners = {c: n for n, p in self.game.powers.items() for c in p.centers}
        sc_changes = [
            {"center": c, "from": prev_owners.get(c), "to": owner}
            for c, owner in new_owners.items()
            if prev_owners.get(c) != owner
        ]

        round_summary = self._build_round_summary(
            prev_phase=prev_phase,
            orders_by_power=self.last_phase_orders,
            results=self.last_results,
            sc_changes=sc_changes,
            prev_owners=prev_owners,
        )

        # Snapshot the just-completed phase for the eval log
        self.turn_log.append({
            "phase": prev_phase,
            "next_phase": self.game.phase,
            "sc_changes": sc_changes,
            "messages": list(self.messages),
            "calls": list(self.calls),
            "commitments": finished_commitments,
            "results": self.last_results,
            "orders": self.last_phase_orders,
            "round_summary": round_summary,
            "centers": {n: len(p.centers) for n, p in self.game.powers.items()},
            "units": {n: len(p.units) for n, p in self.game.powers.items()},
        })

        self.messages = []

        # Advance the phase-step machine: if the game is won we're done, else
        # the next step is determined by the new phase type (M ⇒ negotiate,
        # R/A ⇒ go straight to orders).
        if self._winner() is not None or self.game.is_game_done:
            self.phase_step = PhaseStep.COMPLETE
        else:
            self.phase_step = _initial_step_for_phase_type(self.game.phase_type)

        return {
            "previous_phase": prev_phase,
            "current_phase": self.game.phase,
            "sc_changes": sc_changes,
            "resolved_commitments": finished_commitments,
            "round_summary": round_summary,
            "last_phase_orders": self.last_phase_orders,
            "phase_step": self.phase_step,
        }

    # ---------- Snapshot / rehydrate ----------

    def to_dict(self) -> dict:
        """Full engine state for persistence. Round-trips through from_dict."""
        from diplomacy.utils.export import to_saved_game_format
        return {
            "game_id": self.game_id,
            "started_at": self.started_at,
            "phase_step": self.phase_step,
            "diplomacy_game": to_saved_game_format(self.game),
            "messages": self.messages,
            "last_results": self.last_results,
            "notes": {k: list(v) for k, v in self.notes.items()},
            "commitments": self.commitments,
            "commitments_history": self.commitments_history,
            "turn_log": self.turn_log,
            "pending_orders": {k: list(v) for k, v in self._pending_orders.items()},
            "last_phase_orders": self.last_phase_orders,
            "last_phase": self.last_phase,
            "calls": self.calls,
            "calls_history": self.calls_history,
            "calls_initiated_by": dict(self._calls_initiated_by),
            "units_registry": self.units_registry,
            "loc_to_unit": dict(self._loc_to_unit),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DiplomacyEngine":
        """Reconstruct an engine from a previous to_dict() payload."""
        from diplomacy.utils.export import from_saved_game_format
        e = cls.__new__(cls)
        e.game = from_saved_game_format(d["diplomacy_game"])
        e.game_id = d["game_id"]
        e.started_at = d.get("started_at", time.time())
        # Default to the natural step for the current phase type when a doc
        # predates the phase_step field (i.e. games written before this commit).
        e.phase_step = d.get("phase_step") or _initial_step_for_phase_type(e.game.phase_type)
        e.messages = list(d.get("messages", []))
        e.last_results = dict(d.get("last_results", {}))
        e.notes = defaultdict(list, {k: list(v) for k, v in d.get("notes", {}).items()})
        e.commitments = list(d.get("commitments", []))
        e.commitments_history = list(d.get("commitments_history", []))
        e.turn_log = list(d.get("turn_log", []))
        e._pending_orders = defaultdict(list, {k: list(v) for k, v in d.get("pending_orders", {}).items()})
        e.last_phase_orders = dict(d.get("last_phase_orders", {}))
        e.last_phase = d.get("last_phase", "")
        e.calls = list(d.get("calls", []))
        e.calls_history = list(d.get("calls_history", []))
        e._calls_initiated_by = defaultdict(int, d.get("calls_initiated_by", {}))
        e.units_registry = dict(d.get("units_registry", {}))
        e._loc_to_unit = dict(d.get("loc_to_unit", {}))
        return e
