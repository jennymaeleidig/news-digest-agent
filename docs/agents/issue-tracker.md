# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Two kinds of tickets

Never write a bare "ticket" without saying which kind. This tracker holds **implementation tickets** and **wayfinding tickets**, and they live side by side under the same `.scratch/<effort>/issues/` directory — so the discriminating marker below is how you tell them apart at a glance.

- **Implementation tickets** are the build work of an effort. Created by `ticket-creator` (via the `/to-impl-tickets` prompt), one file per `.scratch/<effort>/issues/<NN>-<slug>.md`. They carry a `Status:` line (`open` / `claimed` / `resolved`), a `Blocked by:` line, a `What to build` statement, and `- [ ]` acceptance criteria — and **no `Type:` line**. Built by `ticket-implementer`, closed by `ticket-closer`; terminal `resolved`. They survive the effort: kept by `journey-logger`, retired by `effort-closer`.
- **Wayfinding tickets** (decision tickets) resolve a question on the way to an effort's destination; they are **never implemented**. Created by the `/wayfind` prompt (wrapping the `wayfinder` skill), one file per `.scratch/<effort>/issues/<NN>-<slug>.md`. A `Type:` line names the kind (`research` / `prototype` / `grilling` / `task`); the body is a `## Question`. Each is resolved and closed by wayfinding as the journey advances. They are **journey** — deleted by `journey-logger` once their decisions are folded into `spec.md`.

**How to tell them apart** — with one look at the file:

- A `Type:` line ⇒ **wayfinding ticket**.
- No `Type:` line, plus `Status:` / `Blocked by:` / `What to build` / `- [ ]` ⇒ **implementation ticket**.

## Conventions

- One effort per directory: `.scratch/<effort>/`
- The spec is `.scratch/<effort>/spec.md`
- Implementation issues are one file per implementation ticket at `.scratch/<effort>/issues/<NN>-<slug>.md`, numbered from `01` in dependency order (blockers first) — never a single combined tickets file
- Current state is recorded as a `Status:` line near the top of each issue file
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## Status

Both ticket kinds carry a `Status:` line and share the terminal marker: **`Status: resolved` means done**. What implementation tickets add on top of that is a pre-terminal lifecycle.

**Implementation tickets** run the full lifecycle (`ticket-creator`, `ticket-implementer`, `ticket-closer`, `effort-closer`): `open` → `claimed` → `resolved`. The `Status:` line holds exactly one of three states:

- `open` — a published, unclaimed ticket. A missing `Status:` line also means `open`.
- `claimed` — a session has picked the ticket up. Set before any work so concurrent sessions skip it.
- `resolved` — terminal: hand-off and close are the same state. Never `done`.

Lifecycle: publish → `open`; claim → `claimed`; hand-off/close → `resolved`.

- `effort-specifier` publishes the **spec** at `.scratch/<effort>/spec.md` — no `Status:` line; publishing is the whole signal.
- `ticket-creator` publishes one **implementation ticket** file per `.scratch/<effort>/issues/<NN>-<slug>.md`, each `Status: open`.

**Wayfinding tickets** use the same vocabulary and the same concurrency guard as implementation tickets: a session claims the frontier ticket it picks up (`Status: claimed`) before working, and resolving it sets `Status: resolved`. The completion signal is identical — **`Status: resolved`** — reached when wayfinding appends the `## Answer` to the body and points the map's Decisions-so-far at it (the answer is the proof; there is no separate closer). Never `done`.

## When a skill says "publish to the issue tracker"

- `effort-specifier` → write `.scratch/<effort>/spec.md`
- `ticket-creator` → write one file per implementation ticket at `.scratch/<effort>/issues/<NN>-<slug>.md`

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations (wayfinding tickets only)

Created by the `/wayfind` prompt (wraps the `wayfinder` skill) — the map and its child wayfinding tickets live here, resolved at the end of the journey. The **map** is a file with one **child** file per wayfinding ticket.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Child ticket** (a wayfinding ticket): `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`).
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Claim**: a session takes the next frontier ticket and sets its `Status:` to `claimed` before any work, so concurrent sessions skip it — this is the `claim it` step the `/wayfind` flow runs. A claimed ticket leaves the frontier.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
