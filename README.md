# FDC Flask App

Internal operations platform for Fashion Distribution Center: inventory, orders, shipping, DAM, client portal, and warehouse tools.

## Run locally

1. Use Python 3.11 and the project venv.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy secrets into `nssm-fdcef-environment.env` (gitignored). Required for a full boot:
   - `SECRET_KEY`
   - `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`
   - `SQLSERVER_HOST` / `SQLSERVER_USER` / `SQLSERVER_PASSWORD`
4. Start: `python app.py` (port 5555). Interactive runs enable the Werkzeug reloader; the NSSM service does not.

## Production (NSSM)

The Windows service loads the same env file before `Config`. Set `FDC_REQUIRE_SECRETS=1` so missing DB/secret keys fail at startup.

Staff also open the app over LAN HTTP (`http://192.168.254.30:5555`). Leave `FDC_SESSION_COOKIE_SECURE` unset so session cookies still work on LAN. Public HTTPS is terminated at Cloudflare.

After moving secrets out of `setting.py`, rotate Slack tokens, SMTP, and any password that previously lived in git.

### Attendance Wi-Fi poller (separate service)

Presence polling can run outside `fdcef` so a hung web process does not freeze Online/Slack:

1. `powershell -ExecutionPolicy Bypass -File auto_run\Install-AttendancePoller.ps1`
2. Set `ATTENDANCE_EXTERNAL_POLLER=1` in `nssm-fdcef-environment.env`
3. Restart `fdcef` (disables the in-process thread) and keep `fdcef-attendance` running

Manual: `python py\standalone\attendance_poller.py`

## Auth and client portal

- Passwords are stored with bcrypt. Existing plaintext rows are upgraded on the next successful login.
- `/delete_user` and user admin routes are yeti-only.
- Client portal users belong to the `client` group. Prefer `seed_client_portal_group()` over `sql/seed_client_portal.sql`.
- Shared TK / external stock passwords are retired; those tools require a normal login.
- Whatnot live ingest fails closed unless `WHATNOT_LIVE_INGEST_TOKEN` is set. Put the same token in the Chrome extension popup.

## Optional modules

`py/utilities/module_loader.py` loads feature blueprints independently. A failed import becomes a 503 for that tool instead of taking the whole app down. `/health` reports loaded vs failed modules.

## SQL scripts

`sql/` is applied manually (no Alembic). Typical order:

- `sql/add_users_*.sql` when adding user columns
- App startup also creates `user_groups` if missing
- `sql/seed_client_portal.sql` only inserts a missing Client group; it does not write plaintext passwords

## Logs

Rotating logs live under `output/logs/` (`system.log`, `error.log`, `user.log`, `visitor.log`, `online.log`). Keep `output/` out of git.

## Tests

```
python scripts/run_tests.py
python scripts/check_locales.py
```

Do not install pytest into the app venv. Pytest ships a `py.py` shim that shadows this repo's `py/` package.
