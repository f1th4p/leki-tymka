from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import groupby

import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("LEKI_DB", "leki.db")


def _secret(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    try:
        return st.secrets.get(name)  # type: ignore[attr-defined]
    except Exception:
        return None


TURSO_URL = _secret("TURSO_DATABASE_URL")
TURSO_TOKEN = _secret("TURSO_AUTH_TOKEN")


class _Row(dict):
    """Dict-style row also indexable by position (mimics sqlite3.Row minimally)."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, raw):
        self._raw = raw
        self._cols = [c[0] for c in (raw.description or ())]

    def fetchone(self):
        r = self._raw.fetchone()
        return _Row(self._cols, r) if r is not None else None

    def fetchall(self):
        return [_Row(self._cols, r) for r in self._raw.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Conn:
    def __init__(self, raw, is_remote: bool):
        self._raw = raw
        self._is_remote = is_remote

    def execute(self, sql, params=()):
        if params:
            params = tuple(None if isinstance(p, float) and p != p else p for p in params)
            cur = self._raw.execute(sql, params)
        else:
            cur = self._raw.execute(sql)
        return _Cursor(cur)

    def executemany(self, sql, seq):
        seq = [
            tuple(None if isinstance(p, float) and p != p else p for p in row)
            for row in seq
        ]
        self._raw.executemany(sql, seq)

    def executescript(self, script):
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()
        # Remote sync leci w tle co `sync_interval` sekund — patrz _open_raw.

    def close(self):
        # Połączenie jest cache'owane przez @st.cache_resource, nie zamykamy.
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    form TEXT NOT NULL,
    unit TEXT NOT NULL,
    doses_per_package INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    med_id INTEGER NOT NULL REFERENCES medications(id),
    purchased_at TEXT NOT NULL,
    opened_at TEXT,
    doses_initial INTEGER NOT NULL,
    doses_left INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    med_id INTEGER NOT NULL REFERENCES medications(id),
    time_of_day TEXT NOT NULL,
    dose_amount INTEGER NOT NULL DEFAULT 1,
    active_from TEXT NOT NULL,
    active_to TEXT
);

CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY,
    med_id INTEGER NOT NULL REFERENCES medications(id),
    taken_at TEXT NOT NULL,
    doses INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('scheduled','ad_hoc','skipped')),
    auto INTEGER NOT NULL DEFAULT 0,
    package_id INTEGER REFERENCES packages(id),
    slot_key TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_intake_slot
    ON intakes(med_id, slot_key) WHERE slot_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS health_logs (
    id INTEGER PRIMARY KEY,
    log_date TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('morning','evening')),
    pef INTEGER,
    symptoms INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_health_date_period
    ON health_logs(log_date, period);

CREATE TABLE IF NOT EXISTS snoozes (
    id INTEGER PRIMARY KEY,
    med_id INTEGER NOT NULL,
    slot_key TEXT NOT NULL,
    snoozed_to TEXT NOT NULL,
    UNIQUE(med_id, slot_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SEED_MEDS = [
    ("Ventolin Dysk 200", "inhalator", "dawka", 60),
    ("Momester 50", "spray donosowy", "dawka", 140),
    ("Montelukast 5 mg", "tabletki do żucia", "tabletka", 28),
    ("Flixotide Dysk 100", "inhalator", "dawka", 60),
    ("Seretide Dysk 100", "inhalator", "dawka", 60),
    ("Clatra 20 mg", "tabletki", "tabletka", 30),
]

SEED_SCHEDULE = [
    ("Ventolin Dysk 200", "08:00"),
    ("Ventolin Dysk 200", "20:00"),
    ("Momester 50", "08:00"),
    ("Montelukast 5 mg", "08:00"),
    ("Flixotide Dysk 100", "08:00"),
    ("Flixotide Dysk 100", "20:00"),
    ("Seretide Dysk 100", "08:00"),
    ("Seretide Dysk 100", "20:00"),
    ("Clatra 20 mg", "08:00"),
]


@st.cache_resource
def _open_raw():
    if TURSO_URL:
        import libsql
        raw = libsql.connect(
            database=DB_PATH,
            sync_url=TURSO_URL,
            auth_token=TURSO_TOKEN,
            sync_interval=30,
        )
        try:
            raw.sync()
        except Exception:
            pass
        return raw, True
    raw = sqlite3.connect(DB_PATH, check_same_thread=False)
    raw.execute("PRAGMA foreign_keys = ON")
    return raw, False


@contextmanager
def conn():
    raw, is_remote = _open_raw()
    c = _Conn(raw, is_remote)
    yield c
    c.commit()


MIGRATIONS = [
    "ALTER TABLE medications ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE medications ADD COLUMN paused_reason TEXT",
    "ALTER TABLE packages ADD COLUMN brand TEXT",
    "ALTER TABLE packages ADD COLUMN approximate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE medications ADD COLUMN category TEXT NOT NULL DEFAULT 'controller'",
    "ALTER TABLE medications ADD COLUMN meal_hint TEXT",
    "ALTER TABLE intakes ADD COLUMN given_by TEXT",
    "UPDATE medications SET category = 'rescue' WHERE name LIKE 'Ventolin%' AND category = 'controller'",
]


def _migrate(c) -> None:
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except Exception:
            pass


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        if c.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0:
            seed_rows = [
                (n, f, u, d, "rescue" if n.startswith("Ventolin") else "controller")
                for n, f, u, d in SEED_MEDS
            ]
            c.executemany(
                "INSERT INTO medications(name, form, unit, doses_per_package, category) VALUES(?,?,?,?,?)",
                seed_rows,
            )
            today = date.today().isoformat()
            meds = {row["name"]: row["id"] for row in c.execute("SELECT id, name FROM medications")}
            for name, _form, _unit, dpp in SEED_MEDS:
                mid = meds[name]
                c.execute(
                    "INSERT INTO packages(med_id, purchased_at, opened_at, doses_initial, doses_left, active) VALUES(?,?,?,?,?,1)",
                    (mid, today, today, dpp, dpp),
                )
                c.execute(
                    "INSERT INTO packages(med_id, purchased_at, opened_at, doses_initial, doses_left, active) VALUES(?,?,NULL,?,?,1)",
                    (mid, today, dpp, dpp),
                )
            for name, t in SEED_SCHEDULE:
                c.execute(
                    "INSERT INTO schedules(med_id, time_of_day, dose_amount, active_from) VALUES(?,?,?,?)",
                    (meds[name], t, 1, today),
                )


@dataclass
class Slot:
    med_id: int
    med_name: str
    unit: str
    dose_amount: int
    slot_dt: datetime
    slot_key: str
    status: str


def _slot_key(med_id: int, slot_dt: datetime) -> str:
    return f"{med_id}@{slot_dt.isoformat(timespec='minutes')}"


def _generate_expected_slots(c: sqlite3.Connection, horizon_start: date, now: datetime) -> list[Slot]:
    rows = c.execute(
        """
        SELECT s.id, s.med_id, m.name, m.unit, s.time_of_day, s.dose_amount, s.active_from, s.active_to
        FROM schedules s JOIN medications m ON m.id = s.med_id
        WHERE m.paused = 0
        """
    ).fetchall()
    slots: list[Slot] = []
    today = now.date()
    for r in rows:
        af = date.fromisoformat(r["active_from"])
        at = date.fromisoformat(r["active_to"]) if r["active_to"] else None
        hh, mm = r["time_of_day"].split(":")
        t = time(int(hh), int(mm))
        start = max(af, horizon_start)
        # active_to jest exclusive — ostatni aktywny dzień to active_to - 1
        end = min(at - timedelta(days=1), today) if at else today
        d = start
        while d <= end:
            slot_dt = datetime.combine(d, t)
            slots.append(
                Slot(
                    med_id=r["med_id"],
                    med_name=r["name"],
                    unit=r["unit"],
                    dose_amount=r["dose_amount"],
                    slot_dt=slot_dt,
                    slot_key=_slot_key(r["med_id"], slot_dt),
                    status="pending",
                )
            )
            d += timedelta(days=1)
    return slots


def _fifo_deduct(c: sqlite3.Connection, med_id: int, doses: int, when: datetime) -> int | None:
    pkg = c.execute(
        """
        SELECT id, doses_left, opened_at FROM packages
        WHERE med_id = ? AND active = 1 AND doses_left > 0
        ORDER BY (opened_at IS NULL), opened_at, purchased_at, id
        LIMIT 1
        """,
        (med_id,),
    ).fetchone()
    if pkg is None:
        return None
    if pkg["opened_at"] is None:
        c.execute("UPDATE packages SET opened_at = ? WHERE id = ?", (when.date().isoformat(), pkg["id"]))
    new_left = max(0, pkg["doses_left"] - doses)
    still_active = 1 if new_left > 0 else 0
    c.execute(
        "UPDATE packages SET doses_left = ?, active = ? WHERE id = ?",
        (new_left, still_active, pkg["id"]),
    )
    return pkg["id"]


def close_pending(now: datetime) -> int:
    created = 0
    today = now.date()
    with conn() as c:
        last = c.execute(
            "SELECT MAX(taken_at) AS m FROM intakes WHERE kind IN ('scheduled','skipped')"
        ).fetchone()["m"]
        horizon = (
            datetime.fromisoformat(last).date() if last else today - timedelta(days=1)
        )
        for s in _generate_expected_slots(c, horizon, now):
            if s.slot_dt.date() >= today:
                continue
            if s.slot_dt > now:
                continue
            exists = c.execute(
                "SELECT 1 FROM intakes WHERE med_id = ? AND slot_key = ?",
                (s.med_id, s.slot_key),
            ).fetchone()
            if exists:
                continue
            pkg_id = _fifo_deduct(c, s.med_id, s.dose_amount, s.slot_dt)
            c.execute(
                """
                INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id, slot_key)
                VALUES(?,?,?,?,?,?,?)
                """,
                (s.med_id, s.slot_dt.isoformat(timespec="minutes"), s.dose_amount,
                 "scheduled", 1, pkg_id, s.slot_key),
            )
            created += 1
    return created


def today_slots(now: datetime) -> list[dict]:
    today = now.date()
    with conn() as c:
        sched = c.execute(
            """
            SELECT s.med_id, m.name, m.unit, m.meal_hint, s.time_of_day, s.dose_amount,
                   s.active_from, s.active_to
            FROM schedules s JOIN medications m ON m.id = s.med_id
            WHERE m.paused = 0 AND m.category = 'controller'
              AND s.active_from <= ? AND (s.active_to IS NULL OR s.active_to > ?)
            ORDER BY s.time_of_day, m.name
            """,
            (today.isoformat(), today.isoformat()),
        ).fetchall()
        out = []
        for r in sched:
            hh, mm = r["time_of_day"].split(":")
            slot_dt = datetime.combine(today, time(int(hh), int(mm)))
            key = _slot_key(r["med_id"], slot_dt)
            intake = c.execute(
                "SELECT id, kind, auto, given_by FROM intakes WHERE med_id = ? AND slot_key = ?",
                (r["med_id"], key),
            ).fetchone()
            sz = c.execute(
                "SELECT snoozed_to FROM snoozes WHERE med_id = ? AND slot_key = ?",
                (r["med_id"], key),
            ).fetchone()
            out.append({
                "med_id": r["med_id"],
                "name": r["name"],
                "unit": r["unit"],
                "meal_hint": r["meal_hint"],
                "time": r["time_of_day"],
                "slot_dt": slot_dt,
                "slot_key": key,
                "dose_amount": r["dose_amount"],
                "intake_id": intake["id"] if intake else None,
                "kind": intake["kind"] if intake else None,
                "auto": bool(intake["auto"]) if intake else False,
                "given_by": intake["given_by"] if intake else None,
                "snoozed_to": datetime.fromisoformat(sz["snoozed_to"]) if sz else None,
            })
        return out


def stock_overview() -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT m.id AS med_id, m.name, m.unit, m.doses_per_package,
                   m.paused, m.paused_reason, m.category, m.meal_hint,
                   COALESCE(SUM(CASE WHEN p.active = 1 THEN p.doses_left ELSE 0 END), 0) AS doses_left,
                   SUM(CASE WHEN p.active = 1 THEN 1 ELSE 0 END) AS active_packages
            FROM medications m LEFT JOIN packages p ON p.med_id = m.id
            GROUP BY m.id ORDER BY m.name
            """
        ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
    return df


def paused_meds() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, name, paused_reason FROM medications WHERE paused = 1 ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def pause_med(med_id: int, reason: str) -> None:
    today = date.today().isoformat()
    with conn() as c:
        c.execute(
            "UPDATE schedules SET active_to = ? WHERE med_id = ? AND active_to IS NULL",
            (today, med_id),
        )
        c.execute(
            "UPDATE medications SET paused = 1, paused_reason = ? WHERE id = ?",
            (reason, med_id),
        )


def resume_med(med_id: int) -> None:
    today = date.today().isoformat()
    with conn() as c:
        last = c.execute(
            """
            SELECT time_of_day, dose_amount FROM schedules
            WHERE med_id = ? AND id IN (
                SELECT MAX(id) FROM schedules WHERE med_id = ? GROUP BY time_of_day
            )
            """,
            (med_id, med_id),
        ).fetchall()
        for s in last:
            c.execute(
                "INSERT INTO schedules(med_id, time_of_day, dose_amount, active_from) VALUES(?,?,?,?)",
                (med_id, s["time_of_day"], s["dose_amount"], today),
            )
        c.execute(
            "UPDATE medications SET paused = 0, paused_reason = NULL WHERE id = ?",
            (med_id,),
        )


def days_of_supply(med_id: int, doses_left: int) -> float | None:
    with conn() as c:
        rows = c.execute(
            "SELECT dose_amount FROM schedules WHERE med_id = ? AND active_to IS NULL",
            (med_id,),
        ).fetchall()
    per_day = sum(r["dose_amount"] for r in rows)
    if per_day == 0:
        return None
    return round(doses_left / per_day, 1)


def end_date_for(med_id: int, doses_left: int) -> str | None:
    dps = days_of_supply(med_id, doses_left)
    if dps is None:
        return None
    return (date.today() + timedelta(days=int(dps))).isoformat()


def skip_slot(med_id: int, slot_key: str, slot_dt: datetime) -> None:
    with conn() as c:
        existing = c.execute(
            "SELECT id, kind, package_id, doses FROM intakes WHERE med_id = ? AND slot_key = ?",
            (med_id, slot_key),
        ).fetchone()
        if existing:
            if existing["kind"] == "skipped":
                return
            if existing["package_id"] is not None:
                c.execute(
                    "UPDATE packages SET doses_left = doses_left + ?, active = 1 WHERE id = ?",
                    (existing["doses"], existing["package_id"]),
                )
            c.execute("DELETE FROM intakes WHERE id = ?", (existing["id"],))
        c.execute(
            """
            INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id, slot_key)
            VALUES(?,?,?,?,?,?,?)
            """,
            (med_id, slot_dt.isoformat(timespec="minutes"), 0, "skipped", 0, None, slot_key),
        )


def undo_slot(med_id: int, slot_key: str) -> None:
    with conn() as c:
        row = c.execute(
            "SELECT id, package_id, doses, kind FROM intakes WHERE med_id = ? AND slot_key = ?",
            (med_id, slot_key),
        ).fetchone()
        if not row:
            return
        if row["kind"] == "scheduled" and row["package_id"] is not None:
            c.execute(
                "UPDATE packages SET doses_left = doses_left + ?, active = 1 WHERE id = ?",
                (row["doses"], row["package_id"]),
            )
        c.execute("DELETE FROM intakes WHERE id = ?", (row["id"],))


def take_slot_now(med_id: int, slot_key: str, dose_amount: int, when: datetime, given_by: str | None = None) -> None:
    with conn() as c:
        exists = c.execute(
            "SELECT 1 FROM intakes WHERE med_id = ? AND slot_key = ?",
            (med_id, slot_key),
        ).fetchone()
        if exists:
            return
        pkg_id = _fifo_deduct(c, med_id, dose_amount, when)
        c.execute(
            """
            INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id, slot_key, given_by)
            VALUES(?,?,?,?,0,?,?,?)
            """,
            (med_id, when.isoformat(timespec="minutes"), dose_amount, "scheduled", pkg_id, slot_key, given_by),
        )
        c.execute("DELETE FROM snoozes WHERE med_id = ? AND slot_key = ?", (med_id, slot_key))


def record_ad_hoc(med_id: int, doses: int, when: datetime, given_by: str | None = None) -> None:
    with conn() as c:
        pkg_id = _fifo_deduct(c, med_id, doses, when)
        c.execute(
            "INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id, given_by) VALUES(?,?,?,?,0,?,?)",
            (med_id, when.isoformat(timespec="minutes"), doses, "ad_hoc", pkg_id, given_by),
        )


def snooze_slot(med_id: int, slot_key: str, snoozed_to: datetime) -> None:
    with conn() as c:
        c.execute(
            """
            INSERT INTO snoozes(med_id, slot_key, snoozed_to) VALUES(?,?,?)
            ON CONFLICT(med_id, slot_key) DO UPDATE SET snoozed_to = excluded.snoozed_to
            """,
            (med_id, slot_key, snoozed_to.isoformat(timespec="minutes")),
        )


def add_packages(
    med_id: int,
    count: int,
    doses_each: int,
    purchased_at: date,
    brand: str | None = None,
    approximate: bool = False,
) -> None:
    with conn() as c:
        for _ in range(count):
            c.execute(
                """
                INSERT INTO packages(med_id, purchased_at, opened_at, doses_initial, doses_left, active, brand, approximate)
                VALUES(?,?,NULL,?,?,1,?,?)
                """,
                (med_id, purchased_at.isoformat(), doses_each, doses_each, brand or None, 1 if approximate else 0),
            )


def list_packages(med_id: int) -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT id, purchased_at, opened_at, doses_initial, doses_left, active, brand, approximate
            FROM packages WHERE med_id = ?
            ORDER BY (opened_at IS NULL), opened_at, purchased_at, id
            """,
            (med_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def update_package(pkg_id: int, doses_left: int, active: int, opened_at: str | None) -> None:
    with conn() as c:
        c.execute(
            "UPDATE packages SET doses_left = ?, active = ?, opened_at = ? WHERE id = ?",
            (doses_left, active, opened_at, pkg_id),
        )


def rename_med(med_id: int, new_name: str) -> None:
    with conn() as c:
        c.execute("UPDATE medications SET name = ? WHERE id = ?", (new_name, med_id))


def update_package_initial(pkg_id: int, doses_initial: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE packages SET doses_initial = ?, doses_left = MIN(doses_left, ?) WHERE id = ?",
            (doses_initial, doses_initial, pkg_id),
        )


def schedules_for_med(med_id: int) -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT id, time_of_day, dose_amount, active_from, active_to
            FROM schedules WHERE med_id = ?
            ORDER BY (active_to IS NOT NULL), time_of_day, active_from
            """,
            (med_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def weekly_adherence(now: datetime) -> tuple[int, int]:
    start = now.date() - timedelta(days=6)
    with conn() as c:
        slots = _generate_expected_slots(c, start, now)
        expected = sum(1 for s in slots if s.slot_dt <= now)
        start_iso = datetime.combine(start, time(0, 0)).isoformat(timespec="minutes")
        end_iso = now.isoformat(timespec="minutes")
        taken = c.execute(
            """
            SELECT COUNT(*) FROM intakes
            WHERE kind = 'scheduled' AND slot_key IS NOT NULL
              AND taken_at >= ? AND taken_at <= ?
            """,
            (start_iso, end_iso),
        ).fetchone()[0]
    return int(taken), int(expected)


def all_meds() -> pd.DataFrame:
    with conn() as c:
        return pd.DataFrame([dict(r) for r in c.execute(
            "SELECT id, name, unit, doses_per_package, category, meal_hint FROM medications ORDER BY name"
        )])


def set_med_category(med_id: int, category: str, meal_hint: str | None) -> None:
    with conn() as c:
        c.execute(
            "UPDATE medications SET category = ?, meal_hint = ? WHERE id = ?",
            (category, meal_hint, med_id),
        )


def get_setting(key: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    with conn() as c:
        if value is None or value == "":
            c.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            c.execute(
                """
                INSERT INTO settings(key, value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )


def log_health(log_date: str, period: str, pef: int | None, symptoms: int | None, note: str | None) -> None:
    with conn() as c:
        c.execute(
            """
            INSERT INTO health_logs(log_date, period, pef, symptoms, note, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(log_date, period) DO UPDATE SET
                pef = excluded.pef,
                symptoms = excluded.symptoms,
                note = excluded.note,
                created_at = excluded.created_at
            """,
            (log_date, period, pef, symptoms, note, datetime.now().isoformat(timespec="minutes")),
        )


def health_logs(days: int = 30) -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT log_date, period, pef, symptoms, note
            FROM health_logs ORDER BY log_date DESC, period DESC LIMIT ?
            """,
            (days * 2,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def streak_days(now: datetime) -> int:
    today = now.date()
    streak = 0
    with conn() as c:
        d = today - timedelta(days=1)
        oldest = today - timedelta(days=365)
        while d >= oldest:
            day_end = datetime.combine(d, time(23, 59))
            day_slots = [
                s for s in _generate_expected_slots(c, d, day_end)
                if s.slot_dt.date() == d
            ]
            if not day_slots:
                break
            taken = c.execute(
                """
                SELECT COUNT(*) FROM intakes
                WHERE kind = 'scheduled' AND slot_key IS NOT NULL
                  AND taken_at LIKE ? || '%'
                """,
                (d.isoformat(),),
            ).fetchone()[0]
            if taken < len(day_slots):
                break
            streak += 1
            d -= timedelta(days=1)
    return streak


def monthly_adherence(year: int, month: int) -> list[dict]:
    from calendar import monthrange
    _, ndays = monthrange(year, month)
    today = date.today()
    out = []
    with conn() as c:
        for d in range(1, ndays + 1):
            dt = date(year, month, d)
            if dt > today:
                out.append({"day": d, "date": dt, "expected": 0, "taken": 0, "status": ""})
                continue
            day_slots = [
                s for s in _generate_expected_slots(c, dt, datetime.combine(dt, time(23, 59)))
                if s.slot_dt.date() == dt
            ]
            exp = len(day_slots)
            taken = c.execute(
                """
                SELECT COUNT(*) FROM intakes
                WHERE kind = 'scheduled' AND slot_key IS NOT NULL
                  AND taken_at LIKE ? || '%'
                """,
                (dt.isoformat(),),
            ).fetchone()[0]
            if exp == 0:
                status = "·"
            elif taken >= exp:
                status = "🟢"
            elif taken == 0:
                status = "🔴"
            else:
                status = "🟡"
            out.append({"day": d, "date": dt, "expected": exp, "taken": int(taken), "status": status})
    return out


def rescue_meds() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, name, unit FROM medications WHERE category = 'rescue' AND paused = 0 ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def rescue_doses_today(med_id: int, today: date) -> int:
    with conn() as c:
        row = c.execute(
            """
            SELECT COALESCE(SUM(doses), 0) AS s FROM intakes
            WHERE med_id = ? AND kind = 'ad_hoc' AND taken_at LIKE ? || '%'
            """,
            (med_id, today.isoformat()),
        ).fetchone()
    return int(row["s"])


def add_schedule(med_id: int, time_of_day: str, dose_amount: int) -> None:
    today = date.today().isoformat()
    with conn() as c:
        c.execute(
            "INSERT INTO schedules(med_id, time_of_day, dose_amount, active_from) VALUES(?,?,?,?)",
            (med_id, time_of_day, dose_amount, today),
        )


def end_schedule(schedule_id: int) -> None:
    today = date.today().isoformat()
    with conn() as c:
        c.execute("UPDATE schedules SET active_to = ? WHERE id = ? AND active_to IS NULL", (today, schedule_id))


def _require_password() -> None:
    pw = _secret("APP_PASSWORD")
    if not pw:
        return
    if st.session_state.get("authed"):
        return
    token = st.query_params.get("k")
    if token == pw:
        st.session_state["authed"] = True
        return
    st.title("🔒 Leki Tymka")
    entered = st.text_input("Hasło", type="password")
    if entered:
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Błędne hasło")
    st.stop()


def main():
    st.set_page_config(page_title="Leki Tymka", page_icon="💊", layout="centered")
    _require_password()
    init_db()
    now = datetime.now().replace(second=0, microsecond=0)

    created = close_pending(now)
    if created:
        st.toast(f"Domknięto {created} zaplanowanych dawek do {now:%H:%M}.")

    st.title("💊 Leki Tymka")
    st.caption(f"Teraz: {now:%Y-%m-%d %H:%M}")

    tab_dzis, tab_apteczka, tab_historia, tab_zdrowie = st.tabs(
        ["Dziś", "Apteczka", "Historia", "Zdrowie"]
    )

    with tab_dzis:
        slots = today_slots(now)

        caregivers_raw = get_setting("caregivers") or "Ja"
        caregivers = [x.strip() for x in caregivers_raw.split(",") if x.strip()] or ["Ja"]
        if len(caregivers) > 1:
            giver = st.selectbox("👤 Kto daje dzisiaj", caregivers, key="giver")
        else:
            giver = caregivers[0]

        taken = sum(1 for s in slots if s["kind"] == "scheduled")
        total = len(slots)
        upcoming = [
            s for s in slots
            if s["slot_dt"] > now and s["kind"] is None
            and (s["snoozed_to"] is None or s["snoozed_to"] <= now or s["snoozed_to"] > s["slot_dt"])
        ]
        if upcoming:
            nu = min(upcoming, key=lambda s: s["slot_dt"])
            delta = nu["slot_dt"] - now
            h, m = divmod(delta.seconds // 60, 60)
            hm = f"{h}h {m}min" if h else f"{m} min"
            st.info(
                f"🕒 Następna: **{nu['name']}** o {nu['time']} (za {hm}) • "
                f"zażyte dziś: {taken}/{total}"
            )
        elif total and taken >= total:
            st.success(f"🎉 Wszystkie dzisiejsze dawki wzięte ({taken}/{total})")

        wk_taken, wk_expected = weekly_adherence(now)
        streak = streak_days(now)
        pieces = []
        if wk_expected:
            pct = round(100 * wk_taken / wk_expected)
            pieces.append(f"📊 7 dni: {wk_taken}/{wk_expected} ({pct}%)")
        if streak:
            pieces.append(f"🔥 {streak} dni pod rząd 100%")
        if pieces:
            st.caption("  •  ".join(pieces))

        visit_iso = get_setting("next_doctor_visit")
        if visit_iso:
            try:
                vd = date.fromisoformat(visit_iso)
                days_to_visit = (vd - now.date()).days
                if days_to_visit >= 0:
                    stockdf = stock_overview()
                    at_risk = []
                    for r in stockdf.itertuples():
                        if r.paused or r.category == "rescue":
                            continue
                        dps = days_of_supply(int(r.med_id), int(r.doses_left))
                        if dps is not None and dps < days_to_visit + 7:
                            at_risk.append(r.name)
                    if at_risk:
                        st.warning(
                            f"📅 Wizyta u pediatry za {days_to_visit} dni ({vd:%d.%m}). "
                            f"Zapas może nie dociągnąć: {', '.join(at_risk)}"
                        )
            except ValueError:
                pass

        rescue = rescue_meds()
        if rescue:
            st.markdown("**🚨 Ratunkowe**")
            for rm in rescue:
                doses_today = rescue_doses_today(int(rm["id"]), now.date())
                c1, c2, c3 = st.columns([3, 2, 2])
                label = f"💨 **{rm['name']}**"
                if doses_today > 0:
                    label += f" — dziś: **{doses_today}** {rm['unit']}"
                c1.write(label)
                qty_r = c2.number_input(
                    "ile", min_value=1, max_value=10, value=1, step=1,
                    key=f"rescue_qty_{rm['id']}", label_visibility="collapsed",
                )
                if c3.button("+ Napad", key=f"rescue_btn_{rm['id']}", type="primary"):
                    record_ad_hoc(int(rm["id"]), int(qty_r), now, given_by=giver)
                    st.rerun()
                if doses_today >= 3:
                    st.error(
                        f"⚠️ {doses_today} dawek ratunkowych dziś — rozważ konsultację."
                    )
            st.divider()

        pm = paused_meds()
        if pm:
            st.warning(
                "⏸️ Zawieszone: "
                + ", ".join(
                    f"**{p['name']}**" + (f" ({p['paused_reason']})" if p.get("paused_reason") else "")
                    for p in pm
                )
                + " — sloty nie są generowane, opakowania nieruszone."
            )

        if not slots:
            st.info("Brak zaplanowanych dawek na dziś.")
        else:
            for t_str, group_iter in groupby(slots, key=lambda s: s["time"]):
                group = list(group_iter)
                pending = [s for s in group if s["kind"] is None]
                if len(group) > 1 and len(pending) > 1:
                    if st.button(
                        f"✅ Wziął wszystko o {t_str} ({len(pending)})",
                        key=f"bulk_{t_str}",
                    ):
                        for s in pending:
                            take_slot_now(s["med_id"], s["slot_key"], s["dose_amount"], now, given_by=giver)
                        st.rerun()
                for s in group:
                    past = s["slot_dt"] <= now
                    snoozed_active = (
                        s["kind"] is None
                        and s["snoozed_to"] is not None
                        and s["snoozed_to"] > now
                    )
                    if s["kind"] == "scheduled":
                        icon = "✅"
                    elif s["kind"] == "skipped":
                        icon = "⏭️"
                    elif snoozed_active:
                        icon = "💤"
                    elif past:
                        icon = "⏳"
                    else:
                        icon = "🕒"
                    meal_suffix = f" 🍽️ *{s['meal_hint']}*" if s.get("meal_hint") else ""
                    giver_suffix = f" · {s['given_by']}" if s.get("given_by") else ""
                    cols = st.columns([4, 1.2, 1.5, 1.5, 1.3])
                    cols[0].write(
                        f"{icon} **{s['name']}** — {s['dose_amount']} {s['unit']}{meal_suffix}{giver_suffix}"
                    )
                    if snoozed_active:
                        cols[1].write(f"{s['time']} → {s['snoozed_to']:%H:%M}")
                    else:
                        cols[1].write(s["time"])
                    if s["kind"] in ("scheduled", "skipped"):
                        if cols[2].button("cofnij", key=f"undo_{s['slot_key']}"):
                            undo_slot(s["med_id"], s["slot_key"])
                            st.rerun()
                    else:
                        if cols[2].button("wziął", key=f"take_{s['slot_key']}"):
                            take_slot_now(s["med_id"], s["slot_key"], s["dose_amount"], now, given_by=giver)
                            st.rerun()
                    if s["kind"] is None:
                        if cols[3].button("💤 +30min", key=f"snz_{s['slot_key']}"):
                            snooze_slot(s["med_id"], s["slot_key"], now + timedelta(minutes=30))
                            st.rerun()
                    if s["kind"] != "skipped":
                        if cols[4].button("pomiń", key=f"skip_{s['slot_key']}"):
                            skip_slot(s["med_id"], s["slot_key"], s["slot_dt"])
                            st.rerun()

        st.divider()
        with st.expander("💉 Dawka doraźna"):
            meds_df = all_meds()
            if not meds_df.empty:
                name = st.selectbox("Lek", meds_df["name"], key="adhoc_med")
                qty = st.number_input("Liczba dawek", min_value=1, max_value=20, value=1, step=1, key="adhoc_qty")
                when_date = st.date_input("Data", value=now.date(), key="adhoc_date")
                when_time = st.time_input("Godzina", value=now.time().replace(second=0, microsecond=0), key="adhoc_time")
                if st.button("Zarejestruj dawkę doraźną", type="primary", key="adhoc_btn"):
                    med_id = int(meds_df.loc[meds_df["name"] == name, "id"].iloc[0])
                    record_ad_hoc(med_id, int(qty), datetime.combine(when_date, when_time), given_by=giver)
                    st.success(f"Zapisano: {name} × {qty}")
                    st.rerun()

    with tab_apteczka:
        df = stock_overview()
        df["dni_zapasu"] = [
            None if bool(r.paused) else days_of_supply(int(r.med_id), int(r.doses_left))
            for r in df.itertuples()
        ]
        df["do_kiedy"] = [
            None if bool(r.paused) else end_date_for(int(r.med_id), int(r.doses_left))
            for r in df.itertuples()
        ]
        def _row_label(r):
            icon = "🚨" if r["category"] == "rescue" else "💊"
            pause = "⏸️ " if r["paused"] else ""
            meal = f" 🍽️" if r.get("meal_hint") else ""
            return f"{icon} {pause}{r['name']}{meal}"

        df["display_name"] = df.apply(_row_label, axis=1)

        st.subheader("Stan ogólny")
        active_only = df[df["paused"] == 0].copy()
        low = active_only[active_only["dni_zapasu"].fillna(999) < 7]
        if not low.empty:
            st.warning(f"⚠️ Mało zapasu (< 7 dni) — {len(low)} lek(ów)")
            for r in low.itertuples():
                with st.expander(f"{r.name} — {r.dni_zapasu} d, do {r.do_kiedy}"):
                    c1, c2 = st.columns([1, 2])
                    n_low = c1.number_input(
                        "Ile szt.", min_value=1, max_value=10, value=1, step=1,
                        key=f"lown_{r.med_id}",
                    )
                    pdate_low = c2.date_input(
                        "Data zakupu", value=date.today(), key=f"lowd_{r.med_id}"
                    )
                    if st.button(
                        "➕ Dodaj opakowanie", key=f"lowadd_{r.med_id}", type="primary"
                    ):
                        add_packages(
                            int(r.med_id), int(n_low), int(r.doses_per_package), pdate_low
                        )
                        st.success(f"Dodano {n_low} op. {r.name}")
                        st.rerun()
        st.dataframe(
            df[["display_name", "doses_left", "active_packages", "dni_zapasu", "do_kiedy"]].rename(
                columns={
                    "display_name": "Lek",
                    "doses_left": "Pozostałe dawki",
                    "active_packages": "Aktywne opak.",
                    "dni_zapasu": "Dni zapasu",
                    "do_kiedy": "Skończy się",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption("💊 codzienny · 🚨 ratunkowy · 🍽️ wskazówka posiłkowa · ⏸️ zawieszony")

        st.divider()
        st.subheader("Szczegóły leków")
        meds_df = all_meds()
        pm_list = paused_meds()
        paused_info = {p["id"]: (p.get("paused_reason") or "") for p in pm_list}
        for m in meds_df.itertuples():
            is_paused = int(m.id) in paused_info
            label = f"{'⏸️ ' if is_paused else ''}{m.name}"
            with st.expander(label):
                with st.popover("✏️ Zmień nazwę leku"):
                    new_name = st.text_input(
                        "Nowa nazwa",
                        value=m.name,
                        key=f"rename_input_{m.id}",
                    )
                    if st.button("Zapisz nazwę", key=f"rename_btn_{m.id}", type="primary"):
                        nn = new_name.strip()
                        if nn and nn != m.name:
                            rename_med(int(m.id), nn)
                            st.rerun()
                hint_options = ["", "przed jedzeniem", "z jedzeniem", "po jedzeniu", "na pusty żołądek", "przed snem"]
                cur_hint = m.meal_hint or ""
                cur_cat = m.category or "controller"
                with st.popover("🏷️ Typ / posiłek"):
                    new_cat = st.radio(
                        "Typ leku",
                        options=["controller", "rescue"],
                        index=0 if cur_cat == "controller" else 1,
                        format_func=lambda x: "💊 Codzienny" if x == "controller" else "🚨 Ratunkowy",
                        key=f"cat_{m.id}",
                        help="Ratunkowe nie pojawiają się w slotach dziennych — rejestrujesz je przyciskiem '+ Napad'.",
                    )
                    new_hint = st.selectbox(
                        "Wskazówka posiłkowa",
                        options=hint_options,
                        index=hint_options.index(cur_hint) if cur_hint in hint_options else 0,
                        format_func=lambda x: "— brak —" if x == "" else x,
                        key=f"hint_{m.id}",
                    )
                    if st.button("Zapisz", key=f"cat_save_{m.id}", type="primary"):
                        set_med_category(int(m.id), new_cat, new_hint or None)
                        st.rerun()
                if is_paused:
                    st.info(f"Zawieszony. Powód: *{paused_info.get(int(m.id)) or '—'}*")
                    if st.button("▶️ Wznów (odtworzy schemat od dziś)", key=f"resume_{m.id}"):
                        resume_med(int(m.id))
                        st.rerun()
                else:
                    with st.popover("⏸️ Zawieś lek"):
                        reason = st.text_input(
                            "Powód (np. niedostępny w aptece)",
                            key=f"pause_reason_{m.id}",
                        )
                        st.caption("Zamyka aktywny schemat, sloty przestają być generowane, opakowania nieruszone.")
                        if st.button("Potwierdź zawieszenie", key=f"pause_btn_{m.id}", type="primary"):
                            pause_med(int(m.id), reason.strip())
                            st.rerun()

                st.markdown("**Schemat**")
                sdf = schedules_for_med(int(m.id))
                if sdf.empty:
                    st.caption("Brak wpisów.")
                else:
                    active_sch = sdf[sdf["active_to"].isna()]
                    ended_sch = sdf[sdf["active_to"].notna()]
                    if active_sch.empty:
                        st.caption("Brak aktywnych wpisów.")
                    for sch in active_sch.itertuples():
                        c1, c2 = st.columns([4, 1])
                        c1.write(f"• {sch.time_of_day} × {sch.dose_amount} (od {sch.active_from})")
                        if c2.button("zakończ", key=f"endsch_{sch.id}"):
                            end_schedule(int(sch.id))
                            st.rerun()
                    if not ended_sch.empty:
                        with st.popover(f"Zakończone ({len(ended_sch)})"):
                            for sch in ended_sch.itertuples():
                                st.caption(
                                    f"{sch.time_of_day} × {sch.dose_amount} • "
                                    f"{sch.active_from} → {sch.active_to}"
                                )
                with st.popover("➕ Dodaj wpis do schematu"):
                    t_new = st.time_input("Godzina", value=time(8, 0), key=f"addsch_t_{m.id}")
                    amt_new = st.number_input(
                        "Dawka", min_value=1, max_value=10, value=1, step=1,
                        key=f"addsch_a_{m.id}",
                    )
                    if st.button("Dodaj", key=f"addsch_btn_{m.id}", type="primary"):
                        add_schedule(
                            int(m.id),
                            f"{t_new.hour:02d}:{t_new.minute:02d}",
                            int(amt_new),
                        )
                        st.rerun()

                st.markdown("**Opakowania**")
                pkgs = list_packages(int(m.id))
                if pkgs.empty:
                    st.caption("Brak opakowań.")
                else:
                    for p in pkgs.itertuples():
                        brand_label = p.brand if p.brand else m.name
                        approx_mark = " ~" if p.approximate else ""
                        with st.form(f"pkg_form_{p.id}"):
                            opened_disp = p.opened_at if pd.notna(p.opened_at) else None
                            st.write(
                                f"**{brand_label}**{approx_mark}  •  "
                                f"Zakup: {p.purchased_at} • Otwarte: {opened_disp or '—'}"
                            )
                            c1, c2, c3 = st.columns([2, 2, 1])
                            new_initial = c1.number_input(
                                "Pojemność opak.",
                                min_value=1, max_value=1000,
                                value=int(p.doses_initial), step=1,
                                key=f"pkg_init_{p.id}",
                                help="Ile dawek łącznie mieści to opakowanie.",
                            )
                            new_left = c2.number_input(
                                "Dawki pozostałe" + (" (szac.)" if p.approximate else ""),
                                min_value=0, max_value=int(new_initial),
                                value=min(int(p.doses_left), int(new_initial)),
                                step=1,
                                key=f"pkg_left_{p.id}",
                            )
                            new_active = 1 if c3.checkbox(
                                "aktywne", value=bool(p.active), key=f"pkg_act_{p.id}"
                            ) else 0
                            if st.form_submit_button("💾 Zapisz zmiany"):
                                changed = False
                                if int(new_initial) != int(p.doses_initial):
                                    update_package_initial(int(p.id), int(new_initial))
                                    changed = True
                                if int(new_left) != int(p.doses_left) or int(new_active) != int(p.active):
                                    update_package(
                                        int(p.id), int(new_left), int(new_active), p.opened_at
                                    )
                                    changed = True
                                if changed:
                                    st.rerun()

                st.markdown("**Dodaj zakupione opakowania**")
                c1, c2, c3 = st.columns([1, 2, 2])
                n = c1.number_input("Ile szt.", min_value=1, max_value=10, value=1, step=1, key=f"add_n_{m.id}")
                doses_each = c2.number_input(
                    "Dawek w opakowaniu",
                    min_value=1,
                    max_value=500,
                    value=int(m.doses_per_package),
                    step=1,
                    key=f"add_d_{m.id}",
                )
                pdate = c3.date_input("Data zakupu", value=date.today(), key=f"add_date_{m.id}")
                c4, c5 = st.columns([3, 2])
                brand = c4.text_input(
                    "Marka / zamiennik (opcjonalnie)",
                    value="",
                    placeholder=m.name,
                    help="Zostaw puste jeśli to ten sam lek. Wpisz np. 'Adablix' gdy zamiennik.",
                    key=f"add_brand_{m.id}",
                )
                approx = c5.checkbox(
                    "dawka przybliżona",
                    value=False,
                    help="Dla sprayów/płynów gdzie pompka nie daje stałej dawki.",
                    key=f"add_approx_{m.id}",
                )
                if st.button("Dodaj", key=f"add_btn_{m.id}"):
                    add_packages(int(m.id), int(n), int(doses_each), pdate, brand=brand.strip() or None, approximate=approx)
                    st.success(f"Dodano {n} op. {brand.strip() or m.name}")
                    st.rerun()

    with tab_historia:
        st.subheader("Kalendarz miesięczny")
        today = now.date()
        cc1, cc2 = st.columns(2)
        month_default = today.month - 1
        cal_month = cc1.selectbox(
            "Miesiąc", list(range(1, 13)), index=month_default, key="cal_month",
            format_func=lambda x: ["sty","lut","mar","kwi","maj","cze","lip","sie","wrz","paź","lis","gru"][x-1],
        )
        year_options = list(range(today.year - 2, today.year + 1))
        cal_year = cc2.selectbox(
            "Rok", year_options, index=len(year_options) - 1, key="cal_year"
        )
        from calendar import monthrange
        first_wd, ndays = monthrange(int(cal_year), int(cal_month))
        cal = monthly_adherence(int(cal_year), int(cal_month))
        by_day = {row["day"]: row for row in cal}

        header = st.columns(7)
        for i, lab in enumerate(["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]):
            header[i].markdown(f"<div style='text-align:center;color:#888'><small>{lab}</small></div>", unsafe_allow_html=True)
        weeks = (first_wd + ndays + 6) // 7
        for wk in range(weeks):
            row_cols = st.columns(7)
            for i in range(7):
                pos = wk * 7 + i
                day_num = pos - first_wd + 1
                if 1 <= day_num <= ndays:
                    r = by_day.get(day_num, {"status": "", "taken": 0, "expected": 0})
                    if r["expected"] == 0:
                        body = f"<div style='text-align:center'><b>{day_num}</b><br><small style='color:#bbb'>—</small></div>"
                    else:
                        body = (
                            f"<div style='text-align:center'><b>{day_num}</b> {r['status']}"
                            f"<br><small>{r['taken']}/{r['expected']}</small></div>"
                        )
                    row_cols[i].markdown(body, unsafe_allow_html=True)
                else:
                    row_cols[i].markdown("&nbsp;", unsafe_allow_html=True)
        st.caption("🟢 100% · 🟡 częściowo · 🔴 brak · — brak schematu")

        st.divider()
        st.subheader("Ostatnie przyjęcia")
        with conn() as c:
            rows = c.execute(
                """
                SELECT i.taken_at, m.name AS lek, p.brand, i.doses, i.kind, i.auto, i.given_by
                FROM intakes i JOIN medications m ON m.id = i.med_id
                LEFT JOIN packages p ON p.id = i.package_id
                ORDER BY i.taken_at DESC
                """
            ).fetchall()
        full = pd.DataFrame([dict(r) for r in rows])
        if full.empty:
            st.info("Brak historii.")
        else:
            full["auto"] = full["auto"].map({1: "auto", 0: "ręcznie"})
            full["lek"] = full.apply(
                lambda r: f"{r['lek']} ({r['brand']})" if r.get("brand") and r["brand"] != r["lek"] else r["lek"],
                axis=1,
            )
            full = full.drop(columns=["brand"]).rename(
                columns={
                    "taken_at": "Kiedy",
                    "lek": "Lek",
                    "doses": "Dawki",
                    "kind": "Rodzaj",
                    "auto": "Źródło",
                    "given_by": "Kto",
                }
            )
            st.dataframe(
                full.head(100),
                hide_index=True,
                use_container_width=True,
            )
            csv = full.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 Eksport CSV (cała historia)",
                data=csv,
                file_name=f"leki_historia_{date.today().isoformat()}.csv",
                mime="text/csv",
            )

    with tab_zdrowie:
        today_z = now.date()
        st.subheader("Dzisiejszy wpis")
        with conn() as c:
            m_row = c.execute(
                "SELECT pef, symptoms, note FROM health_logs WHERE log_date = ? AND period = 'morning'",
                (today_z.isoformat(),),
            ).fetchone()
            e_row = c.execute(
                "SELECT pef, symptoms, note FROM health_logs WHERE log_date = ? AND period = 'evening'",
                (today_z.isoformat(),),
            ).fetchone()

        col_m, col_e = st.columns(2)
        with col_m:
            st.markdown("**☀️ Rano**")
            pef_m = st.number_input(
                "PEF (l/min)", min_value=0, max_value=900,
                value=int(m_row["pef"]) if m_row and m_row["pef"] is not None else 0,
                step=10, key="pef_m",
            )
            sym_m = st.slider(
                "Duszność (0-10)", 0, 10,
                value=int(m_row["symptoms"]) if m_row and m_row["symptoms"] is not None else 0,
                key="sym_m",
            )
            note_m = st.text_area(
                "Notatka", value=(m_row["note"] if m_row else "") or "",
                key="note_m", height=80,
            )
            if st.button("💾 Zapisz (rano)", key="save_m", type="primary"):
                log_health(today_z.isoformat(), "morning",
                           pef_m or None, sym_m, note_m.strip() or None)
                st.success("Zapisano.")
                st.rerun()

        with col_e:
            st.markdown("**🌙 Wieczór**")
            pef_e = st.number_input(
                "PEF (l/min)", min_value=0, max_value=900,
                value=int(e_row["pef"]) if e_row and e_row["pef"] is not None else 0,
                step=10, key="pef_e",
            )
            sym_e = st.slider(
                "Duszność (0-10)", 0, 10,
                value=int(e_row["symptoms"]) if e_row and e_row["symptoms"] is not None else 0,
                key="sym_e",
            )
            note_e = st.text_area(
                "Notatka", value=(e_row["note"] if e_row else "") or "",
                key="note_e", height=80,
            )
            if st.button("💾 Zapisz (wieczór)", key="save_e", type="primary"):
                log_health(today_z.isoformat(), "evening",
                           pef_e or None, sym_e, note_e.strip() or None)
                st.success("Zapisano.")
                st.rerun()

        st.divider()
        st.subheader("Ostatnie 30 dni")
        logs = health_logs(30)
        if logs.empty:
            st.info("Brak wpisów.")
        else:
            pef_pivot = logs.pivot_table(
                index="log_date", columns="period", values="pef", aggfunc="first"
            ).sort_index()
            if not pef_pivot.empty:
                st.markdown("**PEF (l/min)**")
                st.line_chart(pef_pivot)
            sym_pivot = logs.pivot_table(
                index="log_date", columns="period", values="symptoms", aggfunc="first"
            ).sort_index()
            if not sym_pivot.empty:
                st.markdown("**Duszność (0-10)**")
                st.line_chart(sym_pivot)
            st.dataframe(
                logs.rename(columns={
                    "log_date": "Data", "period": "Pora",
                    "pef": "PEF", "symptoms": "Duszność", "note": "Notatka",
                }),
                hide_index=True,
                use_container_width=True,
            )
            csv_h = logs.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 Eksport PEF/objawy (CSV)",
                data=csv_h,
                file_name=f"zdrowie_{today_z.isoformat()}.csv",
                mime="text/csv",
            )

        st.divider()
        st.subheader("Ustawienia")
        cur_visit = get_setting("next_doctor_visit") or ""
        try:
            visit_val = date.fromisoformat(cur_visit) if cur_visit else today_z + timedelta(days=30)
        except ValueError:
            visit_val = today_z + timedelta(days=30)
        visit_date_new = st.date_input(
            "📅 Następna wizyta u pediatry",
            value=visit_val, key="visit_date",
        )
        if st.button("Zapisz datę wizyty", key="save_visit"):
            set_setting("next_doctor_visit", visit_date_new.isoformat())
            st.success("Zapisano.")
            st.rerun()

        cur_caregivers = get_setting("caregivers") or "Ja"
        cg_new = st.text_input(
            "👤 Opiekunowie (po przecinku)",
            value=cur_caregivers, key="caregivers_input",
            help="Kto może podawać leki. Lista pojawi się jako selektor na zakładce Dziś.",
        )
        if st.button("Zapisz opiekunów", key="save_cg"):
            set_setting("caregivers", cg_new.strip() or "Ja")
            st.success("Zapisano.")
            st.rerun()

        cur_topic = get_setting("ntfy_topic") or ""
        topic_new = st.text_input(
            "🔔 ntfy.sh — topic powiadomień",
            value=cur_topic, key="ntfy_topic_input",
            help=(
                "Zainstaluj appkę ntfy (iOS/Android/web), subskrybuj ten temat "
                "na https://ntfy.sh/twoj_topic. Wybierz trudną do odgadnięcia nazwę. "
                "Skonfiguruj cron (GitHub Actions) — patrz notify.py."
            ),
        )
        if st.button("Zapisz topic", key="save_topic"):
            set_setting("ntfy_topic", topic_new.strip() or None)
            st.success("Zapisano.")
            st.rerun()


if __name__ == "__main__":
    main()
