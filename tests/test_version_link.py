"""版本关联测试：退修生成新版本、复检引用原检验段、已放行不可回退。"""
from __future__ import annotations

from millinspect.domain import Conflict, DomainError
from tests.helpers import PNG_1, ServiceCase


class VersionLinkTest(ServiceCase):
    def test_repair_creates_linked_new_version(self):
        vid, sid, _ = self.inspect_with_defect("B-001", "broken_warp", 10.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "broken_warp", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        self.svc.complete_task(task, "李复", "confirmed", "断经确认", now=self.now)
        order = self.svc.open_repair("B-001", "穿经修复", now=self.now)
        self.svc.start_repair(order, now=self.now)
        v2 = self.svc.finish_repair(order, "周修", now=self.now)

        v1 = self.svc._version_row(vid)
        v2row = self.svc._version_row(v2)
        self.assertEqual(v2row["version_no"], 2)
        self.assertEqual(v2row["kind"], "post_repair")
        self.assertEqual(v2row["supersedes"], vid)
        self.assertEqual(v2row["repair_order_id"], order)
        # 当前版本指针切到新版本
        bolt = self.svc._bolt_row("B-001")
        self.assertEqual(bolt["current_version"], v2)
        # 布卷进入复检中（等待退修后复检）
        self.assertEqual(bolt["status"], "reinspecting")

    def test_reinspection_references_original_segments_only(self):
        vid, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        # 退修后 V2 成为当前版本，旧版本 V1 的段不再可被派发复检
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        self.svc.complete_task(task, "李复", "confirmed", "污渍确认", now=self.now)
        order = self.svc.open_repair("B-001", "清洗", now=self.now)
        self.svc.start_repair(order, now=self.now)
        v2 = self.svc.finish_repair(order, "周修", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)

    def test_no_duplicate_active_task_per_segment(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)

    def test_released_bolt_cannot_revert_to_pending_or_repair(self):
        # 一卷无疵点布走完整流程后放行
        self.make_bolt("B-009", length=120.0)
        vid = self.svc.start_inspection("B-009", "王检", now=self.now)
        sid = self.svc.add_segment(vid, 1, 0, 120, "王检", now=self.now)
        self.svc.release("B-009", "钱班", now=self.now)
        # 已放行不能开始初检
        with self.assertRaises(Conflict):
            self.svc.start_inspection("B-009", "王检", now=self.now)
        # 不能退修
        with self.assertRaises(Conflict):
            self.svc.open_repair("B-009", "再修一次", now=self.now)
        # 不能追加疵点
        with self.assertRaises(Conflict):
            self.svc.add_defect(sid, "stain", 5, "放行后发现", PNG_1, now=self.now)
        # 不能派发复检
        with self.assertRaises(Conflict):
            self.svc.enqueue_reinspection("B-009", sid, "stain", now=self.now)

    def test_cannot_release_without_inspection(self):
        self.make_bolt("B-010", length=120.0)
        with self.assertRaises(Conflict):
            self.svc.release("B-010", "钱班", now=self.now)

    def test_second_repair_chains_v3(self):
        vid, sid, _ = self.inspect_with_defect("B-001", "broken_warp", 10.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "broken_warp", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        self.svc.complete_task(task, "李复", "confirmed", "", now=self.now)
        o1 = self.svc.open_repair("B-001", "第一次修复", now=self.now)
        self.svc.start_repair(o1, now=self.now)
        v2 = self.svc.finish_repair(o1, "周修", now=self.now)
        s2 = self.svc.add_segment(v2, 1, 0, 40, "周修", now=self.now)
        self.svc.add_defect(s2, "broken_warp", 11, "修复不彻底", PNG_1, now=self.now)
        t2 = self.svc.enqueue_reinspection("B-001", s2, "broken_warp", now=self.now)
        self.svc.claim_task(t2, "孙复", now=self.now)
        self.svc.complete_task(t2, "孙复", "confirmed", "", now=self.now)
        o2 = self.svc.open_repair("B-001", "第二次修复", now=self.now)
        self.svc.start_repair(o2, now=self.now)
        v3 = self.svc.finish_repair(o2, "周修", now=self.now)
        self.assertEqual(self.svc._version_row(v3)["version_no"], 3)
        self.assertEqual(self.svc._version_row(v3)["supersedes"], v2)
        self.assertEqual(self.svc._bolt_row("B-001")["current_version"], v3)

    def test_segment_from_other_bolt_rejected(self):
        # B-001 初检并有一个段
        _, sid_b1, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        # B-002 入检并开始初检（状态为初检中，确保能走到段归属校验）
        self.make_bolt("B-002", length=120.0)
        self.svc.start_inspection("B-002", "赵检", now=self.now)
        # 试图把 B-001 的原检验段挂到 B-002 的复检上
        with self.assertRaises(DomainError):
            self.svc.enqueue_reinspection("B-002", sid_b1, "stain", now=self.now)

    def test_old_version_frozen_during_repair(self):
        vid, sid, _ = self.inspect_with_defect("B-001", "stain", 10.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        self.svc.complete_task(task, "李复", "confirmed", "污渍确认", now=self.now)
        order = self.svc.open_repair("B-001", "清洗", now=self.now)
        self.svc.start_repair(order, now=self.now)
        # 退修中：旧版本不能追加疵点、不能对旧段派发复检
        with self.assertRaises(Conflict):
            self.svc.add_defect(sid, "other", 12, "退修中追加", PNG_1, now=self.now)
        with self.assertRaises(Conflict):
            self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        # 完成退修后：旧段仍不可派发（已非当前版本），新版本可正常走流程
        v2 = self.svc.finish_repair(order, "周修", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        ns = self.svc.add_segment(v2, 1, 0, 40, "周修", now=self.now)
        nt = self.svc.enqueue_reinspection("B-001", ns, "stain", now=self.now)
        self.assertGreater(nt, 0)
