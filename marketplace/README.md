# SafeTrade: marketplace with escrow

A Facebook Marketplace-style app where payment goes through the app and the seller is only paid
once **both** buyer and seller approve. Flask + SQLite, plain HTML/CSS, mobile-friendly.

## Run
    pip install flask
    cd marketplace
    python3 app.py                      # http://localhost:5080
Demo logins: `alice / demo1234`, `bob / demo1234` (each starts with $500 demo money), `admin / admin1234`
(set `SAFETRADE_ADMIN_PW` before first run to change it). Reset: delete `safetrade.db`.
Tests: `python3 test_escrow.py && python3 test_returns.py && python3 test_neighbors.py && python3 test_public.py`.

Env: `SECRET_KEY` (set it in production, or sessions reset on restart), `SAFETRADE_DB`, `SAFETRADE_UPLOADS`,
`SAFETRADE_FEE_PCT` (platform fee taken from the seller's payout on completed sales, default 5),
`SAFETRADE_RETURN_DAYS` (no-questions-asked return window, default 1; 0 pays the seller as soon as both approve), `PORT`.

## Put it online (Render)
`render.yaml` at the repo root sets everything up: Render dashboard > New > Blueprint > pick this repo and branch,
enter an admin password when asked, Apply. About $7.25/month (Starter instance + 1 GB disk for the database and photos).
The public site starts empty: no alice/bob demo accounts (`SAFETRADE_DEMO_DATA=0`), only `admin` with your password.
Every page shows a "test version, pretend money" banner until real payments (Stripe Connect) are wired in.

## Photos
Up to 8 photos per listing (pick several at once on a phone). Each upload is turned upright, saved as a 1600px JPEG
plus a 640px thumbnail for browse tiles, so big phone photos load fast. HEIC (iPhone) works via pillow-heif.
Photos are kept if the rest of the sell form has an error, and sellers can add photos to a live listing.
Browse has a Photos (grid) and List view. Price cap is $10,000,000.

## How a sale works
1. Buyer taps **Buy with Safe Transfer**. The price leaves the buyer's wallet and is held in escrow. The listing is hidden.
2. Buyer and seller arrange pickup/shipping in the order's message thread.
3. Seller approves ("I handed it over"). Buyer approves ("I received it as described"). Order doesn't matter.
4. When both have approved, a 1-day return window starts. The money stays held.
5. Buyer can return the item any time in the window, no reason needed. When the seller confirms they have it back,
   the buyer gets the full amount and the listing goes back on sale. No fee is charged on a return.
6. If the window ends with no return, the seller is paid automatically (price minus the 5% fee) and can withdraw.

| Situation | Who can do what |
|---|---|
| Neither approved yet | Buyer or seller can cancel, buyer is refunded, listing goes back on sale |
| One side approved | Seller can still cancel and refund; buyer can't (they'd keep item and money) |
| Return window | Buyer can start a return; nobody can cancel |
| Return started | Seller confirms "I got it back" and the buyer is refunded; never auto-paid to the seller |
| Anything wrong (incl. item returned damaged) | Either side opens a dispute: funds are frozen |
| Disputed | Admin (`/admin`) refunds buyer or releases to seller |
| Released / refunded | Final |

## Neighbors (no money)
`/neighbors` is a community board for things nobody is selling: **Need a hand** (moving, a ride, advice),
**Borrow** (tools, a trailer, a ladder) and **Free stuff** (what you'd toss that someone else could use).
Anyone can browse; logged-in users post (optional photo), reply, and the poster marks it done, reopens or deletes it.
Done posts sink to the bottom. Sample posts are seeded on first run.

## Design notes
- Money is integer cents. The `ledger` table (deposit, withdraw, escrow_hold, escrow_release, escrow_refund) is the
  source of truth; a balance is the sum of a user's rows. Escrow held = sum of open orders.
- Every money move runs in one `BEGIN IMMEDIATE` transaction that re-checks the order state, so two buyers can't pay
  for the same item and a release can't happen twice.
- Passwords hashed (werkzeug), CSRF token on every form, session regenerated on login, uploads checked by
  extension and file signature, user text auto-escaped.

## Before real money
Wallet deposits/withdrawals are **simulated**. To go live, replace `deposit`/`withdraw`/`_release` with a
payment provider that supports holding funds for marketplaces (Stripe Connect with separate charges and
transfers, or an escrow provider such as Escrow.com / Trustap). You'll also need KYC for sellers,
money-transmitter compliance (handled by the provider if you never hold funds yourself), a policy for buyers who
never approve (e.g. auto-release N days after seller confirms unless disputed), and a real database (Postgres).
