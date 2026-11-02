"""SQLite 阶段缓存：MRTR 恢复轮 / 重复调用时零重复劳动。

缓存三类产物：
- article:{url}   抽取结果（Article 序列化 JSON），含已接受的客户端摘要
- media:{url}     图片 data URI（头图 + 正文图），重跑不再下载
- render:{key}    渲染产物文件路径列表（文件仍存在才命中）

WAL 模式 + 短事务，供 asyncio 事件循环线程安全调用。
"""

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from .models import Article, BodyBlock, BodyImage

DB_PATH = Path(__file__).resolve().parent.parent / "news2card_cache.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        "key TEXT PRIMARY KEY, payload TEXT, updated_at REAL)"
    )
    return conn


def _put(key: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO cache(key, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(payload, ensure_ascii=False), time.time()),
        )


def _get(key: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------- Article 序列化

def article_to_dict(a: Article) -> dict:
    return asdict(a)


def article_from_dict(d: dict) -> Article:
    blocks = []
    for b in d.get("blocks") or []:
        img = b.get("image")
        blocks.append(BodyBlock(
            type=b["type"], text=b.get("text", ""),
            image=BodyImage(**img) if img else None,
        ))
    d = {**d, "blocks": blocks}
    return Article(**d)


class StageCache:
    """阶段产物缓存（键规则见模块 docstring）。"""

    def get_article(self, url: str) -> Optional[Article]:
        d = _get(f"article:{url}")
        return article_from_dict(d) if d else None

    def save_article(self, url: str, article: Article) -> None:
        _put(f"article:{url}", article_to_dict(article))

    def get_media(self, url: str) -> Optional[dict]:
        return _get(f"media:{url}")

    def save_media(self, url: str, media: dict) -> None:
        _put(f"media:{url}", media)

    def get_render(self, key: str) -> List[str]:
        d = _get(f"render:{key}")
        if not d:
            return []
        files = [f for f in d.get("files", []) if Path(f).exists()]
        return files

    def save_render(self, key: str, files: List[str]) -> None:
        _put(f"render:{key}", {"files": files})
