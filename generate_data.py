#!/usr/bin/env python3
"""
generate_data.py  -  Synthetic data generator for CIA-4 (Blood Bank Analytics)

ALL DATA PRODUCED HERE IS SYNTHETIC. No real donor or patient information is used.

It simulates a multi-branch blood bank network day by day (default: 180 days ending NOW):
  * donors donate -> each donation yields RBC (+ sometimes platelets / plasma) units
  * hospitals raise requests -> filled first-expiry-first-out (FEFO) from local stock,
    else from another branch (slower), else the request is (partly) unfulfilled
  * units that reach their expiry date without being issued are wasted
  * ~1.5% of donations are TTI-reactive and discarded
  * deliberate imbalance: Kochi over-collects, Hyderabad/Delhi are short -> a real
    inter-branch transfer story for the dashboard
  * deliberate data mess (formatting variants in blood group / phone / name, donors
    registered separately at several branches) so the SQL cleaning step has real work

Output: CSV files in ./data  (branches, donors, donations, hospital_requests, stock_events)

Usage:  python generate_data.py [--days 180] [--seed 42] [--out data]
"""
import argparse
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone, time

IST = timezone(timedelta(hours=5, minutes=30))

# branch_id, name, city, type, donation events/day, demand ratio (requests relative to supply)
BRANCHES = [
    ("BR01", "Bengaluru Central", "Bengaluru", "HUB",   30, 0.90),
    ("BR02", "Kochi Camp Centre", "Kochi",     "SPOKE", 34, 0.45),   # over-collects -> expiry surplus
    ("BR03", "Chennai Main",      "Chennai",   "SPOKE", 22, 0.75),
    ("BR04", "Hyderabad East",    "Hyderabad", "SPOKE", 14, 1.30),   # under-supplied
    ("BR05", "Mumbai West",       "Mumbai",    "SPOKE", 26, 0.85),
    ("BR06", "Delhi North",       "Delhi",     "SPOKE", 18, 1.05),   # under-supplied
]
REQ_BASE = 0.86  # base requests per donation event (tuned so network is ~balanced)

BG = ["O+", "B+", "A+", "AB+", "O-", "B-", "A-", "AB-"]
SUPPLY_W = [0.36, 0.31, 0.21, 0.07, 0.02, 0.015, 0.01, 0.005]
DEMAND_W = [0.34, 0.29, 0.20, 0.07, 0.05, 0.02, 0.02, 0.01]   # O- demand > O- supply -> stockouts

COMP = ["RBC", "PLATELETS", "PLASMA"]
COMP_DEMAND_W = [0.72, 0.19, 0.09]
UNITS_REQ = {"RBC": ([1, 2, 3], [0.55, 0.28, 0.17]),
             "PLATELETS": ([1, 2], [0.6, 0.4]),
             "PLASMA": ([1, 2], [0.7, 0.3])}
VOLUME = {"RBC": 350, "PLATELETS": 250, "PLASMA": 220}
P_PLATELETS = 0.22
P_PLASMA = 0.20
P_TTI = 0.015

URGENCY = ["EMERGENCY", "URGENT", "ROUTINE"]
URGENCY_W = [0.15, 0.30, 0.55]
RESP_MIN = {"EMERGENCY": (8, 45), "URGENT": (30, 90), "ROUTINE": (60, 240)}

FIRST = ["Aarav", "Vivaan", "Aditya", "Arjun", "Rohan", "Karthik", "Suresh", "Ramesh", "Anil", "Vikram",
         "Priya", "Ananya", "Divya", "Meera", "Sneha", "Lakshmi", "Kavya", "Neha", "Pooja", "Fatima",
         "Mohammed", "Imran", "Joseph", "Thomas", "George", "Mary", "Anitha", "Rahul", "Sanjay", "Deepak"]
LAST = ["Sharma", "Nair", "Menon", "Reddy", "Iyer", "Patel", "Khan", "Das", "Singh", "Kumar",
        "Pillai", "Varma", "Joshi", "Mehta", "Rao", "Gupta", "Thomas", "Mathew", "Bose", "Naidu"]
HOSP_SUFFIX = ["General Hospital", "Trauma Centre", "Children's Hospital", "Cancer Institute", "Maternity Hospital"]


def poisson(rng, lam):
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def dirty_bg(rng, bg):
    """Raw systems store blood group inconsistently."""
    r = rng.random()
    if r < 0.90:
        return bg
    if r < 0.94:
        return bg.lower()
    if r < 0.97:
        return " " + bg + " "
    return bg.replace("+", " POS").replace("-", " NEG")


def phone_variant(rng, digits):
    r = rng.random()
    if r < 0.60:
        return digits
    if r < 0.80:
        return "+91" + digits
    if r < 0.93:
        return "+91 " + digits[:5] + " " + digits[5:]
    return "0" + digits


def name_variant(rng, name):
    r = rng.random()
    if r < 0.65:
        return name
    if r < 0.80:
        return name.upper()
    if r < 0.90:
        return "  " + name.replace(" ", "  ") + " "
    return name.lower()


def simulate(days, seed, out):
    rng = random.Random(seed)
    now = datetime.now(IST).replace(microsecond=0)
    today = now.date()
    start_day = today - timedelta(days=days - 1)
    branch_ids = [b[0] for b in BRANCHES]
    coll_rate = {b[0]: b[4] for b in BRANCHES}
    req_rate = {b[0]: b[4] * b[5] * REQ_BASE for b in BRANCHES}
    camp_p = {b[0]: (0.6 if b[0] == "BR02" else 0.3) for b in BRANCHES}
    city = {b[0]: b[2] for b in BRANCHES}

    donor_rows, unit_rows, event_rows, request_rows = [], [], [], []
    ids = {"D": 0, "U": 0, "N": 0, "E": 0, "R": 0}

    def nid(prefix, width=6):
        ids[prefix] += 1
        return f"{prefix}{ids[prefix]:0{width}d}"

    persons = []           # every synthetic human
    pools = {b: [] for b in branch_ids}   # persons by home branch

    def add_record(person, branch, when):
        """Create a donor record for this person at a branch (may become a duplicate)."""
        did = nid("D")
        person["records"][branch] = did
        donor_rows.append([did, name_variant(rng, person["name"]), person["dob"].isoformat(), person["gender"],
                           phone_variant(rng, person["phone"]), dirty_bg(rng, person["bg"]), branch,
                           when.isoformat()])
        return did

    def new_person(branch, when):
        gender = rng.choice(["M", "F"])
        p = {"name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
             "dob": (today - timedelta(days=rng.randint(18 * 365, 60 * 365))),
             "gender": gender, "phone": str(rng.choice("6789")) + "".join(rng.choice("0123456789") for _ in range(9)),
             "bg": rng.choices(BG, SUPPLY_W)[0], "home": branch, "last": None, "records": {}}
        persons.append(p)
        pools[branch].append(p)
        add_record(p, branch, when)
        return p

    # ---- pre-existing donor base (registered before the window; some are eligible to donate again)
    for b in BRANCHES:
        for _ in range(b[4] * 40):
            p = new_person(b[0], datetime.combine(start_day - timedelta(days=rng.randint(60, 700)), time(10), IST))
            if rng.random() < 0.7:
                p["last"] = datetime.combine(start_day - timedelta(days=rng.randint(10, 300)), time(10), IST)

    stock = {}   # (branch, bg, comp) -> list of unit dicts

    def stock_list(b, bg, comp):
        return stock.setdefault((b, bg, comp), [])

    def take(b, bg, comp, t):
        lst = stock_list(b, bg, comp)
        ok = [u for u in lst if u["expiry"] > t]
        if not ok:
            return None
        u = min(ok, key=lambda x: x["expiry"])        # FEFO
        lst.remove(u)
        return u

    def take_remote(b, bg, comp, t):
        cands = sorted(((len(stock_list(x, bg, comp)), x) for x in branch_ids if x != b), reverse=True)
        for _, x in cands:
            u = take(x, bg, comp, t)
            if u:
                return u, x
        return None, None

    def do_collect(branch, t):
        # choose donor: repeat donor (if eligible) or new walk-in
        person = None
        src = branch
        if rng.random() < 0.10:
            src = rng.choice([x for x in branch_ids if x != branch])   # donor travels to another branch
        if rng.random() < 0.65:
            for _ in range(8):
                c = rng.choice(pools[src])
                if c["last"] is None or (t - c["last"]).days >= 90:
                    person = c
                    break
        if person is None:
            person = new_person(branch, t)
        person["last"] = t
        # donor record at this branch (duplicates arise when a person appears at several branches)
        if branch in person["records"]:
            did = person["records"][branch]
        elif person["records"] and rng.random() < 0.35:
            did = next(iter(person["records"].values()))
        else:
            did = add_record(person, branch, t)
        donation_id = nid("N")
        collection_type = "CAMP" if rng.random() < camp_p[branch] else "CENTER"
        tti = rng.random() < P_TTI
        comps = ["RBC"]
        if rng.random() < P_PLATELETS:
            comps.append("PLATELETS")
        if rng.random() < P_PLASMA:
            comps.append("PLASMA")
        for comp in comps:
            if comp == "RBC":
                shelf = timedelta(days=rng.choices([35, 42], [0.4, 0.6])[0])
            elif comp == "PLATELETS":
                shelf = timedelta(days=5)
            else:
                shelf = timedelta(days=365)
            uid = nid("U", 7)
            exp = t + shelf
            unit_rows.append([uid, donation_id, did, branch, dirty_bg(rng, person["bg"]), comp,
                              t.isoformat(), exp.isoformat(), VOLUME[comp],
                              "REACTIVE" if tti else "NON-REACTIVE", collection_type])
            if tti:
                dt = t + timedelta(days=1)
                if dt <= now:
                    event_rows.append([nid("E", 7), uid, "DISCARDED_TTI", dt.isoformat(), branch, "", ""])
            else:
                stock_list(branch, person["bg"], comp).append(
                    {"id": uid, "branch": branch, "bg": person["bg"], "comp": comp, "expiry": exp})

    def do_request(branch, t):
        bg = rng.choices(BG, DEMAND_W)[0]
        comp = rng.choices(COMP, COMP_DEMAND_W)[0]
        urg = rng.choices(URGENCY, URGENCY_W)[0]
        vals, w = UNITS_REQ[comp]
        n = rng.choices(vals, w)[0]
        got = []
        for _ in range(n):
            u = take(branch, bg, comp, t)
            src = branch
            if u is None:
                u, src = take_remote(branch, bg, comp, t)
            if u is None:
                break
            got.append((u, src))
        rid = nid("R")
        hosp = f"{city[branch]} {rng.choice(HOSP_SUFFIX)}"
        if not got:
            status, fulfilled_at, by = "UNFULFILLED", "", ""
        else:
            status = "FULFILLED" if len(got) == n else "PARTIAL"
            lo, hi = RESP_MIN[urg]
            minutes = rng.uniform(lo, hi)
            if any(s != branch for _, s in got):
                minutes += rng.uniform(60, 150)        # inter-branch transport
            f_at = min(t + timedelta(minutes=minutes), now)
            fulfilled_at, by = f_at.isoformat(), got[0][1]
            for u, s in got:
                event_rows.append([nid("E", 7), u["id"], "ISSUED", f_at.isoformat(), s, branch, rid])
        request_rows.append([rid, hosp, branch, dirty_bg(rng, bg), comp, n, len(got), urg,
                             t.isoformat(), fulfilled_at, by, status])

    def sweep_expiry(limit):
        for (b, bg, comp), lst in stock.items():
            dead = [u for u in lst if u["expiry"] <= limit]
            for u in dead:
                lst.remove(u)
                event_rows.append([nid("E", 7), u["id"], "EXPIRED", u["expiry"].isoformat(), b, "", ""])

    d = start_day
    while d <= today:
        day0 = datetime.combine(d, time(0), IST)
        timeline = []
        for b in branch_ids:
            for _ in range(poisson(rng, coll_rate[b])):
                timeline.append((day0 + timedelta(hours=8 + rng.random() * 9), "C", b))
            for _ in range(poisson(rng, req_rate[b])):
                timeline.append((day0 + timedelta(hours=7 + rng.random() * 16), "R", b))
        timeline = sorted(x for x in timeline if x[0] <= now)
        for t, kind, b in timeline:
            (do_collect if kind == "C" else do_request)(b, t)
        sweep_expiry(min(day0 + timedelta(days=1), now))
        d += timedelta(days=1)

    # ---- write CSVs
    os.makedirs(out, exist_ok=True)

    def write(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"  {name:24s} {len(rows):>7,} rows")

    print(f"Generated {days} days ending {now.isoformat()}")
    write("branches.csv", ["branch_id", "branch_name", "city", "branch_type"], [b[:4] for b in BRANCHES])
    write("donors.csv", ["donor_id", "full_name", "dob", "gender", "phone", "blood_group",
                         "registered_branch_id", "registered_at"], donor_rows)
    write("donations.csv", ["unit_id", "donation_id", "donor_id", "branch_id", "blood_group", "component",
                            "collected_at", "expiry_at", "volume_ml", "tti_status", "collection_type"], unit_rows)
    write("hospital_requests.csv", ["request_id", "hospital_name", "branch_id", "blood_group", "component",
                                    "units_requested", "units_fulfilled", "urgency", "requested_at",
                                    "fulfilled_at", "fulfilled_by_branch_id", "status"], request_rows)
    write("stock_events.csv", ["event_id", "unit_id", "event_type", "event_time", "from_branch_id",
                               "to_branch_id", "request_id"], event_rows)

    # ---- quick sanity summary
    coll = {c: 0 for c in COMP}
    for r in unit_rows:
        coll[r[5]] += 1
    comp_of = {r[0]: r[5] for r in unit_rows}
    exp = {c: 0 for c in COMP}
    for e in event_rows:
        if e[2] == "EXPIRED":
            exp[comp_of[e[1]]] += 1
    print("\nSanity check (last-run, whole period):")
    for c in COMP:
        print(f"  {c:10s} collected {coll[c]:>6,}  expired {exp[c]:>5,}  wastage {100*exp[c]/max(coll[c],1):5.1f}%")
    st = {}
    for r in request_rows:
        st[r[11]] = st.get(r[11], 0) + 1
    tot = sum(st.values())
    print("  Requests:", {k: f"{v} ({100*v/tot:.1f}%)" for k, v in st.items()})
    print("  Units in stock now by branch:",
          {b: sum(len(l) for (bb, _, _), l in stock.items() if bb == b) for b in branch_ids})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    simulate(a.days, a.seed, a.out)
