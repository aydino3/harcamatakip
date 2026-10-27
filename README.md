# Harcama Takibi

A small Turkish expense tracking and monthly budgeting app. Business logic and storage use Python and SQLite; the HTTP adapter uses Flask and the production server uses Waitress.

## Run

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python3 app.py
```

## Vercel free deployment

Vercel's function filesystem is temporary. For a usable deployment, connect the project to Supabase through the Vercel Marketplace integration. It supplies `POSTGRES_URL`. For an application-managed database or a region migration, set the server-only Secret `EXPENSE_DATABASE_URL` to the new Supabase transaction-pooler URL. Connection priority is `EXPENSE_DATABASE_URL`, then `POSTGRES_URL`, then `DATABASE_URL`; this prevents an older Marketplace integration from redirecting a migrated app. Never commit the connection string. `vercel.json` pins Functions to Frankfurt (`fra1`), beside the Frankfurt Supabase database. The SQLite fallback under `/tmp` is only suitable for a disposable smoke test: accounts and expenses can disappear between function instances or deployments.

Recommended Vercel setup:

1. Import this repository into Vercel.
2. In the Vercel Marketplace, connect the Supabase project to this Vercel project. Confirm that `POSTGRES_URL` is present for Production. Do not copy database credentials into source files. Add the application settings below as well:

```env
APP_ENV=production
PRIVATE_BETA=0
COOKIE_SECURE=1
TRUST_PROXY=1
```

`VERCEL` is supplied by Vercel automatically. Do not set `EXPENSE_DB_PATH` or `EXPENSE_BACKUP_DIR` for the Supabase deployment. Keep `PRIVATE_BETA=1` only when you want invite-only registration; for a public beta, set it to `0`.

3. Deploy the project from the repository root. The `vercel.json` file routes all requests to the Flask app in `api/index.py`.

If you want a private beta, generate an invitation in a trusted shell after deployment:

```sh
APP_ENV=production PRIVATE_BETA=1 python app.py invite create --expires-hours 72
```

This keeps the public URL reachable but restricts account signup to valid one-time invitation codes.

Open http://127.0.0.1:8000. The server binds to `127.0.0.1` by default and creates the SQLite database at `data/expenses.sqlite3`. To use another local port or database path:

```sh
APP_PORT=8001 EXPENSE_DB_PATH=/path/to/private/expenses.sqlite3 python3 app.py
```

For a local private-beta run, start with `PRIVATE_BETA=1 python3 app.py` and create invitations with the operator commands below. Omitting the flag preserves open self-registration for the existing local-development workflow.

On macOS/Linux, the default data directory is restricted to the current user and the database file is set to mode `0600`. If you set `EXPENSE_DB_PATH`, keep its parent directory private too. See [Backup and restore](#backup-and-restore) for operator commands and recovery warnings.

### Development and production configuration

The default is local development (`APP_ENV=development`) on `127.0.0.1:8000`. Supported settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Environment label. `production` refuses the development entry point and is required by `production.py`. |
| `PRIVATE_BETA` | `0` in development; required in production | Set to `1` to disable open registration and require an operator-issued invitation. Production accepts only explicit `0` or `1`. |
| `APP_HOST` | `127.0.0.1` | Development server bind address; keep loopback for local use. |
| `APP_PORT` | `8000` | Development server port. |
| `EXPENSE_DB_PATH` | `data/expenses.sqlite3` | SQLite database path. Production requires an absolute path inside `PERSISTENT_DATA_DIR`. |
| `PERSISTENT_DATA_DIR` | unset | Production requires an existing mount root. `REQUIRE_PERSISTENT_MOUNT=1` verifies it is detected as a mounted filesystem before DB creation. |
| `REQUIRE_PERSISTENT_MOUNT` | `1` in production | Keep enabled in deployment. Set to `0` only for a local temporary-disk production-server smoke test. |
| `EXPENSE_BACKUP_DIR` | beside the configured database in `backups/` | Operator backup location. Production requires an absolute path on the configured persistent disk and outside public/static files. |
| `EXPENSE_BACKUP_KEY` | unset | Required only for encrypted backup/restore. A URL-safe base64 encoding of 32 random bytes. Store separately from backups in an operator secret manager/password manager. |
| `COOKIE_SECURE` | `0` | Set to `1` only when using HTTPS. Production mode always marks session cookies Secure. |
| `PORT` | Render supplies it; otherwise `10000` | Production Waitress TCP port. |
| `WEB_CONCURRENCY` | `4` | Waitress worker threads, constrained to 1–16. |
| `TRUST_PROXY` | `0` | Set to `1` only behind Render's Cloudflare edge; rate limits then use its overwritten `CF-Connecting-IP` header. |

There are currently no application signing keys or production credentials to configure; the server-side random session token is stored as a hash in SQLite. The backup key is separately required only for backup operations. Do not add secret values to source control. `python3 app.py` runs Flask's development server on loopback. `APP_ENV=production python3 app.py` deliberately exits. Production uses the included Waitress WSGI entry point, validates configuration and the persistent database directory before accepting requests, and sets Secure cookies. `/healthz` returns only `{"status":"ok"}` after a database check. This does not establish production readiness: the rate limiter is for a single instance, and operational monitoring, encrypted/off-site backup handling, deployment rehearsal, and a security review remain necessary.

### Private beta invitations

Open self-registration remains enabled in local development unless `PRIVATE_BETA=1` is set. Production startup requires `PRIVATE_BETA` to be explicitly set to `0` or `1`; the Render Blueprint sets it to `1`. In private-beta mode, the registration form requires an invitation code. Login, session cookies, CSRF checks, and per-user authorization continue to use the existing authentication flow.

Create an invitation from a trusted operator shell in the project directory, with the same application/database environment as the running app:

```sh
PRIVATE_BETA=1 python3 app.py invite create --expires-hours 72
```

The command prints the raw bearer token once to that operator's terminal. Deliver it to its intended beta user through a private channel; do not paste it into tickets, source code, logs, or public chat. The database stores only its SHA-256 hash. Expiry must be between 1 and 720 hours (default 72 hours). Invitations are single-use and are consumed only when account creation succeeds; failed registration attempts do not consume a valid invite. Duplicate account creation is protected by a transaction.

Revoke an unused invitation using the numeric invitation ID printed at creation:

```sh
PRIVATE_BETA=1 python3 app.py invite revoke 12
```

The raw token cannot be shown again. If it is lost, revoke its invitation ID and create a replacement. Used, expired, and revoked invitations cannot be recovered or reused. The invitation is not bound to an email address: anyone who obtains the bearer token before it is used can claim the one account it authorizes. Protect operator shell access and share tokens carefully. These commands are local operator commands, not HTTP endpoints. For production, run them in an authorized operator shell with the same `APP_ENV=production`, persistent disk, database path and mount variables used by the service; startup validation applies before the command can access the database.

For the Render disk layout in `render.yaml`, an authorized operator shell can use:

```sh
APP_ENV=production PRIVATE_BETA=1 PERSISTENT_DATA_DIR=/var/data REQUIRE_PERSISTENT_MOUNT=1 EXPENSE_DB_PATH=/var/data/expenses.sqlite3 EXPENSE_BACKUP_DIR=/var/data/backups PORT=10000 python3 app.py invite create --expires-hours 72
APP_ENV=production PRIVATE_BETA=1 PERSISTENT_DATA_DIR=/var/data REQUIRE_PERSISTENT_MOUNT=1 EXPENSE_DB_PATH=/var/data/expenses.sqlite3 EXPENSE_BACKUP_DIR=/var/data/backups PORT=10000 python3 app.py invite revoke 12
```

The operator shell must have the same mounted volume attached and permission to access the project and database. Do not set `REQUIRE_PERSISTENT_MOUNT=0` for a deployed operator command.

Install the pinned dependencies using `python -m pip install -r requirements.txt`. `.python-version` pins the Render interpreter. `production.py` starts Waitress; `wsgi.py` exports the WSGI app and refuses import unless `APP_ENV=production`. A minimal Render Blueprint is in `render.yaml`; it defines one web service and one persistent disk but has not been deployed or verified on Render. For local production-server smoke testing, create an existing writable data directory first, then run:

```sh
APP_ENV=production PERSISTENT_DATA_DIR=/tmp/expense-data REQUIRE_PERSISTENT_MOUNT=0 EXPENSE_DB_PATH=/tmp/expense-data/expenses.sqlite3 EXPENSE_BACKUP_DIR=/tmp/expense-data/backups PORT=10000 COOKIE_SECURE=1 TRUST_PROXY=0 python production.py
```

Production startup intentionally fails when a persistent mount root is absent, not detected as a mount, or not writable; when the database path is not absolute/inside the mount; or when backups are outside the mount. It does not create a missing production mount. Once initialized, a private marker on the disk makes startup refuse to bootstrap an empty database if the DB file later disappears or becomes incompatible. The local smoke command explicitly disables only the mountpoint check because `/tmp` is not a mounted persistent volume; never deploy with that override. For Render, the blueprint expects the persistent disk mounted at `/var/data`; verify that mount and its permissions in the service before accepting traffic. `TRUST_PROXY=1` trusts `CF-Connecting-IP` for rate-limit keys; Render documents Cloudflare edge ingress, and Cloudflare documents this header for edge-to-origin requests. The application ignores caller-controlled `X-Forwarded-For`. This provider-specific behavior has not been confirmed against a live Render service; do not enable it behind another ingress without separately verifying header behavior. [Render proxy/IP guidance](https://render.com/articles/how-render-handles-ddos-attacks).

### Authentication rate limits

Failed authentication actions are stored as SHA-256 digests of the source address and attempted identity, not raw IP/email values. Per source/identity pair, the limits are 20 login attempts, 10 registration attempts, and 10 account-deletion password confirmations per rolling 15 minutes; additional per-source caps are 100, 30, and 50 respectively. A successful corresponding action clears that source's counters. Login returns the same 401 message whether the email is unknown or the password is wrong; a blocked key receives a generic 429. Unknown-user login attempts also run the password hash check to reduce timing-based account discovery. The database-backed limiter persists across restarts and atomically reserves attempts, but it is not a distributed limiter: run exactly one app instance with the SQLite database. Behind Render, the application reads Render's overwritten `CF-Connecting-IP` only when `TRUST_PROXY=1`; it does not trust the caller-controlled X-Forwarded-For chain. This is a basic safeguard, not a substitute for managed edge controls or monitoring.

### Render deployment review steps (not executed)

1. Review `render.yaml`, region, plan, one-instance setting, and persistent-disk size. Do not provision until the recurring cost and data-region choice are accepted.
2. The current Blueprint uses `type: web`, which is publicly reachable on a Render URL. `PRIVATE_BETA=1` requires a valid one-use invitation for account registration, but does not make the service network-private or prevent login-page access. Use a separate access restriction if the beta must be hidden from the public internet, and verify that restriction before accepting users.
3. Before provisioning, confirm one persistent disk will be mounted at `/var/data`; only files below that mount persist. Startup requires `APP_ENV=production`, `PERSISTENT_DATA_DIR=/var/data`, `REQUIRE_PERSISTENT_MOUNT=1`, `EXPENSE_DB_PATH=/var/data/expenses.sqlite3`, `EXPENSE_BACKUP_DIR=/var/data/backups`, `COOKIE_SECURE=1`, `TRUST_PROXY=1`, and a valid `PORT`; keep one app instance. Load `EXPENSE_BACKUP_KEY` from a separate operator secret store for encrypted backup/restore. No app signing key is used by the current server-side session model.
4. During an access-restricted controlled verification, check `/healthz`, signup/login/logout, CSRF rejection, user isolation, expense create/delete, CSV import/export, and restart persistence. Confirm HTTPS redirects/certificates and the actual headers received by the app. `TRUST_PROXY=1` trusts `CF-Connecting-IP`; this provider ingress behavior has not been tested in a live Render service.
5. Create an encrypted operator backup, copy it to a separate access-controlled location, rehearse restore to a separate database, adopt and execute a retention policy, and document restore ownership before inviting users. No automated or off-site backup is configured.
6. Verify platform logs, alerts, resource/disk thresholds, rollback procedure, account-deletion/recovery implications, and whether the selected plan provides access restrictions suitable for a private test.

### Controlled staging configuration (prepared, not provisioned)

The existing `render.yaml` is the staging template; it remains a public web-service type with `PRIVATE_BETA=1`, so invitations restrict registration but do not hide the login page or protect the URL from public requests. Before using it, add a provider-level access restriction if the beta must be reachable only by approved testers, then verify the restriction from an unauthenticated external client. Do not copy a local or production SQLite file into staging. Let the new `/var/data` disk initialize an empty database and use only synthetic accounts and records created after the staging service is access-restricted.

Keep one service instance and the configured 1 GB disk mounted at `/var/data`; SQLite is local to that disk and this setup is not suitable for horizontal scaling. Configure `APP_ENV=production`, `PRIVATE_BETA=1`, `PERSISTENT_DATA_DIR=/var/data`, `REQUIRE_PERSISTENT_MOUNT=1`, `EXPENSE_DB_PATH=/var/data/expenses.sqlite3`, `EXPENSE_BACKUP_DIR=/var/data/backups`, `COOKIE_SECURE=1`, and `TRUST_PROXY=1` in the service environment; Render supplies `PORT`. Set `EXPENSE_BACKUP_KEY` as a private secret only when performing encrypted backup/restore operations, never in the Blueprint or repository. Keep HTTPS enabled at the provider edge, verify TLS and the request headers before enabling trusted proxy IP handling, and use the production Waitress entry point. The health route reveals only service/database status.

The Blueprint's `autoDeployTrigger: checksPass` and the GitHub workflow are prepared to make normal Git-based automatic deployments wait for repository CI. The workflow must first be connected to the real repository and observed passing; Render's manual deploy controls remain separate. This workspace copy has no Git metadata, and neither the workflow nor provider gating has been exercised remotely.

**Cost estimate before approval:** the current Blueprint's `0.5c-512mb` compute plan is listed at about **$7/month**, and its 1 GB persistent disk at **$0.25/month**, or roughly **$7.25/month before taxes, excess usage, and any access-control/workspace plan requirements**. Rates and plan requirements can change; check the provider dashboard before provisioning. [Current Render pricing](https://render.com/pricing). No service or paid resource has been created.

### Deployment assessment (not deployed)

The app now has a Flask WSGI adapter and Waitress production entry point. The Render Blueprint has not been deployed, and provider ingress, disk persistence, HTTPS behavior, restart persistence, access restrictions, and restore procedures have not been verified in a live service. Keep SQLite on a single app instance and one persistent local volume; do not horizontally scale this database.

- **Render web service with a paid persistent disk:** least operational work of these options. Render documents [managed TLS](https://render.com/docs/tls) and [dashboard-managed environment variables/secrets](https://render.com/docs/configure-environment-variables). A [paid persistent disk](https://render.com/docs/disks) retains files across restarts/deploys; its free service filesystem is ephemeral. The included Blueprint sets the Waitress start command, port, persistent database path, and health route; confirm these settings and the mount in Render before serving traffic. Disk snapshots are provider recovery, not a substitute for tested SQLite backups. Compute and disk incur ongoing charges; check current [plan/storage rates](https://render.com/pricing) before creating a service.
- **Fly Machine with a Fly Volume:** gives a single-machine deployment with a mounted persistent volume. [Fly Proxy provides TLS termination](https://fly.io/docs/networking/tls/); [application secrets](https://fly.io/docs/django/the-basics/secrets/) are configured separately from source. A [volume](https://fly.io/docs/about/pricing/) persists across machine restarts, but it is local to a machine/region and a single-volume SQLite deployment has no automatic failover. Compute, provisioned volume, snapshots, and traffic can be billed separately; review current rates before provisioning.

Recommendation for a later deployment: start with one Render service and one paid disk after reviewing the deployment checklist above. It is simpler to operate; the trade-off is provider-bound storage and a single-instance availability limit. No account, paid resource, domain, or deployment has been created.

## Features

The signed-in interface is organized into four independent navigation areas. **Genel Bakış** is a summary-only dashboard for the selected month, actual spending, budget progress, compact income and card-liability snapshots, and recent activity. **Harcamalarım** contains the expense list and entry flow, monthly/category budget management, recurring expense rules, search and category/payment-method filters, and CSV import/export. **Kredi Kartlarım** contains card management, statements, repayments, and installment schedules. **Gelirlerim** contains the income-entry form, received/expected records, recurring income schedules, status controls, and income CSV export. Planned recurring items and expected income remain separate from completed expenses and received income.

- Account signup and login with server-side sessions
- Account deletion with current-password verification and explicit typed confirmation
- Private expenses, categories, payment methods, and monthly overall/category budgets
- Turkish dashboard with current-month totals, recent expenses, category totals, and prior-month comparison
- Expense create, edit, and delete
- Weekly, monthly, and yearly recurring-expense rules with edit, pause, resume, cancellation, and a separate upcoming list
- Explicitly confirm a planned recurring occurrence to add one real expense record; planned amounts stay out of actual totals until confirmed
- Spending insights with category budget warnings and an actual-record-based month-end pace estimate when at least five days of the current month have elapsed
- Authenticated CSV export and preview-first CSV import, with Turkish dates/decimal separators, row-level errors, and duplicate review
- Money stored as integer minor units (kuruş); dates are ISO calendar dates; TRY is the only supported currency in this MVP
- Per-user database queries and foreign-key ownership constraints
- Clear empty, loading, validation, and request-error states; future-dated completed expenses are rejected

No workbook values are loaded as defaults. See [the workbook source map](docs/workbook-source-map.md) for sheet-level findings and calculation ambiguities. The source workbook is not modified or copied into the app.

## Data model and privacy

Users own expenses, categories, payment methods, budgets, and recurring rules through `user_id` ownership. Every data query is scoped by the authenticated user ID, and composite foreign keys prevent linking another user's category or payment method. Recurring occurrence rows are unique by rule and scheduled date; confirming one creates an actual expense in the same database transaction. Mutations also check the owning user. Passwords use PBKDF2-HMAC-SHA256 with per-user random salts. Session tokens are random, stored only as hashes, expire after seven days, and are sent in HttpOnly, SameSite=Strict cookies. Logout revokes only the current session. State-changing requests require a session-bound CSRF token. Account deletion requires the current password and typed `SİL` confirmation, then deletes the user and all owned records transactionally; foreign-key cascades also invalidate every session for that account. Responses set a content security policy and anti-framing/content-sniffing headers.

These are local-MVP protections, not a guarantee against other code running as the same OS user. The app has no TLS itself, password recovery, audit log, or automatic cleanup of expired session rows. Unhandled HTTP errors return a generic message; server logs record only the exception class and stack frames, without exception text or request data. The app does not monitor or alert on those logs. Its SQLite-backed rate limit is keyed per source and attempted identity, scoped to one database/app instance, and is not a distributed edge defense. SQLite write-locking and the single-instance setup are not verified for public multi-user load. For a custom database path, protect its parent directory. A startup migration removes duplicate overall budgets created by an earlier SQLite NULL-uniqueness bug and preserves the most recently inserted amount for each user/month.

CSV import accepts UTF-8 CSV with `Tarih`, `Tutar`, and `Açıklama` headers (English aliases `Date`, `Amount`, `Description` are also accepted). `Kategori`, `Ödeme yöntemi`, and `Not` are optional. Supported dates include `YYYY-MM-DD` and `DD.MM.YYYY`; comma decimals and Turkish thousands separators are parsed. Import previews every row, reports invalid rows, flags exact likely duplicates (same date, amount, description and category), and skips duplicates by default. Users explicitly select any duplicate to include. Imports only insert records and never replace or delete existing expenses. Category/payment names must exist in that user's account. Export includes only the signed-in user's expenses and prefixes formula-like text cells to avoid spreadsheet formula evaluation. CSV files are limited to 1 MB; do not treat this workflow as a general Excel workbook importer.

## Recurring expenses and insights

Add a rule from the dashboard, select a frequency, and set its start date and optional end date. Monthly rules remain anchored to the original start day: a rule starting on the 31st uses the last day of shorter months and returns to the 31st when possible. Yearly February 29 rules use February 28 in non-leap years and February 29 in leap years. Rules can be edited, paused, resumed, or cancelled; cancellation does not delete the rule or modify any already-created expense. The app does not automatically create expenses or send reminders. It displays the next 45 days of active planned occurrences separately, with the next expected date shown on each rule.

Marking a planned occurrence as “Gerçekleşti / ödendi” creates a normal expense dated on the confirmation day. A unique rule/date key and a database transaction make repeat submissions idempotent. Planned occurrences are excluded from spending, budgets, and category totals until confirmed. The dashboard's month-end estimate extrapolates only the selected current month's actual records, and is withheld when there are fewer than five elapsed days or no spending. It is a pace estimate, not a promise or financial recommendation. Category warnings use the same actual totals and category budgets as the dashboard summary.

## Income tracking

Income is owned by the authenticated user. A safe startup migration extends the allowed recurrence values to include daily schedules while preserving existing rule, amount-history, and income-record IDs and relationships. Create one-time expected income or daily, weekly, monthly, or yearly recurring salary, additional income, bonuses, freelance income, refunds, or other income. The form labels a one-time date “Ödeme tarihi” and recurring schedule start “İlk ödeme tarihi”; it previews the recurrence rule. Monthly schedules can explicitly use the last calendar day; salary defaults to that rule. Mark a record as received and the actual receipt date is stored; received and still-expected amounts appear separately on the dashboard. Expected one-time records can be edited or deleted; received records remain in history. Recurring rules can be edited, paused, resumed, or stopped; stopping prevents future occurrences while retaining received history. Future amount changes can be added with an effective date; confirmed history is retained.

Monthly salary rules default to the last calendar day, including February, leap years, and year changes. Other monthly schedules default to the original day and clamp to shorter months without drifting; users can opt into the explicit last-calendar-day rule. Occurrence generation is idempotent and happens when a month is viewed. Expected income is not added to expense totals or budgets, and must not be treated as confirmed cash. A separate account-scoped CSV export covers income records; recurring rule definitions are not exported. Expense CSV import/export behavior is unchanged.

Credit card management is implemented as a separate liability ledger: users can maintain cards, assign expense records to statement cycles, correct statement dates, record full or partial repayments, and schedule installment purchases. Repayments are not expenses, so they do not affect category spending or expense budgets. A card's displayed liability is its opening balance plus card purchase totals less card repayments. The cash position and forecast use that liability ledger's repayment and statement records; they do not connect to or verify a bank account. See [docs/credit-cards-tr.md](docs/credit-cards-tr.md) for card calculation rules and limitations.

## Cash position, payment calendar, and forecast

Each account can set an explicit opening cash-and-bank balance and the date it applies. The confirmed position is that opening amount plus income marked received on or after the effective date, less non-card expenses on or after that date, less card repayments on or after that date, plus dated adjustments. Records before the opening date are retained but excluded from this calculation. Expected income is never included in confirmed cash. Card purchases remain expenses for spending reports but reduce cash only when a card repayment is recorded. The result is an account-entered estimate, not a bank-verified balance; there is no bank connection or reconciliation.

Use a dated, explained positive or negative adjustment when the confirmed balance needs correction. The app keeps adjustment history and includes adjustments only from the opening-balance date. Updating the opening balance or its effective date changes the baseline calculation; it does not rewrite underlying historical transactions.

The Overview includes a chronological calendar with month, date range, status, and event-type filters. It derives events from user-owned expenses, income records and generated income schedules, recurring expense rules, card statements, repayments, installments, and balance adjustments. Recurring occurrences already confirmed as expenses are not listed as planned items. A statement event shows both its closing date and its payment due date. Completed and expected income remain distinct; salary schedules retain their configured date, including the default last day of the month.

The cash-flow estimate starts with the current confirmed balance and processes future expected income, scheduled cash expenses, dated adjustments, and unpaid statement balances in date order. The default view projects through the end of the month two months ahead. Statement balances are the projected card outflows; installment calendar rows are informational because they are already included in statement totals. A confirmed card repayment reduces cash and the statement remainder but does not create a second spending expense. The forecast does not move salary dates for weekends or holidays and is not financial advice. It is marked insufficient when no opening cash baseline exists. Active recurring expenses without a payment method, and recurring card expenses without a selected card/statement, are excluded from projected cash and produce an explicit incomplete-data warning. Card opening liabilities have no assigned due date until actual statements exist. No future bank transactions, interest, fees, card spending forecasts, or unscheduled cash activity are inferred.

New `cash_settings` and `cash_adjustments` tables are created through the existing additive SQLite schema initialization. They are account-owned and removed with account deletion. The migration does not reset or rewrite existing expense, income, card, or statement data.

## Backup and restore

Backups are operator-only command-line files; there is no backup download or administrator HTTP endpoint. By default, files go to `data/backups/`, beside `data/expenses.sqlite3` and outside `static/`. Set `EXPENSE_BACKUP_DIR` to choose another private location. On macOS/Linux, the app sets the backup directory to mode `0700` and backup files to `0600`. On Windows, restrict the directory with the operating-system ACLs for the account running the app.

Encrypted backup files use a versioned format with AES-256-GCM authenticated encryption through the pinned `cryptography` library. The implementation streams SQLite bytes, verifies the GCM authentication tag before using decrypted data, and publishes completed backups by atomic rename. For SQLite snapshotting and validation, encrypted create/restore temporarily stages a plaintext database in the operating system's temporary directory in a mode-`0600` file on POSIX; handled exits remove it, but abrupt termination can leave it behind. Operators should ensure that temporary storage is access-controlled and check for stale `expense-private-*.sqlite3` files after a crash. The environment variable `EXPENSE_BACKUP_KEY` must hold a URL-safe base64-encoded 32-byte key. Generate one once and record it in a protected password manager or operator secret store:

```sh
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Copy the generated value directly into the operator's protected secret store and load it into the shell only when needed (for example, `read -s EXPENSE_BACKUP_KEY` followed by `export EXPENSE_BACKUP_KEY`). Never put it in source control, the backup directory, shell history, logs, or a command argument. Losing the key makes encrypted backups unrecoverable; store at least one protected recovery copy separately from the backup files. The program never prints the key. Encryption protects backups at rest but does not protect a compromised running host or a key available in its process environment.

For local development, an unencrypted backup remains available explicitly:

```sh
python3 app.py backup
```

This unencrypted form is for local development only. To create an encrypted local backup, first load `EXPENSE_BACKUP_KEY`, then run:

```sh
python3 app.py backup --encrypted
```

SQLite's online backup API captures a consistent snapshot; the app checks integrity, required tables/columns, and foreign keys before publishing. It writes hidden temporary files and atomically publishes only after completion; handled failures clean up staging files. A forced process/host termination can leave hidden temporary staging files, but does not publish them under a valid backup filename. No backup is scheduled, copied off-host, or pruned automatically.

For an operator shell in the production service, pass the same mounted-disk configuration; production CLI commands validate the mount before touching a database:

```sh
APP_ENV=production PERSISTENT_DATA_DIR=/var/data EXPENSE_DB_PATH=/var/data/expenses.sqlite3 EXPENSE_BACKUP_DIR=/var/data/backups python app.py backup
```

Production mode always creates an encrypted backup and fails if `EXPENSE_BACKUP_KEY` is missing or malformed. Load the value from the operator's protected secret store in the shell before running the command. For the Render Blueprint, configure it as a private environment variable through the provider dashboard; do not commit it to `render.yaml`.

Restore requires stopping the app first. In the server terminal press `Ctrl-C`, then run this from the project directory with a file inside the configured backup directory:

```sh
python3 app.py restore data/backups/expenses-YYYYMMDD-HHMMSS-RANDOM.sqlite3.enc
```

For a custom database and backup location, use the same environment settings as the app:

```sh
EXPENSE_DB_PATH=/private/path/expenses.sqlite3 EXPENSE_BACKUP_DIR=/private/path/backups python3 app.py restore /private/path/backups/expenses-YYYYMMDD-HHMMSS-RANDOM.sqlite3.enc
```

Restore detects encrypted files from their versioned header. It rejects missing, incorrect keys, authentication/tag failures, truncated/tampered content, incompatible schemas, integrity or foreign-key violations, symlinks, and files outside the configured backup directory before replacing the active database. Production refuses plaintext restore. It refuses to proceed while SQLite WAL, shared-memory, or journal sidecars remain. Before replacing an existing valid database, it creates a fresh recovery backup (encrypted in production and for encrypted restores). The candidate is decrypted to a private temporary file and validated before the active database is atomically replaced. If validation or decryption fails, the active database is unchanged. Stop the application first; run restore only after the process is stopped, then start the app again. If the current database is corrupt and cannot be backed up, restore refuses to overwrite it; preserve files and resolve manually.

For production restore, stop the service first and run from an operator shell on the mounted disk:

```sh
APP_ENV=production PERSISTENT_DATA_DIR=/var/data EXPENSE_DB_PATH=/var/data/expenses.sqlite3 EXPENSE_BACKUP_DIR=/var/data/backups python app.py restore /var/data/backups/<backup-filename>.sqlite3.enc
```

Load the same `EXPENSE_BACKUP_KEY` securely for restore. The restore command prints backup paths only, never credentials or expense contents.

Account deletion is irreversible in the active database. Recovery uses whole-database restore, which rewinds every user's data to the backup point and may resurrect an account deleted after that backup. Selective per-account restore is not supported.

**Retention recommendation (not implemented automation):** keep daily encrypted backups for 14 days and weekly encrypted backups for 8 weeks, with at least one separately stored, access-controlled copy. Review capacity and legal needs before adopting this schedule. This app does not schedule backups, perform off-site copies, verify copied files, or automatically prune old backups. Operators must manually copy completed encrypted `.enc` files to another access-controlled location using an approved secure transfer method; retain the key in a separate protected secret store. Verify the copied backup by rehearsing a restore before relying on it.

Before removing old files, first review candidates without deleting:

```sh
find /var/data/backups -type f -name 'expenses-*.sqlite3.enc' -mtime +30 -print
```

Only after verifying the listed files and confirming a recent known-good encrypted backup, repeat with `-delete`. No off-site copy exists until an operator has separately copied and verified it.

**Restore rehearsal checklist:** (1) stop the app; (2) make a fresh encrypted backup; (3) copy a selected encrypted backup and the key through their separate protected channels; (4) restore into a separate temporary database directory, not over the live database; (5) run `python3 -c 'import app; from pathlib import Path; app.validate_database(Path("/path/to/test.sqlite3"))'` with `EXPENSE_DB_PATH` configured for that temporary file; (6) start a temporary app and confirm expected accounts/records without exposing them; (7) delete temporary plaintext files and document elapsed recovery time. Do not use real data for routine automated tests.

## Commands

```sh
python -m unittest discover -s tests -v
python -m py_compile app.py webapp.py wsgi.py production.py tests/test_app.py
node --check static/app.js
python3 app.py
```

### Browser end-to-end tests

The browser suite uses the pinned Playwright package and a temporary SQLite database. Install Node dependencies and a matching Playwright Chromium once, then run the suite:

```sh
npm ci
npx playwright install chromium
PYTHON_BIN=.venv/bin/python npm run test:e2e
```

Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to an existing compatible Chromium/Chrome binary to use it instead of Playwright's managed browser. The current suite starts an isolated loopback app with synthetic records in a temporary SQLite database. It covers login, read-only loading, partial payments, income receipt, payment-plan editing, profile/card persistence, and layout overflow at 320px, 390px, 768px and 1440px. The former UI suite is retained in `tests/archive/`. It never reads the normal application database or workbook. This test runner requires permission to launch Chromium; it cannot attach to the in-app browser. In restricted macOS sandboxes, Chromium can terminate with `SIGTRAP`; do not report that suite as passed. Local CUA checks are separate evidence.

The test suite includes local WSGI HTTP integration tests that bind an ephemeral loopback port and covers recurring income and salary calendar boundaries, expense recurring frequencies, card statement boundaries, repayment accounting, installment rounding and idempotency, ownership, insight thresholds, authentication rate limits, production database path validation, health checks, backup/restore, and account deletion. There is no package build, linter, or type-check configuration in this project. Most current UI copy lives in `static/app.js`. API validation errors are Turkish strings in `webapp.py` and `app.py`; before adding another locale, move both UI and API messages into locale dictionaries.

### Continuous integration

`.github/workflows/ci.yml` runs the Python regression suite, Python and JavaScript syntax checks, dependency consistency check, and Playwright Chromium end-to-end suite on pushes and pull requests. The Render Blueprint sets `autoDeployTrigger: checksPass` so configured Render deployments wait for repository CI checks before deploying. This workflow has not run on a remote CI provider from this project copy; the directory currently has no Git metadata or remote, so provider integration and deployment gating still need to be verified after the project is connected to its repository. CI does not provision infrastructure, create backups, or verify live hosting behavior.

## Next steps

1. Define an optional local backup schedule and retention policy; backups are manual today.
2. Add editing and archiving for categories and payment methods.
3. Add richer date-range filters and reporting.
4. Verify persistent storage and encrypted off-site backup/restore before public deployment.


## Local simple-payment review

The current interface shows income dates, installment/payment dates, and the selected range's income minus payments. It labels this result as excluding opening balances. Card purchases remain distinct from actual cash payments. Profile and card definitions are under Profil ve Ayarlar; storing a card's specific due date creates no financial transaction. Existing cards remain visible and retain their history.

The new schema additions are initialized by create_app: payment_events, nullable request keys, profile_cards, and users.display_name. They preserve existing financial rows. Take a verified database backup before production release; on rollback keep additive tables/columns and restore the prior code, never drop new payment records. The implementation plan tracks verification evidence and the mandatory user review gate before any deployment.

For isolated PostgreSQL tests, set FINANCE_TEST_POSTGRES_ADMIN_DSN to a local test admin connection or FINANCE_TEST_PGLITE_SERVER to the local WASM server module. The helper rejects non-loopback test hosts. Clear live database environment variables before testing. WASM results do not replace production reconciliation or native PostgreSQL CI.


### Reviewed payment entry model (27 October 2026)

Income entry distinguishes one-time income and recurring income. Recurring income supports monthly or weekly schedules. Monthly last-day mode takes a start month, not an additional day, and resolves short months/leap years on the server. The redundant recurring-income list is removed from the current UI.

Payment entry has three explicit types: card installments (card plus first/final payment dates), one-time card purchases (purchase date distinct from the selected card's stored payment due date), and bank/cash recurring payments (monthly/weekly frequency, first/final date, no card). The additive payment_plan_details table stores the owned card link and type; expenses appear only once in the selected-range cash plan. Missing/stale card due dates produce validation errors instead of invented deadlines. Card-date changes move only completely unpaid one-time plans; completed or partial payment history remains anchored.

For local review of authorized account data, tools/local_review_sandbox.mjs proxies authentication through the read-only reviewer and all financial writes exclusively to an isolated, private local SQLite copy. Its Python worker removes production database variables, validates foreign keys, and reconciles source/copy totals before serving. This tool is local-only and is never part of production routing. Source passwords are not stored. User edits in this copy are not synchronized to production. Do not publish before explicit local approval.


The overview defaults to a full calendar month and supports previous/next month navigation. Its income/payment/net cards and event list use that month. A separate general-total section retains the independently selected broader date range. Both views call the same read-only calculation endpoint. Run `node --test tests/monthly_overview.test.cjs` for month-boundary, separate-total, event-lookup, and response-race regressions.
