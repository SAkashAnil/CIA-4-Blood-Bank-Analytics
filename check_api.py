#!/usr/bin/env python3
"""
check_api.py - quick self-test for the dashboard API. Start app.py first, then in a second terminal run:

    python check_api.py

Prints PASS/FAIL for every endpoint (no packages needed beyond Python itself).
"""
import json
import sys
import urllib.error
import urllib.request

import os
BASE = os.environ.get("BASE_URL", "http://localhost:5000")


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


CHECKS = [
    # (name, path, expected status, key that must be in the JSON, or None)
    ("Health",                     "/api/health",                                   200, "last_activity"),
    ("KPIs",                       "/api/kpis",                                     200, "wastage_rate_pct"),
    ("KPIs filtered (branch)",     "/api/kpis?branch=BR02",                         200, "units_in_stock"),
    ("Inventory",                  "/api/inventory",                                200, "by_blood_group"),
    ("Expiry risk",                "/api/expiry-risk",                              200, "buckets"),
    ("Requests",                   "/api/requests",                                 200, "summary"),
    ("Requests (emergency only)",  "/api/requests?urgency=EMERGENCY",               200, "recent"),
    ("Branches",                   "/api/branches",                                 200, "branches"),
    ("Transfer recommendations",   "/api/transfer-recommendations",                 200, "recommendations"),
    ("Live feed",                  "/api/live-feed?limit=5",                        200, "events"),
    ("Trends",                     "/api/trends?days=7",                            200, "daily"),
    ("Blood group filter (O-)",    "/api/kpis?blood_group=O-",                      200, "units_in_stock"),
    ("Rejects bad blood group",    "/api/kpis?blood_group=XYZ",                     400, "error"),
    ("Rejects SQL injection",      "/api/kpis?branch=BR01';DROP%20TABLE%20x;--",    400, "error"),
    ("Unknown API route",          "/api/nope",                                     404, "error"),
    ("Dashboard page",             "/",                                             200, None),
    ("Secrets not downloadable",   "/.env",                                         404, None),
    ("Source not downloadable",    "/app.py",                                       404, None),
]

failed = 0
for name, path, want, key in CHECKS:
    status, body = get(path)
    ok = status == want
    if ok and key:
        try:
            ok = key in json.loads(body)
        except ValueError:
            ok = False
    if want == 200 and key is None:
        ok = ok and "<html" in body.lower()
    if want == 404 and key is None:
        ok = ok and "DATABASE_URL" not in body
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {name:28s} {path}  -> HTTP {status}")

print("\nAll checks passed." if not failed else f"\n{failed} check(s) failed. If the failures are 503, Neon may be waking up - run again in a few seconds.")
sys.exit(1 if failed else 0)
