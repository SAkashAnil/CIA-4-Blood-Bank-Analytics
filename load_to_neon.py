#!/usr/bin/env python3
"""
load_to_neon.py  -  ELT pipeline runner (Extract -> Load -> Transform)

  E  extract : reads the CSV extracts in ./data  (produced by generate_data.py)
  L  load    : bulk-loads them (COPY) into the raw.* tables in Neon Postgres
  T  transform: runs 02_transform_views.sql -> analytics.* views (cleaning + KPIs) inside the database

Setup:  create a file called .env next to this script containing ONE line:
        DATABASE_URL=postgresql://user:password@ep-xxxx.aws.neon.tech/neondb?sslmode=require
Run:    python load_to_neon.py
"""
import os
import sys
import time

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# load order + columns (must match the CSV headers)
TABLES = [
    ("raw.branches", "branches.csv"),
    ("raw.donors", "donors.csv"),
    ("raw.donations", "donations.csv"),
    ("raw.hospital_requests", "hospital_requests.csv"),
    ("raw.stock_events", "stock_events.csv"),
]


def get_url():
    url = os.environ.get("DATABASE_URL")
    env_file = os.path.join(HERE, ".env")
    if not url and os.path.exists(env_file):
        for line in open(env_file, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("DATABASE_URL not found. Create a .env file with DATABASE_URL=postgresql://...")
    return url


def run_sql_file(cur, name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        cur.execute(f.read())


def main():
    conn = psycopg2.connect(get_url())
    conn.autocommit = False
    cur = conn.cursor()

    print("[1/3] Creating raw landing tables ...")
    run_sql_file(cur, "01_raw_schema.sql")

    print("[2/3] Loading CSV extracts into raw.* (COPY) ...")
    for table, csv_name in TABLES:
        t0 = time.time()
        with open(os.path.join(DATA, csv_name), "r", encoding="utf-8") as f:
            cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT csv, HEADER true)", f)
        cur.execute(f"select count(*) from {table}")
        print(f"      {table:24s} {cur.fetchone()[0]:>8,} rows  ({time.time()-t0:.1f}s)")

    print("[3/3] Building transformation views (clean_* and vw_*) ...")
    run_sql_file(cur, "02_transform_views.sql")
    conn.commit()

    conn.autocommit = True          # ANALYZE: refresh planner statistics after the bulk load
    for table, _ in TABLES:
        cur.execute(f"ANALYZE {table}")
    conn.autocommit = False

    print("\nHeadline KPIs (last 30 days):")
    cur.execute("select * from analytics.vw_kpi_summary")
    cols = [c[0] for c in cur.description]
    for k, v in zip(cols, cur.fetchone()):
        print(f"      {k:28s} {v}")
    conn.close()
    print("\nDone. The pipeline is safe to re-run at any time (it rebuilds the raw tables).")


if __name__ == "__main__":
    main()
