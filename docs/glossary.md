# Diplomacy AI — Glossary

Canonical terminology for the project. Use these terms in code, in events, in
URLs, and in conversation. **Do not invent synonyms** — if a concept doesn't fit
one of these words, the glossary needs updating, not the code.

---

## Game-state hierarchy

```
GAME
  └─ YEAR           ── 1901, 1902, 1903, …
       └─ SEASON     ── Spring · Fall · Winter   (descriptive only — not addressable)
            └─ PHASE  ── Movement · Retreats · Adjustments
                 │       addressable as a short code: S1901M, S1901R,
                 │       F1901M, F1901R, W1901A
                 │
                 └─ STAGE  ── the sub-step we orchestrate
                      │   inside each phase. Pipelined: Negotiation → Order →
                      │   Resolution → Dispatch.
                      │
                      ├─ Negotiation Stage   (Movement phases only)
                      │     └─ ROUND  ── 1, 2, …, N  (configurable)
                      │           └─ EVENTS (letters, calls, thoughts, commitments)
                      │
                      ├─ Order Stage
                      │     └─ EVENTS (each power's submitted orders)
                      │
                      ├─ Resolution Stage
                      │     └─ EVENTS (engine adjudication: resolved, sc_changes)
                      │
                      └─ Dispatch Stage
                            └─ EVENTS (headlines / dispatch narrative)
```

## Term-by-term definitions

### `game`
One match. Has a `game_id`, an agents config (which model + policy plays which
power), a start time, an eventual winner-or-draw, and a complete event history.

### `year`
The Diplomacy calendar year, e.g. `1901`. Descriptive label only — never
appears in URLs or as a primary identifier. Used inside the phase short code
(`S1901M`).

### `season`
One of `Spring`, `Fall`, `Winter`. **There is no Summer or Autumn** in
Diplomacy — retreats that fall chronologically in summer/autumn are still
labelled by the preceding movement season. Descriptive label only; not
addressable.

### `phase`
The atomic, addressable engine step. Three types: `Movement`, `Retreats`,
`Adjustments`. Identified by a short code that encodes season + year + type:

| Short code | Long form                | Type |
| ---------- | ------------------------ | ---- |
| S1901M     | SPRING 1901 MOVEMENT     | M    |
| S1901R     | SPRING 1901 RETREATS     | R    |
| F1901M     | FALL 1901 MOVEMENT       | M    |
| F1901R     | FALL 1901 RETREATS       | R    |
| W1901A     | WINTER 1901 ADJUSTMENTS  | A    |

We run python-diplomacy with `DONT_SKIP_PHASES` enabled so every phase fires
and is logged, even when nothing happens (no dislodgements → quiet retreat).

### `stage`
A sub-step within a phase that we orchestrate. Each phase runs its stages in
this order:

1. **Negotiation Stage** — only in Movement phases. Contains 1..N rounds of
   private letters, calls, internal thoughts, and public commitments.
2. **Order Stage** — every power submits its orders for this phase.
3. **Resolution Stage** — the engine adjudicates orders. Emits `resolved`
   event with SC changes, dislodgements, and per-order results.
4. **Dispatch Stage** — produces the narrative summary (headlines or quiet
   flavor text) that closes the phase.

### `round`
A sub-step of the Negotiation stage. **Only meaningful inside negotiations.**
1-indexed in URLs (`/rounds/1` = first round); 0-indexed internally in code.

### `event`
The atomic unit of game history. Append-only. Has a fixed envelope:

```jsonc
{
  "ts":      "2026-05-18T12:34:56Z",
  "game_id": "1777521929_b43d2420",
  "phase":   "S1901M",         // short code; required
  "stage":   "negotiation",    // negotiation | order | resolution | dispatch
  "round":   1,                // present only when stage = negotiation
  "kind":    "message",        // see kinds table below
  "data":    { …kind-specific payload… }
}
```

### `kind` (event types)

| kind            | Stage        | Description |
| --------------- | ------------ | ----------- |
| `phase_start`   | (boundary)   | Emitted at the start of a phase. |
| `thought`       | negotiation  | Agent's private reasoning for the round. |
| `message`       | negotiation  | A private letter from one power to another. |
| `call`          | negotiation  | A back-and-forth synchronous conversation. |
| `commitment`    | negotiation  | A public pledge declared this phase. |
| `order`         | order        | Orders submitted by a power. |
| `resolved`      | resolution   | Engine output: SC changes + per-order results. |
| `dispatch`      | dispatch     | Narrative headlines that close a phase. |
| `round_marker`  | negotiation  | UI-only divider between negotiation rounds. |

### `dispatch`
The closing event of every phase. Has three flavors keyed by `phase_type`
(`M` / `R` / `A`):

- **Movement Dispatch** — battle headlines (clashes, captures, dislodgements).
- **Retreat Dispatch** — retreat headlines, or a quiet flavor line if no
  dislodgements occurred.
- **Adjustment Dispatch** — build/disband summary, or a quiet flavor line if
  no powers built or removed forces.

When a phase has no actionable events, the Dispatch carries a hardcoded
atmospheric line instead of an empty list. Replays never see "nothing here."

### Banished terms

- **`turn`** — too vague; overlaps `phase`. Reserved for casual UI labels
  only ("Run Negotiations" button reading "Run Turn" is fine; `turn_log` in
  code is not).
- **`Summer`, `Autumn`** — chronologically real, but not Diplomacy seasons.
  Retreats are labelled by the preceding movement season.
- **`round_summary`** — old name for `dispatch`. Renamed for accuracy
  (it's phase-level, not round-level).

---

## URL conventions

```
GET  /api/games
GET  /api/games/<game_id>
GET  /api/games/<game_id>/events?phase=&stage=&kind=&round=&power=&since=
GET  /api/games/<game_id>/phases
GET  /api/games/<game_id>/phases/<phase>
GET  /api/games/<game_id>/phases/<phase>/rounds/<round>
GET  /api/games/<game_id>/phases/<phase>/stages/<stage>
```

- Phase in URLs always uses the short code: `S1901M`, `W1901A`, etc.
- Rounds are 1-indexed in URLs (translated to/from 0-indexed internal storage
  at the API boundary).
- Stage values: `negotiation`, `order`, `resolution`, `dispatch`.

## Quick rules of thumb

- A `phase` is what the engine sees; a `stage` is what we orchestrate inside it.
- A `round` only exists inside a Negotiation stage.
- Every phase ends in a `dispatch` event, even quiet ones.
- Events have one `kind`; `data` carries kind-specific payload.
- Color is reserved for **country identity**. Type/status is signalled by icon.

---

_This document is canonical. If a piece of code or a piece of UI uses
different terminology, the code/UI is wrong, not the glossary._
