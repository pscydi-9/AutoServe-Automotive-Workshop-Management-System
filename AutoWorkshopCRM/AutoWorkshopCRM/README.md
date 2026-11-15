# AutoServe Pro — Automotive Workshop CRM & Service Planner

A desktop CRM for vehicle service workshops, built with a strict MVC architecture. The UI is a responsive CustomTkinter layout that also works on Android through **Pydroid 3**.

## Quick start

```bash
pip install -r requirements.txt     # customtkinter, bcrypt, tkcalendar (Linux: sudo apt install python3-tk)
cp .env.example .env                # optional: live Gmail OTP + your admin e-mail
python main.py                      # launch the GUI
python main.py --check              # headless smoke test (schema, seed, e-mail mode)
python main.py --reset              # delete and re-seed the local SQLite DB
python -m unittest discover -s tests -v
```

**Sign-in is two-step.** First enter a username and password (verified with bcrypt). Then enter the 6-digit code sent to the account's e-mail, or to its phone through the SMS mock. The default login is `admin` / `Admin@123`. Change it in **Admin Settings** or with `ADMIN_USERNAME` / `ADMIN_PASSWORD` before the first launch.

### Live Gmail OTP

1. Turn on 2-Step Verification on the sending Google account, then create an App Password at <https://myaccount.google.com/apppasswords>.
2. Put the values in `.env`. Real environment variables override the file. `.env` is git-ignored.
   ```
   EMAIL_SENDER=your.workshop@gmail.com
   EMAIL_APP_PASSWORD=abcd efgh ijkl mnop
   ADMIN_EMAIL=your.real.inbox@gmail.com
   ```
3. Run `python main.py --check`. The `OTP e-mail` line should say `LIVE via smtp.gmail.com:587`.

If `EMAIL_SENDER` or `EMAIL_APP_PASSWORD` is missing, e-mail falls back to **mock mode**: the code is printed in the terminal as JSON. Phone OTP is always the JSON-logging mock.

`ADMIN_EMAIL` is applied when the admin account is first created. On an existing database it replaces the placeholder `admin@autoserve.local`. After that, change the e-mail in Admin Settings.

### Upgrading from v1

Existing `workshop.db` files are migrated automatically (`database/migrations/002_security_catalog.sql`, run in one transaction). The migration adds:
- usernames and bcrypt password hashes (the admin gets the seed credentials)
- the repairs catalogue
- `Service_Jobs.service_id`

It also normalises phone numbers to 10 digits. Demo vehicles are topped up to 50+.

## Architecture

```
core/          config.py (frozen dataclass config, theme, secrets via env)
               db_manager.py (connection pool + transaction context manager)
               notification_service.py (async smtplib e-mail, mock/real SMS)
models/        auth_model.py (HMAC OTP cache, lockout, session tokens)
               workshop_model.py (inventory math, scheduling, billing, ACID allocation)
controllers/   auth_controller.py, workshop_controller.py, base_controller.py
views/         components.py (cards, fields, PhoneEntry, DatePickerField, grid, table)
               login_view.py (password → OTP), dashboard_view.py (7 pages incl. Admin Settings)
database/      schema_sqlite.sql (runtime v2), schema_postgresql.sql (reference 3NF DDL),
               migrations/ (versioned upgrade scripts)
tests/         unit tests: transactions, OTP security, ACID and concurrency
main.py        composition root / entry point
```

Dependencies only point one way: **View → Controller → Model → core**. Views never import `sqlite3` or run SQL. Controllers turn domain exceptions into `OperationResult` objects that the UI can display.

## Key engineering points

| Concern | Implementation |
|---|---|
| Transactions | `DatabaseManager.transaction()` runs `BEGIN IMMEDIATE`, then `COMMIT`, or `ROLLBACK` on any exception |
| Inventory ACID | `WorkshopModel.allocate_parts` checks the job is active, decrements stock with a guarded `UPDATE ... WHERE stock_quantity >= ?`, upserts `Parts_Used` with a unit-price snapshot, and recalculates `total_bill`. Everything happens in one transaction. A concurrency test confirms stock is never oversold |
| Passwords | bcrypt (cost 12) hashes only. Five wrong passwords lock the username for 5 minutes. Unknown users still run a dummy bcrypt check so response time doesn't reveal which usernames exist. Usernames are whitelisted with a regex, and every SQL statement is parameterised |
| OTP security | Codes come from the `secrets` CSPRNG. Only HMAC-SHA256 digests are kept in memory. TTL is 299 s (at most 5 minutes). Codes are compared in constant time, are single-use, lock out after 5 attempts, and have a 30 s resend cooldown. Each code is tied to one purpose and one account, so a login code can't approve a credential change |
| Credential changes | Admin Settings needs the current password. A fresh OTP goes to the **current** e-mail. The parameterised `UPDATE` runs only after that code is verified, and all sessions are then rotated |
| Phone validation | `PhoneEntry` uses a Tk `validatecommand` that runs on every keystroke and only allows `^[0-9]{0,10}$`. On submit the value must match `^[0-9]{10}$`. The model and the SQLite `CHECK` constraints enforce the same rule |
| Dates | Job booking and the schedule use a `tkcalendar` pop-up (`DatePickerField`). Past dates can't be booked |
| Mechanic delete | Runs in one `BEGIN IMMEDIATE` transaction. It is blocked if the mechanic has a job In Progress, unless you choose someone to reassign active job cards to. Pending jobs are unassigned and history is detached before the `DELETE` |
| Non-blocking mail | `smtplib` (STARTTLS/SSL) runs on a `ThreadPoolExecutor`. Results are sent back to the Tk main loop through a queue-based dispatcher |
| SMS mock | `WORKSHOP_SMS_MOCK=1` prints structured JSON request/response diagnostics with masked recipient and authorization fields |
| Responsive UI | Breakpoints are mobile < 760 px and tablet < 1150 px. The sidebar becomes a top-bar menu, grids reflow, and low-priority table columns are hidden |

## Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `WORKSHOP_DB_PATH`, `WORKSHOP_DB_POOL_SIZE` | SQLite location and pool size |
| `EMAIL_SENDER`, `EMAIL_APP_PASSWORD` | Live Gmail SMTP for OTP e-mails |
| `ADMIN_EMAIL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_PHONE` | Seed administrator |
| `WORKSHOP_SECRET_KEY` | HMAC key (a random key is generated per process if unset) |
| `WORKSHOP_SMTP_HOST/PORT/SENDER/USE_SSL` | SMTP relay overrides (default `smtp.gmail.com:587` STARTTLS) |
| `WORKSHOP_EMAIL_MOCK`, `WORKSHOP_SMS_MOCK` | Force mock dispatch (`1`/`0`) |
| `WORKSHOP_ADMIN_NAME` | Seed administrator display name |
| `WORKSHOP_APPEARANCE` | `dark` / `light` / `system` |

For Gmail, use an App Password. Never commit credentials.

## Pydroid 3

Copy the folder to your device, install `customtkinter`, `bcrypt` and `tkcalendar` from Pydroid's pip menu, and run `main.py`. On Android the app opens full-screen with mobile scaling.
