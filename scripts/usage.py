#!/usr/bin/env python3
"""lean-ultracode — estimate a workflow (cost, usage limits, agents and time) and measure the real run.

  usage.py status                                 current usage, mode, learned shapes and runs left open
  usage.py mode [thorough|light|75|50|25|ask|off] sets (or shows) the session mode
  usage.py estimate PLAN.json [--json]            what the user sees before confirming: agents and what
                                                  each does, cost, 5-hour and weekly %, time
  usage.py start PLAN.json --level thorough|light registers the run; returns run id, cap and marker
  usage.py finish RUN_ID --wf WF_ID [--spent N]    measures real spend and time per phase, compares them
           (or --transcripts DIR)                 with the estimate and learns each role's shape
  usage.py measure --wf WF_ID | DIR               only measures a workflow, per phase

Cost unit "u": one Opus input token. Each token type weighs by its relative price (input 1, cache
write 1.25, output 5, cache read depending on the model) and each model by its input price against
Opus (references/seeds.json, API list prices).

Each role has a SHAPE: turns, average context per turn and output tokens of one agent, at the role's
default effort; another effort scales turns and output (effort_turns, effort_output). The seeds come
from real workflows; `finish` measures the real shape of each phase and, with >=2 samples of a role,
the median of the last 5 replaces the seed. Time comes from seconds per turn of each model, also
measured and learned, and from how many agents run at once on this machine (min(16, CPUs-2)).
Real spend comes from the agents' transcripts (<workflow folder>/agent-*.jsonl), not from
budget.spent(), which also counts the main session.

How many % one million u is gets learned separately, from the history the status line writes
(~/.claude/usage-history.jsonl) against ALL local Claude Code usage in that span
(~/.claude/projects/**/*.jsonl). Only points that rise within a window count (falling ones are stale
snapshots of idle sessions) and only long spans: the % arrives rounded to integers.

Own data (runs, samples, ratios, mode) in ~/.claude/lean-ultracode/, outside any repo.
"""
import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SNAP = HOME / ".claude" / "usage-snapshot.json"
HIST = HOME / ".claude" / "usage-history.jsonl"
PROJECTS = HOME / ".claude" / "projects"
# LEAN_ULTRACODE_DATA points the own data to another folder (tests that leave the real calibration alone)
DATA = Path(os.environ.get("LEAN_ULTRACODE_DATA") or HOME / ".claude" / "lean-ultracode")
RUNS = DATA / "runs"
CALIB = DATA / "calibration.jsonl"   # one line per measured phase: real shape, time and cost, against the estimate
RATES = DATA / "rates.json"          # % per million u, per 5-hour and weekly window
MODE = DATA / "mode.json"            # mode set with /lean-ultracode (ask by default)
SEEDS = Path(__file__).resolve().parent.parent / "references" / "seeds.json"

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# thorough = the plan as is (100 %); light = cheapest model that still does it; 75/50/25 = fine cuts by argument
LEVELS = ["thorough", "light", "75", "50", "25"]
LEVEL_ALIASES = {"100": "thorough"}
LEVEL_NAMES = {"thorough": "THOROUGH (more usage and effort)", "light": "LIGHT (less usage and effort)",
               "75": "75 %", "50": "50 %", "25": "25 %"}
MODES = ["ask", "thorough", "light", "75", "50", "25", "off"]
# minimum span to measure the ratio: (seconds, % points of rise)
MIN_SPAN = {"f": (1800, 5), "w": (4 * 3600, 2)}
LEARN_MIN, LEARN_LAST = 2, 5   # minimum samples of a role to replace the seed; how many are looked at
CONC_VAR = "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"   # documented: 1-256, Claude Code >= 2.1.269


def concurrency():
    """Agents the Workflow runtime runs at once: the variable if set, otherwise min(16, CPUs-2)."""
    try:
        n = int(os.environ.get(CONC_VAR) or 0)
    except ValueError:
        n = 0
    return n if n > 0 else max(1, min(16, (os.cpu_count() or 4) - 2))


CONCURRENCY = concurrency()


def norm_level(v):
    v = str(v).strip().lower().rstrip("%").strip()
    v = LEVEL_ALIASES.get(v, v)
    if v not in LEVELS:
        raise argparse.ArgumentTypeError(f"unknown level: {v} (use {', '.join(LEVELS)})")
    return v


# ── seeds and prices ─────────────────────────────────────────────────────────────────────────────

def load_seeds():
    return json.loads(SEEDS.read_text(encoding="utf-8"))


def down(effort, steps=1):
    return EFFORTS[max(0, EFFORTS.index(effort) - steps)]


def lower_effort(a, b):
    return a if EFFORTS.index(a) <= EFFORTS.index(b) else b


def family(model_id):
    m = (model_id or "").lower()
    for f in ("haiku", "sonnet", "opus", "fable"):
        if f in m:
            return f
    return "opus"


def weight(model, seeds):
    return seeds["model_weight"].get(model, 1.0)


def cheaper(a, b, seeds):
    return a if weight(a, seeds) <= weight(b, seeds) else b


def cache_read_price(model, seeds):
    cr = seeds["price"]["cache_read"]
    return cr.get(model, 0.1) if isinstance(cr, dict) else cr


def token_cost(model, tok, seeds):
    """Cost in u of a handful of tokens of one model: input, cache write and read, output."""
    p = seeds["price"]
    return weight(model, seeds) * (p["input"] * tok["input"] + p["cache_write"] * tok["cache_write"]
                                   + cache_read_price(model, seeds) * tok["cache_read"] + p["output"] * tok["output"])


# ── shapes by role and seconds per turn: seeded or learned ──────────────────────────────────────

def seed_shape(role, seeds):
    r = seeds["roles"].get(role) or seeds["roles"]["analysis"]
    return {"turns": r["turns"], "ctx": r["ctx"], "write": None, "output": r["output"],
            "effort": r["effort"], "src": "seed", "n": 0}


def learned_shapes(samples, seeds):
    """Median of each role's latest samples (turns and output normalised to the base effort)."""
    eo, et, by_role, out = seeds["effort_output"], seeds["effort_turns"], {}, {}
    for s in samples:
        if s.get("role") in seeds["roles"] and s.get("turns"):
            by_role.setdefault(s["role"], []).append(s)
    for role, ss in by_role.items():
        ss = ss[-LEARN_LAST:]
        if len(ss) < LEARN_MIN:
            continue
        base = seeds["roles"][role]["effort"]
        eff = lambda s: s.get("effort") or base
        out[role] = {"turns": statistics.median(s["turns"] * et[base] / et.get(eff(s), 1.0) for s in ss),
                     "ctx": statistics.median(s["ctx"] for s in ss),
                     "write": statistics.median(s["write"] for s in ss),
                     "output": statistics.median(s["output"] * eo[base] / eo.get(eff(s), 1.0) for s in ss),
                     "effort": base, "src": f"{len(ss)} runs", "n": len(ss)}
    return out


def learned_spt(samples, seeds):
    """Seconds per turn of each model: median of the latest samples, otherwise the seed."""
    spt, by_model = dict(seeds["seconds_per_turn"]), {}
    for s in samples:
        if s.get("spt") and s.get("model"):
            by_model.setdefault(s["model"], []).append(s["spt"])
    for m, v in by_model.items():
        if len(v) >= LEARN_MIN:
            spt[m] = statistics.median(v[-LEARN_LAST:])
    return spt


def stage_shape(st, role, seeds, shapes):
    """The role's shape, with whatever the plan sets by hand (turns, ctx, output) on top."""
    shape = dict(shapes.get(role) or seed_shape(role, seeds))
    for k in ("turns", "ctx", "output"):
        if st.get(k):
            shape[k] = st[k]
    if st.get("turns") or st.get("ctx"):
        shape["write"] = None   # with another shape, the cache write goes back to the formula
    if st.get("effort") and (st.get("output") or st.get("turns")):
        shape["effort"] = st["effort"]   # values set by hand refer to the plan's effort
    return shape


def agent_cost(model, effort, shape, seeds):
    """Cost of ONE agent in u, its output tokens and its turns.

    Every turn re-reads the whole context (cache read) and writes what is new (cache write); turns and
    output scale with effort against the effort the shape was measured at (lower effort, fewer tool
    calls)."""
    p, eo, et = seeds["price"], seeds["effort_output"], seeds["effort_turns"]
    turns = shape["turns"] * et[effort] / et[shape["effort"]]
    ctx = shape["ctx"]
    write = shape["write"] if shape.get("write") else ctx * (0.5 + 0.05 * turns)
    out = shape["output"] * eo[effort] / eo[shape["effort"]]
    u = cache_read_price(model, seeds) * ctx * turns + p["cache_write"] * write + p["output"] * out
    return u * weight(model, seeds), out, turns


# ── levels and estimate ─────────────────────────────────────────────────────────────────────────

def stage_items(st):
    items = st.get("items") or []
    return [str(i) for i in items]


def stage_at_level(st, level, seeds):
    """Returns (model, effort, agents per round, votes, rounds) of a plan stage at one level.

    thorough → the plan as is: the model each task needs, every agent.
    light    → the cheapest model that still does the task, at the lowest effort (`light` table of
               seeds), one vote and one round; every agent is kept.
    Fine-grained cuts, by argument only (/lean-ultracode 75%|50%|25%):
      75 → effort -1 everywhere except synthesis
      50 → 75 + votes to 1, rounds at most 2, extraction to haiku/low, analysis and verify to sonnet/medium
      25 → 50 + design to sonnet/medium, analysis and verify only half (the most important lenses and
           findings), synthesis at medium effort, rounds 1
    A stage with agentType (or fixed) keeps its model and effort at every level: only votes, rounds and
    the halvings change. A model or effort the plan already set lower is never raised.
    """
    role = st.get("role", "analysis")
    base = seeds["roles"].get(role) or seeds["roles"]["analysis"]
    model = family(st.get("model") or base["model"])
    effort = st.get("effort") or base["effort"]
    fixed = bool(st.get("agentType") or st.get("fixed"))
    agents = st.get("agents") or len(stage_items(st)) or 1
    votes, rounds = st.get("votes", 1), st.get("rounds", 1)

    if level == "thorough":
        return model, effort, agents, votes, rounds
    if level == "light":
        votes, rounds = 1, 1
        if not fixed:
            em, ee = seeds.get("light", {}).get(role) or (model, effort)
            model, effort = cheaper(model, em, seeds), lower_effort(effort, ee)
        return model, effort, agents, votes, rounds

    lv = int(level)
    if lv <= 50:
        votes, rounds = 1, min(rounds, 2)
    if lv <= 25:
        rounds = 1
        if role in ("analysis", "verify"):
            agents = max(1, (agents + 1) // 2)
    if not fixed:
        if lv <= 75 and role != "synthesis":
            effort = down(effort)
        if lv <= 50 and role == "extraction":
            model, effort = cheaper(model, "haiku", seeds), lower_effort(effort, "low")
        if lv <= 50 and role in ("analysis", "verify"):
            model, effort = cheaper(model, "sonnet", seeds), lower_effort(effort, "medium")
        if lv <= 25 and role == "design":
            model, effort = cheaper(model, "sonnet", seeds), lower_effort(effort, "medium")
        if lv <= 25 and role == "synthesis":
            effort = lower_effort(effort, "medium")
    return model, effort, agents, votes, rounds


def stage_keys(plan):
    """The key that ties each stage to its real agents: `id`, else the phase, else phase · role when two
    stages share a phase (one progress group per phase in the script, one stage per role in the plan)."""
    stages = plan["stages"]
    phases = Counter(st.get("phase", "?") for st in stages)
    keys = []
    for st in stages:
        ph = st.get("phase", "?")
        keys.append(st.get("id") or (ph if phases[ph] == 1 else f"{ph} · {st.get('role', 'analysis')}"))
    dup = [k for k, v in Counter(keys).items() if v > 1]
    if dup:
        sys.exit(f"the plan has stages that cannot be told apart: {', '.join(dup)} — give each one an `id`")
    return keys


def estimate(plan, level, seeds, shapes, spt):
    """Cost, output and time of the plan at one level.

    Time: stages run one after another (a phase waits for the one before, or one item flows through all
    of them), and the agents of a stage run at once up to the machine's concurrency. A stage marked
    "sequential" runs its agents one by one; a plan marked "sequential" runs every agent one by one.
    "wait" = seconds of each round that are not agents (test runs, builds): they add to the time, not to
    the cost."""
    rows, cost, out, n, chain, total_s, waits = [], 0.0, 0.0, 0, 0.0, 0.0, 0.0
    serial = bool(plan.get("sequential"))
    for st, key in zip(plan["stages"], stage_keys(plan)):
        role = st.get("role", "analysis")
        model, effort, agents, votes, rounds = stage_at_level(st, level, seeds)
        count = agents * votes * rounds
        shape = stage_shape(st, role, seeds, shapes)
        u1, o1, t1 = agent_cost(model, effort, shape, seeds)
        secs = t1 * spt.get(model, 20)
        wait = float(st.get("wait") or 0) * rounds
        items = stage_items(st)
        if items and agents < len(items):
            items = items[:agents]
        rows.append({"key": key, "phase": st.get("phase", "?"), "does": st.get("does", ""), "items": items,
                     "prefix": st.get("prefix"), "role": role,
                     "agents": count, "per_round": agents, "votes": votes, "rounds": rounds, "model": model,
                     "effort": effort, "agent_type": st.get("agentType"), "turns": t1, "ctx": shape["ctx"],
                     "output": o1, "seconds": secs, "wait": wait, "cost": count * u1, "src": shape["src"]})
        cost += count * u1
        out += count * o1
        n += count
        if serial or st.get("sequential"):
            chain += secs * count + wait        # its agents, one after another
        else:
            chain += secs * rounds + wait       # one agent per round on the critical path
        total_s += secs * count
        waits += wait
    seq = total_s + waits                       # everything, one after another
    wall = seq if serial else min(seq, max(chain, total_s / CONCURRENCY + waits) * 1.1)
    return {"rows": rows, "cost": cost, "output": out, "agents": n, "wall_s": wall, "seq_s": seq,
            "wait_s": waits, "sequential": serial, "concurrency": CONCURRENCY}


# ── real measurement from a workflow's transcripts ──────────────────────────────────────────────

def find_wf_dir(wf):
    hits = [p for p in PROJECTS.glob(f"*/*/subagents/workflows/{wf}*") if p.is_dir()]
    if not hits:
        sys.exit(f"cannot find the folder of workflow {wf} under {PROJECTS}/*/*/subagents/workflows/")
    return max(hits, key=lambda p: p.stat().st_mtime)


def agent_labels(tdir):
    """agentId → (phase, label), from the .meta.json files and, on top, from the journal."""
    out = {}
    for mp in tdir.rglob("agent-*.meta.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        out[mp.name[len("agent-"):-len(".meta.json")]] = (m.get("workflowPhase"), m.get("description"))
    j = tdir / "journal.jsonl"
    if j.exists():
        for line in j.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "started" and e.get("agentId"):
                old = out.get(e["agentId"], (None, None))
                out[e["agentId"]] = (e.get("phase") or old[0], e.get("label") or old[1])
    return out


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def read_agent(f):
    """A message appears several times in the transcript (once per block): count the last entry of
    each id. Returns (model, turns, tokens, start, end) or None if the agent never spoke."""
    last, t0, t1 = {}, None, None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = parse_ts(e.get("timestamp") or "")
        if t:
            t0 = t if t0 is None else min(t0, t)
            t1 = t if t1 is None else max(t1, t)
        m = e.get("message") or {}
        if isinstance(m, dict) and m.get("usage") and m.get("id"):
            last[m["id"]] = m
    if not last:
        return None
    tok = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for m in last.values():
        u = m["usage"]
        tok["input"] += u.get("input_tokens") or 0
        tok["cache_write"] += u.get("cache_creation_input_tokens") or 0
        tok["cache_read"] += u.get("cache_read_input_tokens") or 0
        tok["output"] += u.get("output_tokens") or 0
    return family(next(iter(last.values())).get("model")), len(last), tok, t0, t1


def assign(a, rows):
    """The plan stage an agent belongs to: its label among a stage's items, else a stage whose `prefix`
    starts its label, else the only stage with its phase (or, with several, the only one of its model).
    None if nothing matches."""
    label, phase = a.get("label") or "", norm(a["phase"])
    for r in rows:
        if label and label in (r.get("items") or []):
            return r["key"]
    for r in rows:
        if label and r.get("prefix") and label.startswith(r["prefix"]):
            return r["key"]
    same = [r for r in rows if norm(r["phase"]) == phase]
    if len(same) > 1:
        same = [r for r in same if r["model"] == a["model"]]
    return same[0]["key"] if len(same) == 1 else None


def measure(tdir, seeds, since=None, rows=None):
    """Real spend and time of a workflow's agents, agent by agent and grouped by phase (or, given the
    plan's rows, by stage). With `since` (epoch), agents that started before it are left out: a run
    relaunched with resumeFromRunId writes in the same folder as the first one."""
    tdir = Path(tdir).expanduser()
    labels, agents, before = agent_labels(tdir), [], 0
    for f in sorted(tdir.rglob("agent-*.jsonl")):
        r = read_agent(f)
        if not r:
            continue
        model, turns, tok, t0, t1 = r
        if since and t0 and t0 < since - 120:
            before += 1
            continue
        phase, label = labels.get(f.name[len("agent-"):-len(".jsonl")], (None, None))
        dur = (t1 - t0) if (t0 and t1) else 0.0
        agents.append({"phase": phase or "?", "label": label, "model": model, "turns": turns,
                       "ctx": tok["cache_read"] / turns, "write": tok["cache_write"], "output": tok["output"],
                       "cost": token_cost(model, tok, seeds), "tok": tok, "dur": dur, "spt": dur / turns,
                       "t0": t0, "t1": t1})
    for a in agents:
        a["group"] = (assign(a, rows) if rows else None) or a["phase"]
    by_phase = {}
    for a in agents:
        ph = by_phase.setdefault(a["group"], {"agents": 0, "cost": 0.0, "models": Counter(),
                                              "turns": [], "ctx": [], "write": [], "output": [], "spt": [], "dur": []})
        ph["agents"] += 1
        ph["cost"] += a["cost"]
        ph["models"][a["model"]] += 1
        for k in ("turns", "ctx", "write", "output", "spt", "dur"):
            ph[k].append(a[k])
    for ph in by_phase.values():
        ph["model"] = ph["models"].most_common(1)[0][0]
        ph["models"] = "/".join(sorted(ph["models"]))
    starts = [a["t0"] for a in agents if a["t0"]]
    ends = [a["t1"] for a in agents if a["t1"]]
    wall = (max(ends) - min(starts)) if starts and ends else 0.0
    return {"agents": len(agents), "cost": sum(a["cost"] for a in agents), "by_phase": by_phase, "list": agents,
            "wall_s": wall, "seq_s": sum(a["dur"] for a in agents), "output": sum(a["output"] for a in agents),
            "before": before}


# ── current usage and % per million u ───────────────────────────────────────────────────────────

def measure_window(t0, t1, seeds):
    """Cost of ALL local Claude Code usage between t0 and t1 (epoch): main sessions, subagents and
    other open sessions. That is what moves the usage %, so the calibration is against this (not
    against the workflow alone)."""
    seen = {}
    for f in PROJECTS.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < t0:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m, t = e.get("message") or {}, parse_ts(e.get("timestamp") or "")
            if not (isinstance(m, dict) and m.get("usage") and m.get("id") and t):
                continue
            if t0 <= t <= t1:
                seen[m["id"]] = m
    cost = 0.0
    for m in seen.values():
        u = m["usage"]
        tok = {"input": u.get("input_tokens") or 0, "cache_write": u.get("cache_creation_input_tokens") or 0,
               "cache_read": u.get("cache_read_input_tokens") or 0, "output": u.get("output_tokens") or 0}
        cost += token_cost(family(m.get("model")), tok, seeds)
    return cost, len(seen)


def read_snapshot():
    if not SNAP.exists():
        return None
    try:
        s = json.loads(SNAP.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    rl = s.get("rate_limits") or {}
    if not rl.get("five_hour") and not rl.get("seven_day"):
        return None
    return s


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def envelope(pts):
    """Within a window usage only rises: a point below the maximum seen is the stale snapshot of an
    idle session. Keep only the rising ones."""
    keep, top = [], None
    for t, p in sorted(pts):
        if top is None or p > top:
            keep.append((t, p))
            top = p
    return keep


def update_rates(seeds):
    """Recomputes % per million u from every window in the history (from its first to its last rising
    point) that is long enough. One ratio per window; the median of the last 5 is used. If prices
    change (seeds.version) all of them are recomputed."""
    rates = json.loads(RATES.read_text(encoding="utf-8")) if RATES.exists() else {}
    groups, pv = {}, seeds.get("version")
    for h in read_jsonl(HIST):
        for k, rk in (("f", "fr"), ("w", "wr")):
            if h.get(k) is not None and h.get(rk) and h.get("ts"):
                groups.setdefault((k, h[rk]), []).append((h["ts"], h[k]))
    for (k, reset), pts in groups.items():
        pts = envelope(pts)
        (t0, p0), (t1, p1) = pts[0], pts[-1]
        span, rise = MIN_SPAN[k]
        gid, old = f"{k}:{reset}", rates.get(f"{k}:{reset}")
        if t1 - t0 < span or p1 - p0 < rise or (old and old["t1"] == t1 and old.get("pv") == pv):
            continue
        cost, _ = measure_window(t0, t1, seeds)
        if cost > 0:
            rates[gid] = {"k": k, "t0": t0, "t1": t1, "rise": p1 - p0, "points": len(pts), "cost": cost,
                          "r": (p1 - p0) / (cost / 1e6), "pv": pv}
    DATA.mkdir(parents=True, exist_ok=True)
    RATES.write_text(json.dumps(rates, indent=1), encoding="utf-8")
    return rates


WINDOW_S = {"f": 5 * 3600, "w": 7 * 86400}
PROVISIONAL_MIN_PCT = 5   # the % arrives rounded: below 5 points the rounding is over 10 % of it


def provisional_rate(k, snap, seeds):
    """With no valid span in the history yet: the % the current window already shows against all local
    usage since it began (resets_at minus its length). One point, so the rounding of the % weighs more
    and usage on other machines counts as if it were here: provisional, and said so."""
    rl = ((snap or {}).get("rate_limits") or {}).get("five_hour" if k == "f" else "seven_day") or {}
    pct, reset, now = rl.get("used_percentage"), rl.get("resets_at"), (snap or {}).get("ts") or time.time()
    if pct is None or not reset or pct < PROVISIONAL_MIN_PCT:
        return None
    cost, _ = measure_window(reset - WINDOW_S[k], now, seeds)
    return pct / (cost / 1e6) if cost > 0 else None


def learned_spent_ratio(samples, seeds):
    r = [s["spent_ratio"] for s in samples if s.get("spent_ratio")][-LEARN_LAST:]
    return (statistics.median(r), len(r)) if len(r) >= 1 else (seeds.get("spent_ratio", 5.0), 0)


def calibration(seeds, snap=None):
    samples = read_jsonl(CALIB)
    c = {"samples": sum(1 for s in samples if s.get("role")), "shapes": learned_shapes(samples, seeds),
         "spt": learned_spt(samples, seeds), "r5": None, "r7": None, "n5": 0, "n7": 0, "provisional": []}
    c["spent_ratio"], c["n_spent"] = learned_spent_ratio(samples, seeds)
    rates = update_rates(seeds)
    for k, rk, nk in (("f", "r5", "n5"), ("w", "r7", "n7")):
        r = [v["r"] for v in sorted(rates.values(), key=lambda v: v["t1"]) if v["k"] == k][-5:]
        c[nk] = len(r)
        c[rk] = statistics.median(r) if r else None
        if c[rk] is None and snap:
            c[rk] = provisional_rate(k, snap, seeds)
            if c[rk] is not None:
                c["provisional"].append(k)
    return c


def current_pct(snap):
    rl = (snap or {}).get("rate_limits") or {}
    return (rl.get("five_hour") or {}).get("used_percentage"), (rl.get("seven_day") or {}).get("used_percentage")


# ── mode ───────────────────────────────────────────────────────────────────────────────────────

def read_mode():
    try:
        m = json.loads(MODE.read_text(encoding="utf-8"))
        return m if m.get("mode") in MODES else {"mode": "ask"}
    except (OSError, ValueError):
        return {"mode": "ask"}


def mode_line():
    m = read_mode()
    since = f" — set on {datetime.fromtimestamp(m['ts']):%d/%m %H:%M}" if m.get("ts") else ""
    if m["mode"] == "ask":
        return "Mode: ask (default: estimate and ask for confirmation, thorough or light, before every workflow)"
    if m["mode"] == "off":
        return f"Mode: off (no tiering and no confirmation; the script carries level=off){since}"
    return f"Mode: {LEVEL_NAMES.get(m['mode'], m['mode'])} fixed (the estimate is shown, no question){since}"


# ── formatting ─────────────────────────────────────────────────────────────────────────────────

def fmt_u(n):
    return f"{n / 1e6:.1f}M u" if n >= 1e6 else f"{n / 1e3:.0f}k u"


def fmt_k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.1f}k" if n >= 1e3 else f"{n:.0f}")


def fmt_usd(u, seeds):
    return f"${u / 1e6 * seeds.get('usd_per_mu', 4.0):.0f}"


def fmt_dur(s):
    s = int(round(s))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    return f"{s // 3600} h {(s % 3600) // 60:02d}"


def fmt_reset(epoch):
    if not epoch:
        return "?"
    d = datetime.fromtimestamp(epoch)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return d.strftime("%H:%M") if epoch - time.time() < 86400 else f"{days[d.weekday()]} {d:%d %H:%M}"


def usage_line(snap):
    if not snap:
        return "Current usage: no data (the status line has not written ~/.claude/usage-snapshot.json yet)"
    rl = snap["rate_limits"]
    f, w = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    age = (time.time() - snap.get("ts", 0)) / 60
    stale = f"  (snapshot {age:.0f} min old)" if age > 15 else ""
    pct = lambda x: f"{x['used_percentage']:.0f} %" if "used_percentage" in x else "?"
    return (f"Current usage:  5 h {pct(f)} (resets {fmt_reset(f.get('resets_at'))})"
            f"   ·   weekly {pct(w)} (resets {fmt_reset(w.get('resets_at'))}){stale}")


def shapes_line(c, seeds):
    learned = [f"{r} ({s['n']})" for r, s in c["shapes"].items()]
    seed = [r for r in seeds["roles"] if r not in c["shapes"]]
    parts = []
    if learned:
        parts.append("learned from real runs: " + ", ".join(learned))
    if seed:
        parts.append("seed: " + ", ".join(seed))
    return "Shapes by role — " + " · ".join(parts)


def rates_lines(c):
    if c["r5"] is None:
        return ["Usage ratio: no valid 5-hour span yet (needs >=30 min and >=5 rising points in the history, "
                f"or a current window at >={PROVISIONAL_MIN_PCT} %) — no % is projected"]
    src = lambda k, n: "provisional: current window only" if k in c["provisional"] else f"{n} spans"
    r7 = f"{c['r7']:.2f} % weekly ({src('w', c['n7'])})" if c["r7"] is not None else "weekly: no span yet"
    return [f"Usage ratio: 1M u ≈ {c['r5']:.1f} % of 5 h ({src('f', c['n5'])}) · {r7}"]


def open_runs():
    out = []
    for p in sorted(RUNS.glob("*.json")) if RUNS.exists() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("state") == "running":
            out.append(r)
    return out


def phase_table(m):
    """Real table per phase: agents, model, medians of turns/context/output/duration and cost."""
    lines = [f"  {'Phase':<16}{'Agents':>8}  {'Model':<12}{'Turns':>7}{'Ctx':>9}{'Output':>9}{'Durat.':>8}{'Cost':>10}"]
    for name, ph in sorted(m["by_phase"].items(), key=lambda kv: -kv[1]["cost"]):
        med = lambda k: statistics.median(ph[k])
        lines.append(f"  {name[:16]:<16}{ph['agents']:>8}  {ph['models'][:12]:<12}{med('turns'):>7.0f}"
                     f"{fmt_k(med('ctx')):>9}{fmt_k(med('output')):>9}{fmt_dur(med('dur')):>8}{fmt_u(ph['cost']):>10}")
    return lines


def wrap_items(items, width=100, indent="      "):
    """The labels of a phase's agents, in a few lines."""
    lines, cur = [], ""
    for it in items:
        piece = it if not cur else ", " + it
        if len(cur) + len(piece) > width and cur:
            lines.append(indent + cur + ",")
            cur = it
        else:
            cur += piece
    if cur:
        lines.append(indent + cur)
    if len(lines) > 3:
        lines = lines[:3] + [indent + f"… ({len(items)} in total)"]
    return lines


# ── commands ───────────────────────────────────────────────────────────────────────────────────

def cmd_status(_):
    seeds = load_seeds()
    snap = read_snapshot()
    print(usage_line(snap))
    print(mode_line())
    c = calibration(seeds, snap)
    print(shapes_line(c, seeds))
    print("Seconds per turn: " + " · ".join(f"{m} {v:.0f}" for m, v in c["spt"].items())
          + f" · {CONCURRENCY} agents at once on this machine ({os.cpu_count()} CPUs"
          + (f", {CONC_VAR}={os.environ[CONC_VAR]})" if os.environ.get(CONC_VAR) else f"; {CONC_VAR} raises the limit)"))
    for line in rates_lines(c):
        print(line)
    print(f"Brake: budget.spent() ≈ {c['spent_ratio']:.1f}× the agents' real output "
          + (f"(learned, {c['n_spent']} runs)" if c["n_spent"] else "(seed; `finish --spent` learns it)"))
    for r in open_runs():
        print(f"Run left open: {r['id']} ({r['name']}, level {r['level']}) → "
              f"usage.py finish {r['id']} --wf <Workflow runId>")


def cmd_mode(a):
    if a.value:
        DATA.mkdir(parents=True, exist_ok=True)
        MODE.write_text(json.dumps({"mode": a.value, "ts": time.time()}), encoding="utf-8")
    print(mode_line())


def pct_of(cost, r):
    return None if r is None else cost / 1e6 * r


def impact_line(cost, c, p5, p7):
    d5, d7 = pct_of(cost, c["r5"]), pct_of(cost, c["r7"])
    if d5 is None or p5 is None:
        return ""
    s = f" → 5 h +{d5:.0f} % ({p5:.0f} → {p5 + d5:.0f} %)"
    if d7 is not None and p7 is not None:
        s += f" · weekly +{d7:.0f} % ({p7:.0f} → {p7 + d7:.0f} %)"
    if p5 + d5 >= 90:
        s += "  ⚠ leaves the 5-hour window above 90 %"
    return s


def print_mode(title, e, c, p5, p7, seeds, items):
    """One full mode: header with cost, % and time, and the per-phase table with what each agent does."""
    print(f"{title}: {e['agents']} agents · ~{fmt_u(e['cost'])} (Opus-input-equivalent tokens, "
          f"≈ {fmt_usd(e['cost'], seeds)} of API){impact_line(e['cost'], c, p5, p7)}")
    waits = f"; {fmt_dur(e['wait_s'])} of it waiting on non-agent work" if e.get("wait_s") else ""
    if e.get("sequential"):
        print(f"  Time ~{fmt_dur(e['wall_s'])}, every agent one after another{waits}")
    else:
        print(f"  Time ~{fmt_dur(e['wall_s'])} in parallel ({e['concurrency']} agents at once on this machine; "
              f"in sequence ~{fmt_dur(e['seq_s'])}{waits})")
    print(f"  {'Phase · what each agent does':<46}{'Agents':>14}  {'Model / effort':<18}{'Turns':>7}{'Durat.':>8}{'Cost':>9}{'5 h':>6}{'week':>6}")
    for r in e["rows"]:
        head = r["phase"] + (" · " + r["does"] if r["does"] else "")
        mult = ""
        if r["votes"] > 1 or r["rounds"] > 1:
            mult = f" ({r['per_round']}" + (f"×{r['votes']} votes" if r["votes"] > 1 else "") + (f"×{r['rounds']} rounds" if r["rounds"] > 1 else "") + ")"
        model = (r["agent_type"] or (r["model"] + " / " + r["effort"]))[:18]
        s5, s7 = pct_of(r["cost"], c["r5"]), pct_of(r["cost"], c["r7"])
        print(f"  {head[:46]:<46}{(str(r['agents']) + mult)[:14]:>14}  {model:<18}{r['turns']:>7.0f}{fmt_dur(r['seconds']):>8}"
              f"{fmt_u(r['cost']):>9}{(f'{s5:.0f} %' if s5 is not None else '—'):>6}{(f'{s7:.0f} %' if s7 is not None else '—'):>6}")
        if r.get("wait"):
            print(f"      + {fmt_dur(r['wait'])} waiting (tests, builds)")
        if items:
            for line in wrap_items(r["items"]):
                print(line)


def risk_lines(deep, light, seeds):
    """What light mode gives up, stage by stage: the model or effort it lowers and what that risks."""
    risk, out = seeds.get("risk", {}), []
    for d, l in zip(deep["rows"], light["rows"]):
        if (d["model"], d["effort"]) == (l["model"], l["effort"]):
            continue
        what = risk.get(d["role"]) if d["model"] != l["model"] else "lower effort: fewer tool calls, less checking"
        out.append(f"    {d['key'][:28]}: {d['model']}/{d['effort']} → {l['model']}/{l['effort']}"
                   + (f" — {what}" if what else ""))
    return out


def cmd_estimate(a):
    seeds, plan, snap = load_seeds(), json.loads(Path(a.plan).read_text(encoding="utf-8")), read_snapshot()
    c = calibration(seeds, snap)
    ests = {lv: estimate(plan, lv, seeds, c["shapes"], c["spt"]) for lv in LEVELS}
    if a.json:
        print(json.dumps({"calibration": {k: v for k, v in c.items() if k != "shapes"}, "levels": ests}, indent=1))
        return
    p5, p7 = current_pct(snap)
    deep, light = ests["thorough"], ests["light"]
    print(usage_line(snap))
    print(mode_line())
    print()
    print(f"Workflow '{plan.get('name', '?')}': {len(plan['stages'])} phases · " + shapes_line(c, seeds))
    print()
    print_mode(LEVEL_NAMES["thorough"], deep, c, p5, p7, seeds, items=True)
    print()
    print_mode(LEVEL_NAMES["light"], light, c, p5, p7, seeds, items=False)
    print(f"  Same agents and coverage, one vote and one round: {light['cost'] / (deep['cost'] or 1) * 100:.0f} % of the cost "
          f"of the thorough mode, {light['agents']} agents against {deep['agents']}")
    risks = risk_lines(deep, light, seeds)
    if risks:
        print("  What light risks:")
        for line in risks:
            print(line)
    print()
    cuts = []
    for lv in ("75", "50", "25"):
        x = ests[lv]
        line = f"{lv} % ~{fmt_u(x['cost'])} ({x['cost'] / (deep['cost'] or 1) * 100:.0f} %), {x['agents']} agents, ~{fmt_dur(x['wall_s'])}"
        dd = pct_of(x["cost"], c["r5"])
        if dd is not None and p5 is not None:
            line += f" → 5 h {p5 + dd:.0f} %"
        cuts.append(line)
    print("  Fine-grained cuts, by argument only (/lean-ultracode 75%|50%|25%): " + "  ·  ".join(cuts))
    if c["r5"] is None:
        print("  (no projected %: the usage ratio has no valid span in the history yet)")
    elif "f" in c["provisional"]:
        print("  (projected % provisional: measured on the current window only, until the history has a full span)")


def cmd_start(a):
    seeds, plan = load_seeds(), json.loads(Path(a.plan).read_text(encoding="utf-8"))
    c = calibration(seeds)
    e = estimate(plan, a.level, seeds, c["shapes"], c["spt"])
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    # budget.spent() counts the turn's output tokens, main session included: the script measures it
    # from its own start (S0). It counts several times what the agents' transcripts show (spent_ratio,
    # learned by `finish --spent`), so the cap is in its units, not in transcript output. In thorough
    # mode it is only a brake against loops that never converge (runaway_factor); in light somewhat
    # shorter; in the fine cuts, the agreed one.
    factor = {"thorough": seeds.get("runaway_factor", 3.0), "light": seeds.get("light_factor", 2.0)}.get(
        a.level, seeds.get("cut_factor", 1.5))
    cap = max(20000, int(e["output"] * factor * c["spent_ratio"]))
    marker = f"// lean-ultracode: level={a.level} run={run_id} cap={cap}"
    run = {"id": run_id, "name": plan.get("name", "?"), "level": a.level, "state": "running",
           "started_at": time.time(), "est_cost": e["cost"], "est_output": e["output"], "est_wall_s": e["wall_s"],
           "cap": cap, "spent_ratio": c["spent_ratio"], "rows": e["rows"], "plan": plan}
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{run_id}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "level": a.level, "est_cost": int(e["cost"]), "est_output": int(e["output"]),
                      "est_wall_min": round(e["wall_s"] / 60, 1), "cap": cap, "marker": marker}))


def cmd_measure(a):
    seeds = load_seeds()
    tdir = find_wf_dir(a.wf) if a.wf else Path(a.dir)
    m = measure(tdir, seeds, since=a.since)
    if not m["agents"]:
        sys.exit(f"no agent-*.jsonl transcripts with usage in {tdir}")
    print(f"Workflow in {tdir}")
    for line in phase_table(m):
        print(line)
    print(f"  {m['agents']} agents · real cost {fmt_u(m['cost'])} (≈ {fmt_usd(m['cost'], seeds)}) · "
          f"{fmt_dur(m['wall_s'])} wall-clock ({fmt_dur(m['seq_s'])} of agent work)")


def norm(s):
    return " ".join(str(s or "").casefold().split())


def cmd_finish(a):
    seeds = load_seeds()
    p = RUNS / f"{a.run_id}.json"
    if not p.exists():
        sys.exit(f"run {a.run_id} does not exist")
    run = json.loads(p.read_text(encoding="utf-8"))
    if run.get("state") == "closed":
        sys.exit(f"run {a.run_id} is already closed")
    if not (a.wf or a.transcripts):
        sys.exit("pass --wf <Workflow runId> or --transcripts <folder>")
    tdir = find_wf_dir(a.wf) if a.wf else Path(a.transcripts).expanduser()
    plan_rows = run.get("rows", [])
    for r in plan_rows:
        r.setdefault("key", r["phase"])   # runs registered before stage keys existed
    # only the agents of THIS run: a relaunch with resumeFromRunId shares the folder with the first one
    m = measure(tdir, seeds, since=None if a.all_agents else run.get("started_at"), rows=plan_rows)
    if not m["agents"]:
        sys.exit(f"no agent-*.jsonl transcripts with usage in {tdir}: not closing")
    if m["before"]:
        print(f"  {m['before']} agents started before this run (an earlier run in the same folder) are left out"
              " — --all-agents counts them")

    rows = {norm(r["key"]): r for r in plan_rows}
    samples, seen = [], set()
    arrow = lambda x, y, f: f"{f(x)} → {f(y)}"
    row = lambda *c: "  " + " ".join(f"{str(v)[:w]:{a}{w}}" for v, w, a in
                                    zip(c, (14, 9, 16, 10, 17, 15, 15, 24), ("<", ">", "<", ">", ">", ">", ">", ">")))
    print(row("Phase", "Agents", "Model", "Turns", "Ctx", "Output", "Durat.", "Cost est → real"))
    for name, ph in sorted(m["by_phase"].items(), key=lambda kv: -kv[1]["cost"]):
        med = lambda k: statistics.median(ph[k])
        r = rows.get(norm(name))
        if r:
            seen.add(norm(name))
            print(row(name, arrow(r["agents"], ph["agents"], str), r["model"] + " → " + ph["models"],
                      arrow(r["turns"], med("turns"), lambda v: f"{v:.0f}"), arrow(r["ctx"], med("ctx"), fmt_k),
                      arrow(r["output"], med("output"), fmt_k), arrow(r["seconds"], med("dur"), fmt_dur),
                      arrow(r["cost"], ph["cost"], fmt_u)))
            samples.append({"ts": time.time(), "run": run["id"], "name": run["name"], "level": run["level"],
                            "phase": name, "role": r["role"], "model": ph["model"], "effort": r["effort"],
                            "agents": ph["agents"], "turns": med("turns"), "ctx": med("ctx"), "write": med("write"),
                            "output": med("output"), "spt": med("spt"), "cost": ph["cost"], "est_cost": r["cost"]})
        else:
            print(row(name, f"— → {ph['agents']}", ph["models"], f"{med('turns'):.0f}", fmt_k(med("ctx")),
                      fmt_k(med("output")), fmt_dur(med("dur")), "— → " + fmt_u(ph["cost"])) + "   (phase not in the plan)")
    for k, r in rows.items():
        if k not in seen:
            print(row(r["key"], f"{r['agents']} → 0", r["model"], "", "", "", "", fmt_u(r["cost"]) + " → 0")
                  + "   (planned, no agents: different phase name, or labels outside its items/prefix?)")
    ratio = m["cost"] / run["est_cost"] if run.get("est_cost") else float("nan")
    print(f"  total: estimated {fmt_u(run['est_cost'])} → real {fmt_u(m['cost'])} (×{ratio:.2f}; ≈ {fmt_usd(m['cost'], seeds)})"
          + (f" · {a.dropped} agents not launched by the cap" if a.dropped else ""))
    if run.get("est_wall_s"):
        print(f"  time: estimated {fmt_dur(run['est_wall_s'])} → real {fmt_dur(m['wall_s'])} wall-clock "
              f"({fmt_dur(m['seq_s'])} of agent work, {m['seq_s'] / (m['wall_s'] or 1):.1f} at once on average)")
    if a.spent and m["output"]:
        sr = a.spent / m["output"]
        print(f"  brake: budget.spent() {fmt_k(a.spent)} against {fmt_k(m['output'])} of agent output = ×{sr:.1f}"
              f" (cap was {fmt_k(run.get('cap', 0))}) — learned for the next cap")
        samples.append({"ts": time.time(), "run": run["id"], "spent_ratio": sr})
    elif not a.spent:
        print("  (no --spent: return `spent: budget.spent() - S0` from the script so the cap learns its units)")
    DATA.mkdir(parents=True, exist_ok=True)
    with CALIB.open("a", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(s) + "\n")
    run.update(state="closed", finished_at=time.time(), transcripts=str(tdir), dropped=a.dropped,
               measured={"agents": m["agents"], "cost": m["cost"], "wall_s": m["wall_s"], "seq_s": m["seq_s"],
                         "by_phase": {k: {"agents": v["agents"], "cost": v["cost"], "models": v["models"]}
                                      for k, v in m["by_phase"].items()}})
    p.write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(f"  {sum(1 for s in samples if s.get('role'))} phases learned · run {run['id']} closed")


def main():
    # Windows consoles default to cp1252, which cannot print «≈» or «→»
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=cmd_status)
    s = sub.add_parser("mode")
    s.add_argument("value", nargs="?", type=lambda v: LEVEL_ALIASES.get(v.strip().lower().rstrip("%"), v.strip().lower().rstrip("%")),
                   choices=MODES, help="ask | thorough | light | 75 | 50 | 25 | off")
    s.set_defaults(f=cmd_mode)
    s = sub.add_parser("estimate")
    s.add_argument("plan")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_estimate)
    s = sub.add_parser("start")
    s.add_argument("plan")
    s.add_argument("--level", type=norm_level, required=True, help="thorough | light | 75 | 50 | 25")
    s.set_defaults(f=cmd_start)
    s = sub.add_parser("finish")
    s.add_argument("run_id")
    s.add_argument("--wf", help="Workflow runId (wf_…), as the tool returns it")
    s.add_argument("--transcripts", help="workflow folder (transcriptDir), when the runId is not at hand")
    s.add_argument("--dropped", type=int, default=0, help="agents the cap left unlaunched")
    s.add_argument("--spent", type=int, default=0, help="the `spent` the script returned (budget.spent() - S0)")
    s.add_argument("--all-agents", action="store_true",
                   help="also count agents that started before the run (by default a resumed run's old ones are left out)")
    s.set_defaults(f=cmd_finish)
    s = sub.add_parser("measure")
    s.add_argument("dir", nargs="?")
    s.add_argument("--wf")
    s.add_argument("--since", type=float, help="epoch: leave out agents that started before it")
    s.set_defaults(f=cmd_measure)
    a = ap.parse_args()
    if a.cmd == "measure" and not (a.dir or a.wf):
        ap.error("measure: pass a folder or --wf")
    a.f(a)


if __name__ == "__main__":
    main()
