"""镜面脏度台账与排序。

批次策略**定死为脏度优先（grade_first）**：脏度高的镜子永远排在前面，区域连片
只能在**同一脏度档内**作平局裁决，绝不允许跨档把脏镜子挤到后面。这样"先洗最
脏的"这条业务承诺不会被任何顺路优化破坏。

平局裁决同样定死：同档先按分区聚拢（同分区连片，减少空驶），分区相同再按与
站内方向的角度就近连片，最后按镜名稳定兜底。整个排序是纯函数，同一份输入永远
得到同一份顺序。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ...config import WashLimits
from ...errors import InterlockError


@dataclass(frozen=True)
class DirtEntry:
    """一面镜子的脏度与位置。"""

    name: str
    grade: int
    group: str
    x_m: float
    y_m: float

    @property
    def angle_deg(self) -> float:
        """相对站心（原点）的方位角，用于同档内就近连片。"""

        return math.degrees(math.atan2(self.x_m, self.y_m))

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "grade": self.grade,
            "group": self.group,
            "x_m": round(self.x_m, 2),
            "y_m": round(self.y_m, 2),
        }


class DirtLedger:
    """脏度登记表：更新脏度、按 grade_first 给出全局清洗顺序。"""

    def __init__(self, limits: WashLimits) -> None:
        self._limits = limits
        self._entries: dict[str, DirtEntry] = {}

    @property
    def limits(self) -> WashLimits:
        return self._limits

    def _check_grade(self, grade: int) -> int:
        value = int(grade)
        if not 1 <= value <= self._limits.dirt_max_grade:
            raise InterlockError(
                f"脏度档必须落在 1..{self._limits.dirt_max_grade}，收到 {value}", component="wash"
            )
        return value

    def set(self, name: str, grade: int, group: str = "a", x_m: float = 0.0, y_m: float = 0.0) -> DirtEntry:
        """登记或更新一面镜子的脏度。"""

        entry = DirtEntry(str(name), self._check_grade(grade), str(group), float(x_m), float(y_m))
        self._entries[entry.name] = entry
        return entry

    def get(self, name: str) -> DirtEntry:
        try:
            return self._entries[name]
        except KeyError as error:
            raise InterlockError(f"未登记脏度的镜子: {name}", component="wash") from error

    def remove(self, *names: str) -> None:
        for name in names:
            self._entries.pop(name, None)

    def clear(self) -> None:
        self._entries.clear()

    def order(self, names: list[str] | None = None) -> list[DirtEntry]:
        """按 grade_first 给出清洗顺序。

        排序键（从前到后，优先级严格递减）：
        1. 脏度档降序（-grade）——跨档不可被区域覆盖；
        2. 分区名升序——同档同区连片；
        3. 方位角升序——同区内就近连片；
        4. 镜名升序——稳定兜底，保证纯函数确定性。
        """

        selected = list(self._entries.values()) if names is None else [self.get(name) for name in names]
        return sorted(selected, key=lambda item: (-item.grade, item.group, item.angle_deg, item.name))

    def describe(self) -> list[dict]:
        return [entry.as_dict() for entry in self.ordered_view()]

    def ordered_view(self) -> list[DirtEntry]:
        return self.order()
