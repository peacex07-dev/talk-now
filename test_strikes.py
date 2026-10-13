"""Exercise the strike ladder: warn, warn, 3d, warn, 6d, warn, 12d. Run: python3 test_strikes.py"""
import os, tempfile, sqlite3
from datetime import datetime, timedelta
os.environ["TALKNOW_DB"] = tempfile.mktemp(suffix=".db"); os.environ.pop("OPENAI_API_KEY", None)
import app as A

conn = sqlite3.connect(os.environ["TALKNOW_DB"]); conn.row_factory = sqlite3.Row
conn.execute("INSERT INTO users(handle,created) VALUES('tester','now')"); conn.commit()
uid = conn.execute("SELECT id FROM users WHERE handle='tester'").fetchone()[0]
expected = [("warn1",0),("warn2",0),("ban",3),("warn1",0),("ban",6),("warn1",0),("ban",12),("warn1",0),("ban",24)]
now = datetime(2026, 10, 13, 8, 0)
for i, (act, days) in enumerate(expected, 1):
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert A.ban_until(u, now) is None, f"offense {i}: user should not be banned yet"
    r = A.apply_strike(conn, uid, now)
    assert (r["action"], r["days"]) == (act, days), f"offense {i}: got {r}, want {act},{days}"
    print(f"offense {i}: {act}" + (f" {days} days until {r['ban_until']}" if days else ""))
    if days:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        assert A.ban_until(u, now) and u["last_ban_days"] == days
        now += timedelta(days=days, minutes=1)  # ban expires
assert A.moderate("you are an idiot")[0] and not A.moderate("I'm having a hard day")[0]
# HTTP smoke test: post flow, blocked post, heavy crisis footer
c = A.app.test_client()
assert b"988" in c.get("/lane/heavy/soul").data and b"988" not in c.get("/lane/light/body").data
r = c.post("/lane/mid/spirit/new", data={"title": "Hello", "body": "Looking for guidance"}); assert r.status_code == 302
r = c.post("/lane/mid/spirit/new", data={"title": "you idiot", "body": ""}); assert b"Warning" in r.data
h = c.get("/").data; assert b"Personal" in h and b"Local Insider" in h and b"like a good restaurant" in h
h = c.get("/personal").data; assert b"What's it about" in h and b"How heavy" not in h
assert h.index(b"category=soul") < h.index(b"category=spirit") < h.index(b"category=body")
h = c.get("/personal?category=spirit").data; assert b"Medium" in h; assert b"/lane/light/spirit" in h and b"/lane/heavy/spirit" in h
assert h.index(b"/lane/light/spirit") < h.index(b"/lane/mid/spirit") < h.index(b"/lane/heavy/spirit")
print("ALL TESTS PASSED")
