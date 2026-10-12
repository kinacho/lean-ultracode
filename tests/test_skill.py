"""Smoke tests for lean-ultracode, standard library only:  python -m unittest discover -s tests

They run every script the way Claude Code runs it (a subprocess), with HOME pointed at a temporary
folder so the real ~/.claude is never read or written, and with a cp1252 console encoding to catch
what breaks on Windows. CI runs them on Linux, macOS and Windows.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ROOT / "scripts" / "usage.py"
HOOK = ROOT / "hooks" / "workflow-guard.py"
SNAPSHOT = ROOT / "scripts" / "statusline-snapshot.sh"
PLAN = ROOT / "evals" / "files" / "plan.json"
FIXTURE = ROOT / "evals" / "files" / "review-workflow.js"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.env = dict(os.environ, HOME=str(self.tmp), USERPROFILE=str(self.tmp),
                        LEAN_ULTRACODE_DATA=str(self.data),
                        PYTHONIOENCODING="cp1252")   # a Windows console: cannot encode ≈ or →
        self.env.pop("PYTHONUTF8", None)
        for k in ("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS", "CLAUDE_CODE_EFFORT_LEVEL",
                  "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, script, *args, stdin=None):
        p = subprocess.run([sys.executable, str(script), *map(str, args)], env=self.env,
                           input=stdin, capture_output=True)
        out = p.stdout.decode("utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, f"{script.name} {args} failed:\n{p.stderr.decode('utf-8', 'replace')}")
        return out

    def write_settings(self, settings):
        d = self.tmp / ".claude"
        d.mkdir(exist_ok=True)
        (d / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def write_plan(self, plan):
        f = self.tmp / "plan.json"
        f.write_text(json.dumps(plan), encoding="utf-8")
        return f


class TestUsage(Sandbox):
    def test_status_without_any_data(self):
        out = self.run_py(USAGE, "status")
        self.assertIn("Current usage: no data", out)
        self.assertIn("Mode: ask", out)

    def test_estimate_prints_both_modes_on_a_cp1252_console(self):
        out = self.run_py(USAGE, "estimate", PLAN)
        self.assertIn("THOROUGH (more usage and effort): 14 agents", out)
        self.assertIn("LIGHT (less usage and effort): 14 agents", out)
        self.assertIn("≈", out)                   # printed, not crashed
        self.assertIn("What light risks:", out)
        self.assertIn("Verify: opus/medium → sonnet/low", out)

    def test_mode_is_remembered(self):
        self.run_py(USAGE, "mode", "light")
        self.assertIn("LIGHT", self.run_py(USAGE, "status"))

    def test_start_registers_the_run_and_gives_the_marker(self):
        r = json.loads(self.run_py(USAGE, "start", PLAN, "--level", "thorough"))
        self.assertTrue(r["marker"].startswith("// lean-ultracode: level=thorough run="))
        self.assertIn(f"cap={r['cap']}", r["marker"])
        run = json.loads((self.data / "runs" / f"{r['run_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(run["state"], "running")
        # the cap is in budget.spent() units: estimated output x 3 (thorough) x spent_ratio (seed 5)
        self.assertEqual(r["cap"], int(r["est_output"] * 3 * 5))

    STAGES = [{"phase": "Feature", "prefix": "impl:", "role": "implement", "agents": 3},
              {"phase": "Feature", "prefix": "fix:", "role": "fix", "agents": 3},
              {"phase": "Gate", "role": "mechanical", "agents": 1, "wait": 3600}]

    def times(self, sequential, concurrency):
        # fixed concurrency: otherwise it comes from the CPUs, and a 3-CPU runner runs one agent at a time
        self.env["CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"] = str(concurrency)
        plan = {"name": "t", "stages": self.STAGES, **({"sequential": True} if sequential else {})}
        return json.loads(self.run_py(USAGE, "estimate", self.write_plan(plan), "--json"))["levels"]["thorough"]

    def test_sequential_plan_takes_as_long_as_its_sum(self):
        serial = self.times(sequential=True, concurrency=8)
        self.assertAlmostEqual(serial["wall_s"], serial["seq_s"])
        self.assertEqual(serial["wait_s"], 3600)
        free = self.times(sequential=False, concurrency=8)
        self.assertLess(free["wall_s"], serial["wall_s"])
        self.assertLessEqual(free["wall_s"], free["seq_s"])   # parallel never longer than sequential

    def test_one_agent_at_a_time_is_sequential_anyway(self):
        free = self.times(sequential=False, concurrency=1)
        self.assertAlmostEqual(free["wall_s"], free["seq_s"])

    def test_finish_matches_by_prefix_and_leaves_out_an_earlier_run(self):
        plan = self.write_plan({"name": "f", "stages": [
            {"phase": "Feature", "prefix": "impl:", "role": "implement", "agents": 1},
            {"phase": "Feature", "prefix": "fix:", "role": "fix", "agents": 1}]})
        run_id = json.loads(self.run_py(USAGE, "start", plan, "--level", "thorough"))["run_id"]
        wf = self.tmp / "wf"
        wf.mkdir()
        now = time.time()

        def agent(name, phase, label, t0, turns):
            iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
            lines = [json.dumps({"timestamp": iso(t0 + i * 10), "message": {
                "id": f"{name}-{i}", "model": "claude-opus-5-5",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                          "cache_read_input_tokens": 50000, "output_tokens": 500}}}) for i in range(turns)]
            (wf / f"agent-{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (wf / f"agent-{name}.meta.json").write_text(json.dumps(
                {"workflowPhase": phase, "description": label}), encoding="utf-8")

        agent("a", "Feature", "impl:auth", now + 5, 40)
        agent("b", "Feature", "fix:auth", now + 500, 12)
        agent("old", "Feature", "impl:auth", now - 7200, 30)   # a previous run in the same folder
        out = self.run_py(USAGE, "finish", run_id, "--transcripts", wf, "--spent", 100000)
        self.assertIn("1 agents started before this run", out)
        self.assertIn("2 phases learned", out)
        self.assertNotIn("not in the plan", out)
        self.assertIn("learned for the next cap", out)
        samples = [json.loads(l) for l in (self.data / "calibration.jsonl").read_text(encoding="utf-8").splitlines()]
        roles = {s["role"]: s["turns"] for s in samples if s.get("role")}
        self.assertEqual(roles, {"implement": 40, "fix": 12})
        self.assertTrue(any(s.get("spent_ratio") for s in samples))


    def test_an_effort_forced_by_the_environment_is_flagged(self):
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", self.run_py(USAGE, "status"))
        self.env["CLAUDE_CODE_EFFORT_LEVEL"] = "low"
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL=low overrides every agent's effort", self.run_py(USAGE, "status"))

    def test_a_forced_model_in_the_settings_is_flagged(self):
        self.write_settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})
        self.assertIn("every agent runs on haiku", self.run_py(USAGE, "estimate", PLAN))

    def test_the_size_guideline_and_the_large_workflow_warning(self):
        self.write_settings({"workflowSizeGuideline": "small"})
        out = self.run_py(USAGE, "estimate", PLAN)
        self.assertIn("14 agents against the size guideline small (fewer than 5)", out)
        self.assertIn("'Large workflow' (14 agents, over 5)", out)   # a chosen guideline moves the threshold
        self.write_settings({"workflowSizeGuideline": "large"})
        self.assertNotIn("size guideline", self.run_py(USAGE, "estimate", PLAN))


def load_usage():
    spec = importlib.util.spec_from_file_location("usage", USAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.u = load_usage()
        self.seeds = self.u.load_seeds()

    def tok(self, cache_read):
        return {"input": 10, "cache_write": 1000, "cache_read": cache_read, "output": 500}

    def test_haiku_prices_a_long_prompt_at_five_times(self):
        short, long = self.u.token_cost("haiku", self.tok(90000), self.seeds), self.u.token_cost("haiku", self.tok(150000), self.seeds)
        base = lambda cr: self.u.weight("haiku", self.seeds) * (10 + 1.25 * 1000 + 0.1 * cr + 5 * 500)
        self.assertAlmostEqual(short, base(90000))
        self.assertAlmostEqual(long, 5 * base(150000))
        # Sonnet and Opus: one price up to 1M
        self.assertAlmostEqual(self.u.token_cost("opus", self.tok(150000), self.seeds), 10 + 1250 + 0.05 * 150000 + 2500)

    def test_haiku_is_the_cheapest_by_far(self):
        self.assertLess(self.u.weight("haiku", self.seeds) * 10, self.u.weight("sonnet", self.seeds))

    def test_a_retried_agent_keeps_its_stage(self):
        rows = [{"key": "Review", "phase": "Review", "items": ["lens:a"], "model": "sonnet"},
                {"key": "Verify", "phase": "Verify", "items": [], "model": "opus"}]
        a = {"label": "lens:a (retry 2)", "phase": "Verify", "model": "opus"}
        self.assertEqual(self.u.assign(a, rows), "Review")

    def test_passing_the_limit_says_the_run_will_pause(self):
        c = {"r5": 10.0, "r7": 1.0}
        self.assertIn("pauses until the 5-hour reset", self.u.impact_line(2e6, c, 85, 20))
        self.assertIn("above 90 %", self.u.impact_line(0.6e6, c, 85, 20))
        self.assertIn("weekly limit", self.u.impact_line(2e6, c, 10, 99))


class TestHook(Sandbox):
    def hook(self, script):
        payload = json.dumps({"tool_name": "Workflow", "tool_input": {"script": script}}, ensure_ascii=False)
        out = self.run_py(HOOK, stdin=payload.encode("utf-8"))
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else None

    def fixed_fixture(self):
        return FIXTURE.read_text(encoding="utf-8").replace(
            "{ model: 'sonnet', label", "{ model: 'sonnet', effort: 'high', label") + "\n// revisión ≈ ok\n"

    def register(self, phases):
        runs = self.data / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "20261001-120000.json").write_text(json.dumps(
            {"plan": {"stages": [{"phase": p} for p in phases]}}), encoding="utf-8")

    def test_rejects_an_agent_without_effort(self):
        self.assertIn("line 20: run() without effort", self.hook(FIXTURE.read_text(encoding="utf-8")))

    def test_rejects_a_script_without_marker(self):
        self.assertIn("missing marker", self.hook("const x = await agent('hi', { model: 'haiku' })"))

    def test_passes_a_correct_script_with_non_ascii_text(self):
        self.register(["Review", "Verify"])
        self.assertIsNone(self.hook(self.fixed_fixture()))

    def test_rejects_a_full_model_id(self):
        script = self.fixed_fixture().replace("model: 'sonnet'", "model: 'claude-sonnet-5-5'")
        self.assertIn("takes only an alias", self.hook(script))

    def test_rejects_a_phase_the_plan_does_not_have(self):
        self.register(["Lenses", "Verify"])
        reason = self.hook(self.fixed_fixture())
        self.assertIn("phase 'Review' is not in the plan", reason)


def real_bash():
    """A bash that runs scripts. On Windows a bare "bash" resolves to System32's WSL launcher, which
    fails without a distro; Claude Code itself uses Git Bash there, so look it up by full path."""
    b = shutil.which("bash")
    return b if b and "system32" not in b.lower() else None


BASH = real_bash()


@unittest.skipUnless(BASH and shutil.which("jq"), "needs bash (Git Bash on Windows) and jq")
class TestStatusLine(Sandbox):
    def feed(self, five, week):
        status = {"session_id": "s", "model": {"id": "claude-opus-5-5"}, "cost": {},
                  "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": 1900000000},
                                  "seven_day": {"used_percentage": week, "resets_at": 1900500000}}}
        p = subprocess.run([BASH, SNAPSHOT.as_posix()], input=json.dumps(status).encode(),
                           env=self.env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout, b"")   # never prints into the status line

    def test_writes_the_snapshot_and_one_history_line_per_change(self):
        self.feed(3, 10)
        self.feed(3, 10)   # unchanged: no new history line
        self.feed(4, 10)
        snap = json.loads((self.tmp / ".claude" / "usage-snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["rate_limits"]["five_hour"]["used_percentage"], 4)
        hist = (self.tmp / ".claude" / "usage-history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l)["f"] for l in hist], [3, 4])

    def test_the_estimate_reads_the_snapshot(self):
        self.feed(7, 20)
        self.assertIn("5 h 7 %", self.run_py(USAGE, "status"))


if __name__ == "__main__":
    unittest.main()
