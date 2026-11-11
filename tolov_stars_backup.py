# -*- coding: utf-8 -*-
"""Hisob to'ldirish boti: pyTelegramBotAPI (telebot) + Telethon userbot.

Vazifasi:
- Foydalanuvchi summani kiritadi (masalan: 24 000 so'm).
- To'lov qilgach, "To'ladim" tugmasini bosib kartasining oxirgi 2 yoki 4 raqamini kiritadi.
- @HUMOcardbot xabarnomalari Telethon orqali avtomatik ushlanadi.
- Har bir xabarnoma (msg_id) qat'iy 1 marta (idempotent) qabul qilinadi.
- Faqat belgilangan kartaga (*3064) va faqat kirim (To'ldirish) xabarlari hisobga olinadi.
- To'lov tasdiqlangach, foydalanuvchi balansi darhol to'ldiriladi.
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
import time

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
# Karta oxirgi 4 raqami (agar kiritilmagan bo'lsa, karta raqamidan avtomat ajratib olinadi)
CARD_LAST4  = os.getenv("HUMO_CARD_LAST4", "").strip() or (
    CARD_NUMBER.replace(" ", "")[-4:] if len(CARD_NUMBER.replace(" ", "")) >= 4 else ""
)
HUMO_CHAT   = os.getenv("HUMO_CHAT", "HUMOcardbot").strip()

ORDER_TTL   = int(os.getenv("ORDER_TTL", "300"))       # 5 daqiqa (300 soniya)
MIN_AMOUNT  = int(os.getenv("MIN_AMOUNT", "1000"))
MAX_AMOUNT  = int(os.getenv("MAX_AMOUNT", "50000000"))

# Admin user ID lar: vergul bilan ajratilgan (masalan: 12345678,87654321)
_admin_raw  = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS   = [int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()]

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
""")

# Mavjud bazalar uchun ustunlarni tekshirish / qo'shish
for col, c_type in [("paid_at", "INTEGER"), ("sender_card", "TEXT"), ("humo_msg_id", "INTEGER")]:
    try:
        db.execute(f"ALTER TABLE topups ADD COLUMN {col} {c_type}")
    except Exception:
        pass


try:
    db.execute("ALTER TABLE seen ADD COLUMN processed_at INTEGER")
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
            "SELECT id, amount, status, created_at, sender_card FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (uid,),
        ).fetchall()


def create_topup(uid: int, amount: int):
    """Yangi to'ldirish buyurtmasi. Foydalanuvchining avvalgi to'lanmagan buyurtmalarini avtomat bekor qiladi."""
    now = int(time.time())
    with lock:
        # Bitta foydalanuvchida bir vaqtda faqat 1 ta aktiv buyurtma bo'lishi qoidasi
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
    """Foydalanuvchi 'To'ladim' deb kartasining oxirgi raqamlarini kiritganda chaqiriladi."""
    now = int(time.time())
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            # 1. Buyurtmani tekshirish
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

            # 5 daqiqadan o'tgan bo'lsa (muddati tugagan) qat'iy rad etish
            if status in ("expired", "cancelled") or expires_at < now:
                if status != "cancelled":
                    db.execute("UPDATE topups SET status='expired' WHERE id=?", (tid,))
                db.execute("COMMIT")
                return False, "expired"

            # Statusni confirmed ga o'tkazish va karta raqamini yozish
            db.execute(
                "UPDATE topups SET sender_card=?, status='confirmed' WHERE id=?",
                (card_digits, tid),
            )

            # 2. Avvalroq tushgan va biriktirilmagan to'lovlar orasidan qidirish (Unmatched buffer)
            unmatched = db.execute(
                "SELECT id, msg_id, created_at FROM unmatched_payments "
                "WHERE amount=? AND status='pending' AND created_at >= ? "
                "ORDER BY id ASC LIMIT 1",
                (amount, now - (ORDER_TTL + 120)),
            ).fetchone()

            if unmatched:
                u_id, msg_id, u_ts = unmatched
                # Darhol to'lovni tasdiqlash
                db.execute("UPDATE unmatched_payments SET status='claimed' WHERE id=?", (u_id,))
                db.execute(
                    "UPDATE topups SET status='paid', paid_at=?, humo_msg_id=? WHERE id=?",
                    (now, msg_id, tid),
                )
                ensure_user(uid)
                db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
                bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
                db.execute("COMMIT")
                return True, ("matched_immediately", amount, bal, msg_id)

            db.execute("COMMIT")
            return True, ("waiting_bank", amount, tid)
        except Exception:
            db.execute("ROLLBACK")
            raise


def claim_and_credit_humo(amount: int, msg_id: int, card_last4: str, raw_text: str, paid_ts: float):
    """
    HUMO xabari kelganda buyurtmani topib balansi to'ldiradi.
    QAT'IY QOIDA: Foydalanuvchi karta raqamini kiritmaguncha (status='confirmed' bo'lmaguncha)
    pul HЕCH QACHON hisobga qo'shilmaydi! Unmatched bufferga saqlanadi.
    """
    now = int(time.time())
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            # FAQAT VA FAQAT foydalanuvchi karta oxirgi 4 raqamini kiritgan (status='confirmed') buyurtmani qidirish
            row = db.execute(
                "SELECT id, user_id, sender_card FROM topups "
                "WHERE status='confirmed' AND amount=? AND expires_at >= ? "
                "ORDER BY id ASC LIMIT 1",
                (amount, now - 120),  # 2 daqiqa kechikish toleransi
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
                bal = db.execute("SELECT balance FROM users WHERE id=?", (uid := user_id,)).fetchone()[0]
                db.execute("COMMIT")
                return {
                    "matched": True,
                    "order_id": order_id,
                    "user_id": user_id,
                    "amount": amount,
                    "balance": bal,
                    "sender_card": sender_card,
                }

            # Agar foydalanuvchi hali karta raqamini kiritmagan bo'lsa,
            # pulni UNMATCHED_PAYMENTS buferiga saqlaymiz.
            # Foydalanuvchi karta raqamini kiritgan soniyada biriktiriladi!
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
    """Xabar avval ko'rilganmi? Bir martalik qat'iy himoya."""
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
    """
    HUMO bot xabari:
      🎉 To'ldirish
      ➕ 24.000,00 UZS
      📍 CLICK P2P UZCARD>Tos
      💳 HUMOCARD *3064
      🕓 14:19 09.11.2026
      💰 52.917,00 UZS

    Qat'iy qoidalar:
    1. Chiqim (💸 Amaliyot / ➖) bo'lsa -> rad etiladi.
    2. Karta CARD_LAST4 (*3064) ga mos bo'lmasa -> rad etiladi.
    3. Faqat musbat kirim summasi qaytariladi.
    """
    if not text:
        return None

    # Chiqim xabarlarini chetlab o'tish
    if re.search(r"(?:Amaliyot|[\u2796\-])", text, re.I) and not re.search(r"To['ʼ`]?ldirish", text, re.I):
        return None

    # Faqat To'ldirish / Kirim
    if not re.search(r"(?:To['ʼ`]?ldirish|[\u2795+\uff0b])", text, re.I):
        return None

    # Karta oxirgi 4 raqami tekshiruvi (*3064)
    if not re.search(rf"HUMOCARD[^\n]*\*{CARD_LAST4}", text, re.I):
        # Boshqa kartaga tushgan pulni ushlamaymiz
        return None

    # Summani ajratib olish
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
    if USE_MTPROTO and MTPROTO_HOST and MTPROTO_SECRET:
        proxy = (MTPROTO_HOST, MTPROTO_PORT, bytes.fromhex(MTPROTO_SECRET))
        log.info("MTProto proxy: %s:%s", MTPROTO_HOST, MTPROTO_PORT)
        return TelegramClient(
            "humo_session", api_id, api_hash,
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
        return TelegramClient("humo_session", api_id, api_hash, proxy=tuple(proxy_args))
    return TelegramClient("humo_session", api_id, api_hash)


client = _build_client()
humo_entity = None

# ══════════════════════════════════════════ BOT ════════════════════════
bot = telebot.TeleBot(BOT_TOKEN or "123456789:AAFDummyTokenForModuleImportSafety", parse_mode=None)

# Foydalanuvchi holatlari (FSM)
_state_lock = threading.Lock()
_waiting_amount: set = set()            # Summa kiritishini kutayotgan userlar
_waiting_card: dict = {}               # {user_id: topup_id} - Karta oxirgi raqamini kutayotganlar


def waiting_amount_add(uid: int):
    with _state_lock:
        _waiting_amount.add(uid)
        _waiting_card.pop(uid, None)


def waiting_amount_remove(uid: int):
    with _state_lock:
        _waiting_amount.discard(uid)


def waiting_amount_check(uid: int) -> bool:
    with _state_lock:
        return uid in _waiting_amount


def waiting_card_set(uid: int, tid: int):
    with _state_lock:
        _waiting_card[uid] = tid
        _waiting_amount.discard(uid)


def waiting_card_get(uid: int):
    with _state_lock:
        return _waiting_card.get(uid)


def waiting_card_remove(uid: int):
    with _state_lock:
        _waiting_card.pop(uid, None)


# ── Klaviaturalar ───────────────────────────────────────────────────────
def reply_main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("💰 Balansim", "💳 Hisob to'ldirish")
    kb.row("📋 To'lovlar tarixi")
    return kb


def menu_kb():
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
        f"Assalomu alaykum, <b>{html.escape(name)}</b>!\n\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"💰 Hisobingiz: <b>{fmt(bal)} so'm</b>\n\n"
        f"Quyidagi menyudan kerakli bo'limni tanlang:"
    )


def balance_text(uid: int, name: str = "") -> str:
    bal, total_paid, recent_paid = get_user_stats(uid)
    display_name = html.escape(str(name or uid))
    text = (
        f"👤 <b>Foydalanuvchi:</b> {display_name}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
        f"💰 <b>Joriy hisobingiz:</b> <b>{fmt(bal)} so'm</b>\n"
        f"📥 <b>Jami tushgan pul:</b> <b>{fmt(total_paid)} so'm</b>\n"
    )
    if recent_paid:
        text += "\n📋 <b>So'nggi muvaffaqiyatli to'lovlar:</b>\n"
        for amt, ts in recent_paid:
            t_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            text += f"  ✅ <b>+{fmt(amt)} so'm</b> <i>({t_str})</i>\n"
    else:
        text += "\n<i>(Hozircha to'lovlar mavjud emas)</i>\n"
    return text


def history_text(uid: int) -> str:
    rows = get_all_topups_history(uid)
    if not rows:
        return "📋 Sizda hali buyurtmalar tarixi mavjud emas."
    text = "📋 <b>So'nggi buyurtmalar holati:</b>\n\n"
    for tid, amt, status, c_at, s_card in rows:
        icon = {"paid": "✅", "confirmed": "⏳", "pending": "🕒", "expired": "❌", "cancelled": "🚫"}.get(status, "❓")
        status_name = {
            "paid": "To'langan",
            "confirmed": "Tasdiqlanmoqda",
            "pending": "Kutilmoqda",
            "expired": "Muddati o'tgan",
            "cancelled": "Bekor qilingan",
        }.get(status, status)
        card_info = f" (karta: *{s_card})" if s_card else ""
        t_str = time.strftime("%d.%m %H:%M", time.localtime(c_at))
        text += f"{icon} <b>#{tid}</b> | <b>{fmt(amt)} so'm</b> | {status_name}{card_info} <i>({t_str})</i>\n"
    return text


# ── Bot handlerlari ─────────────────────────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    waiting_amount_remove(m.from_user.id)
    waiting_card_remove(m.from_user.id)
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
    waiting_amount_remove(m.from_user.id)
    waiting_card_remove(m.from_user.id)
    bot.send_message(
        m.chat.id,
        balance_text(m.from_user.id, m.from_user.full_name),
        parse_mode="HTML",
        reply_markup=balance_kb(),
    )


@bot.message_handler(func=lambda m: m.text and "balans" in m.text.lower())
def text_balance(m):
    cmd_balance(m)


@bot.message_handler(func=lambda m: m.text and "tarix" in m.text.lower())
def text_history(m):
    waiting_amount_remove(m.from_user.id)
    waiting_card_remove(m.from_user.id)
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
    waiting_amount_add(m.from_user.id)
    bot.send_message(
        m.chat.id,
        f"💵 <b>To'ldirish summasini kiriting (so'mda):</b>\n\n"
        f"Minimal: <b>{fmt(MIN_AMOUNT)} so'm</b>\n"
        f"Maksimal: <b>{fmt(MAX_AMOUNT)} so'm</b>",
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
            reply_markup=menu_kb(),
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



@bot.callback_query_handler(func=lambda c: c.data == "menu")
def cb_menu(c):
    waiting_amount_remove(c.from_user.id)
    waiting_card_remove(c.from_user.id)
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
    waiting_amount_remove(c.from_user.id)
    waiting_card_remove(c.from_user.id)
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
    waiting_amount_remove(c.from_user.id)
    waiting_card_remove(c.from_user.id)
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
    waiting_amount_add(c.from_user.id)
    try:
        bot.edit_message_text(
            f"💵 <b>To'ldirish summasini kiriting (so'mda):</b>\n\n"
            f"Minimal: <b>{fmt(MIN_AMOUNT)} so'm</b>\n"
            f"Maksimal: <b>{fmt(MAX_AMOUNT)} so'm</b>",
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
    except Exception:
        pass


    bot.answer_callback_query(c.id)


# ── 1-qadam: Foydalanuvchi summani kiritadi ─────────────────────────────
@bot.message_handler(content_types=["text"], func=lambda m: waiting_amount_check(m.from_user.id))
def got_amount(m):
    txt = (m.text or "").replace(" ", "").replace(",", "").replace(".", "")
    if not txt.isdigit() or not (MIN_AMOUNT <= int(txt) <= MAX_AMOUNT):
        bot.send_message(
            m.chat.id,
            f"❌ Faqat {fmt(MIN_AMOUNT)} dan {fmt(MAX_AMOUNT)} gacha butun son kiriting:",
        )
        return

    amount = int(txt)
    waiting_amount_remove(m.from_user.id)
    tid, amount = create_topup(m.from_user.id, amount)

    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        kb.add(types.InlineKeyboardButton(
            "📋 Kartani nusxalash",
            copy_text=types.CopyTextButton(text=CARD_NUMBER.replace(" ", "")),
        ))
    except AttributeError:
        pass
    kb.add(types.InlineKeyboardButton("✅ To'ladim (Karta raqamini kiritish)", callback_data=f"paid:{tid}"))
    kb.add(types.InlineKeyboardButton("🗑 Bekor qilish", callback_data=f"cancel:{tid}"))
    kb.add(types.InlineKeyboardButton("↩️ Bosh menyu", callback_data="menu"))

    bot.send_message(
        m.chat.id,
        f"💳 <b>To'ldirish buyurtmasi #{tid}</b>\n\n"
        f"🔹 Karta: <code>{CARD_NUMBER}</code>\n"
        + (f"🔹 Egasi: <b>{CARD_OWNER}</b>\n" if CARD_OWNER else "")
        + f"💰 To'lov summasi: <b>{fmt(amount)} so'm</b>\n\n"
        f"⏱ To'lov <b>{ORDER_TTL // 60} daqiqa</b> amal qiladi.\n\n"
        f"⚠️ <b>Ko'rsatma:</b>\n"
        f"1. Yuqoridagi kartaga <b>{fmt(amount)} so'm</b> o'tkazing.\n"
        f"2. Pul o'tkazgach, pastdagi <b>'✅ To'ladim'</b> tugmasini bosing va o'tkazma qilgan kartangizning <b>oxirgi 4 ta raqamini</b> kiriting.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── 2-qadam: Foydalanuvchi "To'ladim" tugmasini bosadi ──────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("paid:"))
def cb_paid(c):
    tid = int(c.data.split(":")[1])
    # Foydalanuvchini karta raqami kiritish holatiga o'tkazish
    waiting_card_set(c.from_user.id, tid)

    # Buyurtma summasini bazadan olish
    with lock:
        order_row = db.execute("SELECT amount FROM topups WHERE id=?", (tid,)).fetchone()
    amt_str = f"💰 To'lov summasi: <b>{fmt(order_row[0])} so'm</b>\n\n" if order_row else ""

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🗑 Bekor qilish", callback_data=f"cancel:{tid}"))
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
        bot.edit_message_text(
            prompt_text,
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        bot.send_message(
            c.message.chat.id,
            prompt_text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    bot.answer_callback_query(c.id)


# ── 3-qadam: Foydalanuvchi kartasining oxirgi raqamlarini kiritadi ───────
@bot.message_handler(content_types=["text"], func=lambda m: bool(waiting_card_get(m.from_user.id)))
def got_card_digits(m):
    uid = m.from_user.id
    tid = waiting_card_get(uid)
    raw_digits = (m.text or "").strip().replace(" ", "").replace("*", "").replace("-", "")

    # Qat'iy 4 ta raqam talabi (bank standarti)
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
    except Exception as e:
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
                reply_markup=menu_kb(),
            )
        else:
            bot.send_message(m.chat.id, f"❌ Xatolik: {result}", reply_markup=menu_kb())
        return

    if result == "already_paid":
        bot.send_message(
            m.chat.id,
            f"✅ Buyurtma #{tid} allaqachon to'langan!",
            reply_markup=menu_kb(),
        )
        return

    status_type = result[0]
    if status_type == "matched_immediately":
        _, amt, new_bal, msg_id = result
        bot.send_message(
            m.chat.id,
            f"🎉 <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"➕ Qo'shildi: <b>+{fmt(amt)} so'm</b>\n"
            f"💳 Karta: <b>*{raw_digits}</b>\n"
            f"💰 Yangi balansingiz: <b>{fmt(new_bal)} so'm</b>",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
    elif status_type == "waiting_bank":
        _, amt, _ = result
        bot.send_message(
            m.chat.id,
            f"⏳ <b>To'lov tasdiqlanishi kutilmoqda...</b>\n\n"
            f"📋 Buyurtma: <b>#{tid}</b>\n"
            f"💰 Summa: <b>{fmt(amt)} so'm</b>\n"
            f"💳 Karta oxiri: <b>*{raw_digits}</b>\n\n"
            f"Bankdan to'lov xabari kelishi bilan balansingiz avtomatik to'ldiriladi (odatda 5–30 soniya ichida).",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(c):
    tid = int(c.data.split(":")[1])
    waiting_amount_remove(c.from_user.id)
    waiting_card_remove(c.from_user.id)
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
    log.info("HUMO xabar keldi: msg_id=%s | %s", msg_id, text[:60].replace("\n", " "))

    # 1-darajali himoya: Bir martalik qat'iy ko'rilganlik (Idempotent seen)
    if not mark_seen(msg_id):
        log.info("Xabar #%s allaqachon qayta ishlangan. O'tkazib yuborildi.", msg_id)
        return

    # 2-darajali filtrlash: Faqat to'ldirish, faqat *3064 kartaga
    amount = parse_humo_message(text)
    if amount is None:
        log.info("msg_id=%s: ushbu botga tegishli to'ldirish emas (boshqa karta yoki chiqim).", msg_id)
        return

    log.info("HUMO qonuniy kirim aniqlandi: %s so'm (*%s kartasiga)", amount, CARD_LAST4)

    # 3-darajali moslashtirish: Buyurtma bilan bog'lash va kreditlash
    res = claim_and_credit_humo(amount, msg_id, CARD_LAST4, text, ts)

    if res["matched"]:
        uid = res["user_id"]
        bal = res["balance"]
        s_card = res.get("sender_card")
        card_str = f"\n💳 Karta: <b>..{s_card}</b>" if s_card else ""
        log.info("To'lov muvaffaqiyatli biriktirildi: user=%s +%s so'm yangi balans=%s", uid, amount, bal)
        try:
            bot.send_message(
                uid,
                f"🎉 <b>To'lov qabul qilindi!</b>\n\n"
                f"➕ Qo'shildi: <b>+{fmt(amount)} so'm</b>{card_str}\n"
                f"💰 Yangi hisobingiz: <b>{fmt(bal)} so'm</b>",
                parse_mode="HTML",
                reply_markup=menu_kb(),
            )
        except Exception:
            log.exception("Foydalanuvchiga xabar yuborib bo'lmadi.")
    else:
        # Hozircha to'lov buferda saqlandi (foydalanuvchi karta raqamini kiritishi kutilmoqda)
        now = int(time.time())
        with lock:
            has_pending_order = db.execute(
                "SELECT 1 FROM topups WHERE amount=? AND status IN ('pending', 'confirmed') AND expires_at >= ?",
                (amount, now - 60),
            ).fetchone()

        if has_pending_order:
            # Foydalanuvchi ayni paytda karta raqamini kiritayotgani sababli ortiqcha shovqin qilmaymiz
            log.info("HUMO to'lovi buferda: %s so'm (msg_id=%s). Foydalanuvchi tasdiqlashi kutilmoqda.", amount, msg_id)
        else:
            # Bazada bunday aktiv buyurtma umuman yo'q (chindan ham kutilmagan to'lov)
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
            for tid, uid, amt in rows:
                log.info("So'rov #%s muddati o'tdi va avtomat bekor qilindi (user=%s, summa=%s)", tid, uid, amt)
                try:
                    bot.send_message(
                        uid,
                        f"⏰ <b>Buyurtma #{tid} bekor qilindi (5 daqiqa o'tdi).</b>\n\n"
                        f"💰 Summa: <b>{fmt(amt)} so'm</b>\n\n"
                        f"To'lovning 5 daqiqalik amal qilish muddati tugadi. Xavfsizlik maqsadida ushbu buyurtma avtomatik yopildi.\n\n"
                        f"• Qaytadan to'ldirish uchun: <b>/menu</b> orqali yangi hisob to'ldirishni tanlang.\n"
                        f"• <i>Agar to'lovni amalga oshirgan bo'lsangiz, xavotir olmang — to'lovingiz tizimda saqlangan. Adminga murojaat qilsangiz zudlik bilan biriktirib beradi.</i>",
                        parse_mode="HTML",
                        reply_markup=menu_kb(),
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("expire_loop xato")
        time.sleep(10)  # Har 10 soniyada muddati o'tgan buyurtmalarni tekshiradi


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
    log.info("Bot ishga tushdi va HUMO xabarlarini tinglayapti...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
