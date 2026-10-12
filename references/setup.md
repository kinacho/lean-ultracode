# Setting up lean-ultracode

The skill works as soon as it is installed: it tiers models, estimates cost and time, and asks before
launching. Two optional pieces make it stricter and more precise. Neither comes with the skill, since
both live in the user's Claude Code settings; ask the user before touching their settings.

## Where the skill lives

`<skill dir>` below is wherever the skill was installed:

| Installed with | `<skill dir>` |
|---|---|
| Copied or linked into a project | `.claude/skills/lean-ultracode` (from the project root) |
| Copied or linked for the user | `~/.claude/skills/lean-ultracode` |

Its own data (runs, learned shapes, ratios, mode) always goes to `~/.claude/lean-ultracode/`, per user
and outside any repo, so it is shared across projects. `LEAN_ULTRACODE_DATA=<dir>` moves it elsewhere,
which is useful for tests that must leave the real calibration alone.

## 1. The guard hook (optional, recommended)

`hooks/workflow-guard.py` rejects a Workflow script that skipped the skill (§6 of `SKILL.md`). Add it as
a `PreToolUse` hook with matcher `Workflow`. For a per-project install, put it in the project's
`.claude/settings.local.json` (personal, not committed) so it does not impose itself on the rest of
the team:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Workflow",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/skills/lean-ultracode/hooks/workflow-guard.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

For a user install, put the same block in `~/.claude/settings.json` with the command
`python3 ~/.claude/skills/lean-ultracode/hooks/workflow-guard.py`. If `hooks.PreToolUse` already exists,
append the entry to the array instead of replacing it.

On Windows, use whichever command starts Python there: `python3` only exists with the Python install
manager or the Microsoft Store build; with the python.org installer it is `python` (or `py -3`). Write
the path with forward slashes, e.g. `python C:/Users/<you>/.claude/skills/lean-ultracode/hooks/workflow-guard.py`.

Check it: `/hooks` lists it, and a Workflow script without the marker is rejected with a reason that
names what is missing.

## 2. Usage snapshot from the status line (optional)

The 5-hour and weekly percentages only reach Claude Code's status line command (`rate_limits` in its
JSON input). Without them `estimate` still prints agents, cost and time, and says that no % is
projected. To get them, call `scripts/statusline-snapshot.sh` from the status line command with the
same input:

```bash
#!/bin/bash
input=$(cat)
echo "$input" | bash ~/.claude/skills/lean-ultracode/scripts/statusline-snapshot.sh   # or the project path
# … the rest of your status line, reading "$input" …
```

If there is no status line yet, `/statusline` creates one; then add those two lines at the top. The
script needs `bash` and `jq`, prints nothing and always exits 0, so it never breaks the status line.

| System | bash | jq |
|---|---|---|
| macOS | built in | `brew install jq` |
| Linux | built in | `sudo apt install jq` (or your distribution's package) |
| Windows | Git Bash, which Claude Code already uses | `winget install jqlang.jq` |

The % per million u starts projecting once the history holds a valid span: at least 30 minutes and 5
rising points in one 5-hour window (4 hours and 2 points for the weekly one). Until then `status` says
so.

## 3. Concurrency (optional)

The Workflow runtime runs `min(16, CPUs − 2)` agents at once. On a machine with few CPUs that, not the
model, is the limit on speed. `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1-256, Claude Code ≥ 2.1.269)
under `env` in `~/.claude/settings.json` raises it; `usage.py status` prints the value in use and the
time estimate follows it. It applies to sessions started after the change.
