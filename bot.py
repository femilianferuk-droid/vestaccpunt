import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    select,
    func,
    text as sa_text
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError
)

# ===== НАСТРОЙКИ =====
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

YOOMONEY_WALLET = "4100119286550472"
CLIENT_ID = os.getenv("CLIENT_ID", "")
CRYPTO_BOT_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_IDS = [7973988177]

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ===== БАЗА ДАННЫХ =====
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Float, default=0.0)
    total_spent = Column(Float, default=0.0)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    country = Column(String(50), default="США")
    session_string = Column(Text, nullable=True)
    is_sold = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    price = Column(Float, default=20.0)
    created_at = Column(DateTime, default=datetime.utcnow)

class Purchase(Base):
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    account_id = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    payment_method = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    code_sent = Column(Boolean, default=False)

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    payment_id = Column(String(255), unique=True)
    status = Column(String(50), default="pending")
    method = Column(String(50))
    type = Column(String(50), default="deposit")
    created_at = Column(DateTime, default=datetime.utcnow)

try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Failed to create database engine: {e}")
    sys.exit(1)

# ===== АВТО-МИГРАЦИЯ =====
async def run_migrations():
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent FLOAT DEFAULT 0.0",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS session_string TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS code_sent BOOLEAN DEFAULT FALSE",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS type VARCHAR(50) DEFAULT 'deposit'",
    ]
    try:
        async with engine.begin() as conn:
            for migration in migrations:
                try:
                    await conn.execute(sa_text(migration))
                except:
                    pass
            await conn.commit()
        logger.info("Migrations completed")
    except Exception as e:
        logger.error(f"Migration error: {e}")

# ===== ИНИЦИАЛИЗАЦИЯ =====
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
pending_auth = {}

# ===== ПРЕМИУМ ЭМОДЗИ =====
PREMIUM_EMOJI_IDS = {
    'bot': '6030400221232501136',
    'lock': '6037249452824072506',
    'loading': '5345906554510012647',
    'check': '5870633910337015697',
    'cross': '5870657884844462243',
    'home': '5873147866364514353',
    'profile': '5870994129244131212',
    'wallet': '5769126056262898415',
    'money': '5904462880941545555',
    'crypto': '5260752406890711732',
    'star': '6041731551845159060',
    'location': '6042011682497106307',
    'box': '5884479287171485878',
    'tag': '5886285355279193209',
    'code': '5940433880585605708',
    'stats': '5870921681735781843',
    'broadcast': '6039422865189638057',
    'add': '5771851822897566479',
    'back': '5893057118545646106',
    'clock': '5983150113483134607',
    'buy': '5963103826075456248',
    'info': '6028435952299413210',
    'edit': '5870676941614354370',
}

EMOJI_CHARS = {
    'bot': '🤖', 'lock': '🔒', 'loading': '🔄', 'check': '✅',
    'cross': '❌', 'home': '🏘️', 'profile': '👤', 'wallet': '💰',
    'money': '💵', 'crypto': '🪙', 'star': '⭐', 'location': '📍',
    'box': '📦', 'tag': '🏷️', 'code': '🔐', 'stats': '📊',
    'broadcast': '📣', 'add': '➕', 'back': '◀️', 'clock': '⏰',
    'buy': '🛒', 'info': 'ℹ️', 'edit': '✏️',
}

def pe(name: str) -> str:
    """Возвращает HTML-тег премиум эмодзи с символом внутри"""
    emoji_id = PREMIUM_EMOJI_IDS.get(name, PREMIUM_EMOJI_IDS['info'])
    char = EMOJI_CHARS.get(name, '📌')
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Купить аккаунт", callback_data="buy_account"),
        InlineKeyboardButton(text="Мои покупки", callback_data="my_purchases")
    )
    builder.row(
        InlineKeyboardButton(text="Профиль", callback_data="profile"),
        InlineKeyboardButton(text="Пополнить", callback_data="deposit_balance")
    )
    return builder.as_markup()

def countries_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🇺🇸 США • 20₽", callback_data="country_USA"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu"))
    return builder.as_markup()

def account_found_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🛒 КУПИТЬ", callback_data="show_payment_methods"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="buy_account"))
    return builder.as_markup()

def payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 Баланс бота", callback_data="pay_balance"))
    builder.row(InlineKeyboardButton(text="💵 ЮMoney", callback_data="pay_yoomoney"))
    builder.row(InlineKeyboardButton(text="🪙 Crypto Bot", callback_data="pay_crypto"))
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="buy_account"))
    return builder.as_markup()

def check_payment_keyboard(payment_id: str, method: str, is_deposit: bool = True):
    prefix = "check_deposit" if is_deposit else "check_purchase"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"{prefix}_{method}_{payment_id}"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return builder.as_markup()

def get_code_keyboard(purchase_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔐 Получить код", callback_data=f"get_code_{purchase_id}"))
    builder.row(InlineKeyboardButton(text="◀️ К покупкам", callback_data="my_purchases"))
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit_balance"))
    builder.row(InlineKeyboardButton(text="📦 Мои покупки", callback_data="my_purchases"))
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu"))
    return builder.as_markup()

def deposit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💵 ЮMoney", callback_data="deposit_yoomoney"))
    builder.row(InlineKeyboardButton(text="🪙 Crypto Bot", callback_data="deposit_crypto"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="profile"))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="➕ Добавить аккаунты", callback_data="admin_add_accounts"))
    builder.row(InlineKeyboardButton(text="✏️ Управление балансом", callback_data="admin_balance"))
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu"))
    return builder.as_markup()

# ===== TElethon ФУНКЦИИ =====
async def create_telethon_client(session_string: str = None) -> TelegramClient:
    if session_string:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    else:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
    return client

async def send_code_to_phone(phone: str) -> dict:
    try:
        client = await create_telethon_client()
        await client.connect()
        sent = await client.send_code_request(phone)
        pending_auth[phone] = {'client': client, 'phone_code_hash': sent.phone_code_hash, 'phone': phone}
        return {'success': True, 'phone_code_hash': sent.phone_code_hash}
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        return {'success': False, 'error': str(e)}

async def verify_code_and_get_session(phone: str, code: str, phone_code_hash: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {'success': False, 'error': 'Сессия не найдена'}
        client = auth_data['client']
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return {'success': False, 'need_password': True, 'error': 'Требуется 2FA пароль'}
        session_string = client.session.save()
        await client.disconnect()
        pending_auth.pop(phone, None)
        return {'success': True, 'session_string': session_string}
    except PhoneCodeInvalidError:
        return {'success': False, 'error': 'Неверный код'}
    except PhoneCodeExpiredError:
        return {'success': False, 'error': 'Код истек'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

async def verify_2fa_password(phone: str, password: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {'success': False, 'error': 'Сессия не найдена'}
        client = auth_data['client']
        await client.sign_in(password=password)
        session_string = client.session.save()
        await client.disconnect()
        pending_auth.pop(phone, None)
        return {'success': True, 'session_string': session_string}
    except PasswordHashInvalidError:
        return {'success': False, 'error': 'Неверный пароль 2FA'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

async def get_code_from_session(session_string: str) -> Optional[str]:
    client = None
    try:
        client = await create_telethon_client(session_string)
        await client.connect()
        if not await client.is_user_authorized():
            return None
        async for dialog in client.iter_dialogs():
            if dialog.name and "42777" in dialog.name:
                messages = await client.get_messages(dialog, limit=5)
                for message in messages:
                    if message.text:
                        codes = re.findall(r'\b\d{5}\b', message.text)
                        if codes:
                            return codes[0]
        return None
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        return None
    finally:
        if client:
            await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
async def get_user(user_id: int):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        return result.scalar_one_or_none()

async def get_or_create_user(user_id: int, username: str = None):
    user = await get_user(user_id)
    if not user:
        async with async_session() as session:
            user = User(telegram_id=user_id, username=username, is_admin=(user_id in ADMIN_IDS))
            session.add(user)
            await session.commit()
            await session.refresh(user)
    return user

async def get_available_account():
    async with async_session() as session:
        result = await session.execute(
            select(Account).where(
                Account.is_sold == False,
                Account.is_verified == True,
                Account.session_string != None,
                Account.session_string != ""
            ).limit(1)
        )
        return result.scalar_one_or_none()

async def create_yoomoney_payment(amount: float, payment_id: str) -> Optional[str]:
    return (
        f"https://yoomoney.ru/quickpay/confirm.xml?"
        f"receiver={YOOMONEY_WALLET}&"
        f"quickpay-form=shop&"
        f"targets=Vest+Account+{payment_id}&"
        f"paymentType=SB&"
        f"sum={amount}&"
        f"label={payment_id}"
    )

async def check_yoomoney_payment(payment_id: str) -> bool:
    if not CLIENT_ID:
        return False
    try:
        url = "https://yoomoney.ru/api/operation-history"
        headers = {"Authorization": f"Bearer {CLIENT_ID}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"label": payment_id, "records": 10, "type": "deposition"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=30) as response:
                if response.status == 200:
                    result = await response.json()
                    for op in result.get("operations", []):
                        if op.get("label") == payment_id and op.get("status") == "success":
                            return True
        return False
    except Exception as e:
        logger.error(f"YooMoney check error: {e}")
        return False

async def create_crypto_bot_invoice(amount: float, payment_id: str) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        usdt_amount = round(amount / 90, 2)
        payload = {
            "asset": "USDT", "amount": str(usdt_amount),
            "description": f"Vest Account #{payment_id}",
            "payload": payment_id, "allow_comments": False,
            "allow_anonymous": False, "expires_in": 3600
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=30) as response:
                return await response.json()
    except Exception as e:
        logger.error(f"Crypto Bot error: {e}")
        return None

async def check_crypto_bot_invoice(invoice_id: int) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        params = {"invoice_ids": str(invoice_id)}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=30) as response:
                data = await response.json()
                if data.get("ok") and data.get("result", {}).get("items"):
                    return data["result"]["items"][0]
        return None
    except Exception as e:
        logger.error(f"Crypto Bot check error: {e}")
        return None

async def generate_payment_id():
    return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

# ===== ОБРАБОТЧИКИ КОМАНД =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    text = (
        f'{pe("bot")} <b>Vest Account</b>\n\n'
        f'{pe("lock")} Покупка аккаунтов\n'
        f'{pe("loading")} Быстро и безопасно\n\n'
        '<i>Выберите действие:</i>'
    )
    await message.answer(text, reply_markup=main_menu_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(f'{pe("cross")} <b>Доступ запрещен</b>')
        return
    await message.answer(f'{pe("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        f'{pe("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>',
        reply_markup=main_menu_keyboard()
    )

@router.callback_query(F.data == "buy_account")
async def callback_buy_account(callback: CallbackQuery):
    await callback.answer()
    account = await get_available_account()
    text = f'{pe("location")} <b>Выберите страну</b>\n\n'
    text += f'{pe("check")} Аккаунты в наличии' if account else f'{pe("cross")} Нет доступных аккаунтов'
    await callback.message.edit_text(text, reply_markup=countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def callback_country(callback: CallbackQuery):
    await callback.answer()
    country = callback.data.replace("country_", "")
    account = await get_available_account()
    if account:
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {'account_id': account.id, 'price': account.price}
        text = (
            f'{pe("check")} <b>Аккаунт найден!</b>\n\n'
            f'{pe("location")} Страна: <b>{country}</b>\n'
            f'{pe("money")} Цена: <b>{account.price}₽</b>\n\n'
            '<i>Нажмите КУПИТЬ для продолжения</i>'
        )
        await callback.message.edit_text(text, reply_markup=account_found_keyboard())
    else:
        await callback.message.edit_text(
            f'{pe("cross")} <b>Нет доступных аккаунтов</b>',
            reply_markup=countries_keyboard()
        )

@router.callback_query(F.data == "show_payment_methods")
async def callback_show_payment_methods(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    text = (
        f'{pe("buy")} <b>Покупка аккаунта</b>\n\n'
        f'{pe("money")} Сумма к оплате: <b>{price}₽</b>\n\n'
        '<i>Выберите способ оплаты:</i>'
    )
    await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_balance")
async def callback_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    account_id = pending.get('account_id')
    if not account_id:
        await callback.message.edit_text(f'{pe("cross")} <b>Ошибка</b>', reply_markup=main_menu_keyboard())
        return
    if user.balance >= price:
        async with async_session() as session:
            user = await session.get(User, user.id)
            account = await session.get(Account, account_id)
            if account.is_sold:
                await callback.message.edit_text(f'{pe("cross")} <b>Аккаунт продан</b>', reply_markup=main_menu_keyboard())
                return
            user.balance -= price
            user.total_spent = (user.total_spent or 0) + price
            account.is_sold = True
            purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="balance")
            session.add(purchase)
            await session.commit()
            await session.refresh(purchase)
            text = (
                f'{pe("check")} <b>Оплата успешна!</b>\n\n'
                f'{pe("tag")} Номер: <code>{account.phone}</code>\n'
                f'{pe("money")} Сумма: <b>{price}₽</b>\n\n'
                'Нажмите чтобы получить код:'
            )
            await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = (
            f'{pe("cross")} <b>Недостаточно средств</b>\n\n'
            f'{pe("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n'
            f'{pe("money")} Нужно: <b>{price}₽</b>'
        )
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_yoomoney")
async def callback_pay_yoomoney(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    payment_id = await generate_payment_id()
    async with async_session() as session:
        session.add(Payment(user_id=callback.from_user.id, amount=price, payment_id=payment_id, method="yoomoney", status="pending", type="purchase"))
        await session.commit()
    payment_url = await create_yoomoney_payment(price, payment_id)
    text = (
        f'{pe("money")} <b>Оплата ЮMoney</b>\n\n'
        f'Сумма: <b>{price}₽</b>\n'
        f'Кошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
        f'<a href="{payment_url}">💳 Нажмите для оплаты</a>\n\n'
        '⚠️ <b>После оплаты нажмите проверку</b>'
    )
    await callback.message.edit_text(text, reply_markup=check_payment_keyboard(payment_id, "yoomoney", is_deposit=False), disable_web_page_preview=True)

@router.callback_query(F.data == "pay_crypto")
async def callback_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    payment_id = await generate_payment_id()
    async with async_session() as session:
        session.add(Payment(user_id=callback.from_user.id, amount=price, payment_id=payment_id, method="crypto", status="pending", type="purchase"))
        await session.commit()
    await callback.message.edit_text(f'{pe("loading")} <b>Создаю счет...</b>')
    invoice = await create_crypto_bot_invoice(price, payment_id)
    if invoice and invoice.get("ok"):
        result = invoice.get("result", {})
        pay_url = result.get("pay_url")
        invoice_id = result.get("invoice_id")
        async with async_session() as session:
            p = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
            p = p.scalar_one_or_none()
            if p:
                p.payment_id = str(invoice_id)
                await session.commit()
        text = (
            f'{pe("crypto")} <b>Оплата Crypto Bot</b>\n\n'
            f'Сумма: <b>{price}₽</b>\n\n'
            f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
            '⚠️ <b>После оплаты нажмите проверку</b>'
        )
        await callback.message.edit_text(text, reply_markup=check_payment_keyboard(str(invoice_id), "crypto", is_deposit=False), disable_web_page_preview=True)
    else:
        await callback.message.edit_text(f'{pe("cross")} <b>Ошибка создания счета</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_stars")
async def callback_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        f'{pe("star")} <b>Оплата Telegram Stars</b>\n\nНапишите: <b>@v3estnikov</b>',
        reply_markup=payment_methods_keyboard()
    )

@router.callback_query(F.data.startswith("check_purchase_"))
async def callback_check_purchase(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("check_purchase_", "").split("_", 1)
    method = parts[0]
    payment_id = "_".join(parts[1:])
    await callback.message.edit_text(
        f'{pe("loading")} <b>Проверяю оплату...</b>',
        reply_markup=check_payment_keyboard(payment_id, method, is_deposit=False)
    )
    success = False
    if method == "yoomoney":
        success = await check_yoomoney_payment(payment_id)
    elif method == "crypto":
        inv = await check_crypto_bot_invoice(int(payment_id))
        if inv and inv.get("status") == "paid":
            success = True
    if success:
        pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
        account_id = pending.get('account_id')
        price = pending.get('price', 20)
        if account_id:
            async with async_session() as session:
                account = await session.get(Account, account_id)
                if account.is_sold:
                    await callback.message.edit_text(f'{pe("cross")} <b>Аккаунт продан</b>', reply_markup=main_menu_keyboard())
                    return
                pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
                pr = pr.scalar_one_or_none()
                if pr:
                    pr.status = "completed"
                user = await session.get(User, callback.from_user.id)
                if user:
                    user.total_spent = (user.total_spent or 0) + price
                account.is_sold = True
                purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method=method)
                session.add(purchase)
                await session.commit()
                await session.refresh(purchase)
                text = (
                    f'{pe("check")} <b>Оплата подтверждена!</b>\n\n'
                    f'{pe("tag")} Номер: <code>{account.phone}</code>\n'
                    f'{pe("money")} Сумма: <b>{price}₽</b>\n\n'
                    'Нажмите чтобы получить код:'
                )
                await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
        else:
            await callback.message.edit_text(f'{pe("cross")} <b>Данные утеряны</b>', reply_markup=main_menu_keyboard())
    else:
        await callback.answer("⏳ Оплата не найдена", show_alert=True)

@router.callback_query(F.data.startswith("check_deposit_"))
async def callback_check_deposit(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("check_deposit_", "").split("_", 1)
    method = parts[0]
    payment_id = "_".join(parts[1:])
    await callback.message.edit_text(
        f'{pe("loading")} <b>Проверяю пополнение...</b>',
        reply_markup=check_payment_keyboard(payment_id, method, is_deposit=True)
    )
    success = False
    if method == "yoomoney":
        success = await check_yoomoney_payment(payment_id)
    elif method == "crypto":
        inv = await check_crypto_bot_invoice(int(payment_id))
        if inv and inv.get("status") == "paid":
            success = True
    if success:
        async with async_session() as session:
            pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
            payment = pr.scalar_one_or_none()
            if payment and payment.status != "completed":
                payment.status = "completed"
                user = await session.get(User, callback.from_user.id)
                deposit_amount = payment.amount
                user.balance += deposit_amount
                await session.commit()
                text = (
                    f'{pe("check")} <b>Баланс пополнен!</b>\n\n'
                    f'{pe("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n'
                    f'{pe("wallet")} Баланс: <b>{user.balance:.2f}₽</b>'
                )
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu"))
                await callback.message.edit_text(text, reply_markup=builder.as_markup())
            elif payment and payment.status == "completed":
                await callback.answer("Уже зачислен", show_alert=True)
    else:
        await callback.answer("⏳ Не найдено", show_alert=True)

@router.callback_query(F.data.startswith("get_code_"))
async def callback_get_code(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_code_", ""))
    async with async_session() as session:
        result = await session.execute(select(Purchase).where(Purchase.id == purchase_id))
        purchase = result.scalar_one_or_none()
        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("Не найдена", show_alert=True)
            return
        if purchase.code_sent:
            await callback.answer("Уже получен", show_alert=True)
            return
        account = await session.get(Account, purchase.account_id)
        if not account or not account.session_string:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await callback.message.edit_text(f'{pe("loading")} <b>Получаю код...</b>')
        code = await get_code_from_session(account.session_string)
        if code:
            purchase.code_sent = True
            await session.commit()
            text = (
                f'{pe("check")} <b>Код получен!</b>\n\n'
                f'{pe("tag")} Номер: <code>{account.phone}</code>\n'
                f'{pe("lock")} Код: <code>{code}</code>\n\n'
                '⚠️ <i>Сохраните код</i>'
            )
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu"))
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        else:
            await callback.message.edit_text(
                f'{pe("cross")} <b>Не удалось получить код</b>\n\n@v3estnikov',
                reply_markup=get_code_keyboard(purchase_id)
            )

@router.callback_query(F.data == "my_purchases")
async def callback_my_purchases(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc())
        )
        purchases = result.scalars().all()
        if purchases:
            text = f'{pe("box")} <b>Ваши покупки</b>\n\n'
            builder = InlineKeyboardBuilder()
            for purchase in purchases:
                account = await session.get(Account, purchase.account_id)
                status = "✅" if purchase.code_sent else "⏳"
                phone = account.phone if account else "Н/Д"
                date = purchase.created_at.strftime('%d.%m.%y')
                text += f'{status} <code>{phone}</code> • {purchase.amount}₽ • {date}\n'
                if not purchase.code_sent:
                    builder.row(InlineKeyboardButton(
                        text=f"🔐 Получить код • {phone}",
                        callback_data=f"get_code_{purchase.id}"
                    ))
            builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu"))
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        else:
            await callback.message.edit_text(
                f'{pe("box")} <b>Мои покупки</b>\n\nУ вас пока нет покупок.',
                reply_markup=main_menu_keyboard()
            )

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Purchase.id)).where(Purchase.user_id == callback.from_user.id)
        )
        purchases_count = result.scalar() or 0
    text = (
        f'{pe("profile")} <b>Профиль</b>\n\n'
        f'{pe("tag")} ID: <code>{user.telegram_id}</code>\n'
        f'{pe("profile")} @{user.username or "нет"}\n\n'
        '━━━ 💰 БАЛАНС ━━━\n'
        f'{pe("wallet")} <b>{user.balance:.0f}₽</b>\n'
        '━━━━━━━━━━━━━━\n\n'
        '━━ 📊 СТАТИСТИКА ━━\n'
        f'{pe("box")} Покупок: <b>{purchases_count}</b>\n'
        f'{pe("money")} Потрачено: <b>{(user.total_spent or 0):.0f}₽</b>\n'
        f'{pe("clock")} С нами с: {user.created_at.strftime("%d.%m.%Y")}\n'
        '━━━━━━━━━━━━━━'
    )
    await callback.message.edit_text(text, reply_markup=profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def callback_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    text = (
        f'{pe("wallet")} <b>Пополнение баланса</b>\n\n'
        f'{pe("money")} <b>ЮMoney</b> - перевод\n'
        f'{pe("crypto")} <b>Crypto Bot</b> - криптовалюта\n\n'
        '<i>Минимум: 10₽</i>'
    )
    await callback.message.edit_text(text, reply_markup=deposit_keyboard())

@router.callback_query(F.data == "deposit_yoomoney")
async def callback_deposit_yoomoney(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{pe("money")} <b>Введите сумму (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'yoomoney'

@router.callback_query(F.data == "deposit_crypto")
async def callback_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{pe("crypto")} <b>Введите сумму (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

@router.callback_query(F.data.startswith("admin_"))
async def callback_admin(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    data = callback.data
    if data == "admin_stats":
        async with async_session() as session:
            r = await session.execute(select(func.count(User.id)))
            total_users = r.scalar() or 0
            r = await session.execute(select(func.count(Account.id)))
            total_accounts = r.scalar() or 0
            r = await session.execute(select(func.count(Account.id)).where(Account.is_sold == True))
            sold_accounts = r.scalar() or 0
            r = await session.execute(select(func.count(Account.id)).where(Account.is_verified == True))
            verified_accounts = r.scalar() or 0
            r = await session.execute(select(func.count(Purchase.id)))
            total_purchases = r.scalar() or 0
            r = await session.execute(select(func.sum(Purchase.amount)))
            total_revenue = r.scalar() or 0
            stats_text = (
                f'{pe("stats")} <b>Статистика</b>\n\n'
                f'{pe("profile")} Пользователей: <b>{total_users}</b>\n'
                f'{pe("box")} Аккаунтов: <b>{total_accounts}</b>\n'
                f'{pe("check")} Верифицировано: <b>{verified_accounts}</b>\n'
                f'{pe("buy")} Продано: <b>{sold_accounts}</b>\n'
                f'{pe("box")} Покупок: <b>{total_purchases}</b>\n'
                f'{pe("money")} Выручка: <b>{total_revenue:.0f}₽</b>'
            )
            await callback.message.edit_text(stats_text, reply_markup=admin_keyboard())
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'):
            dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
        await callback.message.edit_text(
            f'{pe("broadcast")} <b>Рассылка</b>\n\nОтправьте сообщение.',
            reply_markup=builder.as_markup()
        )
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
        await callback.message.edit_text(
            f'{pe("add")} <b>Добавление аккаунта</b>\n\nОтправьте номер: <code>+79001234567</code>',
            reply_markup=builder.as_markup()
        )
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'):
            dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
        await callback.message.edit_text(
            f'{pe("edit")} <b>Изменение баланса</b>\n\nОтправьте ID пользователя.',
            reply_markup=builder.as_markup()
        )

@router.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # Пополнение баланса
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10:
                await message.answer(
                    f'{pe("cross")} <b>Минимум: 10₽</b>',
                    reply_markup=deposit_keyboard()
                )
                return
            payment_id = await generate_payment_id()
            async with async_session() as session:
                session.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method=method, status="pending", type="deposit"))
                await session.commit()
            if method == "yoomoney":
                payment_url = await create_yoomoney_payment(amount, payment_id)
                await message.answer(
                    f'{pe("money")} <b>Пополнение ЮMoney</b>\n\n'
                    f'Сумма: <b>{amount}₽</b>\nКошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
                    f'<a href="{payment_url}">💳 Нажмите для оплаты</a>\n\n'
                    '⚠️ <b>После оплаты нажмите проверку</b>',
                    reply_markup=check_payment_keyboard(payment_id, "yoomoney", is_deposit=True),
                    disable_web_page_preview=True
                )
            elif method == "crypto":
                await message.answer(f'{pe("loading")} Создаю счет...')
                invoice = await create_crypto_bot_invoice(amount, payment_id)
                if invoice and invoice.get("ok"):
                    result = invoice.get("result", {})
                    pay_url = result.get("pay_url")
                    invoice_id = result.get("invoice_id")
                    async with async_session() as session:
                        p = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
                        p = p.scalar_one_or_none()
                        if p:
                            p.payment_id = str(invoice_id)
                            await session.commit()
                    await message.answer(
                        f'{pe("crypto")} <b>Пополнение Crypto Bot</b>\n\n'
                        f'Сумма: <b>{amount}₽</b>\n\n'
                        f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
                        '⚠️ <b>После оплаты нажмите проверку</b>',
                        reply_markup=check_payment_keyboard(str(invoice_id), "crypto", is_deposit=True),
                        disable_web_page_preview=True
                    )
                else:
                    await message.answer(
                        f'{pe("cross")} <b>Ошибка создания счета</b>',
                        reply_markup=deposit_keyboard()
                    )
        except ValueError:
            await message.answer(f'{pe("cross")} <b>Введите число</b>')
        return

    # Добавление аккаунта (админ)
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]
        step = acc_data.get('step')
        if step == 'phone':
            phone = text
            acc_data['phone'] = phone
            await message.answer(f'{pe("loading")} Отправляю код...')
            result = await send_code_to_phone(phone)
            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']
                acc_data['step'] = 'code'
                await message.answer(f'{pe("check")} <b>Код отправлен на {phone}</b>\n\nВведите код:')
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{pe("cross")} <b>Ошибка</b>\n{result.get("error")}',
                    reply_markup=admin_keyboard()
                )
        elif step == 'code':
            code = text
            phone = acc_data['phone']
            phone_code_hash = acc_data['phone_code_hash']
            await message.answer(f'{pe("loading")} Проверяю...')
            result = await verify_code_and_get_session(phone, code, phone_code_hash)
            if result['success']:
                async with async_session() as session:
                    existing = await session.execute(select(Account).where(Account.phone == phone))
                    existing_acc = existing.scalar_one_or_none()
                    if existing_acc:
                        existing_acc.session_string = result['session_string']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                    else:
                        session.add(Account(phone=phone, session_string=result['session_string'], is_verified=True, is_sold=False))
                    await session.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n'
                    f'{pe("tag")} Номер: <code>{phone}</code>\n'
                    f'{pe("check")} Верифицирован\n'
                    '<i>Доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            elif result.get('need_password'):
                acc_data['step'] = 'password'
                await message.answer(f'{pe("lock")} <b>Введите 2FA пароль:</b>')
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{pe("cross")} <b>{result.get("error")}</b>',
                    reply_markup=admin_keyboard()
                )
        elif step == 'password':
            password = text
            phone = acc_data['phone']
            result = await verify_2fa_password(phone, password)
            if result['success']:
                async with async_session() as session:
                    existing = await session.execute(select(Account).where(Account.phone == phone))
                    existing_acc = existing.scalar_one_or_none()
                    if existing_acc:
                        existing_acc.session_string = result['session_string']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                    else:
                        session.add(Account(phone=phone, session_string=result['session_string'], is_verified=True, is_sold=False))
                    await session.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n'
                    f'{pe("tag")} Номер: <code>{phone}</code>\n'
                    '<i>Доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            else:
                await message.answer(f'{pe("cross")} <b>{result.get("error")}</b>\nПопробуйте еще раз:')
        return

    # Изменение баланса (админ)
    if hasattr(dp, 'awaiting_balance') and user_id in dp.awaiting_balance:
        bal_data = dp.awaiting_balance[user_id]
        step = bal_data.get('step')
        if step == 'user_id':
            try:
                target_id = int(text)
                target_user = await get_user(target_id)
                if not target_user:
                    await message.answer(f'{pe("cross")} <b>Пользователь не найден</b>', reply_markup=admin_keyboard())
                    del dp.awaiting_balance[user_id]
                    return
                bal_data['target_id'] = target_id
                bal_data['step'] = 'amount'
                await message.answer(
                    f'{pe("edit")} <b>Изменение баланса</b>\n\n'
                    f'Пользователь: <code>{target_id}</code>\n'
                    f'Баланс: <b>{target_user.balance:.0f}₽</b>\n\n'
                    '<code>+100</code> - пополнить\n<code>-50</code> - списать\n<code>500</code> - установить'
                )
            except ValueError:
                await message.answer(f'{pe("cross")} <b>Введите ID</b>')
        elif step == 'amount':
            try:
                value = text
                target_id = bal_data['target_id']
                async with async_session() as session:
                    target_user = await session.execute(select(User).where(User.telegram_id == target_id))
                    target_user = target_user.scalar_one_or_none()
                    if not target_user:
                        del dp.awaiting_balance[user_id]
                        await message.answer(f'{pe("cross")} <b>Не найден</b>', reply_markup=admin_keyboard())
                        return
                    old_balance = target_user.balance
                    if value.startswith('+'):
                        target_user.balance += float(value[1:])
                    elif value.startswith('-'):
                        target_user.balance = max(0, target_user.balance - float(value[1:]))
                    else:
                        target_user.balance = float(value)
                    await session.commit()
                    del dp.awaiting_balance[user_id]
                    await message.answer(
                        f'{pe("check")} <b>Баланс изменен!</b>\n\n'
                        f'Пользователь: <code>{target_id}</code>\n'
                        f'Было: <b>{old_balance:.0f}₽</b>\nСтало: <b>{target_user.balance:.0f}₽</b>',
                        reply_markup=admin_keyboard()
                    )
            except ValueError:
                await message.answer(f'{pe("cross")} <b>Введите сумму</b>')
        return

    # Рассылка (админ)
    if hasattr(dp, 'awaiting_broadcast') and user_id in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(user_id)
        async with async_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()
            sent = 0
            for user in users:
                try:
                    await message.copy_to(chat_id=user.telegram_id)
                    sent += 1
                    await asyncio.sleep(0.05)
                except:
                    continue
        await message.answer(
            f'{pe("check")} <b>Рассылка завершена</b>\n\nОтправлено: <b>{sent}</b> из <b>{len(users)}</b>',
            reply_markup=admin_keyboard()
        )
        return

    await message.answer(
        f'{pe("info")} <b>Используйте кнопки меню</b>',
        reply_markup=main_menu_keyboard()
    )

# ===== ЗАПУСК =====
async def setup_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")

async def main():
    await setup_db()
    await run_migrations()

    if not hasattr(dp, 'pending_accounts'):
        dp.pending_accounts = {}
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    if not hasattr(dp, 'awaiting_accounts'):
        dp.awaiting_accounts = {}
    if not hasattr(dp, 'awaiting_broadcast'):
        dp.awaiting_broadcast = set()
    if not hasattr(dp, 'awaiting_balance'):
        dp.awaiting_balance = {}

    dp.include_router(router)

    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
