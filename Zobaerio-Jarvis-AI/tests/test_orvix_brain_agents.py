import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.orvix.agents import AGENT_SPECS, Agent, ChiefIntelligenceAgent
from jarvis.orvix.brain import ExecutionEngine, Planner

class PlannerTests(unittest.TestCase):
    def test_explicit_sequence_split(self):
        plan = Planner().decompose("Research databases and then write code for the import")
        self.assertGreaterEqual(len(plan.steps), 3)  # 2 user steps + verification
        self.assertEqual(plan.steps[-1].task_type, "verification")

    def test_build_pipeline(self):
        plan = Planner().decompose("Build a website for my shop")
        titles = " ".join(s.title.lower() for s in plan.steps)
        self.assertIn("implement", titles)
        self.assertIn("test", titles)

    def test_simple_command_single_step_plus_verify(self):
        plan = Planner().decompose("Hello")
        self.assertEqual(len(plan.steps), 2)

class EngineTests(unittest.TestCase):
    def test_all_steps_complete(self):
        plan = Planner().decompose("Hello")
        report = ExecutionEngine(lambda step, ctx: "did: " + step.title).execute(plan)
        self.assertEqual(report.status, "completed")

    def test_failure_retries_then_reports_partial(self):
        calls = {"n": 0}
        def flaky(step, ctx):
            calls["n"] += 1
            raise RuntimeError("boom")
        plan = Planner().decompose("Hello")
        report = ExecutionEngine(flaky).execute(plan)
        self.assertIn(report.status, ("partial", "blocked"))
        self.assertTrue(report.blockers)
        self.assertEqual(calls["n"], 4)  # 2 steps x (1 try + 1 alternative)

    def test_alternative_approach_recovers(self):
        def runner(step, ctx):
            if ctx.get("alternative_approach"):
                return "recovered"
            raise RuntimeError("first approach failed")
        plan = Planner().decompose("Hello")
        report = ExecutionEngine(runner).execute(plan)
        self.assertEqual(report.status, "completed")

class AgentTests(unittest.TestCase):
    def test_all_spec_agents_exist(self):
        names = {s.name for s in AGENT_SPECS}
        for expected in ("Chief Intelligence Agent", "Research Agent", "Coding Agent",
                         "Software Engineering Agent", "Browser Automation Agent", "Computer Use Agent",
                         "File Management Agent", "Data Analysis Agent", "Business Intelligence Agent",
                         "Productivity Agent", "Communication Agent", "Security Agent",
                         "Quality Assurance Agent", "System Administration Agent", "Workflow Automation Agent"):
            self.assertIn(expected, names)
        self.assertEqual(len(AGENT_SPECS), 16)  # Chief + 15 specialised (incl. Personal Assistant)

    def test_agent_without_provider_is_honest_blocked(self):
        agent = Agent(AGENT_SPECS[1], orchestrator=None)
        result = agent.run("do research")
        self.assertFalse(result.ok)
        self.assertIn("configured", result.blocked_reason)

    def test_chief_blocked_without_provider(self):
        chief = ChiefIntelligenceAgent()
        out = chief.execute_command("Build a website for my shop")
        self.assertEqual(out["status"], "blocked")
        self.assertTrue(out["blockers"])

    def test_chief_completes_with_fake_orchestrator(self):
        # Test double at the orchestrator boundary (unit test only).
        class FakeOrch:
            def complete(self, prompt, task_type=None, system=""):
                class R: text = "result for: " + prompt[:40]
                return R()
        chief = ChiefIntelligenceAgent(orchestrator=FakeOrch())
        out = chief.execute_command("Hello")
        self.assertEqual(out["status"], "completed")
        self.assertTrue(all(s["status"] == "completed" for s in out["steps"]))

if __name__ == "__main__":
    unittest.main()
