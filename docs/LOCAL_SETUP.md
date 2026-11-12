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

## Run the tests

On a new test database, install with `-i mrp_grid_scheduler --without-demo=all --test-enable --test-tags /mrp_grid_scheduler --stop-after-init --no-http`. To repeat the tests against that isolated database, use `-u mrp_grid_scheduler` instead of `-i`. Check the Odoo log and process exit code; the test users and records are test fixtures, not customer data.

## First steps in GridSync

Give a manufacturing user the GridSync Planner role, or use the GridSync Manager role for site administration. Open GridSync, create a site and select its work centers. Planner access is limited to allowed companies; only a manager can delete a site.

Under Tariffs, a GridSync Manager can add an Energy-Charts DE-LU source and select **Sync prices**. The source displays its license, attribution, sync time and 15-minute EUR/MWh intervals. A repeat sync updates existing intervals rather than duplicating them. Missing prices are not filled with zero. The sync uses the public Energy-Charts API and does not require an API key.

Automatic sync is on by default for each company. Odoo checks every five minutes while its scheduled actions are running. Opening Overview or Tariffs also checks for a refresh if the last attempt was at least five minutes ago. An Odoo administrator can turn it off under **GridSync Å® Settings**. GridSync managers can still use **Sync prices**.

The DE-LU feed is a wholesale day-ahead market price, not a UAE tariff or a customer's electricity bill. Manual intervals can be entered with their source reference; recurring tariff rules, CSV import and production scheduling are still being built. Price data is never used to change work orders in this version.
