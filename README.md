# Talk Now — web prototype
Anonymous, mobile-friendly support forum. Flask + SQLite, plain HTML/CSS.

## Run
    pip install flask requests
    cd /workspace/talk-now/app
    python3 app.py            # http://localhost:5050
    OPENAI_API_KEY=sk-... python3 app.py   # optional: use OpenAI moderation instead of local word list
Tests: `python3 test_strikes.py && python3 test_ranking.py && python3 test_local.py && python3 test_quick.py`. Reset data: delete `talknow.db` (re-seeds sample posts).

## Features
- Random anonymous handle (e.g. KindRiver412) stored in a session cookie; no real identity.
- Home: choose **Personal**, **Local Insider** or **Quick Advice**.
- Quick Advice: one feed of short questions (newest first), New Question (question + optional short description, Enter creates); replies, stars, rank, revisits, moderation and strikes as elsewhere. 2 sample questions seeded.
- Personal: pick soul / spirit / body, then Light / Medium / Heavy -> lane of posts + New Post button.
- Local Insider: enter an area (case/whitespace normalized, e.g. "  laFAYette " -> Lafayette) or tap an existing area.
  Area page: threads (sort Newest / Most replies / Recently active), New Thread (title + description, Enter creates),
  and search across thread titles, descriptions and replies in that area. Replies, stars, rank score, revisit tracking,
  anonymity, moderation and strikes work the same as Personal. Sample Lafayette threads are seeded.
- New post: title, description underneath, Enter creates the post. Respect reminder on forms.
- Post page: threaded replies (reply to post or to any reply).
- Heavy screens: 988 Suicide & Crisis Lifeline footer (tel:988, 988lifeline.org).
- Moderation `moderate(text)`: OpenAI omni-moderation if OPENAI_API_KEY set (self-harm disclosures not punished), else local insult/profanity list.
- Strike ladder (flagged content is blocked): warning, "don't do it again", 3-day ban; after a ban: warning, then ban doubling (6, 12, 24...). Stored in `users` (cycle_strikes, last_ban_days, ban_until). Banned users see the end time and can only read.
- Sample posts tagged "sample".

## Answer ratings & rank score
- Each anonymous user can give any reply (not their own) 1-5 stars; one rating per user per reply, changeable (latest counts).
- Events table logs `reply` (someone replied to an answer) and `revisit` (user reopened a post >= REVISIT_GAP_MIN=30 min
  after last view, or in a new browser session via `tn_visit` cookie; refreshes inside the gap are ignored).
- Replies are sorted by rank score (shown subtly as "rank N").

Formula (constants at top of the Ranking section in app.py), with s = (stars-3)/2 in [-1, 1]:

    decay = 1 / (1 + age_hours / EARLY_DECAY_H)
    score = W_EARLY_RATING   * sum(s of ratings given < LATE_AFTER_H after the answer) * decay
          + W_LATE_RATING    * sum(s of ratings given >= LATE_AFTER_H after the answer)
          + W_REVISIT        * revisit_events
          + W_FOLLOWUP_REPLY * reply_events

Defaults: W_EARLY_RATING=1.0, W_LATE_RATING=3.0, W_REVISIT=1.5, W_FOLLOWUP_REPLY=2.0, LATE_AFTER_H=24, EARLY_DECAY_H=24.
Early stars fade as the thread ages; lasting engagement keeps an answer on top.
