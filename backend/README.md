# Restaurant Platform API

FastAPI backend for a normalized Clover Sandbox menu and server-side Clover v2
OAuth. Clover remains the inventory system of record. This milestone supports
one backend process, `APP_ENV=development`, and `CLOVER_ENV=sandbox`.

## Staff admin portal and kitchen workflow

The staff UI lives at `http://localhost:3000/admin`. It is separate from the
customer QR route. The backend protects `/api/admin/*` with a short-lived,
HMAC-signed HttpOnly cookie (SameSite=Lax; Secure outside development). Login,
logout, kitchen transitions, payment refresh, and Close Table require an exact
Origin match from `FRONTEND_ORIGINS` (or legacy `FRONTEND_ORIGIN`). An in-process login throttle is an MVP guard; use a
shared edge limiter and HTTPS for multi-process/public deployment.

In the backend's private environment, set `ADMIN_USERNAME`,
`ADMIN_PASSWORD_HASH`, and `ADMIN_SESSION_SECRET`. Generate the hash locally
without putting the password in shell history:

```powershell
.\.venv\Scripts\python.exe -m scripts.hash_admin_password
```

Use a password of at least 12 characters. The helper prompts twice and prints
only the salted PBKDF2-SHA256 hash; place
that hash in `ADMIN_PASSWORD_HASH`. Use a separate random, 32+-character
`ADMIN_SESSION_SECRET`. `ADMIN_SESSION_HOURS` defaults to 8. Keep all three
values out of source control and browser environment variables. Set
`FRONTEND_ORIGIN=http://localhost:3000` for local use, or set a comma-separated
`FRONTEND_ORIGINS` for multiple exact origins; the latter takes precedence. In the frontend's
private server environment, set `ADMIN_API_ORIGIN=http://127.0.0.1:8000`
before starting Next.js (and at build time for production). This is a
same-origin proxy target, not a `NEXT_PUBLIC_*` secret; the admin browser calls
only `/api/admin/*` on the frontend origin.

The Tables grid and detail panel read local session, kitchen-line, and
last-confirmed normalized order data. Prep Summary groups outstanding line
quantities by authoritative item ID across open tables, excluding DELIVERED
lines. Staff kitchen actions never mutate Clover. Selecting a table or using
Refresh payment makes one read-only Clover order GET; the 15-second dashboard
poll reads local SQLite only. The stored payment state is cleared on a new
confirmed customer order and on a failed payment refresh. A missing/deleted
Clover order or unavailable payment read returns local table and kitchen data
with an Unavailable payment warning; it does not erase the stored order reference
or prevent the audited local-only Force Close action. Clover's documented
`paymentState` values are mapped to Unpaid, Partially Paid, Paid, or Unknown;
only exact `PAID` can authorize closure. See the
[Clover order API](https://docs.clover.com/dev/reference/ordergetorder-3).

Each confirmed customer submission creates stable SQLite kitchen lines;
same-key replays and reconciliation do not duplicate them. A line, including
quantity greater than one, is one status unit: NEW → READY → DELIVERED.
Per-piece tracking is a possible future enhancement. Add-ons create new lines
without changing earlier delivered statuses. The table state is derived from
those lines: New Order (any NEW), In Progress (no NEW but READY), All Delivered
(all DELIVERED while open), and Closed. An open table with no confirmed lines
shows Awaiting Order.

The `kitchen_lines` table and index are added with `CREATE TABLE IF NOT EXISTS`.
Nullable `payment_state` and `payment_checked_at` columns are added to the
existing `table_sessions` SQLite table if absent;
the existing session database is not recreated. On first access, prior
successful submissions are backfilled from stored confirmed snapshots. Because
historic fulfillment status was not recorded, open historic lines start NEW
conservatively; closed historic lines are marked DELIVERED. Historic Clover
line IDs may be unavailable and remain null. Review open legacy tables in the
admin portal before marking them delivered.

Close Table is a local, idempotent status transition. It preserves the Clover
order reference, mutations, and kitchen history; it refuses undelivered lines,
pending/applied/uncertain mutations, or a payment state other than fresh `PAID`.
Close Table re-reads Clover payment immediately before its local transaction.
An empty table with no Clover order cannot be closed through this workflow.
A separate admin-only **Force Close Table** action is available after at least
one kitchen line exists and every line is DELIVERED. It requires a reason and
explicit confirmation, closes only the local session, and never calls or
mutates Clover. It snapshots the actor, timestamp, reason, last known payment
state, Clover order reference, and whether order reconciliation was unresolved.
An unresolved order write is shown as a strong warning before confirmation;
the override does not resolve that write. Force-closed visits are labeled in
History. Do not use this fallback as a substitute for normal payment checks.
A missing current total or Clover status is shown as unavailable. Payments
remain manual in Clover. Closed sessions appear in
newest-first history; rescanning the same permanent table QR starts a fresh
session with no inherited Clover order or kitchen lines.

Manual A1 check in development/Sandbox:

1. Start backend and frontend with the admin configuration above. Open
   `http://localhost:3000/admin` and sign in.
2. Scan an A1 QR, submit three small customer items, and confirm A1
   appears GREEN / New Order at `/admin/tables` with its lines as NEW.
3. Select the tile and mark one line Ready. The table stays GREEN while any
   NEW item remains. Mark the others Ready to see AMBER / In Progress, then
   deliver every line to see RED / All Delivered. Check Items Pending along the
   way; delivered portions should disappear. Resolve any order sync issue when
   possible; a force close must explicitly acknowledge and audit an unresolved one.
4. Before payment, Close Table stays disabled. Pay the Clover order manually,
   then select **Refresh payment** and verify Paid. Select **Close Table** and
   confirm once. Verify A1 returns to Available and appears Closed in `/admin/history` with the
   previous order reference and final known total, if one was confirmed.
5. Scan the **same** A1 QR again. Its signed table link remains valid, but the
   new session ID differs and has no previous Clover order or cart state. The
   old session and kitchen lines remain visible in history. Do not submit a
   Clover order solely to test closing outside a planned Sandbox check.

Future work may add shared login throttling/session revocation, staff roles,
and richer reporting. No payment mutations, full-screen
KDS, AI, reservation, or WebSocket feature is included here.

## Run locally on Windows PowerShell

From the `restaurant-platform` directory:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

For an existing virtual environment, installation only adds missing dependencies
(including `cryptography`); do not use `--upgrade` unnecessarily. If PowerShell
activation is unavailable, run `.\.venv\Scripts\python.exe` in place of `python`.
Never overwrite an existing `.env`. Fill settings manually and keep secrets out
of source control, terminal output, logs, and support messages.

The safe command for the existing backend, without activation, is:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

Open:

- Health: http://127.0.0.1:8000/health
- API documentation: http://127.0.0.1:8000/docs
- Menu: http://127.0.0.1:8000/api/menu
- Orders: `POST http://127.0.0.1:8000/api/orders` (creates a Sandbox order)
- Sandbox order-type chooser: http://127.0.0.1:8000/api/dev/clover/order-types

The temporary order-type chooser is read-only and works only with
`APP_ENV=development`, `CLOVER_ENV=sandbox`, and the Sandbox API base URL. It
uses the configured merchant and selected sandbox-token/OAuth credentials; it
accepts no merchant ID from the request. It omits deleted types and returns
only ID, label, default/hidden flags, and customer ID method. Keep this
unauthenticated development endpoint off public deployments. Copy the chosen
type's ID into `CLOVER_ORDER_TYPE_ID` manually, then restart the backend.

## Customer QR ordering (development Sandbox)

The customer path is: printed QR → opaque signed table token → Next.js
`/order/[tableToken]` → FastAPI public API → server-side table session → one
running Clover order. The QR identifies a physical table; the session is the
active dining visit/check, and Clover is the POS record. The browser receives
neither Clover credentials nor the Clover order ID. Payments are not implemented.

Set `TABLE_TOKEN_SIGNING_KEY` in the backend environment to a *separate* Fernet
key. To generate one locally, run:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store that output only in the backend's private configuration. Keep a backup:
losing it invalidates printed QR codes. `TABLE_TOKEN_PREVIOUS_KEYS` can hold
comma-separated retired keys during rotation; new tokens use the primary key.
Increment `TABLE_TOKEN_VERSION` to revoke all older QRs, then reprint them.
Tokens are long-lived bearer capabilities: anyone with a copy of a QR can use
its table until it is revoked. Their encrypted payload binds the table and
version to the configured merchant without exposing that merchant ID to the
browser. Do not print one for a table that is not meant
to accept public orders. Per-token revocation and customer identity are not
implemented.

Set backend `FRONTEND_ORIGIN=http://localhost:3000` and
`FRONTEND_PUBLIC_URL=http://localhost:3000` for local development. To allow both
local and tunnel clients, use for example
`FRONTEND_ORIGINS=http://localhost:3000,https://your-exact-frontend.trycloudflare.com`.
CORS accepts only the configured exact origins, GET/POST, and the headers needed
for ordering; it allows credentials for admin cookies and never uses wildcards. The
legacy `/api/orders` and `/api/table-sessions` routes require a separate
`X-Internal-API-Key` header whenever either frontend origin setting is set. Set a strong
`INTERNAL_API_KEY` in the backend's private configuration if staff tooling must
use those routes; if it is blank, those routes are unavailable. Never send this
key from the customer app. The
in-process public rate limiter is a development guard, not a distributed
production limit; replace it with a shared edge/Redis limiter before public
deployment. Existing backend routes remain development/Sandbox-only.

From `restaurant-platform/backend`, start the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

In a second terminal, from `restaurant-platform/frontend`:

```powershell
if (-not (Test-Path -LiteralPath .env.local)) { Copy-Item .env.example .env.local }
npm install
npm run dev
```

`NEXT_PUBLIC_API_BASE_URL` in the frontend `.env.local` is only the public
backend origin, normally `http://127.0.0.1:8000`. Do not put any Clover or QR
secret in a `NEXT_PUBLIC_*` variable. The menu comes from `/api/menu`; names
and prices are not hardcoded in the frontend. The cart is display-only for
pricing and is stored in `sessionStorage` scoped to token and session. A
pending submission keeps the same idempotency key across refresh/retry, so a
double tap cannot create another Clover mutation. A new submission after
confirmation gets a new key but reuses the same table session.

With the backend working directory still `restaurant-platform/backend`, make
the development QR for T12:

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_table_qr T12
```

The utility prints the signed token and full URL and writes
`backend/.local/qr/T12-v1.png` (ignored by Git). For manual API testing, use
the printed raw token or the token segment copied from the QR URL: the public
GET and POST endpoints accept either the raw Fernet `=` padding or its `%3D`
URL representation, and both apply identical signature, version, and merchant
checks. Do not send a table name instead of the token. For testing on a phone, use
HTTPS tunnels to both frontend and backend (for example, separate `ngrok http
3000` and `ngrok http 8000` processes). Set `FRONTEND_PUBLIC_URL` and
`FRONTEND_ORIGINS` to the exact local and frontend tunnel origins, and set the frontend
`NEXT_PUBLIC_API_BASE_URL` to the backend tunnel origin. Set frontend
`FRONTEND_DEV_ORIGIN` to the frontend tunnel origin so Next
accepts its development assets. Restart both servers and regenerate the QR.
Do not hardcode tunnel hosts or expose a development
backend beyond the controlled test.

Manual Sandbox acceptance: scan the newly generated QR on a phone, verify it
shows Table 12 and the live Clover menu, order one item, and confirm a single
new Clover order. Tap **Add more items**, order a second item, and confirm the
same Clover order receives one new line and its persisted total updates. Repeat
the submission only with its existing key if troubleshooting; do not send a
fresh key after a reconciliation error. Confirm no internal Clover ID appears
in the customer UI. Automated tests use mocked Clover calls only.

## Running table sessions (development Sandbox)

`POST /api/table-sessions` with `{"table":"T12"}` creates or resumes the one
OPEN session for normalized table `T12`. Table IDs are trimmed, uppercased, and
limited to 1–32 letters, digits, underscores, or hyphens. The response includes
`session_id`, `table`, `status`, nullable `clover_order_id`, and creation/update/
closure timestamps. `GET /api/table-sessions/{session_id}` returns that record.
Sessions are scoped to the configured Clover merchant and Sandbox environment,
so switching from a Sandbox token to OAuth does not start a second check.
There is no public close endpoint yet; closure policy is not final.

`POST /api/table-sessions/{session_id}/items` accepts 1–20 unique Clover item
IDs with integer quantities 1–10 (100 units at most) and requires an
`Idempotency-Key` header. The client cannot supply a Clover order ID, name, or
price. The first submission reuses the validated atomic-order service, then
persists the resulting Clover order ID. Later submissions validate current
inventory and append to **that same order ID** using Clover's
[`line_items`](https://docs.clover.com/dev/reference/ordercreatelineitem) endpoint
for one unit or [`bulk_line_items`](https://docs.clover.com/dev/reference/orderbulkcreatelineitems)
for multiple units. A bulk request includes prices read from Clover inventory
because the documented bulk API requires a price on each line; no client price
is accepted. Quantity is represented by repeated inventory-backed line items,
not `unitQty`. Clover does not recalculate an existing order's total after a
line-item append. The backend therefore reads the full order and paged,
expanded line items, calculates the total using Clover's documented rules,
updates only `total` through `POST /v3/merchants/{mId}/orders/{orderId}`, then
reads the order again. The response uses Clover's confirmed total, currency,
and lines, never the locally calculated number. Missing subtotal/tax remain
`null`.
Readback groups lines only when inventory ID, displayed name, and unit price
match and unit quantity is not variable; custom lines may have a null item ID.
The [Clover atomic-order guide](https://docs.clover.com/dev/docs/create-an-atomic-order)
requires a manual total update when an existing order amount changes. The
calculator supports USD fixed-price lines, repeated-unit quantities, modifier
amounts, line percentage/fixed discounts, order percentage discounts, tax-rate
grouping with half-up rounding, VAT/tax-removed flags, and percentage/fixed
service charges. It refuses missing prices/rates, unrecognized charge types,
non-fixed items, and fixed order discounts across multiple lines, because
writing a guessed total would be unsafe. The first atomic-order creation path
keeps its mutation behavior. Its confirmed order ID is persisted before the
table-session readback. Verify this behavior in Sandbox before payment work begins.

Table sessions are platform records in a separate local database at
`backend/.local/table-sessions.sqlite3`; the Clover order is the running POS
check. A partial unique index enforces one OPEN session per merchant/table.
Mutation claims and successful responses are durable and scoped to a session.
The same key plus request returns the saved result; a changed request returns
409. Concurrent writes to one session are serialized locally, while SQLite
blocks duplicate or competing writes across processes. An uncertain POST
outcome or crash while a mutation is pending blocks further additions until
manual reconciliation; do not blindly retry under a new key. If Clover
confirmed an append but the pre-update read failed, retrying the **same key**
performs only the calculation/read and total update. Once the total POST may
have been sent, any failure blocks further additions for manual reconciliation;
it is never retried blindly. After a confirmed total update, a failed final
readback (including after first-order creation) enters a durable
`readback_pending` phase. The API returns HTTP 503 with
`ORDER_CONFIRMATION_PENDING`; the customer cart and same idempotency key stay
intact. **Check order status** retries only the stored order's GET/readback.
Once it succeeds, the response is saved and subsequent same-key retries return
that result. Do not use a new key while confirmation is pending. This local
store and process lock are for the single-worker development milestone, not a
complete production reconciliation system.

For a read-only, redacted local state diagnosis run
`python -m scripts.inspect_table_session T20` from `backend`. It reports the
session's operation state and each mutation's state/phase without printing
credentials, item details, or full Clover order IDs. A `state=uncertain` row
with `metadata_present=False` means the ledger has no proof that the append
completed. Even if items are visible in Clover, a new key must remain blocked
until the exact prior request can be verified and reconciled; the shared order
ID alone is not proof of that mutation. Do not delete or manually flip that
row. For this legacy shape, retrying the **original key with the identical
original payload** can perform GET-only proof against the last saved successful
order snapshot. Every requested item and modifier must be present in the
running Clover order beyond that prior snapshot. Only then is the append marked
confirmed; because the old row never reached the total-update phase, the
normal total calculation/update may proceed once. A different key cannot supply
that missing proof directly, but the backend can try a bounded GET-only
reconstruction: subtract the last successful order's line/modifier counts from
the current Clover readback, rebuild candidate request bodies, and require an
exact match to the saved request fingerprint. Any extra/missing line, modifier,
unsupported quantity, or unmatched fingerprint leaves the row blocked. When
the hash matches, the append is confirmed without replaying it; its total
update, which the empty phase proves was never attempted, can proceed once.
If Clover already persists the exact calculated total, the backend confirms
that by GET and skips the total POST too.
Confirmed readback-pending rows, including more than one, are reconciled
oldest-first using GETs; the session lock is recomputed from any unresolved
rows in the same transaction that saves each success. An uncertain row remains
blocking and is reported distinctly as requiring manual reconciliation.
If an order-total POST outcome is unknown but the prepared target was saved,
the backend checks the persisted Clover total with a GET. An exact match
confirms that total without repeating its POST; a mismatch remains blocked.

For existing Table 19, run `python -m scripts.inspect_table_session T19` from
`backend` and confirm mutation 2 is `applied/readback_pending` with
`resume_phase=total_confirmed` and `ambiguous=False`. Verify the expected items
and total on the same Clover order, then restart the backend without clearing
its SQLite data. Use **Check order status** in the original browser session (or
replay the identical request with its original `Idempotency-Key`); do not submit
a new item or key first. This phase makes only Clover order and paged line-item
GETs. On success, the mutation becomes `succeeded` and the session operation
state becomes `idle`. If it stays pending, check the development-only
`DEV Clover reconciliation` log's `check=` reason and redacted counts/totals;
the public response remains `ORDER_CONFIRMATION_PENDING`. Never clear the row
or repeat the append/total POST to bypass a mismatch.

If a customer submits a new course with a new key while an older mutation is
unresolved, the backend first attempts the applicable safe proof above. It
saves each confirmed prior result and recomputes the session lock in one SQLite
transaction, then claims the new key in the same request. A failed GET, total
mismatch, or unmatched legacy request fingerprint leaves the lock in place and
does not submit the new course. An unproven outcome is never retried by POST.

Manual confirmation-recovery check in Sandbox: scan a fresh table QR, submit
one item, and—if a final readback failure can be safely simulated—verify the
UI says the order was sent and offers **Check order status**. Confirm Clover
contains exactly one order with that item. Click **Check order status** using
the unchanged browser session; it must not create another order or line item.
When the readback becomes available, the UI should show **Order received**,
clear the submitted cart, and allow **Add more items**. If the failure is an
ambiguous mutation rather than confirmed readback failure, stop and reconcile
manually; do not bypass it with a new key. Automated tests simulate this with
mocked Clover responses; no live recovery has been verified.

For the existing Table 20 case: run the redacted inspection command above and
confirm Clover shows one Goat Biryani, one Lassi with Sweet, and one Indian
Masala Tea on the same order. Restart the updated backend and frontend without
clearing the local backend database or the existing Table 20 browser session.
Select one **new small item** and submit it once. The backend should GET the
existing order, match the Lassi/Tea delta to the old request fingerprint, finish
that old append/total, then append the new item to the **same** Clover order.
Check Sales Activity/order details for no duplicate Goat Biryani, Lassi, or Tea;
the Sweet modifier; the new item once; and the updated total. Confirm the UI
reaches **Order received** and permits another course. If exact fingerprint
proof or Clover readback fails, the new request remains blocked without any
POST; use the original key/payload if available or arrange staff-led manual
reconciliation. No live recovery has been verified by automated tests.

Closing a session rejects new additions; replaying a previously successful
key only returns its saved response and does not mutate Clover.
Bulk append failures are treated as uncertain because partial application is
not documented; definite single-line validation rejections can use a new key
after correction.

Manual Sandbox acceptance test, after the backend is running with your own
Sandbox OAuth connection or token and a Dine In `CLOVER_ORDER_TYPE_ID`:

```powershell
$base = 'http://127.0.0.1:8000'
$table = 'TEST' + (Get-Date -Format 'MMddHHmmss')
$sessionBody = @{ table = $table } | ConvertTo-Json
$session = Invoke-RestMethod -Method Post -Uri "$base/api/table-sessions" -ContentType 'application/json' -Body $sessionBody
$firstBody = @{ items = @(@{ item_id = '<BIRYANI_SANDBOX_ITEM_ID>'; quantity = 1 }) } | ConvertTo-Json -Depth 4
$first = Invoke-RestMethod -Method Post -Uri "$base/api/table-sessions/$($session.session_id)/items" -Headers @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() } -ContentType 'application/json' -Body $firstBody
$moreBody = @{ items = @(@{ item_id = '<LASSI_SANDBOX_ITEM_ID>'; quantity = 1 }) } | ConvertTo-Json -Depth 4
$appendKey = [guid]::NewGuid().ToString()
$second = Invoke-RestMethod -Method Post -Uri "$base/api/table-sessions/$($session.session_id)/items" -Headers @{ 'Idempotency-Key' = $appendKey } -ContentType 'application/json' -Body $moreBody
$replay = Invoke-RestMethod -Method Post -Uri "$base/api/table-sessions/$($session.session_id)/items" -Headers @{ 'Idempotency-Key' = $appendKey } -ContentType 'application/json' -Body $moreBody
$current = Invoke-RestMethod -Method Get -Uri "$base/api/table-sessions/$($session.session_id)"
if ($first.clover_order_id -ne $second.clover_order_id -or $current.clover_order_id -ne $first.clover_order_id) { throw 'Clover order ID changed' }
$second.order
if ($replay.order.total -ne $second.order.total) { throw 'Replay total changed' }
```

Each POST above creates or changes a real **Sandbox** order only when you run
it. The generated test table avoids resuming an unrelated open T12 session.
Confirm in Clover that the new line appears once on the same check and the
reported total matches the Clover order total (for a tax/discount-free $16.99
Biryani plus $4.99 Lassi, $21.98). If the API returns reconciliation-required,
inspect the order in Clover before sending any new request. Do not treat the
check as payment-ready until Sandbox verification succeeds. Clover
Dining table mapping is still unvalidated: our table-session ID is not a
Clover Dining table assignment. Payments, automatic closure, and final closure
rules are not implemented. No live append was executed by the automated tests.
These development routes do not yet have customer or staff authentication; do
not expose them publicly without access control and rate limits.

## Sandbox order creation

`POST /api/orders` creates an order through Clover's documented
`POST /v3/merchants/{mId}/atomic_order/orders` endpoint. Set
`CLOVER_ORDER_TYPE_ID` to an order type belonging to your Sandbox merchant;
the backend checks it through `GET /v3/merchants/{mId}/order_types/{id}` before
creating an order. No order type is hard-coded. Obtain/verify the ID manually in
your merchant's Clover configuration. A missing ID returns sanitized HTTP 503.
This endpoint is restricted to the configured development/Sandbox environment
and exact Sandbox API base URL. It reuses the selected sandbox-token or OAuth
credential provider; OAuth mode never falls back to `CLOVER_API_TOKEN`.

The request accepts a required `table` (trimmed, 1–32 printable characters) and
1–20 unique `item_id`/integer `quantity` pairs (1–10 each, at most 100 units).
It requires an `Idempotency-Key` header (8–128 safe characters). Prices, names,
and totals are never accepted from callers. One filtered Clover inventory GET
checks the requested IDs, current prices, and orderability. Missing, hidden,
unavailable, deleted, age-restricted, or per-unit-priced items are rejected.
Each fixed-price quantity is represented by repeated item entries in
`orderCart.lineItems`; Clover's `unitQty` has different fixed-point semantics
for `PER_UNIT` items and is not used. The order type goes in
`orderCart.orderType.id`.

`table` is returned and saved in the local idempotency record, but **is not sent
to Clover**: the documented atomic-order payload does not establish a safe
table field. It therefore will not appear as a Clover Dining table assignment.
The response uses actual Clover order totals in major currency units. If Clover
omits subtotal, tax, total, or currency, that field is `null`; no amount is
invented. Item names and unit prices come from the server-read inventory.

The same key and identical request returns the saved success without another
Clover POST. A key reused for a different order returns HTTP 409. Claiming is
atomic in local SQLite at `backend/.local/orders.sqlite3`, with only hashes of
keys and request bodies persisted. Concurrent same-key requests in one process
wait for the first result. A pending claim after a crash, or an uncertain
post-send outcome (timeout, network error, server error, malformed success),
returns HTTP 409 on reuse: **do not automatically retry**. Investigate in Clover
and reconcile manually before using a new key. A definite validation rejection
(400/404/422) does not cache success; the key may be retried after correction.
No payment, checkout, modifiers, discounts, tax override, or refunds are
implemented. Local SQLite and single-worker operation are development-only;
production needs a shared durable store and operational reconciliation process.
The endpoint has no application-level customer authentication yet; do not expose
it publicly without access controls and rate limits.

After configuring your own Sandbox merchant, token/OAuth connection, and order
type, this PowerShell example creates one real **Sandbox** order only when you
run it. Replace placeholders yourself; never paste credentials into the command:

```powershell
$body = @{ table = 'T12'; items = @(@{ item_id = '<SANDBOX_ITEM_ID>'; quantity = 2 }) } | ConvertTo-Json -Depth 4
$headers = @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/orders' -Headers $headers -ContentType 'application/json' -Body $body
```

If using an HTTPS tunnel, substitute your own HTTPS origin in the URI. Do not
run this command against a production merchant. Automated tests use only
synthetic inventory, temporary SQLite files, and mocked Clover HTTP.

## Clover Sandbox Menu Import

The importer reads `data/chennai_cafe_menu.json`, compares it with the configured
merchant's inventory, and writes a text summary plus an ignored JSON report in
`.local/menu-import/`. It is development/Sandbox-only and uses the existing
configured Clover merchant and selected sandbox-token or OAuth credentials.
From `backend`, run the dry run first (also the default when no mode is given):

```powershell
.\.venv\Scripts\python.exe -m scripts.import_menu --dry-run
```

Apply requires a separate explicit confirmation:

```powershell
.\.venv\Scripts\python.exe -m scripts.import_menu --apply --confirm APPLY_SANDBOX_MENU
```

Existing inventory is never deleted or renamed, and existing item prices are
never overwritten. Name-matched price conflicts block apply. Weekend availability
is metadata only and is not enforced in Clover. The Special Thali dosa choices
are Sada Dosa and Masala Dosa at +$0.00 in the master file. Review a clean dry
run before any manual Sandbox apply. Each POST is sent once;
an uncertain or partial outcome requires a fresh dry run and manual reconciliation.

## Categorized customer menu and modifiers

`GET /api/menu` returns `categories`, ordered by the local master menu's
`display_order`, with each category's Clover ID, name, and `items`. Each item
contains its Clover ID, name, authoritative price, USD currency, availability
type, and its Clover modifier groups/options with IDs, price deltas, and
selection limits. Unknown Clover categories follow the 15 known categories;
uncategorized Clover items appear in a final `Uncategorized` group. Empty
description, image, and dietary fields are placeholders only. No Clover IDs
are hardcoded in the frontend.

Clover supplies item existence, category membership, prices, modifier IDs,
option prices, and selection rules. The master JSON supplies only category
display order and availability metadata. Weekend metadata is shown but does
not block ordering. A Clover/master price discrepancy uses Clover's price and
logs only the item name in development. A normalized, token-free in-process
menu cache lasts `MENU_CACHE_TTL_SECONDS` (default 60, 0 disables caching).
The current single-worker development mode does not use Redis.

The frontend renders categories dynamically. For example, adding Lassi opens
its Clover-provided `Lassi Style` options. A required selection such as Sweet
is sent as a modifier ID, not a name or price. The backend re-reads Clover
relationships and prices, validates min/max and item ownership, and sends the
modifier with the atomic order or applies it to a newly appended line item.
Append totals are recalculated from Clover line-item modifications. An
uncertain multi-step append still requires manual reconciliation; never retry
it under a new key without inspecting the Clover order.

## Run tests

With the virtual environment active and from `backend`:

```powershell
python -m pytest tests -q
```

Tests disable real dotenv loading and remove application setting variables before
application imports. They use synthetic credentials, temporary encrypted databases,
HTTPS ASGI clients, mocked outbound HTTP, and an injectable clock. External HTTP
is blocked; default database paths are redirected to test temporary directories.
Pytest's `tmp_path` uses the OS temporary directory; its cache is kept under
`.local/test-temp/`. Do not pass a backend-root `.pytest-*` path to `--basetemp`.

## Authentication modes

`CLOVER_AUTH_MODE=sandbox_token` remains the default. `/api/menu` uses
`CLOVER_MERCHANT_ID` and `CLOVER_API_TOKEN`. Missing credentials return sanitized
HTTP 503. All OAuth settings may remain blank, with `CLOVER_OAUTH_ENABLED=false`.
Health requires no Clover credentials. Blank/whitespace-only optional URLs become
`None`; nonblank URLs still undergo HTTP/HTTPS validation.

`CLOVER_AUTH_MODE=oauth` uses only the stored access token scoped to the configured
merchant, app, and environment. It never falls back to `CLOVER_API_TOKEN`. Missing,
revoked, unrecoverable, or expired credentials return HTTP 503 with a reconnection
message. OAuth menu access can continue using a stored connection with launch
routes disabled. Both modes reject Clover use outside development/Sandbox;
health remains available to diagnose the application.

`CLOVER_OAUTH_ENABLED=true` separately enables `/clover/launch`, `/clover/callback`,
and `/clover/complete`. Disabled routes return HTTP 501. Enabled but incomplete or
unsafe configuration returns sanitized HTTP 503. It is possible to connect Clover
while keeping the menu in sandbox-token mode, then switch modes manually later.

## Configure OAuth manually

Fill `CLOVER_MERCHANT_ID`, `CLOVER_APP_ID`, `CLOVER_APP_SECRET`,
`CLOVER_TOKEN_ENCRYPTION_KEY`, and an HTTPS `PUBLIC_BASE_URL` origin. The origin
must have no userinfo, path prefix, query, or fragment. Use these exact endpoints:

```dotenv
CLOVER_OAUTH_AUTHORIZE_URL=https://sandbox.dev.clover.com/oauth/v2/authorize
CLOVER_OAUTH_TOKEN_URL=https://apisandbox.dev.clover.com/oauth/v2/token
CLOVER_OAUTH_REFRESH_URL=https://apisandbox.dev.clover.com/oauth/v2/refresh
```

The server accepts only these Sandbox OAuth endpoints and the Sandbox API base
URL in OAuth mode. It constructs `/clover/callback` and `/clover/complete` from the
configured HTTPS origin, never Host/forwarding headers or caller redirect values.
Restart after changing configuration; settings and the provider are process-local.
No real HTTPS hostname, tunnel, or credentials are provisioned by this project.

Configure the Clover developer app conceptually as follows:

| Clover setting | Value |
| --- | --- |
| Site URL | `https://<development-host>/` |
| Alternate Launch Path | `/clover/launch` |
| Application-requested callback | `https://<development-host>/clover/callback` |
| CORS Domain | blank |
| Default OAuth Response | Code |

The development host must serve HTTPS with the same origin registered in Clover.
Grant the app inventory read permission. Open `/clover/launch` on that HTTPS host;
the Secure browser cookie will not work through a plain HTTP callback. CORS is
unnecessary because Clover requests originate from the backend.

## Authorization and refresh behavior

Launch creates independent random state and a browser nonce. Their hashes and
expected merchant/app/environment are stored for at most ten minutes. The nonce
cookie is Secure, HttpOnly, SameSite=Lax, host-only, and bounded to that window.
Launch hints are untrusted; unsolicited authorization codes are rejected.

Callback validates state, cookie, expiry, and identity. State consumption is
atomic and precedes exchange. Denials consume valid state and return a generic
error; no raw Clover error is echoed. A successful exchange is followed by an
inventory read limited to one item against the configured merchant using the new
access token. Only verified tokens are saved. Callback clears the temporary cookie
and redirects to a static, query-free completion page. This is a Clover connection,
not authentication to a restaurant admin portal.

Access tokens refresh within 60 seconds of provider-supplied expiration. A shared
process lock serializes refresh; the token record is reread inside the lock. Both
tokens and their expiration timestamps are replaced atomically, with version
checks so stale refreshes cannot overwrite a newer connection.

Code exchanges and refresh requests are not automatically retried. A durable
`refreshing` marker is written before refresh HTTP begins. A timeout, ambiguous
failure, or restart during that operation requires reconnecting through launch;
the old single-use refresh token is not reused. Recovery-token behavior is
deliberately unimplemented. A failed replacement never writes partial token data.

## Local encrypted storage

Runtime data lives in `backend/.local/clover-oauth.sqlite3`; the entire `.local/`
directory is Git-ignored. SQLite stores merchant/app/environment, authenticated
encrypted tokens, provider timestamps, status, version, update metadata, and
hashed pending authorizations. It stores no plaintext token, code, state, or nonce.
The adapter uses short parameterized transactions with blocking work offloaded
from the event loop and supports the local single-process Sandbox milestone only.

`CLOVER_TOKEN_ENCRYPTION_KEY` is a Fernet key: URL-safe base64 encoding of 32 random
bytes. Generate your own with trusted local tooling using
`cryptography.fernet.Fernet.generate_key()`, and save it securely outside the
database in secret configuration. No real key is supplied or generated here.
Keep the same key across normal restarts. Losing/changing it makes existing tokens
unreadable; reconnect after securely retiring the old local data. Back up the key
separately from the encrypted database, and restrict local file permissions.

## Safe logging

Use the exact `--no-access-log --no-proxy-headers` single-worker run command above.
Uvicorn access logs would otherwise include OAuth callback query strings. The
application also suppresses Uvicorn access and HTTPX/httpcore log channels and
emits only HTTP method, matched route path, and status. Unknown paths use a fixed
label; automatic slash redirects are disabled. Errors never log raw provider
exceptions or token bodies. OAuth responses use `Cache-Control: no-store` and
`Referrer-Policy: no-referrer` plus the existing security headers.

Configure any future HTTPS proxy, tunnel, APM, or request recorder to omit query
strings, cookies, headers, and bodies for OAuth endpoints. This application cannot
control external logging. TLS verification stays enabled, outbound redirects are
disabled, and no wildcard CORS or browser token-management endpoints are exposed.

## Protocol references and documentation conflicts

For atomic orders, [Clover's atomic-order guide](https://docs.clover.com/dev/docs/create-an-atomic-order)
shows `orderCart.lineItems` as an array and `orderCart.orderType.id`; this is
the request form used here. A separate
[age-restricted-items example](https://docs.clover.com/dev/docs/manage-age-restricted-items-orders-api)
shows `lineItems.elements` instead. Neither example provides an unambiguous
safe table field, so table stays platform-only. The
[Orders FAQ](https://docs.clover.com/dev/docs/orders-faqs) also distinguishes
Orders/Register visibility from Clover Dining table assignment. The documented
[inventory filter](https://docs.clover.com/dev/docs/using-filters-to-retrieve-multiple-results-from-an-endpoint)
supports a bounded `id in (...)` lookup. A manual Sandbox acceptance test is
still needed to confirm the request shape and returned fields for this merchant.

Implementation follows the concrete JSON POST examples in
[Clover's v2 token guide](https://docs.clover.com/dev/docs/generate-expiring-tokens-using-v2-oauth-flow)
and [refresh guide](https://docs.clover.com/dev/docs/refresh-access-tokens): exchange
sends `client_id`, `client_secret`, `code`; refresh sends `client_id`, `refresh_token`.
Expirations are provider Unix timestamps, never assumed token lifetimes.

The [high-trust guide](https://docs.clover.com/dev/docs/high-trust-app-auth-flow)
calls exchange values query parameters in prose, but its request body and the v2
guide's cURL example use JSON. We follow JSON; secrets never go in URLs. It also
mentions `client_redirect_URL` in prose but its authorization example uses
`redirect_uri`, which is what we send.

The [flow overview](https://docs.clover.com/dev/docs/oauth-flows-in-clover) has one
authorization example on `apisandbox.dev.clover.com`; its endpoint table and the
high-trust guide agree on `sandbox.dev.clover.com`, which we use. The overview's
state example supplies `client_id`, `response_type=code`, `redirect_uri`, `state`.
The token guide's ambiguous launch prose does not bypass our validated callback.

[Clover's CORS guide](https://docs.clover.com/dev/docs/using-cors) describes browser
API calls and mentions a token response option. This backend uses the server-side
code flow and leaves CORS Domain blank; it does not implement the legacy implicit
flow or render token values, even where documentation screenshots do so.

## Remaining production requirements

Production needs a replacement token store (for example PostgreSQL), managed key
storage/rotation, distributed refresh coordination, deployment/logging hardening,
connection ownership/admin authentication, rate limits, consent lifecycle and
revocation handling, and monitoring. Local SQLite and sandbox-token mode are
explicitly rejected outside development/Sandbox. Multi-worker deployments and
automatic recovery tokens are unsupported. A real HTTPS development host and a
manual Sandbox OAuth acceptance test are still required; automated tests make no
live Clover requests.
