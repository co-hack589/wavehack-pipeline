"""ポイントマスタの読み込み・型定義。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Point:
    name: str
    name_en: str
    area: str
    latitude: float
    longitude: float
    beach_direction: str
    confidence: str
    source: str
    note: str

    @property
    def id(self) -> str:
        return self.name_en.lower()


def load_points(path: Path) -> list[Point]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Point(**item) for item in raw]
