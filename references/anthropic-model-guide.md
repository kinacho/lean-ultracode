# What Anthropic's model guide says, and how this skill uses it

Read on 2026-09-30 from:

- https://platform.claude.com/docs/en/about-claude/models/choosing-a-model
- https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- https://platform.claude.com/docs/en/build-with-claude/effort

Re-read them when a new model ships or `seeds.json` prices change; the figures below are Anthropic's,
measured at list prices on their benchmarks, and drift.

## Model selection matrix (choosing-a-model)

| When you need… | Start with | Anthropic's examples | Role here |
|---|---|---|---|
| The highest available capability | Claude Fable 5.1 | agent sessions that run for hours, multistep deep research, analysis carried through to a finished document | never in workers unless the user asks |
| Complex agentic coding and enterprise work | Claude Opus 5.5 | multihour autonomous agents, large refactors, complex systems engineering | `design`, `implement`, `fix`, `verify`, `synthesis` |
| Speed and capability for everyday coding, agent and enterprise work | Claude Sonnet 5.5 | code generation, data analysis, agentic tool use | `analysis`, `extraction` |
| The lowest latency and price | Claude Haiku 4.5 | high-volume processing, cost-sensitive work needing reasoning, **sub-agent tasks** | `mechanical` |

"Most workloads start with Claude Opus 5.5." Move to Fable 5.1 only when Opus 5.5 at `xhigh`/`max`
still falls short on demanding reasoning or long-horizon work.

## Effort (effort page)

- Effort scales **all** output: text, tool calls and thinking. Lower effort makes **fewer and terser tool
  calls** — fewer turns, not only fewer output tokens. That is why it moves an agent's cost, whose bulk is
  the context re-read every turn.
- Defaults: `high` everywhere except Claude Opus 5.5, whose default is `medium`. Setting the default
  explicitly is identical to omitting it.
- Recommended starting points: Opus 5.5 `medium`, then sweep; Sonnet 5.5 `high` in general but `medium`
  for well-specified agentic coding and multistep tool use, `high` for harder or longer ones; `low` "for
  simpler tasks that need the best speed and lowest costs, such as subagents".
- **Haiku 4.5 does not support `effort`** (not in the supported-models list). The skill still writes one
  for uniformity; it has no effect there.

## Measured trade-offs (optimizing-for-cost-and-intelligence)

- Research/knowledge work: `medium` matches the default's accuracy at 70-87 % of the cost; `low` is 1-3
  points down for 33-50 % less cost. Long-horizon coding on Opus 5.5: `low` ≈ 33 % of the cost, ~8
  points down; `medium` ≈ 70 %, ~2.5 points down; `xhigh` +1.4 points for 2.5× the cost.
- Compare models on **cost per completed task**, not per token: frontier models take fewer turns.
  Opus 5.5 at `medium` matched Fable 5.1 on a saturated coding set at a fifth of the cost; Fable 5.1 at
  `low` beat Sonnet 5 by 11 points for 35 % less per task on the hard set.
- Cache reads: Opus 5.5 and Sonnet 5.5 both $0.20 per million, Fable 5.1 $0.25, Haiku $0.10. In a
  tool-heavy agent that is the dominant term, so stepping Opus → Sonnet saves less than the input price
  suggests. Rule from the guide: before downgrading, price the stronger model at `low`.
- Latency: `low` took 4.5 min against 7.9 at default on a wide-search benchmark. An explicit "time
  matters" instruction plus an elapsed-time clock saved 33-69 % time and 28-54 % cost with ±1.9 points.
- Outputs: a one-line answer format used 39 % fewer output tokens than a two-line one at equal accuracy;
  a five-section memo cost 2.5×. Ask for the answer you will read.
- Checkable outcomes: run at `low`, re-run failures at `high` → 97 % pass for 55 % less cost (costs
  wall-clock on the failures).
- Orchestrator with cheaper workers pays off when the work exceeds one context or as insurance against
  cost tails; when the work fits one context, a single model at lower effort was 22-30 % cheaper at the
  same accuracy. It is, however, up to 7× faster in wall-clock when the work fans out (2.3 h against
  15-20 h with 25 workers).
- Price the tail: on a 20-problem run, the top 2 carried 43 % of spend.

## How this skill applies it

- The point of a workflow here is **wall-clock**, not cost: the guide says orchestration is the slow
  path's fast alternative, and that is the trade the user makes knowingly. Cost and usage limits are shown
  and confirmed, not minimised by default.
- Model by role follows the matrix; effort by role follows the recommended starting points (Opus
  `medium`, Sonnet `high` for long lenses, `medium` for bounded extraction, Haiku for mechanical).
- When torn between two rows, take the one with more judgement at the lower effort, not the cheaper one.
- `estimate` models effort as scaling turns and output (`effort_turns`, `effort_output` in
  `seeds.json`), seeded from the per-task figures above, and learns the real shapes from `finish`.
- Prompt hygiene for agents comes from the guide: self-contained prompt, "time matters" line, terse
  structured output, no "verify twice".
