# SafeTrade: marketplace with escrow

A Facebook Marketplace-style app where payment goes through the app and the seller is only paid
once **both** buyer and seller approve. Flask + SQLite, plain HTML/CSS, mobile-friendly.

## Run
    pip install flask
    cd marketplace
    python3 app.py                      # http://localhost:5080
Demo logins: `alice / demo1234`, `bob / demo1234` (each starts with $500 demo money), `admin / admin1234`
(set `SAFETRADE_ADMIN_PW` before first run to change it). Reset: delete `safetrade.db`.
Tests: `python3 test_escrow.py`.

Env: `SECRET_KEY` (set it in production, or sessions reset on restart), `SAFETRADE_DB`, `SAFETRADE_UPLOADS`,
`SAFETRADE_FEE_PCT` (platform fee taken from the seller's payout, default 0), `PORT`.

## How a sale works
1. Buyer taps **Buy with Safe Transfer**. The price leaves the buyer's wallet and is held in escrow. The listing is hidden.
2. Buyer and seller arrange pickup/shipping in the order's message thread.
3. Seller approves ("I handed it over"). Buyer approves ("I received it as described"). Order doesn't matter.
4. When both have approved, the money is released to the seller's wallet. Seller can withdraw.

| Situation | Who can do what |
|---|---|
| Neither approved yet | Buyer or seller can cancel, buyer is refunded, listing goes back on sale |
| One side approved | Seller can still cancel and refund; buyer can't (they'd keep item and money) |
| Anything wrong | Either side opens a dispute: funds are frozen |
| Disputed | Admin (`/admin`) refunds buyer or releases to seller |
| Released / refunded | Final |

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
