"""Return window + platform fee: seller is paid only after the window ends; returns refund the full amount, no fee."""
import os, tempfile
from datetime import datetime, timedelta
os.environ["SAFETRADE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["SAFETRADE_UPLOADS"] = tempfile.mkdtemp()
import app as A
assert A.FEE_PCT == 5 and A.RETURN_DAYS == 4   # defaults

conn = A.connect()
uid = {r["username"]: r["id"] for r in conn.execute("SELECT * FROM users")}
alice, bob, admin = uid["alice"], uid["bob"], uid["admin"]
bal = lambda u: A.balance(conn, u)
status = lambda o: conn.execute("SELECT status FROM orders WHERE id=?", (o,)).fetchone()[0]
later = lambda d: (datetime.now() + timedelta(days=d)).isoformat(timespec="seconds")
def refused(fn, *a):
    try: fn(*a)
    except A.Refused: return True
    return False
def sale(price):
    lid = conn.execute("INSERT INTO listings(seller_id,title,price,created) VALUES(?,?,?,?)", (alice, "Thing", price, A.now())).lastrowid
    oid = A.buy(conn, lid, bob); A.approve(conn, oid, alice)
    assert A.approve(conn, oid, bob) == "return_window"
    return lid, oid

# window ends with no return -> seller paid minus 5%
a, b = bal(alice), bal(bob)
lid, oid = sale(10000)
assert status(oid) == "return_window" and bal(alice) == a and bal(bob) == b - 10000
assert A.pending_payouts(conn, alice) == 9500 and A.escrow_held(conn, bob) == 10000
assert A.release_due(conn, later(3)) == 0 and bal(alice) == a
assert refused(A.request_return, conn, oid, alice)            # seller can't "return"
assert refused(A.cancel, conn, oid, alice) and refused(A.cancel, conn, oid, bob)
assert A.release_due(conn, later(5)) == 1
assert bal(alice) == a + 9500 and status(oid) == "released" and A.pending_payouts(conn, alice) == 0
assert conn.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0] == "sold"
assert refused(A.request_return, conn, oid, bob)              # too late

# return on day 4: full refund, no fee, item back on sale
a, b = bal(alice), bal(bob)
lid, oid = sale(4000)
A.request_return(conn, oid, bob, "Changed my mind")
assert status(oid) == "return_requested"
assert A.release_due(conn, later(30)) == 0                    # a pending return is never auto-paid
assert refused(A.confirm_return, conn, oid, bob)              # only the seller confirms
A.confirm_return(conn, oid, alice)
assert bal(bob) == b and bal(alice) == a and status(oid) == "refunded"
assert conn.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0] == "active"

# returned item damaged -> seller disputes, admin decides
lid, oid = sale(2000); A.request_return(conn, oid, bob)
A.dispute(conn, oid, alice, "Came back cracked"); assert status(oid) == "disputed"
a = bal(alice); A.resolve(conn, oid, admin, "release"); assert bal(alice) == a + 1900

# buyer can dispute inside the window too
lid, oid = sale(1000); A.dispute(conn, oid, bob, "Stopped working day 3")
assert A.release_due(conn, later(30)) == 0 and status(oid) == "disputed"

# money is conserved
total = conn.execute("SELECT SUM(amount) FROM ledger").fetchone()[0] + A.escrow_held(conn)
fees = conn.execute("SELECT SUM(fee) FROM orders WHERE status='released'").fetchone()[0]
assert total + fees == 100000

# old databases get the new columns
import sqlite3
p = tempfile.mktemp(suffix=".db"); c = sqlite3.connect(p)
c.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, listing_id INTEGER, buyer_id INTEGER, seller_id INTEGER, amount INTEGER, fee INTEGER, status TEXT, buyer_ok INTEGER, seller_ok INTEGER, dispute_by INTEGER, dispute_reason TEXT, created TEXT, closed TEXT)")
c.commit(); c.close()
A.DB = p; c = A.connect(); A.init_db(c)
assert {"return_until", "return_reason"} <= {r[1] for r in c.execute("PRAGMA table_info(orders)")}
A.DB = os.environ["SAFETRADE_DB"]
print("returns core OK")

# ---------- HTTP ----------
def client(user):
    c = A.app.test_client(); c.get("/login"); post(c, "/login", username=user, password="demo1234"); return c
def tok(c):
    with c.session_transaction() as s: return s["csrf"]
def post(c, url, **data): return c.post(url, data={"csrf": tok(c), **data})
seller, buyer = client("alice"), client("bob")
assert b"4 days" in buyer.get("/").data and b"4 days" in buyer.get("/listing/1").data
lid = int(post(seller, "/sell", title="Drill", price="80").headers["Location"].rsplit("/", 1)[1])
oid = int(post(buyer, f"/listing/{lid}/buy").headers["Location"].rsplit("/", 1)[1])
post(seller, f"/order/{oid}/approve"); post(buyer, f"/order/{oid}/approve")
h = buyer.get(f"/order/{oid}").data; assert b"Return this item" in h and b"Return window" in h
h = seller.get(f"/order/{oid}").data; assert b"$76.00" in h and b"Return this item" not in h
assert b"waiting to be paid out" in seller.get("/wallet").data
post(buyer, f"/order/{oid}/return", reason="Too heavy")
h = seller.get(f"/order/{oid}").data; assert b"Too heavy" in h and b"I got it back" in h
assert b"your action" in seller.get("/orders").data
b0 = A.balance(conn, bob); post(seller, f"/order/{oid}/return/confirm")
assert A.balance(conn, bob) == b0 + 8000 and b"Refunded" in buyer.get(f"/order/{oid}").data

# auto-payout happens on the next page load after the window
oid = int(post(buyer, f"/listing/{lid}/buy").headers["Location"].rsplit("/", 1)[1])
post(seller, f"/order/{oid}/approve"); post(buyer, f"/order/{oid}/approve")
conn.execute("UPDATE orders SET return_until=? WHERE id=?", (later(-1), oid))
a0 = A.balance(conn, alice); h = seller.get(f"/order/{oid}").data
assert A.balance(conn, alice) == a0 + 7600 and b"Return window ended" in h and b"SafeTrade" in h
print("ALL RETURN TESTS PASSED")
