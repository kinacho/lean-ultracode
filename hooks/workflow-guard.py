#!/usr/bin/env python3
"""lean-ultracode PreToolUse(Workflow) hook: rejects the script if it does not follow the skill.

  1. It must carry the marker  // lean-ultracode: level=thorough|light|75|50|25|off run=ID cap=N  (proof
     that the run was estimated, the user chose thorough or light and it was registered with
     `usage.py start`). With level=off nothing else is checked. The cap number must appear in the
     script (const CAP).
  2. Every agent(...) or run(...) call must set model and effort, or agentType, or bring them through
     a spread (...T.scan). If the options are a bare variable they cannot be checked and pass.
  3. The script must check budget.spent() (the brake against loops that never converge).
  4. A child workflow launched with workflow({scriptPath: '…'}) is checked the same way (one level).
  5. Every literal `phase: '…'` must be a phase of the plan registered under run=ID (what `finish`
     matches the real agents against): a renamed phase would otherwise surface only after the run.
  6. A literal model is an alias (haiku, sonnet, opus, fable): a per-agent model takes no full model ID.

A saved workflow (tool_input.name, or workflow('name')) cannot be read and passes unchecked.
"""
import io
import json
import os
import re
import sys
from pathlib import Path

# the same folder usage.py writes the runs to
DATA = Path(os.environ.get("LEAN_ULTRACODE_DATA") or Path.home() / ".claude" / "lean-ultracode")
PHASE = re.compile(r"\bphase\s*:\s*(['\"])([^'\"\n]*)\1")
MODEL = re.compile(r"\bmodel\s*:\s*(['\"])([^'\"\n]*)\1")
ALIASES = {"haiku", "sonnet", "opus", "fable"}

CALL = re.compile(r"(?<![\w.$])(agent|run)\s*\(")
MARK = re.compile(r"lean-ultracode:\s*level=(thorough|light|100|75|50|25|off)\b([^\n]*)")
CHILD = re.compile(r"\bworkflow\s*\(\s*\{[^}]*?\bscriptPath\s*:\s*(['\"])([^'\"]+)\1")
TOKENS = re.compile(r"//[^\n]*|/\*.*?\*/|`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", re.S)


def split_args(src, start):
    """From the '(' at src[start-1], returns the top-level arguments (as text) and the end index."""
    depth, i, cur, args, quote = 0, start, [], [], None
    while i < len(src):
        ch = src[i]
        if quote:
            cur.append(ch)
            if ch == "\\":
                cur.append(src[i + 1] if i + 1 < len(src) else "")
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(cur).strip())
                return [a for a in args if a], i
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    return None, i


def strip_code(s):
    """Drops comments and turns every string into "" (in a single pass, so a // inside a string or a
    quote inside a comment does not throw anything off)."""
    return TOKENS.sub(lambda m: "" if m.group(0)[0] == "/" else '""', s)


def check_calls(body):
    problems = []
    has_wrapper = bool(re.search(r"(async\s+)?function\s+run\s*\(|\brun\s*=", body))
    for cm in CALL.finditer(body):
        name = cm.group(1)
        if name == "run" and not has_wrapper:
            continue  # a run(...) that is not the skill's wrapper
        if re.search(r"function\s+$", body[max(0, cm.start() - 20):cm.start()]):
            continue  # the definition itself
        args, _ = split_args(body, cm.end())
        if args is None:
            continue
        line = body.count("\n", 0, cm.start()) + 1
        if len(args) < 2:
            problems.append(f"line {line}: {name}() without options (model/effort missing)")
            continue
        opts = args[-1]
        if not opts.startswith("{") or "..." in opts or re.search(r"\bagentType\s*:", opts):
            continue  # variable (not checkable), spread or custom agent
        missing = [k for k in ("model", "effort") if not re.search(rf"\b{k}\s*[:,}}]", opts)]
        if missing:
            problems.append(f"line {line}: {name}() without {' or '.join(missing)}")
    return problems


def plain(s):
    return " ".join(str(s).casefold().split())


def check_phases(script, run_id):
    """Literal phases of the script that the registered plan does not have. Without the run file
    (another data folder, a run registered elsewhere) nothing can be compared and nothing is said."""
    try:
        run = json.loads((DATA / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    planned = {plain(st.get("phase", "")) for st in (run.get("plan") or {}).get("stages", [])}
    code = TOKENS.sub(lambda m: "" if m.group(0)[0] == "/" else m.group(0), script)   # comments out, strings kept
    missing = sorted({m.group(2) for m in PHASE.finditer(code) if plain(m.group(2)) not in planned})
    if not missing:
        return []
    return [f"phase {', '.join(repr(x) for x in missing)} is not in the plan of run {run_id} "
            f"(plan phases: {', '.join(sorted(planned))}): rename it, or fix the plan and run `usage.py start` again"]


def check(script, depth=0):
    m = MARK.search(script)
    if not m:
        return ["missing marker `// lean-ultracode: level=thorough|light run=ID cap=N` (load the lean-ultracode skill, "
                "estimate, ask the user to confirm and register the run with usage.py start before launching)"]
    level, rest = m.group(1), m.group(2)
    if level == "off":
        return []
    problems = []
    run_id = re.search(r"\brun=(\S+)", rest)
    if not run_id:
        problems.append("the marker lacks run=ID: run `usage.py start PLAN --level N` and copy the marker it returns")
    elif depth == 0:
        problems += check_phases(script, run_id.group(1))
    body = strip_code(script)
    cap = re.search(r"\bcap=(\d[\d_]*)", rest)
    if not cap:
        problems.append("the marker lacks cap=N: `usage.py start` gives the cap")
    else:
        n = cap.group(1).replace("_", "")
        if not re.search(rf"(?<!\d){n}(?!\d)", body.replace("_", "")):
            problems.append(f"the marker's cap ({n}) does not appear in the script: set `const CAP = {n}`")
    code = TOKENS.sub(lambda m: "" if m.group(0)[0] == "/" else m.group(0), script)   # comments out, strings kept
    for mm in MODEL.finditer(code):
        if mm.group(2).strip().lower() not in ALIASES:
            line = code.count("\n", 0, mm.start()) + 1
            problems.append(f"line {line}: model '{mm.group(2)}': a per-agent model takes only an alias "
                            "(haiku, sonnet, opus, fable), not a full model ID")
    if "budget.spent()" not in body:
        problems.append("the script never checks budget.spent(): use the skill's run() wrapper with the cap (brake against loops)")
    problems += check_calls(body)
    if depth == 0:
        for cm in CHILD.finditer(script):
            path = Path(cm.group(2)).expanduser()
            try:
                child = path.read_text(encoding="utf-8")
            except OSError:
                problems.append(f"workflow({path}): cannot read the child script")
                continue
            problems += [f"{path.name}: {p}" for p in check(child, depth + 1)]
    return problems


def main():
    # the hook gets UTF-8; on Windows stdin would be decoded as cp1252
    data = json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))
    if data.get("tool_name") != "Workflow":
        return
    ti = data.get("tool_input") or {}
    script = ti.get("script")
    if not script and ti.get("scriptPath"):
        try:
            script = Path(ti["scriptPath"]).expanduser().read_text(encoding="utf-8")
        except OSError:
            return
    if not script:
        return  # saved workflow, by name
    problems = check(script)
    if not problems:
        return
    reason = "lean-ultracode: the workflow does not follow the skill —\n- " + "\n- ".join(problems[:15])
    if len(problems) > 15:
        reason += f"\n- … and {len(problems) - 15} more"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
