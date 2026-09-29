"""接收竞争测试：复检人约束、双任务/双线程同时接收同一布卷只允许一条成功。"""
from __future__ import annotations

import threading
from datetime import timedelta

from millinspect.domain import Conflict, DomainError
from tests.helpers import PNG_1, ServiceCase


class ClaimRuleTest(ServiceCase):
    def test_reinspector_cannot_equal_initial_inspector(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0, inspector="王检")
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.claim_task(task, "王检", now=self.now)  # 与初检人相同
        # 换复检人成功
        self.svc.claim_task(task, "李复", now=self.now)
        self.assertEqual(self.svc.queue()[0]["assignee"], "李复")

    def test_cannot_claim_twice(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.claim_task(task, "孙复", now=self.now)

    def _bolt_with_two_tasks(self) -> tuple[int, int]:
        """一卷布两段各派发一条 queued 任务。"""
        self.make_bolt("B-001", length=120.0)
        vid = self.svc.start_inspection("B-001", "王检", now=self.now)
        s1 = self.svc.add_segment(vid, 1, 0, 40, "王检", now=self.now)
        s2 = self.svc.add_segment(vid, 2, 40, 80, "王检", now=self.now)
        self.svc.add_defect(s1, "stain", 10, "污渍 1", PNG_1, now=self.now)
        self.svc.add_defect(s2, "color_diff", 50, "色差 1", PNG_1, now=self.now)
        t1 = self.svc.enqueue_reinspection("B-001", s1, "stain", now=self.now)
        t2 = self.svc.enqueue_reinspection("B-001", s2, "color_diff", now=self.now)
        return t1, t2

    def test_two_queued_tasks_same_bolt_only_one_can_be_claimed(self):
        t1, t2 = self._bolt_with_two_tasks()
        self.svc.claim_task(t1, "李复", now=self.now)
        # 第二条复检线再接收同卷任务 -> 拒绝
        with self.assertRaises(Conflict):
            self.svc.claim_task(t2, "孙复", now=self.now)
        claimed = [t for t in self.svc.queue() if t["bolt_code"] == "B-001" and t["status"] == "claimed"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], t1)

    def test_concurrent_claims_only_one_wins(self):
        """两条复检线程同时接收同一布卷的两条任务，恰好一条成功。"""
        t1, t2 = self._bolt_with_two_tasks()
        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, task_id: int) -> None:
            barrier.wait()  # 尽量放大竞争：两线程同时进入
            try:
                self.svc.claim_task(task_id, name, now=self.now)
                results[name] = "ok"
            except Conflict:
                results[name] = "conflict"
            except Exception as exc:  # noqa: BLE001
                results[name] = f"error:{exc!r}"

        th1 = threading.Thread(target=worker, args=("李复", t1))
        th2 = threading.Thread(target=worker, args=("孙复", t2))
        th1.start(); th2.start()
        th1.join(5); th2.join(5)

        self.assertEqual(sorted(results.values()), ["conflict", "ok"], results)
        claimed = self.svc.conn.execute(
            "SELECT COUNT(*) c FROM reinspection_tasks WHERE bolt_code='B-001' AND status='claimed'"
        ).fetchone()["c"]
        queued = self.svc.conn.execute(
            "SELECT COUNT(*) c FROM reinspection_tasks WHERE bolt_code='B-001' AND status='queued'"
        ).fetchone()["c"]
        self.assertEqual(claimed, 1)
        self.assertEqual(queued, 1)

    def test_concurrent_claims_on_same_task_only_one_wins(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0, inspector="王检")
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        results = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            barrier.wait()
            try:
                self.svc.claim_task(task, name, now=self.now)
                results.append(("ok", name))
            except Conflict:
                results.append(("conflict", name))

        th1 = threading.Thread(target=worker, args=("李复",))
        th2 = threading.Thread(target=worker, args=("孙复",))
        th1.start(); th2.start(); th1.join(5); th2.join(5)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for status, _ in results if status == "ok"), 1)
        assignee = self.svc.conn.execute(
            "SELECT assignee FROM reinspection_tasks WHERE id=?", (task,)
        ).fetchone()["assignee"]
        self.assertIn(assignee, {"李复", "孙复"})

    def test_after_complete_second_task_can_be_claimed(self):
        t1, t2 = self._bolt_with_two_tasks()
        self.svc.claim_task(t1, "李复", now=self.now)
        self.svc.complete_task(t1, "李复", "cleared", "无碍", now=self.now)
        # 第一条完成后，同一布卷的下一条任务可以被另一条复检线接收
        self.svc.claim_task(t2, "孙复", now=self.now)
        self.assertEqual(self.svc.conn.execute(
            "SELECT status FROM reinspection_tasks WHERE id=?", (t2,)
        ).fetchone()["status"], "claimed")

    def test_only_assignee_can_complete(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        with self.assertRaises(Exception):
            self.svc.complete_task(task, "孙复", "cleared", now=self.now)

    def test_timeout_blocks_complete_and_release(self):
        _, sid, _ = self.inspect_with_defect("B-001", "other", 10.0, inspector="赵检")
        task = self.svc.enqueue_reinspection(
            "B-001", sid, "other",
            timeout_seconds=1, now=self.now,
        )
        self.svc.claim_task(task, "李复", now=self.now)
        later = self.now + timedelta(seconds=2)
        n = self.svc._sweep_timeouts(now=later)
        self.assertEqual(n, 1)
        with self.assertRaises(Conflict):
            self.svc.complete_task(task, "李复", "cleared", now=later)
        decision = self.svc.evaluate_release("B-001")
        self.assertFalse(decision.allowed)
        self.assertTrue(any("超时" in r for r in decision.reasons))

    def test_timeout_redispatch_allows_release(self):
        _, sid, _ = self.inspect_with_defect("B-001", "other", 10.0, inspector="赵检")
        task = self.svc.enqueue_reinspection("B-001", sid, "other", timeout_seconds=1, now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        later = self.now + timedelta(seconds=2)
        self.svc._sweep_timeouts(now=later)
        # 重新派发并完成
        task2 = self.svc.enqueue_reinspection("B-001", sid, "other", now=later)
        self.svc.claim_task(task2, "孙复", now=later)
        self.svc.complete_task(task2, "孙复", "cleared", "复核无碍", now=later)
        decision = self.svc.evaluate_release("B-001")
        self.assertTrue(decision.allowed, decision.reasons)
        rid = self.svc.release("B-001", "钱班", now=later)
        self.assertGreater(rid, 0)
