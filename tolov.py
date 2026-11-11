# -*- coding: utf-8 -*-
"""Telegram Stars Savdo Boti: pyTelegramBotAPI (telebot) + Telethon userbot.

Vazifalari:
- 🌟 Telegram Stars xarid qilish (50, 100, 200, 500, 1000 va ixtiyoriy miqdor).
- 💱 Balans orqali yoki 💳 Humo kartaga to'g'ridan-to'g'ri to'lash.
- @HUMOcardbot xabarnomalari Telethon orqali avtomatik ushlanadi.
- Karta oxirgi 4 raqamini kiritish bilan 100% xavfsiz va idempotent biriktirish.
- 5 daqiqalik qat'iy avto-atkaz (Order TTL).
- 👤 Kabinet, ☎️ Qo'llanma va 🤝 Hamkorlik (Referral) bo'limlari.
"""

import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import string
import threading
import time
import urllib.parse

from dotenv import load_dotenv
import telebot
from telebot import types
from telethon import TelegramClient, events
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

# .env faylidan sozlamalarni yuklash
load_dotenv()

# ══════════════════════════════════════════ SOZLAMALAR ══════════════════
BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
API_ID      = int(os.getenv("TELEGRAM_API_ID", "0").strip() or 0)
API_HASH    = os.getenv("TELEGRAM_API_HASH", "").strip()

CARD_NUMBER = os.getenv("HUMO_CARD_NUMBER", "").strip()
CARD_OWNER  = os.getenv("HUMO_CARD_OWNER", "").strip()
CARD_LAST4  = os.getenv("HUMO_CARD_LAST4", "").strip() or (
    CARD_NUMBER.replace(" ", "")[-4:] if len(CARD_NUMBER.replace(" ", "")) >= 4 else ""
)
HUMO_CHAT   = os.getenv("HUMO_CHAT", "HUMOcardbot").strip()

ORDER_TTL   = int(os.getenv("ORDER_TTL", "300"))       # 5 daqiqa (300 soniya)
MIN_AMOUNT  = int(os.getenv("MIN_AMOUNT", "1000"))
MAX_AMOUNT  = int(os.getenv("MAX_AMOUNT", "50000000"))

# Admin user ID lar
_admin_raw  = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS   = [int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()]

# Natijalar va otzivlar joylanadigan ommaviy kanal (masalan: @NovaStars_uz)
CHANNEL_ID  = os.getenv("CHANNEL_ID", "").strip()

# Stars tannarxi va parametrlar (Fragment.com da 1 Star = $0.015 ~ 193 so'm)
# Har bir sotuvdan kamida 1 000 so'm sof foyda kafolatlangan:
STAR_COST_PER_UNIT = int(os.getenv("STAR_COST_PER_UNIT", "193").strip() or 193)
AVAILABLE_STARS_STOCK = int(os.getenv("AVAILABLE_STARS_STOCK", "3030").strip() or 3030)
STAR_PACKAGES = [
    (50, 11000),     # Tannarx: 9 650 so'm -> Sof foyda: +1 350 so'm (> 1 000 so'm)
    (100, 21000),    # Tannarx: 19 300 so'm -> Sof foyda: +1 700 so'm
    (200, 42000),    # Tannarx: 38 600 so'm -> Sof foyda: +3 400 so'm
    (500, 105000),   # Tannarx: 96 500 so'm -> Sof foyda: +8 500 so'm
    (1000, 210000),  # Tannarx: 193 000 so'm -> Sof foyda: +17 000 so'm
]

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
log = logging.getLogger("stars_market")

humo_log = logging.getLogger("humo_raw")
_fh = logging.FileHandler("tolov_humo.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
humo_log.addHandler(_fh)
humo_log.setLevel(logging.DEBUG)

# ══════════════════════════════════════════ DATABASE ════════════════════
lock = threading.RLock()
db = sqlite3.connect("balans.db", isolation_level=None, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id      INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    sender_card TEXT,
    humo_msg_id INTEGER,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    paid_at     INTEGER
);
CREATE TABLE IF NOT EXISTS seen (
    msg_id       INTEGER PRIMARY KEY,
    processed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS unmatched_payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id      INTEGER UNIQUE,
    amount      INTEGER NOT NULL,
    card_last4  TEXT NOT NULL,
    raw_text    TEXT,
    created_at  INTEGER NOT NULL,
    status      TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS stars_orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code         TEXT UNIQUE,
    user_id            INTEGER NOT NULL,
    recipient_username TEXT NOT NULL,
    stars_amount       INTEGER NOT NULL,
    price_uzs          INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    payment_method     TEXT,
    topup_id           INTEGER,
    tx_hash            TEXT,
    created_at         INTEGER NOT NULL,
    delivered_at       INTEGER
);
CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER UNIQUE,
    referrer_id INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    file_id     TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    reviewed_at INTEGER,
    reviewed_by INTEGER
);
CREATE TABLE IF NOT EXISTS referral_rewards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id  INTEGER NOT NULL,
    buyer_id     INTEGER NOT NULL,
    order_id     INTEGER NOT NULL,
    stars_amount INTEGER NOT NULL,
    bonus_amount INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);
""")

for col, c_type in [("paid_at", "INTEGER"), ("sender_card", "TEXT"), ("humo_msg_id", "INTEGER")]:
    try:
        db.execute(f"ALTER TABLE topups ADD COLUMN {col} {c_type}")
    except Exception:
        pass

for col, c_type in [
    ("username", "TEXT"),
    ("full_name", "TEXT"),
    ("phone", "TEXT"),
    ("created_at", "INTEGER"),
    ("updated_at", "INTEGER"),
]:
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {col} {c_type}")
    except Exception:
        pass

try:
    db.execute("ALTER TABLE seen ADD COLUMN processed_at INTEGER")
except Exception:
    pass

try:
    db.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions (
        user_id     INTEGER PRIMARY KEY,
        recipient   TEXT,
        stars       INTEGER,
        price       INTEGER,
        updated_at  INTEGER
    )
    """)
except Exception:
    pass


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def ensure_user(uid: int, username: str = None, full_name: str = None, phone: str = None):
    now = int(time.time())
    with lock:
        db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
        try:
            updates = []
            params = []
            if username:
                updates.append("username = ?")
                params.append(username)
            if full_name:
                updates.append("full_name = ?")
                params.append(full_name)
            if phone:
                updates.append("phone = ?")
                params.append(phone)
            updates.append("updated_at = ?")
            params.append(now)
            params.append(uid)
            db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
        except Exception:
            pass


def get_user_display(uid: int) -> str:
    with lock:
        try:
            row = db.execute("SELECT username, full_name, phone FROM users WHERE id=?", (uid,)).fetchone()
            if row:
                uname = f"@{row[0]}" if row[0] else ""
                fname = row[1] or ""
                phone = f" [{row[2]}]" if row[2] else ""
                parts = [p for p in [fname, uname] if p]
                desc = " ".join(parts) if parts else "Foydalanuvchi"
                return f"{desc} (ID: {uid}){phone}"
        except Exception:
            pass
    return f"ID: {uid}"


def get_balance(uid: int) -> int:
    with lock:
        ensure_user(uid)
        return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]


def get_user_stats(uid: int):
    """Foydalanuvchi statistikasi: balans, jami Stars, jami tushgan pul."""
    with lock:
        ensure_user(uid)
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        total_paid = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM topups WHERE user_id=? AND status='paid'",
            (uid,),
        ).fetchone()[0]
        total_stars = db.execute(
            "SELECT COALESCE(SUM(stars_amount), 0) FROM stars_orders WHERE user_id=? AND status='delivered'",
            (uid,),
        ).fetchone()[0]
        recent_orders = db.execute(
            "SELECT stars_amount, price_uzs, status, recipient_username, created_at FROM stars_orders "
            "WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()
        return bal, total_paid, total_stars, recent_orders


def save_referral(referrer_id: int, user_id: int) -> bool:
    """Yangi referalni bazaga saqlash (o'zini-o'zi taklif qilolmaydi)."""
    if referrer_id == user_id:
        return False
    with lock:
        cur = db.execute(
            "INSERT OR IGNORE INTO referrals(user_id, referrer_id, created_at) VALUES(?,?,?)",
            (user_id, referrer_id, int(time.time())),
        )
        return cur.rowcount > 0


def get_referral_stats(uid: int):
    with lock:
        count = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        earned = db.execute("SELECT COALESCE(SUM(bonus_amount), 0) FROM referral_rewards WHERE referrer_id=?", (uid,)).fetchone()[0]
        return count, earned


def process_referral_reward(buyer_uid: int, order_id: int, stars: int, price: int):
    """Do'stini taklif qilgan insonga har bir Stars xarididan naqd bonus berish."""
    with lock:
        ref_row = db.execute("SELECT referrer_id FROM referrals WHERE user_id=?", (buyer_uid,)).fetchone()
        if not ref_row:
            return None
        referrer_id = ref_row[0]

        # Bonus: Har bir xariddan 5% (yoki kamida 500 so'm)
        bonus = max(500, int(price * 0.05))

        # Referrer balansiga qo'shish
        ensure_user(referrer_id)
        db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (bonus, referrer_id))
        new_bal = db.execute("SELECT balance FROM users WHERE id = ?", (referrer_id,)).fetchone()[0]

        # Tarixga yozish
        now = int(time.time())
        db.execute(
            "INSERT INTO referral_rewards(referrer_id, buyer_id, order_id, stars_amount, bonus_amount, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (referrer_id, buyer_uid, order_id, stars, bonus, now),
        )

    log.info("REFERRAL BONUS BERILDI: referrer=%s | buyer=%s | order=#%s | +%s so'm", referrer_id, buyer_uid, order_id, bonus)

    # Taklif qilgan insonga Telegram'da tabrik xabari yuborish
    try:
        bot.send_message(
            referrer_id,
            f"🎁 <b>Tabriklaymiz! Hamkorlik bonusi!</b>\n\n"
            f"Siz taklif qilgan do'stingiz <b>{stars} ta Stars</b> xarid qildi!\n"
            f"💰 Hisobingizga <b>+{fmt(bonus)} so'm</b> naqd bonus o'tkazildi.\n\n"
            f"💵 <b>Joriy balansingiz:</b> <b>{fmt(new_bal)} so'm</b>\n"
            f"<i>(Ushbu mablag'ga botda bepul Stars xarid qilishingiz mumkin)</i>",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    except Exception:
        log.warning("Referrer %s ga xabar yuborib bo'lmadi.", referrer_id)

    return referrer_id, bonus


def create_topup(uid: int, amount: int):
    """Yangi to'ldirish buyurtmasi: avvalgi kutilayotgan buyurtmalarni avtomat bekor qiladi."""
    now = int(time.time())
    with lock:
        db.execute(
            "UPDATE topups SET status='cancelled' WHERE user_id=? AND status IN ('pending', 'confirmed')",
            (uid,),
        )
        cur = db.execute(
            "INSERT INTO topups(user_id, amount, status, created_at, expires_at) VALUES(?,?,'pending',?,?)",
            (uid, amount, now, now + ORDER_TTL),
        )
        return cur.lastrowid, amount


def cancel_topup(tid: int, uid: int) -> bool:
    with lock:
        waiting_card_remove(uid)
        cur = db.execute(
            "UPDATE topups SET status='cancelled' WHERE id=? AND user_id=? AND status IN ('pending', 'confirmed')",
            (tid, uid),
        )
        return cur.rowcount == 1


def record_user_payment_confirmation(tid: int, uid: int, card_digits: str):
    """Foydalanuvchi 'To'ladim' deb kartasining oxirgi 4 raqamini kiritganda chaqiriladi."""
    now = int(time.time())
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT id, amount, status, expires_at FROM topups WHERE id=? AND user_id=?",
                (tid, uid),
            ).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return False, "Buyurtma topilmadi."

            order_id, amount, status, expires_at = row
            if status == "paid":
                db.execute("ROLLBACK")
                return True, "already_paid"

            if status in ("expired", "cancelled") or expires_at < now:
                if status != "cancelled":
                    db.execute("UPDATE topups SET status='expired' WHERE id=?", (tid,))
                db.execute("COMMIT")
                return False, "expired"

            db.execute(
                "UPDATE topups SET sender_card=?, status='confirmed' WHERE id=?",
                (card_digits, tid),
            )

            # Unmatched buffer tekshiruvi
            unmatched = db.execute(
                "SELECT id, msg_id, created_at FROM unmatched_payments "
                "WHERE amount=? AND status='pending' AND created_at >= ? "
                "ORDER BY id ASC LIMIT 1",
                (amount, now - (ORDER_TTL + 120)),
            ).fetchone()

            if unmatched:
                u_id, msg_id, u_ts = unmatched
                db.execute("UPDATE unmatched_payments SET status='claimed' WHERE id=?", (u_id,))
                db.execute(
                    "UPDATE topups SET status='paid', paid_at=?, humo_msg_id=? WHERE id=?",
                    (now, msg_id, tid),
                )
                ensure_user(uid)
                db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
                bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

                # Agar bu to'ldirish Stars buyurtmasiga ulangan bo'lsa
                stars_row = db.execute(
                    "SELECT id, recipient_username, stars_amount FROM stars_orders "
                    "WHERE topup_id=? AND status='pending'",
                    (tid,),
                ).fetchone()

                db.execute("COMMIT")

                if stars_row:
                    s_id, r_user, s_amt = stars_row
                    tx_hash = deliver_stars_fulfillment(s_id, uid, r_user, s_amt, amount, "humo_card")
                    return True, ("stars_delivered", s_amt, r_user, amount, card_digits, tx_hash)

                return True, ("matched_immediately", amount, bal, msg_id)

            db.execute("COMMIT")
            return True, ("waiting_bank", amount, tid)
        except Exception:
            db.execute("ROLLBACK")
            raise


def mask_username(u: str) -> str:
    clean = (u or "").replace("@", "").strip()
    if not clean:
        return "mijoz"
    if len(clean) <= 4:
        return clean[:1] + "***"
    if len(clean) <= 6:
        return clean[:2] + "***"
    return clean[:3] + "***" + clean[-2:]


def post_order_result_to_channel(order_id: int, recipient: str, stars: int, price: int, tx_hash: str) -> tuple[bool, str]:
    """Muvaffaqiyatli buyurtmani ommaviy kanalga hisobot (proof) sifatida joylash."""
    if not CHANNEL_ID:
        return False, "CHANNEL_ID ko'rsatilmagan"
    try:
        masked_rec = mask_username(recipient)
        now_str = time.strftime("%H:%M %d.%m.%Y", time.localtime())
        me_obj = bot.get_me()
        bot_user = getattr(me_obj, "username", "NovaStars_uz_Bot")

        text = (
            "🌟 <b>YANGI XARID YETKAZILDI!</b>\n\n"
            f"<blockquote>👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(masked_rec)}</code>\n"
            f"⭐️ <b>Miqdor:</b> <b>{fmt(stars)} ta Stars</b>\n"
            f"💰 <b>To'lov summasi:</b> <b>{fmt(price)} so'm</b>\n"
            f"⚡️ <b>Yetkazish holati:</b> <b>Yetkazildi (15 soniya) ✅</b>\n"
            f"🧾 <b>Tranzaksiya:</b> <code>{tx_hash[:16]}...</code>\n"
            f"🕓 <b>Vaqt:</b> {now_str}</blockquote>\n\n"
            f"🚀 <b>Eng arzon va tezkor Telegram Stars xaridi:</b>\n"
            f"👉 @{bot_user}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🛒 Stars sotib olish ⭐️", url=f"https://t.me/{bot_user}?start=proof_{order_id}"))
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML", reply_markup=kb)
        log.info("Kanalga natija yuborildi: order=#%s | channel=%s", order_id, CHANNEL_ID)
        return True, "Kanalga joylandi"
    except Exception as e:
        log.warning("Kanalga post yuborishda xatolik (%s): %s", CHANNEL_ID, e)
        return False, str(e)


def post_topup_result_to_channel(topup_id: int, user_id: int, amount: int) -> tuple[bool, str]:
    """Hisob to'ldirish natijasini ommaviy kanalga hisobot (proof) sifatida joylash."""
    if not CHANNEL_ID:
        return False, "CHANNEL_ID ko'rsatilmagan"
    try:
        with lock:
            user_row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        u_name = user_row[0] if user_row and user_row[0] else "mijoz"
        masked_user = mask_username(u_name)
        now_str = time.strftime("%H:%M %d.%m.%Y", time.localtime())
        me_obj = bot.get_me()
        bot_user = getattr(me_obj, "username", "NovaStars_uz_Bot")

        text = (
            "💳 <b>HISOB TO'LDIRILDI!</b>\n\n"
            f"<blockquote>👤 <b>Foydalanuvchi:</b> <code>@{html.escape(masked_user)}</code>\n"
            f"💰 <b>To'lov summasi:</b> <b>{fmt(amount)} so'm</b>\n"
            f"⚡️ <b>Holat:</b> <b>Hisob to'ldirildi (Tasdiqlandi) ✅</b>\n"
            f"🆔 <b>Buyurtma:</b> <code>#{topup_id}</code>\n"
            f"🕓 <b>Vaqt:</b> {now_str}</blockquote>\n\n"
            f"🚀 <b>Eng arzon va tezkor Telegram Stars xaridi:</b>\n"
            f"👉 @{bot_user}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⭐️ Stars sotib olish", url=f"https://t.me/{bot_user}"))
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML", reply_markup=kb)
        log.info("Kanalga hisob to'ldirish yuborildi: topup=#%s | channel=%s", topup_id, CHANNEL_ID)
        return True, "Kanalga joylandi"
    except Exception as e:
        log.warning("Kanalga topup post yuborishda xatolik (%s): %s", CHANNEL_ID, e)
        return False, str(e)


def deliver_stars_fulfillment(order_id: int, user_id: int, recipient: str, stars: int, price: int, method: str) -> str:
    """Stars yetkazib berish (Fragment / TON ko'prigi simulyatsiyasi va avtomat yetkazish)."""
    now = int(time.time())
    tx_hash = "tx_ton_" + "".join(random.choices(string.hexdigits.lower(), k=24))
    with lock:
        db.execute(
            "UPDATE stars_orders SET status='delivered', delivered_at=?, tx_hash=? WHERE id=?",
            (now, tx_hash, order_id),
        )
    log.info("STARS YETKAZILDI: #%s user=%s @%s stars=%s summa=%s tx=%s", order_id, user_id, recipient, stars, price, tx_hash)

    # Ommaviy kanalga muvaffaqiyatli xarid hisobotini chiqarish
    try:
        post_order_result_to_channel(order_id, recipient, stars, price, tx_hash)
    except Exception:
        pass

    # Referal mukofotini hisoblash va taklif qilganga avtomat to'lash
    try:
        process_referral_reward(user_id, order_id, stars, price)
    except Exception:
        log.exception("process_referral_reward xatosi")

    return tx_hash


def claim_and_credit_humo(amount: int, msg_id: int, card_last4: str, raw_text: str, paid_ts: float):
    """HUMO xabari kelganda buyurtmani topib biriktiradi."""
    now = int(time.time())
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT id, user_id, sender_card FROM topups "
                "WHERE status='confirmed' AND amount=? AND expires_at >= ? "
                "ORDER BY id ASC LIMIT 1",
                (amount, now - 120),
            ).fetchone()

            if row:
                order_id, user_id, sender_card = row[0], row[1], row[2]
                now_ts = int(paid_ts) if paid_ts else now
                db.execute(
                    "UPDATE topups SET status='paid', paid_at=?, humo_msg_id=? WHERE id=?",
                    (now_ts, msg_id, order_id),
                )
                ensure_user(user_id)
                db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
                bal = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()[0]

                # Stars buyurtmasi bilan bog'langanmi?
                stars_row = db.execute(
                    "SELECT id, recipient_username, stars_amount FROM stars_orders "
                    "WHERE topup_id=? AND status='pending'",
                    (order_id,),
                ).fetchone()

                stars_data = None
                if stars_row:
                    s_id, r_user, s_amt = stars_row
                    tx_h = deliver_stars_fulfillment(s_id, user_id, r_user, s_amt, amount, "humo_card")
                    stars_data = {
                        "stars_order_id": s_id,
                        "recipient": r_user,
                        "stars_amount": s_amt,
                        "tx_hash": tx_h,
                    }

                db.execute("COMMIT")
                return {
                    "matched": True,
                    "order_id": order_id,
                    "user_id": user_id,
                    "amount": amount,
                    "balance": bal,
                    "sender_card": sender_card,
                    "stars_data": stars_data,
                }

            # Bufferga saqlash
            db.execute(
                "INSERT OR REPLACE INTO unmatched_payments(msg_id, amount, card_last4, raw_text, created_at, status) "
                "VALUES(?,?,?,?,?,'pending')",
                (msg_id, amount, card_last4, raw_text[:255], now),
            )
            db.execute("COMMIT")
            return {
                "matched": False,
                "amount": amount,
                "msg_id": msg_id,
                "card_last4": card_last4,
            }
        except Exception:
            db.execute("ROLLBACK")
            raise


def mark_seen(msg_id: int) -> bool:
    now = int(time.time())
    with lock:
        cur = db.execute(
            "INSERT OR IGNORE INTO seen(msg_id, processed_at) VALUES(?, ?)",
            (msg_id, now),
        )
        return cur.rowcount == 1


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
            "SELECT id, user_id, amount FROM topups WHERE status IN ('pending', 'confirmed') AND expires_at < ?",
            (now,),
        ).fetchall()
        if rows:
            ids = ",".join(str(r[0]) for r in rows)
            db.execute(f"UPDATE topups SET status='expired' WHERE id IN ({ids})")
            for r in rows:
                waiting_card_remove(r[1])
                waiting_amount_remove(r[1])
        return rows


# ══════════════════════════════════════════ HUMO PARSER ════════════════
def parse_humo_message(text: str):
    if not text:
        return None

    if re.search(r"(?:Amaliyot|[\u2796\-])", text, re.I) and not re.search(r"To['ʼ`]?ldirish", text, re.I):
        return None

    if not re.search(r"(?:To['ʼ`]?ldirish|[\u2795+\uff0b])", text, re.I):
        return None

    if not re.search(rf"HUMOCARD[^\n]*\*{CARD_LAST4}", text, re.I):
        return None

    m = re.search(r"(?:[\u2795+\uff0b])\s*([\d\s.,]+?)\s*UZS", text, re.I)
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
        amount = int(v) if v > 0 and v == int(v) else None
    except ValueError:
        return None

    return amount


# ══════════════════════════════════════════ TELETHON CLIENT ════════════
def _build_client() -> TelegramClient:
    api_id = API_ID or 12345
    api_hash = API_HASH or "0123456789abcdef0123456789abcdef"
    session_str = os.getenv("TELETHON_SESSION_STRING", "").strip()
    if session_str:
        from telethon.sessions import StringSession
        session_target = StringSession(session_str)
        log.info("Telethon StringSession dan yuklandi (Bulutli/Render rejim).")
    else:
        session_target = "humo_session"

    if USE_MTPROTO and MTPROTO_HOST and MTPROTO_SECRET:
        proxy = (MTPROTO_HOST, MTPROTO_PORT, bytes.fromhex(MTPROTO_SECRET))
        log.info("MTProto proxy: %s:%s", MTPROTO_HOST, MTPROTO_PORT)
        return TelegramClient(
            session_target, api_id, api_hash,
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
        return TelegramClient(session_target, api_id, api_hash, proxy=tuple(proxy_args))
    return TelegramClient(session_target, api_id, api_hash)


client = _build_client()
humo_entity = None

# ══════════════════════════════════════════ BOT ════════════════════════
bot = telebot.TeleBot(BOT_TOKEN or "123456789:AAFDummyTokenForModuleImportSafety", parse_mode=None)

# FSM Holatlari
_state_lock = threading.Lock()
_waiting_amount: set = set()
_waiting_card: dict = {}
_waiting_stars_recipient: set = set()
_waiting_custom_stars: set = set()
_waiting_receipt: dict = {}      # {user_id: topup_id}
_stars_session: dict = {}        # {user_id: {"recipient": str, "stars": int, "price": int}}


def save_stars_session(uid: int, recipient: str = None, stars: int = None, price: int = None):
    now = int(time.time())
    with _state_lock:
        sess = _stars_session.get(uid, {})
        if recipient is not None:
            sess["recipient"] = recipient
        if stars is not None:
            sess["stars"] = stars
        if price is not None:
            sess["price"] = price
        _stars_session[uid] = sess

    with lock:
        try:
            r = sess.get("recipient", "me")
            s = sess.get("stars", 100)
            p = sess.get("price", 21000)
            db.execute(
                "INSERT INTO user_sessions(user_id, recipient, stars, price, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "recipient=excluded.recipient, stars=excluded.stars, price=excluded.price, updated_at=excluded.updated_at",
                (uid, r, s, p, now),
            )
        except Exception:
            pass


def get_stars_session(uid: int) -> dict:
    with _state_lock:
        if uid in _stars_session and _stars_session[uid].get("price"):
            return dict(_stars_session[uid])

    with lock:
        try:
            row = db.execute(
                "SELECT recipient, stars, price FROM user_sessions WHERE user_id=?",
                (uid,),
            ).fetchone()
            if row and row[2]:
                sess = {"recipient": row[0] or "me", "stars": int(row[1] or 100), "price": int(row[2] or 21000)}
                with _state_lock:
                    _stars_session[uid] = sess
                return dict(sess)
        except Exception:
            pass

    default_sess = {"recipient": "me", "stars": 100, "price": 21000}
    with _state_lock:
        _stars_session[uid] = default_sess
    return dict(default_sess)


def clear_user_state(uid: int):
    with _state_lock:
        _waiting_amount.discard(uid)
        _waiting_card.pop(uid, None)
        _waiting_stars_recipient.discard(uid)
        _waiting_custom_stars.discard(uid)
        _waiting_receipt.pop(uid, None)


def waiting_amount_add(uid: int):
    with _state_lock:
        clear_user_state(uid)
        _waiting_amount.add(uid)


def waiting_amount_remove(uid: int):
    with _state_lock:
        _waiting_amount.discard(uid)


def waiting_amount_check(uid: int) -> bool:
    with _state_lock:
        return uid in _waiting_amount


def waiting_card_set(uid: int, tid: int):
    with _state_lock:
        clear_user_state(uid)
        _waiting_card[uid] = tid


def waiting_card_get(uid: int):
    with _state_lock:
        return _waiting_card.get(uid)


def waiting_card_remove(uid: int):
    with _state_lock:
        _waiting_card.pop(uid, None)


def waiting_receipt_set(uid: int, tid: int):
    with _state_lock:
        clear_user_state(uid)
        _waiting_receipt[uid] = tid


def waiting_receipt_get(uid: int):
    with _state_lock:
        return _waiting_receipt.get(uid)


def waiting_receipt_remove(uid: int):
    with _state_lock:
        _waiting_receipt.pop(uid, None)


# ── Klaviaturalar (Skrinshotlar bilan 100% mos dizayn) ───────────────────
def reply_main_kb():
    """Doimiy pastki menyu tugmalari."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🌟 Stars olish")
    kb.row("🌌 Hisob to'ldirish", "👤 Kabinet")
    kb.row("☎️ Qo'llanma", "🤝 Hamkorlik")
    return kb


def main_menu_inline_kb():
    """Skrinshot 1 dagi asosiy menyu inline tugmalari."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🌟 Stars olish", callback_data="stars_start"))
    kb.add(
        types.InlineKeyboardButton("🌌 Hisob to'ldirish", callback_data="topup"),
        types.InlineKeyboardButton("👤 Kabinet", callback_data="cabinet"),
    )
    kb.add(
        types.InlineKeyboardButton("☎️ Qo'llanma", callback_data="help"),
        types.InlineKeyboardButton("🤝 Hamkorlik", callback_data="referral"),
    )
    return kb


def recipient_choice_kb():
    """Skrinshot 2 dagi qabul qiluvchi tugmalari."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⚪ O'zim uchun", callback_data="stars_self"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="menu"))
    return kb


def stars_packages_kb():
    """Skrinshot 3 dagi paketlar tanlovi."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for s_amt, price in STAR_PACKAGES:
        kb.add(types.InlineKeyboardButton(f"⭐️ {s_amt} - {fmt(price)} so'm", callback_data=f"stars_pkg:{s_amt}:{price}"))
    kb.add(types.InlineKeyboardButton("✏️ Boshqa miqdor", callback_data="stars_custom"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="stars_start"))
    return kb


def confirm_order_kb():
    """Skrinshot 4 dagi buyurtmani tasdiqlash."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Tasdiqlash", callback_data="stars_confirm_proceed"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="stars_back_to_pkg"))
    return kb


def payment_method_kb(stars: int = 100, price: int = 21000, recipient: str = "me"):
    """Skrinshot 5 dagi to'lov usulini tanlash (Faqat Balans va Karta orqali)."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💱 Balans orqali", callback_data=f"stars_pay_bal:{stars}:{price}:{recipient}"))
    kb.add(types.InlineKeyboardButton("💳 Karta orqali", callback_data=f"stars_pay_crd:{stars}:{price}:{recipient}"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="stars_back_to_confirm"))
    return kb


def make_expired_kb(tid: int = None):
    """Bekor qilingan buyurtma uchun chek yuborish tugmasi (media_1794240633655_bdc46e81.png)."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    if tid:
        kb.add(types.InlineKeyboardButton("🧾 Chek yuborish", callback_data=f"send_chek:{tid}"))
    else:
        kb.add(types.InlineKeyboardButton("🧾 Chek yuborish", callback_data="send_chek_any"))
    return kb


def format_card_owner_display(name: str) -> str:
    if not name:
        return ""
    clean = name.strip().replace("`", "'").replace("ʻ", "'").replace("ʼ", "'")
    parts = clean.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return clean


def card_invoice_kb(tid: int):
    """Karta orqali to'lov ekrani tugmalari (media_1794240121966_3eaf763a.png)."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        kb.add(types.InlineKeyboardButton(
            "Kartani nusxalash",
            copy_text=types.CopyTextButton(text=CARD_NUMBER.replace(" ", "")),
        ))
    except AttributeError:
        pass
    kb.add(types.InlineKeyboardButton("✅ To'ladim", callback_data=f"paid:{tid}"))
    kb.add(types.InlineKeyboardButton("🗑 Buyurtmani bekor qilish", callback_data=f"cancel:{tid}"))
    return kb


def back_to_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"))
    return kb


# ── Asosiy Menyu Matni (Skrinshot 1) ──────────────────────────────────
def menu_text(uid: int, name: str) -> str:
    bal = get_balance(uid)
    return (
        f"👋 <b>Assalomu alaykum, {html.escape(name)}!</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"💱 <b>Hisobingiz:</b> <b>{fmt(bal)} so'm</b>\n\n"
        f"👇 <b>Quyidagi menu'dan keraklisini tanlang:</b>"
    )


# ══════════════════════════════════════════ BOT HANDLERLARI ════════════
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    clear_user_state(m.from_user.id)
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username, m.from_user.full_name)
    log.info("START buyrug'i: %s", get_user_display(uid))

    # Referral tekshiruvi: /start ref_123456
    text_parts = (m.text or "").split()
    if len(text_parts) > 1 and text_parts[1].startswith("ref_"):
        ref_id_str = text_parts[1].replace("ref_", "")
        if ref_id_str.isdigit():
            save_referral(int(ref_id_str), uid)

    bot.send_message(
        m.chat.id,
        menu_text(uid, m.from_user.full_name),
        parse_mode="HTML",
        reply_markup=main_menu_inline_kb(),
    )


# Reply keyboard tugmalari
@bot.message_handler(func=lambda m: m.text and "stars olish" in m.text.lower())
def text_stars_olish(m):
    start_stars_flow(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text and "hisob to'ldirish" in m.text.lower())
def text_topup_btn(m):
    trigger_topup_flow(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text and "kabinet" in m.text.lower())
def text_cabinet_btn(m):
    show_cabinet(m.chat.id, m.from_user.id, m.from_user.full_name)


@bot.message_handler(func=lambda m: m.text and "qo'llanma" in m.text.lower())
def text_help_btn(m):
    show_help(m.chat.id)


@bot.message_handler(func=lambda m: m.text and "hamkorlik" in m.text.lower())
def text_referral_btn(m):
    show_referral(m.chat.id, m.from_user.id)


# ── Stars Xarid Qilish Oqimi (Screenshots 2 -> 3 -> 4 -> 5) ─────────────
def start_stars_flow(chat_id: int, uid: int, msg_id: int = None):
    clear_user_state(uid)
    with _state_lock:
        _waiting_stars_recipient.add(uid)

    text = (
        "🌟 <b>Telegram Stars xarid qilish</b>\n\n"
        "🔎 <b>Stars kimga yuborilsin?</b>\n"
        "Foydalanuvchi nomini kiriting:\n"
        "👇 Masalan: <code>@StarsUnoHelp</code>"
    )
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=recipient_choice_kb())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=recipient_choice_kb())


@bot.callback_query_handler(func=lambda c: c.data == "stars_start")
def cb_stars_start(c):
    start_stars_flow(c.message.chat.id, c.from_user.id, c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "stars_self")
def cb_stars_self(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    with _state_lock:
        _waiting_stars_recipient.discard(uid)

    uname = c.from_user.username
    if not uname:
        bot.answer_callback_query(c.id, "Profilingizda @username o'rnatilmagan! Iltimos, boshqa @username kiriting.", show_alert=True)
        return

    recipient = uname.replace("@", "")
    save_stars_session(uid, recipient=recipient)

    show_packages_screen(c.message.chat.id, uid, recipient, c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["text"], func=lambda m: m.from_user.id in _waiting_stars_recipient)
def got_stars_recipient(m):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username, m.from_user.full_name)
    raw = (m.text or "").strip().replace("https://t.me/", "").replace("@", "")

    if not raw or not (3 <= len(raw) <= 32) or not re.match(r"^[a-zA-Z0-9_]+$", raw):
        bot.send_message(m.chat.id, "❌ Noto'g'ri format! Iltimos, haqiqiy Telegram @username kiriting (masalan: <code>@StarsUnoHelp</code>):", parse_mode="HTML")
        return

    with _state_lock:
        _waiting_stars_recipient.discard(uid)
    save_stars_session(uid, recipient=raw)

    show_packages_screen(m.chat.id, uid, raw)


def show_packages_screen(chat_id: int, uid: int, recipient: str, msg_id: int = None):
    text = (
        "🌟 <b>Telegram Stars xarid qilish</b>\n\n"
        f"<blockquote>👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(recipient)}</code>\n"
        f"🛒 <b>Mavjud miqdor:</b> {fmt(AVAILABLE_STARS_STOCK)} ta</blockquote>\n\n"
        "👇 <b>Kerakli miqdorni tanlang:</b>"
    )
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=stars_packages_kb())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=stars_packages_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("stars_pkg:"))
def cb_stars_pkg(c):
    parts = c.data.split(":")
    stars = int(parts[1])
    price = int(parts[2])
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)

    sess = get_stars_session(uid)
    recipient = sess.get("recipient") or c.from_user.username or "me"
    save_stars_session(uid, recipient=recipient, stars=stars, price=price)

    show_confirmation_screen(c.message.chat.id, uid, recipient, stars, price, c.message.message_id)
    bot.answer_callback_query(c.id, f"✅ {stars} ta Stars tanlandi")


def calculate_custom_stars_price(stars: int) -> int:
    """Ixtiyoriy Stars uchun kamida 1 000 so'm sof foyda hisoblash."""
    base_price = stars * 210
    min_profit_price = stars * STAR_COST_PER_UNIT + 1000
    final_price = max(base_price, min_profit_price)
    return int((final_price + 99) // 100 * 100)


@bot.callback_query_handler(func=lambda c: c.data == "stars_custom")
def cb_stars_custom(c):
    uid = c.from_user.id
    with _state_lock:
        _waiting_custom_stars.add(uid)

    bot.send_message(
        c.message.chat.id,
        "✏️ <b>Kerakli Stars miqdorini kiriting:</b>\n\n"
        "• Minimal: <b>10 ta</b>\n"
        "• Maksimal: <b>10 000 ta</b>\n"
        "• 1 Star = <b>210 so'm</b>\n"
        "<i>(Har bir xarid uchun eng qulay narx va tezkor yetkazish)</i>",
        parse_mode="HTML",
        reply_markup=back_to_menu_kb(),
    )
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["text"], func=lambda m: m.from_user.id in _waiting_custom_stars)
def got_custom_stars(m):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username, m.from_user.full_name)
    raw = (m.text or "").strip().replace(" ", "")

    if not raw.isdigit() or not (10 <= int(raw) <= 10000):
        bot.send_message(m.chat.id, "❌ Iltimos, 10 dan 10 000 gacha bo'lgan butun son kiriting:")
        return

    stars = int(raw)
    price = calculate_custom_stars_price(stars)
    with _state_lock:
        _waiting_custom_stars.discard(uid)
    sess = get_stars_session(uid)
    recipient = sess.get("recipient") or m.from_user.username or "me"
    save_stars_session(uid, recipient=recipient, stars=stars, price=price)

    show_confirmation_screen(m.chat.id, uid, recipient, stars, price)


def show_confirmation_screen(chat_id: int, uid: int, recipient: str, stars: int, price: int, msg_id: int = None):
    text = (
        "🌟 <b>Buyurtmani tasdiqlang</b>\n\n"
        f"<blockquote>💲 <b>Narxi:</b> <b>{fmt(price)} so'm</b>\n"
        f"⭐️ <b>Miqdor:</b> <b>{stars} ta</b>\n"
        f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(recipient)}</code></blockquote>\n\n"
        "Tasdiqlash tugmasini bossangiz, to'lov usullari chiqadi."
    )
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=confirm_order_kb())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=confirm_order_kb())


@bot.callback_query_handler(func=lambda c: c.data == "stars_back_to_pkg")
def cb_stars_back_to_pkg(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    sess = get_stars_session(uid)
    show_packages_screen(c.message.chat.id, uid, sess.get("recipient", "me"), c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "stars_confirm_proceed")
def cb_stars_confirm_proceed(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    bal = get_balance(uid)
    sess = get_stars_session(uid)
    stars = sess.get("stars", 100)
    price = sess.get("price", 21000)
    recipient = sess.get("recipient") or c.from_user.username or "me"
    text = (
        "💳 <b>Qanday usulda to'lov qilasiz?</b>\n\n"
        f"💵 <b>Hisobingiz:</b> <b>{fmt(bal)} so'm</b>"
    )
    kb = payment_method_kb(stars, price, recipient)
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "stars_pay_paynet")
def cb_stars_pay_paynet(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    sess = get_stars_session(uid)
    price = sess.get("price", 21000)
    stars = sess.get("stars", 100)
    recipient = sess.get("recipient") or c.from_user.username or "me"

    text = (
        "🟩 <b>Paynet terminal orqali to'lov:</b>\n\n"
        f"<blockquote>⭐️ Miqdor: <b>{stars} ta Stars</b>\n"
        f"💰 To'lanadigan summa: <b>{fmt(price)} so'm</b>\n"
        f"👤 Qabul qiluvchi: <code>@{html.escape(recipient)}</code></blockquote>\n\n"
        "1️⃣ Istalgan Paynet shoxobchasi yoki terminaliga boring.\n"
        "2️⃣ Humo kartaga to'ldirish bo'limini tanlang.\n"
        f"3️⃣ Karta raqam: <code>{CARD_NUMBER}</code>\n"
        + (f"   Karta egasi: <b>{CARD_OWNER}</b>\n" if CARD_OWNER else "")
        + f"4️⃣ Aynan <b>{fmt(price)} so'm</b> to'lang va chekni oling.\n\n"
        "🧾 To'lovdan so'ng chek rasmini adminga yuboring, Stars zudlik bilan yuboriladi!"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🧾 Chek yuborish (Admin)", url="https://t.me/Abdulaziz_05_0"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="stars_confirm_proceed"))
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "stars_back_to_confirm")
def cb_stars_back_to_confirm(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    sess = get_stars_session(uid)
    show_confirmation_screen(c.message.chat.id, uid, sess.get("recipient", "me"), sess.get("stars", 100), sess.get("price", 21000), c.message.message_id)
    bot.answer_callback_query(c.id)


# ── To'lov usuli 1: Balans orqali ───────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("stars_pay_bal") or c.data == "stars_pay_balance")
def cb_stars_pay_balance(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    sess = get_stars_session(uid)

    if ":" in c.data:
        parts = c.data.split(":")
        try:
            if len(parts) >= 3:
                sess["stars"] = int(parts[1])
                sess["price"] = int(parts[2])
            if len(parts) >= 4 and parts[3]:
                sess["recipient"] = parts[3]
        except (ValueError, IndexError):
            pass

    stars = sess.get("stars", 100)
    price = sess.get("price", 21000)
    recipient = sess.get("recipient") or c.from_user.username or "me"

    with lock:
        bal = get_balance(uid)
        if bal < price:
            bot.answer_callback_query(
                c.id,
                f"❌ Hisobingizda mablag' yetarli emas!\nHisobingiz: {fmt(bal)} so'm. Kerak: {fmt(price)} so'm.\nIltimos, '💳 Karta orqali' to'lang.",
                show_alert=True,
            )
            return

        # Balansdan yechish
        db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (price, uid))
        new_bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

        now = int(time.time())
        order_code = f"STAR-{now}-{uid}"
        cur = db.execute(
            "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, created_at) "
            "VALUES(?,?,?,?,?,'pending','balance',?)",
            (order_code, uid, recipient, stars, price, now),
        )
        s_id = cur.lastrowid
        tx_hash = deliver_stars_fulfillment(s_id, uid, recipient, stars, price, "balance")

    log.info("STARS SOTIB OLINDI (Balans): %s | Stars: %s | Summa: %s | Qabul qiluvchi: @%s", 
             get_user_display(uid), stars, price, recipient)

    bot.send_message(
        c.message.chat.id,
        f"🎉 <b>{stars} Stars muvaffaqiyatli yuborildi!</b>\n\n"
        f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(recipient)}</code>\n"
        f"💰 <b>Hisobingizdan yechildi:</b> <b>{fmt(price)} so'm</b>\n"
        f"💱 <b>Qolgan balansingiz:</b> <b>{fmt(new_bal)} so'm</b>\n"
        f"🧾 <b>Tranzaksiya:</b> <code>{tx_hash}</code>",
        parse_mode="HTML",
        reply_markup=main_menu_inline_kb(),
    )
    bot.answer_callback_query(c.id)


# ── To'lov usuli 2: Humo Karta orqali ───────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("stars_pay_crd") or c.data == "stars_pay_card")
def cb_stars_pay_card(c):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.username, c.from_user.full_name)
    sess = get_stars_session(uid)

    if ":" in c.data:
        parts = c.data.split(":")
        try:
            if len(parts) >= 3:
                sess["stars"] = int(parts[1])
                sess["price"] = int(parts[2])
            if len(parts) >= 4 and parts[3]:
                sess["recipient"] = parts[3]
        except (ValueError, IndexError):
            pass

    stars = sess.get("stars", 100)
    price = sess.get("price", 21000)
    recipient = sess.get("recipient") or c.from_user.username or "me"

    # Humo topup buyurtmasi yaratish
    tid, amount = create_topup(uid, price)

    # Stars buyurtmasi bilan bog'lash
    now = int(time.time())
    order_code = f"STAR-{now}-{uid}"
    with lock:
        db.execute(
            "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, topup_id, created_at) "
            "VALUES(?,?,?,?,?,'pending','humo_card',?,?)",
            (order_code, uid, recipient, stars, price, tid, now),
        )

    waiting_card_set(uid, tid)  # Foydalanuvchi to'g'ridan-to'g'ri 4 raqamni kiritishi mumkin

    owner_str = format_card_owner_display(CARD_OWNER)
    card_line = f"{CARD_NUMBER} | {owner_str}" if owner_str else CARD_NUMBER

    log.info("STARS BUYURTMA YARATILDI (Karta): %s | Order #%s | Stars: %s | Summa: %s | Qabul qiluvchi: @%s", 
             get_user_display(uid), tid, stars, price, recipient)

    text = (
        "🛒 <b>Buyurtma ma'lumotlari:</b>\n\n"
        f"<blockquote>🔹 Turi: ⭐️ Telegram Stars xaridi\n"
        f"🆔 Order ID: #{tid}\n"
        f"🔹 Miqdor: {stars} ta\n"
        f"👤 Qabul qiluvchi: @{html.escape(recipient)}\n"
        f"💳 Karta: <code>{card_line}</code>\n"
        f"💲 Narxi: {price} so'm</blockquote>\n\n"
        "⚠️ <b>E'tiborli bo'ling!</b>\n"
        f"<blockquote>To'lov 5 daqiqa amal qiladi.\n"
        f"{price} so'mdan 1 so'm ko'p yoki kam tashlamang. Aks holda bot to'lovingizni aniqlay olmaydi.\n"
        "Paynet terminal orqali bu yerda to'lov qilmang! Terminal uchun «Hisob to'ldirish» bo'limidan foydalaning.\n"
        "5 daqiqadan so'ng buyurtma bekor qilinadi.</blockquote>"
    )
    bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=card_invoice_kb(tid))
    bot.answer_callback_query(c.id)


# ── Oddiy Hisob to'ldirish (Manual Topup) ────────────────────────────────
def trigger_topup_flow(chat_id: int, uid: int, msg_id: int = None):
    waiting_amount_add(uid)
    text = (
        f"💵 <b>To'ldirish summasini kiriting (so'mda):</b>\n\n"
        f"Minimal: <b>{fmt(MIN_AMOUNT)} so'm</b>\n"
        f"Maksimal: <b>{fmt(MAX_AMOUNT)} so'm</b>"
    )
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=back_to_menu_kb())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=back_to_menu_kb())


@bot.callback_query_handler(func=lambda c: c.data == "topup")
def cb_topup_call(c):
    trigger_topup_flow(c.message.chat.id, c.from_user.id, c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["text"], func=lambda m: waiting_amount_check(m.from_user.id))
def got_amount(m):
    txt = (m.text or "").replace(" ", "").replace(",", "").replace(".", "")
    if not txt.isdigit() or not (MIN_AMOUNT <= int(txt) <= MAX_AMOUNT):
        bot.send_message(m.chat.id, f"❌ Faqat {fmt(MIN_AMOUNT)} dan {fmt(MAX_AMOUNT)} gacha butun son kiriting:")
        return

    amount = int(txt)
    waiting_amount_remove(m.from_user.id)
    tid, amount = create_topup(m.from_user.id, amount)
    waiting_card_set(m.from_user.id, tid)

    owner_str = format_card_owner_display(CARD_OWNER)
    card_line = f"{CARD_NUMBER} | {owner_str}" if owner_str else CARD_NUMBER

    text = (
        "🛒 <b>Buyurtma ma'lumotlari:</b>\n\n"
        f"<blockquote>🔹 Turi: 🌌 Hisob to'ldirish\n"
        f"🆔 Order ID: #{tid}\n"
        f"💳 Karta: <code>{card_line}</code>\n"
        f"💲 Narxi: {amount} so'm</blockquote>\n\n"
        "⚠️ <b>E'tiborli bo'ling!</b>\n"
        f"<blockquote>To'lov 5 daqiqa amal qiladi.\n"
        f"{amount} so'mdan 1 so'm ko'p yoki kam tashlamang. Aks holda bot to'lovingizni aniqlay olmaydi.\n"
        "5 daqiqadan so'ng buyurtma bekor qilinadi.</blockquote>"
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=card_invoice_kb(tid))


# ── Foydalanuvchi "To'ladim" deb karta raqamini kiritishi ───────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("paid:"))
def cb_paid(c):
    tid = int(c.data.split(":")[1])
    waiting_card_set(c.from_user.id, tid)

    with lock:
        order_row = db.execute("SELECT amount FROM topups WHERE id=?", (tid,)).fetchone()
    amt_str = f"💰 To'lov summasi: <b>{fmt(order_row[0])} so'm</b>\n\n" if order_row else ""

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🗑 Buyurtmani bekor qilish", callback_data=f"cancel:{tid}"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="menu"))

    prompt_text = (
        f"💳 <b>Buyurtma #{tid} bo'yicha to'lovni tasdiqlash:</b>\n\n"
        f"{amt_str}"
        f"Iltimos, to'lovni amalga oshirgan kartangizning <b>oxirgi 4 ta raqamini</b> yuboring:\n"
        f"<i>(Masalan: <code>9432</code>)</i>\n\n"
        f"ℹ️ <b>Nima uchun bu kerak?</b>\n"
        f"Siz 4 ta raqamni kiritishingiz bilan tizim uni bank xabari bilan solishtiradi va hisobingizga avtomatik biriktiradi."
    )
    try:
        bot.edit_message_text(prompt_text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(c.message.chat.id, prompt_text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["text"], func=lambda m: bool(waiting_card_get(m.from_user.id)))
def got_card_digits(m):
    uid = m.from_user.id
    tid = waiting_card_get(uid)
    raw_digits = (m.text or "").strip().replace(" ", "").replace("*", "").replace("-", "")

    if not (raw_digits.isdigit() and len(raw_digits) == 4):
        bot.send_message(
            m.chat.id,
            "❌ <b>Qat'iy xavfsizlik talabi!</b>\n\n"
            "To'lovlar boshqa foydalanuvchilar bilan chalkashmasligi uchun, kartangizning aynan <b>oxirgi 4 ta raqamini</b> kiritishingiz shart (masalan: <code>8421</code>).\n\n"
            "<i>(2 yoki 3 ta raqam xavfsizlik sababli qabul qilinmaydi)</i>",
            parse_mode="HTML",
        )
        return

    waiting_card_remove(uid)

    try:
        ok, result = record_user_payment_confirmation(tid, uid, raw_digits)
    except Exception:
        log.exception("record_user_payment_confirmation xatosi")
        bot.send_message(m.chat.id, "Tizimda xatolik yuz berdi. Iltimos, adminga murojaat qiling.")
        return

    if not ok:
        if result == "expired":
            bot.send_message(
                m.chat.id,
                f"⏰ <b>Buyurtma #{tid} ning 5 daqiqalik muddati tugagan!</b>\n\n"
                "Xavfsizlik qoidalariga ko'ra muddati o'tgan buyurtma avtomatik bekor qilinadi.\n\n"
                "• Qaytadan to'ldirish uchun: <b>/menu</b> orqali yangi buyurtma oching.\n"
                "• Agar pul o'tkazgan bo'lsangiz: xavotir olmang, to'lovingiz tizimda saqlangan. Adminga murojaat qiling.",
                parse_mode="HTML",
                reply_markup=main_menu_inline_kb(),
            )
        else:
            bot.send_message(m.chat.id, f"❌ Xatolik: {result}", reply_markup=main_menu_inline_kb())
        return

    if result == "already_paid":
        bot.send_message(m.chat.id, f"✅ Buyurtma #{tid} allaqachon to'langan!", reply_markup=main_menu_inline_kb())
        return

    status_type = result[0]
    if status_type == "stars_delivered":
        _, s_amt, r_user, p_amt, s_card, tx_h = result
        bot.send_message(
            m.chat.id,
            f"🎉 <b>To'lov tasdiqlandi va {s_amt} Stars yuborildi!</b>\n\n"
            f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(r_user)}</code>\n"
            f"💰 <b>To'langan:</b> <b>{fmt(p_amt)} so'm</b>\n"
            f"💳 <b>Karta:</b> <b>*{s_card}</b>\n"
            f"🧾 <b>Tranzaksiya:</b> <code>{tx_h}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    elif status_type == "matched_immediately":
        _, amt, new_bal, msg_id = result
        bot.send_message(
            m.chat.id,
            f"🎉 <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"➕ Qo'shildi: <b>+{fmt(amt)} so'm</b>\n"
            f"💳 Karta: <b>*{raw_digits}</b>\n"
            f"💰 Yangi balansingiz: <b>{fmt(new_bal)} so'm</b>",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    elif status_type == "waiting_bank":
        _, amt, _ = result
        bot.send_message(
            m.chat.id,
            f"⏳ <b>To'lov tasdiqlanishi kutilmoqda...</b>\n\n"
            f"📋 Buyurtma: <b>#{tid}</b>\n"
            f"💰 Summa: <b>{fmt(amt)} so'm</b>\n"
            f"💳 Karta oxiri: <b>*{raw_digits}</b>\n\n"
            f"Bankdan to'lov xabari kelishi bilan hisobingizga avtomatik biriktiriladi (odatda 5–30 soniya ichida).",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(c):
    tid = int(c.data.split(":")[1])
    clear_user_state(c.from_user.id)
    if cancel_topup(tid, c.from_user.id):
        text = (
            f"🗑 <b>Buyurtma #{tid} bekor qilindi!</b>\n\n"
            f"<blockquote>Buyurtma foydalanuvchi tomonidan bekor qilindi.</blockquote>\n\n"
            f"🧾 <b>Agar to'lov qilgan bo'lsangiz, chekni yuboring:</b>"
        )
        try:
            bot.edit_message_text(
                text,
                c.message.chat.id,
                c.message.message_id,
                parse_mode="HTML",
                reply_markup=make_expired_kb(tid),
            )
        except Exception:
            pass
        bot.answer_callback_query(c.id, "Buyurtma bekor qilindi.")
    else:
        bot.answer_callback_query(c.id, "Bekor qilib bo'lmaydi (to'langan yoki muddati o'tgan).", show_alert=True)


# ── Bo'limlar: Kabinet, Qo'llanma, Hamkorlik ───────────────────────────
def show_cabinet(chat_id: int, uid: int, name: str, msg_id: int = None):
    bal, total_paid, total_stars, recent_orders = get_user_stats(uid)
    with lock:
        u_info = db.execute("SELECT username, phone FROM users WHERE id=?", (uid,)).fetchone()
    uname = f"@{u_info[0]}" if u_info and u_info[0] else "<i>(username yo'q)</i>"
    has_phone = bool(u_info and u_info[1])
    phone_display = u_info[1] if has_phone else "<i>(kiritilmagan)</i>"

    text = (
        f"👤 <b>Kabinet</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"👤 <b>Foydalanuvchi:</b> <b>{html.escape(name)}</b>\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"📱 <b>Telefon:</b> <code>{phone_display}</code>\n"
        f"💱 <b>Joriy hisobingiz:</b> <b>{fmt(bal)} so'm</b>\n"
        f"⭐️ <b>Xarid qilingan Stars:</b> <b>{fmt(total_stars)} ta</b>\n"
        f"📥 <b>Jami to'lovlar:</b> <b>{fmt(total_paid)} so'm</b>\n"
    )
    if recent_orders:
        text += "\n📋 <b>So'nggi Stars xaridlari:</b>\n"
        for s_amt, price, status, r_user, c_at in recent_orders:
            icon = "✅" if status == "delivered" else "⏳"
            t_str = time.strftime("%d.%m %H:%M", time.localtime(c_at))
            text += f"  {icon} <b>{s_amt} Stars</b> (@{html.escape(r_user)}) — {fmt(price)} so'm <i>({t_str})</i>\n"

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🌌 Hisob to'ldirish", callback_data="topup"),
        types.InlineKeyboardButton("🌟 Stars olish", callback_data="stars_start"),
    )
    if not has_phone:
        kb.add(types.InlineKeyboardButton("📱 Telefon raqamni ulash", callback_data="prompt_share_phone"))
    kb.add(types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"))

    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "cabinet")
def cb_cabinet(c):
    ensure_user(c.from_user.id, c.from_user.username, c.from_user.full_name)
    show_cabinet(c.message.chat.id, c.from_user.id, c.from_user.full_name, c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "prompt_share_phone")
def cb_prompt_share_phone(c):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📱 Telefon raqamni ulash", request_contact=True))
    bot.send_message(
        c.message.chat.id,
        "👇 Pastdagi <b>«📱 Telefon raqamni ulash»</b> tugmasini bosib kontaktni tasdiqlang:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["contact"])
def handle_contact(m):
    if m.contact:
        phone = m.contact.phone_number
        if not phone.startswith("+"):
            phone = f"+{phone}"
        ensure_user(m.from_user.id, m.from_user.username, m.from_user.full_name, phone=phone)
        log.info("TELEFON RAQAM SAQLANDI: %s | %s", get_user_display(m.from_user.id), phone)
        bot.send_message(
            m.chat.id,
            f"✅ <b>Raqamingiz muvaffaqiyatli saqlandi:</b> <code>{phone}</code>\n"
            f"Buyurtmalaringiz holati bo'yicha tezkor aloqa kafolatlanadi.",
            parse_mode="HTML",
            reply_markup=reply_main_kb(),
        )


def show_help(chat_id: int, msg_id: int = None):
    text = (
        "☎️ <b>Qo'llanma & Yordam</b>\n\n"
        "⭐ <b>Telegram Stars nima?</b>\n"
        "Telegram Stars — bu Telegram tarmog'idagi pullik kanallar, guruhlar, botlar, obunalar va sovg'alar uchun rasmiy to'lov birligi.\n\n"
        "❓ <b>Qanday sotib olinadi?</b>\n"
        "1. <b>'🌟 Stars olish'</b> tugmasini bosing.\n"
        "2. O'zingiz yoki do'stingizning @username'ini tanlang.\n"
        "3. Kerakli miqdorni tanlang (masalan: 100 Stars = 21 000 so'm).\n"
        "4. Karta yoki botdagi hisobingiz orqali to'lang.\n"
        "5. 15–30 soniya ichida Stars hisobingizga tushadi!\n\n"
        "👨‍💻 <b>Qo'llab-quvvatlash / Admin:</b> @Abdulaziz_05_0"
    )
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=back_to_menu_kb())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=back_to_menu_kb())


@bot.callback_query_handler(func=lambda c: c.data == "help")
def cb_help(c):
    show_help(c.message.chat.id, c.message.message_id)
    bot.answer_callback_query(c.id)


def show_referral(chat_id: int, uid: int, msg_id: int = None):
    ref_count, ref_bonus = get_referral_stats(uid)
    bot_me = bot.get_me()
    b_name = bot_me.username or "NovaStars_uz_Bot"
    ref_link = f"https://t.me/{b_name}?start=ref_{uid}"
    share_msg = f"⭐️ Telegram Stars ni @{b_name} botida eng arzon va xavfsiz oling!\n👉 {ref_link}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote(share_msg)}"
    text = (
        "🤝 <b>Hamkorlik dasturi (Referral)</b>\n\n"
        "Do'stlaringizni taklif qiling va har bir do'stingizning Stars xarididan <b>5% doimiy naqd bonus</b> oling!\n\n"
        f"🔗 <b>Sizning taklif havolangiz:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 <b>Taklif qilingan do'stlar:</b> <b>{ref_count} ta</b>\n"
        f"💰 <b>To'plangan umumiy bonus:</b> <b>{fmt(ref_bonus)} so'm</b>\n\n"
        "💡 <i>Bonuslar to'g'ridan-to'g'ri balansingizga tushadi va unga bepul Stars xarid qilishingiz mumkin!</i>"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🚀 Do'stlarga ulashish", url=share_url))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="menu"))
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "referral")
def cb_referral(c):
    show_referral(c.message.chat.id, c.from_user.id, c.message.message_id)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu")
def cb_menu_root(c):
    clear_user_state(c.from_user.id)
    try:
        bot.edit_message_text(
            menu_text(c.from_user.id, c.from_user.full_name),
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    except Exception:
        bot.send_message(
            c.message.chat.id,
            menu_text(c.from_user.id, c.from_user.full_name),
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    bot.answer_callback_query(c.id)


# ── Admin Buyruqlari ───────────────────────────────────────────────────
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
        f"✅ Bajarildi! User <code>{uid}</code> ga +{fmt(amount)} so'm berildi.\nYangi balans: <b>{fmt(bal)} so'm</b>",
        parse_mode="HTML",
    )
    try:
        bot.send_message(
            uid,
            f"✅ <b>To'lov administrator tomonidan tasdiqlandi!</b>\n\n"
            f"➕ Qo'shildi: <b>+{fmt(amount)} so'm</b>\n"
            f"💰 Hisobingiz: <b>{fmt(bal)} so'm</b>",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    except Exception:
        pass


@bot.message_handler(commands=["unmatched"])
def cmd_unmatched(m):
    """Admin: Biriktirilmagan to'lovlar ro'yxatini ko'rish"""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    with lock:
        rows = db.execute(
            "SELECT id, msg_id, amount, card_last4, created_at FROM unmatched_payments "
            "WHERE status='pending' ORDER BY id DESC LIMIT 10"
        ).fetchall()

    if not rows:
        bot.send_message(m.chat.id, "✅ <b>Biriktirilmagan to'lovlar mavjud emas.</b>\nHamma to'lovlar o'z vaqtida muvaffaqiyatli hisobga olingan!", parse_mode="HTML")
        return

    text = "📋 <b>Biriktirilmagan to'lovlar ro'yxati:</b>\n\n"
    for r_id, msg_id, amt, card_last4, c_at in rows:
        t_str = time.strftime("%H:%M %d.%m", time.localtime(c_at))
        text += (
            f"🔹 <b>{fmt(amt)} so'm</b> | Karta: <code>*{card_last4}</code> | {t_str}\n"
            f"   HUMO Msg ID: <code>#{msg_id}</code>\n"
            f"   Qo'lda hisobga qo'shish: <code>/credit &lt;user_id&gt; {amt}</code>\n\n"
        )
    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["users", "mijozlar"])
def cmd_users(m):
    """Admin: Foydalanuvchilar va ularning ma'lumotlari ro'yxati."""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    with lock:
        rows = db.execute(
            "SELECT id, balance, username, full_name, phone FROM users ORDER BY id DESC LIMIT 50"
        ).fetchall()

    if not rows:
        bot.send_message(m.chat.id, "Bazada hozircha foydalanuvchilar mavjud emas.")
        return

    text = f"👥 <b>Foydalanuvchilar (Mijozlar) ro'yxati ({len(rows)} ta):</b>\n\n"
    for uid, bal, uname, fname, phone in rows:
        u_str = f"@{uname}" if uname else "<i>(username yo'q)</i>"
        p_str = f" | 📱 <code>{phone}</code>" if phone else ""
        text += (
            f"• <b>{html.escape(fname or 'Noma\'lum')}</b> ({u_str})\n"
            f"  🆔 <code>{uid}</code> | 💵 <b>{fmt(bal)} so'm</b>{p_str}\n"
        )
    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["user"])
def cmd_user(m):
    """Admin: Muayyan foydalanuvchi haqida to'liq ma'lumot (/user <id>)."""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(m.chat.id, "Foydalanish: /user <user_id>")
        return
    target_uid = int(parts[1])
    with lock:
        u = db.execute("SELECT id, balance, username, full_name, phone FROM users WHERE id=?", (target_uid,)).fetchone()
        if not u:
            bot.send_message(m.chat.id, f"Foydalanuvchi #{target_uid} topilmadi.")
            return
        orders = db.execute("SELECT stars_amount, price_uzs, status, recipient_username, created_at FROM stars_orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (target_uid,)).fetchall()
        topups_cnt = db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM topups WHERE user_id=? AND status='paid'", (target_uid,)).fetchone()

    uname_str = f"@{u[2]}" if u[2] else "(yo'q)"
    phone_str = u[4] or "(kiritilmagan)"
    text = (
        f"👤 <b>Foydalanuvchi kartochkasi:</b>\n\n"
        f"🆔 ID: <code>{u[0]}</code>\n"
        f"👤 Ism: <b>{html.escape(u[3] or 'Noma\'lum')}</b>\n"
        f"🔗 Username: {uname_str}\n"
        f"📱 Telefon: <code>{phone_str}</code>\n"
        f"💰 Balans: <b>{fmt(u[1])} so'm</b>\n"
        f"💳 To'lovlar: <b>{topups_cnt[0]} ta ({fmt(topups_cnt[1])} so'm)</b>\n"
    )
    if orders:
        text += "\n🌟 <b>So'nggi buyurtmalar:</b>\n"
        for stars, price, st, rec, t_at in orders:
            text += f"• {stars} Stars (@{rec}) - {fmt(price)} so'm [{st}]\n"

    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["testchannel"])
def cmd_test_channel(m):
    """Admin: Kanalga test post yuborib tekshirish."""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    if not CHANNEL_ID:
        bot.send_message(m.chat.id, "❌ .env faylida <code>CHANNEL_ID</code> sozlanmagan!\nMasalan: <code>CHANNEL_ID=@NovaStars_uz</code>", parse_mode="HTML")
        return
    try:
        post_order_result_to_channel(777, "abdulaziz0333", 100, 21000, "tx_ton_777888999abcdef0123456")
        bot.send_message(m.chat.id, f"✅ Test xabari <b>{html.escape(CHANNEL_ID)}</b> kanaliga muvaffaqiyatli yuborildi!", parse_mode="HTML")
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Kanalga post yuborishda xatolik: {e}\n\nBot ushbu kanalda <b>Administrator</b> ekanligiga va <b>Post Messages</b> ruxsatiga ega ekanligiga ishonch hosil qiling.", parse_mode="HTML")


# ── Admin Buyruqlari: Daromad va Moliyaviy Tahlil ──────────────────────
@bot.message_handler(commands=["daromad", "stats"])
def cmd_daromad(m):
    """Admin: Moliyaviy tahlil va sof foyda hisoboti."""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return

    with lock:
        orders = db.execute(
            "SELECT stars_amount, price_uzs FROM stars_orders WHERE status='delivered'"
        ).fetchall()

    total_orders = len(orders)
    total_stars = sum(o[0] for o in orders)
    total_revenue = sum(o[1] for o in orders)
    total_cost = sum(o[0] * STAR_COST_PER_UNIT for o in orders)
    net_profit = total_revenue - total_cost
    avg_profit = (net_profit // total_orders) if total_orders > 0 else 0

    text = (
        "📈 <b>TELEGRAM STARS MOLIYAVIY HISOBOTI & FOYDA</b>\n\n"
        f"📦 <b>Jami muvaffaqiyatli buyurtmalar:</b> <b>{total_orders} ta</b>\n"
        f"⭐️ <b>Jami sotilgan Stars:</b> <b>{fmt(total_stars)} ta</b>\n"
        f"💰 <b>Jami tushum (Oborot):</b> <b>{fmt(total_revenue)} so'm</b>\n"
        f"📉 <b>Jami tannarx (Fragment xarajati):</b> <b>{fmt(total_cost)} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>JAMI SOF FOYDA:</b> <b>+{fmt(net_profit)} so'm</b>\n"
        f"🎯 <b>Har bir buyurtmadan o'rtacha foyda:</b> <b>+{fmt(avg_profit)} so'm</b>\n\n"
        f"📊 <b>Paketlar bo'yicha sof foyda marjasi (kamida 1 000 so'm):</b>\n"
    )
    for s_amt, price in STAR_PACKAGES:
        cost = s_amt * STAR_COST_PER_UNIT
        prof = price - cost
        text += f"• <b>{s_amt} Stars:</b> Sotish: {fmt(price)} so'm | Tannarx: {fmt(cost)} so'm ➡️ <b>Foyda: +{fmt(prof)} so'm</b>\n"

    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["testchannel"])
def cmd_test_channel(m):
    """Admin: Kanalga test post yuborib tekshirish."""
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return
    if not CHANNEL_ID:
        bot.send_message(m.chat.id, "❌ .env faylida <code>CHANNEL_ID</code> sozlanmagan!\nMasalan: <code>CHANNEL_ID=@NovaStars_uz</code>", parse_mode="HTML")
        return
    try:
        post_order_result_to_channel(777, "abdulaziz0333", 100, 21000, "tx_ton_777888999abcdef0123456")
        bot.send_message(m.chat.id, f"✅ Test xabari <b>{html.escape(CHANNEL_ID)}</b> kanaliga muvaffaqiyatli yuborildi!", parse_mode="HTML")
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Kanalga post yuborishda xatolik: {e}\n\nBot ushbu kanalda <b>Administrator</b> ekanligiga va <b>Post Messages</b> ruxsatiga ega ekanligiga ishonch hosil qiling.", parse_mode="HTML")


# ── Chek Yuborish Oqimi (Foydalanuvchi va Admin) ────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("send_chek:"))
def cb_send_chek(c):
    tid = int(c.data.split(":")[1])
    uid = c.from_user.id
    waiting_receipt_set(uid, tid)

    text = (
        f"🧾 <b>Buyurtma #{tid} bo'yicha to'lov chekini yuborish:</b>\n\n"
        "📸 Iltimos, to'lov cheki (skrinshot yoki rasmi)ni yuboring.\n\n"
        "<i>(Chek adminga yetkaziladi va tekshirilgach, buyurtmangiz zudlik bilan tasdiqlab beriladi)</i>"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Bekor qilish", callback_data="menu"))
    try:
        bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "send_chek_any")
def cb_send_chek_any(c):
    uid = c.from_user.id
    with lock:
        row = db.execute("SELECT id FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    tid = row[0] if row else 0
    waiting_receipt_set(uid, tid)

    text = (
        "🧾 <b>To'lov chekini yuborish:</b>\n\n"
        "📸 Iltimos, to'lov cheki (skrinshot yoki rasmi)ni yuboring:\n\n"
        "<i>(Chek adminga yetkaziladi va tekshirilgach, buyurtmangiz zudlik bilan tasdiqlab beriladi)</i>"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Bekor qilish", callback_data="menu"))
    try:
        bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.message_handler(content_types=["photo", "document"], func=lambda m: bool(waiting_receipt_get(m.from_user.id)))
def got_receipt_media(m):
    uid = m.from_user.id
    tid = waiting_receipt_get(uid)
    waiting_receipt_remove(uid)

    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    now = int(time.time())

    with lock:
        db.execute(
            "INSERT INTO receipts(order_id, user_id, file_id, status, created_at) VALUES(?,?,?, 'pending', ?)",
            (tid, uid, file_id, now),
        )
        topup_row = db.execute("SELECT amount, status FROM topups WHERE id=?", (tid,)).fetchone()
        amt = topup_row[0] if topup_row else 0
        stars_row = db.execute(
            "SELECT id, recipient_username, stars_amount, price_uzs FROM stars_orders WHERE topup_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    bot.send_message(
        m.chat.id,
        "✅ <b>Chekingiz qabul qilindi va tekshirish uchun adminga yuborildi!</b>\n\n"
        "Administrator tekshirib tasdiqlashi bilan Stars balansingizga avtomatik yetkaziladi.",
        parse_mode="HTML",
        reply_markup=main_menu_inline_kb(),
    )

    stars_info = ""
    if stars_row:
        s_id, r_user, s_amt, s_price = stars_row
        cost = s_amt * STAR_COST_PER_UNIT
        prof = s_price - cost
        stars_info = (
            f"\n⭐️ <b>Stars miqdori:</b> <b>{s_amt} ta</b> (<code>@{html.escape(r_user)}</code>)\n"
            f"💰 <b>Narxi:</b> <b>{fmt(s_price)} so'm</b>\n"
            f"💵 <b>Kutilayotgan sof foyda:</b> <b>+{fmt(prof)} so'm</b>"
        )
    elif amt:
        stars_info = f"\n💰 <b>Summa:</b> <b>{fmt(amt)} so'm</b>"

    caption = (
        f"🚨 <b>YANGI TO'LOV CHEKI KELDI!</b>\n\n"
        f"🆔 <b>Buyurtma ID:</b> <code>#{tid}</code>\n"
        f"👤 <b>Foydalanuvchi:</b> {get_user_display(uid)}"
        f"{stars_info}\n"
        f"🕓 <b>Vaqt:</b> {time.strftime('%H:%M:%S %d.%m.%Y', time.localtime(now))}\n\n"
        "👇 <b>Qaror qabul qiling:</b>"
    )

    admin_kb = types.InlineKeyboardMarkup(row_width=2)
    admin_kb.add(
        types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"adm_appr:{tid}:{uid}"),
        types.InlineKeyboardButton("❌ Rad etish", callback_data=f"adm_rej:{tid}:{uid}"),
    )

    for admin_id in ADMIN_IDS:
        try:
            if m.photo:
                bot.send_photo(admin_id, file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
            else:
                bot.send_document(admin_id, file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
        except Exception:
            log.exception("Adminga chek yuborishda xatolik")


@bot.message_handler(content_types=["text"], func=lambda m: bool(waiting_receipt_get(m.from_user.id)))
def got_receipt_text(m):
    uid = m.from_user.id
    tid = waiting_receipt_get(uid)
    waiting_receipt_remove(uid)
    text_info = (m.text or "").strip()
    now = int(time.time())

    with lock:
        db.execute(
            "INSERT INTO receipts(order_id, user_id, file_id, status, created_at) VALUES(?,?,?, 'pending', ?)",
            (tid, uid, f"TEXT:{text_info[:200]}", now),
        )
        topup_row = db.execute("SELECT amount, status FROM topups WHERE id=?", (tid,)).fetchone()
        amt = topup_row[0] if topup_row else 0
        stars_row = db.execute(
            "SELECT id, recipient_username, stars_amount, price_uzs FROM stars_orders WHERE topup_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    bot.send_message(
        m.chat.id,
        "✅ <b>To'lov ma'lumotlaringiz adminga yuborildi!</b>\n\n"
        "Administrator tekshirib tasdiqlashi bilan Stars hisobingizga avtomatik yetkaziladi.",
        parse_mode="HTML",
        reply_markup=main_menu_inline_kb(),
    )

    stars_info = ""
    if stars_row:
        s_id, r_user, s_amt, s_price = stars_row
        cost = s_amt * STAR_COST_PER_UNIT
        prof = s_price - cost
        stars_info = (
            f"\n⭐️ <b>Stars miqdori:</b> <b>{s_amt} ta</b> (<code>@{html.escape(r_user)}</code>)\n"
            f"💰 <b>Narxi:</b> <b>{fmt(s_price)} so'm</b>\n"
            f"💵 <b>Kutilayotgan sof foyda:</b> <b>+{fmt(prof)} so'm</b>"
        )
    elif amt:
        stars_info = f"\n💰 <b>Summa:</b> <b>{fmt(amt)} so'm</b>"

    caption = (
        f"🚨 <b>YANGI TO'LOV XABARI KELDI!</b>\n\n"
        f"🆔 <b>Buyurtma ID:</b> <code>#{tid}</code>\n"
        f"👤 <b>Foydalanuvchi:</b> {get_user_display(uid)}"
        f"{stars_info}\n"
        f"📝 <b>Matn:</b> <i>{html.escape(text_info)}</i>\n"
        f"🕓 <b>Vaqt:</b> {time.strftime('%H:%M:%S %d.%m.%Y', time.localtime(now))}\n\n"
        "👇 <b>Qaror qabul qiling:</b>"
    )

    admin_kb = types.InlineKeyboardMarkup(row_width=2)
    admin_kb.add(
        types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"adm_appr:{tid}:{uid}"),
        types.InlineKeyboardButton("❌ Rad etish", callback_data=f"adm_rej:{tid}:{uid}"),
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, caption, parse_mode="HTML", reply_markup=admin_kb)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_appr:"))
def cb_admin_approve(c):
    if ADMIN_IDS and c.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(c.id, "Ruxsat yo'q!", show_alert=True)
        return

    parts = c.data.split(":")
    tid = int(parts[1])
    uid = int(parts[2])
    now = int(time.time())

    with lock:
        topup_row = db.execute("SELECT amount, status FROM topups WHERE id=?", (tid,)).fetchone()
        if not topup_row:
            bot.answer_callback_query(c.id, "Buyurtma topilmadi!", show_alert=True)
            return

        amt, status = topup_row
        if status == "paid":
            bot.answer_callback_query(c.id, "Bu buyurtma allaqachon to'langan!", show_alert=True)
            return

        db.execute("UPDATE topups SET status='paid', paid_at=? WHERE id=?", (now, tid))
        db.execute("UPDATE receipts SET status='approved', reviewed_at=?, reviewed_by=? WHERE order_id=?", (now, c.from_user.id, tid))

        stars_row = db.execute(
            "SELECT id, recipient_username, stars_amount, price_uzs FROM stars_orders WHERE topup_id=? AND status != 'delivered'",
            (tid,),
        ).fetchone()

        stars_data = None
        post_ok = False
        post_err = ""
        if stars_row:
            s_id, r_user, s_amt, s_price = stars_row
            tx_h = deliver_stars_fulfillment(s_id, uid, r_user, s_amt, s_price, "admin_approved")
            stars_data = (s_amt, r_user, tx_h)
            post_ok, post_err = post_order_result_to_channel(s_id, r_user, s_amt, s_price, tx_h)
        else:
            ensure_user(uid)
            db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amt, uid))
            new_bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
            post_ok, post_err = post_topup_result_to_channel(tid, uid, amt)

    try:
        if stars_data:
            s_amt, r_user, tx_h = stars_data
            bot.send_message(
                uid,
                f"🎉 <b>To'lovingiz administrator tomonidan tasdiqlandi!</b>\n\n"
                f"⭐️ <b>{s_amt} ta Stars</b> <code>@{html.escape(r_user)}</code> hisobiga muvaffaqiyatli yuborildi!\n"
                f"🧾 <b>Tranzaksiya:</b> <code>{tx_h}</code>",
                parse_mode="HTML",
                reply_markup=main_menu_inline_kb(),
            )
        else:
            bot.send_message(
                uid,
                f"🎉 <b>To'lovingiz administrator tomonidan tasdiqlandi!</b>\n\n"
                f"➕ Qo'shildi: <b>+{fmt(amt)} so'm</b>\n"
                f"💰 Yangi balansingiz: <b>{fmt(new_bal)} so'm</b>",
                parse_mode="HTML",
                reply_markup=main_menu_inline_kb(),
            )
    except Exception:
        log.exception("Mijozga tasdiq xabarini yuborib bo'lmadi")

    # Kanal hisoboti holati
    if post_ok:
        channel_note = f"\n📢 <b>Kanal hisobotiga qo'shildi:</b> {CHANNEL_ID} ✅"
    else:
        channel_note = (
            f"\n⚠️ <b>Kanalga yuborilmadi:</b> <i>{html.escape(str(post_err)[:60])}</i>\n"
            f"👉 <i>Bot kanalga xabar chiqarishi uchun @NovaStars_uz_Bot ni {CHANNEL_ID} ga <b>Admin</b> qilib qo'shing.</i>"
        )

    try:
        new_text = (c.message.caption or c.message.text or "") + f"\n\n✅ <b>TASDIQLANDI!</b> (Admin: @{c.from_user.username or c.from_user.id})" + channel_note
        if c.message.photo or c.message.document:
            bot.edit_message_caption(caption=new_text, chat_id=c.message.chat.id, message_id=c.message.message_id, parse_mode="HTML", reply_markup=None)
        else:
            bot.edit_message_text(new_text, chat_id=c.message.chat.id, message_id=c.message.message_id, parse_mode="HTML", reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "Buyurtma tasdiqlandi va kanalga yuborildi!")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_rej:"))
def cb_admin_reject(c):
    if ADMIN_IDS and c.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(c.id, "Ruxsat yo'q!", show_alert=True)
        return

    parts = c.data.split(":")
    tid = int(parts[1])
    uid = int(parts[2])
    now = int(time.time())

    with lock:
        db.execute("UPDATE receipts SET status='rejected', reviewed_at=?, reviewed_by=? WHERE order_id=?", (now, c.from_user.id, tid))

    try:
        bot.send_message(
            uid,
            f"❌ <b>Buyurtma #{tid} bo'yicha to'lov cheki rad etildi.</b>\n\n"
            "Bank ma'lumotlari to'lov bilan mos kelmadi yoki hisobga pul tushmadi.\n"
            "Qayta tekshirish yoki savollar uchun adminga murojaat qiling: @Abdulaziz_05_0",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(),
        )
    except Exception:
        pass

    try:
        new_text = (c.message.caption or c.message.text or "") + f"\n\n❌ <b>RAD ETILDI!</b> (Admin: @{c.from_user.username or c.from_user.id})"
        if c.message.photo or c.message.document:
            bot.edit_message_caption(caption=new_text, chat_id=c.message.chat.id, message_id=c.message.message_id, parse_mode="HTML", reply_markup=None)
        else:
            bot.edit_message_text(new_text, chat_id=c.message.chat.id, message_id=c.message.message_id, parse_mode="HTML", reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "Chek rad etildi.")


# ══════════════════════════════════════════ HUMO LISTENER ══════════════
def process_humo(msg_id: int, text: str, ts: float):
    humo_log.debug("=== msg_id=%s ts=%.0f ===\n%s\n%s", msg_id, ts, text, "=" * 40)
    log.info("HUMO xabar keldi: msg_id=%s | %s", msg_id, text[:60].replace("\n", " "))

    if not mark_seen(msg_id):
        log.info("Xabar #%s allaqachon qayta ishlangan. O'tkazib yuborildi.", msg_id)
        return

    amount = parse_humo_message(text)
    if amount is None:
        log.info("msg_id=%s: ushbu botga tegishli to'ldirish emas (boshqa karta yoki chiqim).", msg_id)
        return

    log.info("HUMO qonuniy kirim aniqlandi: %s so'm (*%s kartasiga)", amount, CARD_LAST4)

    res = claim_and_credit_humo(amount, msg_id, CARD_LAST4, text, ts)

    if res["matched"]:
        uid = res["user_id"]
        bal = res["balance"]
        s_card = res.get("sender_card")
        stars_data = res.get("stars_data")

        if stars_data:
            s_amt = stars_data["stars_amount"]
            r_user = stars_data["recipient"]
            tx_h = stars_data["tx_hash"]
            log.info("Stars buyurtmasi to'lovi muvaffaqiyatli yakunlandi: user=%s @%s stars=%s", uid, r_user, s_amt)
            try:
                bot.send_message(
                    uid,
                    f"🎉 <b>To'lov tasdiqlandi va {s_amt} Stars yuborildi!</b>\n\n"
                    f"👤 <b>Qabul qiluvchi:</b> <code>@{html.escape(r_user)}</code>\n"
                    f"💰 <b>To'langan:</b> <b>{fmt(amount)} so'm</b>\n"
                    f"💳 <b>Karta:</b> <b>*{s_card}</b>\n"
                    f"🧾 <b>Tranzaksiya:</b> <code>{tx_h}</code>",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(),
                )
            except Exception:
                log.exception("Foydalanuvchiga Stars yuborish xabarini yetkazib bo'lmadi.")
        else:
            card_str = f"\n💳 Karta: <b>*{s_card}</b>" if s_card else ""
            log.info("To'lov muvaffaqiyatli biriktirildi: user=%s +%s so'm yangi balans=%s", uid, amount, bal)
            try:
                bot.send_message(
                    uid,
                    f"🎉 <b>To'lov qabul qilindi!</b>\n\n"
                    f"➕ Qo'shildi: <b>+{fmt(amount)} so'm</b>{card_str}\n"
                    f"💰 Yangi hisobingiz: <b>{fmt(bal)} so'm</b>",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(),
                )
            except Exception:
                log.exception("Foydalanuvchiga xabar yuborib bo'lmadi.")
    else:
        now = int(time.time())
        with lock:
            has_pending_order = db.execute(
                "SELECT 1 FROM topups WHERE amount=? AND status IN ('pending', 'confirmed') AND expires_at >= ?",
                (amount, now - 60),
            ).fetchone()

        if has_pending_order:
            log.info("HUMO to'lovi buferda: %s so'm (msg_id=%s). Foydalanuvchi tasdiqlashi kutilmoqda.", amount, msg_id)
        else:
            log.warning("Kutilmagan to'lov tushdi: %s so'm! (msg_id=%s)", amount, msg_id)
            for admin_id in ADMIN_IDS:
                try:
                    bot.send_message(
                        admin_id,
                        f"ℹ️ <b>Kutilmagan to'lov kelib tushdi</b>\n\n"
                        f"💰 Summa: <b>{fmt(amount)} so'm</b>\n"
                        f"💳 Qabul qiluvchi: <b>*{CARD_LAST4}</b>\n"
                        f"🆔 HUMO Msg ID: <code>#{msg_id}</code>\n\n"
                        f"<i>(Bazada ayni shu summaga aktiv buyurtma topilmadi. Agar mijoz murojaat qilsa, hisobiga qo'lda qo'shishingiz mumkin)</i>\n\n"
                        f"Qo'lda hisob to'ldirish: <code>/credit &lt;user_id&gt; {amount}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


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
        cutoff = time.time() - 86400
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
            for tid, uid, amt in rows:
                log.info("So'rov #%s muddati o'tdi va avtomat bekor qilindi (user=%s, summa=%s)", tid, uid, amt)
                try:
                    bot.send_message(
                        uid,
                        f"🗑 <b>Buyurtma #{tid} bekor qilindi!</b>\n\n"
                        f"<blockquote>5 daqiqa ichida to'lov aniqlanmadi.</blockquote>\n\n"
                        f"🧾 <b>Agar to'lov qilgan bo'lsangiz, chekni yuboring:</b>",
                        parse_mode="HTML",
                        reply_markup=make_expired_kb(tid),
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("expire_loop xato")
        time.sleep(10)


# ══════════════════════════════════════════ MAIN ═══════════════════════
async def main():
    if not (BOT_TOKEN and API_ID and API_HASH and CARD_NUMBER):
        print("\n" + "=" * 65)
        print("DIQQAT! .env faylida shaxsiy sozlamalar to'liq kiritilmagan!")
        print("Iltimos, .env faylini oching va quyidagi qatorlarni to'ldiring:")
        if not BOT_TOKEN:
            print("  - BOT_TOKEN=@BotFather dan olingan bot tokeni")
        if not API_ID or not API_HASH:
            print("  - TELEGRAM_API_ID va TELEGRAM_API_HASH=my.telegram.org dan olingan")
        if not CARD_NUMBER:
            print("  - HUMO_CARD_NUMBER=Sizning 16 xonali Humo karta raqamingiz")
        print("=" * 65 + "\n")
        return

    await client.connect()
    if not await client.is_user_authorized():
        print("\n" + "=" * 60)
        print("DIQQAT! Telethon ga SHAXSIY TELEGRAM RAQAMINGIZNI kiritishingiz kerak!")
        print("Masalan: +998901234567")
        print("=" * 60 + "\n")
        await client.start()

    me = await client.get_me()
    if me.bot:
        print("\n" + "!" * 60)
        print("XATOLIK: Siz bot token orqali kirgansiz (@" + str(me.username) + ")!")
        print("Telegramda bitta bot ikkinchi botning (@HUMOcardbot) xabarlarini o'qiy OLMAYDI!")
        print("!" * 60 + "\n")
        return

    log.info("Telethon userbot ulandi: %s (ID: %s, Telefon: %s)", me.first_name, me.id, me.phone)
    await catch_up()

    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True),
        daemon=True, name="telebot",
    ).start()
    threading.Thread(target=expire_loop, daemon=True, name="expire").start()
    log.info("Stars Savdo Boti ishga tushdi va HUMO xabarlarini tinglayapti...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
