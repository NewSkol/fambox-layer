# FamBox family layer

The family dashboard that runs on every FamBox box (https://fam.box): the family's day, tasks,
and Pip's suggestions, live on every screen in the house.

Installed and updated by the box itself, from signed version tags only: the box checks each tag's
signature against FamBox's release key before it uses it, and takes a copy of the family's data
first so a failed update puts everything back.

- `dashboard.py`: the dashboard (Python standard library plus `psycopg`)
- `welcome.py`: moving in (onboarding) and sign-in by one-time link
- `pip.py`: Pip: simple rules first, the family's AI for the rest; on Telegram through the family's own bot, in private
  chats and the family's group (consent per person); letter photos to dates; a morning message per person
- `calsync.py`: two-way calendars: the table kept in step with one iCloud calendar (CalDAV, app-specific password) and/or
  one Google calendar (Calendar API, OAuth; `docs/google-calendar-setup.md`), every few minutes and soon after a change
- `ai.py`: Pip's AI (copied from `relay/client.py`): the FamBox AI relay, or the family's own key straight to its provider
- `schema.sql`: the family's tables, in their own `family` schema
- `static/`: styles, fonts (SIL Open Font License, see `static/fonts/OFL-*.txt`) and live updates
- `install.sh`: what the box runs for each new release (`--dry-run` shows the steps)
