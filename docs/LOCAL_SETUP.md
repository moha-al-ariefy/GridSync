# Local setup

This setup is for the Odoo 19 workshop installation on Windows. Keep it separate from real customer data.

## Before starting

- Start PostgreSQL and make sure the Odoo workshop installation is available.
- Use a separate database for GridSync development and another fresh database for tests.
- Keep passwords in the local Odoo configuration or another private credential store. Do not put them in this repository or in command arguments.
- The installed Odoo service and its access settings are managed separately. Check who can reach it before using real data.

## Install GridSync for development

Run the installed Odoo Python and `odoo-bin` from the workshop server directory. Pass the existing Odoo configuration with `-c`, the workshop add-on directories plus this repository with `--addons-path`, and the development database with `-d`. Install the module with `-i mrp_grid_scheduler --without-demo --stop-after-init --no-http`.

GridSync installs Odoo Manufacturing and Invoicing as dependencies. On an existing database, take a backup and try the upgrade on an isolated copy first. Confirm the Invoicing add-on is available on the server, then upgrade GridSync with `-u mrp_grid_scheduler --stop-after-init --no-http`.

The workshop installation normally includes the Python packages listed in [requirements.txt](../requirements.txt). For a separate installation, use the same Python environment that runs Odoo and keep its version constraints.

The add-on path is a comma-separated list. Include all required Odoo and workshop directories when overriding it; otherwise installed modules may not be found. The exact paths depend on the installation.

To open the add-on without changing the workshop service, run a separate local Odoo process with the same configuration and add-on paths, `--http-interface=127.0.0.1`, a free `--http-port`, a database filter for the development database and `--no-database-list`. Stop that process when finished. Do not run tests against a shared or production database.

For the loopback-only Windows preview, use `--limit-time-real=0 --limit-time-real-cron=120` if Odoo stops after an idle browser connection. Odoo can count time spent waiting for the browser's first HTTP request against its request deadline. Keep the preview bound to `127.0.0.1` and filtered to the development database.

## Run the tests

On a new test database, install with `-i mrp_grid_scheduler --without-demo --test-enable --test-tags /mrp_grid_scheduler --stop-after-init --no-http`. To repeat the tests against that isolated database, use `-u mrp_grid_scheduler` instead of `-i`. Check the Odoo log and process exit code; the test users and records are test fixtures, not customer data.

## First steps in GridSync

Give a manufacturing user the GridSync Planner role, or use the GridSync Manager role for site administration. Open GridSync, create a site and select its work centers. Planner access is limited to allowed companies; only a manager can delete a site.

Under Tariffs, a GridSync Manager can choose **Energy-Charts day-ahead**, select a **Market**, save, and select **Sync prices**. Germany and Luxembourg, Austria, Belgium, France, and the Netherlands use the same connector. Create a separate source for each market. Use these wholesale prices for site costs only if the site contract follows that market. The source displays its license, attribution, sync time and 15-minute EUR/MWh intervals. A repeat sync updates existing intervals rather than duplicating them. Missing prices are not filled with zero. The sync uses the public Energy-Charts API and does not require an API key.

Automatic sync is on by default for each company. Odoo checks every five minutes while its scheduled actions are running. Opening Overview or Tariffs also checks for a refresh if the last attempt was at least five minutes ago. An Odoo administrator can turn it off under **GridSync > Settings**. GridSync managers can still use **Sync prices**.

The DE-LU feed shows published wholesale day-ahead prices for Germany and Luxembourg. Overview shows the last successful refresh and warns about old refreshes, failed loads, and gaps.

## Load your own tariff

GridSync managers can add a **Manual intervals**, **Time-of-use rules**, or **CSV import** source under Tariffs. Set the currency, timezone, and source reference from the tariff document or data supplier.

- **Time-of-use rules.** Set the tariff validity dates, then add rules under **Time-of-use rules**. Each rule has days, start and end times, a price per kWh, and optional season dates. Use local times such as 10:00, with 24:00 for the end of the day. Split overnight periods at midnight. Save and select **Generate intervals**. The dialog defaults to up to seven days within the validity dates and supports up to 92 days at a time. Overlapping rules stop generation. Uncovered intervals stay empty and are counted in load history. After editing rules or validity dates, generate the affected dates again.
- **CSV import.** Set the CSV price type if the supplier identifies the data as forecast or actual. Save and select **Import CSV**. Use the UTF-8 header `start,price_per_mwh,currency` and a timestamp with `Z` or an offset, such as `2025-06-02T10:00+04:00`. Enter amounts in the source currency per MWh with a decimal point. The importer accepts negative and zero prices and validates the whole file before writing. Duplicate timestamps inside a file are rejected. Re-importing the same rows leaves prices in place. Check **Replace existing** only if the file should replace different prices at the same timestamps.

## Historical tariff check

The [TAQA Distribution 2025 tariff booklet](https://www.addc.ae/en-US/business/Documents/2025%20Tariff%20%28English%29.pdf) lists 36.6 fils/kWh for peak hours and 27.0 fils/kWh outside peak hours for industrial customers over 1 MW. The peak window is 10:00 to 22:00 from June 1 through September 30, in Asia/Dubai. To check the historical summer example, use AED, enter 0.366 and 0.270 AED/kWh, and generate June 2 through June 8, 2025. The expected output is 672 intervals, 336 at each price. Verify current eligibility and the applicable tariff with the provider before using it for a real site.

Under that published schedule, a November day has 96 intervals at 0.270 AED/kWh. Shifting the same booked workload within a flat rate saves zero on the energy charge. For a live site, use the account's current rate schedule and check it against a recent bill. The [Abu Dhabi Energy Support Program](https://www.added.gov.ae/en/invest/growth-sectors/advanced-manufacturing/industrial-programmes-and-incentives) can change the rates a qualifying site pays.

[DEWA's industrial slabs](https://dewa.gov.ae/en/consumer/billing/slab-tariff) are based on monthly consumption. Entering the slabs as hours of the day would give the wrong cost comparison.


## Set up a work center for scheduling

Open **GridSync > Work centers**, select a work center, and open **Electricity**. A user with both Manufacturing Administrator and GridSync Manager access can enter operating power in kW, add its reference, and choose a tariff from the same company. **Allow schedule suggestions** starts off. Turn it on after choosing an active tariff and entering a positive power value. GridSync planners can view these settings.

## Create a schedule proposal

Open **GridSync > Schedule** and create a proposal. Choose a period of up to 14 days and work centers with schedule suggestions turned on. Their tariffs must use the same currency, and each work center needs fixed working hours. Select **Compute proposal**.

GridSync includes ready and waiting work orders from confirmed manufacturing orders. A work order is included if it is unplanned or planned to start in the period. Other bookings and time off stay in place. Operations keep their dependency order and finish before the manufacturing order deadline, a later planned operation, and the end of the period. They start on 15-minute marks, at the start of working hours or at their current time. Energy is counted only during working hours. Times with missing prices are not used.

Each line shows the current and proposed times, estimated energy and cost, and why the operation moved, stayed or could not be scheduled. The proposal shows current costs when the existing bookings are feasible and covered by prices. It keeps a feasible current schedule if the heuristic cannot find a cheaper complete one. A complete proposal can also show a lower bound. The lower bound assumes each operation gets its cheapest time without competing for it. The run saves the data used and cannot be recomputed. Select **New run with these settings** to try again. An operation that keeps its booking still blocks that time and constrains dependent operations. Computing a proposal leaves work orders in place.

If moving a booked start has a cost, set **Shift penalty per hour** on the work center in its tariff currency. Cost-based proposals weigh that penalty against the expected benefit. The proposal shows the penalty separately from the energy charge. Leave it at zero for the walkthrough below.

For two to six operations within one day, GridSync can check combinations of candidate start times. Look for **Search result** on the proposal. A completed search proves the best cost within those candidate times. If the search hits its limit, the proposal keeps the best complete schedule found so far.

If work centers in one proposal use different tariff currencies, GridSync shows proposal costs in the company currency. It uses the Odoo exchange rate dated on or before the start of the proposal window. The proposal notes show the rate and its date. Add a missing rate in Odoo before computing, and recompute if a rate changes before Apply. Saved meter results use the original site tariff currency.

For a work center on the Great Britain electricity grid, enable **Use GB carbon intensity** under **Electricity**. In **GB carbon intensity**, sync the UTC dates covered by the proposal. Choose **GB electricity emissions** as the schedule objective. The proposal shows estimated kg CO₂. The data notes say how much of the window is covered and how many intervals use forecasts. Missing intervals cannot be used, and Apply rechecks the intensity and whether each interval is a forecast or a published estimate.

Choose **GB cost and emissions** to account for both signals. Use a tariff for the same GB site in the company currency and enter the internal carbon price per tonne. GridSync adds the carbon charge when comparing placements and shows electricity cost and estimated emissions separately. An interval needs both signals to be usable.

A proposal with more than 500 operations goes into the background queue. Refresh the proposal to see its result. The worker computes one proposal at a time using the requesting planner's company access. If the period expires or access changes while it waits, the proposal shows the failure and can be copied into a new draft.

The local Odoo 19 load check used 2,000 test operations across 25 work centers. Queue submission took 0.28 seconds and the worker took 4.35 seconds to compute and save all 2,000 lines. The original booking dates stayed the same. These timings exclude test data setup and time waiting for cron to pick up the proposal.

## Apply or revert a proposal

For a complete proposal with moves, review the dates and costs, then select **Review and apply** and confirm. GridSync checks the saved work orders, prices, calendars and dependencies again before changing Odoo. If the proposal is stale, use **New run with these settings**. Use a new run for proposals computed before Apply was added.

Select **Review revert** on an applied proposal and confirm to restore the original dates. Originally unplanned operations go back to being unplanned. If an order has started, a booking is in the past, or a newer change makes the original dates unsafe, GridSync stops before writing. If another scheduling update is in progress, try again shortly.

## Check the energy cost after a run

After a moved operation is finished, a GridSync manager can open its applied proposal and select **Import readings** on the operation. Choose the site's tariff from the proposal and enter a meter reference. Use a meter that covers only that operation during the uploaded intervals. The run times come from Odoo's productive and performance logs.

Upload a UTF-8 CSV with the header `start,end,kwh`. Use timestamps with `Z` or an explicit offset and enter the kWh consumed in each interval. Intervals may be up to 15 minutes. They must cover the logged run times exactly, including any separate stretches of work. GridSync stops on gaps, overlaps, shared work-center time, or missing site prices. If an interval crosses a price change, its kWh are divided by time.

The result shows metered kWh and the calculated energy charge. When the same workload fits the original booking and those prices are available, it also shows the estimated charge at the original times. Open **Energy result** from the proposal or the work order to review it later.

Uploading the same readings again opens the saved result. A second import with different readings stops before changing the saved result.

The bill comparison is optional. Enter the energy-charge amount only when the bill line covers the same meter and continuous period as the CSV. Leave demand charges, taxes, and fixed fees out. Keep account and meter numbers out of references shared outside the company.


If the bill arrives later, open the saved **Energy result** as a GridSync manager and choose **Compare energy bill**. Enter the energy-only amount and dates for the same meter and exact continuous period. The result keeps the original calculated charge and records who saved the comparison and when. If you have Odoo Billing access, select **Use a posted vendor bill line** to use its untaxed subtotal. Choose the energy-only line for the same meter. Check the displayed vendor, invoice date, and untaxed subtotal, then enter the exact metered period before saving. GridSync saves the comparison without posting or editing a bill.

## Receive readings from a meter gateway

1. In Odoo, create a dedicated internal integration user for the site company. Give it **GridSync / Meter gateway** and assign its work centers on the **GridSync meter gateway** tab. Give the gateway an expiring API key and keep the key in its secret store. Use HTTPS on the target Odoo installation.
2. Configure the gateway to POST to `/json/2/grid.meter.batch/submit_readings` with the Odoo bearer key and the database header if the host needs it. The JSON body has four named fields: `workcenter_id` (integer Odoo ID), `meter_reference` (stable meter identifier), `batch_reference` (stable identifier for the delivery), and `csv_text` (the interval readings as a UTF-8 string with header `start,end,kwh`). Submit one batch covering the operation's productive intervals. See [Odoo JSON-2](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html) for request headers and access.
3. Save the returned batch ID in the gateway's delivery log. In Odoo, open **GridSync > Meter batches** to check the meter, work center, covered times and received time. A retry with the same reference and readings returns the same ID. A reused reference with different readings is rejected.
4. On an applied schedule with a completed operation, open **Import readings**, choose **Meter gateway**, and select the batch. Confirm that the meter covers the operation. The same coverage, tariff and overlap checks apply as for an uploaded file. The saved energy result shows the gateway batch reference so the planner can trace it back to the delivery log.

The meter gateway needs to measure and timestamp intervals before sending them. Check the first device's clock, meter assignment, upload retries, and matching Odoo run times on the site copy before enabling it on the live installation.


## First site pilot

1. Start on a copy of the site's Odoo database. Pick one work center with a dedicated meter and check its operating power, calendar, and tariff against the site's current rate schedule. Confirm that prices cover both the original and proposed times.
2. Ask the planner to review a real proposal against the production schedule. On the copy, apply it, check the dates in Manufacturing, and revert it before the work starts. Check that the original bookings return.
3. Check the first site's meter export against a finished operation's Odoo run log. The intervals must cover the logged run times exactly. If the export only has fixed 15-minute buckets and the job starts or ends mid-bucket, check whether the site can export matching readings. Keep the meter check open until the site can supply them.
4. Compare the imported kWh with the meter export. Independently price the readings using the verified site tariff and check the original-time estimate separately. Use a bill charge only when it covers the same meter and period. Have the planner sign off on the tariff, meter result, and Apply and Revert rehearsal. Record the tariff revision, dates, planner, and any differences in the site's own system.
5. On the target installation, restore a backup to an isolated copy and confirm that the schedules and meter results open. Repeat Apply and Revert at the site's normal workload and note the timings and any retry messages. Trigger a controlled failure on the copy and confirm that the person responsible receives it through the site's monitoring setup. The local rehearsal covered 120 test work orders. Use the target site's workload for sign-off.

The Phase 6 local restore rehearsal used an Odoo ZIP with its filestore. All 9 proposals, 2 meter results (including a saved bill comparison), and 1 test gateway batch matched the source database. A second restore on an isolated copy verified that a posted test vendor bill, its bill line, and the linked GridSync comparison survived together. Repeat these checks on the target before signing off. On Windows, point Odoo's `pg_path` to the PostgreSQL `bin` directory if it cannot find `pg_dump`.

## Test the full workflow in Odoo

Use **New** from a list. **Save** on a new record takes you back to the list, where you can reopen it to generate prices, compute a proposal, or confirm a manufacturing order. When editing an existing record, **Save** keeps you on that record. Save the bill of materials before adding its first operation. The empty Operations tab has an Add Operation button.

Use an isolated Odoo 19 database with GridSync installed or upgraded and work orders enabled in Manufacturing. Sign in as a GridSync Manager with Manufacturing Administrator access. The rates and readings below are test inputs. Use one work center and keep other bookings out of its test window.

1. In **GridSync > Tariffs**, create a **Time-of-use rules** tariff in AED. Pick a date and a future one-hour window aligned to 15 minutes, within the work center's working hours. Use the same timezone when entering the tariff, booking and proposal. Set the tariff validity to that day and enter `Local walkthrough test rates` as its source. Add an **Every day** rule for the first 15 minutes at **0.270 AED/kWh** and another for the remaining 45 minutes at **0.366 AED/kWh**. Generate intervals for that day. Expect four prices in the test window. Hours outside that window have no prices.
2. In **GridSync > Work centers**, open **Electricity**, enter **40 kW** and `Local test input` as its reference, select the tariff and turn on **Allow schedule suggestions**. In **GridSync > Sites**, create a test site and link the work center.
3. In **Manufacturing > Products > Bills of Materials**, create a manufacturing BoM for a test product, save it, and reopen it. Under **Operations**, click **Add Operation**. Select the test work center, set a manual duration of 15 minutes, and save. Reopen the BoM to check the operation. Create and confirm a manufacturing order for one unit using that BoM. In **Work Orders**, open the operation and set its start and end to the second 15-minute slot. Set any manufacturing deadline at or after the end of the window. Write down the original start and end.
4. In **GridSync > Schedule**, select the work center and that one-hour window, then click **Compute proposal**. Expect the first slot to be proposed, with **AED 3.66** at the current time, **AED 2.70** at the proposed time, and **AED 0.96** in estimated savings. Open **Review and apply** and cancel once to confirm that the booking stays put. Reopen it, confirm, and check that Manufacturing shows the new dates.
5. Before either booking starts, use **Review revert** and confirm. Check that Manufacturing shows the exact original dates. Choose **New run with these settings**, compute a new proposal, then apply it. Leave this second proposal applied for the meter check.
6. Once the cheap slot begins, start the work order in Manufacturing. For this test, run it for a minute or two and finish it well before the slot ends. Open the work order's **Time Tracking** tab and use its actual productive start and end, including seconds. Odoo can display dates without seconds. If you can't see the exact values, a Manufacturing Administrator can set the two timestamps explicitly to whole minutes on the test log, within the cheap slot and after Apply. Use those saved test timestamps. Keep real production logs as recorded. The short test run still fits when moved back to the original 15-minute booking. The CSV below illustrates a run from 14:01:12 to 14:03:20 Dubai time. Replace both timestamps with your log times and use 10 kWh as the test input:

   ```csv
   start,end,kwh
   2026-11-13T14:01:12+04:00,2026-11-13T14:03:20+04:00,10
   ```

7. Open the applied proposal and click **Import readings** beside the completed operation. Upload the test CSV, enter `Test press meter` as the meter reference, and confirm the dedicated-meter checkbox. For the optional bill check, enter a test reference, the same start and end, and **AED 2.70** as the energy charge. Click **Import and calculate**.
8. Open **Energy result** from the proposal and from the work order. Expect **10 kWh**, an **AED 2.70** calculated charge, an **AED 3.66** original-time estimate, and **AED 0.96** estimated savings. If you entered the matching bill charge, expect a **0.00** difference. A completed operation should refuse Revert and keep its existing dates.

On a separate test run, try changing a work-order date after Compute and confirm that Apply refuses the stale proposal. Before importing valid readings, try a CSV with a missing or repeated interval and confirm that GridSync saves no result.
