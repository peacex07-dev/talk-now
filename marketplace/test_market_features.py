"""Marketplace basics: message seller before buying, inbox/unread, filters and sort, condition, edit + price drop,
saved items, seller profile and ratings, off-site payment warning."""
import os, tempfile, re
os.environ["SAFETRADE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["SAFETRADE_UPLOADS"] = tempfile.mkdtemp()
import app as A
A.FEE_PCT = 0; A.RETURN_DAYS = 0

def client(user=None):
    c = A.app.test_client(); c.get("/login")
    if user: post(c, "/login", username=user, password="demo1234")
    return c
def tok(c):
    with c.session_transaction() as s: return s["csrf"]
def post(c, url, **data): return c.post(url, data={"csrf": tok(c), **data})
def loc(r): return r.headers["Location"]
alice, bob, anon = client("alice"), client("bob"), client()

# ages and conditions show; anonymous visitors are asked to log in to message
h = anon.get("/listing/1").data
assert b"Listed just now in Lafayette, LA" in h and b"Used - Like new" in h and b"Log in to message or buy" in h
assert b"No ratings yet" in h and b"Joined" in h
assert A.ago("2026-01-01T00:00:00") and A.ago(A.now()) == "just now"

# filters and sort
h = anon.get("/?min=100&max=400").data
assert b"Trek road bike" in h and b"Mid-century armchair" in h and b"iPhone 14" not in h and b"LEGO" not in h
h = anon.get("/?cond=New").data; assert b"LEGO" in h and b"Trek" not in h
h = anon.get("/?near=baton").data; assert b"Trek" in h and b"iPhone" not in h
h = anon.get("/?sort=price_asc").data; assert h.index(b"LEGO") < h.index(b"armchair") < h.index(b"Trek") < h.index(b"iPhone")
h = anon.get("/?sort=price_desc").data; assert h.index(b"iPhone") < h.index(b"LEGO")
assert b"Filters (on)" in anon.get("/?cond=New").data and b"Filters (on)" not in anon.get("/").data
assert b"view=list" in anon.get("/?cond=New").data   # switching views keeps the filters

# message the seller before buying; unread count and inbox
h = bob.get("/listing/1").data; assert b"Is this still available?" in h and b"Send seller a message" in h
r = post(bob, "/listing/1/message", body="Is this still available?"); cid = int(loc(r).rsplit("/", 1)[1])
assert b"Open your conversation" in bob.get("/listing/1").data
assert b'<span class="dot">1</span>' in alice.get("/").data          # alice sees an unread badge
h = alice.get("/inbox").data; assert b"bob" in h and b"Is this still available?" in h and b"Selling" in h
h = alice.get(f"/inbox/{cid}").data; assert b"Is this still available?" in h and b"Buy with Safe Transfer" not in h
assert b'class="dot"' not in alice.get("/").data                       # read now
post(alice, f"/inbox/{cid}", body="Yes! You can text me at 337-555-0142 or pay by Zelle")
h = bob.get(f"/inbox/{cid}").data
assert b"aren't protected" in h and b"Buy with Safe Transfer" in h     # warning shown to the reader
assert b"aren't protected" not in alice.get(f"/inbox/{cid}").data      # not to the sender
r = post(bob, "/listing/1/message", body="Still there?"); assert int(loc(r).rsplit("/", 1)[1]) == cid   # same thread
assert client("admin").get(f"/inbox/{cid}").status_code in (302, 404)
assert post(alice, "/listing/1/message", body="hi").status_code == 404 # can't message yourself
assert b"1 person messaged you" in alice.get("/listing/1").data

# saved items
post(bob, "/listing/2/save"); assert b"Mid-century armchair" in bob.get("/saved").data
assert b">Saved</button>" in bob.get("/listing/2").data
assert post(bob, "/listing/3/save").status_code == 404   # own listing
post(bob, "/listing/2/save"); assert b"Mid-century armchair" not in bob.get("/saved").data

# edit listing; price drops show the old price crossed out
assert bob.get("/listing/1/edit").status_code == 404
assert b'value="420.00"' in alice.get("/listing/1/edit").data
post(alice, "/listing/1/edit", title="iPhone 14, 128GB", price="380", category="Electronics", condition="Used - Good", location="Lafayette, LA", body="x")
h = anon.get("/listing/1").data; assert b"$380.00" in h and b'<s class="was">$420.00</s>' in h and b"Used - Good" in h
post(alice, "/listing/1/edit", title="iPhone 14, 128GB", price="350", category="Electronics", condition="Used - Good", location="Lafayette, LA", body="x")
assert b'<s class="was">$420.00</s>' in anon.get("/").data                # biggest drop kept
post(alice, "/listing/1/edit", title="iPhone 14, 128GB", price="450", category="Electronics", condition="Used - Good", location="Lafayette, LA", body="x")
assert b'class="was"' not in anon.get("/listing/1").data                 # raised above: no strike-through
assert b"Enter a price" in post(alice, "/listing/1/edit", title="t", price="abc").data

# ratings after a completed deal show on the seller's profile and listing
oid = int(loc(post(bob, "/listing/1/buy")).rsplit("/", 1)[1])
assert post(bob, f"/order/{oid}/rate", stars="5").status_code == 403    # not complete yet
post(alice, f"/order/{oid}/approve"); post(bob, f"/order/{oid}/approve")
assert b"Leave rating" in bob.get(f"/order/{oid}").data
post(bob, f"/order/{oid}/rate", stars="5", comment="Fast and friendly")
assert b"already rated" in bob.get(loc(post(bob, f"/order/{oid}/rate", stars="1"))).data
assert post(bob, f"/order/{oid}/rate", stars="9").status_code == 400
post(alice, f"/order/{oid}/rate", stars="4")
h = anon.get("/u/alice").data
assert b"5.0 from 1 rating" in h and b"Fast and friendly" in h and b"1 completed sale" in h and b"Mid-century armchair" in h
assert b"5.0 (1)" in anon.get("/listing/2").data and b"1 sold" in anon.get("/listing/2").data
assert b"4.0 from 1 rating" in anon.get("/u/bob").data
assert anon.get("/u/nobody").status_code == 404
print("ALL MARKETPLACE FEATURE TESTS PASSED")
