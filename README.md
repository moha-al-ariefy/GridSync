# GridSync MRP

GridSync MRP is an Odoo 19 app that finds better times to run work orders using electricity prices or Great Britain grid carbon intensity.

For a UAE factory, load the electricity tariff that applies to the site. Energy-Charts supplies wholesale prices for supported European markets.

## Status

The add-on supports automatic and manual price sync for five Energy-Charts zones, plus manual intervals, time-of-use rules, and tariff CSV imports. The scheduler suggests lower-cost times for open work orders and explains each result. Planners can review and apply the proposed moves, then restore the original bookings if the schedule still allows it.

The [roadmap](ROADMAP.md) covers the build order. The [build plan](docs/GridSync_Build_Plan.pdf) has the original idea and priorities, and [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) has the technical details and things to verify. See [local setup](docs/LOCAL_SETUP.md) for development and test instructions.

For a moved work order that has finished, a GridSync manager can import meter intervals and calculate the energy charge using the site tariff. The saved result shows metered kWh and, when the readings fit the original booking, the estimated cost at the original times. A site can compare the result with a matching bill charge.

## Principles

- Use real, permitted data or user-entered inputs with a source. Clearly label forecasts, estimates and old or missing data.
- Show proposed schedules before applying them. Check for conflicts and make sure changes can be reverted.
- Keep credentials, customer data and database backups out of Git.
- Build on Odoo 19 Manufacturing and check the installed edition and APIs first.

## License

The GridSync add-on is licensed under [LGPL-3](LICENSE). See [GPL-3](COPYING) for the terms it builds on.
