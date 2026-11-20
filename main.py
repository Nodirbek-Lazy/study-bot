"""Study Reminder Bot.

Designed to run as a short-lived job (GitHub Actions cron). Each run:
  1. fetches new Telegram messages and answers commands (group) and
     Mini App saves (private chat),
  2. sends deadline reminders that have become due,
  3. sends the 07:00 / 13:00 / 18:00 (Asia/Tashkent) messages if not yet sent today,
  4. publishes docs/state.json, which the Mini App reads.
State lives in SQLite (bot.db).
"""
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Tashkent")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
STATE_PATH = os.environ.get("STATE_PATH", "docs/state.json")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8889948277:AAEWT4GtiJL7ORHwsgCtFyRIC4m9E-VKuBw")
CHAT_ID = str(os.environ.get("CHAT_ID", "-1004475639886"))
APP_URL = os.environ.get("APP_URL", "").strip()  # e.g. https://user.github.io/repo/
REPO = os.environ.get("GITHUB_REPOSITORY", "")  # set automatically by GitHub Actions
MAX_OPS = 50

# (label, seconds before deadline) - ordered from earliest to latest
OFFSETS = [("7 days", 7 * 86400), ("3 days", 3 * 86400), ("1 day", 86400), ("1 hour", 3600)]

# (name, hour, minute, title)
SLOTS = [
    ("morning", 7, 0, "Good morning"),
    ("midday", 13, 0, "Midday check-in"),
    ("evening", 18, 0, "Evening review"),
]
MAX_SLOT_LATENESS = timedelta(hours=3)  # skip a slot if we only got to it much later

MOTIVATION = [
    "Small steps every day beat big bursts once a month.",
    "You don't need to feel motivated. You just need to start for 10 minutes.",
    "Discipline is choosing what you want most over what you want right now.",
    "Future you is already grateful for the work you do today.",
    "Progress, not perfection. Open the book.",
    "One focused hour is worth three distracted ones.",
    "Consistency is quiet, but it wins.",
    "Tired is allowed. Quitting is optional. Do a little anyway.",
    "Hard things get easier the more often you show up for them.",
    "Don't wait for the perfect moment. Take the moment and make it perfect.",
    "Every chapter you finish is one less to worry about tomorrow.",
    "You've survived every tough study day so far. Today is no different.",
    "Break it down: the next tiny task is all that matters right now.",
    "Your only competition is who you were yesterday.",
    "Done is better than perfect. Get the first draft done.",
    "Rest when you must, but never forget why you started.",
    "Effort compounds. Today's work is tomorrow's confidence.",
    "Put the phone away for 25 minutes. The world will wait.",
]

HELP = (
    "Study Reminder Bot commands:\n"
    "/app - open the planner (calendar)\n"
    "/add YYYY-MM-DD [HH:MM] Title - add a deadline (e.g. /add 2026-12-05 14:00 Calculus exam)\n"
    "/deadlines - upcoming deadlines\n"
    "/remove ID - remove a deadline\n"
    "/aim text - add a goal for today\n"
    "/today - today's plan\n"
    "/done ID - mark an aim as done\n"
    "/motivate - random motivation\n"
    "/help - this message"
)


# ---------------------------------------------------------------- database
def connect(path=DB_PATH):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS deadlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_ts INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders_sent (
            deadline_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            PRIMARY KEY (deadline_id, label)
        );
        CREATE TABLE IF NOT EXISTS aims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            text TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db, key, value):
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


# ---------------------------------------------------------------- telegram
def app_url():
    """Mini App URL: APP_URL if set, else the repo's standard GitHub Pages address."""
    if APP_URL:
        return APP_URL
    if "/" in REPO:
        owner, name = REPO.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def planner_keyboard():
    # Saving from the Mini App (sendData) only works when it is opened from a keyboard button.
    return {
        "keyboard": [[{"text": "📅 Open planner", "web_app": {"url": app_url()}}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self._username = None

    def _post(self, method, payload):
        try:
            r = requests.post(f"{self.base}/{method}", json=payload, timeout=20)
            if not r.ok:
                print(f"{method} failed: {r.status_code} {r.text}", file=sys.stderr)
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"{method} error: {e}", file=sys.stderr)
            return None

    def send(self, text, chat=None, markup=None):
        payload = {"chat_id": chat or self.chat_id, "text": text[:4000]}
        if markup:
            payload["reply_markup"] = markup
        return self._post("sendMessage", payload) is not None

    def username(self):
        if self._username is None:
            res = self._post("getMe", {})
            self._username = ((res or {}).get("result") or {}).get("username", "")
        return self._username

    def is_member(self, user_id):
        """True if the user belongs to the configured study group."""
        if not user_id:
            return False
        res = self._post("getChatMember", {"chat_id": self.chat_id, "user_id": user_id})
        member = (res or {}).get("result") or {}
        status = member.get("status")
        return status in ("creator", "administrator", "member") or (
            status == "restricted" and bool(member.get("is_member"))
        )

    def get_updates(self, offset):
        params = {"timeout": 0, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(f"{self.base}/getUpdates", params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("result", [])


# ---------------------------------------------------------------- helpers
def human_delta(seconds):
    seconds = int(seconds)
    if seconds < 0:
        return "overdue"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_due(due_ts, now):
    local = datetime.fromtimestamp(due_ts, TZ)
    return f"{local:%a %d %b %Y, %H:%M} (in {human_delta(due_ts - now.timestamp())})"


def today_str(now):
    return now.astimezone(TZ).strftime("%Y-%m-%d")


def clean_text(value, limit=200):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_day(value):
    return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y-%m-%d")


def parse_due(value):
    return datetime.strptime(str(value), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)


def upcoming_text(db, now, limit=None):
    rows = db.execute(
        "SELECT * FROM deadlines WHERE due_ts > ? ORDER BY due_ts", (int(now.timestamp()),)
    ).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        return "No upcoming deadlines."
    return "\n".join(f"#{r['id']} {r['title']} - {fmt_due(r['due_ts'], now)}" for r in rows)


def aims_text(db, now):
    rows = db.execute(
        "SELECT * FROM aims WHERE day = ? ORDER BY id", (today_str(now),)
    ).fetchall()
    if not rows:
        return "No aims set for today. Add one with /aim <text>."
    return "\n".join(
        f"{'[x]' if r['done'] else '[ ]'} #{r['id']} {r['text']}" for r in rows
    )


# ---------------------------------------------------------------- deadlines
def mark_passed_offsets(db, did, due_ts, now):
    """Don't fire reminders for offsets that had already passed."""
    for label, secs in OFFSETS:
        if now.timestamp() >= due_ts - secs:
            db.execute(
                "INSERT OR IGNORE INTO reminders_sent (deadline_id, label) VALUES (?, ?)",
                (did, label),
            )


def add_deadline(db, title, due, now):
    due_ts = int(due.timestamp())
    cur = db.execute("INSERT INTO deadlines (title, due_ts) VALUES (?, ?)", (title, due_ts))
    mark_passed_offsets(db, cur.lastrowid, due_ts, now)
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- commands
ADD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s+(.+)$", re.S)


def cmd_add(db, args, now):
    m = ADD_RE.match(args.strip())
    if not m:
        return "Usage: /add YYYY-MM-DD [HH:MM] Title\nExample: /add 2026-12-05 14:00 Calculus exam"
    date_s, time_s, title = m.group(1), m.group(2) or "23:59", m.group(3).strip()
    try:
        naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "That date or time isn't valid. Use YYYY-MM-DD and 24-hour HH:MM."
    due = naive.replace(tzinfo=TZ)
    if due <= now:
        return "That deadline is already in the past."
    did = add_deadline(db, title, due, now)
    return f"Added #{did}: {title}\nDue {fmt_due(int(due.timestamp()), now)}"


def cmd_remove(db, args, now):
    if not args.strip().lstrip("#").isdigit():
        return "Usage: /remove ID (see /deadlines)"
    did = int(args.strip().lstrip("#"))
    row = db.execute("SELECT title FROM deadlines WHERE id = ?", (did,)).fetchone()
    if not row:
        return f"No deadline with ID {did}."
    db.execute("DELETE FROM deadlines WHERE id = ?", (did,))
    db.execute("DELETE FROM reminders_sent WHERE deadline_id = ?", (did,))
    db.commit()
    return f"Removed #{did}: {row['title']}"


def cmd_aim(db, args, now):
    text = args.strip()
    if not text:
        return "Usage: /aim <what you want to do today>"
    db.execute("INSERT INTO aims (day, text) VALUES (?, ?)", (today_str(now), text))
    db.commit()
    return "Aim added.\n\n" + aims_text(db, now)


def cmd_done(db, args, now):
    s = args.strip().lstrip("#")
    if not s.isdigit():
        return "Usage: /done ID (see /today)"
    cur = db.execute("UPDATE aims SET done = 1 WHERE id = ? AND day = ?", (int(s), today_str(now)))
    db.commit()
    if cur.rowcount == 0:
        return f"No aim #{s} for today."
    return "Nice work!\n\n" + aims_text(db, now)


def handle_command(db, text, now):
    parts = text.strip().split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        return cmd_add(db, args, now)
    if cmd == "/deadlines":
        return "Upcoming deadlines:\n" + upcoming_text(db, now)
    if cmd == "/remove":
        return cmd_remove(db, args, now)
    if cmd == "/aim":
        return cmd_aim(db, args, now)
    if cmd == "/today":
        return "Today's plan:\n" + aims_text(db, now)
    if cmd == "/done":
        return cmd_done(db, args, now)
    if cmd == "/motivate":
        return random.choice(MOTIVATION)
    return None  # unknown command: stay quiet (other bots may share the group)


# ---------------------------------------------------------------- mini app
def _apply_op(db, op, now):
    kind = op["op"]
    if kind == "add_deadline":
        title = clean_text(op["title"])
        due = parse_due(op["due"])
        if not title or due <= now:
            raise ValueError("bad deadline")
        add_deadline(db, title, due, now)
        return f"+ deadline: {title} ({due:%d %b %H:%M})"
    if kind == "edit_deadline":
        did = int(op["id"])
        row = db.execute("SELECT * FROM deadlines WHERE id = ?", (did,)).fetchone()
        if not row:
            raise ValueError("no such deadline")
        title = clean_text(op.get("title", row["title"]))
        if not title:
            raise ValueError("empty title")
        due_ts = row["due_ts"]
        if "due" in op:
            due = parse_due(op["due"])
            if due <= now:
                raise ValueError("deadline in the past")
            if int(due.timestamp()) != due_ts:
                due_ts = int(due.timestamp())
                db.execute("DELETE FROM reminders_sent WHERE deadline_id = ?", (did,))
                mark_passed_offsets(db, did, due_ts, now)
        db.execute("UPDATE deadlines SET title = ?, due_ts = ? WHERE id = ?", (title, due_ts, did))
        return f"~ deadline: {title}"
    if kind == "remove_deadline":
        did = int(op["id"])
        row = db.execute("SELECT title FROM deadlines WHERE id = ?", (did,)).fetchone()
        if not row:
            raise ValueError("no such deadline")
        db.execute("DELETE FROM deadlines WHERE id = ?", (did,))
        db.execute("DELETE FROM reminders_sent WHERE deadline_id = ?", (did,))
        return f"- deadline: {row['title']}"
    if kind == "add_aim":
        day, text = parse_day(op["day"]), clean_text(op["text"])
        if not text:
            raise ValueError("empty aim")
        db.execute(
            "INSERT INTO aims (day, text, done) VALUES (?, ?, ?)", (day, text, int(bool(op.get("done"))))
        )
        return f"+ aim ({day}): {text}"
    if kind in ("edit_aim", "done_aim", "remove_aim"):
        aid = int(op["id"])
        row = db.execute("SELECT * FROM aims WHERE id = ?", (aid,)).fetchone()
        if not row:
            raise ValueError("no such aim")
        if kind == "remove_aim":
            db.execute("DELETE FROM aims WHERE id = ?", (aid,))
            return f"- aim: {row['text']}"
        if kind == "done_aim":
            done = int(bool(op.get("done")))
            db.execute("UPDATE aims SET done = ? WHERE id = ?", (done, aid))
            return f"{'done' if done else 'not done'}: {row['text']}"
        text = clean_text(op.get("text", row["text"]))
        day = parse_day(op["day"]) if "day" in op else row["day"]
        if not text:
            raise ValueError("empty aim")
        db.execute("UPDATE aims SET text = ?, day = ? WHERE id = ?", (text, day, aid))
        return f"~ aim: {text}"
    raise ValueError("unknown operation")


def apply_ops(db, ops, now):
    """Apply Mini App operations. Returns (summary lines, number of failed operations)."""
    lines, failed = [], 0
    for op in ops[:MAX_OPS]:
        try:
            lines.append(_apply_op(db, op, now))
        except (ValueError, KeyError, TypeError, AttributeError):
            failed += 1
    failed += max(0, len(ops) - MAX_OPS)
    db.commit()
    return lines, failed


def handle_web_app_data(db, tg, msg, now):
    user = msg.get("from") or {}
    chat = msg["chat"]["id"]
    if not tg.is_member(user.get("id")):
        tg.send("Only members of the study group can change the planner.", chat=chat)
        return
    try:
        data = json.loads(msg["web_app_data"]["data"])
        ops = data["ops"]
        batch = str(data.get("batch", ""))[:40]
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
    except (ValueError, KeyError, TypeError):
        tg.send("Couldn't read the planner changes. Open the planner and save again.", chat=chat)
        return
    applied = json.loads(get_meta(db, "applied_batches", "[]"))
    if batch and batch in applied:
        tg.send("Those changes were already saved.", chat=chat)
        return
    lines, failed = apply_ops(db, ops, now)
    if batch:
        applied.append(batch)
        set_meta(db, "applied_batches", json.dumps(applied[-50:]))
    note = f"Saved {len(lines)} change{'s' if len(lines) != 1 else ''}."
    if failed:
        note += f" {failed} could not be applied (past deadline or item no longer exists)."
    tg.send(note, chat=chat)
    if lines:
        name = clean_text(user.get("first_name") or "Someone", 40)
        tg.send(f"{name} updated the planner:\n" + "\n".join(lines[:15]))


def handle_group_message(db, tg, msg, now):
    text = msg.get("text") or ""
    if not text.startswith("/"):
        return
    if text.split()[0].split("@")[0].lower() == "/app":
        username = tg.username()
        if not app_url() or not username:
            tg.send("The planner isn't set up yet. The group admin needs to set APP_URL.")
            return
        tg.send(
            "The planner opens in a private chat with the bot.",
            markup={
                "inline_keyboard": [
                    [{"text": "Open planner", "url": f"https://t.me/{username}?start=planner"}]
                ]
            },
        )
        return
    reply = handle_command(db, text, now)
    if reply:
        tg.send(reply)


def handle_private_message(db, tg, msg, now):
    chat = msg["chat"]["id"]
    text = msg.get("text") or ""
    if not text.startswith(("/start", "/app", "/help")):
        return
    if not tg.is_member((msg.get("from") or {}).get("id")):
        tg.send("This bot is for members of the study group.", chat=chat)
        return
    if not app_url():
        tg.send("The planner isn't set up yet. The group admin needs to set APP_URL.", chat=chat)
        return
    tg.send(
        "Tap Open planner below to see and edit deadlines and aims. "
        "Open it from that button so Save works.",
        chat=chat,
        markup=planner_keyboard(),
    )


def process_updates(db, tg, now):
    offset = get_meta(db, "update_offset")
    updates = tg.get_updates(int(offset) if offset else None)
    for u in updates:
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        try:
            if "web_app_data" in msg:
                handle_web_app_data(db, tg, msg, now)
            elif str(chat.get("id")) == CHAT_ID:
                handle_group_message(db, tg, msg, now)
            elif chat.get("type") == "private":
                handle_private_message(db, tg, msg, now)
        except Exception as e:  # never let one bad message block the queue
            print(f"update {u.get('update_id')} failed: {e!r}", file=sys.stderr)
        set_meta(db, "update_offset", u["update_id"] + 1)
    return len(updates)


def build_state(db, now):
    local = now.astimezone(TZ)
    cutoff_ts = int((now - timedelta(days=60)).timestamp())
    cutoff_day = (local - timedelta(days=60)).strftime("%Y-%m-%d")
    deadlines = [
        {
            "id": r["id"],
            "title": r["title"],
            "due": datetime.fromtimestamp(r["due_ts"], TZ).strftime("%Y-%m-%dT%H:%M"),
            "ts": r["due_ts"],
        }
        for r in db.execute(
            "SELECT * FROM deadlines WHERE due_ts >= ? ORDER BY due_ts", (cutoff_ts,)
        )
    ]
    aims = [
        {"id": r["id"], "day": r["day"], "text": r["text"], "done": bool(r["done"])}
        for r in db.execute("SELECT * FROM aims WHERE day >= ? ORDER BY day, id", (cutoff_day,))
    ]
    return {
        "v": 1,
        "tz": "Asia/Tashkent",
        "offset_min": int(local.utcoffset().total_seconds() // 60),
        "deadlines": deadlines,
        "aims": aims,
        "applied": json.loads(get_meta(db, "applied_batches", "[]")),
        "quotes": MOTIVATION,
    }


def write_state(db, now, path=STATE_PATH):
    """Write the Mini App's state file. Returns True if the file changed."""
    text = json.dumps(build_state(db, now), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


# ---------------------------------------------------------------- scheduled
def check_deadlines(db, tg, now):
    ts = now.timestamp()
    rows = db.execute("SELECT * FROM deadlines WHERE due_ts > ?", (int(ts),)).fetchall()
    for r in rows:
        sent = {
            x["label"]
            for x in db.execute(
                "SELECT label FROM reminders_sent WHERE deadline_id = ?", (r["id"],)
            )
        }
        due_now = [(l, s) for l, s in OFFSETS if ts >= r["due_ts"] - s and l not in sent]
        if not due_now:
            continue
        tightest = min(due_now, key=lambda x: x[1])[0]
        ok = tg.send(
            f"Deadline reminder ({tightest} left)\n{r['title']}\nDue {fmt_due(r['due_ts'], now)}"
        )
        if ok:
            for label, _ in due_now:
                db.execute(
                    "INSERT OR IGNORE INTO reminders_sent (deadline_id, label) VALUES (?, ?)",
                    (r["id"], label),
                )
            db.commit()


def build_slot_message(db, name, title, now):
    if name == "morning":
        return (
            f"{title}! Let's make today count.\n\n"
            f"Today's aims:\n{aims_text(db, now)}\n\n"
            f"Next deadlines:\n{upcoming_text(db, now, limit=5)}\n\n"
            f"{random.choice(MOTIVATION)}\n\nPlan your day with /app or /aim <text>."
        )
    if name == "midday":
        return (
            f"{title}. How is the plan going?\n\n{aims_text(db, now)}\n\n"
            f"Mark finished ones with /done ID or in /app."
        )
    return (
        f"{title}. Time to look back at today.\n\n{aims_text(db, now)}\n\n"
        f"Upcoming deadlines:\n{upcoming_text(db, now, limit=5)}\n\n"
        f"{random.choice(MOTIVATION)}"
    )


def run_slots(db, tg, now):
    local = now.astimezone(TZ)
    today = today_str(now)
    for name, hour, minute, title in SLOTS:
        slot_time = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        key = f"slot_{name}"
        if local < slot_time or get_meta(db, key) == today:
            continue
        if local - slot_time > MAX_SLOT_LATENESS:
            set_meta(db, key, today)  # missed window; don't send stale message
            continue
        if tg.send(build_slot_message(db, name, title, local)):
            set_meta(db, key, today)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("BOT_TOKEN and CHAT_ID environment variables are required.")
    db = connect()
    tg = Telegram(BOT_TOKEN, CHAT_ID)
    now = datetime.now(timezone.utc)
    process_updates(db, tg, now)
    now = datetime.now(timezone.utc)
    check_deadlines(db, tg, now)
    run_slots(db, tg, now)
    write_state(db, now)


if __name__ == "__main__":
    main()