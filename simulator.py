#!/usr/bin/env python3
"""
simulator.py  -  Real-time event simulator for the blood bank network (CIA-4)

Writes NEW events into the raw.* tables in Neon every couple of seconds, exactly like a
branch system would: donations arrive, hospitals raise requests, units are issued.
The SQL views recalculate on every query, so the dashboard changes while you watch.

  python simulator.py                  normal traffic, one event about every 2.5 seconds
  python simulator.py --interval 1     faster
  python simulator.py --surge O-       trauma surge: most requests are for O-negative
  python simulator.py --reset          delete everything the simulator created (back to the loaded data)

Stop with Ctrl+C.

IMPORTANT (be honest about this in the viva): this is SIMULATED streaming. In production the same
events would arrive continuously from branch systems through Kafka / Flume (see CIA-3); here a Python
loop plays the role of that stream, and the database and dashboard side is unchanged.

Design notes
  * Simulated rows use ids with an 'S' marker (DS.., US.., NS.., ES.., RS..) so they are easy to
    identify and --reset can remove them without touching the loaded data.
  * Timestamps use the DATABASE clock (now()), never a client clock.
  * A request is stamped with the time the hospital raised it, and is fulfilled "now", so
    response times stay realistic (emergency about 10-45 minutes, cross-branch supply slower).
  * Stock is issued first-expiry-first-out (FEFO). If the local branch has none, the unit with the
    earliest expiry anywhere in the network is used.
"""
import argparse
import os
import random
import sys
import time
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2 import extras

from generate_data import (BG, BRANCHES, COMP, COMP_DEMAND_W, DEMAND_W, FIRST, HOSP_SUFFIX, LAST, P_PLASMA,
                           P_PLATELETS, P_TTI, REQ_BASE, RESP_MIN, SUPPLY_W, UNITS_REQ, URGENCY, URGENCY_W,
                           VOLUME, dirty_bg, name_variant, phone_variant)

HERE = os.path.dirname(os.path.abspath(__file__))


def get_url():
    url = os.environ.get("DATABASE_URL")
    env_file = os.path.join(HERE, ".env")
    if not url and os.path.exists(env_file):
        for line in open(env_file, encoding="utf-8-sig"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("DATABASE_URL not found. Create a .env file next to simulator.py (same one the dashboard uses).")
    return url


def connect(url):
    conn = psycopg2.connect(url, connect_timeout=20, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=3, application_name="bloodbank-simulator")
    conn.autocommit = False
    return conn


# --------------------------------------------------------------------------------------
def reset(url):
    conn = connect(url)
    cur = conn.cursor()
    counts = {}
    for label, sql in [
        ("stock events", "delete from raw.stock_events where event_id like 'ES%'"),
        ("hospital requests", "delete from raw.hospital_requests where request_id like 'RS%'"),
        ("blood units", "delete from raw.donations where unit_id like 'US%'"),
        ("donor records", "delete from raw.donors where donor_id like 'DS%'"),
    ]:
        cur.execute(sql)
        counts[label] = cur.rowcount
    conn.commit()
    conn.close()
    print("Removed simulated data:", ", ".join(f"{v:,} {k}" for k, v in counts.items()))
    print("The database is back to the originally loaded data.")


# --------------------------------------------------------------------------------------
class Simulator:
    def __init__(self, url, interval, surge, p_donation):
        self.url, self.interval, self.surge, self.p_donation = url, interval, surge, p_donation
        self.rng = random.Random()
        self.seq = 0
        self.conn = None
        self.stats = dict(donations=0, units=0, requests=0, met=0, partial=0, unmet=0, cross=0, new_donors=0, dup_donors=0)

    # ---------- setup
    def open(self):
        self.conn = connect(self.url)
        cur = self.conn.cursor(cursor_factory=extras.RealDictCursor)
        cur.execute("select branch_id, branch_name, city from raw.branches order by branch_id")
        rows = cur.fetchall()
        if not rows:
            sys.exit("raw.branches is empty. Run load_to_neon.py first.")
        self.branch = {r["branch_id"]: r for r in rows}
        ids = list(self.branch)
        coll = {b[0]: b[4] for b in BRANCHES}
        dem = {b[0]: b[4] * b[5] * REQ_BASE for b in BRANCHES}
        self.ids = ids
        self.w_coll = [coll.get(i, 20) for i in ids]
        self.w_req = [dem.get(i, 15) for i in ids]
        # donors who have not donated in the last 90 days (one record per person)
        cur.execute("""
            select c.donor_id, c.full_name, c.dob, c.gender, c.phone10, c.blood_group, c.registered_branch_id
            from analytics.clean_donors c
            where c.donor_id = c.master_donor_id and c.blood_group is not null and length(c.phone10) = 10
              and c.donor_id not in (select donor_id from raw.donations where collected_at > now() - interval '90 days')
            order by random() limit 1500""")
        self.pool = [dict(r) for r in cur.fetchall()]
        self.conn.commit()

    def nid(self, prefix):
        self.seq += 1
        return f"{prefix}S{int(time.time() * 1000)}{self.seq % 1000:03d}"

    def name(self, bid):
        return self.branch[bid]["branch_name"]

    # ---------- donors
    def new_person(self):
        return {"full_name": f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)}",
                "dob": date.today() - timedelta(days=self.rng.randint(18 * 365, 60 * 365)),
                "gender": self.rng.choice(["M", "F"]),
                "phone10": str(self.rng.choice("6789")) + "".join(self.rng.choice("0123456789") for _ in range(9)),
                "blood_group": self.rng.choices(BG, SUPPLY_W)[0]}

    def register(self, cur, person, branch):
        did = self.nid("D")
        cur.execute("""insert into raw.donors (donor_id, full_name, dob, gender, phone, blood_group, registered_branch_id, registered_at)
                       values (%s, %s, %s, %s, %s, %s, %s, now())""",
                    (did, name_variant(self.rng, person["full_name"]), person["dob"], person["gender"],
                     phone_variant(self.rng, person["phone10"]), dirty_bg(self.rng, person["blood_group"]), branch))
        return did

    # ---------- event 1: a donation
    def donation(self, cur):
        rng = self.rng
        branch = rng.choices(self.ids, self.w_coll)[0]
        kind = "walk-in donor"
        if self.pool and rng.random() < 0.45:
            person = self.pool.pop(rng.randrange(len(self.pool)))
            donor_id = person["donor_id"]
            kind = "repeat donor"
            if rng.random() < 0.15:
                branch = rng.choice([b for b in self.ids if b != person["registered_branch_id"]] or self.ids)
            if branch != person["registered_branch_id"] and rng.random() < 0.65:
                donor_id = self.register(cur, person, branch)      # second registration of the same person
                kind = "repeat donor, registered again at this branch"
                self.stats["dup_donors"] += 1
        else:
            person = self.new_person()
            donor_id = self.register(cur, person, branch)
            self.stats["new_donors"] += 1
        bg = person["blood_group"]
        comps = ["RBC"]
        if rng.random() < P_PLATELETS:
            comps.append("PLATELETS")
        if rng.random() < P_PLASMA:
            comps.append("PLASMA")
        tti = rng.random() < P_TTI
        camp = "CAMP" if rng.random() < 0.3 else "CENTER"
        donation_id = self.nid("N")
        rows = []
        for comp in comps:
            days = rng.choices([35, 42], [0.4, 0.6])[0] if comp == "RBC" else (5 if comp == "PLATELETS" else 365)
            rows.append((self.nid("U"), donation_id, donor_id, branch, dirty_bg(rng, bg), comp, days, VOLUME[comp],
                         "REACTIVE" if tti else "NON-REACTIVE", camp))
        extras.execute_values(cur, """
            insert into raw.donations (unit_id, donation_id, donor_id, branch_id, blood_group, component,
                                       collected_at, expiry_at, volume_ml, tti_status, collection_type) values %s""",
                              rows, template="(%s,%s,%s,%s,%s,%s, now(), now() + make_interval(days => %s), %s,%s,%s)")
        self.stats["donations"] += 1
        self.stats["units"] += len(rows)
        note = "TTI reactive, units discarded" if tti else kind
        return f"DONATION  {self.name(branch):<18} {bg:<3} {'+'.join(comps):<20} {note}"

    # ---------- stock lookup (first-expiry-first-out)
    def take(self, cur, bg, comp, n, branch=None, exclude=None):
        cond, params = "", {"bg": bg, "comp": comp, "n": n}
        if branch:
            cond, params["b"] = "and d.branch_id = %(b)s", branch
        elif exclude:
            cond, params["b"] = "and d.branch_id <> %(b)s", exclude
        cur.execute(f"""
            select d.unit_id, d.branch_id
            from raw.donations d
            where analytics.norm_bg(d.blood_group) = %(bg)s and upper(trim(d.component)) = %(comp)s
              and d.tti_status = 'NON-REACTIVE' and d.expiry_at > now() + interval '30 minutes' {cond}
              and not exists (select 1 from raw.stock_events e where e.unit_id = d.unit_id)
            order by d.expiry_at limit %(n)s""", params)
        return cur.fetchall()

    # ---------- event 2: a hospital request
    def request(self, cur):
        rng = self.rng
        branch = rng.choices(self.ids, self.w_req)[0]
        surge = self.surge and rng.random() < 0.55
        bg = self.surge if surge else rng.choices(BG, DEMAND_W)[0]
        comp = "RBC" if surge else rng.choices(COMP, COMP_DEMAND_W)[0]
        urgency = "EMERGENCY" if (surge and rng.random() < 0.6) else rng.choices(URGENCY, URGENCY_W)[0]
        vals, w = UNITS_REQ[comp]
        n = rng.choices(vals, w)[0]

        got = [(u, b) for u, b in self.take(cur, bg, comp, n, branch=branch)]
        if len(got) < n:
            got += self.take(cur, bg, comp, n - len(got), exclude=branch)
        rid = self.nid("R")
        hospital = f"{self.branch[branch]['city']} {rng.choice(HOSP_SUFFIX)}"
        st = self.stats
        st["requests"] += 1
        if not got:
            status, minutes = "UNFULFILLED", 0
            cur.execute("""insert into raw.hospital_requests (request_id, hospital_name, branch_id, blood_group, component,
                           units_requested, units_fulfilled, urgency, requested_at, fulfilled_at, fulfilled_by_branch_id, status)
                           values (%s,%s,%s,%s,%s,%s,0,%s, now(), null, null, 'UNFULFILLED')""",
                        (rid, hospital, branch, dirty_bg(rng, bg), comp, n, urgency))
            st["unmet"] += 1
            outcome = "UNMET      no matching stock in the network"
        else:
            status = "FULFILLED" if len(got) == n else "PARTIAL"
            lo, hi = RESP_MIN[urgency]
            minutes = rng.uniform(lo, hi)
            cross = any(b != branch for _, b in got)
            if cross:
                minutes += rng.uniform(60, 150)
                st["cross"] += 1
            supplier = next((b for _, b in got if b != branch), got[0][1])   # a remote supplier if any unit came from elsewhere
            cur.execute("""insert into raw.hospital_requests (request_id, hospital_name, branch_id, blood_group, component,
                           units_requested, units_fulfilled, urgency, requested_at, fulfilled_at, fulfilled_by_branch_id, status)
                           values (%s,%s,%s,%s,%s,%s,%s,%s, now() - make_interval(secs => %s), now(), %s, %s)""",
                        (rid, hospital, branch, dirty_bg(rng, bg), comp, n, len(got), urgency, minutes * 60, supplier, status))
            extras.execute_values(cur, """
                insert into raw.stock_events (event_id, unit_id, event_type, event_time, from_branch_id, to_branch_id, request_id)
                values %s on conflict (unit_id) do nothing""",
                                  [(self.nid("E"), u, "ISSUED", b, branch, rid) for u, b in got],
                                  template="(%s,%s,%s, now(), %s,%s,%s)")
            st["met" if status == "FULFILLED" else "partial"] += 1
            src = f"from {self.name(supplier)}" if cross else "local stock"
            outcome = f"{status:<10} {src} ({minutes:.0f} min)"
        return f"REQUEST   {self.name(branch):<18} {bg:<3} {comp} x{n:<13} {urgency:<10} {outcome}"

    # ---------- main loop
    def run(self):
        self.open()
        print(f"\n  Simulator running against Neon. About one event every {self.interval:g} s."
              + (f" SURGE mode: {self.surge} demand." if self.surge else ""))
        print("  Watch the dashboard update. Press Ctrl+C to stop.\n")
        while True:
            try:
                with self.conn.cursor() as cur:
                    line = self.donation(cur) if self.rng.random() < self.p_donation else self.request(cur)
                self.conn.commit()
                print(f"  {datetime.now():%H:%M:%S}  {line}", flush=True)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                print(f"  connection lost ({type(e).__name__}); reconnecting in 5 s ...", flush=True)
                time.sleep(5)
                try:
                    self.conn.close()
                except Exception:
                    pass
                try:
                    self.open()
                except Exception as e2:
                    print("  reconnect failed:", type(e2).__name__, flush=True)
            except psycopg2.Error as e:
                self.conn.rollback()
                print("  event skipped:", str(e).strip().splitlines()[0], flush=True)
            time.sleep(self.interval * self.rng.uniform(0.6, 1.4))

    def summary(self):
        s = self.stats
        print(f"\n  Stopped. Sent {s['donations']} donations ({s['units']} units, {s['new_donors']} new donors, "
              f"{s['dup_donors']} duplicate registrations) and {s['requests']} requests "
              f"({s['met']} met in full, {s['partial']} partly met, {s['unmet']} unmet, {s['cross']} supplied across branches).")
        print("  To remove everything the simulator added:  python simulator.py --reset\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Real-time event simulator for the blood bank dashboard")
    ap.add_argument("--interval", type=float, default=2.5, help="average seconds between events (default 2.5)")
    ap.add_argument("--surge", metavar="GROUP", help="trauma surge: most requests are for this blood group, e.g. O-")
    ap.add_argument("--donation-share", type=float, default=0.5, help="share of events that are donations (default 0.5)")
    ap.add_argument("--reset", action="store_true", help="delete all simulator-created rows and exit")
    ap.add_argument("--max-events", type=int, default=0, help="stop after this many events (0 = run until Ctrl+C)")
    a = ap.parse_args()
    if a.surge and a.surge.upper() not in BG:
        sys.exit(f"--surge must be one of: {', '.join(BG)}")
    url = get_url()
    if a.reset:
        reset(url)
        sys.exit(0)
    sim = Simulator(url, a.interval, a.surge.upper() if a.surge else None, a.donation_share)
    if a.max_events:                       # used for automated testing
        sim.open()
        done = 0
        while done < a.max_events:
            with sim.conn.cursor() as cur:
                line = sim.donation(cur) if sim.rng.random() < sim.p_donation else sim.request(cur)
            sim.conn.commit()
            print(f"  {datetime.now():%H:%M:%S}  {line}")
            done += 1
            time.sleep(a.interval)
        sim.summary()
    else:
        try:
            sim.run()
        except KeyboardInterrupt:
            sim.summary()
