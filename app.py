from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

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
            cur = self._raw.execute(sql, params)
        else:
            cur = self._raw.execute(sql)
        return _Cursor(cur)

    def executemany(self, sql, seq):
        self._raw.executemany(sql, seq)

    def executescript(self, script):
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()
        if self._is_remote and hasattr(self._raw, "sync"):
            try:
                self._raw.sync()
            except Exception:
                pass

    def close(self):
        try:
            self._raw.close()
        except Exception:
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


def _open_raw():
    if TURSO_URL:
        import libsql_experimental as libsql
        raw = libsql.connect(database=DB_PATH, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
        try:
            raw.sync()
        except Exception:
            pass
        return raw, True
    raw = sqlite3.connect(DB_PATH)
    raw.execute("PRAGMA foreign_keys = ON")
    return raw, False


@contextmanager
def conn():
    raw, is_remote = _open_raw()
    c = _Conn(raw, is_remote)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO medications(name, form, unit, doses_per_package) VALUES(?,?,?,?)",
                SEED_MEDS,
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
        end = min(at, today) if at else today
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
    with conn() as c:
        last = c.execute(
            "SELECT MAX(taken_at) AS m FROM intakes WHERE kind IN ('scheduled','skipped')"
        ).fetchone()["m"]
        horizon = (
            datetime.fromisoformat(last).date() if last else date.today() - timedelta(days=1)
        )
        for s in _generate_expected_slots(c, horizon, now):
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
            SELECT s.med_id, m.name, m.unit, s.time_of_day, s.dose_amount, s.active_from, s.active_to
            FROM schedules s JOIN medications m ON m.id = s.med_id
            WHERE s.active_from <= ? AND (s.active_to IS NULL OR s.active_to >= ?)
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
                "SELECT id, kind, auto FROM intakes WHERE med_id = ? AND slot_key = ?",
                (r["med_id"], key),
            ).fetchone()
            out.append({
                "med_id": r["med_id"],
                "name": r["name"],
                "unit": r["unit"],
                "time": r["time_of_day"],
                "slot_dt": slot_dt,
                "slot_key": key,
                "dose_amount": r["dose_amount"],
                "intake_id": intake["id"] if intake else None,
                "kind": intake["kind"] if intake else None,
                "auto": bool(intake["auto"]) if intake else False,
            })
        return out


def stock_overview() -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT m.id AS med_id, m.name, m.unit, m.doses_per_package,
                   COALESCE(SUM(CASE WHEN p.active = 1 THEN p.doses_left ELSE 0 END), 0) AS doses_left,
                   SUM(CASE WHEN p.active = 1 THEN 1 ELSE 0 END) AS active_packages
            FROM medications m LEFT JOIN packages p ON p.med_id = m.id
            GROUP BY m.id ORDER BY m.name
            """
        ).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
    return df


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


def take_slot_now(med_id: int, slot_key: str, dose_amount: int, when: datetime) -> None:
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
            INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id, slot_key)
            VALUES(?,?,?,?,0,?,?)
            """,
            (med_id, when.isoformat(timespec="minutes"), dose_amount, "scheduled", pkg_id, slot_key),
        )


def record_ad_hoc(med_id: int, doses: int, when: datetime) -> None:
    with conn() as c:
        pkg_id = _fifo_deduct(c, med_id, doses, when)
        c.execute(
            "INSERT INTO intakes(med_id, taken_at, doses, kind, auto, package_id) VALUES(?,?,?,?,0,?)",
            (med_id, when.isoformat(timespec="minutes"), doses, "ad_hoc", pkg_id),
        )


def add_packages(med_id: int, count: int, doses_each: int, purchased_at: date) -> None:
    with conn() as c:
        for _ in range(count):
            c.execute(
                "INSERT INTO packages(med_id, purchased_at, opened_at, doses_initial, doses_left, active) VALUES(?,?,NULL,?,?,1)",
                (med_id, purchased_at.isoformat(), doses_each, doses_each),
            )


def list_packages(med_id: int) -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT id, purchased_at, opened_at, doses_initial, doses_left, active
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


def list_schedules() -> pd.DataFrame:
    with conn() as c:
        rows = c.execute(
            """
            SELECT s.id, m.name AS lek, s.time_of_day AS godzina, s.dose_amount AS dawka,
                   s.active_from AS od, s.active_to AS do_
            FROM schedules s JOIN medications m ON m.id = s.med_id
            ORDER BY (s.active_to IS NOT NULL), m.name, s.time_of_day
            """
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def all_meds() -> pd.DataFrame:
    with conn() as c:
        return pd.DataFrame([dict(r) for r in c.execute("SELECT id, name, unit, doses_per_package FROM medications ORDER BY name")])


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


def main():
    st.set_page_config(page_title="Leki Tymka", page_icon="💊", layout="centered")
    init_db()
    now = datetime.now().replace(second=0, microsecond=0)

    created = close_pending(now)
    if created:
        st.toast(f"Domknięto {created} zaplanowanych dawek do {now:%H:%M}.")

    st.title("💊 Leki Tymka")
    st.caption(f"Teraz: {now:%Y-%m-%d %H:%M}")

    tab_dzis, tab_apteczka, tab_schemat, tab_historia = st.tabs(
        ["Dziś", "Apteczka", "Schemat", "Historia"]
    )

    with tab_dzis:
        slots = today_slots(now)
        if not slots:
            st.info("Brak zaplanowanych dawek na dziś.")
        for s in slots:
            past = s["slot_dt"] <= now
            cols = st.columns([3, 2, 2, 2])
            if s["kind"] == "scheduled":
                icon = "✅"
            elif s["kind"] == "skipped":
                icon = "⏭️"
            elif past:
                icon = "⏳"
            else:
                icon = "🕒"
            cols[0].write(f"{icon} **{s['name']}** — {s['dose_amount']} {s['unit']}")
            cols[1].write(s["time"])
            if s["kind"] in ("scheduled", "skipped"):
                label = "cofnij"
                if cols[2].button(label, key=f"undo_{s['slot_key']}"):
                    undo_slot(s["med_id"], s["slot_key"])
                    st.rerun()
            else:
                if cols[2].button("wziął", key=f"take_{s['slot_key']}"):
                    take_slot_now(s["med_id"], s["slot_key"], s["dose_amount"], now)
                    st.rerun()
            if s["kind"] != "skipped":
                if cols[3].button("pomiń", key=f"skip_{s['slot_key']}"):
                    skip_slot(s["med_id"], s["slot_key"], s["slot_dt"])
                    st.rerun()

        st.divider()
        st.subheader("Dawka doraźna")
        meds_df = all_meds()
        if not meds_df.empty:
            name = st.selectbox("Lek", meds_df["name"])
            qty = st.number_input("Liczba dawek", min_value=1, max_value=20, value=1, step=1)
            when_date = st.date_input("Data", value=now.date())
            when_time = st.time_input("Godzina", value=now.time().replace(second=0, microsecond=0))
            if st.button("Zarejestruj dawkę doraźną", type="primary"):
                med_id = int(meds_df.loc[meds_df["name"] == name, "id"].iloc[0])
                record_ad_hoc(med_id, int(qty), datetime.combine(when_date, when_time))
                st.success(f"Zapisano: {name} × {qty}")
                st.rerun()

    with tab_apteczka:
        df = stock_overview()
        df["dni_zapasu"] = [days_of_supply(int(r.med_id), int(r.doses_left)) for r in df.itertuples()]
        st.subheader("Stan ogólny")
        low = df[df["dni_zapasu"].fillna(999) < 7]
        if not low.empty:
            st.warning(
                "⚠️ Mało zapasu (< 7 dni): "
                + ", ".join(f"{r.name} ({r.dni_zapasu} d)" for r in low.itertuples())
            )
        st.dataframe(
            df[["name", "doses_left", "active_packages", "dni_zapasu"]].rename(
                columns={
                    "name": "Lek",
                    "doses_left": "Pozostałe dawki",
                    "active_packages": "Aktywne opak.",
                    "dni_zapasu": "Dni zapasu",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )

        st.divider()
        st.subheader("Szczegóły opakowań")
        meds_df = all_meds()
        for m in meds_df.itertuples():
            with st.expander(f"{m.name}"):
                pkgs = list_packages(int(m.id))
                if pkgs.empty:
                    st.caption("Brak opakowań.")
                else:
                    for p in pkgs.itertuples():
                        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
                        c1.write(f"Zakup: {p.purchased_at}")
                        c2.write(f"Otwarte: {p.opened_at or '—'}")
                        new_left = c3.number_input(
                            "Dawki",
                            min_value=0,
                            max_value=int(p.doses_initial),
                            value=int(p.doses_left),
                            step=1,
                            key=f"pkg_left_{p.id}",
                        )
                        new_active = 1 if c4.checkbox("akt.", value=bool(p.active), key=f"pkg_act_{p.id}") else 0
                        if (new_left != p.doses_left) or (new_active != p.active):
                            update_package(int(p.id), int(new_left), int(new_active), p.opened_at)
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
                if st.button("Dodaj", key=f"add_btn_{m.id}"):
                    add_packages(int(m.id), int(n), int(doses_each), pdate)
                    st.success(f"Dodano {n} op. {m.name}")
                    st.rerun()

    with tab_schemat:
        st.subheader("Aktywny i historyczny schemat")
        sdf = list_schedules()
        if sdf.empty:
            st.info("Brak schematów.")
        else:
            active = sdf[sdf["do_"].isna()]
            ended = sdf[sdf["do_"].notna()]
            st.markdown("**Aktywne**")
            st.dataframe(
                active.drop(columns=["id", "do_"]).rename(
                    columns={"lek": "Lek", "godzina": "Godzina", "dawka": "Dawka", "od": "Od"}
                ),
                hide_index=True,
                use_container_width=True,
            )
            if not active.empty:
                to_end = st.selectbox(
                    "Zakończ wpis (wybierz)",
                    options=[None] + active["id"].tolist(),
                    format_func=lambda i: "—" if i is None else f"{active.loc[active.id==i,'lek'].iloc[0]} {active.loc[active.id==i,'godzina'].iloc[0]}",
                )
                if to_end and st.button("Zakończ z dzisiejszą datą"):
                    end_schedule(int(to_end))
                    st.rerun()
            if not ended.empty:
                st.markdown("**Zakończone**")
                st.dataframe(
                    ended.drop(columns=["id"]).rename(
                        columns={"lek": "Lek", "godzina": "Godzina", "dawka": "Dawka", "od": "Od", "do_": "Do"}
                    ),
                    hide_index=True,
                    use_container_width=True,
                )

        st.divider()
        st.subheader("Dodaj wpis do schematu")
        meds_df = all_meds()
        if not meds_df.empty:
            name = st.selectbox("Lek ", meds_df["name"], key="sch_med")
            t = st.time_input("Godzina", value=time(8, 0), key="sch_time")
            amt = st.number_input("Dawka (ile)", min_value=1, max_value=10, value=1, step=1, key="sch_amt")
            if st.button("Dodaj do schematu", type="primary"):
                med_id = int(meds_df.loc[meds_df["name"] == name, "id"].iloc[0])
                add_schedule(med_id, f"{t.hour:02d}:{t.minute:02d}", int(amt))
                st.success("Dodano.")
                st.rerun()

    with tab_historia:
        st.subheader("Ostatnie przyjęcia")
        with conn() as c:
            rows = c.execute(
                """
                SELECT i.taken_at, m.name AS lek, i.doses, i.kind, i.auto
                FROM intakes i JOIN medications m ON m.id = i.med_id
                ORDER BY i.taken_at DESC LIMIT 100
                """
            ).fetchall()
        hdf = pd.DataFrame([dict(r) for r in rows])
        if hdf.empty:
            st.info("Brak historii.")
        else:
            hdf["auto"] = hdf["auto"].map({1: "auto", 0: "ręcznie"})
            st.dataframe(
                hdf.rename(columns={"taken_at": "Kiedy", "lek": "Lek", "doses": "Dawki", "kind": "Rodzaj", "auto": "Źródło"}),
                hide_index=True,
                use_container_width=True,
            )


if __name__ == "__main__":
    main()
