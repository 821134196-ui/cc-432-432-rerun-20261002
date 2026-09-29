"""位置区间测试：检验段边界与疵点落点。"""
from __future__ import annotations

from millinspect.domain import DomainError
from tests.helpers import PNG_1, ServiceCase


class SegmentRangeTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_bolt("B-001", length=120.0)
        self.vid = self.svc.start_inspection("B-001", "王检", now=self.now)

    def test_valid_segment(self):
        sid = self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        seg = self.svc.conn.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        self.assertEqual((seg["start_m"], seg["end_m"]), (0.0, 40.0))

    def test_reject_zero_or_negative_length_segment(self):
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 1, 40, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 2, 80, 30, "王检", now=self.now)

    def test_reject_negative_start(self):
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 1, -1, 40, "王检", now=self.now)

    def test_reject_segment_beyond_bolt_length(self):
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 1, 100, 130, "王检", now=self.now)

    def test_touching_segments_are_allowed(self):
        # [0,40) 与 [40,80] 首尾相接，不算重叠
        self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        sid = self.svc.add_segment(self.vid, 2, 40, 80, "王检", now=self.now)
        self.assertGreater(sid, 0)

    def test_reject_overlapping_segments(self):
        self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 2, 30, 70, "王检", now=self.now)

    def test_defect_must_fall_inside_segment(self):
        sid = self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_defect(sid, "stain", 40.5, "越界", PNG_1, now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_defect(sid, "stain", -0.1, "越界", PNG_1, now=self.now)

    def test_defect_on_segment_boundaries_allowed(self):
        sid = self.svc.add_segment(self.vid, 1, 10, 40, "王检", now=self.now)
        d1 = self.svc.add_defect(sid, "stain", 10.0, "起点", PNG_1, now=self.now)
        d2 = self.svc.add_defect(sid, "stain", 40.0, "终点", PNG_1, now=self.now)
        self.assertNotEqual(d1, d2)

    def test_duplicate_seq_rejected(self):
        self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_segment(self.vid, 1, 40, 80, "王检", now=self.now)

    def test_defect_requires_photo(self):
        sid = self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_defect(sid, "stain", 12, "无照片", None, now=self.now)

    def test_missing_photo_type_cannot_carry_photo(self):
        sid = self.svc.add_segment(self.vid, 1, 0, 40, "王检", now=self.now)
        with self.assertRaises(DomainError):
            self.svc.add_defect(sid, "missing_photo", 12, "应无照片", PNG_1, now=self.now)
        did = self.svc.add_defect(sid, "missing_photo", 12, "确无照片", None, now=self.now)
        row = self.svc.conn.execute("SELECT photo_path, resolved_at FROM defects WHERE id=?", (did,)).fetchone()
        self.assertIsNone(row["photo_path"])
        self.svc.attach_photo(did, PNG_1, now=self.now)
        row = self.svc.conn.execute("SELECT photo_path, resolved_at FROM defects WHERE id=?", (did,)).fetchone()
        self.assertIsNotNone(row["photo_path"])
        self.assertIsNotNone(row["resolved_at"])
