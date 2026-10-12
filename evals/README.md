# Evaluation Guide for lean-ultracode

This directory contains structured test cases to ensure the skill performs reliably and safely.

No case launches a workflow: each one stops before the `Workflow` call (or, for eval 5, never gets
there), so a run costs one session and no subagents. Every `usage.py` call points
`LEAN_ULTRACODE_DATA` at a folder of the eval's workspace, so the real calibration in
`~/.claude/lean-ultracode/` is never written.

Fixtures in `evals/files/`:

- `plan.json`: the plan of a two-phase review workflow (six analysis agents, eight verifiers), for eval 2.
- `review-workflow.js`: the matching script, already registered, whose review call sets `model` but not
  `effort`, so `hooks/workflow-guard.py` rejects it with `line 20: run() without effort` (evals 3 and 4).

Safety rules to paste into every run: do not launch any Workflow, do not edit any settings file, and
pass `LEAN_ULTRACODE_DATA` exactly as the prompt gives it.

## How to Run an Evaluation

To get valid results, follow the **"Clean Context"** principle: every test run must start in a
completely new, empty chat session.

### 1. With Skill (Test Run)
Open a new session and paste the following instruction:

```text
Execute this task:
- Skill path: .claude/skills/lean-ultracode
- Task: Use a workflow to review the modules billing, orders and stock through the lenses correctness and security, one agent per module and lens, and verify every finding adversarially. Do not launch it yet: prepare it and ask me how to launch it. Use LEAN_ULTRACODE_DATA=skills/lean-ultracode-workspace/iteration-1/eval-1/data for every usage.py call.
- Input files: none
- Save outputs to: skills/lean-ultracode-workspace/iteration-1/eval-1/with_skill/outputs/
```

### 2. Without Skill (Baseline)
Open another new session and paste the same instruction but **remove the skill path**:

```text
Execute this task:
- Task: Use a workflow to review the modules billing, orders and stock through the lenses correctness and security, one agent per module and lens, and verify every finding adversarially. Do not launch it yet: prepare it and ask me how to launch it.
- Input files: none
- Save outputs to: skills/lean-ultracode-workspace/iteration-1/eval-1/without_skill/outputs/
```

Repeat with the prompt and input files of each case in `evals.json`, changing `eval-1` to the case id.
For eval 5 (the near-miss), the pass condition is that the skill is **not** used: no `usage.py`, no plan
and no mode question.

## Grading (Generating grading.json)
Open a new chat session and paste the following prompt, filling in the outputs you just generated:

```text
You are an impartial AI evaluator. I am testing a skill called lean-ultracode.
Here are the assertions the output must pass:
[PASTE THE assertions OF THE CASE FROM evals/evals.json HERE]

Here is the output generated WITH the skill:
[PASTE THE CONTENT OF with_skill/outputs/ HERE]

Here is the output generated WITHOUT the skill (baseline):
[PASTE THE CONTENT OF without_skill/outputs/ HERE]

Evaluate each assertion ONLY for the "with skill" output. Provide concrete evidence from the text.
Format your response EXACTLY as a valid JSON matching this schema, so I can save it as `grading.json`:
{
  "assertion_results": [
    { "text": "assertion text", "passed": true/false, "evidence": "quote from output" }
  ],
  "summary": { "passed": X, "failed": Y, "total": Z, "pass_rate": 0.X }
}
```

Save it as `skills/lean-ultracode-workspace/iteration-1/eval-<id>/grading.json`.
