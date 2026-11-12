# GridSync MRP - Development Roadmap

GridSync runs on Odoo 19. The core flow runs from tariff inputs to review, apply and revert.

## 0. Get Odoo ready

- Start Odoo and check that PostgreSQL is running.
- Open the workshop instance and check the Odoo edition, database and installed apps.
- Set up a separate development database and make sure Manufacturing and work orders can be enabled.
- Find the Odoo source, add-on path and test command.
- Check the hackathon timing, submission format, pre-event work rules and network access.
- Check how work-order dates and calendars work in the installed Odoo version before changing schedules.

**Done when:** Odoo opens, the development database is ready, Manufacturing is accessible and a clean add-on installation test works.

**Last check:** Odoo was installed but stopped. PostgreSQL was running.

## 1. Build the GridSync foundation

- Create the `mrp_grid_scheduler` add-on with the Manufacturing dependency and a GridSync menu.
- Add planner permissions and company access rules.
- Make sure the add-on installs cleanly and can be upgraded. Keep the development setup repeatable.
- Keep credentials and customer data out of Git.
- Add a workcenter and a manufacturing work order to the development database to check the workflow. Clearly label any manually entered demo inputs.

**Done when:** GridSync installs on a fresh Odoo 19 database, a planner can open it, and unauthorized or cross-company access is blocked.

## 2. Add real electricity prices

- Build a tariff provider and store price intervals with their source, timezone, currency, units, validity and data age. Mark forecasts and actual values clearly.
- Support a documented manual time-of-use tariff and validated CSV import. Check the current UAE tariff and customer eligibility before using it in a UAE demo.
- Add the Germany-Luxembourg Energy-Charts price feed after checking the endpoint, terms, attribution and limits.
- Cache the data and show clear errors when it is old, missing or unavailable. Make imports and refreshes safe to repeat.

**Done when:** a planner can see real, permitted electricity prices in Odoo, check their source and spot any missing intervals.

**Phase 2 check.** The live DE-LU feed, time-of-use rules, and CSV import all use the same 15-minute price intervals. The historical TAQA tariff check generated seven days from the published 2025 rates. See [local setup](docs/LOCAL_SETUP.md) for the source and import format.

## 3. Build the scheduler

- Read the work orders, durations, deadlines, workcenter power, dependencies, calendars and existing bookings into a separate scheduling engine.
- Compare feasible time slots and suggest a lower-cost schedule. Explain why a job stays in place or cannot be scheduled.
- Keep results consistent for the same inputs. Add a valid lower bound where useful, and label the scheduler as a heuristic.
- Handle negative prices, partial intervals, units and daylight-saving changes.
- Add carbon inputs only after the source and calculation are verified. Price-based scheduling should work without carbon data.

**Done when:** tests show that proposed jobs respect availability, deadlines and dependencies, do not overlap, and have correctly calculated costs.

**Phase 3 check.** GridSync > Schedule creates proposals from open work orders, working hours, existing bookings, dependencies, deadlines and 15-minute prices. The engine in `services/scheduler.py` has no ORM dependency and is a deterministic greedy heuristic. Tests cover partial intervals, negative and missing prices, breaks, a daylight-saving day, dependency order, overlap and costs checked one minute at a time. Each run saves a snapshot that reproduces it. Computing a proposal leaves work orders in place. Estimates use electricity prices.

## 4. Review, apply and revert

- Show the current and proposed schedules, estimated costs, data age, assumptions and any unscheduled jobs.
- Ask for planner confirmation before applying a proposal.
- Check for changes to work orders and scheduling conflicts before writing anything.
- Save a snapshot and add a tested revert. If a record changed after the run, stop and explain instead of silently overwriting it.

**Done when:** a planner can run, review, apply and revert a real work order in the development database, with permissions and conflicts checked. A proposal cannot be applied if its calendar or constraints were not checked.

**Phase 4 check.** Review a complete proposal before applying it. GridSync checks the saved work orders and the current calendar before it updates Odoo. Revert restores the original dates and calendar bookings when they still fit. The proposal keeps the planner and time for both actions.

## 5. Get GridSync ready for launch

- Start with one manufacturing site. Verify its current electricity tariff against its bill and rate schedule, including effective dates, peak hours and any site-specific terms. Get workcenter power readings or documented estimates from the site.
- Compare the original and proposed schedules using the same tariff. Show estimated energy-charge savings, including zero savings when the applicable rate is flat. Keep wholesale market prices labeled with their market and currency.
- Run proposals against the site's schedule for review first. Have the planner check the dates, deadlines and costs. Start applying changes after the planner has tested approval, conflict handling and revert on a copy of the site database.
- Review permissions and company isolation. Test a clean install, an upgrade, backup restore, missing and stale prices, and simultaneous schedule edits. Check the planner screens in Odoo and make sure sync and scheduling failures are visible.
- Confirm who owns the code and include the LGPL and GPL texts in the customer build. Confirm that the organizers accept the Green and Renewable Energy framing.

**Done when:** a planner at the first site can verify the price inputs, review and apply a proposal, and recover the previous schedule when it is safe to do so. Record what the site verified before making savings claims.

## 6. After the core works

- Check the workflow and willingness to pay with manufacturers. Confirm data access and costs.
- Add an energy and cost ledger using real time logs and sourced power profiles. Label the results as estimates unless actual meter data is available.
- Explore accounting, more market providers and currency conversion.
- Consider exact optimization, an optional ESG integration and advanced scheduling after the core is reliable and the required data is available.
- Repeat the rollout checks at other sites and verify operating and support costs before expanding.

## Build order

The original plan suggests about three days, but that is an optimistic estimate. Start with **0 and 1**, then finish the price-to-proposal flow in **2 and 3**. Add safe apply and revert in **4**, then prepare for the first site launch in **5**.

If time gets tight, leave extra providers and carbon features for later. Keep the scheduling checks and safety features.

For the technical details, see the [development plan](DEVELOPMENT_PLAN.md) and the [original build plan](docs/GridSync_Build_Plan.pdf).
