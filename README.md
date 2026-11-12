# GridSync MRP

GridSync MRP is an Odoo 19 manufacturing app I plan to build for the hackathon. It will help planners find better times to run production based on electricity prices and, when reliable data is available, carbon intensity.

The UAE use case will use applicable published or customer-provided tariffs. The live Germany-Luxembourg price feed is a separate example. Price and carbon data need to match the same market and time period.

## Status

The add-on is in development. It includes site setup, an electricity price overview, access controls and automatic and manual sync for Germany-Luxembourg day-ahead prices. These are wholesale market prices, not a factory's electricity bill. Tariff sources also support manual intervals, time-of-use rules, and validated CSV imports. The scheduler suggests lower-cost times for open work orders and explains each result. Planners can review and apply a complete proposal, then restore the original bookings if the schedule still allows it.

The [roadmap](ROADMAP.md) covers the build order. The [build plan](docs/GridSync_Build_Plan.pdf) has the original idea and priorities, and [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) has the technical details and things to verify. See [local setup](docs/LOCAL_SETUP.md) for development and test instructions.

## Principles

- Use real, permitted data or user-entered inputs with a source. Clearly label forecasts, estimates and old or missing data.
- Show proposed schedules before applying them. Check for conflicts and make sure changes can be reverted.
- Keep credentials, customer data and database backups out of Git.
- Build on Odoo 19 Manufacturing and check the installed edition and APIs first.
