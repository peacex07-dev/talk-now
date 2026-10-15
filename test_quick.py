"""Quick Advice tests: home link, feed, posting, moderation/strikes, replies, rating, rank."""
import os, tempfile, re
os.environ["TALKNOW_DB"] = tempfile.mktemp(suffix=".db"); os.environ.pop("OPENAI_API_KEY", None)
import app as A
c = A.app.test_client()
h = c.get("/").data; assert b"Quick Advice" in h and b"get fast answers from others" in h and b"Local Insider" in h and b"Personal" in h
h = c.get("/quick").data; assert b"fall asleep faster" in h and b"sample" in h and b"New Question" in h and b"988" not in h
assert b"fall asleep" not in c.get("/lane/mid/spirit").data and b"fall asleep" not in c.get("/local/Lafayette").data
r = c.post("/quick/new", data={"title": "Best way to remove a coffee stain?", "body": ""}); assert r.status_code == 302
pid = int(r.headers["Location"].rsplit("/", 1)[1])
h = c.get("/quick").data; assert h.index(b"coffee stain") < h.index(b"fall asleep")
assert b"Quick Advice" in c.get(f"/post/{pid}").data
c2 = A.app.test_client(); c2.post(f"/post/{pid}", data={"body": "Cold water and dish soap"})
h = c.get(f"/post/{pid}").data; assert b"Cold water" in h and b"rank" in h
rid = int(re.search(rb"/rate/(\d+)", h).group(1)); c.post(f"/rate/{rid}", data={"stars": "4"})
assert b"class='on'" in c.get(f"/post/{pid}").data
assert b"Warning" in c.post("/quick/new", data={"title": "you idiot"}).data
assert b"again" in c.post("/quick/new", data={"title": "dumb question loser"}).data
assert b"suspended for 3 days" in c.post("/quick/new", data={"title": "stupid"}).data
assert b"suspended" in c.post("/quick/new", data={"title": "Is it raining?"}).data
print("ALL QUICK ADVICE TESTS PASSED")
