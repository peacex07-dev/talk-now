"""Talk Now - anonymous support forum prototype (Flask + SQLite)."""
import os, re, sqlite3, secrets, random
from datetime import datetime, timedelta
from flask import Flask, g, request, session, redirect, url_for, render_template_string, abort

DB = os.environ.get("TALKNOW_DB", os.path.join(os.path.dirname(__file__), "talknow.db"))
SEVERITIES = ["heavy", "mid", "light"]
CATEGORIES = ["soul", "spirit", "body"]
LABEL = {"light": "Light", "mid": "Medium", "heavy": "Heavy"}
def norm_area(a):
    a = " ".join(re.sub(r"[^\w\s\-\.',]", "", a or "").split())[:60]
    return a.title()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)

# ---------- DB ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, handle TEXT UNIQUE, total_strikes INTEGER DEFAULT 0,
  cycle_strikes INTEGER DEFAULT 0, last_ban_days INTEGER DEFAULT 0, ban_until TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, user_id INTEGER, severity TEXT, category TEXT,
  title TEXT, body TEXT, sample INTEGER DEFAULT 0, created TEXT, area TEXT);
CREATE TABLE IF NOT EXISTS replies(id INTEGER PRIMARY KEY, post_id INTEGER, parent_id INTEGER, user_id INTEGER,
  body TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, reply_id INTEGER, stars INTEGER, rated_at TEXT,
  PRIMARY KEY(user_id, reply_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, user_id INTEGER, reply_id INTEGER, kind TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS views(user_id INTEGER, post_id INTEGER, first_view TEXT, last_view TEXT, visit_id TEXT,
  PRIMARY KEY(user_id, post_id));
"""
def init_db(conn):
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO users(handle,created) VALUES('TalkNow-Sample',?)", (now,))
        uid = conn.execute("SELECT id FROM users WHERE handle='TalkNow-Sample'").fetchone()[0]
        samples = [("mid","spirit","Not sure which faith fits me","I grew up without religion and I'm curious. How did you find your path?"),
                   ("light","body","Trying to walk more","Any tips for building a daily walking habit?"),
                   ("heavy","soul","Really rough week","Lost my job and feeling low. Just need to talk."),
                   ("mid","soul","Marriage feels distant","We barely talk lately. How do you reconnect?")]
        for s,c,t,b in samples:
            conn.execute("INSERT INTO posts(user_id,severity,category,title,body,sample,created) VALUES(?,?,?,?,?,1,?)",(uid,s,c,t,b,now))
        for t,b in [("Best breakfast in Lafayette?","Looking for a good local spot for boudin and coffee."),
                    ("Areas to avoid at night downtown?","New to town, want to stay safe walking around.")]:
            conn.execute("INSERT INTO posts(user_id,severity,category,title,body,sample,created,area) VALUES(?,'','',?,?,1,?,'Lafayette')",(uid,t,b,now))
        conn.execute("INSERT INTO replies(post_id,parent_id,user_id,body,created) VALUES(5,NULL,?,?,?)",
                     (uid,"Try a local bakery early, the boudin goes fast.",now))
        conn.execute("INSERT INTO replies(post_id,parent_id,user_id,body,created) VALUES(3,NULL,?,?,?)",
                     (uid,"Sorry you're going through this. You're not alone here.",now))
    conn.commit()

# ---------- Moderation ----------
BAD_WORDS = ["fuck","shit","bitch","bastard","asshole","cunt","dick","idiot","stupid","loser","moron",
             "retard","kill yourself","kys","worthless","ugly","shut up","hate you","dumb","pathetic","slut","whore"]
def moderate(text):
    """Return (flagged: bool, reason: str). OpenAI if OPENAI_API_KEY set, else local list."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        try:
            import requests
            r = requests.post("https://api.openai.com/v1/moderations",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "omni-moderation-latest", "input": text}, timeout=10)
            res = r.json()["results"][0]
            if res["flagged"]:
                cats = [k for k, v in res["categories"].items() if v]
                # Don't punish people for disclosing self-harm feelings; only for harming others.
                if cats and all(c.startswith("self-harm") and "instructions" not in c for c in cats):
                    return False, ""
                return True, ", ".join(cats)
            return False, ""
        except Exception as e:
            app.logger.warning("OpenAI moderation failed, using local fallback: %s", e)
    low = text.lower()
    for w in BAD_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True, f"disrespectful language ('{w}')"
    return False, ""

# ---------- Strike ladder ----------
def apply_strike(conn, user_id, now=None):
    """Record an offense. Returns dict(action='warn1'|'warn2'|'ban', days, ban_until)."""
    now = now or datetime.now()
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    cycle = u["cycle_strikes"] + 1
    last = u["last_ban_days"]
    threshold = 3 if last == 0 else 2   # first cycle: warn, warn, ban; later: warn, ban
    if cycle >= threshold:
        days = 3 if last == 0 else last * 2
        until = (now + timedelta(days=days)).isoformat(timespec="seconds")
        conn.execute("UPDATE users SET total_strikes=total_strikes+1, cycle_strikes=0, last_ban_days=?, ban_until=? WHERE id=?",
                     (days, until, user_id))
        conn.commit(); return {"action": "ban", "days": days, "ban_until": until}
    conn.execute("UPDATE users SET total_strikes=total_strikes+1, cycle_strikes=? WHERE id=?", (cycle, user_id))
    conn.commit()
    return {"action": "warn2" if (last == 0 and cycle == 2) else "warn1", "days": 0, "ban_until": None}

def ban_until(u, now=None):
    if u["ban_until"] and datetime.fromisoformat(u["ban_until"]) > (now or datetime.now()):
        return u["ban_until"]
    return None

# ---------- Ranking ----------
W_EARLY_RATING = 1.0       # weight of a star rating given soon after the answer was posted
W_LATE_RATING = 3.0        # weight of a star rating given >= LATE_AFTER_H after the answer was posted
W_REVISIT = 1.5            # someone came back to the answer later
W_FOLLOWUP_REPLY = 2.0     # someone replied to the answer
LATE_AFTER_H = 24          # hours after posting when a rating counts as "later"
EARLY_DECAY_H = 24         # early-rating influence halves after this many hours of thread age
REVISIT_GAP_MIN = 30       # min gap between views (same visit) for a revisit to count

def _ts(x): return datetime.fromisoformat(x)

def rank_score(conn, reply_id, now=None):
    """score = W_EARLY*sum(early s)*decay + W_LATE*sum(late s) + W_REVISIT*revisits + W_FOLLOWUP*followup_replies
    where s = (stars-3)/2 in [-1,1], decay = 1/(1+age_h/EARLY_DECAY_H)."""
    now = now or datetime.now()
    r = conn.execute("SELECT created FROM replies WHERE id=?", (reply_id,)).fetchone()
    posted = _ts(r[0]); age_h = max(0, (now - posted).total_seconds() / 3600)
    early = late = 0.0
    for stars, at in conn.execute("SELECT stars, rated_at FROM ratings WHERE reply_id=?", (reply_id,)):
        if _ts(at) > now: continue
        v = (stars - 3) / 2
        if (_ts(at) - posted).total_seconds() / 3600 >= LATE_AFTER_H: late += v
        else: early += v
    ev = dict(conn.execute("SELECT kind, COUNT(*) FROM events WHERE reply_id=? AND created<=? GROUP BY kind",
                           (reply_id, now.isoformat(timespec="seconds"))).fetchall())
    decay = 1 / (1 + age_h / EARLY_DECAY_H)
    return round(W_EARLY_RATING * early * decay + W_LATE_RATING * late
                 + W_REVISIT * ev.get("revisit", 0) + W_FOLLOWUP_REPLY * ev.get("reply", 0), 2)

def rate(conn, user_id, reply_id, stars, now=None):
    now = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute("INSERT INTO ratings VALUES(?,?,?,?) ON CONFLICT(user_id,reply_id) DO UPDATE SET stars=excluded.stars, rated_at=excluded.rated_at",
                 (user_id, reply_id, stars, now)); conn.commit()

def record_view(conn, user_id, post_id, visit_id, now=None):
    """Log 'revisit' events for answers (by others) the user saw before, if gap>=REVISIT_GAP_MIN or new visit.
    Refreshes within the gap in the same visit log nothing. Returns number of events logged."""
    now = now or datetime.now(); n = now.isoformat(timespec="seconds")
    v = conn.execute("SELECT * FROM views WHERE user_id=? AND post_id=?", (user_id, post_id)).fetchone()
    if not v:
        conn.execute("INSERT INTO views VALUES(?,?,?,?,?)", (user_id, post_id, n, n, visit_id)); conn.commit(); return 0
    logged = 0
    if (now - _ts(v[3])).total_seconds() >= REVISIT_GAP_MIN * 60 or v[4] != visit_id:
        for (rid,) in conn.execute("SELECT id FROM replies WHERE post_id=? AND user_id!=? AND created<=?",
                                   (post_id, user_id, v[3])).fetchall():
            conn.execute("INSERT INTO events(user_id,reply_id,kind,created) VALUES(?,?,'revisit',?)", (user_id, rid, n)); logged += 1
    conn.execute("UPDATE views SET last_view=?, visit_id=? WHERE user_id=? AND post_id=?", (n, visit_id, user_id, post_id))
    conn.commit(); return logged

# ---------- Anonymous identity ----------
ADJ = ["Calm","Brave","Kind","Quiet","Bright","Gentle","Steady","Hopeful","Warm","Patient"]
NOUN = ["River","Oak","Sparrow","Harbor","Lantern","Meadow","Stone","Willow","Comet","Ember"]
def current_user():
    conn = db(); uid = session.get("uid")
    if uid:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u: return u
    while True:
        h = f"{random.choice(ADJ)}{random.choice(NOUN)}{random.randint(100,999)}"
        try:
            cur = conn.execute("INSERT INTO users(handle,created) VALUES(?,?)", (h, datetime.now().isoformat(timespec="seconds")))
            conn.commit(); break
        except sqlite3.IntegrityError: continue
    session["uid"] = cur.lastrowid; session.permanent = True
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

def fmt(ts): return datetime.fromisoformat(ts).strftime("%b %d, %Y %I:%M %p")

MESSAGES = {"warn1": "Warning: your message was blocked because it wasn't respectful ({r}). Please keep Talk Now kind.",
            "warn2": "Don't do it again. Your message was blocked ({r}). Next time you'll be suspended.",
            "ban": "Your message was blocked ({r}). You've been suspended for {d} days, until {u}."}

def handle_text(text):
    """Moderate; return None if ok, else a notice string (strike applied)."""
    flagged, reason = moderate(text)
    if not flagged: return None
    s = apply_strike(db(), current_user()["id"])
    return MESSAGES[s["action"]].format(r=reason, d=s["days"], u=fmt(s["ban_until"]) if s["ban_until"] else "")

# ---------- Templates ----------
BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Talk Now</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f1ec;color:#222}
header{background:#3b5b7a;color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}
header a{color:#fff;text-decoration:none;font-weight:700;font-size:1.2em}.me{font-size:.8em;opacity:.85}
main{max-width:640px;margin:auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:10px 0 20px}
.btn{display:block;text-align:center;padding:18px 8px;border-radius:12px;background:#fff;border:2px solid #3b5b7a;color:#3b5b7a;text-decoration:none;font-weight:700;text-transform:capitalize;cursor:pointer;font-size:1em}
.btn.sel,.btn.primary{background:#3b5b7a;color:#fff}.heavy{border-color:#a33;color:#a33}.heavy.sel{background:#a33;color:#fff}
.card{background:#fff;border-radius:12px;padding:14px;margin:10px 0;box-shadow:0 1px 3px #0001}
.card a{color:inherit;text-decoration:none}.meta{font-size:.8em;color:#777}.tag{background:#ffe9a8;padding:1px 6px;border-radius:6px;font-size:.75em}
input,textarea{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #bbb;font-size:1em;margin:6px 0}
textarea{min-height:110px}.notice{background:#fde2e2;border:1px solid #a33;padding:12px;border-radius:10px;margin:10px 0}
.respect{background:#e6f0e6;padding:10px;border-radius:10px;font-size:.9em}.reply{border-left:3px solid #cdd;padding-left:10px;margin:8px 0 8px 6px}
.crisis{margin-top:30px;background:#fff3f3;border:2px solid #a33;padding:14px;border-radius:12px;text-align:center}
.crisis a{color:#a33;font-weight:700}.stars button{background:none;border:0;font-size:1.3em;color:#ccc;cursor:pointer;padding:0 2px}.stars .on{color:#e6a700}
.score{color:#aaa;font-size:.9em}details summary{cursor:pointer;color:#3b5b7a;font-size:.85em}
</style></head><body><header><a href="/">Talk Now</a><span class="me">You are {{ me.handle }}</span></header><main>
{% if notice %}<div class="notice">{{ notice }}</div>{% endif %}
{% if banned %}<div class="notice"><b>You're suspended</b> until {{ banned }}. You can still read, but can't post or reply.</div>{% endif %}
{{ body|safe }}
{% if severity == 'heavy' %}<div class="crisis">If you're in crisis or thinking about suicide, help is available 24/7.<br>
<a href="tel:988">Call or text 988</a> &middot; <a href="https://988lifeline.org" target="_blank">988lifeline.org</a><br>988 Suicide &amp; Crisis Lifeline</div>{% endif %}
</main></body></html>"""
RESPECT = '<p class="respect">🤝 Gotta be respectful. Messages are checked by AI. Disrespect: warning, then "don\'t do it again", then a suspension.</p>'

def page(body, severity=None, notice=None):
    me = current_user(); b = ban_until(me)
    return render_template_string(BASE, body=body, me=me, severity=severity, notice=notice or session.pop("notice", None),
                                  banned=fmt(b) if b else None)

def esc(s):
    from markupsafe import escape; return str(escape(s))

# ---------- Routes ----------
@app.route("/")
def home():
    h = ("<h2>Where do you want to talk?</h2>"
         "<a class='card' style='display:block' href='/personal'><b>Personal</b><p class='meta'>Anonymous support for soul, spirit and body, from light to heavy.</p></a>"
         "<a class='card' style='display:block' href='/local'><b>Local Insider</b><p class='meta'>People here are willing to help and give advice about your area, like a good restaurant or a place to avoid.</p></a>"
         "<p class='meta'>You're anonymous. Nobody knows who you are.</p>")
    return page(h)

@app.route("/personal")
def personal():
    cat = request.args.get("category")
    h = "<h2>What's it about?</h2><div class='grid'>" + "".join(
        f"<a class='btn {'sel' if c==cat else ''}' href='/personal?category={c}'>{c}</a>" for c in CATEGORIES) + "</div>"
    if cat in CATEGORIES:
        h += "<h2>How heavy is it?</h2><div class='grid'>" + "".join(
            f"<a class='btn primary {s}' href='/lane/{s}/{cat}'>{LABEL[s]}</a>" for s in ["light", "mid", "heavy"]) + "</div>"
    h += "<p><a href='/'>&larr; Home</a></p>"
    return page(h)

@app.route("/local")
def local():
    if request.args.get("area") is not None:
        a = norm_area(request.args["area"])
        if a: return redirect(url_for("area", name=a))
    areas = [r[0] for r in db().execute("SELECT area, COUNT(*) n FROM posts WHERE area IS NOT NULL AND area!='' GROUP BY area ORDER BY n DESC LIMIT 30")]
    h = ("<h2>Local Insider</h2><p class='meta'>People here are willing to help and give advice about your area, like a good restaurant or a place to avoid.</p>"
         "<form method='get'><input name='area' placeholder='Enter your area, e.g. Lafayette' required autofocus>"
         "<button class='btn primary' type='submit'>Go</button></form>")
    if areas: h += "<h3>Areas</h3>" + "".join(f"<a class='btn' style='display:inline-block;margin:4px;padding:8px 14px' href='{url_for('area', name=a)}'>{esc(a)}</a>" for a in areas)
    return page(h + "<p><a href='/'>&larr; Home</a></p>")

SORTS = {"new": ("Newest", "p.id DESC"), "replies": ("Most replies", "n DESC, p.id DESC"),
         "active": ("Recently active", "COALESCE((SELECT MAX(created) FROM replies r WHERE r.post_id=p.id), p.created) DESC")}

@app.route("/local/<name>")
def area(name):
    a = norm_area(name)
    if a != name: return redirect(url_for("area", name=a, **request.args))
    q = request.args.get("q", "").strip(); sort = request.args.get("sort", "new")
    if sort not in SORTS: sort = "new"
    sql = """SELECT p.*,u.handle,(SELECT COUNT(*) FROM replies r WHERE r.post_id=p.id) n FROM posts p
             JOIN users u ON u.id=p.user_id WHERE p.area=?"""
    args = [a]
    if q:
        like = f"%{q}%"
        sql += " AND (p.title LIKE ? OR p.body LIKE ? OR EXISTS(SELECT 1 FROM replies r WHERE r.post_id=p.id AND r.body LIKE ?))"
        args += [like, like, like]
    rows = db().execute(sql + " ORDER BY " + SORTS[sort][1], args).fetchall()
    u = url_for("area", name=a)
    h = (f"<h2>Local Insider › {esc(a)}</h2><a class='btn primary' href='{u}/new'>+ New Thread</a>"
         f"<form method='get' style='margin-top:10px'><input name='q' value='{esc(q)}' placeholder='Search threads and replies in {esc(a)}'>"
         f"<input type='hidden' name='sort' value='{sort}'><button class='btn' type='submit'>Search</button></form><p class='meta'>Sort: "
         + " · ".join(f"<b>{lbl}</b>" if k == sort else f"<a href='{u}?sort={k}&q={esc(q)}'>{lbl}</a>" for k, (lbl, _) in SORTS.items()) + "</p>")
    for p in rows:
        h += (f"<div class='card'><a href='/post/{p['id']}'><b>{esc(p['title'])}</b> {'<span class=tag>sample</span>' if p['sample'] else ''}"
              f"<p>{esc(p['body'][:140])}</p><div class='meta'>{esc(p['handle'])} · {fmt(p['created'])} · {p['n']} replies</div></a></div>")
    if not rows: h += f"<p class='meta'>{'No threads match your search.' if q else 'No threads yet. Start one.'}</p>"
    return page(h + "<p><a href='/local'>&larr; Areas</a></p>")

@app.route("/local/<name>/new", methods=["GET", "POST"])
def new_thread(name):
    a = norm_area(name)
    if not a: abort(404)
    me = current_user(); notice = None
    title = request.form.get("title", "").strip(); body = request.form.get("body", "").strip()
    if request.method == "POST":
        if ban_until(me): notice = "You're suspended and can't post right now."
        elif not title: notice = "Please add a title."
        else:
            notice = handle_text(title + "\n" + body)
            if not notice:
                cur = db().execute("INSERT INTO posts(user_id,severity,category,title,body,created,area) VALUES(?,'','',?,?,?,?)",
                                   (me["id"], title, body, datetime.now().isoformat(timespec="seconds"), a))
                db().commit(); return redirect(f"/post/{cur.lastrowid}")
            title = body = ""
    h = (f"<h2>New thread · {esc(a)}</h2>{RESPECT}<form method='post'>"
         f"<input name='title' placeholder='Title, e.g. Best coffee near downtown?' value='{esc(title)}' required autofocus>"
         f"<textarea name='body' placeholder='Description (optional)'>{esc(body)}</textarea>"
         f"<button class='btn primary' type='submit'>Enter</button></form><p><a href='{url_for('area', name=a)}'>&larr; Back</a></p>")
    return page(h, notice=notice)

def check_lane(s, c):
    if s not in SEVERITIES or c not in CATEGORIES: abort(404)

@app.route("/lane/<s>/<c>")
def lane(s, c):
    check_lane(s, c)
    rows = db().execute("""SELECT p.*,u.handle,(SELECT COUNT(*) FROM replies r WHERE r.post_id=p.id) n FROM posts p
        JOIN users u ON u.id=p.user_id WHERE severity=? AND category=? ORDER BY p.id DESC""", (s, c)).fetchall()
    h = f"<h2 style='text-transform:capitalize'>{c} &rsaquo; {LABEL[s]}</h2><a class='btn primary' href='/lane/{s}/{c}/new'>+ New Post</a>"
    for p in rows:
        h += (f"<div class='card'><a href='/post/{p['id']}'><b>{esc(p['title'])}</b> {'<span class=tag>sample</span>' if p['sample'] else ''}"
              f"<p>{esc(p['body'][:140])}</p><div class='meta'>{esc(p['handle'])} · {fmt(p['created'])} · {p['n']} replies</div></a></div>")
    if not rows: h += "<p class='meta'>No posts yet. Be the first.</p>"
    h += "<p><a href='/personal?category={c}'>&larr; Back</a></p>"
    return page(h, severity=s)

@app.route("/lane/<s>/<c>/new", methods=["GET", "POST"])
def new_post(s, c):
    check_lane(s, c); me = current_user(); notice = None
    title = request.form.get("title", "").strip(); body = request.form.get("body", "").strip()
    if request.method == "POST":
        if ban_until(me): notice = "You're suspended and can't post right now."
        elif not title: notice = "Please add a title."
        else:
            notice = handle_text(title + "\n" + body)
            if not notice:
                cur = db().execute("INSERT INTO posts(user_id,severity,category,title,body,created) VALUES(?,?,?,?,?,?)",
                                   (me["id"], s, c, title, body, datetime.now().isoformat(timespec="seconds")))
                db().commit(); return redirect(f"/post/{cur.lastrowid}")
            title = body = ""
    h = (f"<h2 style='text-transform:capitalize'>New post · {c} › {LABEL[s]}</h2>{RESPECT}<form method='post'>"
         f"<input name='title' placeholder='What is on your mind?' value='{esc(title)}' required autofocus>"
         f"<textarea name='body' placeholder='Description (optional)'>{esc(body)}</textarea>"
         f"<button class='btn primary' type='submit'>Enter</button></form><p><a href='/lane/{s}/{c}'>&larr; Back</a></p>")
    return page(h, severity=s, notice=notice)

@app.route("/post/<int:pid>", methods=["GET", "POST"])
def post(pid):
    conn = db(); me = current_user()
    p = conn.execute("SELECT p.*,u.handle FROM posts p JOIN users u ON u.id=p.user_id WHERE p.id=?", (pid,)).fetchone()
    if not p: abort(404)
    if request.method == "POST":
        text = request.form.get("body", "").strip(); parent = request.form.get("parent_id") or None
        if ban_until(me): session["notice"] = "You're suspended and can't reply right now."
        elif text:
            n = handle_text(text)
            if n: session["notice"] = n
            else:
                n_ = datetime.now().isoformat(timespec="seconds")
                conn.execute("INSERT INTO replies(post_id,parent_id,user_id,body,created) VALUES(?,?,?,?,?)",
                             (pid, parent, me["id"], text, n_))
                if parent:
                    pr = conn.execute("SELECT user_id FROM replies WHERE id=?", (parent,)).fetchone()
                    if pr and pr[0] != me["id"]:
                        conn.execute("INSERT INTO events(user_id,reply_id,kind,created) VALUES(?,?,'reply',?)", (me["id"], parent, n_))
                conn.commit()
        return redirect(f"/post/{pid}")
    record_view(conn, me["id"], pid, request.cookies.get("tn_visit") or g.get("new_visit", ""))
    reps = conn.execute("SELECT r.*,u.handle FROM replies r JOIN users u ON u.id=r.user_id WHERE post_id=? ORDER BY r.id", (pid,)).fetchall()
    kids = {}
    mine = dict(conn.execute("SELECT reply_id, stars FROM ratings WHERE user_id=?", (me["id"],)).fetchall())
    score = {r["id"]: rank_score(conn, r["id"]) for r in reps}
    for r in sorted(reps, key=lambda r: -score[r["id"]]): kids.setdefault(r["parent_id"], []).append(r)
    def stars(r):
        if r["user_id"] == me["id"]: return ""
        cur = mine.get(r["id"], 0)
        return (f"<form method='post' action='/rate/{r['id']}' class='stars'>" + "".join(
            f"<button name='stars' value='{i}' class='{'on' if i<=cur else ''}' title='{i} star'>★</button>" for i in range(1,6)) + "</form>")
    def form(parent=""):
        return (f"<form method='post'><input type='hidden' name='parent_id' value='{parent}'>"
                f"<textarea name='body' placeholder='Write a kind reply...' required style='min-height:60px'></textarea>"
                f"<button class='btn primary' type='submit'>Reply</button></form>")
    def tree(parent):
        out = ""
        for r in kids.get(parent, []):
            out += (f"<div class='reply'><div class='meta'>{esc(r['handle'])} · {fmt(r['created'])} <span class='score'>· rank {score[r['id']]}</span></div><div>{esc(r['body'])}</div>{stars(r)}"
                    f"<details><summary>Reply</summary>{form(r['id'])}</details>{tree(r['id'])}</div>")
        return out
    back = (f"<a href='{url_for('area', name=p['area'])}'>&larr; Local Insider › {esc(p['area'])}</a>" if p["area"]
            else f"<a href='/lane/{p['severity']}/{p['category']}'>&larr; {p['category']} › {LABEL.get(p['severity'], '')}</a>")
    h = (f"<p>{back}</p>"
         f"<div class='card'><h2>{esc(p['title'])} {'<span class=tag>sample</span>' if p['sample'] else ''}</h2>"
         f"<p>{esc(p['body'])}</p><div class='meta'>{esc(p['handle'])} · {fmt(p['created'])}</div></div>"
         f"<h3>Replies</h3>{tree(None) or '<p class=meta>No replies yet.</p>'}<h3>Add a reply</h3>{RESPECT}{form()}")
    return page(h, severity=p["severity"])

@app.route("/rate/<int:rid>", methods=["POST"])
def rate_reply(rid):
    conn = db(); me = current_user()
    r = conn.execute("SELECT post_id, user_id FROM replies WHERE id=?", (rid,)).fetchone()
    if not r: abort(404)
    try: st = int(request.form.get("stars", 0))
    except ValueError: st = 0
    if 1 <= st <= 5 and r[1] != me["id"]: rate(conn, me["id"], rid, st)
    return redirect(f"/post/{r[0]}")

@app.before_request
def visit_cookie():
    if not request.cookies.get("tn_visit"): g.new_visit = secrets.token_hex(8)

@app.after_request
def set_visit(resp):
    if g.get("new_visit"): resp.set_cookie("tn_visit", g.new_visit, httponly=True, samesite="Lax")  # browser-session cookie
    return resp

with app.app_context():
    c = sqlite3.connect(DB); init_db(c); c.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
