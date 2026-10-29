"""SafeTrade - marketplace with escrow (Flask + SQLite).

Money flow: buyer pays -> funds held in escrow -> seller confirms handover AND buyer confirms receipt
-> funds released to seller. Either side can cancel/dispute under the rules in ORDER RULES below.
All money is stored as integer cents. The ledger table is the source of truth for balances.
"""
import os, re, sqlite3, secrets
from contextlib import contextmanager
from datetime import datetime
from flask import Flask, g, request, session, redirect, url_for, render_template_string, abort, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SAFETRADE_DB", os.path.join(HERE, "safetrade.db"))
UPLOADS = os.environ.get("SAFETRADE_UPLOADS", os.path.join(HERE, "uploads"))
CATEGORIES = ["Electronics", "Furniture", "Vehicles", "Clothing", "Home & Garden", "Sports", "Toys", "Other"]
ICON = {"Electronics": "📱", "Furniture": "🛋️", "Vehicles": "🚗", "Clothing": "👕", "Home & Garden": "🪴",
        "Sports": "🚲", "Toys": "🧸", "Other": "📦"}
IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_PRICE_CENTS = 100_000_00   # $100,000 cap per listing / deposit
FEE_PCT = float(os.environ.get("SAFETRADE_FEE_PCT", "0"))   # platform fee taken from seller payout, e.g. 2.5

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

def now(): return datetime.now().isoformat(timespec="seconds")

# ---------- DB ----------
def connect():
    conn = sqlite3.connect(DB, isolation_level=None)   # autocommit; money moves use tx() explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def db():
    if "db" not in g: g.db = connect()
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

@contextmanager
def tx(conn):
    """BEGIN IMMEDIATE takes the write lock up front so two buyers can't both pay for one item."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK"); raise

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, body TEXT, price INTEGER NOT NULL, category TEXT, location TEXT, image TEXT,
  status TEXT DEFAULT 'active', created TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
  buyer_id INTEGER NOT NULL REFERENCES users(id), seller_id INTEGER NOT NULL REFERENCES users(id),
  amount INTEGER NOT NULL, fee INTEGER DEFAULT 0, status TEXT NOT NULL, buyer_ok INTEGER DEFAULT 0,
  seller_ok INTEGER DEFAULT 0, dispute_by INTEGER, dispute_reason TEXT, created TEXT, closed TEXT);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), order_id INTEGER,
  kind TEXT NOT NULL, amount INTEGER NOT NULL, created TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, order_id INTEGER, user_id INTEGER, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, order_id INTEGER, user_id INTEGER, body TEXT, created TEXT);
"""

def init_db(conn):
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    with tx(conn):
        ids = {}
        for name, pw, adm in [("admin", os.environ.get("SAFETRADE_ADMIN_PW", "admin1234"), 1),
                              ("alice", "demo1234", 0), ("bob", "demo1234", 0)]:
            ids[name] = conn.execute("INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,?,?)",
                                     (name, generate_password_hash(pw), adm, now())).lastrowid
        for name in ("alice", "bob"):
            conn.execute("INSERT INTO ledger(user_id,kind,amount,created) VALUES(?,'deposit',50000,?)", (ids[name], now()))
        for who, t, b, p, c, loc in [
            ("alice", "iPhone 14, 128GB", "Great condition, always in a case. Charger included.", 42000, "Electronics", "Lafayette, LA"),
            ("alice", "Mid-century armchair", "Teal fabric, solid wood legs. Pickup only.", 15000, "Furniture", "Lafayette, LA"),
            ("bob", "Trek road bike", "54cm frame, new tires last spring.", 38000, "Sports", "Baton Rouge, LA"),
            ("bob", "LEGO Millennium Falcon", "Complete with box and manual.", 9500, "Toys", "Baton Rouge, LA")]:
            conn.execute("INSERT INTO listings(seller_id,title,body,price,category,location,created) VALUES(?,?,?,?,?,?,?)",
                         (ids[who], t, b, p, c, loc, now()))

# ---------- Money ----------
def parse_money(s):
    """'12.5' / '$1,200.00' -> cents, or None if invalid / out of range."""
    s = (s or "").strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", s): return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return cents if 0 < cents <= MAX_PRICE_CENTS else None

def money(c): return f"${c/100:,.2f}"
app.jinja_env.filters["money"] = money

def balance(conn, uid):
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0]

def escrow_held(conn, uid=None):
    q = "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('funded','disputed')"
    if uid is None: return conn.execute(q).fetchone()[0]
    return conn.execute(q + " AND buyer_id=?", (uid,)).fetchone()[0]

def log(conn, oid, uid, text):
    conn.execute("INSERT INTO events(order_id,user_id,text,created) VALUES(?,?,?,?)", (oid, uid, text, now()))

class Refused(Exception): pass

# ---------- ORDER RULES ----------
# funded   : money held. Seller approves (handed over), buyer approves (received as described).
#            Both approved -> released to seller (minus FEE_PCT).
#            Buyer may cancel only before either side approves; seller may cancel any time (refunds buyer).
#            Either side may open a dispute -> 'disputed' (money frozen until an admin decides).
# disputed : admin releases to seller or refunds buyer.
# released / refunded : final.

def buy(conn, listing_id, buyer_id):
    with tx(conn):
        l = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not l: raise Refused("Listing not found.")
        if l["seller_id"] == buyer_id: raise Refused("You can't buy your own listing.")
        if l["status"] != "active": raise Refused("This item is no longer available.")
        if balance(conn, buyer_id) < l["price"]: raise Refused("Not enough funds in your wallet. Add funds first.")
        fee = round(l["price"] * FEE_PCT / 100)
        oid = conn.execute("""INSERT INTO orders(listing_id,buyer_id,seller_id,amount,fee,status,created)
            VALUES(?,?,?,?,?,'funded',?)""", (l["id"], buyer_id, l["seller_id"], l["price"], fee, now())).lastrowid
        conn.execute("INSERT INTO ledger(user_id,order_id,kind,amount,created) VALUES(?,?,'escrow_hold',?,?)",
                     (buyer_id, oid, -l["price"], now()))
        conn.execute("UPDATE listings SET status='pending' WHERE id=?", (l["id"],))
        log(conn, oid, buyer_id, f"Paid {money(l['price'])} into escrow")
        return oid

def _order(conn, oid, uid):
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or uid not in (o["buyer_id"], o["seller_id"]): raise Refused("Order not found.")
    return o

def _release(conn, o, by):
    payout = o["amount"] - o["fee"]
    conn.execute("UPDATE orders SET status='released', closed=? WHERE id=?", (now(), o["id"]))
    conn.execute("INSERT INTO ledger(user_id,order_id,kind,amount,created) VALUES(?,?,'escrow_release',?,?)",
                 (o["seller_id"], o["id"], payout, now()))
    conn.execute("UPDATE listings SET status='sold' WHERE id=?", (o["listing_id"],))
    log(conn, o["id"], by, f"Released {money(payout)} to seller" + (f" ({money(o['fee'])} fee)" if o["fee"] else ""))

def _refund(conn, o, by, why):
    conn.execute("UPDATE orders SET status='refunded', closed=? WHERE id=?", (now(), o["id"]))
    conn.execute("INSERT INTO ledger(user_id,order_id,kind,amount,created) VALUES(?,?,'escrow_refund',?,?)",
                 (o["buyer_id"], o["id"], o["amount"], now()))
    conn.execute("UPDATE listings SET status='active' WHERE id=?", (o["listing_id"],))
    log(conn, o["id"], by, f"{why}. Refunded {money(o['amount'])} to buyer")

def approve(conn, oid, uid):
    """Record this party's approval; release funds once both have approved. Returns the new status."""
    with tx(conn):
        o = _order(conn, oid, uid)
        if o["status"] != "funded": raise Refused("This order can't be approved now.")
        col = "buyer_ok" if uid == o["buyer_id"] else "seller_ok"
        if o[col]: raise Refused("You already approved.")
        conn.execute(f"UPDATE orders SET {col}=1 WHERE id=?", (oid,))
        log(conn, oid, uid, "Buyer confirmed item received as described" if col == "buyer_ok"
            else "Seller confirmed item handed over")
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if o["buyer_ok"] and o["seller_ok"]:
            _release(conn, o, uid)
            return "released"
        return "funded"

def cancel(conn, oid, uid):
    with tx(conn):
        o = _order(conn, oid, uid)
        if o["status"] != "funded": raise Refused("This order can't be cancelled now.")
        if uid == o["buyer_id"] and o["buyer_ok"]:
            raise Refused("You already confirmed you received the item. Open a dispute if something is wrong.")
        if uid == o["buyer_id"] and o["seller_ok"]:
            raise Refused("The seller already confirmed handover. Open a dispute if something is wrong.")
        _refund(conn, o, uid, "Cancelled by " + ("buyer" if uid == o["buyer_id"] else "seller"))

def dispute(conn, oid, uid, reason):
    reason = (reason or "").strip()[:1000]
    if not reason: raise Refused("Tell us what went wrong.")
    with tx(conn):
        o = _order(conn, oid, uid)
        if o["status"] != "funded": raise Refused("This order can't be disputed now.")
        conn.execute("UPDATE orders SET status='disputed', dispute_by=?, dispute_reason=? WHERE id=?", (uid, reason, oid))
        log(conn, oid, uid, "Opened a dispute. Funds frozen until SafeTrade reviews")

def resolve(conn, oid, admin_id, outcome):
    with tx(conn):
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "disputed": raise Refused("Not a disputed order.")
        if outcome == "release": _release(conn, o, admin_id)
        elif outcome == "refund": _refund(conn, o, admin_id, "Dispute resolved for buyer")
        else: raise Refused("Unknown outcome.")

def deposit(conn, uid, cents):
    with tx(conn):
        conn.execute("INSERT INTO ledger(user_id,kind,amount,created) VALUES(?,'deposit',?,?)", (uid, cents, now()))

def withdraw(conn, uid, cents):
    with tx(conn):
        if balance(conn, uid) < cents: raise Refused("You can't withdraw more than your available balance.")
        conn.execute("INSERT INTO ledger(user_id,kind,amount,created) VALUES(?,'withdraw',?,?)", (uid, -cents, now()))

# ---------- Auth & CSRF ----------
def current_user():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

def login_required():
    u = current_user()
    if not u: abort(redirect(url_for("login", next=request.path)))
    return u

def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_hex(16)
    return session["csrf"]
app.jinja_env.globals["csrf"] = csrf_token

@app.before_request
def check_csrf():
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
        abort(400, "Form expired. Go back, refresh and try again.")

def start_session(uid):
    """New session on login (no fixation), with a fresh CSRF token."""
    session.clear(); session["uid"] = uid; session["csrf"] = secrets.token_hex(16)

def flash(msg, kind="ok"): session["notice"] = (kind, msg)

def safe_next(n): return n if n and n.startswith("/") and not n.startswith("//") else url_for("browse")

# ---------- Templates ----------
BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · SafeTrade</title><style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1c1e21;--muted:#65676b;--line:#dadde1;--brand:#0a7c55;--brand-ink:#fff;--warn:#b54708;--bad:#b42318;--soft:#e7f6ef}
@media (prefers-color-scheme:dark){:root{--bg:#18191a;--card:#242526;--ink:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c;--soft:#173528}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--brand)}header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
nav{max-width:1100px;margin:auto;display:flex;gap:14px;align-items:center;padding:10px 16px;flex-wrap:wrap}
nav .logo{font-weight:800;font-size:20px;text-decoration:none;color:var(--brand);margin-right:auto}nav a{text-decoration:none;font-weight:600}
main{max-width:1100px;margin:auto;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.tile{text-decoration:none;color:inherit;padding:0;overflow:hidden;display:block}
.thumb{aspect-ratio:1;background:var(--soft);display:flex;align-items:center;justify-content:center;font-size:64px;overflow:hidden}.thumb img{width:100%;height:100%;object-fit:cover}
.tile .info{padding:10px 12px}.price{font-weight:800;font-size:18px}.meta{color:var(--muted);font-size:14px}
.btn{display:inline-block;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:9px 14px;border-radius:8px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.btn.primary{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}.btn.danger{color:var(--bad)}
input,textarea,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--card);color:var(--ink);margin:4px 0 12px}
label{font-weight:600;font-size:14px}form.inline{display:inline}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.notice{padding:10px 14px;border-radius:8px;margin-bottom:14px;background:var(--soft)}.notice.err{background:#fde8e7;color:var(--bad)}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--soft);color:var(--brand)}
.badge.disputed,.badge.pending{background:#fff3e0;color:var(--warn)}.badge.refunded{background:#eee;color:#555}
.shield{background:var(--soft);border-radius:10px;padding:12px 14px;margin:12px 0}
.steps{list-style:none;padding:0;margin:0}.steps li{padding:8px 0 8px 32px;position:relative;border-left:2px solid var(--line);margin-left:10px}
.steps li:before{content:"";position:absolute;left:-9px;top:10px;width:16px;height:16px;border-radius:50%;background:var(--card);border:2px solid var(--line)}
.steps li.done:before{background:var(--brand);border-color:var(--brand)}.steps li.done{border-color:var(--brand)}
.two{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}@media(max-width:760px){.two{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}
.msg{padding:8px 10px;border-radius:8px;background:var(--bg);margin:6px 0}
</style></head><body><header><nav><a class="logo" href="/">🛡️ SafeTrade</a>
{% if me %}<a href="/">Browse</a><a href="/sell">Sell</a><a href="/orders">Orders</a><a href="/wallet">Wallet {{ bal|money }}</a>
{% if me.is_admin %}<a href="/admin">Admin</a>{% endif %}
<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf() }}"><button class="btn">Log out</button></form>
{% else %}<a href="/login">Log in</a><a class="btn primary" href="/signup">Sign up</a>{% endif %}
</nav></header><main>{% if notice %}<div class="notice {{ 'err' if notice[0]=='err' }}">{{ notice[1] }}</div>{% endif %}{{ body|safe }}</main></body></html>"""

def page(title, tpl, **ctx):
    me = current_user()
    body = render_template_string(tpl, me=me, **ctx)
    return render_template_string(BASE, title=title, body=body, me=me, bal=balance(db(), me["id"]) if me else 0,
                                  notice=session.pop("notice", None))

STATUS_LABEL = {"funded": "In escrow", "disputed": "Disputed", "released": "Completed", "refunded": "Refunded"}
app.jinja_env.globals.update(ICON=ICON, STATUS_LABEL=STATUS_LABEL)

# ---------- Routes: auth ----------
AUTH = """<div class="card" style="max-width:420px;margin:30px auto"><h2>{{ heading }}</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf() }}">
<label>Username</label><input name="username" required autofocus value="{{ username or '' }}">
<label>Password</label><input name="password" type="password" required minlength="{{ 8 if signup else 1 }}">
<button class="btn primary">{{ heading }}</button></form>
<p class="meta">{% if signup %}Have an account? <a href="/login">Log in</a>{% else %}New here? <a href="/signup">Sign up</a>
<br>Demo accounts: alice / demo1234, bob / demo1234{% endif %}</p></div>"""

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower(); p = request.form.get("password", "")
        if not re.fullmatch(r"[a-z0-9_]{3,20}", u):
            flash("Username: 3-20 letters, numbers or _.", "err")
        elif len(p) < 8:
            flash("Password must be at least 8 characters.", "err")
        else:
            try:
                uid = db().execute("INSERT INTO users(username,pw,created) VALUES(?,?,?)",
                                   (u, generate_password_hash(p), now())).lastrowid
                start_session(uid)
                flash("Welcome to SafeTrade! Add funds to your wallet to start buying.")
                return redirect(url_for("browse"))
            except sqlite3.IntegrityError:
                flash("That username is taken.", "err")
        return page("Sign up", AUTH, heading="Sign up", signup=True, username=u)
    return page("Sign up", AUTH, heading="Sign up", signup=True)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        row = db().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["pw"], request.form.get("password", "")):
            start_session(row["id"])
            return redirect(safe_next(request.args.get("next")))
        flash("Wrong username or password.", "err")
        return page("Log in", AUTH, heading="Log in", signup=False, username=u)
    return page("Log in", AUTH, heading="Log in", signup=False)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("browse"))

# ---------- Routes: listings ----------
BROWSE = """<form class="row" method="get" style="margin-bottom:14px">
<input name="q" placeholder="Search Marketplace" value="{{ q }}" style="flex:2;min-width:180px;margin:0">
<select name="cat" style="flex:1;min-width:140px;margin:0"><option value="">All categories</option>
{% for c in cats %}<option {{ 'selected' if c==cat }}>{{ c }}</option>{% endfor %}</select><button class="btn">Search</button></form>
<div class="shield">🛡️ <b>Every purchase is protected.</b> Your payment sits in escrow. The seller only gets paid after
<b>both</b> of you approve: the seller confirms handover and you confirm you got what was described.</div>
{% if not rows %}<p class="meta">No listings match.</p>{% endif %}
<div class="grid">{% for l in rows %}<a class="card tile" href="/listing/{{ l.id }}"><div class="thumb">
{% if l.image %}<img src="/uploads/{{ l.image }}" alt="">{% else %}{{ ICON.get(l.category, '📦') }}{% endif %}</div>
<div class="info"><div class="price">{{ l.price|money }}</div><div>{{ l.title }}</div><div class="meta">{{ l.location }}</div></div></a>{% endfor %}</div>"""

@app.route("/")
def browse():
    q = request.args.get("q", "").strip(); cat = request.args.get("cat", "")
    sql = "SELECT * FROM listings WHERE status='active'"; args = []
    if q:
        sql += " AND (title LIKE ? OR body LIKE ? OR location LIKE ?)"; args += [f"%{q}%"] * 3
    if cat in CATEGORIES:
        sql += " AND category=?"; args.append(cat)
    rows = db().execute(sql + " ORDER BY id DESC", args).fetchall()
    return page("Marketplace", BROWSE, rows=rows, q=q, cat=cat, cats=CATEGORIES)

LISTING = """<div class="two"><div class="card" style="padding:0;overflow:hidden"><div class="thumb" style="aspect-ratio:4/3;font-size:120px">
{% if l.image %}<img src="/uploads/{{ l.image }}" alt="">{% else %}{{ ICON.get(l.category, '📦') }}{% endif %}</div></div>
<div class="card"><h1 style="margin:0 0 4px">{{ l.title }}</h1><div class="price" style="font-size:24px">{{ l.price|money }}</div>
<p class="meta">{{ l.category }} · {{ l.location }} · Listed {{ l.created[:10] }} by <b>{{ seller }}</b>
{% if l.status != 'active' %} · <span class="badge {{ l.status }}">{{ l.status }}</span>{% endif %}</p>
<p style="white-space:pre-wrap">{{ l.body }}</p>
{% if not me %}<a class="btn primary" href="/login?next=/listing/{{ l.id }}">Log in to buy</a>
{% elif me.id == l.seller_id %}<p class="meta">This is your listing.</p>
{% if l.status == 'active' %}<form method="post" action="/listing/{{ l.id }}/remove"><input type="hidden" name="csrf" value="{{ csrf() }}">
<button class="btn danger">Remove listing</button></form>{% endif %}
{% elif l.status == 'active' %}<form method="post" action="/listing/{{ l.id }}/buy"><input type="hidden" name="csrf" value="{{ csrf() }}">
<button class="btn primary" style="width:100%;padding:12px">🛡️ Buy with Safe Transfer · {{ l.price|money }}</button></form>
<p class="meta">Your wallet: {{ bal|money }}{% if bal < l.price %} · <a href="/wallet">add funds</a>{% endif %}</p>
{% else %}<p class="meta">This item is no longer available.</p>{% endif %}
<div class="shield"><b>How Safe Transfer works</b><ol style="margin:6px 0 0;padding-left:20px">
<li>You pay. The money is held by SafeTrade, not the seller.</li><li>Meet up or get it shipped.</li>
<li>Seller confirms handover, you confirm it's as described.</li><li>Only when <b>both</b> approve is the seller paid.</li></ol>
<p class="meta" style="margin:6px 0 0">Problem? Open a dispute and the money stays frozen until we review it.</p></div></div></div>"""

@app.route("/listing/<int:lid>")
def listing(lid):
    l = db().execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    if not l or l["status"] == "removed": abort(404)
    seller = db().execute("SELECT username FROM users WHERE id=?", (l["seller_id"],)).fetchone()[0]
    me = current_user()
    return page(l["title"], LISTING, l=l, seller=seller, bal=balance(db(), me["id"]) if me else 0)

@app.route("/listing/<int:lid>/buy", methods=["POST"])
def buy_route(lid):
    me = login_required()
    try:
        oid = buy(db(), lid, me["id"])
    except Refused as e:
        flash(str(e), "err"); return redirect(url_for("listing", lid=lid))
    flash("Payment is held safely in escrow. Arrange the handover with the seller below.")
    return redirect(url_for("order", oid=oid))

@app.route("/listing/<int:lid>/remove", methods=["POST"])
def remove_listing(lid):
    me = login_required()
    n = db().execute("UPDATE listings SET status='removed' WHERE id=? AND seller_id=? AND status='active'",
                     (lid, me["id"])).rowcount
    flash("Listing removed." if n else "Can't remove this listing.", "ok" if n else "err")
    return redirect(url_for("orders") if n else url_for("listing", lid=lid))

SELL = """<div class="card" style="max-width:620px;margin:auto"><h2>Sell an item</h2>
<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{ csrf() }}">
<label>Title</label><input name="title" required maxlength="100" value="{{ f.title }}">
<label>Price (USD)</label><input name="price" required inputmode="decimal" placeholder="0.00" value="{{ f.price }}">
<label>Category</label><select name="category">{% for c in cats %}<option {{ 'selected' if c==f.category }}>{{ c }}</option>{% endfor %}</select>
<label>Location</label><input name="location" maxlength="60" placeholder="City, State" value="{{ f.location }}">
<label>Description</label><textarea name="body" rows="5" maxlength="3000">{{ f.body }}</textarea>
<label>Photo (optional, max 5 MB)</label><input type="file" name="image" accept="image/*">
<button class="btn primary">Publish listing</button></form>
<p class="meta">Buyers pay into escrow. You're paid when you confirm the handover and the buyer confirms receipt.</p></div>"""

@app.route("/sell", methods=["GET", "POST"])
def sell():
    me = login_required()
    f = {k: request.form.get(k, "").strip() for k in ("title", "price", "category", "location", "body")}
    if request.method == "POST":
        price = parse_money(f["price"])
        if not f["title"]: flash("Add a title.", "err")
        elif price is None: flash("Enter a price between $0.01 and $100,000.", "err")
        else:
            img = save_image(request.files.get("image"))
            if img is False: flash("Photo must be JPG, PNG, WEBP or GIF.", "err")
            else:
                lid = db().execute("""INSERT INTO listings(seller_id,title,body,price,category,location,image,created)
                    VALUES(?,?,?,?,?,?,?,?)""", (me["id"], f["title"][:100], f["body"][:3000], price,
                    f["category"] if f["category"] in CATEGORIES else "Other", f["location"][:60], img, now())).lastrowid
                flash("Your listing is live.")
                return redirect(url_for("listing", lid=lid))
    return page("Sell", SELL, f=f, cats=CATEGORIES)

def save_image(file):
    """Returns stored filename, None if no file, False if invalid."""
    if not file or not file.filename: return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    head = file.stream.read(12); file.stream.seek(0)
    magic = head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG") or head[:4] == b"GIF8" or head[8:12] == b"WEBP"
    if ext not in IMG_EXT or not magic: return False
    os.makedirs(UPLOADS, exist_ok=True)
    name = secrets.token_hex(12) + "." + ext
    file.save(os.path.join(UPLOADS, name))
    return name

@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOADS, name)

# ---------- Routes: orders ----------
ORDERS = """<h2>Buying</h2>{{ table(buying, 'Seller') }}<h2>Selling</h2>{{ table(selling, 'Buyer') }}
<h2>My listings</h2>{% if not mine %}<p class="meta">Nothing listed yet. <a href="/sell">Sell something</a>.</p>{% endif %}
<div class="grid">{% for l in mine %}<a class="card tile" href="/listing/{{ l.id }}"><div class="info">
<div class="price">{{ l.price|money }}</div><div>{{ l.title }}</div><span class="badge {{ l.status }}">{{ l.status }}</span></div></a>{% endfor %}</div>"""
ORDER_TABLE = """{% if not rows %}<p class="meta">None yet.</p>{% else %}<div class="card" style="padding:4px 8px"><table>
<tr><th>Item</th><th>{{ who }}</th><th>Amount</th><th>Status</th></tr>
{% for o in rows %}<tr><td><a href="/order/{{ o.id }}">{{ o.title }}</a></td><td>{{ o.other }}</td><td>{{ o.amount|money }}</td>
<td><span class="badge {{ o.status }}">{{ STATUS_LABEL[o.status] }}</span>{% if o.todo %} <b style="color:var(--warn)">· your action</b>{% endif %}</td></tr>{% endfor %}
</table></div>{% endif %}"""

@app.route("/orders")
def orders():
    me = login_required(); conn = db()
    def rows(side, other):
        mine_ok = "buyer_ok" if side == "buyer_id" else "seller_ok"
        return conn.execute(f"""SELECT o.*, l.title, u.username other, (o.status='funded' AND o.{mine_ok}=0) todo
            FROM orders o JOIN listings l ON l.id=o.listing_id JOIN users u ON u.id=o.{other}
            WHERE o.{side}=? ORDER BY o.id DESC""", (me["id"],)).fetchall()
    def table(r, who): return render_template_string(ORDER_TABLE, rows=r, who=who)
    mine = conn.execute("SELECT * FROM listings WHERE seller_id=? AND status!='removed' ORDER BY id DESC", (me["id"],)).fetchall()
    return page("Orders", ORDERS, buying=rows("buyer_id", "seller_id"), selling=rows("seller_id", "buyer_id"),
                mine=mine, table=table)

ORDER = """<p><a href="/orders">&larr; Orders</a></p><div class="two"><div>
<div class="card"><div class="row"><h2 style="margin:0;flex:1">{{ o.title }}</h2><span class="badge {{ o.status }}">{{ STATUS_LABEL[o.status] }}</span></div>
<p class="meta">Order #{{ o.id }} · {{ o.amount|money }} · Buyer <b>{{ o.buyer }}</b> · Seller <b>{{ o.seller }}</b></p>
<ul class="steps"><li class="done">Buyer paid {{ o.amount|money }} into escrow</li>
<li class="{{ 'done' if o.seller_ok }}">Seller confirms the item was handed over / shipped</li>
<li class="{{ 'done' if o.buyer_ok }}">Buyer confirms the item was received as described</li>
<li class="{{ 'done' if o.status=='released' }}">Funds released to seller{% if o.fee %} ({{ (o.amount-o.fee)|money }} after {{ o.fee|money }} fee){% endif %}</li></ul>
{% if o.status=='funded' and role!='admin' %}<div class="shield">{% if role=='buyer' %}
{% if not o.buyer_ok %}Only approve once you have the item and it matches the listing. <b>After both approvals the money goes to the seller and can't be pulled back.</b>{% else %}You approved. Waiting for the seller to confirm handover.{% endif %}
{% else %}{% if not o.seller_ok %}The {{ o.amount|money }} is secured. Hand over or ship the item, then confirm.{% else %}You confirmed handover. You'll be paid as soon as the buyer approves.{% endif %}{% endif %}</div>
<div class="row">{% if (role=='buyer' and not o.buyer_ok) or (role=='seller' and not o.seller_ok) %}
<form method="post" action="/order/{{ o.id }}/approve" onsubmit="return confirm('{{ 'Confirm you received the item as described?' if role=='buyer' else 'Confirm you handed over / shipped the item?' }}')"><input type="hidden" name="csrf" value="{{ csrf() }}">
<button class="btn primary">{{ 'I received it, approve' if role=='buyer' else 'I handed it over, approve' }}</button></form>{% endif %}
{% if role=='seller' or not (o.seller_ok or o.buyer_ok) %}<form method="post" action="/order/{{ o.id }}/cancel" onsubmit="return confirm('Cancel and refund the buyer?')"><input type="hidden" name="csrf" value="{{ csrf() }}">
<button class="btn danger">Cancel &amp; refund</button></form>{% endif %}</div>
<details style="margin-top:12px"><summary class="meta">Something wrong? Open a dispute</summary>
<form method="post" action="/order/{{ o.id }}/dispute"><input type="hidden" name="csrf" value="{{ csrf() }}">
<textarea name="reason" rows="3" required placeholder="What happened?"></textarea><button class="btn danger">Open dispute</button></form></details>
{% elif o.status=='disputed' %}<div class="shield">⚠️ Disputed by {{ 'buyer' if o.dispute_by==o.buyer_id else 'seller' }}: "{{ o.dispute_reason }}".
Funds are frozen until SafeTrade reviews the case.</div>{% endif %}</div>
<div class="card" style="margin-top:14px"><h3 style="margin-top:0">History</h3>{% for e in events %}
<div class="meta">{{ e.created.replace('T',' ') }} · <b>{{ e.username }}</b> · {{ e.text }}</div>{% endfor %}</div></div>
<div class="card"><h3 style="margin-top:0">Messages</h3>{% if not msgs %}<p class="meta">Arrange pickup or shipping here.</p>{% endif %}
{% for m in msgs %}<div class="msg"><b>{{ m.username }}</b> <span class="meta">{{ m.created[11:16] }}</span><br>{{ m.body }}</div>{% endfor %}
<form method="post" action="/order/{{ o.id }}/message"><input type="hidden" name="csrf" value="{{ csrf() }}">
<textarea name="body" rows="2" required maxlength="1000" placeholder="Write a message"></textarea><button class="btn">Send</button></form>
<p class="meta">Keep payment inside SafeTrade. Anyone asking you to pay outside the app is not protected.</p></div></div>"""

def load_order(oid, me):
    o = db().execute("""SELECT o.*, l.title, b.username buyer, s.username seller FROM orders o
        JOIN listings l ON l.id=o.listing_id JOIN users b ON b.id=o.buyer_id JOIN users s ON s.id=o.seller_id
        WHERE o.id=?""", (oid,)).fetchone()
    if not o or (me["id"] not in (o["buyer_id"], o["seller_id"]) and not me["is_admin"]): abort(404)
    return o

@app.route("/order/<int:oid>")
def order(oid):
    me = login_required(); o = load_order(oid, me)
    role = "buyer" if me["id"] == o["buyer_id"] else "seller" if me["id"] == o["seller_id"] else "admin"
    events = db().execute("SELECT e.*, u.username FROM events e JOIN users u ON u.id=e.user_id WHERE order_id=? ORDER BY e.id", (oid,)).fetchall()
    msgs = db().execute("SELECT m.*, u.username FROM messages m JOIN users u ON u.id=m.user_id WHERE order_id=? ORDER BY m.id", (oid,)).fetchall()
    return page("Order #%d" % oid, ORDER, o=o, role=role, events=events, msgs=msgs)

def order_action(oid, fn, ok_msg):
    me = login_required()
    try:
        res = fn(db(), oid, me["id"])
        flash("Both sides approved. Funds released to the seller." if res == "released" else ok_msg)
    except Refused as e:
        flash(str(e), "err")
    return redirect(url_for("order", oid=oid))

@app.route("/order/<int:oid>/approve", methods=["POST"])
def approve_route(oid): return order_action(oid, approve, "Approval recorded. Waiting for the other side.")

@app.route("/order/<int:oid>/cancel", methods=["POST"])
def cancel_route(oid): return order_action(oid, cancel, "Order cancelled and buyer refunded.")

@app.route("/order/<int:oid>/dispute", methods=["POST"])
def dispute_route(oid):
    return order_action(oid, lambda c, o, u: dispute(c, o, u, request.form.get("reason")), "Dispute opened. Funds are frozen.")

@app.route("/order/<int:oid>/message", methods=["POST"])
def message(oid):
    me = login_required(); o = load_order(oid, me)
    body = request.form.get("body", "").strip()[:1000]
    if body and me["id"] in (o["buyer_id"], o["seller_id"]):
        db().execute("INSERT INTO messages(order_id,user_id,body,created) VALUES(?,?,?,?)", (oid, me["id"], body, now()))
    return redirect(url_for("order", oid=oid))

# ---------- Routes: wallet ----------
WALLET = """<div class="two"><div class="card"><h2 style="margin-top:0">Wallet</h2>
<div class="price" style="font-size:32px">{{ bal|money }}</div><p class="meta">Available · {{ held|money }} of your payments held in escrow</p>
<form method="post" action="/wallet/deposit" class="row"><input type="hidden" name="csrf" value="{{ csrf() }}">
<input name="amount" placeholder="Amount" inputmode="decimal" required style="flex:1;margin:0"><button class="btn primary">Add funds</button></form><br>
<form method="post" action="/wallet/withdraw" class="row"><input type="hidden" name="csrf" value="{{ csrf() }}">
<input name="amount" placeholder="Amount" inputmode="decimal" required style="flex:1;margin:0"><button class="btn">Withdraw to bank</button></form>
<p class="meta">Demo mode: deposits and withdrawals are simulated. No real money moves.</p></div>
<div class="card"><h3 style="margin-top:0">Activity</h3>{% if not rows %}<p class="meta">No activity yet.</p>{% endif %}<table>
{% for r in rows %}<tr><td>{{ r.created[:10] }}</td><td>{{ labels[r.kind] }}{% if r.order_id %} <a href="/order/{{ r.order_id }}">#{{ r.order_id }}</a>{% endif %}</td>
<td style="text-align:right;color:{{ 'var(--brand)' if r.amount>0 else 'inherit' }}">{{ '+' if r.amount>0 }}{{ r.amount|money }}</td></tr>{% endfor %}</table></div></div>"""
LEDGER_LABEL = {"deposit": "Added funds", "withdraw": "Withdrawal", "escrow_hold": "Paid into escrow",
                "escrow_release": "Sale payout", "escrow_refund": "Refund"}

@app.route("/wallet")
def wallet():
    me = login_required(); conn = db()
    rows = conn.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (me["id"],)).fetchall()
    return page("Wallet", WALLET, bal=balance(conn, me["id"]), held=escrow_held(conn, me["id"]), rows=rows, labels=LEDGER_LABEL)

@app.route("/wallet/<op>", methods=["POST"])
def wallet_op(op):
    me = login_required(); cents = parse_money(request.form.get("amount"))
    if cents is None: flash("Enter an amount between $0.01 and $100,000.", "err")
    else:
        try:
            if op == "deposit": deposit(db(), me["id"], cents); flash(f"Added {money(cents)}.")
            elif op == "withdraw": withdraw(db(), me["id"], cents); flash(f"Withdrew {money(cents)}.")
            else: abort(404)
        except Refused as e: flash(str(e), "err")
    return redirect(url_for("wallet"))

# ---------- Routes: admin ----------
ADMIN = """<h2>Disputes</h2><p class="meta">Total held in escrow: {{ held|money }}</p>
{% if not rows %}<p class="meta">No open disputes.</p>{% endif %}
{% for o in rows %}<div class="card" style="margin-bottom:12px"><b><a href="/order/{{ o.id }}">#{{ o.id }} {{ o.title }}</a></b> · {{ o.amount|money }}
<p class="meta">Buyer {{ o.buyer }} · Seller {{ o.seller }} · opened by {{ 'buyer' if o.dispute_by==o.buyer_id else 'seller' }}</p>
<p>"{{ o.dispute_reason }}"</p><div class="row">
<form method="post" action="/admin/resolve/{{ o.id }}"><input type="hidden" name="csrf" value="{{ csrf() }}"><input type="hidden" name="outcome" value="refund">
<button class="btn">Refund buyer</button></form>
<form method="post" action="/admin/resolve/{{ o.id }}"><input type="hidden" name="csrf" value="{{ csrf() }}"><input type="hidden" name="outcome" value="release">
<button class="btn primary">Release to seller</button></form></div></div>{% endfor %}"""

def admin_required():
    me = login_required()
    if not me["is_admin"]: abort(403)
    return me

@app.route("/admin")
def admin():
    admin_required()
    rows = db().execute("""SELECT o.*, l.title, b.username buyer, s.username seller FROM orders o
        JOIN listings l ON l.id=o.listing_id JOIN users b ON b.id=o.buyer_id JOIN users s ON s.id=o.seller_id
        WHERE o.status='disputed' ORDER BY o.id""").fetchall()
    return page("Admin", ADMIN, rows=rows, held=escrow_held(db()))

@app.route("/admin/resolve/<int:oid>", methods=["POST"])
def admin_resolve(oid):
    me = admin_required()
    try:
        resolve(db(), oid, me["id"], request.form.get("outcome")); flash("Dispute resolved.")
    except Refused as e: flash(str(e), "err")
    return redirect(url_for("admin"))

with app.app_context():
    _c = connect(); init_db(_c); _c.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5080)), debug=False)
