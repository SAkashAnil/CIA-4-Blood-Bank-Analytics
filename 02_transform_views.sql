-- =====================================================================
-- 02_transform_views.sql   (ELT step 2: TRANSFORM inside the database)
--   raw.*  ->  analytics.clean_*  ->  analytics.vw_*  (KPI / decision views)
-- Everything is a VIEW, so when new rows land in raw.* (the real-time
-- simulator) every KPI and the dashboard update automatically.
-- =====================================================================
create schema if not exists analytics;

-- ---------- reference data: business assumptions (state these in the viva) ----------
create table if not exists analytics.ref_components (
    component         text primary key,
    shelf_life_days   int,
    risk_window_hours int,      -- unit counts as "at risk" if it expires within this window
    target_cover_days numeric   -- desired days of stock cover per branch
);
truncate analytics.ref_components;
insert into analytics.ref_components values
    ('RBC',       35 , 168, 3),     -- 35-42 d shelf life, at risk within 7 days
    ('PLATELETS',  5 ,  48, 1),     -- 5 d shelf life,     at risk within 48 hours
    ('PLASMA',   365 , 720, 7);     -- frozen, 1 year,     at risk within 30 days

-- ---------- helper: blood-group normaliser ('o pos', ' O+ ' ...  ->  'O+') ----------
create or replace function analytics.norm_bg(g text) returns text
language sql immutable as $$
    select case when x ~ '^(A|B|AB|O)[+-]$' then x end
    from (select replace(replace(replace(replace(
              upper(regexp_replace(coalesce(g, ''), '\s', '', 'g')),
              'POSITIVE', '+'), 'NEGATIVE', '-'), 'POS', '+'), 'NEG', '-') as x) s
$$;

-- ---------- CLEAN: donors + Master Donor Index (dedup) ----------
-- Same person registered at several branches gets several donor_ids.
-- Match key = last 10 digits of phone + date of birth; master id = smallest donor_id in the group.
create or replace view analytics.clean_donors as
with c as (
    select donor_id,
           initcap(regexp_replace(trim(full_name), '\s+', ' ', 'g'))          as full_name,
           dob,
           upper(left(trim(gender), 1))                                        as gender,
           right(regexp_replace(coalesce(phone, ''), '\D', '', 'g'), 10)       as phone10,
           analytics.norm_bg(blood_group)                                      as blood_group,
           registered_branch_id,
           registered_at
    from raw.donors
)
select c.*,
       min(donor_id) over (partition by phone10, dob)       as master_donor_id,
       count(*)      over (partition by phone10, dob) > 1   as in_duplicate_group
from c;

-- ---------- CLEAN: units with their live status ----------
-- status: ISSUED / EXPIRED / DISCARDED_TTI come from stock_events;
-- a unit with no event is IN_STOCK, unless its expiry time has already passed (then EXPIRED).
create or replace view analytics.clean_units as
with base as (
    select u.unit_id, u.donation_id, d.master_donor_id, u.branch_id,
           analytics.norm_bg(u.blood_group)  as blood_group,
           upper(trim(u.component))          as component,
           u.collected_at, u.expiry_at, u.volume_ml,
           upper(trim(u.tti_status))         as tti_status,
           u.collection_type,
           ev.event_type, ev.event_time
    from raw.donations u
    left join analytics.clean_donors d on d.donor_id = u.donor_id
    left join raw.stock_events ev on ev.unit_id = u.unit_id      -- 1 terminal event per unit (unique index)
), s as (
    select b.*,
           case when event_type is not null       then event_type
                when tti_status = 'REACTIVE'      then 'DISCARDED_TTI'
                when expiry_at <= now()           then 'EXPIRED'
                else 'IN_STOCK' end               as status,
           case when event_type is not null       then event_time
                when tti_status = 'REACTIVE'      then collected_at
                when expiry_at <= now()           then expiry_at end as status_time
    from base b
)
select s.*,
       case when status = 'IN_STOCK'
            then round((extract(epoch from (expiry_at - now())) / 3600.0)::numeric, 1) end as hours_to_expiry
from s;

-- ---------- CLEAN: hospital requests ----------
create or replace view analytics.clean_requests as
select r.request_id, r.hospital_name, r.branch_id,
       analytics.norm_bg(r.blood_group)  as blood_group,
       upper(trim(r.component))          as component,
       r.units_requested, r.units_fulfilled,
       upper(trim(r.urgency))            as urgency,
       r.requested_at, r.fulfilled_at, r.fulfilled_by_branch_id,
       upper(trim(r.status))             as status,
       case when r.fulfilled_at is not null
            then round((extract(epoch from (r.fulfilled_at - r.requested_at)) / 60.0)::numeric, 1) end as response_minutes,
       (r.fulfilled_by_branch_id is not null
        and r.fulfilled_by_branch_id <> r.branch_id)                                                  as cross_branch
from raw.hospital_requests r;

-- ---------- MART: current inventory by branch / blood group / component ----------
create or replace view analytics.vw_inventory_status as
select u.branch_id, b.branch_name, u.blood_group, u.component,
       count(*)                                                        as units_in_stock,
       count(*) filter (where u.hours_to_expiry <= c.risk_window_hours) as units_at_risk,
       min(u.expiry_at)                                                as next_expiry,
       min(u.hours_to_expiry)                                          as min_hours_to_expiry
from analytics.clean_units u
join raw.branches b               on b.branch_id = u.branch_id
join analytics.ref_components c   on c.component = u.component
where u.status = 'IN_STOCK'
group by u.branch_id, b.branch_name, u.blood_group, u.component;

-- ---------- MART: expiry buckets (for the expiry-risk heatmap) ----------
create or replace view analytics.vw_expiry_buckets as
select u.branch_id, b.branch_name, u.blood_group, u.component,
       case when u.hours_to_expiry <= 24  then '1. < 24 hours'
            when u.hours_to_expiry <= 72  then '2. 1-3 days'
            when u.hours_to_expiry <= 168 then '3. 3-7 days'
            when u.hours_to_expiry <= 336 then '4. 7-14 days'
            else                               '5. 14+ days' end as expiry_bucket,
       count(*) as units
from analytics.clean_units u
join raw.branches b on b.branch_id = u.branch_id
where u.status = 'IN_STOCK'
group by 1, 2, 3, 4, 5;

-- ---------- MART: stock cover = stock / average daily demand (last 30 days) ----------
create or replace view analytics.vw_stock_cover as
with demand as (
    select branch_id, blood_group, component,
           sum(units_requested)::numeric / 30.0 as avg_daily_demand      -- true demand, incl. unfulfilled
    from analytics.clean_requests
    where requested_at >= now() - interval '30 days'
    group by 1, 2, 3
), stock as (
    select branch_id, blood_group, component, count(*) as units
    from analytics.clean_units where status = 'IN_STOCK'
    group by 1, 2, 3
), grid as (
    select b.branch_id, b.branch_name, g.blood_group, c.component, c.target_cover_days
    from raw.branches b
    cross join (values ('A+'),('A-'),('B+'),('B-'),('AB+'),('AB-'),('O+'),('O-')) g(blood_group)
    cross join analytics.ref_components c
)
select grid.branch_id, grid.branch_name, grid.blood_group, grid.component,
       coalesce(stock.units, 0)                              as units_in_stock,
       round(coalesce(d.avg_daily_demand, 0), 2)             as avg_daily_demand,
       round(coalesce(stock.units, 0) / nullif(d.avg_daily_demand, 0), 1) as cover_days,
       grid.target_cover_days,
       case when coalesce(stock.units, 0) = 0                                          then 'STOCKOUT'
            when coalesce(stock.units, 0) / nullif(d.avg_daily_demand, 0) < grid.target_cover_days     then 'LOW'
            when coalesce(stock.units, 0) / nullif(d.avg_daily_demand, 0) > 3 * grid.target_cover_days then 'SURPLUS'
            else 'OK' end                                     as stock_status
from grid
left join stock  on stock.branch_id  = grid.branch_id and stock.blood_group  = grid.blood_group and stock.component  = grid.component
left join demand d on d.branch_id    = grid.branch_id and d.blood_group      = grid.blood_group and d.component      = grid.component;

-- ---------- MART: inter-branch transfer recommendations ----------
-- surplus  = at-risk units that local demand will NOT consume before they expire
-- need     = units missing to reach the target days of cover at a branch
-- suggestion = move min(surplus, need) from the surplus branch to the deficit branch
create or replace view analytics.vw_transfer_recommendations as
with sc as materialized (
    select s.*, c.risk_window_hours,
           coalesce(i.units_at_risk, 0)     as units_at_risk,
           i.min_hours_to_expiry
    from analytics.vw_stock_cover s
    join analytics.ref_components c on c.component = s.component
    left join analytics.vw_inventory_status i
           on i.branch_id = s.branch_id and i.blood_group = s.blood_group and i.component = s.component
), surplus as (
    select branch_id, branch_name, blood_group, component, units_at_risk, min_hours_to_expiry,
           greatest(0, units_at_risk - ceil(avg_daily_demand * risk_window_hours / 24.0)) as surplus_units
    from sc where units_at_risk > 0
), deficit as (
    select branch_id, branch_name, blood_group, component, units_in_stock, cover_days,
           greatest(0, ceil(avg_daily_demand * target_cover_days) - units_in_stock) as need_units
    from sc where avg_daily_demand > 0
)
select s.branch_id  as from_branch_id, s.branch_name as from_branch,
       d.branch_id  as to_branch_id,   d.branch_name as to_branch,
       s.blood_group, s.component,
       s.surplus_units, d.need_units,
       least(s.surplus_units, d.need_units)::int as suggested_units,
       s.min_hours_to_expiry                     as hours_to_expiry,
       d.units_in_stock                          as receiver_stock,
       d.cover_days                              as receiver_cover_days
from surplus s
join deficit d on d.blood_group = s.blood_group and d.component = s.component and d.branch_id <> s.branch_id
where s.surplus_units > 0 and d.need_units > 0;

-- ---------- MART: daily flow in long format (collected / issued / expired / TTI-discarded) ----------
create or replace view analytics.vw_daily_flow as
select (collected_at at time zone 'Asia/Kolkata')::date as day, branch_id, blood_group, component,
       'COLLECTED' as flow, count(*) as units
from analytics.clean_units group by 1, 2, 3, 4
union all
select (status_time at time zone 'Asia/Kolkata')::date, branch_id, blood_group, component,
       status, count(*)
from analytics.clean_units
where status in ('ISSUED', 'EXPIRED', 'DISCARDED_TTI')
group by 1, 2, 3, 4, 5;

-- ---------- MART: headline KPIs (last 30 days) ----------
create or replace view analytics.vw_kpi_summary as
with u as (
    select count(*) filter (where collected_at >= now() - interval '30 days')                              as collected_30d,
           count(*) filter (where status = 'EXPIRED'       and status_time >= now() - interval '30 days')  as expired_30d,
           count(*) filter (where status = 'DISCARDED_TTI' and status_time >= now() - interval '30 days')  as tti_discarded_30d,
           count(*) filter (where status = 'IN_STOCK')                                                     as units_in_stock
    from analytics.clean_units
), risk as (
    select coalesce(sum(units_at_risk), 0) as units_at_risk from analytics.vw_inventory_status
), r as (
    select count(*)                                                                 as requests_30d,
           count(*) filter (where status = 'FULFILLED')                             as fulfilled_30d,
           count(*) filter (where status in ('UNFULFILLED', 'PARTIAL'))             as stockout_incidents_30d,
           round(avg(response_minutes), 1)                                          as avg_response_min,
           round(avg(response_minutes) filter (where urgency = 'EMERGENCY'), 1)     as avg_emergency_response_min
    from analytics.clean_requests
    where requested_at >= now() - interval '30 days'
), d as (
    select count(*) as donors, count(*) filter (where n >= 2) as repeat_donors
    from (select master_donor_id, count(distinct donation_id) as n
          from analytics.clean_units group by 1) t
)
select u.units_in_stock, risk.units_at_risk,
       u.collected_30d, u.expired_30d, u.tti_discarded_30d,
       round(100.0 * u.expired_30d       / nullif(u.collected_30d, 0), 1)        as wastage_rate_pct,
       round(100.0 * u.tti_discarded_30d / nullif(u.collected_30d, 0), 1)        as tti_discard_rate_pct,
       r.requests_30d, r.fulfilled_30d, r.stockout_incidents_30d,
       round(100.0 * r.fulfilled_30d     / nullif(r.requests_30d, 0), 1)         as fulfilment_rate_pct,
       r.avg_response_min, r.avg_emergency_response_min,
       d.donors as unique_donors, d.repeat_donors,
       round(100.0 * d.repeat_donors     / nullif(d.donors, 0), 1)               as repeat_donor_rate_pct
from u, risk, r, d;

-- ---------- MART: live activity feed (newest rows only - keeps the dashboard fast) ----------
create or replace view analytics.vw_live_feed as
select * from (
    (select collected_at as event_time, 'DONATION' as event_type, branch_id,
            analytics.norm_bg(blood_group) as blood_group, upper(component) as component,
            'Unit ' || unit_id || ' collected' as detail
     from raw.donations order by collected_at desc limit 40)
    union all
    (select requested_at, 'REQUEST', branch_id, analytics.norm_bg(blood_group), upper(component),
            upper(urgency) || ' request - ' || units_requested || ' unit(s) - ' || upper(status)
     from raw.hospital_requests order by requested_at desc limit 40)
    union all
    (select e.event_time, e.event_type, e.from_branch_id, analytics.norm_bg(d.blood_group), upper(d.component),
            'Unit ' || e.unit_id || ' ' || lower(e.event_type)
     from raw.stock_events e join raw.donations d on d.unit_id = e.unit_id
     where e.event_type in ('ISSUED', 'EXPIRED')
     order by e.event_time desc limit 40)
) t
order by event_time desc
limit 100;
