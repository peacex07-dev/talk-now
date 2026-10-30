"""Neighbors section: help / borrow / free posts, replies, done/reopen/delete, owner-only controls."""
import os, tempfile, re
os.environ["SAFETRADE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["SAFETRADE_UPLOADS"] = tempfile.mkdtemp()
import app as A

def client(user=None, pw="demo1234"):
    c = A.app.test_client(); c.get("/login")
    if user: post(c, "/login", username=user, password=pw)
    return c
def tok(c):
    with c.session_transaction() as s: return s["csrf"]
def post(c, url, **data): return c.post(url, data={"csrf": tok(c), **data})

anon = client()
h = anon.get("/neighbors").data
assert b"pressure washer" in h and b"mason jars" in h and b"Neighbors" in anon.get("/").data
h = anon.get("/neighbors?kind=free").data; assert b"mason jars" in h and b"pressure washer" not in h
h = anon.get("/neighbors?q=couch").data; assert b"couch" in h and b"mason" not in h
assert b"Log in to reply" in anon.get("/neighbors/1").data
assert anon.get("/neighbors/new").status_code == 302

alice, bob = client("alice"), client("bob")
assert b"Pick what kind" in post(bob, "/neighbors/new", kind="sell", title="x").data
r = post(bob, "/neighbors/new", kind="borrow", title="<i>Need a ladder</i>", location="Lafayette", body="Gutters")
pid = int(r.headers["Location"].rsplit("/", 1)[1])
h = alice.get(f"/neighbors/{pid}").data; assert b"&lt;i&gt;Need a ladder" in h and b"Got it, thanks" not in h
post(alice, f"/neighbors/{pid}/reply", body="I have a 12ft one, swing by")
h = bob.get(f"/neighbors/{pid}").data; assert b"12ft" in h and b"Got it, thanks" in h
assert post(alice, f"/neighbors/{pid}/status", status="done").status_code == 403   # not owner
post(bob, f"/neighbors/{pid}/status", status="done"); assert b"Done" in anon.get(f"/neighbors/{pid}").data
h = anon.get("/neighbors").data; assert h.index(b"Need a ladder") > h.index(b"mason jars")   # done sinks
post(bob, f"/neighbors/{pid}/status", status="open"); assert b"Got it, thanks" in bob.get(f"/neighbors/{pid}").data
assert post(bob, f"/neighbors/{pid}/status", status="sold").status_code == 403
post(bob, f"/neighbors/{pid}/status", status="removed")
assert anon.get(f"/neighbors/{pid}").status_code == 404 and b"Need a ladder" not in anon.get("/neighbors").data
assert post(alice, f"/neighbors/{pid}/reply", body="hi").status_code == 404
r = post(alice, "/neighbors/new", kind="free", title="Free couch")
assert b"picked up" in alice.get(r.headers["Location"]).data
print("ALL NEIGHBORS TESTS PASSED")
