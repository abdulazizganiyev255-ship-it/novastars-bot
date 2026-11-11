# -*- coding: utf-8 -*-
"""Hisob to'ldirish boti: pyTelegramBotAPI (telebot) + Telethon userbot.

O'rnatish:   pip install "pyTelegramBotAPI>=4.23" telethon pysocks
Ishga tushirish:   python tolov.py
"""

import asyncio
import logging
import re
import sqlite3
import threading
import time

import telebot
from telebot import types
from telethon import TelegramClient, events
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

# ══════════════════════════════════════════ SOZLAMALAR ══════════════════
BOT_TOKEN   = "8634135076:AAGFlQ-257hWeyTK_wLXgewO_gkEgYnpy9M"
API_ID      = 32489865
API_HASH    = "947a7be352c09a69f5b4885165e88745"
CARD_NUMBER = "9860 3501 4601 3064"
CARD_OWNER  = "F. I."
HUMO_CHAT   = "HUMOcardbot"
ORDER_TTL   = 300        # 5 daqiqa
MIN_AMOUNT  = 1_000
MAX_AMOUNT  = 50_000_000

# Admin user ID lar:
ADMIN_IDS   = [8760585426]

# ══════════════════════════════════════════ PROXY ═══════════════════════
USE_MTPROTO    = False
MTPROTO_HOST   = ""
MTPROTO_PORT   = 443
MTPROTO_SECRET = ""

USE_SOCKS5     = False
SOCKS5_HOST    = "127.0.0.1"
SOCKS5_PORT    = 1080
SOCKS5_USER    = ""
SOCKS5_PASS    = ""

# ══════════════════════════════════════════ LOGGING ═════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("balans")

# HUMO xabarlarini alohida faylga saqlash
humo_log = logging.getLogger("humo_raw")
_fh = logging.FileHandler("tolov_humo.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
humo_log.addHandler(_fh)
humo_log.setLevel(logging.DEBUG)

# ══════════════════════════════════════════ DATABASE ════════════════════
lock = threading.RLock()
db = sqlite3.connect("balans.db", isolation_level=None, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id      INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    paid_at    INTEGER
);
CREATE TABLE IF NOT EXISTS seen (msg_id INTEGER PRIMARY KEY);
""")

# Mavjud bazalar uchun ustun qo'shish
try:
    db.execute("ALTER TABLE topups ADD COLUMN paid_at INTEGER")
except Exception:
    pass


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def ensure_user(uid: int):
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))


def get_balance(uid: int) -> int:
    with lock:
        ensure_user(uid)
        return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]


def get_user_stats(uid: int):
    """Foydalanuvchi balansi, jami tushgan pul va oxirgi to'lovlar."""
    with lock:
        ensure_user(uid)
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        total_paid = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM topups WHERE user_id=? AND status='paid'",
            (uid,),
        ).fetchone()[0]
        recent_paid = db.execute(
            "SELECT amount, COALESCE(paid_at, created_at) FROM topups "
            "WHERE user_id=? AND status='paid' ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()
        return bal, total_paid, recent_paid


def get_all_topups_history(uid: int):
    """Oxirgi 5 ta barcha buyurtmalar holati."""
    with lock:
        return db.execute(
            "SELECT id, amount, status, created_at FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()


def create_topup(uid: int, amount: int):
    now = int(time.time())
    with lock:
        while db.execute(
            "SELECT 1 FROM topups WHERE status='pending' AND expires_at>? AND amount=?",
            (now, amount),
        ).fetchone():
            amount += 1
        cur = db.execute(
            "INSERT INTO topups(user_id, amount, created_at, expires_at) VALUES(?,?,?,?)",
            (uid, amount, now, now + ORDER_TTL),
        )
        return cur.lastrowid, amount


def cancel_topup(tid: int, uid: int) -> bool:
    with lock:
        cur = db.execute(
            "UPDATE topups SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
            (tid, uid),
        )
        return cur.rowcount == 1


def claim_and_credit(amount: int, paid_ts: float):
    """
    Summaga mos buyurtmani topib balansi to'ldiradi.
    """
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            # 1-urinish: pending buyurtma
            row = db.execute(
                "SELECT id, user_id FROM topups WHERE status='pending' AND amount=? ORDER BY id DESC LIMIT 1",
                (amount,),
            ).fetchone()

            # 2-urinish: kechikkan bo'lsa (expired holatdagi buyurtma)
            if not row:
                row = db.execute(
                    "SELECT id, user_id FROM topups WHERE status='expired' AND amount=? ORDER BY id DESC LIMIT 1",
                    (amount,),
                ).fetchone()

            if not row:
                db.execute("ROLLBACK")
                return None

            order_id, user_id = row[0], row[1]
            now_ts = int(paid_ts) if paid_ts else int(time.time())
            db.execute("UPDATE topups SET status='paid', paid_at=? WHERE id=?", (now_ts, order_id))
            ensure_user(user_id)
            db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
            bal = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]
            db.execute("COMMIT")
            return user_id, bal
        except Exception:
            db.execute("ROLLBACK")
            raise


def mark_seen(msg_id: int) -> bool:
    with lock:
        return db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (msg_id,)).rowcount == 1


def admin_credit(uid: int, amount: int) -> int:
    with lock:
        ensure_user(uid)
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
        now = int(time.time())
        db.execute(
            "INSERT INTO topups(user_id, amount, status, created_at, expires_at, paid_at) VALUES(?,?,'paid',?,?,?)",
            (uid, amount, now, now, now),
        )
        return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]


def get_expired_topups() -> list:
    now = int(time.time())
    with lock:
        rows = db.execute(
            "SELECT id, user_id FROM topups WHERE status='pending' AND expires_at<?", (now,)
        ).fetchall()
        if rows:
            ids = ",".join(str(r[0]) for r in rows)
            db.execute(f"UPDATE topups SET status='expired' WHERE id IN ({ids})")
        return rows


# ══════════════════════════════════════════ HUMO PARSER ════════════════
def parse_income(text: str):
    """
    HUMO bot xabar formati:
      🎉 To'ldirish
      ➕ 1.000,00 UZS
      💳 HUMOCARD *3064
      💰 53.917,00 UZS (balans, olinmaydi)
    """
    m = re.search(
        r"(?:[\u2795+\uff0b])\s*([\d\s.,]+?)\s*UZS",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None

    raw = m.group(1).strip().replace(" ", "")

    if re.match(r"^\d{1,3}(\.\d{3})+(,\d*)?$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.match(r"^\d{1,3}(,\d{3})+(\.\d*)?$", raw):
        raw = raw.replace(",", "")
    else:
        raw = raw.replace(",", ".")

    try:
        v = float(raw)
    except ValueError:
        log.warning("parse_income float xatosi: %r", raw)
        return None

    return int(v) if v > 0 and v == int(v) else None


# ══════════════════════════════════════════ TELETHON CLIENT ════════════
def _build_client() -> TelegramClient:
    if USE_MTPROTO and MTPROTO_HOST and MTPROTO_SECRET:
        proxy = (MTPROTO_HOST, MTPROTO_PORT, bytes.fromhex(MTPROTO_SECRET))
        log.info("MTProto proxy: %s:%s", MTPROTO_HOST, MTPROTO_PORT)
        return TelegramClient(
            "humo_session", API_ID, API_HASH,
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
            proxy=proxy,
        )
    if USE_SOCKS5 and SOCKS5_HOST:
        try:
            import socks
        except ImportError:
            raise RuntimeError("pip install pysocks")
        proxy_args = [socks.SOCKS5, SOCKS5_HOST, SOCKS5_PORT]
        if SOCKS5_USER:
            proxy_args += [True, SOCKS5_USER, SOCKS5_PASS]
        log.info("SOCKS5 proxy: %s:%s", SOCKS5_HOST, SOCKS5_PORT)
        return TelegramClient("humo_session", API_ID, API_HASH, proxy=tuple(proxy_args))
    return TelegramClient("humo_session", API_ID, API_HASH)


client = _build_client()
humo_entity = None


# ══════════════════════════════════════════ BOT ════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

_waiting_lock = threading.Lock()
_waiting: set = set()


def waiting_add(uid):
    with _waiting_lock:
        _waiting.add(uid)


def waiting_remove(uid):
    with _waiting_lock:
        _waiting.discard(uid)


def waiting_check(uid) -> bool:
    with _waiting_lock:
        return uid in _waiting


# ── Klaviaturalar ───────────────────────────────────────────────────────
def reply_main_kb():
    """Doimiy pastdagi tugmalar menyusi."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("💰 Balansim", "💳 Hisob to'ldirish")
    kb.row("📋 To'lovlar tarixi")
    return kb


def menu_kb():
    """Xabar ostidagi asosiy inline tugmalar."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💰 Balansim", callback_data="balance"),
        types.InlineKeyboardButton("💳 Hisob to'ldirish", callback_data="topup"),
    )
    kb.add(
        types.InlineKeyboardButton("📋 To'lovlar tarixi", callback_data="history"),
    )
    return kb


def balance_kb():
    """Balans ko'rsatilgandagi tugmalar."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 Hisob to'ldirish", callback_data="topup"),
        types.InlineKeyboardButton("🔄 Yangilash", callback_data="balance"),
    )
    kb.add(
        types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"),
    )
    return kb


def back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="menu"))
    return kb


def menu_text(uid: int, name: str) -> str:
    bal = get_balance(uid)
    return (
        f"Assalomu alaykum, <b>{name}</b>!\n\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"💰 Hisobingiz: <b>{fmt(bal)} so'm</b>\n\n"
        f"Quyidagi tugmalardan birini tanlang:"
    )


def balance_text(uid: int, name: str = "") -> str:
    """Balans va tushgan barcha pullarni batafsil ko'rsatuvchi matn."""
    bal, total_paid, recent_paid = get_user_stats(uid)
    text = (
        f"👤 <b>Foydalanuvchi:</b> {name or uid}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
        f"💰 <b>Joriy hisobingiz:</b> <b>{fmt(bal)} so'm</b>\n"
        f"📥 <b>Jami tushgan pul:</b> <b>{fmt(total_paid)} so'm</b>\n"
    )
    if recent_paid:
        text += "\n📋 <b>So'nggi tushgan pullar:</b>\n"
        for amt, ts in recent_paid:
            t_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            text += f"  ✅ <b>+{fmt(amt)} so'm</b> <i>({t_str})</i>\n"
    else:
        text += "\n<i>(Hozircha muvaffaqiyatli to'lovlar mavjud emas)</i>\n"
    return text


def history_text(uid: int) -> str:
    rows = get_all_topups_history(uid)
    if not rows:
        return "📋 Sizda hali buyurtmalar tarixi mavjud emas."
    text = "📋 <b>So'nggi buyurtmalar holati:</b>\n\n"
    for tid, amt, status, c_at in rows:
        icon = {"paid": "✅", "pending": "⏳", "expired": "❌", "cancelled": "🚫"}.get(status, "❓")
        status_name = {
            "paid": "To'langan",
            "pending": "Kutilmoqda",
            "expired": "Muddati o'tgan",
            "cancelled": "Bekor qilingan",
        }.get(status, status)
        t_str = time.strftime("%d.%m %H:%M", time.localtime(c_at))
        text += f"{icon} <b>#{tid}</b> | <b>{fmt(amt)} so'm</b> | {status_name} <i>({t_str})</i>\n"
    return text


# ── Bot handlerlari ─────────────────────────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    waiting_remove(m.from_user.id)
    bot.send_message(
        m.chat.id,
        menu_text(m.from_user.id, m.from_user.full_name),
        parse_mode="HTML",
        reply_markup=reply_main_kb(),
    )
    bot.send_message(
        m.chat.id,
        "Asosiy menyu:",
        reply_markup=menu_kb(),
    )


@bot.message_handler(commands=["balance"])
def cmd_balance(m):
    waiting_remove(m.from_user.id)
    bot.send_message(
        m.chat.id,
        balance_text(m.from_user.id, m.from_user.full_name),
        parse_mode="HTML",
        reply_markup=balance_kb(),
    )


# Reply keyboard tugmalari:
@bot.message_handler(func=lambda m: m.text and "balans" in m.text.lower())
def text_balance(m):
    waiting_remove(m.from_user.id)
    bot.send_message(
        m.chat.id,
        balance_text(m.from_user.id, m.from_user.full_name),
        parse_mode="HTML",
        reply_markup=balance_kb(),
    )


@bot.message_handler(func=lambda m: m.text and "tarix" in m.text.lower())
def text_history(m):
    waiting_remove(m.from_user.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"))
    bot.send_message(
        m.chat.id,
        history_text(m.from_user.id),
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: m.text and "to'ldirish" in m.text.lower())
def text_topup(m):
    waiting_add(m.from_user.id)
    bot.send_message(
        m.chat.id,
        f"💵 To'ldirish summasini kiriting (so'mda):\n"
        f"Min: {fmt(MIN_AMOUNT)}  |  Max: {fmt(MAX_AMOUNT)}",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


@bot.message_handler(commands=["credit"])
def cmd_credit(m):
    """Admin: /credit <user_id> <summa>"""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.send_message(m.chat.id, "Foydalanish: /credit <user_id> <summa>")
        return
    uid, amount = int(parts[1]), int(parts[2])
    bal = admin_credit(uid, amount)
    bot.send_message(
        m.chat.id,
        f"Bajarildi! User {uid} ga +{fmt(amount)} so'm.\nBalans: {fmt(bal)} so'm",
    )
    try:
        bot.send_message(
            uid,
            f"✅ <b>To'lov qabul qilindi!</b>\n\n"
            f"➕ Qo'shildi: <b>+{fmt(amount)} so'm</b>\n"
            f"💰 Hisobingiz: <b>{fmt(bal)} so'm</b>",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "menu")
def cb_menu(c):
    waiting_remove(c.from_user.id)
    try:
        bot.edit_message_text(
            menu_text(c.from_user.id, c.from_user.full_name),
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "balance")
def cb_balance(c):
    waiting_remove(c.from_user.id)
    try:
        bot.edit_message_text(
            balance_text(c.from_user.id, c.from_user.full_name),
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=balance_kb(),
        )
    except Exception:
        pass
    bot.answer_callback_query(c.id, f"Hisobingiz: {fmt(get_balance(c.from_user.id))} so'm")


@bot.callback_query_handler(func=lambda c: c.data == "history")
def cb_history(c):
    waiting_remove(c.from_user.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="menu"))
    try:
        bot.edit_message_text(
            history_text(c.from_user.id),
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "topup")
def cb_topup(c):
    waiting_add(c.from_user.id)
    try:
        bot.edit_message_text(
            f"💵 To'ldirish summasini kiriting (so'mda):\n"
            f"Min: {fmt(MIN_AMOUNT)}  |  Max: {fmt(MAX_AMOUNT)}",
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["text"], func=lambda m: waiting_check(m.from_user.id))
def got_amount(m):
    txt = (m.text or "").replace(" ", "").replace(",", "").replace(".", "")
    if not txt.isdigit() or not (MIN_AMOUNT <= int(txt) <= MAX_AMOUNT):
        bot.send_message(
            m.chat.id,
            f"❌ Faqat {fmt(MIN_AMOUNT)} dan {fmt(MAX_AMOUNT)} gacha butun son kiriting:",
        )
        return

    waiting_remove(m.from_user.id)
    tid, amount = create_topup(m.from_user.id, int(txt))

    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        kb.add(types.InlineKeyboardButton(
            "📋 Kartani nusxalash",
            copy_text=types.CopyTextButton(text=CARD_NUMBER.replace(" ", "")),
        ))
    except AttributeError:
        pass
    kb.add(types.InlineKeyboardButton("🗑 Bekor qilish", callback_data=f"cancel:{tid}"))
    kb.add(types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"))

    bot.send_message(
        m.chat.id,
        f"💳 To'ldirish <b>#{tid}</b>\n\n"
        f"Karta: <code>{CARD_NUMBER}</code>"
        + (f" | {CARD_OWNER}" if CARD_OWNER else "") + "\n"
        f"Summa: <b>{fmt(amount)} so'm</b>\n\n"
        f"⏱ To'lov <b>{ORDER_TTL // 60} daqiqa</b> amal qiladi.\n"
        f"⚠️ Aynan <b>{fmt(amount)} so'm</b> o'tkazing!",
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(c):
    tid = int(c.data.split(":")[1])
    if cancel_topup(tid, c.from_user.id):
        try:
            bot.edit_message_text(
                f"🗑 To'ldirish #{tid} bekor qilindi.",
                c.message.chat.id,
                c.message.message_id,
                reply_markup=menu_kb(),
            )
        except Exception:
            pass
        bot.answer_callback_query(c.id)
    else:
        bot.answer_callback_query(
            c.id, "Bekor qilib bo'lmaydi (to'langan yoki muddati o'tgan).", show_alert=True
        )


# ══════════════════════════════════════════ HUMO LISTENER ══════════════
def process_humo(msg_id: int, text: str, ts: float):
    humo_log.debug("=== msg_id=%s ts=%.0f ===\n%s\n%s", msg_id, ts, text, "=" * 40)
    log.info("HUMO xabar keldi: msg_id=%s | %s", msg_id, text[:50].replace("\n", " "))

    if not mark_seen(msg_id):
        log.info("Xabar #%s allaqachon ko'rilgan.", msg_id)
        return

    amount = parse_income(text)
    if amount is None:
        log.warning("msg_id=%s: summa topilmadi. Xabar: %r", msg_id, text)
        return

    log.info("HUMO kirim aniqlandi: %s so'm", amount)
    res = claim_and_credit(amount, ts)
    if not res:
        log.warning("Mos buyurtma topilmadi: %s so'm!", amount)
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"DIQQAT! {fmt(amount)} so'm tushdi lekin buyurtma topilmadi.\n"
                    f"Qo'lda kreditlash: /credit <user_id> {amount}",
                )
            except Exception:
                pass
        return

    uid, balance = res
    log.info("To'lov muvaffaqiyatli: user=%s +%s so'm yangi balans=%s", uid, amount, balance)
    try:
        bot.send_message(
            uid,
            f"🎉 <b>To'lov qabul qilindi!</b>\n\n"
            f"➕ Qo'shildi: <b>+{fmt(amount)} so'm</b>\n"
            f"💰 Yangi hisobingiz: <b>{fmt(balance)} so'm</b>",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
    except Exception:
        log.exception("Foydalanuvchiga xabar yuborib bo'lmadi.")


@client.on(events.NewMessage(incoming=True))
async def on_message_received(event):
    is_humo = False
    if humo_entity and (event.chat_id == humo_entity.id or event.sender_id == humo_entity.id):
        is_humo = True
    else:
        try:
            chat = await event.get_chat()
            u = getattr(chat, "username", "") or ""
            if u.lower() == HUMO_CHAT.lower():
                is_humo = True
        except Exception:
            pass

    if is_humo:
        await asyncio.to_thread(
            process_humo, event.id, event.raw_text or "", event.date.timestamp()
        )


async def catch_up():
    global humo_entity
    try:
        humo_entity = await client.get_entity(HUMO_CHAT)
        log.info("HUMO bot ulandi: @%s (ID: %s)", getattr(humo_entity, 'username', ''), humo_entity.id)
    except Exception as e:
        log.error("HUMO bot entitysini yuklashda xatolik: %s", e)

    try:
        cutoff = time.time() - 86400  # so'nggi 24 soat
        target = humo_entity if humo_entity else HUMO_CHAT
        msgs = [m async for m in client.iter_messages(target, limit=30)]
        done = 0
        for m in reversed(msgs):
            if m.date.timestamp() < cutoff:
                continue
            await asyncio.to_thread(process_humo, m.id, m.raw_text or "", m.date.timestamp())
            done += 1
        log.info("catch_up: %s ta xabar tekshirildi.", done)
    except Exception:
        log.exception("catch_up xatosi")


# ══════════════════════════════════════════ EXPIRE LOOP ════════════════
def expire_loop():
    while True:
        try:
            rows = get_expired_topups()
            for tid, uid in rows:
                log.info("So'rov #%s muddati o'tdi (user=%s)", tid, uid)
                try:
                    bot.send_message(
                        uid,
                        f"⏰ To'ldirish <b>#{tid}</b> muddati tugadi.\n"
                        "Pul o'tkazgan bo'lsangiz, adminga murojaat qiling.",
                        parse_mode="HTML",
                        reply_markup=menu_kb(),
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("expire_loop xato")
        time.sleep(20)


# ══════════════════════════════════════════ MAIN ═══════════════════════
async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("\n" + "=" * 60)
        print("DIQQAT! Telethon ga SHAXSIY TELEGRAM RAQAMINGIZNI kiritishingiz kerak!")
        print("Masalan: +998901234567")
        print("Bot token kiritmang! Bot ikkinchi botning xabarlarini ko'ra olmaydi!")
        print("=" * 60 + "\n")
        await client.start()

    me = await client.get_me()
    if me.bot:
        print("\n" + "!" * 60)
        print("XATOLIK: Siz bot token orqali kirgansiz (@" + str(me.username) + ")!")
        print("Telegramda bitta bot ikkinchi botning (@HUMOcardbot) xabarlarini o'qiy OLMAYDI!")
        print("Iltimos, humo_session.session faylini o'chirib, o'zingizning shaxsiy")
        print("telefon raqamingiz (+998...) orqali kiring!")
        print("!" * 60 + "\n")
        return

    log.info("Telethon userbot ulandi: %s (ID: %s, Telefon: %s)", me.first_name, me.id, me.phone)
    await catch_up()

    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True),
        daemon=True, name="telebot",
    ).start()
    threading.Thread(target=expire_loop, daemon=True, name="expire").start()
    log.info("Bot ishga tushdi va HUMO xabarlarini tinglayapti...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
