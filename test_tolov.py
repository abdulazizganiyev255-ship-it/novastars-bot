# -*- coding: utf-8 -*-
"""Tolov.py uchun avtomatlashtirilgan Red-Team unit testlari."""

import os
import sqlite3
import sys
import unittest
import time

# Loyiha papkasini yuklash
sys.path.insert(0, r"c:\Users\ASUS\OneDrive\Desktop\tolovapi")

import tolov


class TestTolovP2PIntegration(unittest.TestCase):
    def setUp(self):
        # Test uchun sozlamalar
        tolov.CARD_LAST4 = "3064"
        tolov.CARD_NUMBER = "9860 3501 4601 3064"

        # Alohida test DB
        self.test_db_path = "test_balans.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

        tolov.db = sqlite3.connect(self.test_db_path, isolation_level=None, check_same_thread=False)
        tolov.db.executescript("""
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
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id     INTEGER PRIMARY KEY,
            recipient   TEXT,
            stars       INTEGER,
            price       INTEGER,
            updated_at  INTEGER
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
        for col, c_type in [("username", "TEXT"), ("full_name", "TEXT"), ("phone", "TEXT"), ("created_at", "INTEGER"), ("updated_at", "INTEGER")]:
            try:
                tolov.db.execute(f"ALTER TABLE users ADD COLUMN {col} {c_type}")
            except Exception:
                pass

    def tearDown(self):
        tolov.db.close()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_amount_not_altered(self):
        """Foydalanuvchi summasi o'zgarmasligi kerak (amount += 1 xatosi yo'qolgan)."""
        uid1 = 111111
        uid2 = 222222
        tid1, amt1 = tolov.create_topup(uid1, 24000)
        tid2, amt2 = tolov.create_topup(uid2, 24000)

        self.assertEqual(amt1, 24000)
        self.assertEqual(amt2, 24000)
        self.assertNotEqual(tid1, tid2)

    def test_humo_parser_target_card_and_income_only(self):
        """Faqat belgilangan karta (*3064) va faqat kirim xabarlari o'tishi kerak."""
        # 1. To'g'ri kirim
        valid_msg = (
            "🎉 To'ldirish\n"
            "➕ 24.000,00 UZS\n"
            "📍 CLICK P2P UZCARD>Tos\n"
            "💳 HUMOCARD *3064\n"
            "🕓 14:19 09.11.2026\n"
            "💰 52.917,00 UZS"
        )
        self.assertEqual(tolov.parse_humo_message(valid_msg), 24000)

        # 2. Boshqa kartaga tushgan kirim (*1959) -> RAD ETILISHI KERAK
        other_card = (
            "🎉 To'ldirish\n"
            "➕ 4.000,00 UZS\n"
            "📍 CLICK P2P HUMO ANY\n"
            "💳 HUMOCARD *1959\n"
            "🕓 15:32 09.11.2026\n"
            "💰 4.127,88 UZS"
        )
        self.assertIsNone(tolov.parse_humo_message(other_card))

        # 3. Chiqim amaliyoti (💸 Amaliyot / ➖) -> RAD ETILISHI KERAK
        expense_msg = (
            "💸 Amaliyot\n"
            "➖ 4.044,00 UZS\n"
            "📍 CLICK P2P HUMO ANY\n"
            "💳 HUMOCARD *3064\n"
            "🕓 15:32 09.11.2026\n"
            "💰 50.873,00 UZS"
        )
        self.assertIsNone(tolov.parse_humo_message(expense_msg))

    def test_seen_idempotency(self):
        """Har bir Humo msg_id qat'iy bir marta ko'rilishi shart."""
        msg_id = 999888
        self.assertTrue(tolov.mark_seen(msg_id))
        # 2-marta ko'rilganda False qaytarishi shart
        self.assertFalse(tolov.mark_seen(msg_id))

    def test_flow_user_confirms_first_then_humo_arrives(self):
        """Oqim 1: Foydalanuvchi oldin 'To'ladim' deb karta raqamini kiritadi, keyin Humo xabari keladi."""
        uid = 555001
        tid, _ = tolov.create_topup(uid, 50000)

        # Foydalanuvchi kartasining oxirgi 4 raqamini kiritadi (qat'iy 4 ta raqam)
        ok, res = tolov.record_user_payment_confirmation(tid, uid, "8421")
        self.assertTrue(ok)
        self.assertEqual(res[0], "waiting_bank")

        # Humo botdan xabar keldi
        msg_id = 7001
        claim_res = tolov.claim_and_credit_humo(50000, msg_id, "3064", "🎉 To'ldirish ➕ 50.000 UZS", time.time())
        self.assertTrue(claim_res["matched"])
        self.assertEqual(claim_res["user_id"], uid)
        self.assertEqual(claim_res["balance"], 50000)
        self.assertEqual(claim_res["sender_card"], "8421")

        # Foydalanuvchi balansi bazada tekshiriladi
        self.assertEqual(tolov.get_balance(uid), 50000)

    def test_flow_humo_arrives_first_then_user_confirms(self):
        """Oqim 2: Bank xabari oldin kelganda ham, foydalanuvchi 4 raqamni kiritmaguncha pul qo'shilmasligi shart!"""
        uid = 555002
        tid, _ = tolov.create_topup(uid, 35000)

        # Humo bot xabari keldi (hali user confirm bosmadi)
        msg_id = 7002
        claim_res = tolov.claim_and_credit_humo(35000, msg_id, "3064", "🎉 To'ldirish ➕ 35.000 UZS", time.time())
        # QAT'IY TALAB: Karta raqami kiritilmagani uchun pul HЕCH QACHON qo'shilmaydi!
        self.assertFalse(claim_res["matched"])
        self.assertEqual(tolov.get_balance(uid), 0)

        # Endi foydalanuvchi kartasining oxirgi 4 raqamini kiritadi
        ok, res = tolov.record_user_payment_confirmation(tid, uid, "9432")
        self.assertTrue(ok)
        self.assertEqual(res[0], "matched_immediately")
        self.assertEqual(res[1], 35000)
        self.assertEqual(tolov.get_balance(uid), 35000)

    def test_unmatched_stored_and_matched_on_late_confirm(self):
        """Oqim 3: Pul kelgan paytda umuman topup bo'lmagan, lekin user 30 soniyadan so'ng kiritdi."""
        # 1. Kutilmagan pul tushdi
        msg_id = 7003
        claim_res = tolov.claim_and_credit_humo(12000, msg_id, "3064", "🎉 To'ldirish ➕ 12.000 UZS", time.time())
        self.assertFalse(claim_res["matched"])

        # 2. User 10 soniyadan so'ng buyurtma qilib 'To'ladim' dedi
        uid = 555003
        tid, _ = tolov.create_topup(uid, 12000)
        ok, res = tolov.record_user_payment_confirmation(tid, uid, "8421")
        self.assertTrue(ok)
        self.assertEqual(res[0], "matched_immediately")
        self.assertEqual(res[1], 12000)  # amount
        self.assertEqual(res[2], 12000)  # new balance
        self.assertEqual(tolov.get_balance(uid), 12000)

    def test_stars_purchase_with_balance(self):
        """Stars sotib olish: Balans yetarli bo'lganda darhol yetkaziladi."""
        uid = 555004
        tolov.admin_credit(uid, 50000)
        self.assertEqual(tolov.get_balance(uid), 50000)

        # 100 Stars xarid qilindi (20 000 so'm)
        price = 20000
        stars = 100
        with tolov.lock:
            tolov.db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (price, uid))
            cur = tolov.db.execute(
                "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, created_at) "
                "VALUES('STAR-TEST', ?, 'Abdulaziz_05_0', ?, ?, 'pending', 'balance', ?)",
                (uid, stars, price, int(time.time())),
            )
            s_id = cur.lastrowid
            tx = tolov.deliver_stars_fulfillment(s_id, uid, "Abdulaziz_05_0", stars, price, "balance")

        self.assertTrue(tx.startswith("tx_ton_"))
        self.assertEqual(tolov.get_balance(uid), 30000)
        order = tolov.db.execute("SELECT status, tx_hash FROM stars_orders WHERE id=?", (s_id,)).fetchone()
        self.assertEqual(order[0], "delivered")
        self.assertEqual(order[1], tx)

    def test_stars_purchase_with_humo_card(self):
        """Stars sotib olish: Humo orqali to'langanda avtomat Stars yetkaziladi."""
        uid = 555005
        price = 20000
        stars = 100

        # 1. Topup va Stars order yaratildi
        tid, _ = tolov.create_topup(uid, price)
        with tolov.lock:
            cur = tolov.db.execute(
                "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, topup_id, created_at) "
                "VALUES('STAR-CARD-TEST', ?, 'Abdulaziz_05_0', ?, ?, 'pending', 'humo_card', ?, ?)",
                (uid, stars, price, tid, int(time.time())),
            )
            s_id = cur.lastrowid

        # 2. Foydalanuvchi kartasining 4 raqamini kiritdi
        ok, res = tolov.record_user_payment_confirmation(tid, uid, "9432")
        self.assertTrue(ok)
        self.assertEqual(res[0], "waiting_bank")

        # 3. Humo botdan to'lov xabari keldi
        msg_id = 7004
        claim_res = tolov.claim_and_credit_humo(price, msg_id, "3064", "🎉 To'ldirish ➕ 20.000 UZS", time.time())
        self.assertTrue(claim_res["matched"])
        self.assertIsNotNone(claim_res["stars_data"])
        self.assertEqual(claim_res["stars_data"]["stars_amount"], 100)
        self.assertEqual(claim_res["stars_data"]["recipient"], "Abdulaziz_05_0")

        # Stars buyurtmasi yetkazilganligini tekshirish
        order = tolov.db.execute("SELECT status, tx_hash FROM stars_orders WHERE id=?", (s_id,)).fetchone()
        self.assertEqual(order[0], "delivered")
        self.assertTrue(order[1].startswith("tx_ton_"))

    def test_admin_receipt_approval(self):
        """Mijoz muddati o'tgan buyurtmaga chek yuklaganda, admin tasdiqlashi bilan Stars yetkaziladi."""
        uid = 555006
        admin_id = 6871410879
        tolov.ADMIN_IDS = [admin_id]
        price = 21000
        stars = 100

        # 1. Buyurtma yaratilib muddati o'tdi (expired)
        tid, _ = tolov.create_topup(uid, price)
        with tolov.lock:
            tolov.db.execute("UPDATE topups SET status='expired' WHERE id=?", (tid,))
            cur = tolov.db.execute(
                "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, topup_id, created_at) "
                "VALUES('STAR-RECEIPT-TEST', ?, 'Abdulaziz_05_0', ?, ?, 'pending', 'humo_card', ?, ?)",
                (uid, stars, price, tid, int(time.time())),
            )
            s_id = cur.lastrowid
            tolov.db.execute(
                "INSERT INTO receipts(order_id, user_id, file_id, status, created_at) VALUES(?,?,?, 'pending', ?)",
                (tid, uid, "photo_file_123", int(time.time())),
            )

        # 2. Admin tasdiqlash mock chaqiruvi
        now = int(time.time())
        with tolov.lock:
            tolov.db.execute("UPDATE topups SET status='paid', paid_at=? WHERE id=?", (now, tid))
            tolov.db.execute("UPDATE receipts SET status='approved', reviewed_at=?, reviewed_by=? WHERE order_id=?", (now, admin_id, tid))
            tx_h = tolov.deliver_stars_fulfillment(s_id, uid, "Abdulaziz_05_0", stars, price, "admin_approved")

        self.assertTrue(tx_h.startswith("tx_ton_"))
        order = tolov.db.execute("SELECT status, tx_hash FROM stars_orders WHERE id=?", (s_id,)).fetchone()
        self.assertEqual(order[0], "delivered")
        self.assertEqual(order[1], tx_h)

        receipt = tolov.db.execute("SELECT status FROM receipts WHERE order_id=?", (tid,)).fetchone()
        self.assertEqual(receipt[0], "approved")

    def test_profit_guarantee_1000_som(self):
        """Barcha paketlar va custom buyurtmalar kamida 1 000 so'm sof foyda keltirishi shart!"""
        for s_amt, price in tolov.STAR_PACKAGES:
            cost = s_amt * tolov.STAR_COST_PER_UNIT
            profit = price - cost
            self.assertGreaterEqual(
                profit, 1000,
                f"Paket {s_amt} Stars sof foydasi kamida 1 000 so'm bo'lishi shart! Hozir: {profit} so'm"
            )

        # Custom buyurtmalar tekshiruvi: 10 ta Stars uchun ham
        for custom_stars in [10, 25, 77, 150, 350, 750]:
            p = tolov.calculate_custom_stars_price(custom_stars)
            c = custom_stars * tolov.STAR_COST_PER_UNIT
            self.assertGreaterEqual(p - c, 1000)

    def test_stars_session_persistence_and_no_expiration_popup(self):
        """Sessiya RAM dan o'chib ketsa ham SQLite'dan tiklanishi va xato bermasligi shart."""
        uid = 555007
        tolov.save_stars_session(uid, recipient="testuser77", stars=250, price=52500)

        # RAM'dagi xotirani butunlay tozalaymiz (bot restart holati)
        with tolov._state_lock:
            tolov._stars_session.clear()

        # Tiklab olish tekshiruvi
        sess = tolov.get_stars_session(uid)
        self.assertEqual(sess["recipient"], "testuser77")
        self.assertEqual(sess["stars"], 250)
        self.assertEqual(sess["price"], 52500)

        # Umuman bo'lmagan user uchun ham xato bermasdan xavfsiz standart berishi shart
        dummy_sess = tolov.get_stars_session(999999)
        self.assertIsNotNone(dummy_sess)
        self.assertIn("price", dummy_sess)
        self.assertIn("stars", dummy_sess)

    def test_payment_method_kb_only_balance_and_card(self):
        """To'lov usuli menyusida FAQAT Balans va Karta bo'lishi, Paynet bo'lmasligi shart."""
        kb = tolov.payment_method_kb(stars=100, price=21000, recipient="abdulaziz")
        buttons = [btn for row in kb.keyboard for btn in row]
        button_texts = [btn.text for btn in buttons]

        # Faqat Balans, Karta va Orqaga
        self.assertTrue(any("Balans" in t for t in button_texts))
        self.assertTrue(any("Karta" in t for t in button_texts))
        self.assertTrue(any("Orqaga" in t for t in button_texts))

        # Paynet mutlaqo bo'lmasligi shart
        self.assertFalse(any("Paynet" in t for t in button_texts))

    def test_ensure_user_and_get_display(self):
        """Foydalanuvchi ma'lumotlari to'g'ri saqlanishi va chiroyli formatlanishi shart."""
        uid = 555008
        tolov.ensure_user(uid, username="azik_pro", full_name="Azizbek", phone="+998901234567")
        display = tolov.get_user_display(uid)

        self.assertIn("Azizbek", display)
        self.assertIn("@azik_pro", display)
        self.assertIn(str(uid), display)
        self.assertIn("+998901234567", display)

    def test_referral_reward_system(self):
        """Do'st taklif qilinganda, uning xarididan 5% naqd bonus berilishi va bazaga yozilishi shart."""
        referrer_id = 777001
        buyer_id = 777002
        tolov.ensure_user(referrer_id, username="referrer_boss", full_name="Referrer Boss")
        tolov.ensure_user(buyer_id, username="buyer_friend", full_name="Buyer Friend")

        # 1. Referrer do'stini taklif qildi
        tolov.save_referral(referrer_id, buyer_id)

        # Boshlang'ich balans 0 so'm
        self.assertEqual(tolov.get_balance(referrer_id), 0)

        # 2. Xaridor 100 Stars xarid qildi (21 000 so'm)
        stars = 100
        price = 21000
        with tolov.lock:
            cur = tolov.db.execute(
                "INSERT INTO stars_orders(order_code, user_id, recipient_username, stars_amount, price_uzs, status, payment_method, created_at) "
                "VALUES('STAR-TEST-REF', ?, 'buyer_friend', ?, ?, 'pending', 'card', ?)",
                (buyer_id, stars, price, int(time.time())),
            )
            s_id = cur.lastrowid
            tolov.deliver_stars_fulfillment(s_id, buyer_id, "buyer_friend", stars, price, "card")

        # 3. Referrer balansiga 5% (1 050 so'm) qo'shilganini tekshirish
        expected_bonus = int(price * 0.05)  # 1 050 so'm
        self.assertEqual(tolov.get_balance(referrer_id), expected_bonus)

        # 4. referral_rewards jadvalida qayd etilganini tekshirish
        row = tolov.db.execute(
            "SELECT referrer_id, buyer_id, stars_amount, bonus_amount FROM referral_rewards WHERE referrer_id=?",
            (referrer_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], referrer_id)
        self.assertEqual(row[1], buyer_id)
        self.assertEqual(row[2], stars)
        self.assertEqual(row[3], expected_bonus)

        # 5. Referrer statistikasi
        count, total_bonus = tolov.get_referral_stats(referrer_id)
        self.assertEqual(count, 1)
        self.assertEqual(total_bonus, expected_bonus)



if __name__ == "__main__":
    unittest.main()
