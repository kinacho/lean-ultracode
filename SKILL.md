---
name: lean-ultracode
description: Use this skill when you are about to author or launch any Workflow script — whether ultracode is on or the user asked for a workflow — and whenever the user types /lean-ultracode (args thorough|light|75%|50%|25%|ask|off|status). Give every agent() the model and effort its task needs instead of the inherited session model, estimate the run in two modes — thorough (the model each task needs, more usage and effort) and light (the cheapest model that still does it, less usage and effort) — with agents and what each does, cost, impact on the 5-hour and weekly usage limits and wall-clock time, show both to the user and let them choose before launching, then measure the real run. Before a single Agent call outside a workflow, use only §7 to give it a model and effort. The optional workflow-guard hook rejects scripts that skip it.
metadata:
  version: "1.6.0"
---

# lean-ultracode

A workflow exists to **finish sooner**: independent pieces run in parallel instead of one after another.
Ultracode says token cost is not a constraint; here the user chooses, before the run starts, between
two modes with their full estimate:

- **Thorough** (more usage and effort) — every agent gets **the model its task needs**, not the session
  model by inheritance and not the cheapest by default. The default recommendation.
- **Light** (less usage and effort) — the same agents and coverage with the cheapest model that still does each task, at the
  lowest effort, one vote and one round: maximum token savings.

The rest of ultracode (orchestrating with workflows, adversarial verification) still applies.

All commands below are `python3 <skill dir>/scripts/usage.py …`, where `<skill dir>` is where the skill
is installed: `.claude/skills/lean-ultracode` in a project, `~/.claude/skills/lean-ultracode` for
the user. On Windows, if `python3` is not found or opens the Microsoft Store, use `python` (or
`py -3`) instead. The guard hook and the usage snapshot are optional one-time setup:
`references/setup.md`.

## Arguments (`/lean-ultracode …`)

| Arg | Effect (for the rest of the session) |
|---|---|
| *(none)* or `ask` | Default: estimate both modes and ask the user to choose before every workflow |
| `thorough`, `light` | Fixed mode, no question; the estimate is still shown |
| `75%`, `50%`, `25%` | Fine-grained cuts (§5), fixed, no question |
| `off` | No tiering and no confirmation; the script carries `// lean-ultracode: level=off` |
| `status` | Only `usage.py status`: current usage, mode, learned shapes and timings, runs left open |

Store the choice with `usage.py mode light` (or `thorough`, `ask`, `off`): `status` and `estimate` print
it, so after a context compaction run `status` instead of assuming the default.

## 1. Model and effort by role

Every stage of the script has a **role**. The thorough mode follows Anthropic's model matrix and its
recommended starting effort per model; the light mode takes the cheapest model that still does the
task (`references/anthropic-model-guide.md`, `references/seeds.json`):

| Role | Thorough | Light | For |
|---|---|---|---|
| `mechanical` | haiku / low | haiku / low | search, list, inventory, read and summarise without judgement |
| `extraction` | sonnet / medium | haiku / low | pull data into a schema, transform, compare against a given rule |
| `analysis` | sonnet / high | sonnet / low | review through one lens, investigate a subsystem, find issues |
| `design` | opus / medium | sonnet / medium | design, choose between alternatives, write the deliverable |
| `implement` | opus / medium | sonnet / medium | implement a change in a codebase: read, edit, run its tests (~55 turns) |
| `fix` | opus / medium | sonnet / medium | apply given review findings to an existing change (~16 turns) |
| `verify` | opus / medium | sonnet / low | adversarial verification of a finding |
| `synthesis` | opus / high | opus / low | final judge, synthesis |

Light also sets one vote and one round everywhere; it never cuts agents or coverage.

Pass both on every agent: Opus 5.5, Sonnet 5.5 and Haiku 5.5 all take `low` to `max`, and an agent
without an effort runs at the session's level, not at the one its role needs. `model` takes only an
alias (`haiku`, `sonnet`, `opus`, `fable`), never a full model ID; the hook rejects one.

Each role also has a **shape** — turns, average context per turn and output tokens of one agent —
seeded from real runs and re-learned from every `finish` (§4). So a role is a shape, not only a
model: an implementer and the corrector that applies its review findings are both Opus, but one runs
~55 turns and the other ~16. Put them under one role and the learned median fits neither (it once
estimated implementers at a fifth of their cost and set a cap that would have stopped the run at the
second phase). A reviewer with tools runs ~25 turns at
~100k context; a verifier that gets everything in its prompt ~4 turns at ~40k. That context, re-read
every turn, is what costs; lower effort makes fewer tool calls, so it also shortens the run.

- When torn between two rows, take the one with **more judgement at the lower effort**, not the cheaper
  one: Opus at `low` beats Sonnet at `high` per completed task, and their cache reads cost the same.
- `xhigh`/`max` only for a final judge when the result demands it, and say so. Never `fable` in a worker
  unless the user asks: it is for hours-long sessions and deep research, not for a stage.
- A custom subagent (`agentType`, e.g. a project's own reviewer agent) already carries its model and
  effort: use it as is and mark the stage `agentType` in the plan, with its `model`/`effort`.
- If a stage comes out weak, raise *that* stage next time (effort first, then model), not all of them.
- Keep one model, effort, schema and agent type across a stage's agents: agents that match share the
  prompt cache (the first writes the prefix, the rest start up to 5 s later and read it). Varying one
  of them inside a fan-out makes every agent pay its own prefix.
- Haiku 5.5 bills a request whose prompt passes 100k tokens at 5× every price. Keep a Haiku agent's
  reading small (it is the `mechanical` role); a stage that reads more belongs on Sonnet.

In the script, options come from a table of constants and a `run()` wrapper with the runaway brake:

```js
// lean-ultracode: level=thorough run=20260930-161200 cap=680000   ← the marker printed by usage.py start
const T = {
  scan:   { model: 'haiku',  effort: 'low' },
  review: { model: 'sonnet', effort: 'high' },
  verify: { model: 'opus',   effort: 'medium' },
  judge:  { model: 'opus',   effort: 'high' },
}
const CAP = 680000, S0 = budget.spent()   // brake on the workflow's OWN output tokens (3× the estimate)
const dropped = []
async function run(prompt, opts) {
  if (budget.spent() - S0 >= CAP) { dropped.push(opts.label || prompt.slice(0, 40)); return null }
  return agent(prompt, opts)
}
// … run(p, { ...T.scan, label: 'scan:x', phase: 'Explore' }) …
if (dropped.length) log(`Cap reached: ${dropped.length} agents not launched`)
return { result, dropped, spent: budget.spent() - S0 }
```

The marker goes **after** `export const meta` (which must come first). The result always returns
`dropped` (no silent truncation) and `spent`, which `finish --spent` learns from. `budget.spent()`
counts the whole turn, main loop included, hence `S0`; and it counts several times the output the
agents' transcripts show (measured: more than 4×), so the cap is in its units: estimated output ×
`spent_ratio` (seed 5, learned) × 3 in thorough mode — only a brake against loops that never converge —
2× in light, and the agreed cut (1.5×) in the fine-grained cuts.

## 2. Prompts that finish sooner

From Anthropic's measurements (`references/anthropic-model-guide.md`), for every agent prompt:

- Self-contained: everything the agent needs in the prompt, so it reads instead of searching.
- One line: "Time matters here: do not spend turns that can be avoided; an earlier correct result is
  better." (33-69 % less time, ±2 points.) No "verify twice" or mandatory step lists.
- Ask for the answer you will read, through `schema`: a one-line verdict beats a memo (39 % fewer
  output tokens, same accuracy), and every output token comes back as input in later turns.
- `pipeline()` over barriers, one agent per independent piece. The runtime runs `min(16, CPUs − 2)`
  agents at a time (`status` prints the number); more agents than that queue. On a small machine that
  is the real limit on speed: `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1-256, Claude Code
  ≥ 2.1.269), set under `env` in `~/.claude/settings.json`, raises it, and the time estimate follows it.

## 3. Procedure before every Workflow

1. Write the script (tiered by role) and, in the scratchpad, the **plan** that summarises it:
   ```json
   {"name": "review-analysis", "stages": [
     {"phase": "Explore",    "role": "mechanical", "does": "inventories one folder of the job",
      "items": ["scan:workspace", "scan:gitlab", "scan:db"]},
     {"phase": "Review",     "role": "analysis",   "does": "reviews the page through one lens",
      "items": ["lens:integrity", "lens:conformance", "lens:concision", "lens:correctness"]},
     {"phase": "Verify",     "role": "verify",     "does": "tries to refute one finding", "agents": 6, "votes": 3, "turns": 3},
     {"phase": "Synthesise", "role": "synthesis",  "does": "merges the surviving findings", "agents": 1}
   ]}
   ```
   `does` = what each agent of the stage does, `items` = their labels (`agents` defaults to their count),
   `agents` = agents per round, `votes` = redundancy per item, `rounds` = expected rounds of a loop
   (an honest estimate; loop-until-dry is the most uncertain part). Optional: `model`/`effort` as the
   stage's level-100 values, `agentType` for a custom agent, `turns`/`ctx`/`output` when you know the
   shape better than the role default.

   **Time** — the estimate assumes stages run one after another and a stage's agents at once. Say so
   when that is not true, or the time comes out several times too short:
   - `"sequential": true` on the **plan** when every agent waits for the one before (a chain of
     implement → review → fix, phase after phase); on a **stage** when only its agents go one by one.
   - `"wait": seconds` on a stage for each round's time that is not agents: test runs, builds, a gate
     (a full test suite that runs for an hour is an hour, not the two minutes of the agent that
     launches it).

   **Matching** — `finish` ties every real agent to a stage: by its label among the stage's `items`,
   else by a label `prefix` of the stage, else by `phase`. **The `phase` of the script's `agent()`
   calls must be a phase of the plan** (the hook rejects a literal phase that is not). Several stages may
   share one phase — one progress group per step of the work, not one per role — as long as each
   carries `items` or a `prefix` (`"prefix": "impl:"` for labels `impl:auth`, `impl:billing`…); give a
   stage an `id` when two could still be confused.
   ```json
   {"name": "three-features", "sequential": true, "stages": [
     {"phase": "Feature", "prefix": "impl:", "role": "implement", "does": "implements one feature", "agents": 3},
     {"phase": "Feature", "prefix": "rev:",  "role": "analysis",  "does": "reviews it",             "agents": 3},
     {"phase": "Feature", "prefix": "fix:",  "role": "fix",       "does": "applies the findings",   "agents": 3},
     {"phase": "Gate", "role": "mechanical", "does": "runs the test gate", "agents": 1, "wait": 3600}
   ]}
   ```
2. Run `usage.py estimate plan.json` and show the output **verbatim**: for each mode, agents and what
   each does, model and effort per stage, cost in tokens, projected 5-hour and weekly usage, and
   wall-clock in parallel against sequential. Its ⚠ lines go into the question as well: an effort or
   model forced by the environment (`CLAUDE_CODE_EFFORT_LEVEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`)
   makes the per-stage choices moot, and a plan above the size guideline (`workflowSizeGuideline`)
   needs the user's request to call for that scale.
3. Ask with AskUserQuestion, in the user's language — "How should I launch the workflow?" — with the
   two modes named exactly as `estimate` prints them (thorough: more usage and effort; light: less usage
   and effort) and "Change the plan". Thorough is recommended, unless the 5-hour window would end ≥ 90 %,
   when light is recommended and the reason said. Each option's description
   carries its agents, cost, % and time from the estimate; light's also carries the worst line of
   "What light risks" (quality, not only cost: an implementer on Sonnet in engine code is a real trade).
   With a mode fixed by argument, show the estimate and do not ask.
4. Write the script with the chosen mode's models and efforts (§1; for a fine-grained cut, §5). Run
   `usage.py start plan.json --level thorough|light`: it returns `run_id`, `cap` and the **marker**. Paste
   the marker after `meta`, set `CAP` to that number, and launch.
5. When it finishes, the tool result gives the workflow's `runId` (`wf_…`):
   `usage.py finish RUN_ID --wf wf_… --spent N` (or `--transcripts <transcriptDir>`), with `N` the
   `spent` the script returned and `--dropped N` if the cap fired. Show its table: per stage, estimated
   → real agents, turns, context, output, duration and cost, plus real wall-clock against the estimate.
   Runs left open show up in `status`.
- Resuming with `resumeFromRunId`: cached agents cost nothing. Estimate only the stages that will run
  again (set their `agents`) and register a new run for that relaunch. The relaunch writes in the same
  folder as the first run; `finish` counts only the agents that started after its `start` (it says how
  many it left out; `--all-agents` counts them all).

## 4. Usage, cost, time and calibration

- 5-hour and weekly usage come from `~/.claude/usage-snapshot.json`, which
  `scripts/statusline-snapshot.sh`, called from the status line, writes on every refresh with
  `rate_limits` (`references/setup.md` §2). If it is missing or stale, say so; never invent a percentage.
- **Cost unit `u`** = one Opus input token. Weights by token type: input 1, cache write 1.25, output 5,
  cache read 0.1 (Haiku), 0.05 (Sonnet, Opus), 0.025 (Fable); by model, list price of input vs Opus:
  haiku 0.025, sonnet 0.5, opus 1, fable 2.5, and Haiku ×5 on a request over 100k prompt tokens
  (`seeds.json`, API list prices 2026-10-12). 1M u ≈ $4.
- **Effort** scales both turns and output (`effort_turns`, `effort_output`), seeded from Anthropic's
  per-task figures: `medium` ≈ 70-87 % of the cost of `high`, `low` ≈ 33-50 %.
- **Time**: seconds per turn per model, measured on real runs (Haiku ~4, Sonnet ~10, Opus ~21, Fable
  ~17) and re-learned by `finish`; wall-clock ≈ max(longest chain, total agent-seconds ÷ concurrency).
- **Real cost and time** come from the agents' transcripts (`<transcriptDir>/agent-*.jsonl`, last entry
  of each message and its timestamps; phase from `journal.jsonl` and `agent-*.meta.json`). **Not** from
  `budget.spent()`, which also counts the turn's main session: that is only the brake, never a measurement.
- **Two separate calibrations**:
  - *Shapes by role and seconds per turn by model*: `finish` records, per phase, the median turns,
    context, cache writes, output and seconds per turn of its agents. With ≥ 2 samples of a role (or a
    model), the median of its last 5 replaces the seed (`estimate` says which roles are learned).
  - *% per million u*: Anthropic does not publish what 100 % of a window is. The status line keeps a
    history (`~/.claude/usage-history.jsonl`, one line whenever a % changes) and `usage.py` measures
    each 5-hour/weekly window from its first to its last **rising** point (a point below the maximum
    seen is a stale snapshot from an idle session and is ignored) against **all** local usage in that
    span (main session, subagents and other sessions, from `~/.claude/projects/**/*.jsonl`). Only
    spans of ≥30 min and ≥5 points (5-hour) or ≥4 h and ≥2 points (weekly) count: the % arrives
    rounded to integers, and over a short span the rounding outweighs the change. Until the history
    has a valid span, the ratio is **provisional**: the % the current window already shows (≥ 5 %)
    against all local usage since that window began; `estimate` and `status` say so. Below that, no %
    is projected. A price change in `seeds.json` recomputes every ratio.
  - *What `budget.spent()` counts*: `finish --spent` records it against the agents' real output, and
    the median replaces the seed `spent_ratio` in the next cap.
- **What the runtime does that changes the numbers** (Claude Code docs, 2026-10):
  - At the 5-hour limit the run pauses until the reset and goes on alone (≥ 2.1.271, interactive,
    `autoContinueAtUsageLimit` on, at most twice); a weekly reset more than 24 h away is not waited for
    and those agents fail. `estimate` says which applies when a mode passes 100 %.
  - An agent that stalls starts over, up to five times, and every attempt's tokens count: the real cost
    can exceed the estimate without any loop. `finish` folds `(retry N)` labels into their stage.
  - The agents' prompt cache lasts 5 minutes (`subagentPromptCacheTtl: 1h` keeps it, at a 2× write
    price), so the agent after a long `wait` or in a sequential chain writes its prefix again.
- It is approximate: usage on claude.ai or another machine also moves the % and is invisible here. It
  tells whether a workflow fits, not accounting.
- Data in `~/.claude/lean-ultracode/` (per user, outside any repo): `runs/*.json`, `calibration.jsonl`
  (shapes and timings), `rates.json` (ratios) and `mode.json`.

## 5. Fine-grained cuts (by argument only)

Between thorough and light, `/lean-ultracode 75%|50%|25%` fixes an intermediate cut. `usage.py`
applies these rules to the plan; write the script with them:

| Cut | What drops |
|---|---|
| 75 % | One effort step down everywhere except `synthesis` |
| 50 % | 75 + one verifier per finding, at most 2 rounds in loops, `extraction` → haiku/low, `analysis` and `verify` → sonnet/medium |
| 25 % | 50 + `design` → sonnet/medium, only half of the `analysis` lenses and of the `verify` findings (the most important ones), `synthesis` opus/medium, 1 round. State what was cut in the summary |

The names are nominal: `estimate` prints each cut's real share of the thorough-mode cost and its time. In every
mode a stage marked `agentType` (or `"fixed": true`) keeps its model and effort; only votes, rounds and
the halvings apply to it. Nothing is ever raised above what the plan gave. (If the user wrote a "+500k"
in the message, `budget.total` is already a hard ceiling and the lower of the two wins.)

## 6. The hook

Optional: `references/setup.md` §1 installs it. Without it, follow §3 all the same; nothing enforces
it then. Once registered (PreToolUse, matcher `Workflow`), `hooks/workflow-guard.py` rejects the script
if the marker is missing or lacks `run=` or `cap=` (the cap number must appear in the script), if any
`agent()`/`run()` with literal options does not set `model` and `effort` (`agentType` or a `...T.x`
spread also pass), if it never checks `budget.spent()`, or if a literal `phase: '…'` is not a phase of
the plan registered under that `run=`. A child launched with `workflow({scriptPath})` is checked the same
way. The rejection reason names the line to fix: fix it and relaunch, without asking the user.

## 7. Single Agent calls (outside a workflow)

The Agent tool takes `model` and `effort` per call (Claude Code ≥ 2.1.292). Only this section applies
there: no plan, no estimate, no question and no measurement.

- Pass both on every call, from the §1 table in thorough mode: a search is `mechanical` (haiku / low),
  a review through one lens `analysis` (sonnet / high), a design or an implementation opus / medium.
- `xhigh` and `max` never go to a worker. A custom `subagent_type` that declares more effort than its
  role needs is overruled by the call's `effort`.
- Say in the prompt what the agent may skip at `low` and what it must check at `high`, so the
  instructions and the effort agree.
- A `fork` inherits the session's model and ignores `effort`: do not pass them.
- `CLAUDE_CODE_EFFORT_LEVEL` and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` override the call;
  `usage.py status` says when either is set.
