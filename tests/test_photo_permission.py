"""照片权限测试：仅已接收任务的复检人可申请令牌；任务完成/超时后权限收回。"""
from __future__ import annotations

from datetime import timedelta

from millinspect.domain import Conflict, PermissionDenied
from tests.helpers import PNG_1, ServiceCase


class PhotoPermissionTest(ServiceCase):
    def _claimed(self, inspector="王检", reinspector="李复", dtype="stain"):
        _, sid, did = self.inspect_with_defect("B-001", dtype, 12.0, inspector=inspector)
        task = self.svc.enqueue_reinspection("B-001", sid, dtype, now=self.now)
        self.svc.claim_task(task, reinspector, now=self.now)
        return sid, did, task

    def test_grant_requires_claimed_task(self):
        _, did, task = self._claimed()
        # 未接收任务的人不能申请
        with self.assertRaises(PermissionDenied):
            self.svc.grant_photo(did, task, "孙复", now=self.now)

    def test_grant_and_read_photo(self):
        _, did, task = self._claimed()
        token = self.svc.grant_photo(did, task, "李复", now=self.now)
        data, mime = self.svc.read_photo(token, now=self.now)
        self.assertEqual(data, PNG_1)
        self.assertEqual(mime, "image/png")

    def test_garbage_token_denied(self):
        with self.assertRaises(PermissionDenied):
            self.svc.read_photo("not-a-real-token", now=self.now)

    def test_permission_revoked_after_task_completed(self):
        _, did, task = self._claimed()
        token = self.svc.grant_photo(did, task, "李复", now=self.now)
        self.svc.read_photo(token, now=self.now)  # 此时可读
        self.svc.complete_task(task, "李复", "cleared", "无碍", now=self.now)
        # 任务完成，令牌虽在但权限收回
        with self.assertRaises(PermissionDenied):
            self.svc.read_photo(token, now=self.now)

    def test_permission_revoked_after_timeout(self):
        _, did, task = self._claimed()
        token = self.svc.grant_photo(did, task, "李复", now=self.now)
        later = self.now + timedelta(hours=25)
        self.svc._sweep_timeouts(now=later)
        with self.assertRaises(PermissionDenied):
            self.svc.read_photo(token, now=later)

    def test_token_expires(self):
        from millinspect.domain import PHOTO_GRANT_TTL_SECONDS
        _, did, task = self._claimed()
        token = self.svc.grant_photo(did, task, "李复", now=self.now)
        later = self.now + timedelta(seconds=PHOTO_GRANT_TTL_SECONDS + 1)
        with self.assertRaises(PermissionDenied):
            self.svc.read_photo(token, now=later)

    def test_grant_only_for_defects_of_own_task_version(self):
        # 两卷布，各自有任务；不能拿甲卷的令牌申请乙卷疵点
        _, sid1, did1 = self._claimed(inspector="王检", reinspector="李复")
        task1 = self.svc.conn.execute(
            "SELECT id FROM reinspection_tasks WHERE bolt_code='B-001' AND status='claimed'"
        ).fetchone()["id"]
        self.make_bolt("B-002", length=120.0)
        vid2 = self.svc.start_inspection("B-002", "赵检", now=self.now)
        s2 = self.svc.add_segment(vid2, 1, 0, 40, "赵检", now=self.now)
        did2 = self.svc.add_defect(s2, "stain", 5, "乙卷污渍", PNG_1, now=self.now)
        with self.assertRaises(PermissionDenied):
            self.svc.grant_photo(did2, task1, "李复", now=self.now)

    def test_missing_photo_has_nothing_to_grant(self):
        _, sid, did = self.inspect_with_defect("B-001", "missing_photo", 12.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "missing_photo", now=self.now)
        self.svc.claim_task(task, "李复", now=self.now)
        with self.assertRaises(PermissionDenied):
            self.svc.grant_photo(did, task, "李复", now=self.now)

    def test_cannot_grant_when_task_not_claimed(self):
        _, sid, did = self.inspect_with_defect("B-001", "stain", 12.0)
        task = self.svc.enqueue_reinspection("B-001", sid, "stain", now=self.now)
        with self.assertRaises(Conflict):
            self.svc.grant_photo(did, task, "李复", now=self.now)
