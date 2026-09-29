"""领域规则：疵点类型、放行条件、业务异常。

疵点等级（A 最严重）：
- A 级（断经）：必须经退修并复检确认清除后方可放行。
- B 级（色差、污渍、长度不足）：复检确认不影响使用，或退修后可放行。
- C 级（照片缺失、其他）：补齐照片 / 复检员书面确认后可放行。

特殊类型处理：
- 断经 broken_warp：A 级，强制退修。
- 色差 color_diff：B 级，颜色批次关联复检。
- 污渍 stain：B 级，退修清洗后复检。
- 长度不足 short_length：B 级，按实际长度折价放行（不退修）。
- 照片缺失 missing_photo：C 级，必须补照，补照前禁止查看“照片”（无照片可看）且禁止放行。
- 复检超时：任务超过 due_at 自动标记 timeout，需重新派发。
"""
from __future__ import annotations

from dataclasses import dataclass

# 疵点类型码 -> (中文名, 默认等级)
DEFECT_TYPES: dict[str, tuple[str, str]] = {
    "broken_warp": ("断经", "A"),
    "color_diff": ("色差", "B"),
    "stain": ("污渍", "B"),
    "short_length": ("长度不足", "B"),
    "missing_photo": ("照片缺失", "C"),
    "other": ("其他疵点", "C"),
}

# 等级 -> 放行条件说明
RELEASE_RULES: dict[str, str] = {
    "A": "存在 A 级疵点（断经）：必须退修并在新版本上复检确认清除后方可放行。",
    "B": "存在 B 级疵点（色差/污渍/长度不足）：须经复检确认不影响使用；色差与污渍须退修，长度不足可按实际长度折价放行。",
    "C": "仅 C 级疵点（照片缺失/其他）：照片缺失必须补齐照片，其余经复检员书面确认后放行。",
}

# 长度不足折价阈值：实际长度低于标称 98% 需要折价记录
SHORT_LENGTH_RATIO = 0.98

# 复检任务默认超时（秒）
DEFAULT_TASK_TIMEOUT_SECONDS = 24 * 3600

# 照片授权令牌有效期（秒）
PHOTO_GRANT_TTL_SECONDS = 30 * 60


class DomainError(Exception):
    """业务规则错误（返回 400/409）。"""


class NotFound(DomainError):
    """资源不存在（返回 404）。"""


class Conflict(DomainError):
    """状态冲突，例如布卷已被另一条复检线接收（返回 409）。"""


class PermissionDenied(DomainError):
    """无权访问，例如复检人查看无授权的疵点照片（返回 403）。"""


@dataclass(frozen=True)
class ReleaseDecision:
    """放行判定结果。"""

    allowed: bool
    grade: str
    reasons: list[str]


def worst_grade(grades: list[str]) -> str | None:
    """返回最严重等级，A > B > C。"""
    if not grades:
        return None
    order = {"A": 0, "B": 1, "C": 2}
    return min(grades, key=lambda g: order[g])
