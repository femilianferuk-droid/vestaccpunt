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

SBP_PHONE = "+79818376180"
SBP_BANK = "ЮMoney"
SBP_RECEIVER = "Иван Б"

CRYPTO_BOT_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_IDS = [7973988177]

# Цены по странам (по умолчанию)
DEFAULT_PRICES = {"США": 20.0, "Россия": 15.0, "Индия": 10.0}

# Коды стран для определения по номеру
COUNTRY_CODES = {
    "1": "США",      # +1 США
    "7": "Россия",   # +7 Россия
    "91": "Индия",   # +91 Индия
}

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

class PriceSettings(Base):
    __tablename__ = "price_settings"
    id = Column(Integer, primary_key=True)
    country = Column(String(50), unique=True, nullable=False)
    price = Column(Float, default=20.0)
    updated_at = Column(DateTime, default=datetime.utcnow)

# ===== FSM =====
class MediaStates(StatesGroup):
    waiting_for_media = State()

class SBPStates(StatesGroup):
    waiting_for_screenshot = State()

class PriceStates(StatesGroup):
    waiting_for_price = State()

try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Failed to create database engine: {e}")
    sys.exit(1)

# ===== МИГРАЦИИ =====
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
    'bot': '6030400221232501136', 'lock': '6037249452824072506',
    'loading': '5345906554510012647', 'check': '5870633910337015697',
    'cross': '5870657884844462243', 'home': '5873147866364514353',
    'profile': '5870994129244131212', 'wallet': '5769126056262898415',
    'money': '5904462880941545555', 'crypto': '5260752406890711732',
    'star': '6041731551845159060', 'location': '6042011682497106307',
    'box': '5884479287171485878', 'tag': '5886285355279193209',
    'code': '5940433880585605708', 'stats': '5870921681735781843',
    'broadcast': '6039422865189638057', 'add': '5771851822897566479',
    'back': '5893057118545646106', 'clock': '5983150113483134607',
    'buy': '5963103826075456248', 'info': '6028435952299413210',
    'edit': '5870676941614354370', 'media': '6035128606563241721',
    'sbp': '5879814368572478751', 'photo': '6035128606563241721',
    'bank': '5904462880941545555', 'settings': '5870982283724328568',
    'russia': '6037397706505195857', 'india': '6037397706505195857',
}

EMOJI_CHARS = {
    'bot': '🤖', 'lock': '🔒', 'loading': '🔄', 'check': '✅',
    'cross': '❌', 'home': '🏘️', 'profile': '👤', 'wallet': '💰',
    'money': '💵', 'crypto': '🪙', 'star': '⭐', 'location': '📍',
    'box': '📦', 'tag': '🏷️', 'code': '🔐', 'stats': '📊',
    'broadcast': '📣', 'add': '➕', 'back': '◀️', 'clock': '⏰',
    'buy': '🛒', 'info': 'ℹ️', 'edit': '✏️', 'media': '🖼️',
    'sbp': '💳', 'photo': '📸', 'bank': '🏦', 'settings': '⚙️',
    'russia': '🇷🇺', 'india': '🇮🇳',
}

def pe(name: str) -> str:
    emoji_id = PREMIUM_EMOJI_IDS.get(name, PREMIUM_EMOJI_IDS['info'])
    char = EMOJI_CHARS.get(name, '📌')
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

# ===== ОПРЕДЕЛЕНИЕ СТРАНЫ =====
def detect_country(phone: str) -> str:
    """Определяет страну по номеру телефона"""
    phone = phone.strip().lstrip('+')
    # Проверяем длинные коды сначала
    for code in sorted(COUNTRY_CODES.keys(), key=len, reverse=True):
        if phone.startswith(code):
            return COUNTRY_CODES[code]
    return "США"  # По умолчанию

# ===== ЦЕНЫ =====
async def get_country_price(country: str) -> float:
    """Получает цену для страны из БД или default"""
    async with async_session() as session:
        r = await session.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps:
            return ps.price
    return DEFAULT_PRICES.get(country, 20.0)

async def set_country_price(country: str, price: float):
    """Устанавливает цену для страны"""
    async with async_session() as session:
        r = await session.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps:
            ps.price = price
            ps.updated_at = datetime.utcnow()
        else:
            ps = PriceSettings(country=country, price=price)
            session.add(ps)
        await session.commit()

async def get_all_prices() -> dict:
    """Возвращает все цены"""
    prices = dict(DEFAULT_PRICES)
    async with async_session() as session:
        r = await session.execute(select(PriceSettings))
        for ps in r.scalars().all():
            prices[ps.country] = ps.price
    return prices

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
            media.file_id = file_id; media.file_type = file_type
            media.caption = caption; media.updated_at = datetime.utcnow()
        else:
            media = MediaSettings(section=section, file_id=file_id, file_type=file_type, caption=caption)
            session.add(media)
        await session.commit()

async def send_media_message(target, section: str, text: str, reply_markup: InlineKeyboardMarkup):
    media = await get_media(section)
    if isinstance(target, CallbackQuery):
        msg = target.message
        try: await msg.delete()
        except: pass
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
        await msg.answer(text, reply_markup=reply_markup)

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

async def countries_keyboard():
    """Клавиатура выбора страны с ценами"""
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()
    flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
    for country in ["США", "Россия", "Индия"]:
        price = prices.get(country, DEFAULT_PRICES.get(country, 20))
        flag = flags.get(country, "")
        builder.row(InlineKeyboardButton(
            text=f"{flag} {country} • {price:.0f}₽",
            callback_data=f"country_{country}"
        ))
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
    builder.row(InlineKeyboardButton(text="Цены на аккаунты", callback_data="admin_prices", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['settings']))
    builder.row(InlineKeyboardButton(text="Управление медиа", callback_data="admin_media_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['media']))
    builder.row(InlineKeyboardButton(text="Проверка СБП", callback_data="admin_sbp_check", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['sbp']))
    builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
    return builder.as_markup()

async def price_settings_keyboard():
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()
    for country in ["США", "Россия", "Индия"]:
        price = prices.get(country, 20)
        builder.row(InlineKeyboardButton(
            text=f"🇺🇸 {country}: {price:.0f}₽" if country == "США" else f"🇷🇺 {country}: {price:.0f}₽" if country == "Россия" else f"🇮🇳 {country}: {price:.0f}₽",
            callback_data=f"set_price_{country}"
        ))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def media_menu_keyboard():
    builder = InlineKeyboardBuilder()
    sections = [
        ("Главное меню", "main_menu", "home"),
        ("Покупка", "buy_account", "buy"),
        ("Оплата", "payment_methods", "money"),
        ("Профиль", "profile", "profile"),
        ("Покупки", "my_purchases", "box"),
        ("Пополнение", "deposit", "wallet"),
    ]
    for name, cb, icon in sections:
        builder.row(InlineKeyboardButton(text=name, callback_data=f"set_media_{cb}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS[icon]))
    builder.row(InlineKeyboardButton(text="Удалить все", callback_data="admin_clear_media", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    return builder.as_markup()

def sbp_approve_keyboard(payment_id: str, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Одобрить", callback_data=f"sbp_approve_{payment_id}_{user_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['check']),
        InlineKeyboardButton(text="Отклонить", callback_data=f"sbp_reject_{payment_id}_{user_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross'])
    )
    return builder.as_markup()

# ===== TELETHON =====
async def create_telethon_client(session_string: str = None) -> TelegramClient:
    return TelegramClient(StringSession(session_string) if session_string else StringSession(), API_ID, API_HASH)

async def send_code_to_phone(phone: str) -> dict:
    try:
        client = await create_telethon_client(); await client.connect()
        sent = await client.send_code_request(phone)
        pending_auth[phone] = {'client': client, 'phone_code_hash': sent.phone_code_hash, 'phone': phone}
        logger.info(f"Code sent to {phone}")
        return {'success': True, 'phone_code_hash': sent.phone_code_hash}
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        return {'success': False, 'error': str(e)}

async def verify_code_and_get_session(phone: str, code: str, phone_code_hash: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data: return {'success': False, 'error': 'Сессия не найдена'}
        client = auth_data['client']
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            logger.info(f"Sign in successful for {phone}")
        except SessionPasswordNeededError:
            return {'success': False, 'need_password': True, 'error': 'Требуется 2FA'}
        session_string = client.session.save()
        me = await client.get_me()
        logger.info(f"Verified as: {me.phone}, session saved")
        await client.disconnect()
        pending_auth.pop(phone, None)
        return {'success': True, 'session_string': session_string}
    except PhoneCodeInvalidError:
        logger.error(f"Invalid code for {phone}")
        return {'success': False, 'error': 'Неверный код. Проверьте и попробуйте снова.'}
    except PhoneCodeExpiredError:
        return {'success': False, 'error': 'Код истек. Отправьте номер заново.'}
    except Exception as e:
        logger.error(f"Error verifying: {e}")
        return {'success': False, 'error': str(e)}

async def verify_2fa_password(phone: str, password: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data: return {'success': False, 'error': 'Сессия не найдена'}
        await auth_data['client'].sign_in(password=password)
        session_string = auth_data['client'].session.save()
        await auth_data['client'].disconnect()
        pending_auth.pop(phone, None)
        logger.info(f"2FA verified for {phone}")
        return {'success': True, 'session_string': session_string}
    except PasswordHashInvalidError:
        return {'success': False, 'error': 'Неверный пароль'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

async def get_code_from_session(session_string: str) -> Optional[str]:
    """Получает код из чата Telegram (ищет во всех диалогах)"""
    client = None
    try:
        logger.info("Starting code search...")
        client = await create_telethon_client(session_string)
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.error("Session not authorized")
            return None
        
        me = await client.get_me()
        logger.info(f"Searching code for account: {me.phone}")
        
        # Ищем во всех диалогах
        async for dialog in client.iter_dialogs():
            dialog_name = (dialog.name or "").lower()
            is_target = any(x in dialog_name for x in [
                "42777", "telegram", "код", "code", "login", "verify",
                "подтверждени", "авторизаци", "вход"
            ])
            if is_target:
                logger.info(f"Checking dialog: {dialog.name}")
                messages = await client.get_messages(dialog, limit=10)
                for msg in messages:
                    if msg.text:
                        codes = re.findall(r'\b\d{5}\b', msg.text)
                        if codes:
                            logger.info(f"FOUND CODE: {codes[0]}")
                            return codes[0]
        
        # Ищем везде
        logger.info("Searching all dialogs...")
        async for dialog in client.iter_dialogs():
            messages = await client.get_messages(dialog, limit=3)
            for msg in messages:
                if msg.text:
                    codes = re.findall(r'\b\d{5}\b', msg.text)
                    if codes:
                        logger.info(f"FOUND CODE in {dialog.name}: {codes[0]}")
                        return codes[0]
        
        logger.info("No code found")
        return None
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        return None
    finally:
        if client:
            await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
async def get_user(user_id: int):
    async with async_session() as session:
        r = await session.execute(select(User).where(User.telegram_id == user_id))
        return r.scalar_one_or_none()

async def get_or_create_user(user_id: int, username: str = None):
    user = await get_user(user_id)
    if not user:
        async with async_session() as session:
            user = User(telegram_id=user_id, username=username, is_admin=(user_id in ADMIN_IDS))
            session.add(user); await session.commit(); await session.refresh(user)
    return user

async def get_available_account(country: str = None):
    """Получает доступный аккаунт, опционально фильтруя по стране"""
    async with async_session() as session:
        q = select(Account).where(
            Account.is_sold == False, Account.is_verified == True,
            Account.session_string != None, Account.session_string != ""
        )
        if country:
            q = q.where(Account.country == country)
        r = await session.execute(q.limit(1))
        return r.scalar_one_or_none()

async def get_available_countries() -> list:
    """Возвращает список стран с доступными аккаунтами"""
    async with async_session() as session:
        r = await session.execute(
            select(Account.country, func.count(Account.id))
            .where(Account.is_sold == False, Account.is_verified == True)
            .group_by(Account.country)
        )
        return [(row[0], row[1]) for row in r.all()]

async def create_crypto_bot_invoice(amount: float, payment_id: str) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        payload = {
            "asset": "USDT", "amount": str(round(amount / 90, 2)),
            "description": f"Vest #{payment_id}", "payload": payment_id,
            "allow_comments": False, "allow_anonymous": False, "expires_in": 3600
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers, timeout=30) as resp:
                return await resp.json()
    except: return None

async def check_crypto_bot_invoice(invoice_id: int) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={"invoice_ids": str(invoice_id)}, headers=headers, timeout=30) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("result", {}).get("items"):
                    return data["result"]["items"][0]
        return None
    except: return None

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
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    await send_media_message(callback, "main_menu", f'{pe("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>', main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def cb_buy_account(callback: CallbackQuery):
    await callback.answer()
    available = await get_available_countries()
    text = f'{pe("location")} <b>Выберите страну</b>\n\n'
    if available:
        text += f'{pe("check")} Доступные страны:'
    else:
        text += f'{pe("cross")} Нет доступных аккаунтов'
    await send_media_message(callback, "buy_account", text, await countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def cb_country(callback: CallbackQuery):
    await callback.answer()
    country = callback.data.replace("country_", "")
    account = await get_available_account(country)
    
    if account:
        price = await get_country_price(country)
        if not hasattr(dp, 'pending_accounts'): dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {'account_id': account.id, 'price': price, 'country': country}
        
        flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
        flag = flags.get(country, "")
        
        text = (
            f'{pe("check")} <b>Аккаунт найден!</b>\n\n'
            f'{pe("location")} Страна: {flag} <b>{country}</b>\n'
            f'{pe("money")} Цена: <b>{price:.0f}₽</b>\n\n'
            '<i>Нажмите КУПИТЬ</i>'
        )
        await callback.message.answer(text, reply_markup=account_found_keyboard())
    else:
        await callback.message.answer(
            f'{pe("cross")} <b>Нет доступных аккаунтов для {country}</b>',
            reply_markup=await countries_keyboard()
        )

@router.callback_query(F.data == "show_payment_methods")
async def cb_show_payment(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    text = f'{pe("buy")} <b>Покупка аккаунта</b>\n\n{pe("money")} Сумма: <b>{pending.get("price", 20):.0f}₽</b>\n\n<i>Выберите способ:</i>'
    await send_media_message(callback, "payment_methods", text, payment_methods_keyboard())

@router.callback_query(F.data == "pay_balance")
async def cb_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price, account_id = pending.get('price', 20), pending.get('account_id')
    if not account_id:
        await callback.message.answer(f'{pe("cross")} <b>Ошибка</b>', reply_markup=main_menu_keyboard())
        return
    if user.balance >= price:
        async with async_session() as session:
            user = await session.get(User, user.id)
            account = await session.get(Account, account_id)
            if account.is_sold:
                await callback.message.answer(f'{pe("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard())
                return
            user.balance -= price; user.total_spent = (user.total_spent or 0) + price; account.is_sold = True
            purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="balance")
            session.add(purchase); await session.commit(); await session.refresh(purchase)
            text = f'{pe("check")} <b>Оплата успешна!</b>\n\n{pe("tag")} Номер: <code>{account.phone}</code>\n{pe("money")} Сумма: <b>{price:.0f}₽</b>\n\nНажмите для кода:'
            await callback.message.answer(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = f'{pe("cross")} <b>Недостаточно средств</b>\n\n{pe("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n{pe("money")} Нужно: <b>{price:.0f}₽</b>'
        await callback.message.answer(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_sbp")
async def cb_pay_sbp(callback: CallbackQuery):
    await callback.answer()
    text = f'{pe("sbp")} <b>Оплата через СБП</b>\n\n{pe("info")} Для оплаты товара через СБП необходимо пополнить баланс.\n\nПерейдите в "Пополнить" в главном меню.'
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Пополнить баланс", callback_data="deposit_balance", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['wallet']))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="show_payment_methods", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['back']))
    await callback.message.answer(text, reply_markup=builder.as_markup())

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20); payment_id = await generate_payment_id()
    async with async_session() as session:
        session.add(Payment(user_id=callback.from_user.id, amount=price, payment_id=payment_id, method="crypto", status="pending", type="purchase"))
        await session.commit()
    await callback.message.answer(f'{pe("loading")} <b>Создаю счет...</b>')
    invoice = await create_crypto_bot_invoice(price, payment_id)
    if invoice and invoice.get("ok"):
        r = invoice.get("result", {})
        async with async_session() as session:
            p = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
            p = p.scalar_one_or_none()
            if p: p.payment_id = str(r.get("invoice_id")); await session.commit()
        text = f'{pe("crypto")} <b>Оплата Crypto Bot</b>\n\nСумма: <b>{price:.0f}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ После оплаты нажмите проверку'
        await callback.message.answer(text, reply_markup=check_crypto_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
    else:
        await callback.message.answer(f'{pe("cross")} <b>Ошибка</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{pe("star")} <b>Оплата Telegram Stars</b>\n\nНапишите: <b>@v3estnikov</b>', reply_markup=payment_methods_keyboard())

# ===== ПРОВЕРКА CRYPTO =====
@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def cb_check_purchase(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_purchase_crypto_", "")
    await callback.message.answer(f'{pe("loading")} <b>Проверяю...</b>', reply_markup=check_crypto_keyboard(payment_id))
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
        pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
        account_id, price = pending.get('account_id'), pending.get('price', 20)
        if account_id:
            async with async_session() as session:
                account = await session.get(Account, account_id)
                if account.is_sold:
                    await callback.message.answer(f'{pe("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard())
                    return
                pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
                pr = pr.scalar_one_or_none()
                if pr: pr.status = "completed"
                user = await session.get(User, callback.from_user.id)
                if user: user.total_spent = (user.total_spent or 0) + price
                account.is_sold = True
                purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="crypto")
                session.add(purchase); await session.commit(); await session.refresh(purchase)
                await callback.message.answer(
                    f'{pe("check")} <b>Оплата подтверждена!</b>\n\n{pe("tag")} Номер: <code>{account.phone}</code>\n{pe("money")} Сумма: <b>{price:.0f}₽</b>\n\nНажмите для кода:',
                    reply_markup=get_code_keyboard(purchase.id))
    else:
        await callback.answer("⏳ Не найдено", show_alert=True)

@router.callback_query(F.data.startswith("check_deposit_crypto_"))
async def cb_check_deposit(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_deposit_crypto_", "")
    await callback.message.answer(f'{pe("loading")} <b>Проверяю...</b>', reply_markup=deposit_crypto_check_keyboard(payment_id))
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
        async with async_session() as session:
            pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
            payment = pr.scalar_one_or_none()
            if payment and payment.status != "completed":
                payment.status = "completed"
                user = await session.get(User, callback.from_user.id)
                deposit_amount = payment.amount; user.balance += deposit_amount
                await session.commit()
                text = f'{pe("check")} <b>Баланс пополнен!</b>\n\n{pe("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n{pe("wallet")} Баланс: <b>{user.balance:.2f}₽</b>'
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
                await callback.message.answer(text, reply_markup=builder.as_markup())
    else:
        await callback.answer("⏳ Не найдено", show_alert=True)

# ===== ПОЛУЧЕНИЕ КОДА (бесконечное) =====
@router.callback_query(F.data.startswith("get_code_"))
async def cb_get_code(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_code_", ""))
    
    async with async_session() as session:
        r = await session.execute(select(Purchase).where(Purchase.id == purchase_id))
        purchase = r.scalar_one_or_none()
        
        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("Не найдена", show_alert=True)
            return
        
        account = await session.get(Account, purchase.account_id)
        if not account or not account.session_string:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        
        status_msg = await callback.message.answer(f'{pe("loading")} <b>Получаю код...</b>')
        code = await get_code_from_session(account.session_string)
        await status_msg.delete()
        
        if code:
            # Не ставим code_sent=True, чтобы можно было получать код многократно
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="Получить еще раз", callback_data=f"get_code_{purchase_id}", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['code']))
            builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
            
            await callback.message.answer(
                f'{pe("check")} <b>Код получен!</b>\n\n'
                f'{pe("tag")} Номер: <code>{account.phone}</code>\n'
                f'{pe("lock")} Код: <code>{code}</code>\n\n'
                '⚠️ <i>Код можно получить повторно</i>',
                reply_markup=builder.as_markup()
            )
        else:
            await callback.message.answer(
                f'{pe("cross")} <b>Не удалось получить код</b>\n\n'
                'Попробуйте позже или @v3estnikov',
                reply_markup=get_code_keyboard(purchase_id)
            )

@router.callback_query(F.data == "my_purchases")
async def cb_my_purchases(callback: CallbackQuery):
    await callback.answer()
    async with async_session() as session:
        r = await session.execute(select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc()))
        purchases = r.scalars().all()
        if purchases:
            text = f'{pe("box")} <b>Ваши покупки</b>\n\n'
            builder = InlineKeyboardBuilder()
            for p in purchases:
                account = await session.get(Account, p.account_id)
                phone = account.phone if account else "Н/Д"
                date = p.created_at.strftime('%d.%m.%y')
                text += f'📱 <code>{phone}</code> • {p.amount:.0f}₽ • {date}\n'
                builder.row(InlineKeyboardButton(
                    text=f"Получить код • {phone}",
                    callback_data=f"get_code_{p.id}",
                    icon_custom_emoji_id=PREMIUM_EMOJI_IDS['code']
                ))
            builder.row(InlineKeyboardButton(text="В меню", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['home']))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())
        else:
            await send_media_message(callback, "my_purchases", f'{pe("box")} <b>Мои покупки</b>\n\nУ вас пока нет покупок.', main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    async with async_session() as session:
        r = await session.execute(select(func.count(Purchase.id)).where(Purchase.user_id == callback.from_user.id))
        cnt = r.scalar() or 0
    text = (
        f'{pe("profile")} <b>Профиль</b>\n\n'
        f'{pe("tag")} ID: <code>{user.telegram_id}</code>\n'
        f'{pe("profile")} @{user.username or "нет"}\n\n'
        '━━━ 💰 БАЛАНС ━━━\n'
        f'{pe("wallet")} <b>{user.balance:.0f}₽</b>\n'
        '━━━━━━━━━━━━━━\n\n'
        '━━ 📊 СТАТИСТИКА ━━\n'
        f'{pe("box")} Покупок: <b>{cnt}</b>\n'
        f'{pe("money")} Потрачено: <b>{(user.total_spent or 0):.0f}₽</b>\n'
        f'{pe("clock")} С нами: {user.created_at.strftime("%d.%m.%Y")}\n'
        '━━━━━━━━━━━━━━'
    )
    await send_media_message(callback, "profile", text, profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def cb_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    await send_media_message(callback, "deposit", f'{pe("wallet")} <b>Пополнение баланса</b>\n\n{pe("sbp")} <b>СБП</b> - перевод\n{pe("crypto")} <b>Crypto Bot</b> - криптовалюта\n\n<i>Минимум: 10₽</i>', deposit_keyboard())

@router.callback_query(F.data == "deposit_sbp")
async def cb_deposit_sbp(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{pe("sbp")} <b>Введите сумму (от 10₽)</b>\n\n<i>Отправьте число в чат</i>')
    if not hasattr(dp, 'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'

@router.callback_query(F.data == "deposit_crypto")
async def cb_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{pe("crypto")} <b>Введите сумму (от 10₽)</b>\n\n<i>Отправьте число в чат</i>')
    if not hasattr(dp, 'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

# ===== СБП =====
@router.callback_query(F.data.startswith("sbp_paid_"))
async def cb_sbp_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    payment_id = callback.data.replace("sbp_paid_", "")
    await state.set_state(SBPStates.waiting_for_screenshot)
    await state.update_data(payment_id=payment_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="main_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    await callback.message.answer(f'{pe("photo")} <b>Отправьте скриншот оплаты</b>', reply_markup=builder.as_markup())

@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def h_sbp_screenshot(message: Message, state: FSMContext):
    data = await state.get_data(); payment_id = data.get('payment_id'); await state.clear()
    file_id = message.photo[-1].file_id
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment: payment.screenshot_file_id = file_id; await session.commit()
    await message.answer(f'{pe("check")} <b>Скриншот отправлен!</b>', reply_markup=main_menu_keyboard())
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            user = await get_user(payment.user_id)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(admin_id, file_id,
                        caption=f'{pe("sbp")} <b>СБП платеж</b>\n\n{pe("profile")} ID: <code>{payment.user_id}</code>\n@{user.username or "нет"}\n{pe("money")} Сумма: <b>{payment.amount}₽</b>\n{pe("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n{pe("info")} ID: <code>{payment_id}</code>',
                        reply_markup=sbp_approve_keyboard(payment_id, payment.user_id))
                except: pass

@router.callback_query(F.data.startswith("sbp_approve_"))
async def cb_sbp_approve(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_approve_", "").rsplit("_", 1)
    payment_id, user_id = parts[0], int(parts[1])
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment and payment.status != "completed":
            payment.status = "completed"
            user = await session.execute(select(User).where(User.telegram_id == user_id))
            user = user.scalar_one_or_none()
            if user:
                old_balance = user.balance; user.balance += payment.amount; new_balance = user.balance
                await session.commit()
                await callback.message.edit_caption(
                    f'{callback.message.caption}\n\n{pe("check")} <b>ОДОБРЕНО</b>\n💰 Баланс был: <b>{old_balance:.0f}₽</b> → стал: <b>{new_balance:.0f}₽</b>',
                    reply_markup=None)
                try:
                    await bot.send_message(user_id,
                        f'{pe("check")} <b>Платеж одобрен!</b>\n\n{pe("money")} Зачислено: <b>{payment.amount}₽</b>\n{pe("wallet")} Баланс: <b>{new_balance:.0f}₽</b>')
                except: pass

@router.callback_query(F.data.startswith("sbp_reject_"))
async def cb_sbp_reject(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_reject_", "").rsplit("_", 1)
    payment_id, user_id = parts[0], int(parts[1])
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            payment.status = "rejected"; await session.commit()
            await callback.message.edit_caption(f'{callback.message.caption}\n\n{pe("cross")} <b>ОТКЛОНЕНО</b>', reply_markup=None)
            try: await bot.send_message(user_id, f'{pe("cross")} <b>Платеж отклонен</b>\n\nСвяжитесь с поддержкой: @v3estnikov')
            except: pass

@router.callback_query(F.data == "admin_sbp_check")
async def cb_admin_sbp(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as session:
        r = await session.execute(select(Payment).where(Payment.method == "sbp", Payment.status == "pending", Payment.screenshot_file_id != None).order_by(Payment.created_at.desc()).limit(10))
        payments = r.scalars().all()
        if payments:
            await callback.message.answer(f'{pe("sbp")} <b>Загружаю...</b>')
            for payment in payments:
                user = await get_user(payment.user_id)
                try:
                    await bot.send_photo(callback.from_user.id, payment.screenshot_file_id,
                        caption=f'{pe("sbp")} <b>СБП платеж</b>\n\n{pe("profile")} ID: <code>{payment.user_id}</code>\n@{user.username or "нет"}\n{pe("money")} Сумма: <b>{payment.amount}₽</b>\n{pe("wallet")} Баланс: <b>{user.balance:.0f}₽</b>',
                        reply_markup=sbp_approve_keyboard(payment.payment_id, payment.user_id))
                except: pass
            await callback.message.answer(f'{pe("info")} Проверьте платежи', reply_markup=admin_keyboard())
        else:
            await callback.message.answer(f'{pe("info")} <b>Нет платежей</b>', reply_markup=admin_keyboard())

# ===== АДМИН =====
@router.callback_query(F.data == "admin")
async def cb_admin_return(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.message.answer(f'{pe("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def cb_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: await callback.answer("❌", show_alert=True); return
    data = callback.data
    
    if data == "admin_stats":
        async with async_session() as session:
            users_cnt = (await session.execute(select(func.count(User.id)))).scalar() or 0
            accs_cnt = (await session.execute(select(func.count(Account.id)))).scalar() or 0
            sold_cnt = (await session.execute(select(func.count(Account.id)).where(Account.is_sold == True))).scalar() or 0
            verif_cnt = (await session.execute(select(func.count(Account.id)).where(Account.is_verified == True))).scalar() or 0
            purch_cnt = (await session.execute(select(func.count(Purchase.id)))).scalar() or 0
            revenue = (await session.execute(select(func.sum(Purchase.amount)))).scalar() or 0
            text = f'{pe("stats")} <b>Статистика</b>\n\n{pe("profile")} Пользователей: <b>{users_cnt}</b>\n{pe("box")} Аккаунтов: <b>{accs_cnt}</b>\n{pe("check")} Верифицировано: <b>{verif_cnt}</b>\n{pe("buy")} Продано: <b>{sold_cnt}</b>\n{pe("box")} Покупок: <b>{purch_cnt}</b>\n{pe("money")} Выручка: <b>{revenue:.0f}₽</b>'
            await callback.message.answer(text, reply_markup=admin_keyboard())
    
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'): dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        builder = InlineKeyboardBuilder(); builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.answer(f'{pe("broadcast")} <b>Рассылка</b>\n\nОтправьте сообщение.', reply_markup=builder.as_markup())
    
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'): dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        builder = InlineKeyboardBuilder(); builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.answer(f'{pe("add")} <b>Добавление аккаунта</b>\n\nОтправьте номер: <code>+79001234567</code>\n\nСтрана определится автоматически.', reply_markup=builder.as_markup())
    
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'): dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}
        builder = InlineKeyboardBuilder(); builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
        await callback.message.answer(f'{pe("edit")} <b>Изменение баланса</b>\n\nОтправьте ID пользователя.', reply_markup=builder.as_markup())
    
    elif data == "admin_prices":
        await callback.message.answer(f'{pe("settings")} <b>Цены на аккаунты</b>\n\nВыберите страну для изменения:', reply_markup=await price_settings_keyboard())
    
    elif data == "admin_media_menu":
        await callback.message.answer(f'{pe("media")} <b>Управление медиа</b>', reply_markup=media_menu_keyboard())
    
    elif data == "admin_clear_media":
        async with async_session() as session: await session.execute(sa_text("DELETE FROM media_settings")); await session.commit()
        await callback.message.answer(f'{pe("check")} <b>Медиа удалены!</b>', reply_markup=admin_keyboard())

# ===== ЦЕНЫ =====
@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    country = callback.data.replace("set_price_", "")
    await state.set_state(PriceStates.waiting_for_price)
    await state.update_data(country=country)
    current = await get_country_price(country)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin_prices", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    await callback.message.answer(
        f'{pe("edit")} <b>Изменение цены: {country}</b>\n\nТекущая: <b>{current:.0f}₽</b>\n\nОтправьте новую цену:',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(PriceStates.waiting_for_price), F.text)
async def h_set_price(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); country = data.get('country'); await state.clear()
    try:
        price = float(message.text.strip().replace(',', '.'))
        if price <= 0: await message.answer(f'{pe("cross")} Цена должна быть > 0', reply_markup=admin_keyboard()); return
        await set_country_price(country, price)
        await message.answer(f'{pe("check")} <b>Цена обновлена!</b>\n\n{country}: <b>{price:.0f}₽</b>', reply_markup=admin_keyboard())
    except: await message.answer(f'{pe("cross")} Введите число', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("set_media_"))
async def cb_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    section = callback.data.replace("set_media_", "")
    await state.set_state(MediaStates.waiting_for_media); await state.update_data(section=section)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    builder = InlineKeyboardBuilder(); builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin_media_menu", icon_custom_emoji_id=PREMIUM_EMOJI_IDS['cross']))
    await callback.message.answer(f'{pe("media")} <b>Установка медиа</b>\n\nРаздел: <b>{names.get(section, section)}</b>', reply_markup=builder.as_markup())

@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def h_media(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); section = data.get('section'); await state.clear()
    if message.photo: fid, ftype = message.photo[-1].file_id, "photo"
    elif message.video: fid, ftype = message.video.file_id, "video"
    else: fid, ftype = message.animation.file_id, "animation"
    await set_media(section, fid, ftype, message.caption)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    await message.answer(f'{pe("check")} <b>Медиа установлено!</b>\n\nРаздел: <b>{names.get(section, section)}</b>', reply_markup=admin_keyboard())

# ===== ТЕКСТ =====
@router.message(F.text)
async def h_text(message: Message, state: FSMContext):
    user_id = message.from_user.id; text = message.text.strip()
    
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10: await message.answer(f'{pe("cross")} <b>Минимум: 10₽</b>', reply_markup=deposit_keyboard()); return
            payment_id = await generate_payment_id()
            if method == "sbp":
                async with async_session() as session:
                    session.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="sbp", status="pending", type="deposit"))
                    await session.commit()
                await message.answer(
                    f'{pe("sbp")} <b>Пополнение через СБП</b>\n\n{pe("money")} Сумма: <b>{amount}₽</b>\n\n{pe("bank")} <b>Реквизиты:</b>\n📱 <code>{SBP_PHONE}</code>\n🏦 Банк: <b>{SBP_BANK}</b>\n👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n{pe("info")} ID: <code>{payment_id}</code>\n\n⚠️ Нажмите "Я оплатил"',
                    reply_markup=sbp_payment_keyboard(payment_id))
            elif method == "crypto":
                async with async_session() as session:
                    session.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="crypto", status="pending", type="deposit"))
                    await session.commit()
                await message.answer(f'{pe("loading")} Создаю счет...')
                invoice = await create_crypto_bot_invoice(amount, payment_id)
                if invoice and invoice.get("ok"):
                    r = invoice.get("result", {})
                    async with async_session() as session:
                        p = await session.execute(select(Payment).where(Payment.payment_id == payment_id)); p = p.scalar_one_or_none()
                        if p: p.payment_id = str(r.get("invoice_id")); await session.commit()
                    await message.answer(f'{pe("crypto")} <b>Пополнение Crypto Bot</b>\n\nСумма: <b>{amount}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ Нажмите проверку', reply_markup=deposit_crypto_check_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
        except ValueError: await message.answer(f'{pe("cross")} <b>Введите число</b>')
        return
    
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]; step = acc_data.get('step')
        if step == 'phone':
            phone = text
            country = detect_country(phone)
            price = await get_country_price(country)
            acc_data['phone'] = phone; acc_data['country'] = country; acc_data['price'] = price
            await message.answer(f'{pe("loading")} Отправляю код на {phone}...\nСтрана: {country}')
            r = await send_code_to_phone(phone)
            if r['success']:
                acc_data['phone_code_hash'] = r['phone_code_hash']; acc_data['step'] = 'code'
                await message.answer(f'{pe("check")} <b>Код отправлен на {phone}</b>\n\nСтрана: <b>{country}</b>\nЦена: <b>{price:.0f}₽</b>\n\nВведите код:')
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(f'{pe("cross")} <b>Ошибка:</b> {r.get("error")}', reply_markup=admin_keyboard())
        elif step == 'code':
            phone = acc_data['phone']; country = acc_data['country']; price = acc_data['price']
            await message.answer(f'{pe("loading")} Проверяю код...')
            r = await verify_code_and_get_session(phone, text, acc_data['phone_code_hash'])
            if r['success']:
                async with async_session() as session:
                    ex = await session.execute(select(Account).where(Account.phone == phone)); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string = r['session_string']; ex.is_verified = True; ex.is_sold = False; ex.country = country; ex.price = price
                    else: session.add(Account(phone=phone, country=country, price=price, session_string=r['session_string'], is_verified=True, is_sold=False))
                    await session.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n{pe("tag")} Номер: <code>{phone}</code>\n{pe("location")} Страна: <b>{country}</b>\n{pe("money")} Цена: <b>{price:.0f}₽</b>\n{pe("check")} Верифицирован\n<i>Доступен для покупки</i>',
                    reply_markup=admin_keyboard())
            elif r.get('need_password'): acc_data['step'] = 'password'; await message.answer(f'{pe("lock")} <b>Введите 2FA пароль:</b>')
            else: del dp.awaiting_accounts[user_id]; await message.answer(f'{pe("cross")} <b>{r.get("error")}</b>', reply_markup=admin_keyboard())
        elif step == 'password':
            phone = acc_data['phone']; country = acc_data['country']; price = acc_data['price']
            r = await verify_2fa_password(phone, text)
            if r['success']:
                async with async_session() as session:
                    ex = await session.execute(select(Account).where(Account.phone == phone)); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string = r['session_string']; ex.is_verified = True; ex.is_sold = False; ex.country = country; ex.price = price
                    else: session.add(Account(phone=phone, country=country, price=price, session_string=r['session_string'], is_verified=True, is_sold=False))
                    await session.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(f'{pe("check")} <b>Аккаунт добавлен!</b>\n\n{pe("tag")} Номер: <code>{phone}</code>\n{pe("location")} Страна: <b>{country}</b>\n{pe("money")} Цена: <b>{price:.0f}₽</b>\n<i>Доступен</i>', reply_markup=admin_keyboard())
            else: await message.answer(f'{pe("cross")} <b>{r.get("error")}</b>\nПопробуйте еще раз:')
        return
    
    if hasattr(dp, 'awaiting_balance') and user_id in dp.awaiting_balance:
        bal_data = dp.awaiting_balance[user_id]; step = bal_data.get('step')
        if step == 'user_id':
            try:
                target = await get_user(int(text))
                if not target: await message.answer(f'{pe("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); del dp.awaiting_balance[user_id]; return
                bal_data['target_id'] = int(text); bal_data['step'] = 'amount'
                await message.answer(f'{pe("edit")} <b>Изменение баланса</b>\n\nID: <code>{text}</code>\nБаланс: <b>{target.balance:.0f}₽</b>\n\n<code>+100</code> / <code>-50</code> / <code>500</code>')
            except: await message.answer(f'{pe("cross")} <b>Введите ID</b>')
        elif step == 'amount':
            try:
                target_id = bal_data['target_id']
                async with async_session() as session:
                    target = await session.execute(select(User).where(User.telegram_id == target_id)); target = target.scalar_one_or_none()
                    if not target: del dp.awaiting_balance[user_id]; await message.answer(f'{pe("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); return
                    old = target.balance
                    if text.startswith('+'): target.balance += float(text[1:])
                    elif text.startswith('-'): target.balance = max(0, target.balance - float(text[1:]))
                    else: target.balance = float(text)
                    await session.commit()
                    del dp.awaiting_balance[user_id]
                    await message.answer(f'{pe("check")} <b>Баланс изменен!</b>\n\nID: <code>{target_id}</code>\nБыло: <b>{old:.0f}₽</b>\nСтало: <b>{target.balance:.0f}₽</b>', reply_markup=admin_keyboard())
            except: await message.answer(f'{pe("cross")} <b>Введите сумму</b>')
        return
    
    if hasattr(dp, 'awaiting_broadcast') and user_id in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(user_id)
        async with async_session() as session:
            users = (await session.execute(select(User))).scalars().all()
            sent = 0
            for u in users:
                try: await message.copy_to(chat_id=u.telegram_id); sent += 1; await asyncio.sleep(0.05)
                except: pass
        await message.answer(f'{pe("check")} <b>Рассылка завершена</b>\n\n{sent}/{len(users)}', reply_markup=admin_keyboard())
        return
    
    await message.answer(f'{pe("info")} <b>Используйте кнопки меню</b>', reply_markup=main_menu_keyboard())

# ===== ЗАПУСК =====
async def setup_db():
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)

async def main():
    await setup_db(); await run_migrations()
    for attr in ['pending_accounts', 'awaiting_deposit', 'awaiting_accounts', 'awaiting_balance']:
        if not hasattr(dp, attr): setattr(dp, attr, {})
    if not hasattr(dp, 'awaiting_broadcast'): dp.awaiting_broadcast = set()
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
