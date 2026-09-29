"""放行条件测试：A/B/C 分级放行、断经退修、色差污渍、长度不足折价、照片缺失。"""
from __future__ import annotations

from millinspect.domain import Conflict
from tests.helpers import PNG_1, PNG_2, ServiceCase


def reinspect_cleared(svc, bolt, sid, dtype, inspector="李复", now=None):
    t = svc.enqueue_reinspection(bolt, sid, dtype, now=now)
    svc.claim_task(t, inspector, now=now)
    svc.complete_task(t, inspector, "cleared", "复检无碍", now=now)
    return t


def reinspect_confirmed(svc, bolt, sid, dtype, inspector="李复", now=None):
    t = svc.enqueue_reinspection(bolt, sid, dtype, now=now)
    svc.claim_task(t, inspector, now=now)
    svc.complete_task(t, inspector, "confirmed", "问题确认", now=now)
    return t


def repair_cycle(svc, bolt, reason="修复", now=None, repair_inspector="周修",
                 new_segments=((0, 40), (40, 120))):
    o = svc.open_repair(bolt, reason, now=now)
    svc.start_repair(o, now=now)
    v2 = svc.finish_repair(o, repair_inspector, now=now)
    sids = []
    for i, (a, b) in enumerate(new_segments, start=1):
        sids.append(svc.add_segment(v2, i, a, b, repair_inspector, now=now))
    return o, v2, sids


class CleanBoltReleaseTest(ServiceCase):
    def test_clean_bolt_releases_as_grade_a(self):
        self.make_bolt("B-001", length=120.0)
        vid = self.svc.start_inspection("B-001", "王检", now=self.now)
        self.svc.add_segment(vid, 1, 0, 120, "王检", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.grade, "A")
        rid = self.svc.release("B-001", "钱班", now=self.now)
        self.assertEqual(self.svc._bolt_row("B-001")["status"], "released")
        self.assertEqual(self.svc.conn.execute(
            "SELECT grade FROM releases WHERE id=?", (rid,)).fetchone()["grade"], "A")


class GradeAReleaseTest(ServiceCase):
    def test_broken_warp_must_repair_before_release(self):
        _, sid, _ = self.inspect_with_defect("B-001", "broken_warp", 10.0, inspector="王检")
        # 即使复检确认无碍，A 级断经在未退修版本上仍不能放行
        reinspect_cleared(self.svc, "B-001", sid, "broken_warp", inspector="李复", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("退修" in r for r in d.reasons), d.reasons)

    def test_broken_warp_repairs_but_reinspect_confirmed_still_blocked(self):
        _, sid, _ = self.inspect_with_defect("B-001", "broken_warp", 10.0)
        reinspect_confirmed(self.svc, "B-001", sid, "broken_warp", now=self.now)
        _, v2, sids = repair_cycle(self.svc, "B-001", now=self.now)
        # 新版本上同位置仍有断经
        self.svc.add_defect(sids[0], "broken_warp", 10.5, "未修好", PNG_2, now=self.now)
        t = self.svc.enqueue_reinspection("B-001", sids[0], "broken_warp", now=self.now)
        self.svc.claim_task(t, "孙复", now=self.now)
        self.svc.complete_task(t, "孙复", "confirmed", "断经仍在", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("复检确认清除" in r for r in d.reasons))

    def test_broken_warp_repaired_and_cleared_releases(self):
        _, sid, _ = self.inspect_with_defect("B-001", "broken_warp", 10.0)
        reinspect_confirmed(self.svc, "B-001", sid, "broken_warp", now=self.now)
        _, v2, sids = repair_cycle(self.svc, "B-001", now=self.now)
        # 新版本无疵点，退修后对原区间复检确认清除
        t = self.svc.enqueue_reinspection("B-001", sids[0], "broken_warp", now=self.now)
        self.svc.claim_task(t, "孙复", now=self.now)
        self.svc.complete_task(t, "孙复", "cleared", "断经已清除", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        # 曾有 A 级疵点的退修品，无现存疵点时定 B 级
        self.assertEqual(d.grade, "B")
        self.svc.release("B-001", "钱班", now=self.now)
        self.assertEqual(self.svc._bolt_row("B-001")["status"], "released")


class GradeBReleaseTest(ServiceCase):
    def test_color_diff_must_repair(self):
        _, sid, _ = self.inspect_with_defect("B-001", "color_diff", 30.0)
        reinspect_cleared(self.svc, "B-001", sid, "color_diff", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("色差" in r and "退修" in r for r in d.reasons), d.reasons)

    def test_stain_must_repair(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 30.0)
        reinspect_cleared(self.svc, "B-001", sid, "stain", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("污渍" in r and "退修" in r for r in d.reasons), d.reasons)

    def test_color_diff_repaired_and_rechecked_releases_as_b(self):
        _, sid, _ = self.inspect_with_defect("B-001", "color_diff", 30.0)
        reinspect_confirmed(self.svc, "B-001", sid, "color_diff", now=self.now)
        _, v2, sids = repair_cycle(self.svc, "B-001", now=self.now)
        t = self.svc.enqueue_reinspection("B-001", sids[0], "color_diff", now=self.now)
        self.svc.claim_task(t, "孙复", now=self.now)
        self.svc.complete_task(t, "孙复", "cleared", "复染无色差", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.grade, "B")

    def test_short_length_releases_with_discount_note(self):
        # 长度不足：复检确认不影响使用即可折价放行，不退修
        self.make_bolt("B-001", length=116.0, nominal=120.0)
        vid = self.svc.start_inspection("B-001", "王检", now=self.now)
        sid = self.svc.add_segment(vid, 1, 0, 60, "王检", now=self.now)
        self.svc.add_segment(vid, 2, 60, 116, "王检", now=self.now)
        self.svc.add_defect(sid, "short_length", 0.5, "实测 116m", PNG_1, now=self.now)
        # 未复检前不能放行
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        reinspect_cleared(self.svc, "B-001", sid, "short_length", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.grade, "B")
        rid = self.svc.release("B-001", "钱班", "同意折价", now=self.now)
        note = self.svc.conn.execute("SELECT note FROM releases WHERE id=?", (rid,)).fetchone()["note"]
        self.assertIn("折价", note)
        self.assertIn("96.7%", note)

    def test_short_length_within_tolerance_no_auto_discount(self):
        # 118/120 = 98.3% ≥ 98%，不自动追加折价文案
        self.make_bolt("B-001", length=118.0, nominal=120.0)
        vid = self.svc.start_inspection("B-001", "王检", now=self.now)
        sid = self.svc.add_segment(vid, 1, 0, 60, "王检", now=self.now)
        self.svc.add_defect(sid, "short_length", 1, "118m", PNG_1, now=self.now)
        reinspect_cleared(self.svc, "B-001", sid, "short_length", now=self.now)
        rid = self.svc.release("B-001", "钱班", "正常接收", now=self.now)
        note = self.svc.conn.execute("SELECT note FROM releases WHERE id=?", (rid,)).fetchone()["note"]
        self.assertEqual(note, "正常接收")


class GradeCReleaseTest(ServiceCase):
    def test_missing_photo_blocks_release_until_resupplied(self):
        _, sid, did = self.inspect_with_defect("B-001", "missing_photo", 20.0)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("照片缺失" in r for r in d.reasons), d.reasons)
        # 补照后解除
        self.svc.attach_photo(did, PNG_2, now=self.now)
        reinspect_cleared(self.svc, "B-001", sid, "missing_photo", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.grade, "C")

    def test_other_c_grade_needs_written_confirmation(self):
        _, sid, _ = self.inspect_with_defect("B-001", "other", 20.0)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("书面确认" in r for r in d.reasons), d.reasons)
        reinspect_cleared(self.svc, "B-001", sid, "other", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.grade, "C")


class ReleaseHardRulesTest(ServiceCase):
    def test_release_writes_record_and_is_idempotent_block(self):
        self.make_bolt("B-001", length=120.0)
        vid = self.svc.start_inspection("B-001", "王检", now=self.now)
        self.svc.add_segment(vid, 1, 0, 120, "王检", now=self.now)
        self.svc.release("B-001", "钱班", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.release("B-001", "钱班", now=self.now)
        self.assertEqual(len(self.svc.releases()), 1)

    def test_open_tasks_block_release(self):
        _, sid, _ = self.inspect_with_defect("B-001", "stain", 20.0)
        self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        d = self.svc.evaluate_release("B-001")
        self.assertFalse(d.allowed)
        self.assertTrue(any("未完成" in r for r in d.reasons))
