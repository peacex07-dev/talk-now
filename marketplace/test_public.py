"""Public-site mode: no demo accounts or sample data, admin password required, test-money banner shown."""
import os, tempfile, subprocess, sys
env = dict(os.environ, SAFETRADE_DB=tempfile.mktemp(suffix=".db"), SAFETRADE_UPLOADS=tempfile.mkdtemp(), SAFETRADE_DEMO_DATA="0")
env.pop("SAFETRADE_ADMIN_PW", None)
r = subprocess.run([sys.executable, "-c", "import app"], env=env, capture_output=True, text=True)
assert r.returncode != 0 and "SAFETRADE_ADMIN_PW" in r.stderr

os.environ.update(env, SAFETRADE_ADMIN_PW="s3cret-admin-pw")
import app as A
conn = A.connect()
assert [r[0] for r in conn.execute("SELECT username FROM users")] == ["admin"]
assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
assert conn.execute("SELECT COUNT(*) FROM help_posts").fetchone()[0] == 0
c = A.app.test_client(); h = c.get("/login").data
assert b"alice" not in h and b"pretend" in h and b"pretend" in c.get("/").data
with c.session_transaction() as s: t = s["csrf"]
assert c.post("/login", data={"csrf": t, "username": "admin", "password": "admin1234"}).status_code == 200   # default pw refused
assert c.post("/login", data={"csrf": t, "username": "admin", "password": "s3cret-admin-pw"}).status_code == 302
print("ALL PUBLIC-MODE TESTS PASSED")
