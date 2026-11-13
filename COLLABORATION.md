# Working on GridSync

## Before touching code

- Check the roadmap and say what part you're picking up so nobody builds the same thing twice.
- Start a branch from main. Keep the change small enough that someone can actually review it.
- Use your own Odoo 19 test database. Keep API keys, customer files, and backups out of Git.

## Before asking for review

- Run the tests in the [setup guide](docs/LOCAL_SETUP.md). If you changed a screen, open it in Odoo and go through the flow yourself.
- If the change moves work orders, try Apply and Revert on a copy first. Check what happens if someone edits a booking after Compute.
- Label test rates and readings. If a number is an estimate, say so where it shows up.
- Keep comments short. Explain why the code needs the extra check if the code itself doesn't make it obvious.
- Send the diff, the commit message, and what you actually tested. If something still needs checking, say what it is. Get it reviewed before pushing to main.
