# CIA-4: Real-Time Blood Bank Inventory & Expiry Intelligence

## Project Overview

This project develops a real-time business analytics prototype for a multi-branch blood bank network.

The system monitors:

- Blood inventory
- Expiry risk
- Blood-group availability
- Hospital requests
- Emergency demand
- Fulfilment performance
- Inter-branch transfer opportunities

## Technology Stack

- Python
- Neon PostgreSQL
- SQL
- Flask
- HTML
- CSS
- JavaScript
- Chart.js

## Architecture

Python Synthetic Data
        ↓
Neon PostgreSQL
        ↓
SQL Analytics Views
        ↓
Flask REST API
        ↓
Interactive HTML Dashboard

## Real-Time Analytics

The project uses simulated operational events to demonstrate near-real-time analytics.

Events include:

- New donations
- Hospital requests
- Blood issues
- Inventory changes
- Inter-branch transfers

The simulator updates the PostgreSQL database and the dashboard refreshes the resulting KPIs.

## Dashboard

The dashboard contains three major sections:

1. Executive Overview
2. Expiry & Inventory Intelligence
3. Emergency & Demand Monitoring

## Important Note

The dataset and real-time events are synthetic/simulated and are used only for academic demonstration.

No real patient or donor information is included.

## Running the Project

Install dependencies:

```bash
pip install -r requirements.txt
