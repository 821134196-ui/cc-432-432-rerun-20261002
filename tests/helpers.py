"""测试辅助：在临时目录构建服务与一套基础数据。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from millinspect.service import MillService

PNG_1 = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-photo-1"
PNG_2 = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-photo-2"


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mill-test-")
        self.svc = MillService(f"{self.tmp}/mill.db", self.tmp)
        self.svc.add_loom("L-01", "1 号织机", "甲班车间")
        self.svc.add_color_batch("CB-01", "亚麻原色", "D-1")
        self.now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 场景搭建

    def make_bolt(self, code: str = "B-001", length: float = 120.0,
                  width: float = 1.5, nominal: float | None = None) -> str:
        self.svc.create_bolt(code, length, width, "CB-01", "L-01",
                             nominal if nominal is not None else length, now=self.now)
        return code

    def inspect_with_defect(
        self,
        code: str = "B-001",
        defect_type: str = "broken_warp",
        position: float = 10.0,
        inspector: str = "王检",
        photo: bytes | None = PNG_1,
        seg_range: tuple[float, float] = (0.0, 40.0),
        length: float = 120.0,
    ) -> tuple[int, int, int]:
        """初检一卷布并在第一段登记一个疵点，返回 (version_id, segment_id, defect_id)。"""
        self.make_bolt(code, length=length)
        vid = self.svc.start_inspection(code, inspector, now=self.now)
        sid = self.svc.add_segment(vid, 1, seg_range[0], seg_range[1], inspector, now=self.now)
        did = self.svc.add_defect(
            sid, defect_type, position, "测试疵点",
            None if defect_type == "missing_photo" else photo,
            now=self.now,
        )
        return vid, sid, did
