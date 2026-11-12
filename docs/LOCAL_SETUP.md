# Local setup

This setup is for the Odoo 19 workshop installation on Windows. Keep it separate from real customer data.

## Before starting

- Start PostgreSQL and make sure the Odoo workshop installation is available.
- Use a separate database for GridSync development and another fresh database for tests.
- Keep passwords in the local Odoo configuration or another private credential store. Do not put them in this repository or in command arguments.
- The installed Odoo service and its access settings are managed separately. Check who can reach it before using real data.

## Install GridSync for development

Run the installed Odoo Python and `odoo-bin` from the workshop server directory. Pass the existing Odoo configuration with `-c`, the workshop add-on directories plus this repository with `--addons-path`, and the development database with `-d`. Install the module with `-i mrp_grid_scheduler --without-demo=all --stop-after-init --no-http`.

The add-on path is a comma-separated list. Include all required Odoo and workshop directories when overriding it; otherwise installed modules may not be found. The exact paths depend on the installation.

To open the add-on without changing the workshop service, run a separate local Odoo process with the same configuration and add-on paths, `--http-interface=127.0.0.1`, a free `--http-port`, a database filter for the development database and `--no-database-list`. Stop that process when finished. Do not run tests against a shared or production database.

For the loopback-only Windows preview, use `--limit-time-real=0 --limit-time-real-cron=120` if Odoo stops after an idle browser connection. Odoo can count time spent waiting for the browser's first HTTP request against its request deadline. Keep the preview bound to `127.0.0.1` and filtered to the development database.

## Run the tests

On a new test database, install with `-i mrp_grid_scheduler --without-demo=all --test-enable --test-tags /mrp_grid_scheduler --stop-after-init --no-http`. To repeat the tests against that isolated database, use `-u mrp_grid_scheduler` instead of `-i`. Check the Odoo log and process exit code; the test users and records are test fixtures, not customer data.

## First steps in GridSync

Give a manufacturing user the GridSync Planner role, or use the GridSync Manager role for site administration. Open GridSync, create a site and select its work centers. Planner access is limited to allowed companies; only a manager can delete a site.

Under Tariffs, a GridSync Manager can add an Energy-Charts DE-LU source and select **Sync prices**. The source displays its license, attribution, sync time and 15-minute EUR/MWh intervals. A repeat sync updates existing intervals rather than duplicating them. Missing prices are not filled with zero. The sync uses the public Energy-Charts API and does not require an API key.

Automatic sync is on by default for each company. Odoo checks every five minutes while its scheduled actions are running. Opening Overview or Tariffs also checks for a refresh if the last attempt was at least five minutes ago. An Odoo administrator can turn it off under **GridSync > Settings**. GridSync managers can still use **Sync prices**.

The DE-LU feed shows published wholesale day-ahead prices for Germany and Luxembourg. Overview shows the last successful refresh and warns about old refreshes, failed loads, and gaps. Price data is never used to change work orders in this version.

## Load your own tariff

GridSync managers can add a **Manual intervals**, **Time-of-use rules**, or **CSV import** source under Tariffs. Set the currency, timezone, and source reference from the tariff document or data supplier.

- **Time-of-use rules.** Set the tariff validity dates, then add rules under **Time-of-use rules**. Each rule has days, start and end times, a price per kWh, and optional season dates. Use local times such as 10:00, with 24:00 for the end of the day. Split overnight periods at midnight. Save and select **Generate intervals**. The dialog defaults to up to seven days within the validity dates and supports up to 92 days at a time. Overlapping rules stop generation. Uncovered intervals stay empty and are counted in load history. After editing rules or validity dates, generate the affected dates again.
- **CSV import.** Set the CSV price type if the supplier identifies the data as forecast or actual. Save and select **Import CSV**. Use the UTF-8 header `start,price_per_mwh,currency` and a timestamp with `Z` or an offset, such as `2025-06-02T10:00+04:00`. Enter amounts in the source currency per MWh with a decimal point. The importer accepts negative and zero prices and validates the whole file before writing. Duplicate timestamps inside a file are rejected. Re-importing the same rows leaves prices in place. Check **Replace existing** only if the file should replace different prices at the same timestamps.

## Historical tariff check

The [TAQA Distribution 2025 tariff booklet](https://www.addc.ae/en-US/business/Documents/2025%20Tariff%20%28English%29.pdf) lists 36.6 fils/kWh for peak hours and 27.0 fils/kWh outside peak hours for industrial customers over 1 MW. The peak window is 10:00 to 22:00 from June 1 through September 30, in Asia/Dubai. To check the historical summer example, use AED, enter 0.366 and 0.270 AED/kWh, and generate June 2 through June 8, 2025. The expected output is 672 intervals, 336 at each price. Verify current eligibility and the applicable tariff with the provider before using it for a real site.


## Set up a work center for scheduling

Open **GridSync > Work centers**, select a work center, and open **Electricity**. A user with both Manufacturing Administrator and GridSync Manager access can enter operating power in kW, add its reference, and choose a tariff from the same company. **Allow schedule suggestions** starts off. Turn it on after choosing an active tariff and entering a positive power value. GridSync planners can view these settings.
