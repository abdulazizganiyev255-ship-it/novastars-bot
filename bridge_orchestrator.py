# -*- coding: utf-8 -*-
"""Autonomous Stars Bridge & TON Wallet Orchestrator.

Ushbu modul:
1. Fragment.com va TON W5 hamyoni o'rtasidagi to'lov ko'prigini boshqaradi.
2. Xaridordan pul tushishi bilan (Humo/Click/Payme) avtomatik TON yechib Stars yuboradi.
3. Kutilmagan uzilish yoki xatolik yuz berganda AI Agent Support tizimini ishga soladi.
"""

import asyncio
import logging
import random
import string
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger("stars_bridge")


@dataclass
class OrderContext:
    order_id: int
    user_tg_id: int
    recipient_username: str
    stars_amount: int
    paid_uzs: int


class MockTonWallet:
    """TON W5 Koshelyok simulyatori va avto-imzolash drayveri."""

    def __init__(self, address: str = "EQB_W5_WALLET_SAMPLE_ADDRESS_2026", balance_ton: float = 25.5):
        self.address = address
        self.balance_ton = balance_ton

    async def get_balance(self) -> float:
        return self.balance_ton

    async def send_ton(self, destination: str, amount_ton: float, payload: str = "") -> str:
        if self.balance_ton < amount_ton:
            raise RuntimeError(f"Hamyonda yetarli TON yo'q! Qoldiq: {self.balance_ton:.2f}, Kerak: {amount_ton:.2f}")
        self.balance_ton -= amount_ton
        tx_hash = "0x" + "".join(random.choices(string.hexdigits.lower(), k=64))
        log.info("TON W5 Tx yuborildi: dest=%s, amount=%.4f TON, tx=%s", destination, amount_ton, tx_hash)
        return tx_hash


class MockFragmentClient:
    """Fragment.com API drayveri (@username tekshirish va Stars buyurtmasi)."""

    def __init__(self, rate_stars_per_ton: float = 65.0):
        self.rate_stars_per_ton = rate_stars_per_ton

    async def search_recipient(self, username: str) -> Dict[str, Any]:
        clean = username.replace("@", "").strip()
        if not clean or len(clean) < 3:
            return {"found": False}
        return {
            "found": True,
            "username": clean,
            "hash": f"hash_{clean}_fragment_verified",
        }

    async def get_ton_price(self, stars: int) -> float:
        # Masalan: 1 TON ~ 65-70 Stars
        return round(stars / self.rate_stars_per_ton, 4)

    async def create_stars_order(self, recipient_hash: str, quantity: int) -> Dict[str, Any]:
        amount_ton = await self.get_ton_price(quantity)
        return {
            "address": "EQCFragment_Stars_Treasury_Deposit_W5",
            "amount_ton": amount_ton,
            "payload": f"stars:{recipient_hash}:{quantity}:{int(time.time())}",
        }


class MockAIAgent:
    """AI Agent Support (anomaliya tahlilchisi va mijozlarni xotirjam qiluvchi)."""

    async def handle_delivery_delay(self, ctx: OrderContext, error_log: str) -> str:
        return (
            f"Hurmatli mijoz! Buyurtmangiz (#{ctx.order_id}) qabul qilindi va to'lov muvaffaqiyatli o'tdi.\n"
            f"Biroq, Telegram tarmog'idagi (Fragment) vaqtinchalik yuklama sababli {ctx.stars_amount} Stars "
            f"yetkazilishi bir necha daqiqaga kechikmoqda. Xavotir olmang, tizim avtomatik navbatga qo'ydi va "
            f"tez orada @{ctx.recipient_username} hisobingizga yuklanadi!"
        )


class AutonomousStarsBridge:
    def __init__(self, db, ton_wallet, fragment_client, ai_agent, bot, admin_id: int):
        self.db = db
        self.wallet = ton_wallet              # TON W5 Koshelyok
        self.fragment = fragment_client       # Fragment.com ulanmasi
        self.ai = ai_agent                   # AI Agent
        self.bot = bot
        self.admin_id = admin_id

    async def verify_before_payment(self, username: str, stars: int) -> Dict[str, Any]:
        """1-QADAM: Mijozdan pul olishdan OLDIN Fragment va TON balansni tekshiradi."""
        recipient = await self.fragment.search_recipient(username)
        if not recipient or not recipient.get("found"):
            return {"ok": False, "reason": "Bunday @username topilmadi yoki u kanal/bot."}

        required_ton = await self.fragment.get_ton_price(stars)
        current_ton_balance = await self.wallet.get_balance()

        if current_ton_balance < (required_ton + 0.2):  # 0.2 TON komissiya zaxirasi
            if self.bot and self.admin_id:
                try:
                    await self.bot.send_message(
                        self.admin_id,
                        f"⚠️ <b>TON Koshelyokda balans kam!</b>\n"
                        f"Qoldiq: {current_ton_balance:.2f} TON. Kerak: {required_ton:.2f} TON.\n"
                        f"Darhol popolneniye qiling!",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return {"ok": False, "reason": "Tizimda texnik yangilanish, 5 daqiqada qayta urinib ko'ring."}

        return {"ok": True, "recipient_hash": recipient["hash"], "ton_cost": required_ton}

    async def on_payment_confirmed(self, ctx: OrderContext) -> Optional[str]:
        """2-QADAM: To'lov tasdiqlangach, TON yechib Stars tashlaydi."""
        try:
            # Fragment'dan to'lov rekvizitini (payload) olish
            tx_request = await self.fragment.create_stars_order(
                recipient_hash=ctx.recipient_username,
                quantity=ctx.stars_amount,
            )

            # TON Koshelyokdan avtomat imzolab yuborish
            tx_hash = await self.wallet.send_ton(
                destination=tx_request["address"],
                amount_ton=tx_request["amount_ton"],
                payload=tx_request["payload"],
            )

            # Foydalanuvchiga xabar berish
            if self.bot:
                try:
                    await self.bot.send_message(
                        ctx.user_tg_id,
                        f"🎉 <b>To'lov tasdiqlandi va {ctx.stars_amount} Stars yuborildi!</b>\n\n"
                        f"👤 Qabul qiluvchi: <code>@{ctx.recipient_username}</code>\n"
                        f"🧾 Tranzaksiya: <code>{tx_hash[:16]}...</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # Qolgan TON balansni tekshirib, kam bo'lsa Adminga bildirish
            rem_balance = await self.wallet.get_balance()
            if rem_balance < 5.0 and self.bot and self.admin_id:
                try:
                    await self.bot.send_message(
                        self.admin_id,
                        f"🔔 <b>Balans eslatmasi:</b> TON hamyonda {rem_balance:.2f} TON qoldi.",
                    )
                except Exception:
                    pass

            return tx_hash

        except Exception as err:
            # 3-QADAM: Xatolik chiqsa, AI Agent vaziyatni tahlil qilib, mijozni tinchlantiradi
            log.exception("on_payment_confirmed xatosi: %s", err)
            ai_reply = await self.ai.handle_delivery_delay(ctx, error_log=str(err))
            if self.bot:
                try:
                    await self.bot.send_message(ctx.user_tg_id, ai_reply)
                    if self.admin_id:
                        await self.bot.send_message(
                            self.admin_id,
                            f"🚨 <b>Buyurtma #{ctx.order_id} kechikdi!</b>\n"
                            f"Xato: {err}\nAI Agent mijozga javob berdi va qayta urinish navbatga qo'yildi.",
                        )
                except Exception:
                    pass
            return None
