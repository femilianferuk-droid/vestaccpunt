import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float,
    Integer, String, Text, select, func, text as sa_text
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

# СБП реквизиты
SBP_PHONE = "+79818376180"
SBP_BANK = "ЮMoney"
SBP_RECEIVER = "Иван Б"

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
    screenshot_file_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class MediaSettings(Base):
    __tablename__ = "media_settings"
    id = Column(Integer, primary_key=True)
    section = Column(String(50), unique=True, nullable=False)
    file_id = Column(String(255), nullable=False)
    file_type = Column(String(20), default="photo")
    caption = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

# ===== FSM =====
class MediaStates(StatesGroup):
    waiting_for_media = State()

class SBPStates(StatesGroup):
    waiting_for_screenshot = State()

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
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS screenshot_file_id VARCHAR(255)",
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
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=storage)
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
    'media': '6035128606563241721',
    'sbp': '5879814368572478751',
    'photo': '6035128606563241721',
    'bank': '5904462880941545555',
}

EMOJI_CHARS = {
    'bot': '🤖', 'lock': '🔒', 'loading': '🔄', 'check': '✅',
    'cross': '❌', 'home': '🏘️', 'profile': '👤', 'wallet': '💰',
    'money': '💵', 'crypto': '🪙', 'star': '⭐', 'location': '📍',
    'box': '📦', 'tag': '🏷️', 'code': '🔐', 'stats': '📊',
    'broadcast': '📣', 'add': '➕', 'back': '◀️', 'clock': '⏰',
    'buy': '🛒', 'info': 'ℹ️', 'edit': '✏️', 'media': '🖼️',
    'sbp': '💳', 'photo': '📸', 'bank': '🏦',
}

def pe(name: str) -> str:
    emoji_id = PREMIUM_EMOJI_IDS.get(name, PREMIUM_EMOJI_IDS['info'])
    char = EMOJI_CHARS.get(name, '📌')
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

# ===== МЕДИА =====
async def get_media(section: str) -> Optional[MediaSettings]:
    async with async_session() as session:
        result = await session.execute(select(MediaSettings).where(MediaSettings.section == section))
        return result.scalar_one_or_none()

async def set_media(section: str, file_id: str, file_type: str, caption: str = None):
    async with async_session() as session:
        existing = await session.execute(select(MediaSettings).where(MediaSettings.section == section))
        media = existing.scalar_one_or_none()
        if media:
            media.file_id = file_id
            media.file_type = file_type
            media.caption = caption
            media.updated_at = datetime.utcnow()
        else:
            media = MediaSettings(section=section, file_id=file_id, file_type=file_type, caption=caption)
            session.add(media)
        await session.commit()

async def send_media_message(target, section: str, text: str, reply_markup: InlineKeyboardMarkup):
    media = await get_media(section)
    if isinstance(target, CallbackQuery):
        msg = target.message
    else:
        msg = target
    if media:
        caption = f"{text}\n\n{media.caption}" if media.caption else text
        if media.file_type == "photo":
            await msg.answer_photo(media.file_id, caption=caption, reply_markup=reply_markup)
        elif media.file_type == "video":
            await msg.answer_video(media.file_id, caption=caption, reply_markup=reply_markup)
        elif media.file_type == "animation":
            await msg.answer_animation(media.file_id, caption=caption, reply_markup=reply_markup)
        else:
            await msg.answer(text, reply_markup=reply_markup)
    else:
        if isinstance(target, Message):
            await target.answer(text, reply_markup=reply_markup)
        elif isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=reply_markup)

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Купить аккаунт", callback_data="buy_account", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['buy']),
        InlineKeyboardButton(text="Мои покупки", callback_data="my_purchases", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['box'])
    )
    builder.row(
        InlineKeyboardButton(text="Профиль", callback_data="profile", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['profile']),
        InlineKeyboardButton(text="Пополнить", callback_data="deposit_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet'])
    )
    return builder.as_markup()

def countries_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="США • 20₽", callback_data="country_USA", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['location']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def account_found_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="КУПИТЬ", callback_data="show_payment_methods", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['buy']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="buy_account", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Баланс бота", callback_data="pay_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet']))
    builder.row(InlineKeyboardButton(text="СБП", callback_data="pay_sbp", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['sbp']))
    builder.row(InlineKeyboardButton(text="Crypto Bot", callback_data="pay_crypto", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['crypto']))
    builder.row(InlineKeyboardButton(text="Telegram Stars", callback_data="pay_stars", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['star']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="buy_account", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def check_crypto_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Проверить оплату", callback_data=f"check_purchase_crypto_{payment_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['loading']))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    return builder.as_markup()

def get_code_keyboard(purchase_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Получить код", callback_data=f"get_code_{purchase_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['code']))
    builder.row(InlineKeyboardButton(text="К покупкам", callback_data="my_purchases", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['box']))
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Пополнить баланс", callback_data="deposit_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet']))
    builder.row(InlineKeyboardButton(text="Мои покупки", callback_data="my_purchases", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['box']))
    builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
    return builder.as_markup()

def deposit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="СБП", callback_data="deposit_sbp", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['sbp']))
    builder.row(InlineKeyboardButton(text="Crypto Bot", callback_data="deposit_crypto", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['crypto']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="profile", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def deposit_crypto_check_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Проверить оплату", callback_data=f"check_deposit_crypto_{payment_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['loading']))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    return builder.as_markup()

def sbp_payment_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Я оплатил", callback_data=f"sbp_paid_{payment_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['check']))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Статистика", callback_data="admin_stats", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['stats']))
    builder.row(InlineKeyboardButton(text="Рассылка", callback_data="admin_broadcast", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['broadcast']))
    builder.row(InlineKeyboardButton(text="Добавить аккаунты", callback_data="admin_add_accounts", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['add']))
    builder.row(InlineKeyboardButton(text="Управление балансом", callback_data="admin_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['edit']))
    builder.row(InlineKeyboardButton(text="Управление медиа", callback_data="admin_media_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['media']))
    builder.row(InlineKeyboardButton(text="Проверка СБП платежей", callback_data="admin_sbp_check", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['sbp']))
    builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
    return builder.as_markup()

def media_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Главное меню", callback_data="set_media_main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
    builder.row(InlineKeyboardButton(text="Покупка аккаунта", callback_data="set_media_buy_account", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['buy']))
    builder.row(InlineKeyboardButton(text="Способы оплаты", callback_data="set_media_payment_methods", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['money']))
    builder.row(InlineKeyboardButton(text="Профиль", callback_data="set_media_profile", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['profile']))
    builder.row(InlineKeyboardButton(text="Мои покупки", callback_data="set_media_my_purchases", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['box']))
    builder.row(InlineKeyboardButton(text="Пополнение баланса", callback_data="set_media_deposit", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet']))
    builder.row(InlineKeyboardButton(text="Удалить все медиа", callback_data="admin_clear_media", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def sbp_approve_keyboard(payment_id: str, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Одобрить", callback_data=f"sbp_approve_{payment_id}_{user_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['check']),
        InlineKeyboardButton(text="Отклонить", callback_data=f"sbp_reject_{payment_id}_{user_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross'])
    )
    return builder.as_markup()

# ===== TElethon =====
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
        return None
    finally:
        if client:
            await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
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
            select(Account).where(Account.is_sold == False, Account.is_verified == True, Account.session_string != None, Account.session_string != "").limit(1)
        )
        return result.scalar_one_or_none()

async def create_crypto_bot_invoice(amount: float, payment_id: str) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        usdt_amount = round(amount / 90, 2)
        payload = {"asset": "USDT", "amount": str(usdt_amount), "description": f"Vest Account #{payment_id}", "payload": payment_id, "allow_comments": False, "allow_anonymous": False, "expires_in": 3600}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=30) as response:
                return await response.json()
    except Exception as e:
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
        return None

async def generate_payment_id():
    return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

# ===== ОБРАБОТЧИКИ =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    text = f'{pe("bot")} <b>Vest Account</b>\n\n{pe("lock")} Покупка аккаунтов\n{pe("loading")} Быстро и безопасно\n\n<i>Выберите действие:</i>'
    await send_media_message(message, "main_menu", text, main_menu_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(f'{pe("cross")} <b>Доступ запрещен</b>')
        return
    await message.answer(f'{pe("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    await callback.answer()
    text = f'{pe("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>'
    await send_media_message(callback, "main_menu", text, main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def callback_buy_account(callback: CallbackQuery):
    await callback.answer()
    account = await get_available_account()
    text = f'{pe("location")} <b>Выберите страну</b>\n\n'
    text += f'{pe("check")} Аккаунты в наличии' if account else f'{pe("cross")} Нет доступных аккаунтов'
    await send_media_message(callback, "buy_account", text, countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def callback_country(callback: CallbackQuery):
    await callback.answer()
    country = callback.data.replace("country_", "")
    account = await get_available_account()
    if account:
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {'account_id': account.id, 'price': account.price}
        text = f'{pe("check")} <b>Аккаунт найден!</b>\n\n{pe("location")} Страна: <b>{country}</b>\n{pe("money")} Цена: <b>{account.price}₽</b>\n\n<i>Нажмите КУПИТЬ</i>'
        await callback.message.edit_text(text, reply_markup=account_found_keyboard())
    else:
        await callback.message.edit_text(f'{pe("cross")} <b>Нет доступных аккаунтов</b>', reply_markup=countries_keyboard())

@router.callback_query(F.data == "show_payment_methods")
async def callback_show_payment_methods(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    text = f'{pe("buy")} <b>Покупка аккаунта</b>\n\n{pe("money")} Сумма: <b>{price}₽</b>\n\n<i>Выберите способ оплаты:</i>'
    await send_media_message(callback, "payment_methods", text, payment_methods_keyboard())

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
            text = f'{pe("check")} <b>Оплата успешна!</b>\n\n{pe("tag")} Номер: <code>{account.phone}</code>\n{pe("money")} Сумма: <b>{price}₽</b>\n\nНажмите чтобы получить код:'
            await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = f'{pe("cross")} <b>Недостаточно средств</b>\n\n{pe("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n{pe("money")} Нужно: <b>{price}₽</b>'
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_sbp")
async def callback_pay_sbp(callback: CallbackQuery):
    await callback.answer()
    text = (
        f'{pe("sbp")} <b>Оплата через СБП</b>\n\n'
        f'{pe("info")} Для оплаты товара через СБП\n'
        f'необходимо пополнить баланс бота.\n\n'
        f'Перейдите в раздел "Пополнить" в главном меню.'
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Пополнить баланс", callback_data="deposit_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="show_payment_methods", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

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
        text = f'{pe("crypto")} <b>Оплата Crypto Bot</b>\n\nСумма: <b>{price}₽</b>\n\n<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n⚠️ <b>После оплаты нажмите проверку</b>'
        await callback.message.edit_text(text, reply_markup=check_crypto_keyboard(str(invoice_id)), disable_web_page_preview=True)
    else:
        await callback.message.edit_text(f'{pe("cross")} <b>Ошибка создания счета</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_stars")
async def callback_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(f'{pe("star")} <b>Оплата Telegram Stars</b>\n\nНапишите: <b>@v3estnikov</b>', reply_markup=payment_methods_keyboard())

# ===== ПРОВЕРКА CRYPTO BOT ОПЛАТЫ =====
@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def callback_check_purchase_crypto(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_purchase_crypto_", "")
    await callback.message.edit_text(f'{pe("loading")} <b>Проверяю оплату...</b>', reply_markup=check_crypto_keyboard(payment_id))
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
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
                purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="crypto")
                session.add(purchase)
                await session.commit()
                await session.refresh(purchase)
                text = f'{pe("check")} <b>Оплата подтверждена!</b>\n\n{pe("tag")} Номер: <code>{account.phone}</code>\n{pe("money")} Сумма: <b>{price}₽</b>\n\nНажмите чтобы получить код:'
                await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
        else:
            await callback.message.edit_text(f'{pe("cross")} <b>Данные утеряны</b>', reply_markup=main_menu_keyboard())
    else:
        await callback.answer("⏳ Оплата не найдена", show_alert=True)

@router.callback_query(F.data.startswith("check_deposit_crypto_"))
async def callback_check_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_deposit_crypto_", "")
    await callback.message.edit_text(f'{pe("loading")} <b>Проверяю...</b>', reply_markup=deposit_crypto_check_keyboard(payment_id))
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
        async with async_session() as session:
            pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
            payment = pr.scalar_one_or_none()
            if payment and payment.status != "completed":
                payment.status = "completed"
                user = await session.get(User, callback.from_user.id)
                deposit_amount = payment.amount
                user.balance += deposit_amount
                await session.commit()
                text = f'{pe("check")} <b>Баланс пополнен!</b>\n\n{pe("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n{pe("wallet")} Баланс: <b>{user.balance:.2f}₽</b>'
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
                await callback.message.edit_text(text, reply_markup=builder.as_markup())
            elif payment and payment.status == "completed":
                await callback.message.edit_text(f'{pe("info")} <b>Уже зачислен</b>', reply_markup=main_menu_keyboard())
    else:
        await callback.answer("⏳ Не найдено", show_alert=True)

# ===== ПОЛУЧЕНИЕ КОДА =====
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
            text = f'{pe("check")} <b>Код получен!</b>\n\n{pe("tag")} Номер: <code>{account.phone}</code>\n{pe("lock")} Код: <code>{code}</code>\n\n⚠️ <i>Сохраните код</i>'
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        else:
            await callback.message.edit_text(f'{pe("cross")} <b>Не удалось получить код</b>\n\n@v3estnikov', reply_markup=get_code_keyboard(purchase_id))

@router.callback_query(F.data == "my_purchases")
async def callback_my_purchases(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        result = await session.execute(select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc()))
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
                    builder.row(InlineKeyboardButton(text=f"Получить код • {phone}", callback_data=f"get_code_{purchase.id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['code']))
            builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())
        else:
            text = f'{pe("box")} <b>Мои покупки</b>\n\nУ вас пока нет покупок.'
            await send_media_message(callback, "my_purchases", text, main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    async with async_session() as session:
        result = await session.execute(select(func.count(Purchase.id)).where(Purchase.user_id == callback.from_user.id))
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
    await send_media_message(callback, "profile", text, profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def callback_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    text = (
        f'{pe("wallet")} <b>Пополнение баланса</b>\n\n'
        f'{pe("sbp")} <b>СБП</b> - перевод по номеру телефона\n'
        f'{pe("crypto")} <b>Crypto Bot</b> - криптовалютой\n\n'
        '<i>Минимум: 10₽</i>'
    )
    await send_media_message(callback, "deposit", text, deposit_keyboard())

@router.callback_query(F.data == "deposit_sbp")
async def callback_deposit_sbp(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{pe("sbp")} <b>Введите сумму пополнения (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'

@router.callback_query(F.data == "deposit_crypto")
async def callback_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{pe("crypto")} <b>Введите сумму пополнения (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

# ===== СБП ПОПОЛНЕНИЕ =====
@router.callback_query(F.data.startswith("sbp_paid_"))
async def callback_sbp_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    payment_id = callback.data.replace("sbp_paid_", "")
    await state.set_state(SBPStates.waiting_for_screenshot)
    await state.update_data(payment_id=payment_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    
    await callback.message.edit_text(
        f'{pe("photo")} <b>Отправьте скриншот оплаты</b>\n\n'
        'Сделайте скриншот перевода и отправьте его сюда.',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def handle_sbp_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    payment_id = data.get('payment_id')
    await state.clear()
    
    file_id = message.photo[-1].file_id
    
    # Сохраняем скриншот в БД
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            payment.screenshot_file_id = file_id
            await session.commit()
    
    await message.answer(
        f'{pe("check")} <b>Скриншот получен!</b>\n\n'
        'Администратор проверит ваш платеж и зачислит средства.',
        reply_markup=main_menu_keyboard()
    )
    
    # Отправляем на проверку админам
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            user = await get_user(payment.user_id)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(
                        admin_id,
                        file_id,
                        caption=(
                            f'{pe("sbp")} <b>Новый СБП платеж</b>\n\n'
                            f'{pe("profile")} Пользователь: <code>{payment.user_id}</code>\n'
                            f'@{user.username or "нет"}\n'
                            f'{pe("money")} Сумма: <b>{payment.amount}₽</b>\n'
                            f'{pe("info")} ID: <code>{payment_id}</code>'
                        ),
                        reply_markup=sbp_approve_keyboard(payment_id, payment.user_id)
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")

# ===== АДМИН: ОДОБРЕНИЕ/ОТКЛОНЕНИЕ СБП =====
@router.callback_query(F.data.startswith("sbp_approve_"))
async def callback_sbp_approve(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    parts = callback.data.replace("sbp_approve_", "").split("_")
    payment_id = "_".join(parts[:-1])
    user_id = int(parts[-1])
    
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment and payment.status != "completed":
            payment.status = "completed"
            user = await session.execute(select(User).where(User.telegram_id == user_id))
            user = user.scalar_one_or_none()
            if user:
                user.balance += payment.amount
            await session.commit()
            
            await callback.message.edit_caption(
                callback.message.caption + f'\n\n{pe("check")} <b>ОДОБРЕНО</b>',
                reply_markup=None
            )
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    f'{pe("check")} <b>Платеж одобрен!</b>\n\n'
                    f'{pe("money")} Зачислено: <b>{payment.amount}₽</b>\n'
                    f'{pe("wallet")} Баланс: <b>{user.balance:.2f}₽</b>'
                )
            except:
                pass

@router.callback_query(F.data.startswith("sbp_reject_"))
async def callback_sbp_reject(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    parts = callback.data.replace("sbp_reject_", "").split("_")
    payment_id = "_".join(parts[:-1])
    user_id = int(parts[-1])
    
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            payment.status = "rejected"
            await session.commit()
            
            await callback.message.edit_caption(
                callback.message.caption + f'\n\n{pe("cross")} <b>ОТКЛОНЕНО</b>',
                reply_markup=None
            )
            
            try:
                await bot.send_message(
                    user_id,
                    f'{pe("cross")} <b>Платеж отклонен</b>\n\n'
                    'Свяжитесь с поддержкой: @v3estnikov'
                )
            except:
                pass

# ===== АДМИН: ПРОВЕРКА СБП =====
@router.callback_query(F.data == "admin_sbp_check")
async def callback_admin_sbp_check(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.method == "sbp", Payment.status == "pending", Payment.screenshot_file_id != None).order_by(Payment.created_at.desc()).limit(10)
        )
        payments = result.scalars().all()
        
        if payments:
            await callback.message.edit_text(f'{pe("sbp")} <b>Загружаю платежи...</b>')
            for payment in payments:
                user = await get_user(payment.user_id)
                try:
                    await bot.send_photo(
                        callback.from_user.id,
                        payment.screenshot_file_id,
                        caption=(
                            f'{pe("sbp")} <b>СБП платеж</b>\n\n'
                            f'{pe("profile")} ID: <code>{payment.user_id}</code>\n'
                            f'@{user.username or "нет"}\n'
                            f'{pe("money")} Сумма: <b>{payment.amount}₽</b>\n'
                            f'{pe("info")} ID: <code>{payment.payment_id}</code>'
                        ),
                        reply_markup=sbp_approve_keyboard(payment.payment_id, payment.user_id)
                    )
                except:
                    pass
            await callback.message.answer(
                f'{pe("info")} <b>Проверьте платежи выше</b>',
                reply_markup=admin_keyboard()
            )
        else:
            await callback.message.edit_text(
                f'{pe("info")} <b>Нет платежей для проверки</b>',
                reply_markup=admin_keyboard()
            )

# ===== АДМИН =====
@router.callback_query(F.data == "admin")
async def callback_admin_return(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.message.edit_text(f'{pe("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def callback_admin(callback: CallbackQuery, state: FSMContext):
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
        builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.edit_text(f'{pe("broadcast")} <b>Рассылка</b>\n\nОтправьте сообщение.', reply_markup=builder.as_markup())
    
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.edit_text(f'{pe("add")} <b>Добавление аккаунта</b>\n\nОтправьте номер: <code>+79001234567</code>', reply_markup=builder.as_markup())
    
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'):
            dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.edit_text(f'{pe("edit")} <b>Изменение баланса</b>\n\nОтправьте ID пользователя.', reply_markup=builder.as_markup())
    
    elif data == "admin_media_menu":
        await callback.message.edit_text(f'{pe("media")} <b>Управление медиа</b>\n\nВыберите раздел:', reply_markup=media_menu_keyboard())
    
    elif data == "admin_clear_media":
        async with async_session() as session:
            await session.execute(sa_text("DELETE FROM media_settings"))
            await session.commit()
        await callback.message.edit_text(f'{pe("check")} <b>Все медиа удалены!</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("set_media_"))
async def callback_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    section = callback.data.replace("set_media_", "")
    await state.set_state(MediaStates.waiting_for_media)
    await state.update_data(section=section)
    sections_names = {"main_menu": "Главное меню", "buy_account": "Покупка аккаунта", "payment_methods": "Способы оплаты", "profile": "Профиль", "my_purchases": "Мои покупки", "deposit": "Пополнение баланса"}
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin_media_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    await callback.message.edit_text(f'{pe("media")} <b>Установка медиа</b>\n\nРаздел: <b>{sections_names.get(section, section)}</b>\n\nОтправьте фото, видео или GIF.', reply_markup=builder.as_markup())

@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def handle_media_upload(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    section = data.get('section')
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"
    else:
        return
    await set_media(section, file_id, file_type, message.caption or None)
    await state.clear()
    sections_names = {"main_menu": "Главное меню", "buy_account": "Покупка аккаунта", "payment_methods": "Способы оплаты", "profile": "Профиль", "my_purchases": "Мои покупки", "deposit": "Пополнение баланса"}
    await message.answer(f'{pe("check")} <b>Медиа установлено!</b>\n\nРаздел: <b>{sections_names.get(section, section)}</b>', reply_markup=admin_keyboard())

# ===== ОБРАБОТЧИК ТЕКСТА =====
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10:
                await message.answer(f'{pe("cross")} <b>Минимум: 10₽</b>', reply_markup=deposit_keyboard())
                return
            payment_id = await generate_payment_id()
            
            if method == "sbp":
                async with async_session() as session:
                    session.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="sbp", status="pending", type="deposit"))
                    await session.commit()
                
                text = (
                    f'{pe("sbp")} <b>Пополнение через СБП</b>\n\n'
                    f'{pe("money")} Сумма: <b>{amount}₽</b>\n\n'
                    f'{pe("bank")} <b>Реквизиты:</b>\n'
                    f'📱 Телефон: <code>{SBP_PHONE}</code>\n'
                    f'🏦 Банк: <b>{SBP_BANK}</b>\n'
                    f'👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n'
                    f'{pe("info")} ID: <code>{payment_id}</code>\n\n'
                    '⚠️ <b>После оплаты нажмите "Я оплатил" и отправьте скриншот</b>'
                )
                await message.answer(text, reply_markup=sbp_payment_keyboard(payment_id))
            
            elif method == "crypto":
                async with async_session() as session:
                    session.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="crypto", status="pending", type="deposit"))
                    await session.commit()
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
                        f'{pe("crypto")} <b>Пополнение Crypto Bot</b>\n\nСумма: <b>{amount}₽</b>\n\n<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n⚠️ <b>После оплаты нажмите проверку</b>',
                        reply_markup=deposit_crypto_check_keyboard(str(invoice_id)),
                        disable_web_page_preview=True
                    )
                else:
                    await message.answer(f'{pe("cross")} <b>Ошибка создания счета</b>', reply_markup=deposit_keyboard())
        except ValueError:
            await message.answer(f'{pe("cross")} <b>Введите число</b>')
        return
    
    # Добавление аккаунта
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
                await message.answer(f'{pe("cross")} <b>Ошибка</b>\n{result.get("error")}', reply_markup=admin_keyboard())
        elif step == 'code':
            code = text
            phone = acc_data['phone']
            phone_code_hash = acc_data['phone_code_hash']
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
                await message.answer(f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n{pe("tag")} Номер: <code>{phone}</code>\n{pe("check")} Верифицирован\n<i>Доступен для покупки</i>', reply_markup=admin_keyboard())
            elif result.get('need_password'):
                acc_data['step'] = 'password'
                await message.answer(f'{pe("lock")} <b>Введите 2FA пароль:</b>')
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(f'{pe("cross")} <b>{result.get("error")}</b>', reply_markup=admin_keyboard())
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
                await message.answer(f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n{pe("tag")} Номер: <code>{phone}</code>\n<i>Доступен для покупки</i>', reply_markup=admin_keyboard())
            else:
                await message.answer(f'{pe("cross")} <b>{result.get("error")}</b>\nПопробуйте еще раз:')
        return
    
    # Изменение баланса
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
                await message.answer(f'{pe("edit")} <b>Изменение баланса</b>\n\nПользователь: <code>{target_id}</code>\nБаланс: <b>{target_user.balance:.0f}₽</b>\n\n<code>+100</code> - пополнить\n<code>-50</code> - списать\n<code>500</code> - установить')
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
                    await message.answer(f'{pe("check")} <b>Баланс изменен!</b>\n\nПользователь: <code>{target_id}</code>\nБыло: <b>{old_balance:.0f}₽</b>\nСтало: <b>{target_user.balance:.0f}₽</b>', reply_markup=admin_keyboard())
            except ValueError:
                await message.answer(f'{pe("cross")} <b>Введите сумму</b>')
        return
    
    # Рассылка
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
        await message.answer(f'{pe("check")} <b>Рассылка завершена</b>\n\nОтправлено: <b>{sent}</b> из <b>{len(users)}</b>', reply_markup=admin_keyboard())
        return
    
    await message.answer(f'{pe("info")} <b>Используйте кнопки меню</b>', reply_markup=main_menu_keyboard())

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
