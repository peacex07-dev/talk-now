"""Ranking tests: early-stars-only answer sinks below engaged answer over time; refresh dedupe."""
import os, tempfile, sqlite3
from datetime import datetime, timedelta
os.environ["TALKNOW_DB"] = tempfile.mktemp(suffix=".db"); os.environ.pop("OPENAI_API_KEY", None)
import app as A
c = sqlite3.connect(os.environ["TALKNOW_DB"]); c.row_factory = sqlite3.Row
t0 = datetime(2026, 10, 13, 8, 0); iso = lambda t: t.isoformat(timespec="seconds")
for h in "author a b u1 u2 u3 u4".split(): c.execute("INSERT INTO users(handle,created) VALUES(?,?)", (h, iso(t0)))
U = {r["handle"]: r["id"] for r in c.execute("SELECT id,handle FROM users")}
c.execute("INSERT INTO posts(user_id,severity,category,title,body,created) VALUES(?,?,?,?,?,?)", (U["author"],"mid","soul","Q","?",iso(t0)))
pid = c.execute("SELECT max(id) FROM posts").fetchone()[0]
def reply(u, t, parent=None):
    c.execute("INSERT INTO replies(post_id,parent_id,user_id,body,created) VALUES(?,?,?,?,?)", (pid,parent,U[u],"x",iso(t))); c.commit()
    return c.execute("SELECT max(id) FROM replies").fetchone()[0]
fast = reply("a", t0 + timedelta(minutes=1))      # quick pasted answer
deep = reply("b", t0 + timedelta(minutes=30))     # thoughtful answer
for u in ("u1","u2","u3"): A.rate(c, U[u], fast, 5, t0 + timedelta(minutes=5))
A.rate(c, U["u4"], deep, 4, t0 + timedelta(hours=1))
A.rate(c, U["u4"], deep, 3, t0 + timedelta(hours=1, minutes=5))  # changeable: still one rating
assert c.execute("SELECT COUNT(*) FROM ratings WHERE reply_id=?", (deep,)).fetchone()[0] == 1
early = t0 + timedelta(hours=2)
assert A.rank_score(c, fast, early) > A.rank_score(c, deep, early), "early: fast answer should lead"
# later engagement on deep answer
for u in ("u1","u2"): A.record_view(c, U[u], pid, "v1", t0 + timedelta(hours=3))
for u in ("u1","u2"): assert A.record_view(c, U[u], pid, "v1", t0 + timedelta(hours=30)) == 2  # revisit both answers
c.execute("INSERT INTO events(user_id,reply_id,kind,created) VALUES(?,?,'reply',?)", (U["u1"], deep, iso(t0+timedelta(hours=31))))
A.rate(c, U["u2"], deep, 5, t0 + timedelta(hours=40))   # later rating
c.commit()
late = t0 + timedelta(hours=72)
fs, ds = A.rank_score(c, fast, late), A.rank_score(c, deep, late)
print(f"t+2h fast={A.rank_score(c, fast, early)} deep={A.rank_score(c, deep, early)} | t+72h fast={fs} deep={ds}")
assert ds > fs, "late: engaged answer should outrank early-stars-only answer"
# refresh dedupe: repeated views within gap, same visit -> no events
before = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
for m in range(0, 25, 5): A.record_view(c, U["u3"], pid, "v9", late + timedelta(minutes=m))
assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before, "refresh should not log events"
assert A.record_view(c, U["u3"], pid, "v10", late + timedelta(minutes=26)) == 2  # new browser session counts
# HTTP: rating route + sorting renders
cl = A.app.test_client(); cl.get("/post/%d" % pid); cl.get("/post/%d" % pid)
assert cl.post(f"/rate/{deep}", data={"stars": "4"}).status_code == 302
assert b"rank" in cl.get(f"/post/{pid}").data and b"988" not in cl.get(f"/post/{pid}").data
print("ALL RANKING TESTS PASSED")
