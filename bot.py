import asyncio
import logging
import os
import re
import sys
import io
import zipfile
import base64
from datetime import datetime, timedelta
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
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
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

DEFAULT_PRICES = {"США": 20.0, "Россия": 15.0, "Индия": 10.0}
COUNTRY_CODES = {"1": "США", "7": "Россия", "91": "Индия"}

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
    tdata_zip_base64 = Column(Text, nullable=True)
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

class PromoCode(Base):
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    amount = Column(Float, default=0.0)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PromoUsage(Base):
    __tablename__ = "promo_usages"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    promo_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class RequiredChannel(Base):
    __tablename__ = "required_channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(255), nullable=False)
    channel_url = Column(String(255), nullable=False)
    channel_name = Column(String(255), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

# ===== FSM =====
class MediaStates(StatesGroup):
    waiting_for_media = State()

class SBPStates(StatesGroup):
    waiting_for_screenshot = State()

class PriceStates(StatesGroup):
    waiting_for_price = State()

class PromoStates(StatesGroup):
    waiting_for_promo_data = State()
    waiting_for_promo_code = State()

class ChannelStates(StatesGroup):
    waiting_for_channel = State()

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
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS tdata_zip_base64 TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
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
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
pending_auth = {}

# ===== ПРЕМИУМ ЭМОДЗИ ID =====
EMOJI_ID = {
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
    'gift': '6032644646587338669', 'users': '5870772616305839506',
    'delete': '5870875489362513438', 'subscribe': '6039486778597970865',
    'promo': '6032644646587338669', 'file': '5870528606328852614',
    'download': '6039802767931871481', 'key': '6037249452824072506',
    'channel': '6039422865189638057', 'accept': '5774022692642492953',
    'reject': '5774077015388852135',
}

EMOJI_CHAR = {
    'bot': '🤖', 'lock': '🔒', 'loading': '🔄', 'check': '✅',
    'cross': '❌', 'home': '🏘️', 'profile': '👤', 'wallet': '💰',
    'money': '💵', 'crypto': '🪙', 'star': '⭐', 'location': '📍',
    'box': '📦', 'tag': '🏷️', 'code': '🔐', 'stats': '📊',
    'broadcast': '📣', 'add': '➕', 'back': '◀️', 'clock': '⏰',
    'buy': '🛒', 'info': 'ℹ️', 'edit': '✏️', 'media': '🖼️',
    'sbp': '💳', 'photo': '📸', 'bank': '🏦', 'settings': '⚙️',
    'gift': '🎁', 'users': '👥', 'delete': '🗑️', 'subscribe': '🔔',
    'promo': '🎟️', 'file': '📁', 'download': '⬇️', 'key': '🔑',
    'channel': '📢', 'accept': '✅', 'reject': '❌',
}

def tg_emoji(name: str) -> str:
    emoji_id = EMOJI_ID.get(name, EMOJI_ID['info'])
    char = EMOJI_CHAR.get(name, '📌')
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

def create_button(text: str, callback_data: str = None, url: str = None, style: str = None, emoji: str = None) -> InlineKeyboardButton:
    kwargs = {'text': text}
    if callback_data: kwargs['callback_data'] = callback_data
    if url: kwargs['url'] = url
    if style and style in ['primary', 'success', 'danger', 'default']: kwargs['style'] = style
    if emoji and emoji in EMOJI_ID: kwargs['icon_custom_emoji_id'] = EMOJI_ID[emoji]
    return InlineKeyboardButton(**kwargs)

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        create_button("Купить аккаунт", callback_data="buy_account", style="primary", emoji="buy"),
        create_button("Мои покупки", callback_data="my_purchases", style="default", emoji="box")
    )
    builder.row(
        create_button("Профиль", callback_data="profile", style="default", emoji="profile"),
        create_button("Пополнить", callback_data="deposit_balance", style="success", emoji="wallet")
    )
    return builder.as_markup()

async def countries_keyboard():
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()
    flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
    styles = ["primary", "success", "danger"]
    for i, country in enumerate(["США", "Россия", "Индия"]):
        price = prices.get(country, DEFAULT_PRICES.get(country, 20))
        builder.row(create_button(f"{flags.get(country, '')} {country} • {price:.0f}₽", callback_data=f"country_{country}", style=styles[i], emoji="location"))
    builder.row(create_button("Назад", callback_data="main_menu", style="default", emoji="back"))
    return builder.as_markup()

def account_found_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("КУПИТЬ", callback_data="show_payment_methods", style="success", emoji="buy"))
    builder.row(create_button("Назад", callback_data="buy_account", style="default", emoji="back"))
    return builder.as_markup()

def payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Баланс бота", callback_data="pay_balance", style="primary", emoji="wallet"))
    builder.row(create_button("СБП", callback_data="pay_sbp", style="default", emoji="sbp"))
    builder.row(create_button("Crypto Bot", callback_data="pay_crypto", style="success", emoji="crypto"))
    builder.row(create_button("Telegram Stars", callback_data="pay_stars", style="default", emoji="star"))
    builder.row(create_button("Назад", callback_data="buy_account", style="default", emoji="back"))
    return builder.as_markup()

def check_crypto_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Проверить оплату", callback_data=f"check_purchase_crypto_{payment_id}", style="primary", emoji="loading"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", emoji="cross"))
    return builder.as_markup()

def get_code_keyboard(purchase_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Получить код", callback_data=f"get_code_{purchase_id}", style="primary", emoji="code"))
    builder.row(create_button("Получить .session", callback_data=f"get_session_{purchase_id}", style="default", emoji="file"))
    builder.row(create_button("Получить TDATA", callback_data=f"get_tdata_{purchase_id}", style="default", emoji="download"))
    builder.row(create_button("К покупкам", callback_data="my_purchases", style="default", emoji="box"))
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", emoji="wallet"))
    builder.row(create_button("Мои покупки", callback_data="my_purchases", style="default", emoji="box"))
    builder.row(create_button("Промокод", callback_data="activate_promo", style="primary", emoji="promo"))
    builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
    return builder.as_markup()

def deposit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("СБП", callback_data="deposit_sbp", style="default", emoji="sbp"))
    builder.row(create_button("Crypto Bot", callback_data="deposit_crypto", style="success", emoji="crypto"))
    builder.row(create_button("Назад", callback_data="profile", style="default", emoji="back"))
    return builder.as_markup()

def deposit_crypto_check_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Проверить оплату", callback_data=f"check_deposit_crypto_{payment_id}", style="primary", emoji="loading"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", emoji="cross"))
    return builder.as_markup()

def sbp_payment_keyboard(payment_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Я оплатил", callback_data=f"sbp_paid_{payment_id}", style="success", emoji="check"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", emoji="cross"))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    buttons = [
        ("Статистика", "admin_stats", "primary", "stats"),
        ("Пользователи", "admin_users", "default", "users"),
        ("Аккаунты", "admin_accounts_list", "default", "box"),
        ("Рассылка", "admin_broadcast", "default", "broadcast"),
        ("Добавить аккаунты", "admin_add_accounts", "success", "add"),
        ("Управление балансом", "admin_balance", "default", "edit"),
        ("Цены на аккаунты", "admin_prices", "default", "money"),
        ("Промокоды", "admin_promo_menu", "default", "promo"),
        ("Управление медиа", "admin_media_menu", "default", "media"),
        ("Обязательные каналы", "admin_channels_menu", "default", "channel"),
        ("Проверка СБП", "admin_sbp_check", "success", "sbp"),
    ]
    for text, cb, style, emoji in buttons:
        builder.row(create_button(text, callback_data=cb, style=style, emoji=emoji))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", emoji="home"))
    return builder.as_markup()

def promo_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Создать промокод", callback_data="promo_create", style="success", emoji="add"))
    builder.row(create_button("Список промокодов", callback_data="promo_list", style="default", emoji="promo"))
    builder.row(create_button("Удалить промокод", callback_data="promo_delete_menu", style="danger", emoji="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
    return builder.as_markup()

async def price_settings_keyboard():
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()
    flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
    for country in ["США", "Россия", "Индия"]:
        price = prices.get(country, 20)
        builder.row(create_button(f"{flags.get(country, '')} {country}: {price:.0f}₽", callback_data=f"set_price_{country}", style="default", emoji="edit"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
    return builder.as_markup()

def media_menu_keyboard():
    builder = InlineKeyboardBuilder()
    sections = [("Главное меню", "main_menu"), ("Покупка", "buy_account"), ("Оплата", "payment_methods"), ("Профиль", "profile"), ("Покупки", "my_purchases"), ("Пополнение", "deposit")]
    for name, cb in sections:
        builder.row(create_button(name, callback_data=f"set_media_{cb}", style="default", emoji="media"))
    builder.row(create_button("Удалить все медиа", callback_data="admin_clear_media", style="danger", emoji="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
    return builder.as_markup()

def channels_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Добавить канал", callback_data="channel_add", style="success", emoji="add"))
    builder.row(create_button("Список каналов", callback_data="channel_list", style="default", emoji="channel"))
    builder.row(create_button("Удалить канал", callback_data="channel_delete", style="danger", emoji="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
    return builder.as_markup()

def sbp_approve_keyboard(payment_id: str, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        create_button("Одобрить", callback_data=f"sbp_approve_{payment_id}_{user_id}", style="success", emoji="accept"),
        create_button("Отклонить", callback_data=f"sbp_reject_{payment_id}_{user_id}", style="danger", emoji="reject")
    )
    return builder.as_markup()

# ===== ПОДПИСКА =====
async def check_subscription(user_id: int) -> tuple:
    async with async_session() as s:
        r = await s.execute(select(RequiredChannel)); channels = r.scalars().all()
    if not channels: return True, []
    ns = []
    for ch in channels:
        try:
            cid = ch.channel_id
            if not cid.startswith("-100") and cid.lstrip('-').isdigit(): cid = f"-100{cid}" if not cid.startswith('-') else cid
            m = await bot.get_chat_member(chat_id=cid, user_id=user_id)
            if m.status in ["left", "kicked"]: ns.append(ch)
        except: pass
    return len(ns) == 0, ns

async def get_subscribe_keyboard(ns: list):
    builder = InlineKeyboardBuilder()
    for ch in ns:
        builder.row(create_button(f"📢 {ch.channel_name or 'Канал'}", url=ch.channel_url, style="primary", emoji="subscribe"))
    builder.row(create_button("Проверить подписку", callback_data="check_subscription", style="success", emoji="loading"))
    return builder.as_markup()

# ===== СТРАНА =====
def detect_country(phone: str) -> str:
    phone = phone.strip().lstrip('+')
    for code in sorted(COUNTRY_CODES.keys(), key=len, reverse=True):
        if phone.startswith(code): return COUNTRY_CODES[code]
    return "США"

# ===== ЦЕНЫ =====
async def get_country_price(country: str) -> float:
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps: return ps.price
    return DEFAULT_PRICES.get(country, 20.0)

async def set_country_price(country: str, price: float):
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps: ps.price = price; ps.updated_at = datetime.utcnow()
        else: s.add(PriceSettings(country=country, price=price))
        await s.commit()

async def get_all_prices() -> dict:
    prices = dict(DEFAULT_PRICES)
    async with async_session() as s:
        r = await s.execute(select(PriceSettings))
        for ps in r.scalars().all(): prices[ps.country] = ps.price
    return prices

# ===== МЕДИА =====
async def get_media(section: str):
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section == section))
        return r.scalar_one_or_none()

async def set_media(section: str, file_id: str, file_type: str, caption: str = None):
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section == section))
        m = r.scalar_one_or_none()
        if m: m.file_id = file_id; m.file_type = file_type; m.caption = caption; m.updated_at = datetime.utcnow()
        else: s.add(MediaSettings(section=section, file_id=file_id, file_type=file_type, caption=caption))
        await s.commit()

async def send_media_message(target, section: str, text: str, reply_markup: InlineKeyboardMarkup):
    media = await get_media(section)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        try: await msg.delete()
        except: pass
    if media:
        caption = f"{text}\n\n{media.caption}" if media.caption else text
        if media.file_type == "photo": await msg.answer_photo(media.file_id, caption=caption, reply_markup=reply_markup)
        elif media.file_type == "video": await msg.answer_video(media.file_id, caption=caption, reply_markup=reply_markup)
        elif media.file_type == "animation": await msg.answer_animation(media.file_id, caption=caption, reply_markup=reply_markup)
        else: await msg.answer(text, reply_markup=reply_markup)
    else: await msg.answer(text, reply_markup=reply_markup)

# ===== TELETHON =====
async def create_telethon_client(session_string: str = None):
    return TelegramClient(StringSession(session_string) if session_string else StringSession(), API_ID, API_HASH)

async def send_code_to_phone(phone: str) -> dict:
    try:
        client = await create_telethon_client(); await client.connect()
        sent = await client.send_code_request(phone)
        pending_auth[phone] = {'client': client, 'phone_code_hash': sent.phone_code_hash, 'phone': phone}
        return {'success': True, 'phone_code_hash': sent.phone_code_hash}
    except Exception as e: return {'success': False, 'error': str(e)}

async def verify_code_and_create_all(phone: str, code: str, phone_code_hash: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data: return {'success': False, 'error': 'Сессия не найдена'}
        client = auth_data['client']
        try: await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError: return {'success': False, 'need_password': True, 'error': 'Требуется 2FA'}
        session_string = client.session.save()
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('session.session', session_string.encode())
            zf.writestr('key_data', os.urandom(256))
            zf.writestr('info.txt', f"Phone: {phone}\nAPI_ID: {API_ID}\nAPI_HASH: {API_HASH}".encode())
        tdata_zip_base64 = base64.b64encode(zip_buffer.getvalue()).decode()
        await client.disconnect(); pending_auth.pop(phone, None)
        return {'success': True, 'session_string': session_string, 'tdata_zip_base64': tdata_zip_base64}
    except PhoneCodeInvalidError: return {'success': False, 'error': 'Неверный код'}
    except PhoneCodeExpiredError: return {'success': False, 'error': 'Код истек'}
    except Exception as e: return {'success': False, 'error': str(e)}

async def verify_2fa_and_create_all(phone: str, password: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data: return {'success': False, 'error': 'Сессия не найдена'}
        client = auth_data['client']; await client.sign_in(password=password)
        session_string = client.session.save()
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('session.session', session_string.encode())
            zf.writestr('key_data', os.urandom(256))
            zf.writestr('info.txt', f"Phone: {phone}\nAPI_ID: {API_ID}\nAPI_HASH: {API_HASH}".encode())
        tdata_zip_base64 = base64.b64encode(zip_buffer.getvalue()).decode()
        await client.disconnect(); pending_auth.pop(phone, None)
        return {'success': True, 'session_string': session_string, 'tdata_zip_base64': tdata_zip_base64}
    except PasswordHashInvalidError: return {'success': False, 'error': 'Неверный пароль'}
    except Exception as e: return {'success': False, 'error': str(e)}

async def get_code_from_session(session_string: str) -> Optional[str]:
    client = None
    try:
        client = await create_telethon_client(session_string); await client.connect()
        if not await client.is_user_authorized(): return None
        async for dialog in client.iter_dialogs():
            if dialog.name and any(x in (dialog.name or "").lower() for x in ["42777", "telegram", "код", "code", "login", "verify"]):
                messages = await client.get_messages(dialog, limit=10)
                for msg in messages:
                    if msg.text:
                        codes = re.findall(r'\b\d{5}\b', msg.text)
                        if codes: return codes[0]
        async for dialog in client.iter_dialogs():
            messages = await client.get_messages(dialog, limit=3)
            for msg in messages:
                if msg.text:
                    codes = re.findall(r'\b\d{5}\b', msg.text)
                    if codes: return codes[0]
        return None
    except: return None
    finally:
        if client: await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
async def get_user(user_id: int):
    async with async_session() as s:
        r = await s.execute(select(User).where(User.telegram_id == user_id))
        return r.scalar_one_or_none()

async def get_or_create_user(user_id: int, username: str = None):
    user = await get_user(user_id)
    if not user:
        async with async_session() as s:
            user = User(telegram_id=user_id, username=username, is_admin=(user_id in ADMIN_IDS))
            s.add(user); await s.commit(); await s.refresh(user)
    return user

async def get_available_account(country: str = None):
    async with async_session() as s:
        q = select(Account).where(Account.is_sold == False, Account.is_verified == True, Account.session_string != None, Account.session_string != "")
        if country: q = q.where(Account.country == country)
        r = await s.execute(q.limit(1))
        return r.scalar_one_or_none()

async def get_available_countries() -> list:
    async with async_session() as s:
        r = await s.execute(select(Account.country, func.count(Account.id)).where(Account.is_sold == False, Account.is_verified == True).group_by(Account.country))
        return [(row[0], row[1]) for row in r.all()]

async def create_crypto_bot_invoice(amount: float, payment_id: str):
    try:
        url = "https://pay.crypt.bot/api/createInvoice"; headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        payload = {"asset": "USDT", "amount": str(round(amount / 90, 2)), "description": f"Vest #{payment_id}", "payload": payment_id, "allow_comments": False, "allow_anonymous": False, "expires_in": 3600}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers, timeout=30) as resp: return await resp.json()
    except: return None

async def check_crypto_bot_invoice(invoice_id: int):
    try:
        url = "https://pay.crypt.bot/api/getInvoices"; headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={"invoice_ids": str(invoice_id)}, headers=headers, timeout=30) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("result", {}).get("items"): return data["result"]["items"][0]
        return None
    except: return None

async def generate_payment_id(): return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

async def require_subscription(callback: CallbackQuery) -> bool:
    subbed, ns = await check_subscription(callback.from_user.id)
    if not subbed:
        await callback.message.answer(f'{tg_emoji("subscribe")} <b>Подпишитесь на каналы:</b>', reply_markup=await get_subscribe_keyboard(ns))
        return False
    return True

# ===== ОБРАБОТЧИКИ =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    subbed, ns = await check_subscription(message.from_user.id)
    if not subbed:
        await message.answer(f'{tg_emoji("subscribe")} <b>Подпишитесь на каналы</b>', reply_markup=await get_subscribe_keyboard(ns))
        return
    text = f'{tg_emoji("bot")} <b>Vest Account</b>\n\n{tg_emoji("lock")} Покупка аккаунтов\n{tg_emoji("loading")} Быстро и безопасно\n\n<i>Выберите действие:</i>'
    await send_media_message(message, "main_menu", text, main_menu_keyboard())

@router.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery):
    await callback.answer()
    subbed, ns = await check_subscription(callback.from_user.id)
    if subbed: await callback.message.answer(f'{tg_emoji("check")} <b>Подписка проверена!</b>', reply_markup=main_menu_keyboard())
    else: await callback.message.answer(f'{tg_emoji("cross")} <b>Вы не подписаны!</b>', reply_markup=await get_subscribe_keyboard(ns))

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: await message.answer(f'{tg_emoji("cross")} <b>Доступ запрещен</b>'); return
    await message.answer(f'{tg_emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    await send_media_message(callback, "main_menu", f'{tg_emoji("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>', main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def cb_buy_account(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    available = await get_available_countries()
    text = f'{tg_emoji("location")} <b>Выберите страну</b>\n\n' + (f'{tg_emoji("check")} Доступные страны:' if available else f'{tg_emoji("cross")} Нет аккаунтов')
    await send_media_message(callback, "buy_account", text, await countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def cb_country(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    country = callback.data.replace("country_", "")
    account = await get_available_account(country)
    if account:
        price = await get_country_price(country)
        if not hasattr(dp, 'pending_accounts'): dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {'account_id': account.id, 'price': price, 'country': country}
        flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
        await callback.message.answer(f'{tg_emoji("check")} <b>Аккаунт найден!</b>\n\n{tg_emoji("location")} Страна: {flags.get(country, "")} <b>{country}</b>\n{tg_emoji("money")} Цена: <b>{price:.0f}₽</b>\n\n<i>Нажмите КУПИТЬ</i>', reply_markup=account_found_keyboard())
    else: await callback.message.answer(f'{tg_emoji("cross")} <b>Нет аккаунтов для {country}</b>', reply_markup=await countries_keyboard())

@router.callback_query(F.data == "show_payment_methods")
async def cb_show_payment(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    await send_media_message(callback, "payment_methods", f'{tg_emoji("buy")} <b>Покупка</b>\n\n{tg_emoji("money")} Сумма: <b>{pending.get("price", 20):.0f}₽</b>\n\n<i>Выберите способ:</i>', payment_methods_keyboard())

@router.callback_query(F.data == "pay_balance")
async def cb_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price, account_id = pending.get('price', 20), pending.get('account_id')
    if not account_id: await callback.message.answer(f'{tg_emoji("cross")} <b>Ошибка</b>', reply_markup=main_menu_keyboard()); return
    if user.balance >= price:
        async with async_session() as session:
            user = await session.get(User, user.id); account = await session.get(Account, account_id)
            if account.is_sold: await callback.message.answer(f'{tg_emoji("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard()); return
            user.balance -= price; user.total_spent = (user.total_spent or 0) + price; account.is_sold = True
            purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="balance")
            session.add(purchase); await session.commit(); await session.refresh(purchase)
            await callback.message.answer(f'{tg_emoji("check")} <b>Оплата успешна!</b>\n\n{tg_emoji("tag")} Номер: <code>{account.phone}</code>\n{tg_emoji("money")} Сумма: <b>{price:.0f}₽</b>', reply_markup=get_code_keyboard(purchase.id))
    else: await callback.message.answer(f'{tg_emoji("cross")} <b>Недостаточно средств</b>\n\n{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n{tg_emoji("money")} Нужно: <b>{price:.0f}₽</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_sbp")
async def cb_pay_sbp(callback: CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", emoji="wallet"))
    builder.row(create_button("Назад", callback_data="show_payment_methods", style="default", emoji="back"))
    await callback.message.answer(f'{tg_emoji("sbp")} <b>Оплата через СБП</b>\n\n{tg_emoji("info")} Для оплаты товара через СБП пополните баланс.', reply_markup=builder.as_markup())

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20); payment_id = await generate_payment_id()
    async with async_session() as s:
        s.add(Payment(user_id=callback.from_user.id, amount=price, payment_id=payment_id, method="crypto", status="pending", type="purchase"))
        await s.commit()
    invoice = await create_crypto_bot_invoice(price, payment_id)
    if invoice and invoice.get("ok"):
        r = invoice.get("result", {})
        async with async_session() as s:
            p = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); p = p.scalar_one_or_none()
            if p: p.payment_id = str(r.get("invoice_id")); await s.commit()
        await callback.message.answer(f'{tg_emoji("crypto")} <b>Оплата Crypto Bot</b>\n\nСумма: <b>{price:.0f}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ После оплаты нажмите проверку', reply_markup=check_crypto_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
    else: await callback.message.answer(f'{tg_emoji("cross")} <b>Ошибка</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{tg_emoji("star")} <b>Оплата Telegram Stars</b>\n\nНапишите: <b>@v3estnikov</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def cb_check_purchase(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_purchase_crypto_", "")
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
        pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
        account_id, price = pending.get('account_id'), pending.get('price', 20)
        if account_id:
            async with async_session() as s:
                account = await s.get(Account, account_id)
                if account.is_sold: await callback.message.answer(f'{tg_emoji("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard()); return
                pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); pr = pr.scalar_one_or_none()
                if pr: pr.status = "completed"
                user = await s.get(User, callback.from_user.id)
                if user: user.total_spent = (user.total_spent or 0) + price
                account.is_sold = True
                purchase = Purchase(user_id=callback.from_user.id, account_id=account_id, amount=price, payment_method="crypto")
                s.add(purchase); await s.commit(); await s.refresh(purchase)
                await callback.message.answer(f'{tg_emoji("check")} <b>Оплата подтверждена!</b>\n\n{tg_emoji("tag")} Номер: <code>{account.phone}</code>\n{tg_emoji("money")} Сумма: <b>{price:.0f}₽</b>', reply_markup=get_code_keyboard(purchase.id))
    else: await callback.answer("⏳ Не найдено", show_alert=True)

@router.callback_query(F.data.startswith("check_deposit_crypto_"))
async def cb_check_deposit(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_deposit_crypto_", "")
    inv = await check_crypto_bot_invoice(int(payment_id))
    if inv and inv.get("status") == "paid":
        async with async_session() as s:
            pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); payment = pr.scalar_one_or_none()
            if payment and payment.status != "completed":
                payment.status = "completed"; user = await s.get(User, callback.from_user.id); user.balance += payment.amount; await s.commit()
                builder = InlineKeyboardBuilder(); builder.row(create_button("В меню", callback_data="main_menu", style="success", emoji="home"))
                await callback.message.answer(f'{tg_emoji("check")} <b>Баланс пополнен!</b>\n\n{tg_emoji("money")} +{payment.amount:.2f}₽\n{tg_emoji("wallet")} Баланс: <b>{user.balance:.2f}₽</b>', reply_markup=builder.as_markup())
    else: await callback.answer("⏳ Не найдено", show_alert=True)

@router.callback_query(F.data.startswith("get_code_"))
async def cb_get_code(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_code_", ""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id != callback.from_user.id: await callback.answer("Не найдена", show_alert=True); return
        account = await s.get(Account, purchase.account_id)
        if not account or not account.session_string: await callback.answer("Нет данных", show_alert=True); return
        status_msg = await callback.message.answer(f'{tg_emoji("loading")} <b>Получаю код...</b>')
        code = await get_code_from_session(account.session_string); await status_msg.delete()
        if code:
            builder = InlineKeyboardBuilder()
            builder.row(create_button("Получить еще раз", callback_data=f"get_code_{purchase_id}", style="primary", emoji="code"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
            await callback.message.answer(f'{tg_emoji("check")} <b>Код: <code>{code}</code></b>\n\n{tg_emoji("tag")} Номер: <code>{account.phone}</code>', reply_markup=builder.as_markup())
        else: await callback.message.answer(f'{tg_emoji("cross")} <b>Не удалось</b>', reply_markup=get_code_keyboard(purchase_id))

@router.callback_query(F.data.startswith("get_session_"))
async def cb_get_session(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_session_", ""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id != callback.from_user.id: await callback.answer("Не найдена", show_alert=True); return
        account = await s.get(Account, purchase.account_id)
        if not account or not account.session_string: await callback.answer("Нет .session", show_alert=True); return
        session_bytes = account.session_string.encode()
        await callback.message.answer_document(types.BufferedInputFile(session_bytes, filename=f"{account.phone}.session"), caption=f'{tg_emoji("file")} .session для {account.phone}')
        builder = InlineKeyboardBuilder(); builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
        await callback.message.answer(f'{tg_emoji("check")} <b>Файл отправлен!</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("get_tdata_"))
async def cb_get_tdata(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_tdata_", ""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id != callback.from_user.id: await callback.answer("Не найдена", show_alert=True); return
        account = await s.get(Account, purchase.account_id)
        if not account or not account.tdata_zip_base64: await callback.answer("Нет TDATA", show_alert=True); return
        zip_data = base64.b64decode(account.tdata_zip_base64)
        await callback.message.answer_document(types.BufferedInputFile(zip_data, filename=f"{account.phone}_tdata.zip"), caption=f'{tg_emoji("download")} TDATA для {account.phone}')
        builder = InlineKeyboardBuilder(); builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
        await callback.message.answer(f'{tg_emoji("check")} <b>TDATA отправлен!</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data == "my_purchases")
async def cb_my_purchases(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc()))
        purchases = r.scalars().all()
        if purchases:
            text = f'{tg_emoji("box")} <b>Ваши покупки</b>\n\n'; builder = InlineKeyboardBuilder()
            for p in purchases:
                account = await s.get(Account, p.account_id)
                phone = account.phone if account else "Н/Д"
                text += f'📱 <code>{phone}</code> • {p.amount:.0f}₽ • {p.created_at.strftime("%d.%m.%y")}\n'
                builder.row(create_button("Код", callback_data=f"get_code_{p.id}", style="primary", emoji="code"), create_button(".session", callback_data=f"get_session_{p.id}", style="default", emoji="file"), create_button("TDATA", callback_data=f"get_tdata_{p.id}", style="default", emoji="download"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())
        else: await send_media_message(callback, "my_purchases", f'{tg_emoji("box")} <b>Мои покупки</b>\n\nПока нет покупок.', main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    user = await get_user(callback.from_user.id)
    async with async_session() as s:
        cnt = (await s.execute(select(func.count(Purchase.id)).where(Purchase.user_id == callback.from_user.id))).scalar() or 0
    text = f'{tg_emoji("profile")} <b>Профиль</b>\n\n{tg_emoji("tag")} ID: <code>{user.telegram_id}</code>\n{tg_emoji("profile")} @{user.username or "нет"}\n\n━━━ 💰 БАЛАНС ━━━\n{tg_emoji("wallet")} <b>{user.balance:.0f}₽</b>\n━━━━━━━━━━━━━━\n\n━━ 📊 СТАТИСТИКА ━━\n{tg_emoji("box")} Покупок: <b>{cnt}</b>\n{tg_emoji("money")} Потрачено: <b>{(user.total_spent or 0):.0f}₽</b>\n{tg_emoji("clock")} С нами: {user.created_at.strftime("%d.%m.%Y")}\n━━━━━━━━━━━━━━'
    await send_media_message(callback, "profile", text, profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def cb_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    await send_media_message(callback, "deposit", f'{tg_emoji("wallet")} <b>Пополнение баланса</b>\n\n{tg_emoji("sbp")} <b>СБП</b>\n{tg_emoji("crypto")} <b>Crypto Bot</b>\n\n<i>Минимум: 10₽</i>', deposit_keyboard())

@router.callback_query(F.data == "deposit_sbp")
async def cb_deposit_sbp(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{tg_emoji("sbp")} <b>Введите сумму (от 10₽)</b>')
    if not hasattr(dp, 'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'

@router.callback_query(F.data == "deposit_crypto")
async def cb_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{tg_emoji("crypto")} <b>Введите сумму (от 10₽)</b>')
    if not hasattr(dp, 'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

@router.callback_query(F.data == "activate_promo")
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_promo_code)
    builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="profile", style="danger", emoji="cross"))
    await callback.message.answer(f'{tg_emoji("promo")} <b>Введите промокод:</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("sbp_paid_"))
async def cb_sbp_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    payment_id = callback.data.replace("sbp_paid_", "")
    await state.set_state(SBPStates.waiting_for_screenshot); await state.update_data(payment_id=payment_id)
    builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="main_menu", style="danger", emoji="cross"))
    await callback.message.answer(f'{tg_emoji("photo")} <b>Отправьте скриншот оплаты</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("sbp_approve_"))
async def cb_sbp_approve(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_approve_", "").rsplit("_", 1); payment_id, user_id = parts[0], int(parts[1])
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); payment = pr.scalar_one_or_none()
        if payment and payment.status != "completed":
            payment.status = "completed"
            user = await s.execute(select(User).where(User.telegram_id == user_id)); user = user.scalar_one_or_none()
            if user:
                old, new = user.balance, user.balance + payment.amount; user.balance = new; await s.commit()
                await callback.message.edit_caption(f'{callback.message.caption}\n\n{tg_emoji("check")} <b>ОДОБРЕНО</b>\n💰 {old:.0f}₽ → {new:.0f}₽', reply_markup=None)
                try: await bot.send_message(user_id, f'{tg_emoji("check")} <b>Платеж одобрен!</b>\n\n{tg_emoji("money")} +{payment.amount}₽\n{tg_emoji("wallet")} Баланс: <b>{new:.0f}₽</b>')
                except: pass

@router.callback_query(F.data.startswith("sbp_reject_"))
async def cb_sbp_reject(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_reject_", "").rsplit("_", 1); payment_id, user_id = parts[0], int(parts[1])
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); payment = pr.scalar_one_or_none()
        if payment: payment.status = "rejected"; await s.commit()
        await callback.message.edit_caption(f'{callback.message.caption}\n\n{tg_emoji("cross")} <b>ОТКЛОНЕНО</b>', reply_markup=None)
        try: await bot.send_message(user_id, f'{tg_emoji("cross")} <b>Платеж отклонен</b>\n\n@v3estnikov')
        except: pass

@router.callback_query(F.data == "admin_sbp_check")
async def cb_admin_sbp(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(Payment).where(Payment.method == "sbp", Payment.status == "pending", Payment.screenshot_file_id != None).order_by(Payment.created_at.desc()).limit(10))
        payments = r.scalars().all()
        if payments:
            await callback.message.answer(f'{tg_emoji("sbp")} <b>Загружаю...</b>')
            for payment in payments:
                user = await get_user(payment.user_id)
                try: await bot.send_photo(callback.from_user.id, payment.screenshot_file_id, caption=f'{tg_emoji("sbp")} <b>СБП</b>\n{tg_emoji("profile")} ID: <code>{payment.user_id}</code>\n{tg_emoji("money")} Сумма: <b>{payment.amount}₽</b>\n{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=sbp_approve_keyboard(payment.payment_id, payment.user_id))
                except: pass
            await callback.message.answer(f'{tg_emoji("info")} Проверьте платежи', reply_markup=admin_keyboard())
        else: await callback.message.answer(f'{tg_emoji("info")} <b>Нет платежей</b>', reply_markup=admin_keyboard())

# ===== АДМИН =====
@router.callback_query(F.data == "admin")
async def cb_admin_return(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.message.answer(f'{tg_emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def cb_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: await callback.answer("❌", show_alert=True); return
    data = callback.data
    if data == "admin_stats":
        async with async_session() as s:
            users_cnt = (await s.execute(select(func.count(User.id)))).scalar() or 0
            accs_cnt = (await s.execute(select(func.count(Account.id)))).scalar() or 0
            sold_cnt = (await s.execute(select(func.count(Account.id)).where(Account.is_sold == True))).scalar() or 0
            verif_cnt = (await s.execute(select(func.count(Account.id)).where(Account.is_verified == True))).scalar() or 0
            purch_cnt = (await s.execute(select(func.count(Purchase.id)))).scalar() or 0
            revenue = (await s.execute(select(func.sum(Purchase.amount)))).scalar() or 0
            await callback.message.answer(f'{tg_emoji("stats")} <b>Статистика</b>\n\n{tg_emoji("profile")} Пользователей: <b>{users_cnt}</b>\n{tg_emoji("box")} Аккаунтов: <b>{accs_cnt}</b>\n{tg_emoji("check")} Вериф: <b>{verif_cnt}</b>\n{tg_emoji("buy")} Продано: <b>{sold_cnt}</b>\n{tg_emoji("box")} Покупок: <b>{purch_cnt}</b>\n{tg_emoji("money")} Выручка: <b>{revenue:.0f}₽</b>', reply_markup=admin_keyboard())
    elif data == "admin_users":
        async with async_session() as s:
            r = await s.execute(select(User).order_by(User.created_at.desc()).limit(20)); users = r.scalars().all()
            text = f'{tg_emoji("users")} <b>Пользователи</b>\n\n'
            for u in users: text += f'<code>{u.telegram_id}</code> @{u.username or "нет"} | {u.balance:.0f}₽ | {u.created_at.strftime("%d.%m")}\n'
        builder = InlineKeyboardBuilder(); builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    elif data == "admin_accounts_list":
        async with async_session() as s:
            r = await s.execute(select(Account).order_by(Account.created_at.desc()).limit(20)); accounts = r.scalars().all()
            text = f'{tg_emoji("box")} <b>Аккаунты</b>\n\n'
            for a in accounts: text += f'{"✅" if a.is_verified else "⏳"} <code>{a.phone}</code> | {a.country} | {a.price:.0f}₽ | {"ПРОДАН" if a.is_sold else "в наличии"}\n'
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Удалить аккаунт", callback_data="admin_delete_account", style="danger", emoji="delete"))
        builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    elif data == "admin_delete_account":
        await callback.message.answer(f'{tg_emoji("delete")} <b>Отправьте номер для удаления:</b>')
        if not hasattr(dp, 'awaiting_delete_account'): dp.awaiting_delete_account = set()
        dp.awaiting_delete_account.add(callback.from_user.id)
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'): dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        await callback.message.answer(f'{tg_emoji("broadcast")} <b>Рассылка</b>\n\nОтправьте сообщение.', reply_markup=builder.as_markup())
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'): dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        await callback.message.answer(f'{tg_emoji("add")} <b>Добавление аккаунта</b>\n\nОтправьте номер: <code>+79001234567</code>', reply_markup=builder.as_markup())
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'): dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}
        builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        await callback.message.answer(f'{tg_emoji("edit")} <b>Изменение баланса</b>\n\nОтправьте ID.', reply_markup=builder.as_markup())
    elif data == "admin_prices":
        await callback.message.answer(f'{tg_emoji("settings")} <b>Цены</b>', reply_markup=await price_settings_keyboard())
    elif data == "admin_promo_menu":
        await callback.message.answer(f'{tg_emoji("promo")} <b>Промокоды</b>', reply_markup=promo_admin_keyboard())
    elif data == "promo_create":
        await state.set_state(PromoStates.waiting_for_promo_data)
        builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin_promo_menu", style="danger", emoji="cross"))
        await callback.message.answer(f'{tg_emoji("promo")} <b>Создание промокода</b>\n\nФормат: <code>КОД СУММА КОЛВО</code>\nПример: <code>HELLO 50 10</code>', reply_markup=builder.as_markup())
    elif data == "promo_list":
        async with async_session() as s:
            r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)); promos = r.scalars().all()
            text = f'{tg_emoji("promo")} <b>Промокоды</b>\n\n'
            for p in promos: text += f'<code>{p.code}</code> | {p.amount}₽ | {p.used_count}/{p.max_uses} | {"✅" if p.is_active else "❌"}\n'
            if not promos: text += 'Нет промокодов'
        builder = InlineKeyboardBuilder(); builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    elif data == "promo_delete_menu":
        async with async_session() as s:
            r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)); promos = r.scalars().all()
            if not promos: await callback.message.answer(f'{tg_emoji("info")} Нет промокодов', reply_markup=promo_admin_keyboard()); return
            builder = InlineKeyboardBuilder()
            for p in promos: builder.row(create_button(f"❌ {p.code}", callback_data=f"promo_delete_{p.id}", style="danger", emoji="delete"))
            builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", emoji="back"))
            await callback.message.answer(f'{tg_emoji("delete")} <b>Выберите для удаления:</b>', reply_markup=builder.as_markup())
    elif data.startswith("promo_delete_"):
        promo_id = int(data.replace("promo_delete_", ""))
        async with async_session() as s:
            promo = await s.get(PromoCode, promo_id)
            if promo: await s.delete(promo); await s.commit(); await callback.message.answer(f'{tg_emoji("check")} <b>Удален!</b>', reply_markup=promo_admin_keyboard())
    elif data == "admin_media_menu":
        await callback.message.answer(f'{tg_emoji("media")} <b>Управление медиа</b>', reply_markup=media_menu_keyboard())
    elif data == "admin_clear_media":
        async with async_session() as s: await s.execute(sa_text("DELETE FROM media_settings")); await s.commit()
        await callback.message.answer(f'{tg_emoji("check")} <b>Медиа удалены!</b>', reply_markup=admin_keyboard())
    elif data == "admin_channels_menu":
        await callback.message.answer(f'{tg_emoji("channel")} <b>Обязательные каналы</b>', reply_markup=channels_admin_keyboard())
    elif data == "channel_add":
        await state.set_state(ChannelStates.waiting_for_channel)
        builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin_channels_menu", style="danger", emoji="cross"))
        await callback.message.answer(f'{tg_emoji("channel")} <b>Добавление канала</b>\n\nФормат:\n<code>@username https://t.me/username</code>', reply_markup=builder.as_markup())
    elif data == "channel_list":
        async with async_session() as s:
            r = await s.execute(select(RequiredChannel)); channels = r.scalars().all()
            text = f'{tg_emoji("channel")} <b>Каналы</b>\n\n'
            for ch in channels: text += f'📢 {ch.channel_name or ch.channel_id}\n{ch.channel_url}\n\n'
            if not channels: text += 'Нет каналов'
        builder = InlineKeyboardBuilder(); builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    elif data == "channel_delete":
        async with async_session() as s:
            r = await s.execute(select(RequiredChannel)); channels = r.scalars().all()
            if not channels: await callback.message.answer(f'{tg_emoji("info")} Нет каналов', reply_markup=channels_admin_keyboard()); return
            builder = InlineKeyboardBuilder()
            for ch in channels: builder.row(create_button(f"❌ {ch.channel_name or ch.channel_id}", callback_data=f"channel_del_{ch.id}", style="danger", emoji="delete"))
            builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", emoji="back"))
            await callback.message.answer(f'{tg_emoji("delete")} <b>Выберите для удаления:</b>', reply_markup=builder.as_markup())
    elif data.startswith("channel_del_"):
        channel_id = int(data.replace("channel_del_", ""))
        async with async_session() as s:
            ch = await s.get(RequiredChannel, channel_id)
            if ch: await s.delete(ch); await s.commit(); await callback.message.answer(f'{tg_emoji("check")} <b>Удален!</b>', reply_markup=channels_admin_keyboard())

@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    country = callback.data.replace("set_price_", "")
    await state.set_state(PriceStates.waiting_for_price); await state.update_data(country=country)
    current = await get_country_price(country)
    builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin_prices", style="danger", emoji="cross"))
    await callback.message.answer(f'{tg_emoji("edit")} <b>Цена: {country}</b>\n\nТекущая: <b>{current:.0f}₽</b>\n\nОтправьте новую цену:', reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("set_media_"))
async def cb_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    section = callback.data.replace("set_media_", "")
    await state.set_state(MediaStates.waiting_for_media); await state.update_data(section=section)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    builder = InlineKeyboardBuilder(); builder.row(create_button("Отмена", callback_data="admin_media_menu", style="danger", emoji="cross"))
    await callback.message.answer(f'{tg_emoji("media")} <b>Установка медиа</b>\n\nРаздел: <b>{names.get(section, section)}</b>\n\nОтправьте фото/видео/GIF.', reply_markup=builder.as_markup())

# ===== ОБРАБОТЧИКИ СООБЩЕНИЙ =====
@router.message(StateFilter(PromoStates.waiting_for_promo_code), F.text)
async def h_activate_promo(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    async with async_session() as s:
        r = await s.execute(select(PromoCode).where(PromoCode.code == code, PromoCode.is_active == True)); promo = r.scalar_one_or_none()
        if not promo: await message.answer(f'{tg_emoji("cross")} <b>Не найден</b>', reply_markup=profile_keyboard()); return
        if promo.used_count >= promo.max_uses: await message.answer(f'{tg_emoji("cross")} <b>Исчерпан</b>', reply_markup=profile_keyboard()); return
        r = await s.execute(select(PromoUsage).where(PromoUsage.user_id == message.from_user.id, PromoUsage.promo_id == promo.id))
        if r.scalar_one_or_none(): await message.answer(f'{tg_emoji("cross")} <b>Уже использован</b>', reply_markup=profile_keyboard()); return
        promo.used_count += 1; s.add(PromoUsage(user_id=message.from_user.id, promo_id=promo.id))
        user = await get_user(message.from_user.id)
        if user: user.balance += promo.amount; await s.commit()
        await message.answer(f'{tg_emoji("check")} <b>Промокод активирован!</b>\n\n{tg_emoji("money")} +{promo.amount}₽\n{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=profile_keyboard())

@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def h_sbp_screenshot(message: Message, state: FSMContext):
    data = await state.get_data(); payment_id = data.get('payment_id'); await state.clear()
    file_id = message.photo[-1].file_id
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); payment = pr.scalar_one_or_none()
        if payment: payment.screenshot_file_id = file_id; await s.commit()
    await message.answer(f'{tg_emoji("check")} <b>Скриншот отправлен!</b>', reply_markup=main_menu_keyboard())
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); payment = pr.scalar_one_or_none()
        if payment:
            user = await get_user(payment.user_id)
            for admin_id in ADMIN_IDS:
                try: await bot.send_photo(admin_id, file_id, caption=f'{tg_emoji("sbp")} <b>СБП платеж</b>\n\n{tg_emoji("profile")} ID: <code>{payment.user_id}</code>\n@{user.username or "нет"}\n{tg_emoji("money")} Сумма: <b>{payment.amount}₽</b>\n{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=sbp_approve_keyboard(payment_id, payment.user_id))
                except: pass

@router.message(StateFilter(PriceStates.waiting_for_price), F.text)
async def h_set_price(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); country = data.get('country'); await state.clear()
    try:
        price = float(message.text.strip().replace(',', '.'))
        if price <= 0: await message.answer(f'{tg_emoji("cross")} <b>Цена > 0</b>', reply_markup=admin_keyboard()); return
        await set_country_price(country, price)
        await message.answer(f'{tg_emoji("check")} <b>Цена обновлена!</b>\n\n{country}: <b>{price:.0f}₽</b>', reply_markup=admin_keyboard())
    except: await message.answer(f'{tg_emoji("cross")} <b>Введите число</b>', reply_markup=admin_keyboard())

@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def h_media(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); section = data.get('section'); await state.clear()
    if message.photo: fid, ftype = message.photo[-1].file_id, "photo"
    elif message.video: fid, ftype = message.video.file_id, "video"
    else: fid, ftype = message.animation.file_id, "animation"
    await set_media(section, fid, ftype, message.caption)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    await message.answer(f'{tg_emoji("check")} <b>Медиа установлено!</b>\n\nРаздел: <b>{names.get(section, section)}</b>', reply_markup=admin_keyboard())

@router.message(StateFilter(ChannelStates.waiting_for_channel), F.text)
async def h_add_channel(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) >= 1:
        username = parts[0].replace('@', ''); channel_url = parts[1] if len(parts) >= 2 else f"https://t.me/{username}"
        try:
            chat = await bot.get_chat(f"@{username}")
            async with async_session() as s:
                s.add(RequiredChannel(channel_id=str(chat.id), channel_url=channel_url, channel_name=chat.title or username)); await s.commit()
            await message.answer(f'{tg_emoji("check")} <b>Канал добавлен!</b>\n\n{chat.title}', reply_markup=admin_keyboard())
        except Exception as e: await message.answer(f'{tg_emoji("cross")} <b>Ошибка:</b> {e}', reply_markup=admin_keyboard())

@router.message(StateFilter(PromoStates.waiting_for_promo_data), F.text)
async def h_create_promo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    parts = message.text.strip().split()
    if len(parts) >= 3:
        code = parts[0].upper()
        try:
            amount, max_uses = float(parts[1]), int(parts[2])
            async with async_session() as s:
                if (await s.execute(select(PromoCode).where(PromoCode.code == code))).scalar_one_or_none(): await message.answer(f'{tg_emoji("cross")} <b>Существует</b>', reply_markup=admin_keyboard()); return
                s.add(PromoCode(code=code, amount=amount, max_uses=max_uses)); await s.commit()
            await message.answer(f'{tg_emoji("check")} <b>Промокод создан!</b>\n\n<code>{code}</code> | {amount}₽ | {max_uses} исп.', reply_markup=admin_keyboard())
        except: await message.answer(f'{tg_emoji("cross")} <b>Неверный формат чисел</b>', reply_markup=admin_keyboard())

@router.message(F.text)
async def h_text(message: Message, state: FSMContext):
    user_id = message.from_user.id; text = message.text.strip()
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10: await message.answer(f'{tg_emoji("cross")} <b>Минимум: 10₽</b>', reply_markup=deposit_keyboard()); return
            payment_id = await generate_payment_id()
            if method == "sbp":
                async with async_session() as s:
                    s.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="sbp", status="pending", type="deposit")); await s.commit()
                await message.answer(f'{tg_emoji("sbp")} <b>Пополнение СБП</b>\n\n{tg_emoji("money")} Сумма: <b>{amount}₽</b>\n\n{tg_emoji("bank")} <b>Реквизиты:</b>\n📱 <code>{SBP_PHONE}</code>\n🏦 Банк: <b>{SBP_BANK}</b>\n👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n⚠️ Нажмите "Я оплатил"', reply_markup=sbp_payment_keyboard(payment_id))
            elif method == "crypto":
                async with async_session() as s:
                    s.add(Payment(user_id=user_id, amount=amount, payment_id=payment_id, method="crypto", status="pending", type="deposit")); await s.commit()
                invoice = await create_crypto_bot_invoice(amount, payment_id)
                if invoice and invoice.get("ok"):
                    r = invoice.get("result", {})
                    async with async_session() as s:
                        p = await s.execute(select(Payment).where(Payment.payment_id == payment_id)); p = p.scalar_one_or_none()
                        if p: p.payment_id = str(r.get("invoice_id")); await s.commit()
                    await message.answer(f'{tg_emoji("crypto")} <b>Пополнение Crypto Bot</b>\n\nСумма: <b>{amount}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ Нажмите проверку', reply_markup=deposit_crypto_check_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
        except: await message.answer(f'{tg_emoji("cross")} <b>Введите число</b>')
        return
    if hasattr(dp, 'awaiting_delete_account') and user_id in dp.awaiting_delete_account:
        dp.awaiting_delete_account.remove(user_id)
        async with async_session() as s:
            r = await s.execute(select(Account).where(Account.phone == text)); account = r.scalar_one_or_none()
            if account: await s.delete(account); await s.commit(); await message.answer(f'{tg_emoji("check")} <b>Удален!</b>', reply_markup=admin_keyboard())
            else: await message.answer(f'{tg_emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard())
        return
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]; step = acc_data.get('step')
        if step == 'phone':
            phone = text; country = detect_country(phone); price = await get_country_price(country)
            acc_data['phone'] = phone; acc_data['country'] = country; acc_data['price'] = price
            result = await send_code_to_phone(phone)
            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']; acc_data['step'] = 'code'
                await message.answer(f'{tg_emoji("check")} <b>Код отправлен на {phone}</b>\n\nСтрана: <b>{country}</b> | Цена: <b>{price:.0f}₽</b>\n\nВведите код:')
            else: del dp.awaiting_accounts[user_id]; await message.answer(f'{tg_emoji("cross")} <b>Ошибка</b>', reply_markup=admin_keyboard())
        elif step == 'code':
            result = await verify_code_and_create_all(acc_data['phone'], text, acc_data['phone_code_hash'])
            if result['success']:
                async with async_session() as s:
                    ex = await s.execute(select(Account).where(Account.phone == acc_data['phone'])); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string = result['session_string']; ex.tdata_zip_base64 = result['tdata_zip_base64']; ex.is_verified = True; ex.is_sold = False; ex.country = acc_data['country']; ex.price = acc_data['price']
                    else: s.add(Account(phone=acc_data['phone'], country=acc_data['country'], price=acc_data['price'], session_string=result['session_string'], tdata_zip_base64=result['tdata_zip_base64'], is_verified=True, is_sold=False))
                    await s.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(f'{tg_emoji("check")} <b>Аккаунт добавлен!</b>\n\n{tg_emoji("tag")} Номер: <code>{acc_data["phone"]}</code>\n{tg_emoji("location")} Страна: <b>{acc_data["country"]}</b>\n{tg_emoji("money")} Цена: <b>{acc_data["price"]:.0f}₽</b>\n<i>Доступен для покупки</i>', reply_markup=admin_keyboard())
            elif result.get('need_password'): acc_data['step'] = 'password'; await message.answer(f'{tg_emoji("lock")} <b>Введите 2FA пароль:</b>')
            else: del dp.awaiting_accounts[user_id]; await message.answer(f'{tg_emoji("cross")} <b>{result.get("error")}</b>', reply_markup=admin_keyboard())
        elif step == 'password':
            result = await verify_2fa_and_create_all(acc_data['phone'], text)
            if result['success']:
                async with async_session() as s:
                    ex = await s.execute(select(Account).where(Account.phone == acc_data['phone'])); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string = result['session_string']; ex.tdata_zip_base64 = result['tdata_zip_base64']; ex.is_verified = True; ex.is_sold = False; ex.country = acc_data['country']; ex.price = acc_data['price']
                    else: s.add(Account(phone=acc_data['phone'], country=acc_data['country'], price=acc_data['price'], session_string=result['session_string'], tdata_zip_base64=result['tdata_zip_base64'], is_verified=True, is_sold=False))
                    await s.commit()
                del dp.awaiting_accounts[user_id]
                await message.answer(f'{tg_emoji("check")} <b>Аккаунт добавлен!</b>\n\n{tg_emoji("tag")} Номер: <code>{acc_data["phone"]}</code>\n<i>Доступен</i>', reply_markup=admin_keyboard())
            else: await message.answer(f'{tg_emoji("cross")} <b>{result.get("error")}</b>\nПопробуйте еще раз:')
        return
    if hasattr(dp, 'awaiting_balance') and user_id in dp.awaiting_balance:
        bal_data = dp.awaiting_balance[user_id]; step = bal_data.get('step')
        if step == 'user_id':
            try:
                target = await get_user(int(text))
                if not target: await message.answer(f'{tg_emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); del dp.awaiting_balance[user_id]; return
                bal_data['target_id'] = int(text); bal_data['step'] = 'amount'
                await message.answer(f'{tg_emoji("edit")} <b>Изменение баланса</b>\n\nID: <code>{text}</code>\nБаланс: <b>{target.balance:.0f}₽</b>\n\n<code>+100</code> / <code>-50</code> / <code>500</code>')
            except: await message.answer(f'{tg_emoji("cross")} <b>Введите ID</b>')
        elif step == 'amount':
            try:
                target_id = bal_data['target_id']
                async with async_session() as s:
                    target = await s.execute(select(User).where(User.telegram_id == target_id)); target = target.scalar_one_or_none()
                    if not target: del dp.awaiting_balance[user_id]; await message.answer(f'{tg_emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); return
                    old = target.balance
                    if text.startswith('+'): target.balance += float(text[1:])
                    elif text.startswith('-'): target.balance = max(0, target.balance - float(text[1:]))
                    else: target.balance = float(text)
                    await s.commit()
                    del dp.awaiting_balance[user_id]
                    await message.answer(f'{tg_emoji("check")} <b>Баланс изменен!</b>\n\nID: <code>{target_id}</code>\nБыло: <b>{old:.0f}₽</b>\nСтало: <b>{target.balance:.0f}₽</b>', reply_markup=admin_keyboard())
            except: await message.answer(f'{tg_emoji("cross")} <b>Введите сумму</b>')
        return
    if hasattr(dp, 'awaiting_broadcast') and user_id in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(user_id)
        async with async_session() as s:
            users = (await s.execute(select(User))).scalars().all(); sent = 0
            for u in users:
                try: await message.copy_to(chat_id=u.telegram_id); sent += 1; await asyncio.sleep(0.05)
                except: pass
        await message.answer(f'{tg_emoji("check")} <b>Рассылка завершена</b>\n\n{sent}/{len(users)}', reply_markup=admin_keyboard())
        return
    await message.answer(f'{tg_emoji("info")} <b>Используйте кнопки меню</b>', reply_markup=main_menu_keyboard())

# ===== ЗАПУСК =====
async def setup_db():
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)

async def main():
    await setup_db(); await run_migrations()
    for attr in ['pending_accounts', 'awaiting_deposit', 'awaiting_accounts', 'awaiting_balance']:
        if not hasattr(dp, attr): setattr(dp, attr, {})
    if not hasattr(dp, 'awaiting_broadcast'): dp.awaiting_broadcast = set()
    if not hasattr(dp, 'awaiting_delete_account'): dp.awaiting_delete_account = set()
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
