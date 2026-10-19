"""SQLite durum takibi: hangi video listelendi / izlendi / denetlendi."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  channel TEXT DEFAULT '',
  title TEXT DEFAULT '',
  url TEXT DEFAULT '',
  is_short INTEGER DEFAULT 0,
  duration REAL,
  status TEXT DEFAULT 'listed',   -- listed | watched | analyzed | error
  error TEXT DEFAULT '',
  folder TEXT DEFAULT '',
  frames INTEGER DEFAULT 0,
  mode TEXT DEFAULT '',
  transcript_lang TEXT DEFAULT '',
  analysis TEXT DEFAULT '',
  updated TEXT DEFAULT ''
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def upsert_videos(items: list[dict], channel: str = "") -> None:
    """Listelenen videoları 'listed' olarak kaydeder (durum korunur)."""
    now = _now()
    with _conn() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO videos (id, channel, title, url, is_short, duration, status, updated)
                   VALUES (?,?,?,?,?,?, 'listed', ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title = excluded.title,
                     url = excluded.url,
                     is_short = excluded.is_short,
                     duration = COALESCE(excluded.duration, videos.duration),
                     channel = CASE WHEN excluded.channel != '' THEN excluded.channel
                                    ELSE videos.channel END,
                     updated = excluded.updated""",
                (
                    it["id"],
                    channel or it.get("channel", ""),
                    it.get("title", ""),
                    it.get("url", ""),
                    1 if it.get("is_short") else 0,
                    it.get("duration"),
                    now,
                ),
            )


def set_fields(vid: str, **fields) -> None:
    """Alan günceller; dict değeri analysis için JSON'a çevrilir."""
    if "analysis" in fields and not isinstance(fields["analysis"], str):
        fields["analysis"] = json.dumps(fields["analysis"], ensure_ascii=False)
    if "is_short" in fields:
        fields["is_short"] = 1 if fields["is_short"] else 0
    fields["updated"] = _now()
    cols = ", ".join(f"{key}=?" for key in fields)
    with _conn() as conn:
        conn.execute(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), vid))


def rows(status: str | tuple[str, ...] | None = None) -> list[dict]:
    """Duruma göre kayıtlar (analysis alanı parse edilmiş döner)."""
    with _conn() as conn:
        if status is None:
            cur = conn.execute("SELECT * FROM videos ORDER BY is_short DESC, id")
        elif isinstance(status, str):
            cur = conn.execute(
                "SELECT * FROM videos WHERE status=? ORDER BY is_short DESC, id", (status,)
            )
        else:
            marks = ",".join("?" * len(status))
            cur = conn.execute(
                f"SELECT * FROM videos WHERE status IN ({marks}) ORDER BY is_short DESC, id",
                tuple(status),
            )
        out: list[dict] = []
        for rec in cur.fetchall():
            row = dict(rec)
            if row.get("analysis"):
                try:
                    row["analysis"] = json.loads(row["analysis"])
                except ValueError:
                    row["analysis"] = None
            out.append(row)
        return out


def get(vid: str) -> dict | None:
    for row in rows():
        if row["id"] == vid:
            return row
    return None


def counts() -> dict[str, int]:
    with _conn() as conn:
        cur = conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
        return {rec["status"]: rec["n"] for rec in cur.fetchall()}
