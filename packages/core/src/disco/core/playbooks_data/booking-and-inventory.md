# Search, filters, booking without double-booking, carts, orders and stock

> Filterable listings, date-range availability with a transactional guard, price calculation, cart → order state machines with realtime status, inventory thresholds and out-of-stock rules.

## Listings search
`GET /api/listings?location=&from=&to=&min_price=&max_price=&type=&page=&per=24` → build the WHERE clause from present params only (`LIKE` on city/region for location, exact `type`, price bounds), exclude listings with an overlapping confirmed booking when `from/to` are given (subquery below), order by relevance then price, return `{ items, total, page }`. Never interpolate user input into SQL — bind parameters.

## Availability (no double booking)
```sql
CREATE TABLE bookings(id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL, guest_id INTEGER NOT NULL,
  check_in TEXT NOT NULL, check_out TEXT NOT NULL, guests INTEGER NOT NULL, total_cents INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending_payment','confirmed','cancelled')), payment_status TEXT,
  version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK(check_out > check_in));
CREATE INDEX bookings_listing_dates ON bookings(listing_id, check_in, check_out);
```
Overlap: `a.check_in < b.check_out AND b.check_in < a.check_out`. Create a booking inside one write transaction so two guests cannot both pass the check:
```js
export function createBooking(db, { listingId, guestId, checkIn, checkOut, guests }) {
  return db.transaction(() => {
    const clash = db.prepare(`SELECT id FROM bookings WHERE listing_id=? AND status IN ('pending_payment','confirmed')
      AND check_in < ? AND ? < check_out AND (status='confirmed' OR created_at > ?) LIMIT 1`)
      .get(listingId, checkOut, checkIn, new Date(Date.now() - 15 * 60_000).toISOString());   // pending holds expire after 15 min
    if (clash) { const e = new Error("dates no longer available"); e.status = 409; throw e; }
    const listing = db.prepare("SELECT nightly_cents, cleaning_cents, max_guests FROM listings WHERE id=? AND archived=0").get(listingId);
    if (!listing) { const e = new Error("listing unavailable"); e.status = 404; throw e; }
    if (guests > listing.max_guests) { const e = new Error(`max ${listing.max_guests} guests`); e.status = 400; throw e; }
    const nights = Math.round((Date.parse(checkOut) - Date.parse(checkIn)) / 86_400_000);
    const total = nights * listing.nightly_cents + listing.cleaning_cents;
    const now = new Date().toISOString();
    const { lastInsertRowid } = db.prepare(`INSERT INTO bookings(listing_id,guest_id,check_in,check_out,guests,total_cents,status,created_at,updated_at)
      VALUES(?,?,?,?,?,?,'pending_payment',?,?)`).run(listingId, guestId, checkIn, checkOut, guests, total, now, now);
    return { id: Number(lastInsertRowid), nights, total_cents: total };
  })();
}
```
node:sqlite's `db.transaction` (or `BEGIN IMMEDIATE` … `COMMIT` by hand) serialises writers; SQLite's single-writer lock is the guarantee. After commit: `broadcast("listing:"+listingId, "availability:changed", { listingId, from, to })` so open detail pages grey out the dates live. Show the price breakdown (nights × nightly + cleaning) computed by `GET /api/listings/:id/quote?from&to&guests` — the same function, dry-run.

## Listing management
CRUD under `/api/host/listings` (owner only; `archived` flag instead of delete when bookings exist; hard delete only when none). Dashboard: `GET /api/me/trips` (as guest) and `GET /api/me/hosting` (bookings on my listings), both with status and payment status.

## Cart → order (pharmacy / shop shape)
```sql
CREATE TABLE cart_items(user_id INTEGER NOT NULL, medication_id INTEGER NOT NULL, qty INTEGER NOT NULL CHECK(qty>0), PRIMARY KEY(user_id, medication_id));
CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL
  CHECK(status IN ('pending','processing','ready_for_pickup','completed','cancelled')), total_cents INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE order_items(order_id INTEGER NOT NULL, medication_id INTEGER NOT NULL, qty INTEGER NOT NULL, unit_cents INTEGER NOT NULL);
CREATE TABLE medications(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, unit_cents INTEGER NOT NULL,
  stock INTEGER NOT NULL DEFAULT 0, low_stock_threshold INTEGER NOT NULL DEFAULT 10, out_of_stock INTEGER NOT NULL DEFAULT 0, category TEXT);
```
- Placing an order (one transaction): verify every item `stock >= qty AND out_of_stock = 0` else 409 naming the item; decrement `stock`; insert order+items; clear the cart; broadcast `order:created` to room `pharmacists` and `order:updated` to room `user:<id>`.
- Allowed transitions: `pending→processing→ready_for_pickup→completed`, any→`cancelled` (restock on cancel). Reject others with 409. Each transition: update, email (email playbook), broadcast to both rooms.
- Low stock: after any decrement, if `stock <= low_stock_threshold` insert/refresh a `stock_alerts` row and broadcast `stock:low` to `pharmacists`; the dashboard lists them until stock is raised. `out_of_stock=1` hides "add to cart" for patients and blocks ordering server-side.
- Patient views: browse with search (`name LIKE`), category filter, price sort; cart with quantities; order history with a live status timeline.

## Prove it
Two guests book the same dates in two contexts: exactly one succeeds, the other sees "dates no longer available" and the calendar greys out live; the quote equals nights × rate + cleaning; a pharmacist moving an order to `ready_for_pickup` updates the patient's page without reload and sends the email; ordering more than stock fails with the item name; stock hitting the threshold shows a low-stock alert; an out-of-stock item cannot be added.
