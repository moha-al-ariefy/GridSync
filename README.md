# GridSync MRP

GridSync MRP is an Odoo 19 manufacturing app I plan to build for the hackathon. It will help planners find better times to run production based on electricity prices and, when reliable data is available, carbon intensity.

The UAE use case will use applicable published or customer-provided tariffs. A Germany-Luxembourg electricity price feed is planned as a separate live-data example. Price and carbon data need to match the same market and time period.

## Status

The add-on is in development. It includes site setup, a getting-started guide, access controls and a manual sync for Germany-Luxembourg day-ahead prices. These are wholesale market prices, not a factory's electricity bill. Tariff profiles and the scheduler are still being built, so there are no verified savings or emissions results.

The [roadmap](ROADMAP.md) covers the build order. The [build plan](docs/GridSync_Build_Plan.pdf) has the original idea and priorities, and [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) has the technical details and things to verify. See [local setup](docs/LOCAL_SETUP.md) for development and test instructions.

## Principles

- Use real, permitted data or user-entered inputs with a source. Clearly label forecasts, estimates and old or missing data.
- Show proposed schedules before applying them. Check for conflicts and make sure changes can be reverted.
- Keep credentials, customer data and database backups out of Git.
- Build on Odoo 19 Manufacturing and check the installed edition and APIs first.
