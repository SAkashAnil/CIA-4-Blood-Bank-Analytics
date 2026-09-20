# CIA-4 demo: recording script (about 2 minutes) and viva notes for the real-time part

## Before you record (5 minutes)

1. Refresh the data so nothing looks stale. In `C:\cia4`, run:
   `python generate_data.py` then `python load_to_neon.py`
2. Open **two** cmd windows in `C:\cia4` and arrange them side by side with the browser:
   - Window A: `python app.py` (leave it running)
   - Window B: type `python simulator.py` but **do not press Enter yet**
3. Open http://localhost:5000 in your browser. Press Ctrl+0 so zoom is 100%. Close chat and notification pop-ups.
4. Start the Windows recorder: **Win + Alt + R** (it records the active window; press again to stop). Files save in `Videos\Captures`. Speak while recording, or add a voice-over afterwards.

## Timeline and what to say

| Time | On screen | What to say (in your own words) |
|---|---|---|
| 0:00 to 0:10 | Executive overview | "This is a real-time inventory and expiry dashboard for a multi-branch blood bank network. It extends my CIA-3 Hadoop proposal into a working pipeline: Neon PostgreSQL, SQL views, a Flask API and this dashboard." |
| 0:10 to 0:30 | Point at the KPI band, then the headline sentence | "Every number comes from SQL views, none is typed in. Units at expiry risk are stock expiring inside its risk window. Wastage rate is expired units divided by units collected. Both are above target, so the manager has a problem to fix." |
| 0:30 to 0:45 | Choose **Kochi Camp Centre** in the Branch filter, then Clear filters | "Filters re-query the database. Kochi collects far more than it uses, so its wastage is much higher than the network's." |
| 0:45 to 1:10 | **Expiry and inventory** tab: expiry chart, heatmap, transfer table | "Red in the heatmap means out of stock. The transfer plan matches near-expiry surplus at one branch with a shortage at another, most urgent first, and never counts a unit twice. This is the managerial recommendation." |
| 1:10 to 1:40 | **Emergency and demand** tab. Now press Enter in Window B (simulator). | "Now the real-time part. This script streams donations, hospital requests and issues into the cloud database every couple of seconds." Point at the feed, the header ("Latest activity, seconds ago") and the KPI cards that flash yellow when a value changes. |
| 1:40 to 1:55 | Optional: Ctrl+C in Window B, then run `python simulator.py --surge O- --interval 1` | "Now a trauma surge: most requests are for O-negative. Unmet requests climb and O-negative goes out of stock, which is what the manager would act on." |
| 1:55 to 2:00 | Stop the simulator (Ctrl+C) | "The streaming is simulated. In production the same events would arrive from branch systems through Kafka, as in CIA-3." |

Tips: the dashboard auto-refreshes every 10 seconds and the feed every 4 seconds. Press **Refresh data** if you want an update immediately.

## After recording

Run `python simulator.py --reset` to delete everything the simulator added and return to the loaded data. Re-running `load_to_neon.py` also wipes it, so do not run the loader while the simulator is running.

## Viva answers for the real-time part

- **How is it real-time?** A simulator inserts new events into the raw tables in Neon every few seconds. Every KPI is a SQL view over those tables, so the next query, and the next dashboard refresh, reflects the new data. The dashboard polls the API every 10 seconds, and the feed every 4 seconds.
- **Is it truly streaming?** No, it is simulated streaming, and I say so. It is near-real-time by polling, not push. A production version would receive events from branch systems through Kafka or Flume into the warehouse, with the same tables, views and dashboard.
- **Why not a real stream?** There is no live hospital data feed available, so a generator plays the branches. The database and dashboard side is what would stay unchanged.
- **How do you keep simulated data separate?** Simulated rows carry an `S` marker in their ids, so `--reset` removes exactly those rows.
- **How do you know the timestamps are consistent?** They use the database clock. A request is stamped with the time the hospital raised it and fulfilled now, so response times stay realistic and nothing is dated in the future.
- **What would change with real data?** The ingestion step, in place of the simulator, and the KPI targets, which are illustrative assumptions here. The SQL, API and dashboard would stay the same.
- **How does this relate to CIA-3?** CIA-3 proposed the architecture: sources, ingestion, storage and analytics. CIA-4 implements a working slice of it: raw tables as the landing layer, SQL views as the transformation and analytics layer, and a dashboard as the decision layer.
