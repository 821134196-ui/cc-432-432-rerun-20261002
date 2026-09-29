"""演示数据：织机、颜色批次、布卷编号与初检/复检/放行样例。

运行 ``python -m millinspect.app --seed`` 时在空库上播种。
样例覆盖：待检、初检中、复检队列（含超时）、退修新版本、折价放行、
断经退修后按 B 级修复品放行、照片缺失待补照。
"""
from __future__ import annotations

from datetime import timedelta

from . import photos as photos_mod
from .service import MillService, utcnow

PHOTO_COLORS = {
    "broken_warp": (145, 82, 54),    # 锈红
    "color_diff": (107, 112, 121),   # 板岩灰
    "stain": (122, 84, 58),
    "short_length": (168, 142, 104),
    "other": (140, 124, 96),
}


def seed(svc: MillService) -> None:
    if svc.conn.execute("SELECT COUNT(*) FROM bolts").fetchone()[0]:
        return False

    # 织机
    for code, name, ws in [
        ("L-08", "8 号喷气织机", "甲班织造车间"),
        ("L-12", "12 号喷气织机", "甲班织造车间"),
        ("L-15", "15 号剑杆织机", "乙班织造车间"),
    ]:
        svc.add_loom(code, name, ws)

    # 颜色批次
    for code, color, lot in [
        ("CB-2401", "亚麻原色", "D-2401-3"),
        ("CB-2402", "板岩灰", "D-2402-1"),
        ("CB-2403", "锈红", "D-2403-2"),
    ]:
        svc.add_color_batch(code, color, lot)

    now = utcnow()
    photo = lambda key: photos_mod.placeholder_png(PHOTO_COLORS[key])

    # 1) 纯待检布卷
    svc.create_bolt("B-240901", 120.0, 1.50, "CB-2401", "L-08", 120.0, now=now - timedelta(hours=2))

    # 2) 初检 + 复检队列样例：断经任务待接收、污渍任务已接收
    svc.create_bolt("B-240902", 120.0, 1.50, "CB-2403", "L-12", 120.0, now=now - timedelta(hours=6))
    v = svc.start_inspection("B-240902", "王检", now=now - timedelta(hours=5))
    s1 = svc.add_segment(v, 1, 0, 40, "王检", now=now - timedelta(hours=5))
    s2 = svc.add_segment(v, 2, 40, 80, "王检", now=now - timedelta(hours=5))
    s3 = svc.add_segment(v, 3, 80, 120, "王检", now=now - timedelta(hours=4))
    svc.add_defect(s1, "broken_warp", 35.0, "经纱断头 2 根，已停车标记", photo("broken_warp"), now=now - timedelta(hours=5))
    svc.add_defect(s2, "stain", 72.5, "边部油污约 8cm", photo("stain"), now=now - timedelta(hours=4, minutes=40))
    t_warp = svc.enqueue_reinspection("B-240902", s1, "broken_warp", now=now - timedelta(hours=4))
    t_stain = svc.enqueue_reinspection("B-240902", s2, "stain", now=now - timedelta(hours=3, minutes=30))
    svc.claim_task(t_stain, "李复", now=now - timedelta(hours=3))

    # 3) 退修进度样例：色差确认 -> 退修中 -> 已生成 V2，等待退修后复检
    svc.create_bolt("B-240903", 118.0, 1.48, "CB-2402", "L-15", 120.0, now=now - timedelta(days=1))
    v = svc.start_inspection("B-240903", "赵检", now=now - timedelta(hours=22))
    seg = svc.add_segment(v, 1, 0, 60, "赵检", now=now - timedelta(hours=22))
    svc.add_segment(v, 2, 60, 118, "赵检", now=now - timedelta(hours=21))
    svc.add_defect(seg, "color_diff", 48.0, "与色卡对比偏浅，集中在左幅", photo("color_diff"), now=now - timedelta(hours=22))
    t = svc.enqueue_reinspection("B-240903", seg, "color_diff", now=now - timedelta(hours=20))
    svc.claim_task(t, "李复", now=now - timedelta(hours=19))
    svc.complete_task(t, "李复", "confirmed", "色差属实，建议复修染色", now=now - timedelta(hours=18))
    order = svc.open_repair("B-240903", "左幅色差复修染色", now=now - timedelta(hours=17))
    svc.start_repair(order, now=now - timedelta(hours=16))
    v2 = svc.finish_repair(order, "赵检", now=now - timedelta(hours=2))
    new_seg = svc.add_segment(v2, 1, 0, 60, "赵检", now=now - timedelta(hours=1, minutes=30))
    svc.add_segment(v2, 2, 60, 118, "赵检", now=now - timedelta(hours=1))
    svc.enqueue_reinspection("B-240903", new_seg, "color_diff", now=now - timedelta(hours=1))

    # 4) 长度不足折价放行样例（B 级）
    svc.create_bolt("B-240904", 116.0, 1.50, "CB-2401", "L-08", 120.0, now=now - timedelta(days=2))
    v = svc.start_inspection("B-240904", "王检", now=now - timedelta(hours=50))
    seg = svc.add_segment(v, 1, 0, 60, "王检", now=now - timedelta(hours=49))
    svc.add_segment(v, 2, 60, 116, "王检", now=now - timedelta(hours=48))
    svc.add_defect(seg, "short_length", 0.5, "实测 116m，标称 120m", photo("short_length"), now=now - timedelta(hours=49))
    t = svc.enqueue_reinspection("B-240904", seg, "short_length", now=now - timedelta(hours=47))
    svc.claim_task(t, "孙复", now=now - timedelta(hours=46))
    svc.complete_task(t, "孙复", "cleared", "长度不足不影响使用，同意折价", now=now - timedelta(hours=45))
    svc.release("B-240904", "钱班", "复检确认，折价接收", now=now - timedelta(hours=44))

    # 5) 照片缺失样例：无法放行，等待补照
    svc.create_bolt("B-240905", 122.0, 1.50, "CB-2403", "L-12", 120.0, now=now - timedelta(hours=8))
    v = svc.start_inspection("B-240905", "赵检", now=now - timedelta(hours=7))
    seg = svc.add_segment(v, 1, 0, 50, "赵检", now=now - timedelta(hours=7))
    svc.add_segment(v, 2, 50, 122, "赵检", now=now - timedelta(hours=6, minutes=30))
    svc.add_defect(seg, "missing_photo", 23.0, "现场相机故障，未拍到疵点照片", None, now=now - timedelta(hours=7))
    svc.enqueue_reinspection("B-240905", seg, "missing_photo", now=now - timedelta(hours=6))

    # 6) 断经退修后 B 级修复品放行样例
    svc.create_bolt("B-240906", 120.0, 1.48, "CB-2402", "L-15", 120.0, now=now - timedelta(days=3))
    v = svc.start_inspection("B-240906", "王检", now=now - timedelta(hours=70))
    seg = svc.add_segment(v, 1, 0, 45, "王检", now=now - timedelta(hours=69))
    svc.add_segment(v, 2, 45, 90, "王检", now=now - timedelta(hours=68))
    svc.add_segment(v, 3, 90, 120, "王检", now=now - timedelta(hours=67))
    svc.add_defect(seg, "broken_warp", 12.0, "断经 1 根", photo("broken_warp"), now=now - timedelta(hours=69))
    t = svc.enqueue_reinspection("B-240906", seg, "broken_warp", now=now - timedelta(hours=66))
    svc.claim_task(t, "李复", now=now - timedelta(hours=65))
    svc.complete_task(t, "李复", "confirmed", "断经确认，必须退修", now=now - timedelta(hours=64))
    order = svc.open_repair("B-240906", "穿经修复并复检", now=now - timedelta(hours=63))
    svc.start_repair(order, now=now - timedelta(hours=62))
    v2 = svc.finish_repair(order, "周修", now=now - timedelta(hours=30))
    rseg = svc.add_segment(v2, 1, 0, 45, "周修", now=now - timedelta(hours=29))
    svc.add_segment(v2, 2, 45, 90, "周修", now=now - timedelta(hours=28, minutes=30))
    svc.add_segment(v2, 3, 90, 120, "周修", now=now - timedelta(hours=28))
    t2 = svc.enqueue_reinspection("B-240906", rseg, "broken_warp", now=now - timedelta(hours=27))
    svc.claim_task(t2, "孙复", now=now - timedelta(hours=26))
    svc.complete_task(t2, "孙复", "cleared", "退修后断经已清除，张力正常", now=now - timedelta(hours=25))
    svc.release("B-240906", "钱班", "退修复检合格放行", now=now - timedelta(hours=24))

    # 7) 复检超时样例
    svc.create_bolt("B-240907", 110.0, 1.50, "CB-2401", "L-08", 110.0, now=now - timedelta(days=1, hours=2))
    v = svc.start_inspection("B-240907", "赵检", now=now - timedelta(hours=26))
    seg = svc.add_segment(v, 1, 0, 55, "赵检", now=now - timedelta(hours=25))
    svc.add_segment(v, 2, 55, 110, "赵检", now=now - timedelta(hours=24))
    svc.add_defect(seg, "other", 30.0, "布面轻微起球，原因待查", photo("other"), now=now - timedelta(hours=25))
    svc.enqueue_reinspection("B-240907", seg, "other", timeout_seconds=1, now=now - timedelta(hours=2))

    svc._sweep_timeouts(now=now)
    svc.conn.commit()
    return True
