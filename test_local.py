"""Local Insider tests: area entry/normalize, quick picks, posting, search, sort, moderation, replies/rating/ranking."""
import os, tempfile, re
os.environ["TALKNOW_DB"] = tempfile.mktemp(suffix=".db"); os.environ.pop("OPENAI_API_KEY", None)
import app as A
c = A.app.test_client()
assert A.norm_area("  laFAYette   ") == "Lafayette" and A.norm_area("new   iberia") == "New Iberia"
r = c.get("/local?area=  laFAYette  "); assert r.status_code == 302 and r.headers["Location"].endswith("/local/Lafayette")
assert c.get("/local/lafayette").status_code == 302
h = c.get("/local").data; assert b"/local/Lafayette" in h and b"like a good restaurant" in h  # quick pick from seed
h = c.get("/local/Lafayette").data; assert b"Best breakfast" in h and b"sample" in h and b"New Thread" in h and b"988" not in h
r = c.post("/local/Lafayette/new", data={"title": "Good mechanic?", "body": "Honest shop near Johnston St"}); assert r.status_code == 302
pid = int(r.headers["Location"].rsplit("/", 1)[1])
h = c.get("/local/Lafayette").data; assert h.index(b"Good mechanic") < h.index(b"Best breakfast")   # newest first
assert b"Most replies" in h and c.get("/local/Lafayette?sort=replies").status_code == 200 and c.get("/local/Lafayette?sort=active").status_code == 200
assert b"Good mechanic" in c.get("/local/Lafayette?q=johnston").data and b"Best breakfast" not in c.get("/local/Lafayette?q=johnston").data
assert b"Best breakfast" in c.get("/local/Lafayette?q=bakery").data   # matches reply text
assert b"No threads match" in c.get("/local/Lafayette?q=zzzz").data
assert b"Good mechanic" not in c.get("/local/Baton%20Rouge?q=mechanic").data  # scoped to area
# moderation + strikes in Local Insider
r = c.post("/local/Lafayette/new", data={"title": "you idiot", "body": ""}); assert b"Warning" in r.data
r = c.post(f"/post/{pid}", data={"body": "shut up loser"}, follow_redirects=True); assert b"Don&#39;t do it again" in r.data or b"Don't do it again" in r.data
r = c.post("/local/Lafayette/new", data={"title": "stupid town", "body": ""}); assert b"suspended for 3 days" in r.data
r = c.post("/local/Lafayette/new", data={"title": "Nice park?", "body": ""}); assert b"suspended" in r.data
# replies, rating, rank in Local Insider (second anon user)
c2 = A.app.test_client()
c2.post(f"/post/{pid}", data={"body": "Try the shop on Pinhook"})
h = c2.get(f"/post/{pid}").data; assert b"Try the shop on Pinhook" in h and b"rank" in h and b"Local Insider" in h
rid = int(re.search(rb"/rate/(\d+)", c.get(f"/post/{pid}").data).group(1))
assert c.post(f"/rate/{rid}", data={"stars": "5"}).status_code == 302
assert b"class='on'" in c.get(f"/post/{pid}").data
print("ALL LOCAL INSIDER TESTS PASSED")
