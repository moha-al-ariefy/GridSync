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

This version is the foundation only. It does not fetch electricity prices or change manufacturing schedules yet.
