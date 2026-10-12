# lean-ultracode

**Know what a Claude Code workflow will cost, and how long it will take, before you launch it. Then
check the estimate against what really happened.**

Claude Code's `Workflow` tool runs many subagents in parallel. That gets the job done sooner, but by
default every agent inherits the session's model: a Haiku-sized search runs on Opus, a two-minute
check burns the same budget as a two-hour refactor, and you only find out how much of your 5-hour and
weekly usage went into the run after it has finished.

lean-ultracode is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that
steps in every time Claude is about to launch a workflow:

1. **Picks a model and effort for every agent according to its role.** Searchers get Haiku,
   reviewers Sonnet, implementers and judges Opus, instead of whatever the session happens to be on.
2. **Estimates the run in two modes and asks you to choose one.** For each mode you see the agents
   and what each one does, the cost, how much of your 5-hour and weekly limits it will use, and the
   wall-clock time.
3. **Puts a brake in the script.** A cap on spend stops a loop that never converges, and anything the
   cap prevents is reported, never dropped silently.
4. **Measures the real run** from the agents' transcripts, compares it with the estimate phase by
   phase, and learns from it, so the next estimate is closer.

Everything runs locally, using only the Python standard library. Nothing is sent anywhere.

---

## What it looks like

You ask for something that needs a workflow:

> Review the billing, orders and stock modules for correctness and security, one agent per module and
> lens, and verify every finding adversarially.

Claude writes the workflow script and a short **plan** that summarises it:

```json
{"name": "review-modules", "stages": [
  {"phase": "Review", "role": "analysis", "does": "reviews one module through one lens",
   "items": ["review:billing:correctness", "review:billing:security", "review:orders:correctness",
             "review:orders:security", "review:stock:correctness", "review:stock:security"]},
  {"phase": "Verify", "role": "verify", "does": "tries to refute one finding", "agents": 8}
]}
```

It then runs `usage.py estimate plan.json` and shows you the output exactly as printed:

```text
Current usage:  5 h 2 % (resets 13:10)   ·   weekly 18 % (resets Fri 16 22:00)
Mode: ask (default: estimate and ask for confirmation, thorough or light, before every workflow)

Workflow 'review-modules': 2 phases · Shapes by role — seed: mechanical, extraction, analysis, design, implement, fix, verify, synthesis

THOROUGH (more usage and effort): 14 agents · ~1.9M u (Opus-input-equivalent tokens, ≈ $8 of API) → 5 h +4 % (2 → 6 %) · weekly +1 % (18 → 19 %)
  Time ~6 min in parallel (10 agents at once on this machine; in sequence ~36 min)
  Phase · what each agent does                          Agents  Model / effort      Turns  Durat.     Cost   5 h  week
  Review · reviews one module through one lens               6  sonnet / high          25   4 min   1.4M u   3 %   1 %
      review:billing:correctness, review:billing:security, review:orders:correctness,
      review:orders:security, review:stock:correctness, review:stock:security
  Verify · tries to refute one finding                       8  opus / medium           4    84 s   464k u   1 %   0 %

LIGHT (less usage and effort): 14 agents · ~1.1M u (Opus-input-equivalent tokens, ≈ $4 of API) → 5 h +3 % (2 → 5 %) · weekly +0 % (18 → 18 %)
  Time ~3 min in parallel (10 agents at once on this machine; in sequence ~19 min)
  Phase · what each agent does                          Agents  Model / effort      Turns  Durat.     Cost   5 h  week
  Review · reviews one module through one lens               6  sonnet / low           15   2 min   899k u   2 %   0 %
  Verify · tries to refute one finding                       8  sonnet / low            3    28 s   208k u   0 %   0 %
  Same agents and coverage, one vote and one round: 58 % of the cost of the thorough mode, 14 agents against 14
  What light risks:
    Review: sonnet/high → sonnet/low — lower effort: fewer tool calls, less checking
    Verify: opus/medium → sonnet/low — a Sonnet verifier refutes less: more false findings survive
```

Next it asks you how to launch the workflow:

- **Thorough (more usage and effort):** every agent gets the model its task needs. This is the
  recommended mode.
- **Light (less usage and effort):** the same agents and the same coverage, each on the cheapest
  model that can still do the job, at the lowest effort. The estimate tells you what that trade risks.
- **Change the plan.**

Once you choose, Claude registers the run (`usage.py start`), which returns a marker and a spending
cap. It pastes both into the script and launches it. The top of the script looks like this:

```js
export const meta = { name: 'review-modules', description: '…', phases: [{ title: 'Review' }, { title: 'Verify' }] }
// lean-ultracode: level=thorough run=20261012-091755 cap=1440000
const T = {
  review: { model: 'sonnet', effort: 'high' },
  verify: { model: 'opus',   effort: 'medium' },
}
const CAP = 1440000, S0 = budget.spent()      // brake against loops that never converge
const dropped = []
async function run(prompt, opts) {
  if (budget.spent() - S0 >= CAP) { dropped.push(opts.label); return null }
  return agent(prompt, opts)
}
// … run(p, { ...T.review, label: `review:${m}:${l}`, phase: 'Review' }) …
return { result, dropped, spent: budget.spent() - S0 }
```

When the workflow finishes, `usage.py finish` compares the estimate with what really happened. This
is the output from a real run, with the phase names translated:

```text
  Phase             Agents Model                 Turns               Ctx          Output          Durat.          Cost est → real
  Lenses             6 → 6 sonnet → sonnet     15 → 16   104.9k → 112.1k    3.1k → 11.7k   3 min → 5 min          1.0M u → 1.4M u
  Verify             2 → 2 opus → opus         12 → 13    70.0k → 119.0k     1.9k → 9.6k   3 min → 3 min          296k u → 577k u
  Synthesis          1 → 1 opus → opus           4 → 2     90.0k → 77.5k   25.0k → 20.3k    62 s → 3 min          222k u → 312k u
  total: estimated 1.5M u → real 2.3M u (×1.50; ≈ $9)
  time: estimated 8 min → real 14 min wall-clock (40 min of agent work, 2.8 at once on average)
  3 phases learned · run 20261011-164053 closed
```

Each phase it measures becomes a sample: once a role has two of them, its learned shape replaces the
built-in seed. The estimates start out approximate and get closer the more you use the skill.

---

## Install

The skill is a folder. Copy or link it to one of these places:

| Scope | Where |
|---|---|
| One project | `<project>/.claude/skills/lean-ultracode` |
| Every project (for your user) | `~/.claude/skills/lean-ultracode` |

```bash
git clone https://github.com/kinacho/lean-ultracode ~/.claude/skills/lean-ultracode
```

That is all it needs to work. Claude loads it whenever it is about to write a workflow, or when you
type `/lean-ultracode`.

**Requirements:** Claude Code with the `Workflow` tool, and Python 3.9 or later (standard library
only). The optional status-line snapshot also needs `bash` and `jq`.

It works on macOS, Linux and Windows, and CI runs the test suite on all three. On Windows, `python3`
may not exist (it does with the Python install manager, not with the python.org installer); there the
commands are `python …` or `py -3 …`. [`references/setup.md`](references/setup.md) covers the
per-system details of the optional extras.

### Optional extras

Two extras live in your Claude Code settings, so the skill never installs them itself.
[`references/setup.md`](references/setup.md) has the exact snippets.

1. **Guard hook.** A `PreToolUse` hook on `Workflow` rejects any script that skipped the skill: one
   without the marker, with an agent that has no model or effort, without the spending brake, or with
   a phase name the registered plan does not have. The rejection names the line to fix.
2. **Usage snapshot.** The 5-hour and weekly percentages only reach the status line command.
   `scripts/statusline-snapshot.sh`, called from your status line, saves them so the estimate can
   project them. Without it you still get agents, cost and time, just no percentages.

---

## Arguments

`/lean-ultracode <arg>` sets the mode for the rest of the session:

| Argument | Effect |
|---|---|
| *(none)* or `ask` | Default: estimate both modes and ask before every workflow |
| `thorough` / `light` | Always use that mode; the estimate is still shown, without asking |
| `75%` / `50%` / `25%` | Fixed intermediate cuts (see below) |
| `off` | No tiering and no question; the script carries `level=off` |
| `status` | Show current usage, mode, learned shapes and runs left open |

## Roles

Every stage of a workflow has a role. The role decides the model and effort, and also the expected
**shape** of one agent: how many turns it takes, how much context it re-reads per turn, and how many
tokens it writes.

| Role | Thorough | Light | For |
|---|---|---|---|
| `mechanical` | haiku / low | haiku / low | search, list, inventory, summarise without judgement |
| `extraction` | sonnet / medium | haiku / low | pull data into a schema, compare against a rule |
| `analysis` | sonnet / high | sonnet / low | review through one lens, investigate, find issues |
| `design` | opus / medium | sonnet / medium | design, choose between alternatives, write the deliverable |
| `implement` | opus / medium | sonnet / medium | implement a change: read, edit, run its tests (~55 turns) |
| `fix` | opus / medium | sonnet / medium | apply given review findings to a change (~16 turns) |
| `verify` | opus / medium | sonnet / low | try to refute a finding |
| `synthesis` | opus / high | opus / low | final judge, merge the results |

Light mode never drops agents or coverage. It lowers models and effort and runs one vote and one
round. The fine-grained cuts go further:

| Cut | What it drops |
|---|---|
| `75%` | One effort step everywhere except `synthesis` |
| `50%` | 75 % plus: one verifier per finding, at most 2 rounds, `extraction` on Haiku, `analysis` and `verify` on Sonnet/medium |
| `25%` | 50 % plus: `design` on Sonnet, half of the lenses and findings (the most important ones), `synthesis` at medium, 1 round |

## The plan

The plan is a small JSON file that sits next to the script. It is what the estimate is built from
and what the real run is measured against.

```json
{"name": "three-features", "sequential": true, "stages": [
  {"phase": "Feature", "prefix": "impl:", "role": "implement", "does": "implements one feature", "agents": 3},
  {"phase": "Feature", "prefix": "rev:",  "role": "analysis",  "does": "reviews it",             "agents": 3},
  {"phase": "Feature", "prefix": "fix:",  "role": "fix",       "does": "applies the findings",   "agents": 3},
  {"phase": "Gate",    "role": "mechanical", "does": "runs the test suite", "agents": 1, "wait": 3600}
]}
```

| Field | Meaning |
|---|---|
| `phase` | The `phase` of the stage's `agent()` calls in the script |
| `role` | One of the roles above |
| `does` | What one agent of the stage does, shown in the estimate |
| `items` / `agents` | The agents' labels, or just how many agents run per round |
| `votes`, `rounds` | Redundancy per item; expected rounds of a loop |
| `prefix`, `id` | Tell apart stages that share a phase (labels `impl:auth`, `rev:auth`…) |
| `sequential` | On the plan: every agent waits for the one before. On a stage: only its agents do |
| `wait` | Seconds per round that are not agents: test runs, builds |
| `model`, `effort`, `agentType`, `turns`, `ctx`, `output` | Overrides, when you know better than the role |

The two time fields make a big difference to the estimate. A chain of implement → review → fix that
runs phase after phase is sequential, and a test gate that runs for an hour should be estimated at an
hour, not at the two minutes the agent that launches it takes.

## Commands

All of them are `python3 <skill dir>/scripts/usage.py …`. Claude runs them for you; you only need them
yourself to look inside.

| Command | What it does |
|---|---|
| `status` | Current usage, mode, learned shapes, seconds per turn, usage ratio, runs left open |
| `mode [thorough\|light\|75\|50\|25\|ask\|off]` | Set or show the mode |
| `estimate PLAN.json [--json]` | Estimate both modes (plus the fine-grained cuts) |
| `start PLAN.json --level thorough\|light\|75\|50\|25` | Register the run and print its id, cap and marker |
| `finish RUN_ID --wf wf_… [--spent N] [--dropped N]` | Measure the real run, compare it with the estimate and learn from it |
| `measure --wf wf_… \| DIR [--since EPOCH]` | Measure a workflow without a registered run |

## How the numbers work

- **Cost unit `u`:** one Opus input token. Every other kind of token is weighted by its list price:
  output 5, cache write 1.25, cache read 0.05 on Opus and 0.1 on Sonnet and Haiku. Each model is
  weighted by its input price relative to Opus: Haiku 0.25, Sonnet 0.5, Fable 2.5. One million u is
  about $4 at API list prices. The weights live in
  [`references/seeds.json`](references/seeds.json).
- **Shapes:** most of an agent's cost is the context it re-reads on every turn. A role's shape
  (turns × context, plus output) is therefore what drives the estimate. Effort scales both turns and
  output.
- **Time:** seconds per turn of each model, measured on real runs and re-learned. Concurrency comes
  from the runtime, `min(16, CPUs − 2)` unless
  `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` says otherwise.
- **Usage percentages:** Anthropic does not publish how much one percent of a window is. The skill
  learns it by comparing how the percentage rose in your status line history with all your local
  Claude Code usage over the same span. Until there is enough history, it uses the window in
  progress and labels the figure *provisional*. Usage on claude.ai or on another machine also moves
  the percentage and is invisible here, so the projection tells you whether a workflow fits; it is
  not accounting.
- **The brake:** `budget.spent()` counts more than the agents' transcripts show (measured at more
  than 4×), so the cap is expressed in its units. `finish --spent` learns the exact ratio.

## Your data

Runs, learned shapes, ratios and the mode are stored in `~/.claude/lean-ultracode/`, per user and
outside every repository. Set `LEAN_ULTRACODE_DATA=<dir>` to keep them somewhere else, which is useful
for tests. The skill reads Claude Code's own transcripts under `~/.claude/projects/` and never sends
anything over the network.

## Layout

```text
SKILL.md                         what Claude follows
scripts/usage.py                 estimate, start, finish, measure, status, mode
scripts/statusline-snapshot.sh   optional: saves the usage % from the status line
hooks/workflow-guard.py          optional: rejects workflows that skip the skill
references/seeds.json            prices, roles, starting shapes, light-mode risks
references/setup.md              how to install the optional pieces
references/anthropic-model-guide.md   the model and effort guidance the roles come from
tests/test_skill.py              smoke tests: python -m unittest discover -s tests
evals/                           behaviour cases for the skill itself (run by hand)
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run every script the way Claude Code does, with `HOME` pointed at a temporary folder, so
your real `~/.claude` is never touched. They include a run under a `cp1252` console, to catch encoding
problems that only show up on Windows. The status-line tests are skipped when `bash` or `jq` is
missing.

## Limitations

- The estimates are approximate, especially the first ones. They improve as runs are measured.
- Loops that run "until nothing new turns up" are the least predictable part; `rounds` is your best
  guess.
- The percentages need the status-line snapshot and some history before they can be projected.
- Workflows saved by name (`workflow('name')`) cannot be read by the guard hook and pass unchecked.

## License

[MIT](LICENSE)
