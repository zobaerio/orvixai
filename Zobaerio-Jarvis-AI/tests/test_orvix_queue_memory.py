import sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orvix.memory import LessonLog, MemoryStore, evaluate_execution
from orvix.taskqueue import TaskQueue, Worker

def tmpdir():
    return Path(tempfile.mkdtemp())

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.q = TaskQueue(tmpdir() / "q.db")
    def tearDown(self):
        self.q.close()

    def test_enqueue_claim_complete(self):
        tid = self.q.enqueue("job one", {"a": 1})
        task = self.q.claim_next()
        self.assertEqual(task["id"], tid)
        self.assertEqual(task["payload"], {"a": 1})
        self.q.complete(tid, "done")
        self.assertEqual(self.q.get(tid)["status"], "completed")

    def test_priority_order(self):
        self.q.enqueue("low", priority=9)
        high = self.q.enqueue("high", priority=1)
        self.assertEqual(self.q.claim_next()["id"], high)

    def test_pause_resume_cancel_retry(self):
        tid = self.q.enqueue("x")
        self.q.pause(tid); self.assertEqual(self.q.get(tid)["status"], "paused")
        self.assertIsNone(self.q.claim_next())
        self.q.resume(tid); self.assertEqual(self.q.get(tid)["status"], "pending")
        self.q.cancel(tid); self.assertEqual(self.q.get(tid)["status"], "cancelled")
        self.q.retry(tid); self.assertEqual(self.q.get(tid)["status"], "pending")

    def test_fail_requeues_until_max_attempts(self):
        tid = self.q.enqueue("x", max_attempts=2)
        self.q.claim_next(); self.q.fail(tid, "e1")
        self.assertEqual(self.q.get(tid)["status"], "pending")
        self.q.claim_next(); self.q.fail(tid, "e2")
        self.assertEqual(self.q.get(tid)["status"], "failed")

    def test_checkpoint_and_restart_recovery(self):
        tid = self.q.enqueue("long job")
        self.q.claim_next()
        self.q.save_checkpoint(tid, {"page": 3})
        recovered = self.q.recover()
        self.assertEqual(recovered, 1)
        task = self.q.get(tid)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["checkpoint"], {"page": 3})

    def test_worker_runs_handler(self):
        tid = self.q.enqueue("work")
        worker = Worker(self.q, lambda task, cp, cancelled: "result!")
        self.assertTrue(worker.run_once())
        self.assertEqual(self.q.get(tid)["result"], "result!")
        self.assertFalse(worker.run_once())

    def test_stats(self):
        self.q.enqueue("a"); self.q.enqueue("b")
        self.assertEqual(self.q.stats()["pending"], 2)

class MemoryTests(unittest.TestCase):
    def test_remember_recall_forget_clear(self):
        store = MemoryStore(tmpdir() / "m.json")
        store.remember("language", "Bangla", "preference")
        self.assertEqual(store.recall("language"), "Bangla")
        self.assertEqual(len(store.search("bangla")), 1)
        self.assertTrue(store.forget("language"))
        self.assertIsNone(store.recall("language"))
        store.remember("a", "1"); store.clear()
        self.assertEqual(store.list(), [])

    def test_persistence_across_instances(self):
        path = tmpdir() / "m.json"
        MemoryStore(path).remember("k", "v")
        self.assertEqual(MemoryStore(path).recall("k"), "v")

    def test_invalid_category_rejected(self):
        store = MemoryStore(tmpdir() / "m.json")
        with self.assertRaises(ValueError):
            store.remember("k", "v", "secret-diary")

    def test_prompt_summary_bounded(self):
        store = MemoryStore(tmpdir() / "m.json")
        for i in range(20):
            store.remember(f"k{i}", "x" * 100)
        self.assertLessEqual(len(store.prompt_summary(max_chars=300)), 320)

    def test_lessons_and_strategy_reuse(self):
        log = LessonLog(tmpdir() / "l.json")
        log.record(task_type="coding", strategy="plan;code;test", outcome="completed", lesson="worked")
        log.record(task_type="coding", strategy="wing it", outcome="partial", lesson="no")
        strategies = log.successful_strategies("coding")
        self.assertEqual(len(strategies), 1)
        self.assertEqual(strategies[0]["strategy"], "plan;code;test")

    def test_evaluate_execution(self):
        class S: status = "completed"
        class R: steps = [S(), S()]; status = "completed"; blockers = []
        self.assertIn("worked", evaluate_execution(R()))

if __name__ == "__main__":
    unittest.main()
