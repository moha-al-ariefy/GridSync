# GridSync MRP development plan

GridSync is an Odoo 19 manufacturing add-on I plan to build as `mrp_grid_scheduler`. It will compare production schedules using electricity prices and, when reliable data is available, carbon intensity. The features below are plans, not finished or production-ready work.

The original [build plan](docs/GridSync_Build_Plan.pdf) covers the architecture, data model, scheduler, priorities and demo. Its technical, tariff and licensing details still need to be checked before building around them.

## Product workflow

1. A planner sets the workcenter's power profile and chooses a tariff or data provider. The source, currency, units, validity, timezone and license are recorded.
2. The app imports permitted market data or creates time slots from documented, user-entered tariff rules. It shows the data source, age and whether values are forecasts or actuals. Missing slots aren't silently filled.
3. The planner selects work orders and a time period. The app saves a snapshot of the jobs, durations, dependencies, bookings and available data.
4. The scheduler suggests feasible times and shows the estimated costs, any carbon estimates, assumptions and jobs that couldn't be placed. Where meaningful, it also shows a valid lower bound for comparison.
5. The planner reviews and confirms the changes. Before applying, the app checks for conflicts, record changes and access permissions. It also keeps the information needed to revert safely.

## Priorities

**P0: core scheduling.** Build the Odoo add-on, access rules, company boundaries, provider and slot storage, sync log, a DE-LU Energy-Charts price adapter, manual tariff profiles and CSV import, workcenter energy inputs, a tested deterministic heuristic, run snapshots, review screen, apply and revert. Carbon inputs and estimated DE-LU intensity are part of the proposed P0 only where sourced, reproducible inputs and assumptions can be verified. A tariff-only workflow should remain usable when carbon data is unavailable.

**P1: after core gates pass.** Investigate work-order energy and cost ledger from real time records and specified power profiles, analytic posting, calendar and overtime handling, UK carbon-only provider, currency conversion and performance testing. Time logs and rated power produce estimates unless actual metered consumption is available; never label these figures as measured energy or actual emissions without such evidence.

**P2: optional extras.** Look into a separate ESG bridge, an Odoo AI tool wrapper, an exact solver, Enterprise Gantt, automated re-optimization, contract tariffs and onsite energy inputs. Check edition and API support first. None of these is needed for the core app.

## Scheduling and calculation requirements

- Normalize timestamps to UTC and preserve provider timezone and validity; test daylight-saving transitions, partial slots, gaps, negative prices, mixed resolutions and currency/unit conversion.
- Never combine prices and carbon signals from different markets as though they describe the same grid. Carbon and monetary objectives require explicitly defined units and sources.
- Respect workcenter availability and calendars, fixed bookings, production dependencies and deadlines before suggesting or applying any schedule. Calendar constraints are a safety gate for application even though the source plan lists the calendar enhancement in P1. If calendar handling is not implemented, restrict or disable applying proposals rather than presenting them as feasible.
- Keep the solver independent of the ORM and deterministic. Describe it as a heuristic; do not claim global optimality. Test feasibility and validate the lower bound and any reported gap, including when costs are zero or negative.
- Avoid overwriting a work order changed after a run; recheck availability and conflicts on apply and revert. Repeated operations must be idempotent and transparent.

## Data and environment gates

- Verify the workshop's Odoo 19 edition, installed manufacturing modules, version-specific APIs and work-order scheduling behaviour on a clean development database.
- Verify Energy-Charts endpoints, resolution, limits, data terms and attribution for the exact zone and dates before using its feed. Cache responses, handle errors and show data age.
- Obtain and verify a real, current UAE customer tariff and its applicability, dates and peak windows before claiming a UAE time-of-use example. A flat tariff must not be presented as time-varying. Do not use a fictional customer contract.
- Verify any carbon method, emissions factors and source licenses. Generation mix and renewable share are not automatically equivalent to consumption-based or marginal grid emissions. Clearly label any estimate and its method; do not claim audited Scope 2 or measured emissions.
- Separate Germany-Luxembourg price data from any UK carbon-only workflow; check availability for the same time horizon. Forecasts are not actual values.
- Confirm event timing, submission format, pre-event work rules, access to the workshop instance and venue connectivity with organizers.

## Quality and release gates

Test clean installation and upgrade, tariff parsing, duplicate sync, units, DST, missing slots, scheduling feasibility, dependencies, dates, concurrency, access control and company isolation. Test apply and revert against real Odoo work orders, including changed records and failed operations. Keep recorded public responses as fixtures where allowed; do not present test data as a live customer case. Validate calculations, operating assumptions, data rights and deployment before any claim of a production release.

## Demo direction

The source plan proposes a UAE tariff scenario and a separate live Germany-Luxembourg scenario, using real permitted inputs with attribution and visible data age. Show only features and sources that actually work. If a valid UAE time-varying tariff cannot be established, do not claim a peak-shifting saving for a flat-tariff customer. Saving figures must come from the demonstrated calculation and include the relevant assumptions and limitations.

## Open decisions

- Confirm the utility tariff and carbon sources, rights, temporal resolution and customer applicability.
- Determine a real customer workflow and whether someone would pay; no customer demand or savings are verified.
- Set a repository license and verify the licensing needs of any Odoo add-on and dependencies.
- Confirm delivery deadlines and available time; the source plan's three-day schedule is an estimate, not a guarantee.
