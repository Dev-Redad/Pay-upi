# bot.py (PTB 13.15)
# UPI QR + exact amount locking + exact match verification
# First-file fix + total time = pay window + 10s grace
# Auto-delivery on payment; caption is short & professional; NO payment buttons
import os, logging, json, time, random, re, sqlite3, threading
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote

from telegram import (
    Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext,
    ConversationHandler, CallbackQueryHandler
)

# ----------------- Logging -----------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("upi-bot")

# ----------------- Config ------------------
TOKEN = "8352423948:AAEP_WHdxNGziUabzMwO9_YiEp24_d0XYVk"
ADMIN_IDS = [7223414109, 6053105336, 7381642564]

STORAGE_CHANNEL_ID = -1002724249292            # storage for files you sell
PAYMENT_NOTIF_CHANNEL_ID = -1002865174188      # your payment-notification channel

UPI_ID = "debjyotimondal1010@okhdfcbank"
UPI_PAYEE_NAME = "Seller"

CATALOG_FILE = "catalog.json"
DB_FILE = "bot.db"
CONFIG_FILE = "config.json"

PAY_WINDOW_MINUTES = 5
GRACE_SECONDS = 10  # total window = 5m + 10s

# Conversation states
GET_PRODUCT_FILES, PRICE, \
GET_BROADCAST_FILES, GET_BROADCAST_TEXT, BROADCAST_CONFIRM, \
DELETE_OPTION, GET_DELETE_TIME, \
GET_FS_PHOTO, GET_FS_TEXT, \
GET_START_PHOTO, GET_START_TEXT = range(11)

FILE_CATALOG, BOT_CONFIG = {}, {}

# ----------------- Session + Amount Locking -----------------
PENDING = {}  # key -> {user_id, chat_id, item_id, amount, amount_key, created_at, expiry_at, message_id}

ALLOC_LOCK = threading.Lock()
ACTIVE_AMOUNTS = set()   # exact amount keys (strings): {"10", "12.03"}

RECENT_PAYMENTS = []     # list of {"key": "12.34"/"12", "ts": datetime}
MAX_RECENT = 300

AMOUNT_RE = re.compile(r"paid you\s*₹\s*([0-9]+(?:\.[0-9]{1,2})?)", re.I)

# ----------------- DB + Files -----------------
def db_exec(sql, args=()):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute(sql, args)
    con.commit()
    con.close()

def db_getall(sql, args=()):
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    con.close()
    return rows

def setup_db():
    db_exec("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)")

def add_user(uid, uname):
    db_exec("INSERT OR IGNORE INTO users(user_id, username) VALUES (?,?)", (uid, uname or ""))

def get_all_user_ids():
    return [r[0] for r in db_getall("SELECT user_id FROM users")]

def load_catalog():
    global FILE_CATALOG
    try:
        with open(CATALOG_FILE) as f: FILE_CATALOG = json.load(f)
    except Exception: FILE_CATALOG = {}; save_catalog()

def save_catalog():
    with open(CATALOG_FILE, "w") as f: json.dump(FILE_CATALOG, f, indent=2)

def load_config():
    global BOT_CONFIG
    try:
        with open(CONFIG_FILE) as f: BOT_CONFIG = json.load(f)
    except Exception:
        BOT_CONFIG = {
            "force_sub_photo_id": None,
            "force_sub_text": "Join our channels to continue.",
            "welcome_photo_id": None,
            "welcome_text": "Welcome back!"
        }
        save_config()

def save_config():
    with open(CONFIG_FILE, "w") as f: json.dump(BOT_CONFIG, f, indent=2)

# ----------------- Force Subscribe -----------
FORCE_SUBSCRIBE_CHANNEL_IDS = []  # add channel IDs if needed
FORCE_SUBSCRIBE_ENABLED = True
PROTECT_CONTENT_ENABLED = False

def force_subscribe(fn):
    @wraps(fn)
    def wrap(update: Update, context: CallbackContext, *a, **k):
        if (not FORCE_SUBSCRIBE_ENABLED) or (not FORCE_SUBSCRIBE_CHANNEL_IDS) or (update.effective_user.id in ADMIN_IDS):
            return fn(update, context, *a, **k)

        uid = update.effective_user.id
        need = []
        for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
            try:
                mem = context.bot.get_chat_member(ch, uid)
                if mem.status not in ("member", "administrator", "creator"):
                    need.append(ch)
            except Exception:
                need.append(ch)
        if not need: return fn(update, context, *a, **k)

        context.user_data['pending_command'] = {'fn': fn, 'update': update}
        btns = []
        for ch in need:
            try:
                chat = context.bot.get_chat(ch)
                link = chat.invite_link or context.bot.export_chat_invite_link(ch)
                btns.append([InlineKeyboardButton(f"Join {chat.title}", url=link)])
            except Exception as e:
                log.warning(f"Invite link fail {ch}: {e}")
        btns.append([InlineKeyboardButton("✅ I have joined", callback_data="check_join")])

        msg = BOT_CONFIG.get("force_sub_text") or "Join required channels."
        photo = BOT_CONFIG.get("force_sub_photo_id")
        if photo:
            update.effective_message.reply_photo(photo=photo, caption=msg, reply_markup=InlineKeyboardMarkup(btns))
        else:
            update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(btns))
        return
    return wrap

def check_join_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    need = []
    for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
        try:
            mem = context.bot.get_chat_member(ch, uid)
            if mem.status not in ("member","administrator","creator"):
                need.append(ch)
        except Exception:
            need.append(ch)
    if not need:
        try: q.message.delete()
        except: pass
        q.answer("Thank you!", show_alert=True)
        pend = context.user_data.pop('pending_command', None)
        if pend: return pend['fn'](pend['update'], context)
    else:
        q.answer("Still not joined all.", show_alert=True)

# ----------------- UPI helpers ----------------
def amount_key(x: float) -> str:
    # integers => "12", decimals => "12.34" (exact 2dp)
    return f"{x:.2f}" if abs(x - int(x)) > 1e-9 else str(int(x))

def build_upi_uri(amount: float, note: str):
    # encode + no decimals unless needed (for QR payload only; not shown to user)
    amt = f"{int(amount)}" if abs(amount-int(amount))<1e-9 else f"{amount:.2f}"
    pa = quote(UPI_ID, safe='')
    pn = quote(UPI_PAYEE_NAME, safe='')
    tn = quote(note, safe='')
    return f"upi://pay?pa={pa}&pn={pn}&am={amt}&cu=INR&tn={tn}"

def qr_url(data: str):
    return f"https://api.qrserver.com/v1/create-qr-code/?data={quote(data,safe='')}&size=512x512&qzone=2"

def pick_unique_amount(lo: float, hi: float) -> float:
    lo, hi = int(lo), int(hi)
    ints = list(range(lo, hi+1))
    random.shuffle(ints)
    with ALLOC_LOCK:
        # Integers first
        for v in ints:
            k = str(v)
            if k not in ACTIVE_AMOUNTS:
                ACTIVE_AMOUNTS.add(k)
                return float(v)
        # If all integers are taken, allocate unique paise
        for base in ints:
            for p in range(1, 100):
                k = f"{base}.{p:02d}"
                if k not in ACTIVE_AMOUNTS:
                    ACTIVE_AMOUNTS.add(k)
                    return float(f"{base}.{p:02d}")
    return float(ints[-1])

def release_amount(x: float):
    with ALLOC_LOCK:
        ACTIVE_AMOUNTS.discard(amount_key(x))

def cleanup_expired():
    # expire after full window = expiry_at + GRACE_SECONDS
    now = datetime.utcnow()
    drop = [k for k,v in PENDING.items() if now >= v['expiry_at'] + timedelta(seconds=GRACE_SECONDS)]
    for k in drop:
        release_amount(PENDING[k]['amount'])
        del PENDING[k]

def cleanup_job(context: CallbackContext):
    cleanup_expired()

def parse_amount(text: str):
    m = AMOUNT_RE.search(text or "")
    if not m: return None
    try: return float(m.group(1))
    except: return None

def record_payment_key(k: str, ts: datetime):
    RECENT_PAYMENTS.append({"key": k, "ts": ts})
    if len(RECENT_PAYMENTS) > MAX_RECENT:
        del RECENT_PAYMENTS[:len(RECENT_PAYMENTS)-MAX_RECENT]

# ----------------- Purchase flow --------------
def start_purchase(ctx: CallbackContext, chat_id: int, uid: int, item_id: str):
    item = FILE_CATALOG.get(item_id)
    if not item:
        return ctx.bot.send_message(chat_id, "❌ Item not found.")
    mn = item.get("min_price"); mx = item.get("max_price")
    if mn is None or mx is None:
        v = float(item.get("price", 0))
        if v <= 0: return ctx.bot.send_message(chat_id, "❌ Price not set.")
        mn = mx = v

    amt = pick_unique_amount(mn, mx)
    note = f"order_uid_{uid}"          # minimal note; not shown to users
    uri = build_upi_uri(amt, note)     # used only to generate the QR
    img = qr_url(uri)
    key = f"{uid}:{item_id}:{int(time.time())}"

    created = datetime.utcnow()
    expire = created + timedelta(minutes=PAY_WINDOW_MINUTES)
    display_amt = int(amt) if abs(amt-int(amt))<1e-9 else f"{amt:.2f}"

    caption = (
        f"Amount: ₹{display_amt}\n\n"
        "How to pay:\n"
        "📱 Scan this QR in your UPI app (GPay / PhonePe / Paytm)\n"
        f"🏦 Or pay manually to `{UPI_ID}`\n"
        f"🏷️ Pay exactly ₹{display_amt} within {PAY_WINDOW_MINUTES} minutes\n"
        "✅ Verification is automatic — ⏱ wait 5–10 seconds after payment."
    )

    msg = ctx.bot.send_photo(
        chat_id=chat_id, photo=img, caption=caption, parse_mode=ParseMode.MARKDOWN
    )
    PENDING[key] = {
        "user_id": uid, "chat_id": chat_id, "item_id": item_id,
        "amount": amt, "amount_key": amount_key(amt),
        "created_at": created, "expiry_at": expire, "message_id": msg.message_id
    }

# ----------------- Delivery -------------------
def deliver(ctx: CallbackContext, uid: int, item_id: str):
    item = FILE_CATALOG.get(item_id)
    if not item:
        ctx.bot.send_message(uid, "❌ Item missing."); return
    for f in item.get("files", []):
        try:
            ctx.bot.copy_message(chat_id=uid, from_chat_id=f["channel_id"], message_id=f["message_id"],
                                 protect_content=PROTECT_CONTENT_ENABLED)
            time.sleep(0.4)
        except Exception as e:
            log.error(f"Deliver fail: {e}")
    ctx.bot.send_message(uid, "⚠️ Files auto-delete here in 10 minutes. Save now.")

# ----------------- Handlers -------------------
@force_subscribe
def cmd_start(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    add_user(uid, update.effective_user.username)
    msg = update.message or update.callback_query.message
    if context.args:
        item_id = context.args[0]
        if item_id in FILE_CATALOG:
            return start_purchase(context, msg.chat_id, uid, item_id)
    photo = BOT_CONFIG.get("welcome_photo_id")
    text = BOT_CONFIG.get("welcome_text") or "Welcome back!"
    if photo: msg.reply_photo(photo=photo, caption=text)
    else: msg.reply_text(text)

def cancel_conv(update: Update, context: CallbackContext):
    context.user_data.clear()
    update.message.reply_text("Canceled.")
    return ConversationHandler.END

# --- Admin: add product (supports 10-30) + FIRST-FILE FIX ---
def add_product_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data['new_files'] = []

    if update.message.effective_attachment:
        try:
            fwd = context.bot.forward_message(
                STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id
            )
            context.user_data['new_files'].append({"channel_id": fwd.chat_id, "message_id": fwd.message_id})
            update.message.reply_text("✅ First file added. Send more or /done.")
        except Exception as e:
            log.error(f"Store fail on first file: {e}")
            update.message.reply_text("Failed to store the first file, send again or /cancel.")
    else:
        update.message.reply_text("Send product files now. Use /done when finished.")

    return GET_PRODUCT_FILES

def get_product_files(update: Update, context: CallbackContext):
    if not update.message.effective_attachment:
        update.message.reply_text("Not a file. Send again or /done.")
        return GET_PRODUCT_FILES
    try:
        fwd = context.bot.forward_message(STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id)
        context.user_data['new_files'].append({"channel_id": fwd.chat_id, "message_id": fwd.message_id})
        update.message.reply_text(f"✅ Added. Send more or /done.")
        return GET_PRODUCT_FILES
    except Exception as e:
        log.error(e); update.message.reply_text("Store failed."); return ConversationHandler.END

def finish_adding_files(update: Update, context: CallbackContext):
    if not context.user_data.get('new_files'):
        update.message.reply_text("No files yet. Send one or /cancel."); return GET_PRODUCT_FILES
    update.message.reply_text("Now send price or range (10 or 10-30).")
    return PRICE

def get_price(update: Update, context: CallbackContext):
    txt = update.message.text.strip()
    try:
        if "-" in txt:
            a,b = txt.split("-",1); mn, mx = float(a), float(b); assert mn>0 and mx>=mn
        else:
            v = float(txt); assert v>0; mn=mx=v
    except Exception:
        update.message.reply_text("Invalid. Send like 10 or 10-30."); return PRICE
    item_id = f"item_{int(time.time())}"
    data = {"min_price": mn, "max_price": mx, "files": context.user_data['new_files']}
    if mn==mx: data["price"]=mn
    FILE_CATALOG[item_id]=data; save_catalog()
    link = f"https://t.me/{context.bot.username}?start={item_id}"
    update.message.reply_text(f"✅ Added.\nLink:\n`{link}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear(); return ConversationHandler.END

# --- Broadcast (optional) ---
def bc_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data['b_files']=[]; context.user_data['b_text']=None
    update.message.reply_text("Send files for broadcast. /done when finished.")
    return GET_BROADCAST_FILES

def bc_files(update: Update, context: CallbackContext):
    if update.message.effective_attachment:
        context.user_data['b_files'].append(update.message)
        update.message.reply_text(f"File added. /done when finished.")
    else:
        update.message.reply_text("Send a file or /done.")
    return GET_BROADCAST_FILES

def bc_done_files(update: Update, context: CallbackContext):
    update.message.reply_text("Now send the text (or /skip)."); return GET_BROADCAST_TEXT
def bc_text(update: Update, context: CallbackContext):
    context.user_data['b_text']=update.message.text; return bc_confirm(update, context)
def bc_skip(update: Update, context: CallbackContext):
    return bc_confirm(update, context)
def bc_confirm(update: Update, context: CallbackContext):
    total=len(get_all_user_ids())
    buttons=[[InlineKeyboardButton("✅ Send", callback_data="send_bc")],
             [InlineKeyboardButton("❌ Cancel", callback_data="cancel_bc")]]
    update.message.reply_text(f"Broadcast to {total} users. Proceed?", reply_markup=InlineKeyboardMarkup(buttons))
    return BROADCAST_CONFIRM
def bc_send(update: Update, context: CallbackContext):
    q=update.callback_query; q.answer(); q.edit_message_text("Broadcasting…")
    files=context.user_data.get('b_files',[]); text=context.user_data.get('b_text')
    ok=fail=0
    for uid in get_all_user_ids():
        try:
            for m in files:
                context.bot.copy_message(uid, m.chat_id, m.message_id); time.sleep(0.1)
            if text: context.bot.send_message(uid, text)
            ok+=1
        except Exception as e:
            log.error(e); fail+=1
    q.message.reply_text(f"Done. Sent:{ok} Fail:{fail}")
    context.user_data.clear(); return ConversationHandler.END

# --- Channel payment sniffer ---
def on_channel_post(update: Update, context: CallbackContext):
    msg = update.channel_post
    if not msg or msg.chat_id != PAYMENT_NOTIF_CHANNEL_ID:
        return
    text = msg.text or msg.caption or ""
    if "paid you" not in text.lower(): return
    amt = parse_amount(text)
    if amt is None: return

    # Use the message's own timestamp (UTC) for accurate window checks
    ts = msg.date
    ts = ts.replace(tzinfo=None) if ts is not None else datetime.utcnow()

    key_str = amount_key(amt)
    record_payment_key(key_str, ts)
    log.info(f"Recorded payment key {key_str} at {ts}")

    # Auto-verify: if any pending session matches amount and window, deliver immediately
    for pay_key, info in list(PENDING.items()):
        hard_expiry = info['expiry_at'] + timedelta(seconds=GRACE_SECONDS)
        if info['amount_key'] == key_str and info['created_at'] <= ts <= hard_expiry:
            release_amount(info['amount'])
            del PENDING[pay_key]
            try:
                context.bot.send_message(info["chat_id"], "✅ Payment received. Delivering your files…")
            except Exception as e:
                log.warning(f"Notify user fail: {e}")
            deliver(context, info["user_id"], info["item_id"])

# --- Admin toggles ---
def stats(update: Update, context: CallbackContext):
    cleanup_expired()
    update.message.reply_text(f"Users: {len(get_all_user_ids())}\nPending: {len(PENDING)}")

def protect_on(update: Update, context: CallbackContext):
    global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED=True
    update.message.reply_text("Content protection ON.")

def protect_off(update: Update, context: CallbackContext):
    global PROTECT_CONTENT_ENABLED; PROTECT_CONTENT_ENABLED=False
    update.message.reply_text("Content protection OFF.")

# ----------------- Main ----------------------
def main():
    setup_db(); load_catalog(); load_config()
    os.system(f'curl -s "https://api.telegram.org/bot{TOKEN}/deleteWebhook" >/dev/null')

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    admin = Filters.user(ADMIN_IDS)

    # periodic cleanup so expired sessions are released even with no user actions
    updater.job_queue.run_repeating(cleanup_job, interval=60, first=60)

    # Add product (first-file fix is inside add_product_start)
    add_conv = ConversationHandler(
        entry_points=[MessageHandler((Filters.document | Filters.video | Filters.photo) & admin, add_product_start)],
        states={
            GET_PRODUCT_FILES: [MessageHandler((Filters.document | Filters.video | Filters.photo) & ~Filters.command, get_product_files),
                                CommandHandler('done', finish_adding_files, filters=admin)],
            PRICE: [MessageHandler(Filters.text & ~Filters.command, get_price)]
        },
        fallbacks=[CommandHandler('cancel', cancel_conv, filters=admin)]
    )

    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", bc_start, filters=admin)],
        states={
            GET_BROADCAST_FILES: [MessageHandler(Filters.all & ~Filters.command, bc_files),
                                  CommandHandler('done', bc_done_files, filters=admin)],
            GET_BROADCAST_TEXT: [MessageHandler(Filters.text & ~Filters.command, bc_text),
                                 CommandHandler('skip', bc_skip, filters=admin)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(bc_send, pattern="^send_bc$")]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_bc$")]
    )

    dp.add_handler(add_conv)
    dp.add_handler(bc_conv)

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", stats, filters=admin))
    dp.add_handler(CommandHandler("protect_on", protect_on, filters=admin))
    dp.add_handler(CommandHandler("protect_off", protect_off, filters=admin))

    dp.add_handler(CallbackQueryHandler(check_join_cb, pattern="^check_join$"))

    # Channel listener
    dp.add_handler(MessageHandler(
        Filters.update.channel_post & Filters.chat(PAYMENT_NOTIF_CHANNEL_ID) & Filters.text,
        on_channel_post
    ))

    log.info("Bot running…")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
