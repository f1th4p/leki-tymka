#!/usr/bin/env python3
"""Sprawdź sloty należne w ostatnich 10 minutach i wyślij push przez ntfy.sh.

Uruchamiane co 10 minut przez GitHub Actions (albo cron). Używa tej samej
bazy co app.py — lokalnie SQLite, w produkcji Turso przez libsql.

Wymagane ENV:
  - NTFY_TOPIC              — temat ntfy.sh (sekret, trudna nazwa)
  - TURSO_DATABASE_URL      — opcjonalnie, jeśli używasz Turso
  - TURSO_AUTH_TOKEN        — j.w.
  - LEKI_DB                 — opcjonalna ścieżka do lokalnego SQLite
  - TZ                      — sugerowane "Europe/Warsaw"
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


TZ = ZoneInfo(os.environ.get("APP_TZ", "Europe/Warsaw"))


def _open():
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    db_path = os.environ.get("LEKI_DB", "leki.db")
    if turso_url:
        import libsql  # type: ignore
        raw = libsql.connect(database=db_path, sync_url=turso_url, auth_token=turso_token)
        try:
            raw.sync()
        except Exception:
            pass
        return raw
    return sqlite3.connect(db_path)


def _send(topic: str, title: str, message: str) -> None:
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 4,
        "tags": ["pill"],
    }
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("No NTFY_TOPIC — skipping")
        return 0

    now_local = datetime.now(TZ).replace(second=0, microsecond=0, tzinfo=None)
    today = now_local.date()
    window_start = now_local - timedelta(minutes=10)

    conn = _open()
    cur = conn.execute(
        """
        SELECT s.med_id, m.name, m.unit, m.meal_hint, s.time_of_day, s.dose_amount
        FROM schedules s JOIN medications m ON m.id = s.med_id
        WHERE m.paused = 0 AND m.category = 'controller'
          AND s.active_from <= ? AND (s.active_to IS NULL OR s.active_to > ?)
        """,
        (today.isoformat(), today.isoformat()),
    )

    due: list[tuple[str, int, str, str, str | None]] = []
    for med_id, name, unit, meal_hint, tod, dose in cur.fetchall():
        hh, mm = tod.split(":")
        slot_dt = datetime.combine(today, time(int(hh), int(mm)))
        if not (window_start < slot_dt <= now_local):
            continue
        slot_key = f"{med_id}@{slot_dt.isoformat(timespec='minutes')}"
        exists = conn.execute(
            "SELECT 1 FROM intakes WHERE med_id = ? AND slot_key = ?",
            (med_id, slot_key),
        ).fetchone()
        if exists:
            continue
        sz = conn.execute(
            "SELECT snoozed_to FROM snoozes WHERE med_id = ? AND slot_key = ?",
            (med_id, slot_key),
        ).fetchone()
        if sz:
            try:
                if datetime.fromisoformat(sz[0]) > now_local:
                    continue
            except Exception:
                pass
        due.append((name, int(dose), unit, tod, meal_hint))

    if not due:
        print("No due doses")
        return 0

    lines = []
    for name, dose, unit, tod, hint in due:
        suffix = f" — {hint}" if hint else ""
        lines.append(f"{tod} · {name} × {dose} {unit}{suffix}")
    message = "\n".join(lines)
    title = f"💊 Leki Tymka ({len(due)})"
    try:
        _send(topic, title, message)
    except Exception as e:
        print(f"push failed: {e}")
        return 1
    print(f"pushed {len(due)} doses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
