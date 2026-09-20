-- =====================================================================
-- 01_raw_schema.sql   (ELT step 1: LOAD layer - raw landing tables)
-- Raw tables mirror the source extracts exactly as delivered (dirty values kept).
-- All cleaning happens later, in SQL, inside the warehouse (ELT, not ETL).
-- =====================================================================
create schema if not exists raw;
create schema if not exists analytics;

drop table if exists raw.stock_events      cascade;
drop table if exists raw.hospital_requests cascade;
drop table if exists raw.donations         cascade;
drop table if exists raw.donors            cascade;
drop table if exists raw.branches          cascade;

create table raw.branches (
    branch_id    text primary key,
    branch_name  text,
    city         text,
    branch_type  text                 -- HUB / SPOKE (hub-and-spoke, as in CIA-3)
);

create table raw.donors (
    donor_id             text,
    full_name            text,        -- may contain case / spacing variants
    dob                  date,
    gender               text,
    phone                text,        -- may contain +91, spaces, leading 0
    blood_group          text,        -- may be 'o+', ' O+ ', 'O POS' ...
    registered_branch_id text,
    registered_at        timestamptz
);

-- one row per blood UNIT (component) collected
create table raw.donations (
    unit_id         text primary key,
    donation_id     text,             -- one donation can yield several units (RBC, platelets, plasma)
    donor_id        text,
    branch_id       text,
    blood_group     text,
    component       text,             -- RBC / PLATELETS / PLASMA
    collected_at    timestamptz,
    expiry_at       timestamptz,
    volume_ml       int,
    tti_status      text,             -- REACTIVE / NON-REACTIVE (transfusion-transmissible infection screen)
    collection_type text              -- CENTER / CAMP
);

create table raw.hospital_requests (
    request_id             text primary key,
    hospital_name          text,
    branch_id              text,      -- branch that received the request
    blood_group            text,
    component              text,
    units_requested        int,
    units_fulfilled        int,
    urgency                text,      -- EMERGENCY / URGENT / ROUTINE
    requested_at           timestamptz,
    fulfilled_at           timestamptz,
    fulfilled_by_branch_id text,
    status                 text       -- FULFILLED / PARTIAL / UNFULFILLED
);

-- terminal event of a unit: ISSUED / EXPIRED / DISCARDED_TTI
create table raw.stock_events (
    event_id       text primary key,
    unit_id        text,
    event_type     text,
    event_time     timestamptz,
    from_branch_id text,
    to_branch_id   text,
    request_id     text
);

create index on raw.donations (branch_id, component);
create index on raw.donations (collected_at);
create index on raw.hospital_requests (requested_at);
create unique index on raw.stock_events (unit_id);   -- one terminal event per unit
create index on raw.stock_events (event_time);
