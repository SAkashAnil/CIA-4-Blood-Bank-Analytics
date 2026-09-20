#!/usr/bin/env python3
"""
app.py  -  Backend for the Blood Bank Inventory & Expiry Intelligence dashboard (CIA-4, Stage 2)

    Neon PostgreSQL (analytics.* views)  ->  Flask REST API  ->  static HTML/CSS/JS dashboard

Security
  * DATABASE_URL is read from .env on the SERVER only. It is never sent to the browser,
    never included in an API response, and never written to the log.
  * Only static/ is served to the browser (.env lives one level above it).
  * The API only runs SELECT statements, all with bound parameters (no SQL injection).
  * Query-string filters are validated against whitelists before use.

Run:  python app.py        then open  http://localhost:5000
"""
import functools
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import psycopg2
from flask import Flask, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from psycopg2 import extras, pool
from werkzeug.exceptions import HTTPException

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bloodbank")


# --------------------------------------------------------------------------------------
# Configuration (.env)
# --------------------------------------------------------------------------------------
def load_env():
    """Tiny .env reader: KEY=VALUE lines, real environment variables win."""
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env()
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", "5000"))
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "5"))
MAX_CONNECTIONS = 6


# --------------------------------------------------------------------------------------
# Flask app + JSON encoding (Decimal -> number, datetime -> ISO string)
# --------------------------------------------------------------------------------------
class Provider(DefaultJSONProvider):
    sort_keys = False

    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.json = Provider(app)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0     # always serve the latest HTML/JS/CSS while developing


@app.after_request
def no_store_for_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------------------
# Errors -> clean JSON (never leaks connection details)
# --------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message, code="bad_request"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def err(status, code, message):
    return jsonify({"error": code, "message": message}), status


@app.errorhandler(ApiError)
def _api_error(e):
    return err(e.status, e.code, e.message)


@app.errorhandler(psycopg2.OperationalError)
def _db_down(e):
    log.error("Database unavailable: %s", str(e).strip().splitlines()[0] if str(e).strip() else "unknown")
    return err(503, "database_unavailable",
               "Could not reach the Neon database. If it was idle it may be waking up - wait a few seconds and press Refresh.")


@app.errorhandler(psycopg2.DataError)
def _db_data(e):
    log.warning("Bad value in request: %s", str(e).strip().splitlines()[0])
    return err(400, "bad_parameter", "One of the request parameters has an invalid value.")


@app.errorhandler(psycopg2.Error)
def _db_error(e):
    log.error("Query failed: %s", str(e).strip().splitlines()[0] if str(e).strip() else "unknown")
    return err(500, "query_failed",
               "A database query failed. Check that 02_transform_views.sql has been run in Neon. Details are in the server log.")


@app.errorhandler(HTTPException)
def _http(e):
    if request.path.startswith("/api/"):
        return err(e.code, e.name.lower().replace(" ", "_"), e.description)
    return e


@app.errorhandler(Exception)
def _unexpected(e):
    log.exception("Unexpected error")
    return err(500, "server_error", "Unexpected server error. Details are in the server log.")


# --------------------------------------------------------------------------------------
# Database access: small connection pool + one retry (Neon closes idle connections)
# --------------------------------------------------------------------------------------
_pool = None
_pool_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


def get_pool():
    global _pool
    if not DATABASE_URL:
        raise ApiError(500, "not_configured", "DATABASE_URL is missing. Create a .env file next to app.py.")
    with _pool_lock:
        if _pool is None:
            _pool = pool.ThreadedConnectionPool(
                1, MAX_CONNECTIONS, DATABASE_URL, connect_timeout=20,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                application_name="bloodbank-dashboard")
        return _pool


def run_db(fn):
    """Call fn(cursor) on a pooled connection. Retries once if the connection was dropped."""
    last = None
    for _ in range(2):
        with _slots:
            p = get_pool()
            conn = p.getconn()
            broken = False
            try:
                conn.autocommit = True                     # reads only; no open transactions
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    return fn(cur)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                broken, last = True, e
            finally:
                p.putconn(conn, close=broken)
    raise last


def query(cur, sql, params=None):
    cur.execute(sql, params or {})
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------------------
# Request parameter validation
# --------------------------------------------------------------------------------------
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
COMPONENTS = ["RBC", "PLATELETS", "PLASMA"]
URGENCIES = ["EMERGENCY", "URGENT", "ROUTINE"]
STATUSES = ["FULFILLED", "PARTIAL", "UNFULFILLED"]


def arg(name):
    v = request.args.get(name)
    return v.strip() if v and v.strip() else None


def choice(name, allowed, fix_plus=False):
    v = arg(name)
    if v is None:
        return None
    if fix_plus:
        v = v.replace(" ", "+")            # a raw '+' in a URL arrives as a space
    v = v.upper()
    if v not in allowed:
        raise ApiError(400, f"Invalid {name} '{v}'. Allowed: {', '.join(allowed)}")
    return v


def integer(name, default, lo, hi):
    v = arg(name)
    if v is None:
        return default
    try:
        n = int(v)
    except ValueError:
        raise ApiError(400, f"{name} must be a whole number")
    return max(lo, min(hi, n))


def filters():
    """Common filters shared by most endpoints."""
    branch = arg("branch")
    if branch:
        branch = branch.upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,20}", branch):
            raise ApiError(400, "Invalid branch id")
    return {
        "branch": branch,
        "bg": choice("blood_group", BLOOD_GROUPS, fix_plus=True),
        "comp": choice("component", COMPONENTS),
        "days": integer("days", 30, 1, 365),
    }


def where(alias="", branch_col="branch_id"):
    """SQL fragment for the optional branch / blood group / component filters (params bound, never concatenated)."""
    p = alias + "." if alias else ""
    return (f"(%(branch)s::text is null or {p}{branch_col} = %(branch)s) "
            f"and (%(bg)s::text is null or {p}blood_group = %(bg)s) "
            f"and (%(comp)s::text is null or {p}component = %(comp)s)")


def pct(a, b):
    """Percentage rounded half-up to 1 decimal (matches SQL round())."""
    if not b:
        return None
    return float((Decimal(100) * Decimal(a) / Decimal(b)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def ist_today():
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


# --------------------------------------------------------------------------------------
# Tiny response cache (protects the free Neon instance from repeated identical queries)
# --------------------------------------------------------------------------------------
_cache, _cache_lock = {}, threading.Lock()


def endpoint(rule, ttl=None):
    """Register a JSON endpoint with a short server-side cache. ?refresh=1 bypasses the cache."""
    ttl = CACHE_TTL if ttl is None else ttl

    def deco(fn):
        @functools.wraps(fn)
        def view():
            key = rule + "?" + urlencode(sorted((k, v) for k, v in request.args.items() if k not in ("refresh", "_")))
            if request.args.get("refresh") != "1":
                with _cache_lock:
                    hit = _cache.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    return jsonify(hit[1])
            payload = fn()
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            with _cache_lock:
                if len(_cache) > 300:
                    _cache.clear()
                _cache[key] = (time.monotonic(), payload)
            return jsonify(payload)

        app.add_url_rule(rule, endpoint=fn.__name__, view_func=view)
        return fn
    return deco


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# --------------------------------------------------------------------------------------
# API: health
# --------------------------------------------------------------------------------------
@endpoint("/api/health")
def health():
    def go(cur):
        return query(cur, """
            select now()                                        as db_time,
                   (select count(*) from raw.donors)            as donor_records,
                   (select count(*) from raw.donations)         as units,
                   (select count(*) from raw.hospital_requests) as requests,
                   (select max(collected_at) from raw.donations)         as last_collection,
                   (select max(requested_at) from raw.hospital_requests) as last_request,
                   greatest((select max(collected_at) from raw.donations),
                            (select max(event_time) from raw.stock_events)) as last_activity""")[0]
    return {"status": "ok", **run_db(go)}


# --------------------------------------------------------------------------------------
# API: KPIs  (same definitions as analytics.vw_kpi_summary, but filterable)
# --------------------------------------------------------------------------------------
@endpoint("/api/kpis")
def kpis():
    f = filters()

    def go(cur):
        days = "now() - make_interval(days => %(days)s)"
        u = query(cur, f"""
            select count(*) filter (where status = 'IN_STOCK')                                    as units_in_stock,
                   count(*) filter (where collected_at >= {days})                                 as collected,
                   count(*) filter (where status = 'EXPIRED'       and status_time >= {days})     as expired,
                   count(*) filter (where status = 'DISCARDED_TTI' and status_time >= {days})     as tti_discarded
            from analytics.clean_units
            where {where()}""", f)[0]
        risk = query(cur, f"""
            select coalesce(sum(units_at_risk), 0)::int as units_at_risk
            from analytics.vw_inventory_status
            where {where()}""", f)[0]
        r = query(cur, f"""
            select count(*)                                                        as requests,
                   count(*) filter (where status = 'FULFILLED')                    as fulfilled,
                   count(*) filter (where status in ('UNFULFILLED', 'PARTIAL'))    as unmet_requests,
                   count(*) filter (where urgency = 'EMERGENCY')                   as emergency_requests,
                   count(*) filter (where cross_branch)                            as cross_branch_requests,
                   round(avg(response_minutes), 1)                                 as avg_response_min,
                   round(avg(response_minutes) filter (where urgency = 'EMERGENCY'), 1) as avg_emergency_response_min
            from analytics.clean_requests
            where requested_at >= {days} and {where()}""", f)[0]
        d = query(cur, f"""
            select count(*) as unique_donors, count(*) filter (where n >= 2) as repeat_donors
            from (select master_donor_id, count(distinct donation_id) as n
                  from analytics.clean_units where {where()} group by 1) t""", f)[0]
        return u, risk, r, d

    u, risk, r, d = run_db(go)
    return {
        "period_days": f["days"],
        "filters": {"branch": f["branch"], "blood_group": f["bg"], "component": f["comp"]},
        "units_in_stock": u["units_in_stock"],
        "units_at_risk": risk["units_at_risk"],
        "at_risk_share_pct": pct(risk["units_at_risk"], u["units_in_stock"]),
        "collected": u["collected"],
        "expired": u["expired"],
        "tti_discarded": u["tti_discarded"],
        "wastage_rate_pct": pct(u["expired"], u["collected"]),
        "tti_discard_rate_pct": pct(u["tti_discarded"], u["collected"]),
        "requests": r["requests"],
        "fulfilled": r["fulfilled"],
        "unmet_requests": r["unmet_requests"],
        "fulfilment_rate_pct": pct(r["fulfilled"], r["requests"]),
        "emergency_requests": r["emergency_requests"],
        "emergency_share_pct": pct(r["emergency_requests"], r["requests"]),
        "cross_branch_pct": pct(r["cross_branch_requests"], r["requests"]),
        "avg_response_min": r["avg_response_min"],
        "avg_emergency_response_min": r["avg_emergency_response_min"],
        "unique_donors": d["unique_donors"],
        "repeat_donors": d["repeat_donors"],
        "repeat_donor_rate_pct": pct(d["repeat_donors"], d["unique_donors"]),
    }


# --------------------------------------------------------------------------------------
# API: inventory (analytics.vw_inventory_status + analytics.vw_stock_cover)
# --------------------------------------------------------------------------------------
@endpoint("/api/inventory")
def inventory():
    f = filters()
    f["cover_comp"] = f["comp"] or "RBC"          # the heat-map shows one component at a time

    def go(cur):
        rows = query(cur, f"""
            select branch_id, branch_name, blood_group, component,
                   units_in_stock::int as units_in_stock, units_at_risk::int as units_at_risk,
                   next_expiry, min_hours_to_expiry
            from analytics.vw_inventory_status
            where {where()}
            order by branch_id, blood_group, component""", f)
        cover = query(cur, """
            select branch_id, branch_name, blood_group, component, units_in_stock::int as units_in_stock,
                   avg_daily_demand, cover_days, target_cover_days, stock_status
            from analytics.vw_stock_cover
            where component = %(cover_comp)s
              and (%(branch)s::text is null or branch_id = %(branch)s)
              and (%(bg)s::text is null or blood_group = %(bg)s)
            order by branch_id, blood_group""", f)
        return rows, cover

    rows, cover = run_db(go)

    by_bg, by_branch = {}, {}
    for r in rows:
        for key, bucket, label in ((r["blood_group"], by_bg, "blood_group"), (r["branch_id"], by_branch, "branch_id")):
            b = bucket.setdefault(key, {label: key, "RBC": 0, "PLATELETS": 0, "PLASMA": 0, "total": 0, "at_risk": 0})
            b[r["component"]] = b.get(r["component"], 0) + r["units_in_stock"]
            b["total"] += r["units_in_stock"]
            b["at_risk"] += r["units_at_risk"]
            if label == "branch_id":
                b["branch_name"] = r["branch_name"]
    return {
        "totals": {"units_in_stock": sum(r["units_in_stock"] for r in rows),
                   "units_at_risk": sum(r["units_at_risk"] for r in rows)},
        "by_blood_group": [by_bg[g] for g in BLOOD_GROUPS if g in by_bg],
        "by_branch": sorted(by_branch.values(), key=lambda b: b["branch_id"]),
        "rows": rows,
        "cover": cover,
        "cover_component": f["cover_comp"],
        "cover_window_days": 30,
    }


# --------------------------------------------------------------------------------------
# API: expiry risk (analytics.vw_expiry_buckets + analytics.vw_inventory_status)
# --------------------------------------------------------------------------------------
@endpoint("/api/expiry-risk")
def expiry_risk():
    f = filters()
    limit = integer("limit", 100, 1, 500)

    def go(cur):
        buckets = query(cur, f"""
            select expiry_bucket, component, sum(units)::int as units
            from analytics.vw_expiry_buckets
            where {where()}
            group by 1, 2 order by 1, 2""", f)
        at_risk = query(cur, f"""
            select branch_id, branch_name, blood_group, component,
                   units_in_stock::int as units_in_stock, units_at_risk::int as units_at_risk,
                   next_expiry, min_hours_to_expiry
            from analytics.vw_inventory_status
            where units_at_risk > 0 and {where()}
            order by min_hours_to_expiry asc, units_at_risk desc""", f)
        return buckets, at_risk

    buckets, at_risk = run_db(go)
    pivot = {}
    for b in buckets:
        order, label = b["expiry_bucket"].split(". ", 1)
        row = pivot.setdefault(order, {"order": int(order), "bucket": label, "RBC": 0, "PLATELETS": 0, "PLASMA": 0, "total": 0})
        row[b["component"]] = row.get(b["component"], 0) + b["units"]
        row["total"] += b["units"]
    ordered = [pivot[k] for k in sorted(pivot)]
    return {
        "summary": {
            "units_at_risk": sum(r["units_at_risk"] for r in at_risk),
            "risk_lines": len(at_risk),
            "expiring_within_24h": next((r["total"] for r in ordered if r["order"] == 1), 0),
        },
        "buckets": ordered,
        "at_risk": at_risk[:limit],
    }


# --------------------------------------------------------------------------------------
# API: hospital requests / demand (analytics.clean_requests)
# --------------------------------------------------------------------------------------
@endpoint("/api/requests")
def requests_summary():
    f = filters()
    f["urgency"] = choice("urgency", URGENCIES)
    f["status"] = choice("status", STATUSES)
    f["limit"] = integer("limit", 25, 1, 200)
    period = "requested_at >= now() - make_interval(days => %(days)s)"

    def go(cur):
        by_urgency = query(cur, f"""
            select urgency, count(*)::int as requests,
                   count(*) filter (where status = 'FULFILLED')::int   as fulfilled,
                   count(*) filter (where status = 'PARTIAL')::int     as partial,
                   count(*) filter (where status = 'UNFULFILLED')::int as unfulfilled,
                   count(*) filter (where cross_branch)::int           as cross_branch,
                   coalesce(sum(response_minutes), 0)                  as resp_sum,
                   count(response_minutes)::int                        as resp_n
            from analytics.clean_requests
            where {period} and {where()}
            group by urgency""", f)
        by_bg = query(cur, f"""
            select blood_group, count(*)::int as requests,
                   count(*) filter (where status = 'FULFILLED')::int   as fulfilled,
                   count(*) filter (where status = 'PARTIAL')::int     as partial,
                   count(*) filter (where status = 'UNFULFILLED')::int as unfulfilled
            from analytics.clean_requests
            where {period} and {where()}
            group by blood_group""", f)
        daily = query(cur, f"""
            select (requested_at at time zone 'Asia/Kolkata')::date as day,
                   count(*)::int as requests,
                   count(*) filter (where urgency = 'EMERGENCY')::int as emergency,
                   count(*) filter (where status in ('UNFULFILLED', 'PARTIAL'))::int as unmet
            from analytics.clean_requests
            where (requested_at at time zone 'Asia/Kolkata')::date > (now() at time zone 'Asia/Kolkata')::date - %(days)s
              and {where()}
            group by 1 order by 1""", f)
        recent = query(cur, f"""
            select r.request_id, r.hospital_name, r.branch_id, b.branch_name, r.blood_group, r.component,
                   r.units_requested, r.units_fulfilled, r.urgency, r.status,
                   r.requested_at, r.fulfilled_at, r.response_minutes, r.cross_branch,
                   r.fulfilled_by_branch_id, fb.branch_name as fulfilled_by_branch
            from analytics.clean_requests r
            join raw.branches b       on b.branch_id  = r.branch_id
            left join raw.branches fb on fb.branch_id = r.fulfilled_by_branch_id
            where r.{period} and {where('r')}
              and (%(urgency)s::text is null or r.urgency = %(urgency)s)
              and (%(status)s::text is null  or r.status  = %(status)s)
            order by r.requested_at desc
            limit %(limit)s""", f)
        return by_urgency, by_bg, daily, recent

    by_urgency, by_bg, daily, recent = run_db(go)

    tot = lambda k: sum(r[k] for r in by_urgency)
    emergency = next((r for r in by_urgency if r["urgency"] == "EMERGENCY"), None)
    summary = {
        "requests": tot("requests"), "fulfilled": tot("fulfilled"),
        "partial": tot("partial"), "unfulfilled": tot("unfulfilled"),
        "unmet": tot("partial") + tot("unfulfilled"),
        "emergency": emergency["requests"] if emergency else 0,
        "fulfilment_rate_pct": pct(tot("fulfilled"), tot("requests")),
        "cross_branch_pct": pct(tot("cross_branch"), tot("requests")),
        "avg_response_min": round(float(tot("resp_sum")) / tot("resp_n"), 1) if tot("resp_n") else None,
        "avg_emergency_response_min": round(float(emergency["resp_sum"]) / emergency["resp_n"], 1)
                                      if emergency and emergency["resp_n"] else None,
    }
    for r in by_urgency:
        r["avg_response_min"] = round(float(r.pop("resp_sum")) / r["resp_n"], 1) if r["resp_n"] else None
        r["fulfilment_rate_pct"] = pct(r["fulfilled"], r["requests"])
    by_urgency.sort(key=lambda r: URGENCIES.index(r["urgency"]) if r["urgency"] in URGENCIES else 9)
    by_bg.sort(key=lambda r: BLOOD_GROUPS.index(r["blood_group"]) if r["blood_group"] in BLOOD_GROUPS else 9)

    # fill days with no requests so the chart has a continuous axis
    have = {d["day"]: d for d in daily}
    today = ist_today()
    filled = []
    for i in range(f["days"] - 1, -1, -1):
        day = today - timedelta(days=i)
        filled.append(have.get(day, {"day": day, "requests": 0, "emergency": 0, "unmet": 0}))
    return {"period_days": f["days"], "summary": summary, "by_urgency": by_urgency,
            "by_blood_group": by_bg, "daily": filled, "recent": recent}


# --------------------------------------------------------------------------------------
# API: branches (scorecard + filter list)
# --------------------------------------------------------------------------------------
@endpoint("/api/branches")
def branches():
    f = filters()

    def go(cur):
        return query(cur, """
            select b.branch_id, b.branch_name, b.city, b.branch_type,
                   coalesce(i.units_in_stock, 0)::int as units_in_stock,
                   coalesce(i.units_at_risk, 0)::int  as units_at_risk,
                   coalesce(r.requests, 0)::int       as requests,
                   coalesce(r.emergency, 0)::int      as emergency_requests,
                   coalesce(r.unmet, 0)::int          as unmet_requests,
                   coalesce(r.fulfilled, 0)::int      as fulfilled,
                   r.avg_response_min
            from raw.branches b
            left join (select branch_id, sum(units_in_stock) as units_in_stock, sum(units_at_risk) as units_at_risk
                       from analytics.vw_inventory_status
                       where (%(bg)s::text is null or blood_group = %(bg)s)
                         and (%(comp)s::text is null or component = %(comp)s)
                       group by branch_id) i on i.branch_id = b.branch_id
            left join (select branch_id, count(*) as requests,
                              count(*) filter (where urgency = 'EMERGENCY') as emergency,
                              count(*) filter (where status in ('UNFULFILLED', 'PARTIAL')) as unmet,
                              count(*) filter (where status = 'FULFILLED') as fulfilled,
                              round(avg(response_minutes), 1) as avg_response_min
                       from analytics.clean_requests
                       where requested_at >= now() - make_interval(days => %(days)s)
                         and (%(bg)s::text is null or blood_group = %(bg)s)
                         and (%(comp)s::text is null or component = %(comp)s)
                       group by branch_id) r on r.branch_id = b.branch_id
            order by b.branch_id""", f)

    rows = run_db(go)
    for r in rows:
        r["fulfilment_rate_pct"] = pct(r["fulfilled"], r["requests"])
        r["at_risk_share_pct"] = pct(r["units_at_risk"], r["units_in_stock"])
    return {"period_days": f["days"], "branches": rows}


# --------------------------------------------------------------------------------------
# API: transfer recommendations (analytics.vw_transfer_recommendations)
# --------------------------------------------------------------------------------------
@endpoint("/api/transfer-recommendations")
def transfer_recommendations():
    f = filters()
    limit = integer("limit", 50, 1, 500)

    def go(cur):
        return query(cur, """
            select from_branch_id, from_branch, to_branch_id, to_branch, blood_group, component,
                   surplus_units::int as surplus_units, need_units::int as need_units,
                   suggested_units, hours_to_expiry, receiver_stock::int as receiver_stock, receiver_cover_days
            from analytics.vw_transfer_recommendations
            where (%(bg)s::text is null or blood_group = %(bg)s)
              and (%(comp)s::text is null or component = %(comp)s)
              and (%(branch)s::text is null or from_branch_id = %(branch)s or to_branch_id = %(branch)s)
            order by hours_to_expiry asc nulls last, need_units desc""", f)

    rows = run_db(go)

    # The view lists every sensible sender/receiver pair on its own. Several senders may offer the same
    # receiver's need, so a greedy pass (most urgent expiry first) turns the options into a plan
    # in which no surplus unit and no receiver need is counted twice.
    surplus_left, need_left = {}, {}
    for r in rows:
        surplus_left.setdefault((r["from_branch_id"], r["blood_group"], r["component"]), r["surplus_units"])
        need_left.setdefault((r["to_branch_id"], r["blood_group"], r["component"]), r["need_units"])
    for r in rows:
        s = (r["from_branch_id"], r["blood_group"], r["component"])
        n = (r["to_branch_id"], r["blood_group"], r["component"])
        move = max(0, min(surplus_left[s], need_left[n], r["suggested_units"]))
        surplus_left[s] -= move
        need_left[n] -= move
        r["plan_units"] = move
    plan = [r for r in rows if r["plan_units"] > 0]
    return {
        "summary": {"options": len(rows), "plan_lines": len(plan),
                    "plan_units": sum(r["plan_units"] for r in plan)},
        "recommendations": rows[:limit],
    }


# --------------------------------------------------------------------------------------
# API: live feed (analytics.vw_live_feed)
# --------------------------------------------------------------------------------------
@endpoint("/api/live-feed", ttl=2)
def live_feed():
    branch = arg("branch")
    if branch:
        branch = branch.upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,20}", branch):
            raise ApiError(400, "Invalid branch id")
    since = arg("since")
    if since:
        since = since.replace(" ", "+")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ][0-9:.]+(Z|[+-]\d{2}:?\d{2})?", since):
            raise ApiError(400, "since must be an ISO timestamp, e.g. 2026-09-19T10:00:00+00:00")
    p = {"branch": branch, "since": since, "limit": integer("limit", 30, 1, 100)}

    def go(cur):
        return query(cur, """
            select f.event_time, f.event_type, f.branch_id, b.branch_name, f.blood_group, f.component, f.detail
            from analytics.vw_live_feed f
            left join raw.branches b on b.branch_id = f.branch_id
            where (%(branch)s::text is null or f.branch_id = %(branch)s)
              and (%(since)s::timestamptz is null or f.event_time > %(since)s::timestamptz)
            order by f.event_time desc
            limit %(limit)s""", p)

    events = run_db(go)
    return {"count": len(events), "events": events}


# --------------------------------------------------------------------------------------
# API: daily flow trend (analytics.vw_daily_flow)
# --------------------------------------------------------------------------------------
@endpoint("/api/trends")
def trends():
    f = filters()

    def go(cur):
        return query(cur, f"""
            select day, flow, sum(units)::int as units
            from analytics.vw_daily_flow
            where day > (now() at time zone 'Asia/Kolkata')::date - %(days)s
              and {where()}
            group by 1, 2 order by 1""", f)

    rows = run_db(go)
    days = {}
    for r in rows:
        days.setdefault(r["day"], {})[r["flow"]] = r["units"]
    today = ist_today()
    out = []
    for i in range(f["days"] - 1, -1, -1):
        day = today - timedelta(days=i)
        d = days.get(day, {})
        out.append({"day": day, "collected": d.get("COLLECTED", 0), "issued": d.get("ISSUED", 0),
                    "expired": d.get("EXPIRED", 0), "tti_discarded": d.get("DISCARDED_TTI", 0)})
    return {"period_days": f["days"], "daily": out}


# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    if not DATABASE_URL:
        sys.exit("DATABASE_URL not found. Create a .env file next to app.py with:  DATABASE_URL=postgresql://...")
    print("\n  Blood Bank Intelligence Dashboard")
    print("  Checking connection to Neon ...")
    try:
        h = run_db(lambda cur: query(cur, "select count(*) as n from raw.donations")[0])
        print(f"  Connected. {h['n']:,} blood units found in raw.donations.")
    except Exception as e:                        # keep running: Neon may just be waking up
        print("  WARNING: could not reach the database yet (" + type(e).__name__ + "). "
              "The dashboard will show an error until it responds - press Refresh.")
    print(f"\n  Open your browser at:  http://localhost:{PORT}\n  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
