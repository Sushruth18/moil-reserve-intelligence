"""Shift the demo's time-series forward so the newest production record lands on yesterday.

WHY
---
Every date-windowed view ("last N days from now": GET /production?days=30, the dashboard's
Production pulse, the site Output-variance KPI, the Simulator's blast-delay / downtime features, the
equipment-performance windows, the Watcher's recency checks) is anchored to the wall clock, but the
seeded demo data ends in 2026-09. A demo database therefore goes empty as the calendar moves on.
This script moves the whole timeline forward by ONE constant offset, so spacing between records and
the scenario stories (a blast delayed two days after a rain event, a truck down for five days...)
are preserved exactly.

    offset_days = (yesterday, UTC) - (latest production_records.date)

Running it again the same day computes offset 0 and changes nothing; running it later shifts by the
days elapsed since. Pass --offset-days to override.

WHAT IT SHIFTS (Postgres)
-------------------------
  production_records.date            every row; unique (site_id, date, shift) is kept intact by a
                                     two-phase update (see _shift_production_dates)
  blast_events.planned_date/actual_date
  equipment_status_log.changed_at     feeds downtime %, MTBF/MTTR, the Simulator's downtime features
  equipment.last_status_change
  risk_events.detected_at/resolved_at + dates written inside descriptions ("... on 2026-09-22.",
                                     "(24 Sep 03:00-25 Sep 03:00 UTC)")
WHAT IT SHIFTS (Neo4j)
----------------------
  WeatherEvent.start_date/end_date, BlastPlan.scheduled_date, Equipment.last_status_change,
  RiskEvent.detected_at (nodes with external_ref mirror their Postgres row's NEW value + description),
  CalendarDate.date if any exist. The two nodes seed_scenario_a re-dates relative to "today"
  (WeatherEvent we_bal_01, BlastPlan bp_bal_rain_01) are re-derived with seed_scenario_a's own
  arithmetic instead of being shifted, so the Balaghat rain story keeps its shape.

CAP: nothing may land in the future. Shifted values are capped at the end of yesterday (UTC). The
cap also keeps shifted "down" machines out of the Watcher's recent-change window (last 6 h), so a
refresh can never fabricate new equipment_failure risk events. Each table's report shows how many
rows were capped.

WHAT IT DELIBERATELY DOES NOT TOUCH
-----------------------------------
  * created_at / updated_at columns: they are write timestamps, not timeline data. In particular the
    production edit window (PRODUCTION_EDIT_WINDOW_HOURS, measured from production_records.created_at)
    is unaffected: seeded rows stay locked for PATCH, new rows keep their normal 48 h window.
  * data/satellite_daily_features.csv (+ the frontend's static satellite JSON / NDVI tiles): REAL
    observations (meta: "simulated": false). Re-dating them would present real rainfall as having
    fallen on other days. The Simulator already falls back to the Open-Meteo archive for recent days
    the CSV lacks. weather_records is empty in the current databases.
  * shift_plan_entries, site_notes, reserve_zones: write-time stamps only.

SAFETY
------
  * Default is a DRY RUN: read-only transaction / read-only Neo4j session; prints row counts and
    old -> new ranges per table. Nothing is written.
  * --apply writes (Postgres in one transaction; Neo4j in one transaction; Postgres commits last).
  * --rehearse executes every write inside transactions and ROLLS BACK, printing the post-state.
    It refuses to run against anything but localhost / a local Neo4j.

Run from oresight-backend/ with the target in the process environment (never in a committed file):

    python -m scripts.refresh_demo_dates                 # dry run (default)
    python -m scripts.refresh_demo_dates --dry-run       # same, explicit
    python -m scripts.refresh_demo_dates --rehearse      # local only: write, print, roll back
    python -m scripts.refresh_demo_dates --apply         # really write
    python -m scripts.refresh_demo_dates --skip-neo4j
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Some Windows machines intercept TLS (antivirus / proxy); use the OS trust store when available.
try:  # pragma: no cover - environment specific
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

from neo4j import READ_ACCESS, WRITE_ACCESS  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.graph_db import close_graph_driver, init_graph_driver  # noqa: E402
from scripts.seed_scenario_a import BLAST_PLAN_ID as SCENARIO_A_BLAST_ID  # noqa: E402
from scripts.seed_scenario_a import WEATHER_EVENT_ID as SCENARIO_A_WEATHER_ID  # noqa: E402

# Large temporary shift used to step production dates out of the way of the unique constraint.
_PARK_DAYS = 36500
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DAY_MON_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(_MONTHS) + r")\b(?= \d{2}:\d{2})")

# (table, column, kind) shifted with a plain UPDATE ... SET col = LEAST(col + offset, cap)
_PG_SIMPLE = [
    ("blast_events", "planned_date", "date"),
    ("blast_events", "actual_date", "date"),
    ("equipment_status_log", "changed_at", "ts"),
    ("equipment", "last_status_change", "ts"),
]


# --------------------------------------------------------------------------- pure helpers


@dataclass(frozen=True)
class Window:
    today: date
    offset_days: int
    cap_date: date  # yesterday
    cap_ts: datetime  # yesterday 23:59:59 UTC


def make_window(today: date, anchor: date, offset_override: int | None = None) -> Window:
    cap_date = today - timedelta(days=1)
    offset = offset_override if offset_override is not None else (cap_date - anchor).days
    cap_ts = datetime(cap_date.year, cap_date.month, cap_date.day, 23, 59, 59, tzinfo=timezone.utc)
    return Window(today=today, offset_days=offset, cap_date=cap_date, cap_ts=cap_ts)


def shift_date(d: date, w: Window) -> date:
    return min(d + timedelta(days=w.offset_days), w.cap_date)


def shift_ts(t: datetime, w: Window) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return min(t + timedelta(days=w.offset_days), w.cap_ts)


def shift_description(desc: str | None, detected_at: datetime | None, w: Window) -> str | None:
    """Re-date dates written inside a risk-event description.

    ISO dates ("... on 2026-09-22.") shift directly. Forecast windows are written "%d %b %H:%M" with
    no year ("24 Sep 03:00-25 Sep 03:00 UTC"); the year is taken from whichever of the neighbouring
    years puts the date nearest to the event's own detected_at.
    """
    if not desc or w.offset_days == 0:
        return desc

    # Move the text by the SAME number of days the event's own detected_at moved (which may be less than
    # the offset if that timestamp was capped), with no separate cap: a forecast window must keep its
    # position relative to detection, and a cap applied per date could put a window's end before its start.
    delta = (shift_ts(detected_at, w).date() - detected_at.date()).days if detected_at else w.offset_days

    def iso(m: re.Match) -> str:
        return (date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=delta)).isoformat()

    out = _ISO_DATE_RE.sub(iso, desc)
    if detected_at is None:
        return out

    ref = detected_at.date()

    def day_mon(m: re.Match) -> str:
        month = _MONTHS.index(m[2]) + 1
        candidates = []
        for year in (ref.year - 1, ref.year, ref.year + 1):
            try:
                candidates.append(date(year, month, int(m[1])))
            except ValueError:
                continue
        if not candidates:
            return m[0]
        nearest = min(candidates, key=lambda c: abs((c - ref).days))
        new = nearest + timedelta(days=delta)
        return f"{new.day:02d} {_MONTHS[new.month - 1]}"

    return _DAY_MON_RE.sub(day_mon, out)


# --------------------------------------------------------------------------- reporting


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(v)


def _line(name: str, rows: int, old_min, old_max, new_min, new_max, capped: int, note: str = "") -> None:
    rng_old = f"{_fmt(old_min)} .. {_fmt(old_max)}"
    rng_new = f"{_fmt(new_min)} .. {_fmt(new_max)}"
    print(f"  {name:42} {rows:>6}  {rng_old:35} -> {rng_new:35} capped={capped}{('  ' + note) if note else ''}")


def _mask_host(host: str | None) -> str:
    h = host or ""
    return "localhost" if h in ("localhost", "127.0.0.1") else f"<{h[:4]}...{h[-18:]}>"


# --------------------------------------------------------------------------- Postgres


def _pg_aggregate(conn, table: str, col: str, kind: str, w: Window):
    if kind == "date":
        shifted = f"{col} + CAST(:off AS integer)"
        cap = "CAST(:cap AS date)"
        cap_param = w.cap_date
    else:
        shifted = f"{col} + make_interval(days => CAST(:off AS integer))"
        cap = "CAST(:cap AS timestamptz)"
        cap_param = w.cap_ts
    row = conn.execute(
        text(
            f"SELECT count({col}), min({col}), max({col}), "
            f"count(*) FILTER (WHERE {col} IS NOT NULL AND {shifted} > {cap}) FROM {table}"
        ),
        {"off": w.offset_days, "cap": cap_param},
    ).one()
    return row


def _shift_production_dates(conn, w: Window) -> None:
    """production_records has UNIQUE (site_id, date, shift). A single `date = date + N` can hit that
    constraint mid-statement (row D moves onto a not-yet-moved row at D+N; Postgres checks
    non-deferrable unique constraints per row, not at statement end). So: park every row ~100 years
    out of the way, then bring them back at their final date. No row ever collides, and it is
    one transaction, so a failure leaves the table untouched."""
    conn.execute(text("UPDATE production_records SET date = date + CAST(:p AS integer)"), {"p": _PARK_DAYS})
    conn.execute(
        text("UPDATE production_records SET date = date - CAST(:p AS integer) + CAST(:off AS integer)"),
        {"p": _PARK_DAYS, "off": w.offset_days},
    )


def _risk_event_plan(conn, w: Window) -> list[dict]:
    rows = conn.execute(
        text("SELECT id, detected_at, resolved_at, description FROM risk_events ORDER BY id")
    ).all()
    plan = []
    for rid, detected, resolved, desc in rows:
        plan.append(
            {
                "id": rid,
                "old_detected": detected,
                "new_detected": shift_ts(detected, w) if detected else None,
                "old_resolved": resolved,
                "new_resolved": shift_ts(resolved, w) if resolved else None,
                "old_desc": desc,
                "new_desc": shift_description(desc, detected, w),
            }
        )
    return plan


def report_and_maybe_write_postgres(conn, w: Window, write: bool) -> list[dict]:
    """Print the per-table report. If `write`, also execute the updates on `conn` (not committed here).
    Returns the risk-event plan (needed to mirror into Neo4j)."""
    print(f"\nPOSTGRES  (offset {w.offset_days:+d} days; cap {w.cap_ts:%Y-%m-%d %H:%M}Z)"
          + ("  [offset 0: nothing will be written]" if w.offset_days == 0 else ""))

    # production_records
    n, mn, mx = conn.execute(text("SELECT count(*), min(date), max(date) FROM production_records")).one()
    uniq = conn.execute(
        text("SELECT count(*) FROM (SELECT site_id, date, shift FROM production_records GROUP BY 1,2,3 HAVING count(*) > 1) d")
    ).scalar()
    if mx is not None:
        new_mn, new_mx = mn + timedelta(days=w.offset_days), mx + timedelta(days=w.offset_days)
        capped = 0 if new_mx <= w.cap_date else n
        _line("production_records.date", n, mn, mx, new_mn, new_mx, 0, f"two-phase; dup keys now: {uniq}")
        if capped:
            raise SystemExit("production_records would land in the future; offset is wrong, aborting")
    else:
        _line("production_records.date", 0, None, None, None, None, 0)

    # simple tables
    for table, col, kind in _PG_SIMPLE:
        cnt, mn, mx, capped = _pg_aggregate(conn, table, col, kind, w)
        if cnt:
            shift = (lambda v: shift_date(v, w)) if kind == "date" else (lambda v: shift_ts(v, w))
            _line(f"{table}.{col}", cnt, mn, mx, shift(mn), shift(mx), capped)
        else:
            _line(f"{table}.{col}", 0, None, None, None, None, 0)

    # risk_events (python-side so Neo4j can mirror exact values)
    plan = _risk_event_plan(conn, w)
    det = [p for p in plan if p["old_detected"]]
    res = [p for p in plan if p["old_resolved"]]
    cap_det = sum(1 for p in det if p["old_detected"] + timedelta(days=w.offset_days) > w.cap_ts)
    cap_res = sum(1 for p in res if p["old_resolved"] + timedelta(days=w.offset_days) > w.cap_ts)
    if det:
        _line("risk_events.detected_at", len(det), min(p["old_detected"] for p in det), max(p["old_detected"] for p in det),
              min(p["new_detected"] for p in det), max(p["new_detected"] for p in det), cap_det)
    if res:
        _line("risk_events.resolved_at", len(res), min(p["old_resolved"] for p in res), max(p["old_resolved"] for p in res),
              min(p["new_resolved"] for p in res), max(p["new_resolved"] for p in res), cap_res)
    changed_desc = [p for p in plan if p["new_desc"] != p["old_desc"]]
    print(f"  {'risk_events.description (embedded dates)':42} {len(changed_desc):>6}  rows rewritten")
    for p in changed_desc[:6]:
        old, new = p["old_desc"] or "", p["new_desc"] or ""
        i = next((k for k in range(min(len(old), len(new))) if old[k] != new[k]), 0)
        lo = max(0, i - 25)
        print(f"      id={p['id']}: ...{old[lo:i + 45]!r}\n          -> ...{new[lo:i + 45]!r}")

    untouched = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM site_notes), (SELECT count(*) FROM shift_plan_entries), "
            "(SELECT count(*) FROM reserve_zones), (SELECT count(*) FROM weather_records)"
        )
    ).one()
    print(f"  not shifted by design (write-time stamps / real data): site_notes={untouched[0]}, "
          f"shift_plan_entries={untouched[1]}, reserve_zones={untouched[2]}, weather_records={untouched[3]}; "
          "all created_at/updated_at (edit window unaffected)")

    if write and w.offset_days != 0:
        _shift_production_dates(conn, w)
        for table, col, kind in _PG_SIMPLE:
            if kind == "date":
                conn.execute(
                    text(f"UPDATE {table} SET {col} = LEAST({col} + CAST(:off AS integer), CAST(:cap AS date)) WHERE {col} IS NOT NULL"),
                    {"off": w.offset_days, "cap": w.cap_date},
                )
            else:
                conn.execute(
                    text(
                        f"UPDATE {table} SET {col} = LEAST({col} + make_interval(days => CAST(:off AS integer)), "
                        f"CAST(:cap AS timestamptz)) WHERE {col} IS NOT NULL"
                    ),
                    {"off": w.offset_days, "cap": w.cap_ts},
                )
        for p in plan:
            conn.execute(
                text("UPDATE risk_events SET detected_at = :d, resolved_at = :r, description = :t WHERE id = :id"),
                {"d": p["new_detected"], "r": p["new_resolved"], "t": p["new_desc"], "id": p["id"]},
            )
    return plan


# --------------------------------------------------------------------------- Neo4j


def _nodes(tx, label: str, prop: str) -> list[dict]:
    return tx.run(
        f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
        f"RETURN elementId(n) AS eid, n.id AS id, n.external_ref AS ref, n.{prop} AS v"
    ).data()


def _native(v):
    return v.to_native() if hasattr(v, "to_native") else v


def plan_neo4j(tx, w: Window, pg_plan: list[dict]) -> list[dict]:
    """Return [{label, prop, kind, eid, id, old, new, note}] without writing."""
    by_ref = {str(p["id"]): p for p in pg_plan}
    steps: list[dict] = []

    def add(label, prop, kind, rec, new, note=""):
        old = _native(rec["v"])
        # capped: the plain shift would have landed after the cap (only meaningful for plain-shifted rows)
        limit = w.cap_date if kind == "date" else w.cap_ts
        capped = (not note) and (old + timedelta(days=w.offset_days) > limit)
        steps.append({"label": label, "prop": prop, "kind": kind, "eid": rec["eid"], "id": rec["id"],
                      "old": old, "new": new, "note": note, "capped": bool(capped)})

    start = w.today - timedelta(days=2)  # seed_scenario_a: window starts two days ago, lasts 5 days
    for label, prop, kind in [
        ("WeatherEvent", "start_date", "date"), ("WeatherEvent", "end_date", "date"),
        ("BlastPlan", "scheduled_date", "date"), ("CalendarDate", "date", "date"),
        ("Equipment", "last_status_change", "ts"), ("RiskEvent", "detected_at", "ts"),
    ]:
        for rec in _nodes(tx, label, prop):
            old = _native(rec["v"])
            if label == "WeatherEvent" and rec["id"] == SCENARIO_A_WEATHER_ID:
                add(label, prop, kind, rec, start if prop == "start_date" else start + timedelta(days=5),
                    "re-derived like seed_scenario_a")
            elif label == "BlastPlan" and rec["id"] == SCENARIO_A_BLAST_ID:
                add(label, prop, kind, rec, start + timedelta(days=3), "re-derived like seed_scenario_a")
            elif label == "RiskEvent" and rec["ref"] is not None and str(rec["ref"]) in by_ref:
                add(label, prop, kind, rec, by_ref[str(rec["ref"])]["new_detected"], "mirrors Postgres risk_events")
            elif kind == "date":
                add(label, prop, kind, rec, shift_date(old, w))
            else:
                add(label, prop, kind, rec, shift_ts(old, w))
    # RiskEvent.description mirrors Postgres too
    for rec in tx.run("MATCH (r:RiskEvent) WHERE r.external_ref IS NOT NULL RETURN elementId(r) AS eid, r.id AS id, r.external_ref AS ref, r.description AS v").data():
        p = by_ref.get(str(rec["ref"]))
        if p and p["new_desc"] != rec["v"] and p["new_desc"] is not None:
            steps.append({"label": "RiskEvent", "prop": "description", "kind": "str", "eid": rec["eid"], "id": rec["id"],
                          "old": rec["v"], "new": p["new_desc"], "note": "mirrors Postgres"})
    return steps


def report_neo4j(steps: list[dict], w: Window) -> None:
    print(f"\nNEO4J  (offset {w.offset_days:+d} days)")
    groups: dict[tuple[str, str], list[dict]] = {}
    for s in steps:
        groups.setdefault((s["label"], s["prop"]), []).append(s)
    if not groups:
        print("  (no date properties found)")
    for (label, prop), items in sorted(groups.items()):
        if prop == "description":
            print(f"  {label + '.' + prop:42} {len(items):>6}  descriptions rewritten (mirrors Postgres)")
            continue
        olds = [i["old"] for i in items]
        news = [i["new"] for i in items]
        capped = sum(1 for i in items if i.get("capped"))
        notes = sorted({i["note"] for i in items if i["note"]})
        _line(f"{label}.{prop}", len(items), min(olds), max(olds), min(news), max(news), capped, "; ".join(notes))
        for i in items:
            if i["note"].startswith("re-derived"):
                print(f"      {i['id']}: {_fmt(i['old'])} -> {_fmt(i['new'])}  ({i['note']})")


def write_neo4j(tx, steps: list[dict]) -> None:
    for kind, fn in (("date", "date"), ("ts", "datetime"), ("str", None)):
        groups: dict[tuple[str, str], list[dict]] = {}
        for s in steps:
            if s["kind"] == kind:
                groups.setdefault((s["label"], s["prop"]), []).append(s)
        for (label, prop), items in groups.items():
            rows = [{"eid": i["eid"], "v": (i["new"].isoformat() if fn else i["new"])} for i in items]
            expr = f"{fn}(r.v)" if fn else "r.v"
            tx.run(
                f"UNWIND $rows AS r MATCH (n:{label}) WHERE elementId(n) = r.eid SET n.{prop} = {expr}",
                rows=rows,
            )


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only report (the default)")
    mode.add_argument("--apply", action="store_true", help="write and commit")
    mode.add_argument("--rehearse", action="store_true", help="write inside transactions, print, ROLL BACK (local targets only)")
    ap.add_argument("--offset-days", type=int, default=None, help="override the computed offset")
    ap.add_argument("--skip-neo4j", action="store_true")
    args = ap.parse_args()
    write = args.apply or args.rehearse

    # Probing optional labels/properties (CalendarDate, external_ref) is expected; silence the server's
    # "unknown label/property" notifications so the report stays readable.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    settings = get_settings()
    pg = urlsplit(settings.DATABASE_URL)
    nj = urlsplit(settings.NEO4J_URI)
    pg_local = (pg.hostname or "") in ("localhost", "127.0.0.1")
    nj_local = (nj.hostname or "") in ("localhost", "127.0.0.1", "neo4j")
    mode_name = "APPLY" if args.apply else "REHEARSE (rolled back)" if args.rehearse else "DRY RUN (read-only)"
    print(f"mode: {mode_name}")
    print(f"postgres: host={_mask_host(pg.hostname)} db={pg.path.lstrip('/')}")
    print(f"neo4j:    host={_mask_host(nj.hostname)} scheme={nj.scheme}{'  (skipped)' if args.skip_neo4j else ''}")
    if args.rehearse and not (pg_local and (nj_local or args.skip_neo4j)):
        print("REFUSING: --rehearse performs real writes inside a transaction; local targets only.")
        return 2

    today = datetime.now(timezone.utc).date()
    driver = None if args.skip_neo4j else init_graph_driver()
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                if not write:
                    conn.execute(text("SET TRANSACTION READ ONLY"))  # server-enforced: any write would error
                conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                anchor = conn.execute(text("SELECT max(date) FROM production_records")).scalar()
                if anchor is None:
                    print("production_records is empty; nothing to anchor on. Pass --offset-days to force.")
                    if args.offset_days is None:
                        return 1
                    anchor = today - timedelta(days=1)
                w = make_window(today, anchor, args.offset_days)
                print(f"today (UTC) {today}; latest production record {anchor}; target {w.cap_date}; offset {w.offset_days:+d} days")
                if w.offset_days < 0 and args.offset_days is None:
                    print("Latest production record is in the future; refusing to shift backwards without --offset-days.")
                    return 1

                pg_plan = report_and_maybe_write_postgres(conn, w, write)

                steps: list[dict] = []
                if driver is not None:
                    with driver.session(default_access_mode=WRITE_ACCESS if write else READ_ACCESS) as session:
                        ntx = session.begin_transaction()
                        try:
                            steps = plan_neo4j(ntx, w, pg_plan)
                            report_neo4j(steps, w)
                            if write and w.offset_days != 0:
                                write_neo4j(ntx, steps)
                            if args.apply:
                                ntx.commit()
                            else:
                                ntx.rollback()
                        except Exception:
                            ntx.rollback()
                            raise

                if write:
                    post = conn.execute(text("SELECT min(date), max(date), count(*) FROM production_records")).one()
                    print(f"\npost-state production_records: {post[0]} .. {post[1]} ({post[2]} rows)")
                if args.apply:
                    tx.commit()
                    print("\nAPPLIED and committed.")
                else:
                    tx.rollback()
                    print("\n" + ("Rehearsal rolled back; nothing persisted." if args.rehearse else "Dry run complete; nothing was written."))
            except Exception:
                tx.rollback()
                raise
    finally:
        if driver is not None:
            close_graph_driver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
