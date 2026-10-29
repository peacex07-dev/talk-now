"""Escrow tests: money only reaches the seller after BOTH approve; cancel/dispute/refund rules; HTTP flow."""
import os, tempfile, re
os.environ["SAFETRADE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["SAFETRADE_UPLOADS"] = tempfile.mkdtemp()
import app as A

conn = A.connect()
uid = {r["username"]: r["id"] for r in conn.execute("SELECT * FROM users")}
alice, bob, admin = uid["alice"], uid["bob"], uid["admin"]
bal = lambda u: A.balance(conn, u)
def new_listing(seller, price):
    return conn.execute("INSERT INTO listings(seller_id,title,price,created) VALUES(?,?,?,?)",
                        (seller, "Thing", price, A.now())).lastrowid
def refused(fn, *a):
    try: fn(*a)
    except A.Refused: return True
    return False
total = lambda: conn.execute("SELECT SUM(amount) FROM ledger").fetchone()[0] + A.escrow_held(conn)

# money parsing
assert A.parse_money("12.5") == 1250 and A.parse_money("$1,200") == 120000 and A.parse_money("0.01") == 1
assert A.parse_money("0") is None and A.parse_money("-5") is None and A.parse_money("1.234") is None
assert A.parse_money("abc") is None and A.parse_money("100001") is None
assert bal(alice) == 50000 and bal(bob) == 50000
start = total()

# happy path: seller is NOT paid until both approve, in either order
lid = new_listing(alice, 20000)
assert refused(A.buy, conn, lid, alice)                       # can't buy own item
oid = A.buy(conn, lid, bob)
assert bal(bob) == 30000 and bal(alice) == 50000 and A.escrow_held(conn) == 20000
assert refused(A.buy, conn, lid, bob)                         # already pending
assert A.approve(conn, oid, bob) == "funded" and bal(alice) == 50000
assert refused(A.approve, conn, oid, bob)                     # no double approval
assert A.approve(conn, oid, alice) == "released"
assert bal(alice) == 70000 and A.escrow_held(conn) == 0
assert conn.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0] == "sold"
assert refused(A.cancel, conn, oid, alice) and refused(A.dispute, conn, oid, bob, "x")   # final

lid = new_listing(alice, 1000); oid = A.buy(conn, lid, bob)
A.approve(conn, oid, alice); assert bal(alice) == 70000        # seller first, still held
A.approve(conn, oid, bob); assert bal(alice) == 71000

# insufficient funds, outsiders
lid = new_listing(alice, 999999)
assert refused(A.buy, conn, lid, bob)
lid = new_listing(alice, 500); oid = A.buy(conn, lid, bob)
assert refused(A.approve, conn, oid, admin) and refused(A.cancel, conn, oid, admin)

# buyer cancels before seller approval -> refund, listing back on sale
b = bal(bob); A.cancel(conn, oid, bob); assert bal(bob) == b + 500
assert conn.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0] == "active"

# buyer can't cancel after confirming receipt (would keep item and money)
oid = A.buy(conn, lid, bob); A.approve(conn, oid, bob)
assert refused(A.cancel, conn, oid, bob)
A.cancel(conn, oid, alice)
# buyer can't cancel after seller confirmed handover; seller can
oid = A.buy(conn, lid, bob); A.approve(conn, oid, alice)
assert refused(A.cancel, conn, oid, bob)
b = bal(bob); A.cancel(conn, oid, alice); assert bal(bob) == b + 500

# dispute freezes funds; only admin resolves
oid = A.buy(conn, lid, bob); a, b = bal(alice), bal(bob)
assert refused(A.dispute, conn, oid, bob, "  ")
A.dispute(conn, oid, bob, "Item broken")
assert refused(A.approve, conn, oid, alice) and refused(A.cancel, conn, oid, alice)
assert bal(alice) == a and bal(bob) == b and A.escrow_held(conn) == 500
assert refused(A.resolve, conn, oid, admin, "bogus")
A.resolve(conn, oid, admin, "refund"); assert bal(bob) == b + 500 and bal(alice) == a
assert refused(A.resolve, conn, oid, admin, "release")
oid = A.buy(conn, lid, bob); A.dispute(conn, oid, alice, "Buyer has it, won't approve")
A.resolve(conn, oid, admin, "release"); assert bal(alice) == a + 500

# withdraw can't exceed available balance; money is conserved (ledger + escrow) apart from deposits/withdrawals
assert refused(A.withdraw, conn, bob, bal(bob) + 1)
A.deposit(conn, bob, 100); A.withdraw(conn, bob, 100)
assert total() == start

# fee
A.FEE_PCT = 2.5; lid = new_listing(alice, 10000); oid = A.buy(conn, lid, bob); a = bal(alice)
A.approve(conn, oid, bob); A.approve(conn, oid, alice); assert bal(alice) == a + 9750
A.FEE_PCT = 0
print("escrow core OK")

# ---------- HTTP ----------
def client(user=None, pw="demo1234"):
    c = A.app.test_client(); c.get("/login")
    if user: login(c, user, pw)
    return c
def tok(c):
    with c.session_transaction() as s: return s["csrf"]
def post(c, url, **data): return c.post(url, data={"csrf": tok(c), **data})
def login(c, u, pw="demo1234"): return post(c, "/login", username=u, password=pw)

anon = client()
h = anon.get("/").data; assert b"iPhone 14" in h and b"protected" in h
assert anon.get("/sell").status_code == 302 and anon.get("/wallet").status_code == 302
assert anon.post("/login", data={"username": "alice", "password": "demo1234"}).status_code == 400   # CSRF
assert b"Wrong username" in login(client(), "alice", "nope").data
assert b"iPhone" in anon.get("/?q=iphone").data and b"iPhone" not in anon.get("/?cat=Toys").data

s = client(); assert post(s, "/signup", username="carol", password="password1").status_code == 302
assert b"taken" in post(client(), "/signup", username="carol", password="password1").data
assert b"8 characters" in post(client(), "/signup", username="dave", password="short").data

seller, buyer, adm = client("alice"), client("bob"), client("admin", "admin1234")
assert b"Enter a price" in post(seller, "/sell", title="Lamp", price="abc").data
r = post(seller, "/sell", title="<b>Lamp</b>", price="45.00", category="Furniture", location="Lafayette", body="Works")
lid = int(r.headers["Location"].rsplit("/", 1)[1])
h = buyer.get(f"/listing/{lid}").data; assert b"&lt;b&gt;Lamp" in h and b"$45.00" in h and b"Buy with Safe Transfer" in h
assert b"Buy with Safe Transfer" not in seller.get(f"/listing/{lid}").data

r = post(buyer, f"/listing/{lid}/buy"); oid = int(r.headers["Location"].rsplit("/", 1)[1])
assert buyer.get("/").data.count(b"Lamp") == 0                               # no longer browsable
assert client("carol", "password1").get(f"/order/{oid}").status_code == 404   # outsiders can't see it
h = buyer.get(f"/order/{oid}").data; assert b"In escrow" in h and b"I received it" in h
post(buyer, f"/order/{oid}/message", body="Pickup Saturday?")
assert b"Pickup Saturday?" in seller.get(f"/order/{oid}").data
alice_before = A.balance(conn, alice)
post(buyer, f"/order/{oid}/approve")
assert A.balance(conn, alice) == alice_before
assert b"your action" in seller.get("/orders").data
post(seller, f"/order/{oid}/approve")
assert A.balance(conn, alice) == alice_before + 4500
assert b"Completed" in buyer.get(f"/order/{oid}").data and b"Sale payout" in seller.get("/wallet").data

# dispute through UI, admin resolves
r = post(seller, "/sell", title="Chair", price="10", category="Furniture")
lid = int(r.headers["Location"].rsplit("/", 1)[1])
oid = int(post(buyer, f"/listing/{lid}/buy").headers["Location"].rsplit("/", 1)[1])
post(buyer, f"/order/{oid}/dispute", reason="Never showed up")
assert buyer.get("/admin").status_code == 403
assert b"Never showed up" in adm.get("/admin").data
b = A.balance(conn, bob); post(adm, f"/admin/resolve/{oid}", outcome="refund")
assert A.balance(conn, bob) == b + 1000 and b"No open disputes" in adm.get("/admin").data

# wallet
b = A.balance(conn, bob); post(buyer, "/wallet/deposit", amount="25.50"); assert A.balance(conn, bob) == b + 2550
post(buyer, "/wallet/withdraw", amount="99999")
assert b"more than your available" in buyer.get("/wallet").data and A.balance(conn, bob) == b + 2550

# image upload: real PNG accepted, fake rejected
import io
png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
r = seller.post("/sell", data={"csrf": tok(seller), "title": "Pic", "price": "5", "image": (io.BytesIO(png), "p.png")},
                content_type="multipart/form-data")
lid = int(r.headers["Location"].rsplit("/", 1)[1]); img = re.search(rb"/uploads/(\w+\.png)", seller.get(f"/listing/{lid}").data).group(1)
assert seller.get("/uploads/" + img.decode()).status_code == 200
r = seller.post("/sell", data={"csrf": tok(seller), "title": "Bad", "price": "5", "image": (io.BytesIO(b"<script>"), "x.png")},
                content_type="multipart/form-data")
assert b"Photo must be" in r.data
print("ALL ESCROW TESTS PASSED")
