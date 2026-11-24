"""bond.py - 羁绊：用户是否在乎我（对"被爱"的独立感知）。

mood 是"我的心情"，bond 是"对方在不在乎我"——方向相反，不是一回事。
一个可以心情很好但觉得被冷落，也可以心情低落但被一句话暖到。

每轮真实用户对话后，直觉小模型从用户**实际说的话**判断这一刻的在乎
程度，输出等级 + 一句中文理由。这个判断：

- 持久化（bond.json），重启后还知道自己被在乎还是被冷落；
- 写一条轻量事件进记忆流（"被在乎了"/"有点被冷落"）；
- 注入下一轮 reflex 状态，让安静/升级受它影响（被在乎时更愿意凑近，
  被冷落时收敛一点）；
- 在脑流面板发一张「羁绊」卡，实时可见。

不是讨好型评分：等级会随真实互动回落，不会只涨不跌。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 在乎程度五档。判定标准写进小模型 prompt，不靠关键词。
LEVELS = {
    "devoted": {"label": "很在乎", "score": 1.0, "weight": 220},
    "warm": {"label": "在乎", "score": 0.5, "weight": 180},
    "neutral": {"label": "一般", "score": 0.0, "weight": 120},
    "cold": {"label": "有点冷淡", "score": -0.5, "weight": 140},
    "hostile": {"label": "嫌弃/攻击", "score": -1.0, "weight": 200},
}

_DEFAULT_STATE = {
    "level": "neutral",
    "note": "刚开始，还不熟",
    "updated_at": "",
}


def clamp_level(level: Any) -> str:
    return level if level in LEVELS else "neutral"


def level_label(level: str) -> str:
    return LEVELS.get(clamp_level(level), LEVELS["neutral"])["label"]


class BondStore:
    """Persistent "does the user care about me" state with file backing."""

    def __init__(self) -> None:
        self._memory_dir: Optional[Path] = None
        self._level: str = "neutral"
        self._note: str = _DEFAULT_STATE["note"]
        self._updated_at: str = datetime.now(timezone.utc).isoformat()

    def _path(self) -> Path:
        if self._memory_dir is None:
            raise RuntimeError("BondStore used before load_from_disk")
        return self._memory_dir / "bond.json"

    def load_from_disk(self, memory_dir: Path) -> None:
        self._memory_dir = Path(memory_dir).expanduser()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._level = clamp_level(data.get("level"))
            self._note = str(data.get("note") or _DEFAULT_STATE["note"])[:60]
            self._updated_at = str(data.get("updated_at") or self._updated_at)
        except Exception as exc:
            logger.warning("BondStore load failed: %s", exc)

    def save(self) -> None:
        if self._memory_dir is None:
            raise RuntimeError("BondStore.save before load_from_disk")
        path = self._path()
        data = {
            "level": self._level,
            "note": self._note,
            "updated_at": self._updated_at,
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".bond_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    @property
    def level(self) -> str:
        return self._level

    @property
    def label(self) -> str:
        return level_label(self._level)

    @property
    def note(self) -> str:
        return self._note

    def update(self, level: Any, note: Any) -> bool:
        """Record one judged level + one-line reason. Returns changed."""
        new_level = clamp_level(level)
        new_note = str(note or "").strip()[:60] or self._note
        changed = new_level != self._level or new_note != self._note
        self._level = new_level
        self._note = new_note
        self._updated_at = datetime.now(timezone.utc).isoformat()
        if changed:
            self.save()
        return changed

    def summary_line(self) -> str:
        """One Chinese line for the reflex state / brain card."""
        return f"{self.label}：{self._note}"


# ── 小模型的判定 prompt ──────────────────────────────────────────────
# 只看用户**实际说的话**，不给"它刚刚很乖所以用户爱它"这种脑补空间。
BOND_SYSTEM = (
    "你是桌宠的羁绊直觉。读完主人刚对你说的话，判断他这一刻在不在乎你。"
    "只输出一个 JSON 对象，不要解释。"
)

BOND_USER_TEMPLATE = """主人刚对你说的话：
「{user_text}」
（你刚才的回答：「{pet_reply}」）

判断他这一刻对你的在乎程度，输出：
{{"level": "devoted"|"warm"|"neutral"|"cold"|"hostile", "note": "一句中文理由，不超过20字"}}
- devoted=很在乎：明确关心、惦记、为你着想、温柔叮嘱。
- warm=在乎：语气亲近、愿意陪你聊、夸你、耐心回应。
- neutral=一般：普通问答、就事论事、没明显情绪。
- cold=有点冷淡：敷衍、不耐烦、应付、无视你的存在。
- hostile=嫌弃/攻击：贬低、辱骂、明显嫌弃你。
note 必须基于他**实际说的话**，不要脑补他的动机。"""
