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
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=storage)
router = Router()
pending_auth = {}

# ===== ПРЕМИУМ ЭМОДЗИ ID =====
EMOJI_ID = {
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
    'settings': '5870982283724328568',
    'gift': '6032644646587338669',
    'users': '5870772616305839506',
    'delete': '5870875489362513438',
    'subscribe': '6039486778597970865',
    'promo': '6032644646587338669',
    'file': '5870528606328852614',
    'download': '6039802767931871481',
    'key': '6037249452824072506',
    'channel': '6039422865189638057',
    'accept': '5774022692642492953',
    'reject': '5774077015388852135',
    'default_icon': '5771449289972650710',
    'primary_icon': '6028435952299413210',
    'success_icon': '5870633910337015697',
    'danger_icon': '5870657884844462243',
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
    'channel': '📢', 'accept': '✅', 'reject': '❌', 'default_icon': '📌',
    'primary_icon': 'ℹ️', 'success_icon': '✅', 'danger_icon': '❌',
}

def tg_emoji(name: str) -> str:
    """Возвращает HTML-тег премиум эмодзи"""
    emoji_id = EMOJI_ID.get(name, EMOJI_ID['info'])
    char = EMOJI_CHAR.get(name, '📌')
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'

def icon_custom_emoji_id(name: str) -> str:
    """Возвращает ID премиум эмодзи для кнопок"""
    return EMOJI_ID.get(name, EMOJI_ID['info'])

# ===== ФУНКЦИЯ СОЗДАНИЯ КНОПОК С STYLE =====
def create_button(
    text: str,
    callback_data: str = None,
    url: str = None,
    style: str = None,
    emoji: str = None
) -> InlineKeyboardButton:
    """
    Создает кнопку с премиум эмодзи и стилем.
    style: 'primary', 'success', 'danger', 'default' (или None для обычной)
    emoji: ключ из EMOJI_ID для иконки
    """
    kwargs = {'text': text}
    
    if callback_data:
        kwargs['callback_data'] = callback_data
    if url:
        kwargs['url'] = url
    if style and style in ['primary', 'success', 'danger', 'default']:
        kwargs['style'] = style
    if emoji and emoji in EMOJI_ID:
        kwargs['icon_custom_emoji_id'] = EMOJI_ID[emoji]
    
    return InlineKeyboardButton(**kwargs)

# ===== КЛАВИАТУРЫ С STYLE И ПРЕМИУМ ЭМОДЗИ =====
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
        builder.row(create_button(
            f"{flags.get(country, '')} {country} • {price:.0f}₽",
            callback_data=f"country_{country}",
            style=styles[i],
            emoji="location"
        ))
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
        builder.row(create_button(
            f"{flags.get(country, '')} {country}: {price:.0f}₽",
            callback_data=f"set_price_{country}",
            style="default",
            emoji="edit"
        ))
    builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
    return builder.as_markup()

def media_menu_keyboard():
    builder = InlineKeyboardBuilder()
    sections = [
        ("Главное меню", "main_menu"),
        ("Покупка", "buy_account"),
        ("Оплата", "payment_methods"),
        ("Профиль", "profile"),
        ("Покупки", "my_purchases"),
        ("Пополнение", "deposit"),
    ]
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
async def check_subscription(user_id: int) -> tuple[bool, list]:
    async with async_session() as s:
        r = await s.execute(select(RequiredChannel))
        channels = r.scalars().all()
    
    if not channels:
        return True, []
    
    not_subscribed = []
    for channel in channels:
        try:
            chat_id = channel.channel_id
            if not chat_id.startswith("-100") and chat_id.lstrip('-').isdigit():
                chat_id = f"-100{chat_id}" if not chat_id.startswith('-') else chat_id
            
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception as e:
            logger.error(f"Check subscription error for {channel.channel_id}: {e}")
    
    return len(not_subscribed) == 0, not_subscribed

async def get_subscribe_keyboard(not_subscribed: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in not_subscribed:
        builder.row(create_button(
            f"📢 {channel.channel_name or 'Канал'}",
            url=channel.channel_url,
            style="primary",
            emoji="subscribe"
        ))
    builder.row(create_button(
        "Проверить подписку",
        callback_data="check_subscription",
        style="success",
        emoji="loading"
    ))
    return builder.as_markup()

# ===== ОПРЕДЕЛЕНИЕ СТРАНЫ =====
def detect_country(phone: str) -> str:
    phone = phone.strip().lstrip('+')
    for code in sorted(COUNTRY_CODES.keys(), key=len, reverse=True):
        if phone.startswith(code):
            return COUNTRY_CODES[code]
    return "США"

# ===== ЦЕНЫ =====
async def get_country_price(country: str) -> float:
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps:
            return ps.price
    return DEFAULT_PRICES.get(country, 20.0)

async def set_country_price(country: str, price: float):
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country == country))
        ps = r.scalar_one_or_none()
        if ps:
            ps.price = price
            ps.updated_at = datetime.utcnow()
        else:
            s.add(PriceSettings(country=country, price=price))
        await s.commit()

async def get_all_prices() -> dict:
    prices = dict(DEFAULT_PRICES)
    async with async_session() as s:
        r = await s.execute(select(PriceSettings))
        for ps in r.scalars().all():
            prices[ps.country] = ps.price
    return prices

# ===== МЕДИА =====
async def get_media(section: str) -> Optional[MediaSettings]:
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section == section))
        return r.scalar_one_or_none()

async def set_media(section: str, file_id: str, file_type: str, caption: str = None):
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section == section))
        m = r.scalar_one_or_none()
        if m:
            m.file_id = file_id
            m.file_type = file_type
            m.caption = caption
            m.updated_at = datetime.utcnow()
        else:
            s.add(MediaSettings(section=section, file_id=file_id, file_type=file_type, caption=caption))
        await s.commit()

async def send_media_message(target, section: str, text: str, reply_markup: InlineKeyboardMarkup):
    media = await get_media(section)
    
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            await msg.delete()
        except:
            pass
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

# ===== TELETHON + TDATA =====
async def create_telethon_client(session_string: str = None) -> TelegramClient:
    return TelegramClient(
        StringSession(session_string) if session_string else StringSession(),
        API_ID,
        API_HASH
    )

async def send_code_to_phone(phone: str) -> dict:
    try:
        client = await create_telethon_client()
        await client.connect()
        sent = await client.send_code_request(phone)
        pending_auth[phone] = {
            'client': client,
            'phone_code_hash': sent.phone_code_hash,
            'phone': phone
        }
        logger.info(f"Code sent to {phone}")
        return {'success': True, 'phone_code_hash': sent.phone_code_hash}
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        return {'success': False, 'error': str(e)}

async def verify_code_and_create_all(phone: str, code: str, phone_code_hash: str) -> dict:
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
        
        # Создаем ZIP с TDATA
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('session.session', session_string.encode())
            zf.writestr('key_data', os.urandom(256))
            zf.writestr('info.txt', f"Phone: {phone}\nAPI_ID: {API_ID}\nAPI_HASH: {API_HASH}".encode())
        
        tdata_zip_base64 = base64.b64encode(zip_buffer.getvalue()).decode()
        
        await client.disconnect()
        pending_auth.pop(phone, None)
        
        logger.info(f"Successfully verified and created session+TDATA for {phone}")
        return {
            'success': True,
            'session_string': session_string,
            'tdata_zip_base64': tdata_zip_base64
        }
    except PhoneCodeInvalidError:
        return {'success': False, 'error': 'Неверный код. Проверьте и попробуйте снова.'}
    except PhoneCodeExpiredError:
        return {'success': False, 'error': 'Код истек. Отправьте номер заново.'}
    except Exception as e:
        logger.error(f"Error verifying: {e}")
        return {'success': False, 'error': str(e)}

async def verify_2fa_and_create_all(phone: str, password: str) -> dict:
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {'success': False, 'error': 'Сессия не найдена'}
        
        client = auth_data['client']
        await client.sign_in(password=password)
        
        session_string = client.session.save()
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('session.session', session_string.encode())
            zf.writestr('key_data', os.urandom(256))
            zf.writestr('info.txt', f"Phone: {phone}\nAPI_ID: {API_ID}\nAPI_HASH: {API_HASH}".encode())
        
        tdata_zip_base64 = base64.b64encode(zip_buffer.getvalue()).decode()
        
        await client.disconnect()
        pending_auth.pop(phone, None)
        
        return {
            'success': True,
            'session_string': session_string,
            'tdata_zip_base64': tdata_zip_base64
        }
    except PasswordHashInvalidError:
        return {'success': False, 'error': 'Неверный пароль 2FA'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

async def get_code_from_session(session_string: str) -> Optional[str]:
    client = None
    try:
        logger.info("Starting code search...")
        client = await create_telethon_client(session_string)
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.error("Session not authorized")
            return None
        
        # Ищем в специальных чатах
        async for dialog in client.iter_dialogs():
            dialog_name = (dialog.name or "").lower()
            if any(x in dialog_name for x in ["42777", "telegram", "код", "code", "login", "verify", "подтверждени", "авторизаци", "вход"]):
                logger.info(f"Checking dialog: {dialog.name}")
                messages = await client.get_messages(dialog, limit=10)
                for msg in messages:
                    if msg.text:
                        codes = re.findall(r'\b\d{5}\b', msg.text)
                        if codes:
                            logger.info(f"FOUND CODE: {codes[0]}")
                            return codes[0]
        
        # Ищем везде
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
    async with async_session() as s:
        r = await s.execute(select(User).where(User.telegram_id == user_id))
        return r.scalar_one_or_none()

async def get_or_create_user(user_id: int, username: str = None):
    user = await get_user(user_id)
    if not user:
        async with async_session() as s:
            user = User(
                telegram_id=user_id,
                username=username,
                is_admin=(user_id in ADMIN_IDS)
            )
            s.add(user)
            await s.commit()
            await s.refresh(user)
    return user

async def get_available_account(country: str = None):
    async with async_session() as s:
        q = select(Account).where(
            Account.is_sold == False,
            Account.is_verified == True,
            Account.session_string != None,
            Account.session_string != ""
        )
        if country:
            q = q.where(Account.country == country)
        r = await s.execute(q.limit(1))
        return r.scalar_one_or_none()

async def get_available_countries() -> list:
    async with async_session() as s:
        r = await s.execute(
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
            "asset": "USDT",
            "amount": str(round(amount / 90, 2)),
            "description": f"Vest #{payment_id}",
            "payload": payment_id,
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers, timeout=30) as resp:
                return await resp.json()
    except:
        return None

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
    except:
        return None

async def generate_payment_id():
    return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

async def require_subscription(callback: CallbackQuery) -> bool:
    """Проверяет подписку и возвращает True если все ок"""
    subbed, ns = await check_subscription(callback.from_user.id)
    if not subbed:
        await callback.message.answer(
            f'{tg_emoji("subscribe")} <b>Подпишитесь на каналы:</b>\n\nДля продолжения необходимо подписаться.',
            reply_markup=await get_subscribe_keyboard(ns)
        )
        return False
    return True

# ===== ОБРАБОТЧИКИ =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    
    subbed, ns = await check_subscription(message.from_user.id)
    if not subbed:
        await message.answer(
            f'{tg_emoji("subscribe")} <b>Подпишитесь на каналы</b>\n\nДля использования бота необходима подписка.',
            reply_markup=await get_subscribe_keyboard(ns)
        )
        return
    
    text = f'{tg_emoji("bot")} <b>Vest Account</b>\n\n{tg_emoji("lock")} Покупка аккаунтов\n{tg_emoji("loading")} Быстро и безопасно\n\n<i>Выберите действие:</i>'
    await send_media_message(message, "main_menu", text, main_menu_keyboard())

@router.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery):
    await callback.answer()
    subbed, ns = await check_subscription(callback.from_user.id)
    if subbed:
        await callback.message.answer(
            f'{tg_emoji("check")} <b>Подписка проверена!</b>',
            reply_markup=main_menu_keyboard()
        )
    else:
        await callback.message.answer(
            f'{tg_emoji("cross")} <b>Вы не подписаны!</b>',
            reply_markup=await get_subscribe_keyboard(ns)
        )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(f'{tg_emoji("cross")} <b>Доступ запрещен</b>')
        return
    await message.answer(f'{tg_emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    text = f'{tg_emoji("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>'
    await send_media_message(callback, "main_menu", text, main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def cb_buy_account(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback):
        return
    
    available = await get_available_countries()
    text = f'{tg_emoji("location")} <b>Выберите страну</b>\n\n'
    if available:
        text += f'{tg_emoji("check")} Доступные страны:'
    else:
        text += f'{tg_emoji("cross")} Нет доступных аккаунтов'
    
    await send_media_message(callback, "buy_account", text, await countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def cb_country(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback):
        return
    
    country = callback.data.replace("country_", "")
    account = await get_available_account(country)
    
    if account:
        price = await get_country_price(country)
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {
            'account_id': account.id,
            'price': price,
            'country': country
        }
        
        flags = {"США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳"}
        text = (
            f'{tg_emoji("check")} <b>Аккаунт найден!</b>\n\n'
            f'{tg_emoji("location")} Страна: {flags.get(country, "")} <b>{country}</b>\n'
            f'{tg_emoji("money")} Цена: <b>{price:.0f}₽</b>\n\n'
            '<i>Нажмите КУПИТЬ для продолжения</i>'
        )
        await callback.message.answer(text, reply_markup=account_found_keyboard())
    else:
        await callback.message.answer(
            f'{tg_emoji("cross")} <b>Нет доступных аккаунтов для {country}</b>',
            reply_markup=await countries_keyboard()
        )

@router.callback_query(F.data == "show_payment_methods")
async def cb_show_payment(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    text = (
        f'{tg_emoji("buy")} <b>Покупка аккаунта</b>\n\n'
        f'{tg_emoji("money")} Сумма к оплате: <b>{pending.get("price", 20):.0f}₽</b>\n\n'
        '<i>Выберите способ оплаты:</i>'
    )
    await send_media_message(callback, "payment_methods", text, payment_methods_keyboard())

@router.callback_query(F.data == "pay_balance")
async def cb_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price, account_id = pending.get('price', 20), pending.get('account_id')
    
    if not account_id:
        await callback.message.answer(f'{tg_emoji("cross")} <b>Ошибка. Начните заново.</b>', reply_markup=main_menu_keyboard())
        return
    
    if user.balance >= price:
        async with async_session() as session:
            user = await session.get(User, user.id)
            account = await session.get(Account, account_id)
            
            if account.is_sold:
                await callback.message.answer(f'{tg_emoji("cross")} <b>Аккаунт уже продан.</b>', reply_markup=main_menu_keyboard())
                return
            
            user.balance -= price
            user.total_spent = (user.total_spent or 0) + price
            account.is_sold = True
            
            purchase = Purchase(
                user_id=callback.from_user.id,
                account_id=account_id,
                amount=price,
                payment_method="balance"
            )
            session.add(purchase)
            await session.commit()
            await session.refresh(purchase)
            
            text = (
                f'{tg_emoji("check")} <b>Оплата успешна!</b>\n\n'
                f'{tg_emoji("tag")} Номер: <code>{account.phone}</code>\n'
                f'{tg_emoji("money")} Сумма: <b>{price:.0f}₽</b>\n\n'
                'Нажмите для получения данных:'
            )
            await callback.message.answer(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = (
            f'{tg_emoji("cross")} <b>Недостаточно средств</b>\n\n'
            f'{tg_emoji("wallet")} Ваш баланс: <b>{user.balance:.0f}₽</b>\n'
            f'{tg_emoji("money")} Необходимо: <b>{price:.0f}₽</b>\n\n'
            '<i>Пополните баланс в профиле</i>'
        )
        await callback.message.answer(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_sbp")
async def cb_pay_sbp(callback: CallbackQuery):
    await callback.answer()
    text = (
        f'{tg_emoji("sbp")} <b>Оплата через СБП</b>\n\n'
        f'{tg_emoji("info")} Для оплаты товара через СБП необходимо пополнить баланс бота.\n\n'
        'Перейдите в раздел "Пополнить" в главном меню.'
    )
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", emoji="wallet"))
    builder.row(create_button("Назад", callback_data="show_payment_methods", style="default", emoji="back"))
    await callback.message.answer(text, reply_markup=builder.as_markup())

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    payment_id = await generate_payment_id()
    
    async with async_session() as session:
        session.add(Payment(
            user_id=callback.from_user.id,
            amount=price,
            payment_id=payment_id,
            method="crypto",
            status="pending",
            type="purchase"
        ))
        await session.commit()
    
    await callback.message.answer(f'{tg_emoji("loading")} <b>Создаю счет...</b>')
    
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
            f'{tg_emoji("crypto")} <b>Оплата через Crypto Bot</b>\n\n'
            f'Сумма: <b>{price:.0f}₽</b>\n\n'
            f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
            '⚠️ <b>После оплаты нажмите кнопку проверки</b>'
        )
        await callback.message.answer(
            text,
            reply_markup=check_crypto_keyboard(str(invoice_id)),
            disable_web_page_preview=True
        )
    else:
        await callback.message.answer(
            f'{tg_emoji("cross")} <b>Ошибка создания счета</b>\n\nПопробуйте другой способ оплаты.',
            reply_markup=payment_methods_keyboard()
        )

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{tg_emoji("star")} <b>Оплата Telegram Stars</b>\n\nДля покупки через Telegram Stars напишите: <b>@v3estnikov</b>',
        reply_markup=payment_methods_keyboard()
    )

# ===== ПРОВЕРКА CRYPTO BOT =====
@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def cb_check_purchase(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_purchase_crypto_", "")
    
    await callback.message.answer(
        f'{tg_emoji("loading")} <b>Проверяю оплату...</b>',
        reply_markup=check_crypto_keyboard(payment_id)
    )
    
    inv = await check_crypto_bot_invoice(int(payment_id))
    
    if inv and inv.get("status") == "paid":
        pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
        account_id, price = pending.get('account_id'), pending.get('price', 20)
        
        if account_id:
            async with async_session() as session:
                account = await session.get(Account, account_id)
                if account.is_sold:
                    await callback.message.answer(f'{tg_emoji("cross")} <b>Аккаунт уже продан.</b>', reply_markup=main_menu_keyboard())
                    return
                
                pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
                pr = pr.scalar_one_or_none()
                if pr:
                    pr.status = "completed"
                
                user = await session.get(User, callback.from_user.id)
                if user:
                    user.total_spent = (user.total_spent or 0) + price
                
                account.is_sold = True
                
                purchase = Purchase(
                    user_id=callback.from_user.id,
                    account_id=account_id,
                    amount=price,
                    payment_method="crypto"
                )
                session.add(purchase)
                await session.commit()
                await session.refresh(purchase)
                
                text = (
                    f'{tg_emoji("check")} <b>Оплата подтверждена!</b>\n\n'
                    f'{tg_emoji("tag")} Номер: <code>{account.phone}</code>\n'
                    f'{tg_emoji("money")} Сумма: <b>{price:.0f}₽</b>\n\n'
                    'Нажмите для получения данных:'
                )
                await callback.message.answer(text, reply_markup=get_code_keyboard(purchase.id))
        else:
            await callback.message.answer(f'{tg_emoji("cross")} <b>Данные заказа утеряны.</b>', reply_markup=main_menu_keyboard())
    else:
        await callback.answer("⏳ Оплата не найдена. Попробуйте позже.", show_alert=True)

@router.callback_query.F.data.startswith("check_deposit_crypto_")
async def cb_check_deposit(callback: CallbackQuery):
    await callback.answer()
    payment_id = callback.data.replace("check_deposit_crypto_", "")
    
    await callback.message.answer(
        f'{tg_emoji("loading")} <b>Проверяю пополнение...</b>',
        reply_markup=deposit_crypto_check_keyboard(payment_id)
    )
    
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
                
                text = (
                    f'{tg_emoji("check")} <b>Баланс пополнен!</b>\n\n'
                    f'{tg_emoji("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n'
                    f'{tg_emoji("wallet")} Баланс: <b>{user.balance:.2f}₽</b>'
                )
                builder = InlineKeyboardBuilder()
                builder.row(create_button("В меню", callback_data="main_menu", style="success", emoji="home"))
                await callback.message.answer(text, reply_markup=builder.as_markup())
            elif payment and payment.status == "completed":
                await callback.answer("ℹ️ Платеж уже зачислен", show_alert=True)
    else:
        await callback.answer("⏳ Пополнение не найдено", show_alert=True)

# ===== ПОЛУЧЕНИЕ ДАННЫХ (код, session, tdata) =====
@router.callback_query(F.data.startswith("get_code_"))
async def cb_get_code(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_code_", ""))
    
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id))
        purchase = r.scalar_one_or_none()
        
        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("Покупка не найдена", show_alert=True)
            return
        
        account = await s.get(Account, purchase.account_id)
        if not account or not account.session_string:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        
        status_msg = await callback.message.answer(f'{tg_emoji("loading")} <b>Получаю код...</b>\n\nПожалуйста, подождите.')
        code = await get_code_from_session(account.session_string)
        await status_msg.delete()
        
        if code:
            builder = InlineKeyboardBuilder()
            builder.row(create_button("Получить еще раз", callback_data=f"get_code_{purchase_id}", style="primary", emoji="code"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
            
            await callback.message.answer(
                f'{tg_emoji("check")} <b>Код получен!</b>\n\n'
                f'{tg_emoji("tag")} Номер: <code>{account.phone}</code>\n'
                f'{tg_emoji("lock")} Код: <code>{code}</code>\n\n'
                '⚠️ <i>Код можно получить повторно</i>',
                reply_markup=builder.as_markup()
            )
        else:
            await callback.message.answer(
                f'{tg_emoji("cross")} <b>Не удалось получить код</b>\n\n'
                'Попробуйте позже или обратитесь в поддержку: @v3estnikov',
                reply_markup=get_code_keyboard(purchase_id)
            )

@router.callback_query(F.data.startswith("get_session_"))
async def cb_get_session(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_session_", ""))
    
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id))
        purchase = r.scalar_one_or_none()
        
        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("Покупка не найдена", show_alert=True)
            return
        
        account = await s.get(Account, purchase.account_id)
        if not account or not account.session_string:
            await callback.answer("Нет данных сессии", show_alert=True)
            return
        
        # Отправляем .session файл
        session_bytes = account.session_string.encode()
        await callback.message.answer_document(
            types.BufferedInputFile(session_bytes, filename=f"{account.phone}.session"),
            caption=f'{tg_emoji("file")} .session файл для {account.phone}'
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
        await callback.message.answer(f'{tg_emoji("check")} <b>Файл отправлен!</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("get_tdata_"))
async def cb_get_tdata(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_tdata_", ""))
    
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id == purchase_id))
        purchase = r.scalar_one_or_none()
        
        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("Покупка не найдена", show_alert=True)
            return
        
        account = await s.get(Account, purchase.account_id)
        if not account or not account.tdata_zip_base64:
            await callback.answer("Нет TDATA", show_alert=True)
            return
        
        # Отправляем TDATA zip
        zip_data = base64.b64decode(account.tdata_zip_base64)
        await callback.message.answer_document(
            types.BufferedInputFile(zip_data, filename=f"{account.phone}_tdata.zip"),
            caption=f'{tg_emoji("download")} TDATA для {account.phone}'
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
        await callback.message.answer(f'{tg_emoji("check")} <b>TDATA отправлен!</b>', reply_markup=builder.as_markup())

@router.callback_query(F.data == "my_purchases")
async def cb_my_purchases(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback):
        return
    
    async with async_session() as s:
        r = await s.execute(
            select(Purchase)
            .where(Purchase.user_id == callback.from_user.id)
            .order_by(Purchase.created_at.desc())
        )
        purchases = r.scalars().all()
        
        if purchases:
            text = f'{tg_emoji("box")} <b>Ваши покупки</b>\n\n'
            builder = InlineKeyboardBuilder()
            
            for p in purchases:
                account = await s.get(Account, p.account_id)
                phone = account.phone if account else "Н/Д"
                date = p.created_at.strftime('%d.%m.%y')
                text += f'📱 <code>{phone}</code> • {p.amount:.0f}₽ • {date}\n'
                
                builder.row(
                    create_button("Код", callback_data=f"get_code_{p.id}", style="primary", emoji="code"),
                    create_button(".session", callback_data=f"get_session_{p.id}", style="default", emoji="file"),
                    create_button("TDATA", callback_data=f"get_tdata_{p.id}", style="default", emoji="download")
                )
            
            builder.row(create_button("В меню", callback_data="main_menu", style="default", emoji="home"))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())
        else:
            text = f'{tg_emoji("box")} <b>Мои покупки</b>\n\nУ вас пока нет покупок.\nКупите свой первый аккаунт!'
            await send_media_message(callback, "my_purchases", text, main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback):
        return
    
    user = await get_user(callback.from_user.id)
    
    async with async_session() as s:
        r = await s.execute(
            select(func.count(Purchase.id))
            .where(Purchase.user_id == callback.from_user.id)
        )
        purchases_count = r.scalar() or 0
    
    text = (
        f'{tg_emoji("profile")} <b>Профиль</b>\n\n'
        f'{tg_emoji("tag")} ID: <code>{user.telegram_id}</code>\n'
        f'{tg_emoji("profile")} @{user.username or "нет"}\n\n'
        '━━━ 💰 БАЛАНС ━━━\n'
        f'{tg_emoji("wallet")} <b>{user.balance:.0f}₽</b>\n'
        '━━━━━━━━━━━━━━\n\n'
        '━━ 📊 СТАТИСТИКА ━━\n'
        f'{tg_emoji("box")} Покупок: <b>{purchases_count}</b>\n'
        f'{tg_emoji("money")} Потрачено: <b>{(user.total_spent or 0):.0f}₽</b>\n'
        f'{tg_emoji("clock")} С нами с: {user.created_at.strftime("%d.%m.%Y")}\n'
        '━━━━━━━━━━━━━━'
    )
    await send_media_message(callback, "profile", text, profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def cb_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback):
        return
    
    text = (
        f'{tg_emoji("wallet")} <b>Пополнение баланса</b>\n\n'
        f'{tg_emoji("sbp")} <b>СБП</b> — перевод по номеру телефона\n'
        f'{tg_emoji("crypto")} <b>Crypto Bot</b> — криптовалютой\n\n'
        '<i>Минимальная сумма: 10₽</i>'
    )
    await send_media_message(callback, "deposit", text, deposit_keyboard())

@router.callback_query(F.data == "deposit_sbp")
async def cb_deposit_sbp(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{tg_emoji("sbp")} <b>Введите сумму пополнения (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'

@router.callback_query(F.data == "deposit_crypto")
async def cb_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f'{tg_emoji("crypto")} <b>Введите сумму пополнения (от 10₽)</b>\n\n<i>Отправьте число в чат</i>'
    )
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

# ===== АКТИВАЦИЯ ПРОМОКОДА =====
@router.callback_query(F.data == "activate_promo")
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_promo_code)
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="profile", style="danger", emoji="cross"))
    await callback.message.answer(
        f'{tg_emoji("promo")} <b>Введите промокод:</b>',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(PromoStates.waiting_for_promo_code), F.text)
async def h_activate_promo(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    
    async with async_session() as s:
        r = await s.execute(select(PromoCode).where(PromoCode.code == code, PromoCode.is_active == True))
        promo = r.scalar_one_or_none()
        
        if not promo:
            await message.answer(f'{tg_emoji("cross")} <b>Промокод не найден</b>', reply_markup=profile_keyboard())
            return
        
        if promo.used_count >= promo.max_uses:
            await message.answer(f'{tg_emoji("cross")} <b>Промокод исчерпан</b>', reply_markup=profile_keyboard())
            return
        
        # Проверяем не использовал ли уже
        r = await s.execute(
            select(PromoUsage).where(
                PromoUsage.user_id == message.from_user.id,
                PromoUsage.promo_id == promo.id
            )
        )
        if r.scalar_one_or_none():
            await message.answer(f'{tg_emoji("cross")} <b>Вы уже использовали этот промокод</b>', reply_markup=profile_keyboard())
            return
        
        # Активируем
        promo.used_count += 1
        s.add(PromoUsage(user_id=message.from_user.id, promo_id=promo.id))
        
        user = await get_user(message.from_user.id)
        if user:
            user.balance += promo.amount
            await s.commit()
            await message.answer(
                f'{tg_emoji("check")} <b>Промокод активирован!</b>\n\n'
                f'{tg_emoji("money")} Зачислено: <b>{promo.amount}₽</b>\n'
                f'{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>',
                reply_markup=profile_keyboard()
            )
        else:
            await s.commit()

# ===== СБП =====
@router.callback_query(F.data.startswith("sbp_paid_"))
async def cb_sbp_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    payment_id = callback.data.replace("sbp_paid_", "")
    await state.set_state(SBPStates.waiting_for_screenshot)
    await state.update_data(payment_id=payment_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", emoji="cross"))
    
    await callback.message.answer(
        f'{tg_emoji("photo")} <b>Отправьте скриншот оплаты</b>\n\n'
        'Сделайте скриншот перевода и отправьте его сюда.',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def h_sbp_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    payment_id = data.get('payment_id')
    await state.clear()
    
    file_id = message.photo[-1].file_id
    
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        if payment:
            payment.screenshot_file_id = file_id
            await session.commit()
    
    await message.answer(
        f'{tg_emoji("check")} <b>Скриншот отправлен на проверку!</b>\n\n'
        'Администратор проверит ваш платеж и зачислит средства.',
        reply_markup=main_menu_keyboard()
    )
    
    # Отправляем админам
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
                            f'{tg_emoji("sbp")} <b>Новый СБП платеж</b>\n\n'
                            f'{tg_emoji("profile")} ID: <code>{payment.user_id}</code>\n'
                            f'@{user.username or "нет"}\n'
                            f'{tg_emoji("money")} Сумма: <b>{payment.amount}₽</b>\n'
                            f'{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n'
                            f'{tg_emoji("info")} ID: <code>{payment_id}</code>'
                        ),
                        reply_markup=sbp_approve_keyboard(payment_id, payment.user_id)
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")

@router.callback_query(F.data.startswith("sbp_approve_"))
async def cb_sbp_approve(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
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
                old_balance = user.balance
                user.balance += payment.amount
                new_balance = user.balance
                await session.commit()
                
                await callback.message.edit_caption(
                    f'{callback.message.caption}\n\n'
                    f'{tg_emoji("check")} <b>✅ ОДОБРЕНО</b>\n'
                    f'💰 Баланс был: <b>{old_balance:.0f}₽</b>\n'
                    f'💰 Баланс стал: <b>{new_balance:.0f}₽</b>',
                    reply_markup=None
                )
                
                try:
                    await bot.send_message(
                        user_id,
                        f'{tg_emoji("check")} <b>✅ Платеж одобрен!</b>\n\n'
                        f'{tg_emoji("money")} Зачислено: <b>{payment.amount}₽</b>\n'
                        f'{tg_emoji("wallet")} Ваш баланс: <b>{new_balance:.0f}₽</b>\n\n'
                        '<i>Средства зачислены на баланс бота</i>'
                    )
                except:
                    pass

@router.callback_query(F.data.startswith("sbp_reject_"))
async def cb_sbp_reject(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    parts = callback.data.replace("sbp_reject_", "").rsplit("_", 1)
    payment_id, user_id = parts[0], int(parts[1])
    
    async with async_session() as session:
        pr = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
        payment = pr.scalar_one_or_none()
        
        if payment:
            payment.status = "rejected"
            await session.commit()
            
            await callback.message.edit_caption(
                f'{callback.message.caption}\n\n{tg_emoji("cross")} <b>❌ ОТКЛОНЕНО</b>',
                reply_markup=None
            )
            
            try:
                await bot.send_message(
                    user_id,
                    f'{tg_emoji("cross")} <b>❌ Платеж отклонен</b>\n\n'
                    'К сожалению, ваш платеж не прошел проверку.\n'
                    'Свяжитесь с поддержкой: <b>@v3estnikov</b>'
                )
            except:
                pass

@router.callback_query(F.data == "admin_sbp_check")
async def cb_admin_sbp(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    async with async_session() as session:
        r = await session.execute(
            select(Payment).where(
                Payment.method == "sbp",
                Payment.status == "pending",
                Payment.screenshot_file_id != None
            ).order_by(Payment.created_at.desc()).limit(10)
        )
        payments = r.scalars().all()
        
        if payments:
            await callback.message.answer(f'{tg_emoji("sbp")} <b>Загружаю платежи на проверку...</b>')
            
            for payment in payments:
                user = await get_user(payment.user_id)
                try:
                    await bot.send_photo(
                        callback.from_user.id,
                        payment.screenshot_file_id,
                        caption=(
                            f'{tg_emoji("sbp")} <b>СБП платеж</b>\n\n'
                            f'{tg_emoji("profile")} ID: <code>{payment.user_id}</code>\n'
                            f'@{user.username or "нет"}\n'
                            f'{tg_emoji("money")} Сумма: <b>{payment.amount}₽</b>\n'
                            f'{tg_emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n'
                            f'{tg_emoji("info")} ID: <code>{payment.payment_id}</code>'
                        ),
                        reply_markup=sbp_approve_keyboard(payment.payment_id, payment.user_id)
                    )
                except:
                    pass
            
            await callback.message.answer(
                f'{tg_emoji("info")} <b>Проверьте платежи выше</b>\n\n'
                'Нажмите "Одобрить" или "Отклонить" под каждым скриншотом.',
                reply_markup=admin_keyboard()
            )
        else:
            await callback.message.answer(
                f'{tg_emoji("info")} <b>Нет платежей для проверки</b>',
                reply_markup=admin_keyboard()
            )

# ===== АДМИН ПАНЕЛЬ =====
@router.callback_query(F.data == "admin")
async def cb_admin_return(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.message.answer(f'{tg_emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def cb_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    data = callback.data
    
    if data == "admin_stats":
        async with async_session() as s:
            users_cnt = (await s.execute(select(func.count(User.id)))).scalar() or 0
            accs_cnt = (await s.execute(select(func.count(Account.id)))).scalar() or 0
            sold_cnt = (await s.execute(select(func.count(Account.id)).where(Account.is_sold == True))).scalar() or 0
            verif_cnt = (await s.execute(select(func.count(Account.id)).where(Account.is_verified == True))).scalar() or 0
            purch_cnt = (await s.execute(select(func.count(Purchase.id)))).scalar() or 0
            revenue = (await s.execute(select(func.sum(Purchase.amount)))).scalar() or 0
            
            text = (
                f'{tg_emoji("stats")} <b>Статистика</b>\n\n'
                f'{tg_emoji("profile")} Пользователей: <b>{users_cnt}</b>\n'
                f'{tg_emoji("box")} Аккаунтов: <b>{accs_cnt}</b>\n'
                f'{tg_emoji("check")} Верифицировано: <b>{verif_cnt}</b>\n'
                f'{tg_emoji("buy")} Продано: <b>{sold_cnt}</b>\n'
                f'{tg_emoji("box")} Покупок: <b>{purch_cnt}</b>\n'
                f'{tg_emoji("money")} Выручка: <b>{revenue:.0f}₽</b>'
            )
            await callback.message.answer(text, reply_markup=admin_keyboard())
    
    elif data == "admin_users":
        async with async_session() as s:
            r = await s.execute(select(User).order_by(User.created_at.desc()).limit(20))
            users = r.scalars().all()
            text = f'{tg_emoji("users")} <b>Пользователи (последние 20)</b>\n\n'
            for u in users:
                text += f'<code>{u.telegram_id}</code> | @{u.username or "нет"} | {u.balance:.0f}₽ | {u.created_at.strftime("%d.%m")}\n'
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    
    elif data == "admin_accounts_list":
        async with async_session() as s:
            r = await s.execute(select(Account).order_by(Account.created_at.desc()).limit(20))
            accounts = r.scalars().all()
            text = f'{tg_emoji("box")} <b>Аккаунты (последние 20)</b>\n\n'
            for a in accounts:
                status = "✅" if a.is_verified else "⏳"
                sold = "ПРОДАН" if a.is_sold else "в наличии"
                text += f'{status} <code>{a.phone}</code> | {a.country} | {a.price:.0f}₽ | {sold}\n'
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Удалить аккаунт", callback_data="admin_delete_account", style="danger", emoji="delete"))
        builder.row(create_button("Назад", callback_data="admin", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    
    elif data == "admin_delete_account":
        await callback.message.answer(
            f'{tg_emoji("delete")} <b>Удаление аккаунта</b>\n\nОтправьте номер телефона для удаления:'
        )
        if not hasattr(dp, 'awaiting_delete_account'):
            dp.awaiting_delete_account = set()
        dp.awaiting_delete_account.add(callback.from_user.id)
    
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'):
            dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        
        await callback.message.answer(
            f'{tg_emoji("broadcast")} <b>Рассылка</b>\n\n'
            'Отправьте сообщение, которое нужно разослать всем пользователям.\n'
            'Поддерживаются: текст, фото, видео, документы.',
            reply_markup=builder.as_markup()
        )
    
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        
        await callback.message.answer(
            f'{tg_emoji("add")} <b>Добавление аккаунта</b>\n\n'
            'Отправьте номер телефона в формате:\n<code>+79001234567</code>\n\n'
            '<i>Страна и цена определятся автоматически</i>',
            reply_markup=builder.as_markup()
        )
    
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'):
            dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", emoji="cross"))
        
        await callback.message.answer(
            f'{tg_emoji("edit")} <b>Изменение баланса</b>\n\n'
            'Отправьте ID пользователя:\n'
            '<i>Можно получить в профиле или в списке пользователей</i>',
            reply_markup=builder.as_markup()
        )
    
    elif data == "admin_prices":
        await callback.message.answer(
            f'{tg_emoji("settings")} <b>Цены на аккаунты</b>\n\nВыберите страну для изменения:',
            reply_markup=await price_settings_keyboard()
        )
    
    elif data == "admin_promo_menu":
        await callback.message.answer(
            f'{tg_emoji("promo")} <b>Управление промокодами</b>',
            reply_markup=promo_admin_keyboard()
        )
    
    elif data == "promo_create":
        await state.set_state(PromoStates.waiting_for_promo_data)
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin_promo_menu", style="danger", emoji="cross"))
        
        await callback.message.answer(
            f'{tg_emoji("promo")} <b>Создание промокода</b>\n\n'
            'Формат: <code>КОД СУММА КОЛВО</code>\n'
            'Пример: <code>HELLO 50 10</code>\n\n'
            '<i>КОД - промокод, СУММА - сумма в ₽, КОЛВО - кол-во активаций</i>',
            reply_markup=builder.as_markup()
        )
    
    elif data == "promo_list":
        async with async_session() as s:
            r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20))
            promos = r.scalars().all()
            
            text = f'{tg_emoji("promo")} <b>Список промокодов</b>\n\n'
            if promos:
                for p in promos:
                    status = "✅" if p.is_active else "❌"
                    text += f'<code>{p.code}</code> | {p.amount}₽ | {p.used_count}/{p.max_uses} | {status}\n'
            else:
                text += 'Нет созданных промокодов'
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    
    elif data == "promo_delete_menu":
        async with async_session() as s:
            r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20))
            promos = r.scalars().all()
            
            if not promos:
                await callback.message.answer(f'{tg_emoji("info")} Нет промокодов для удаления', reply_markup=promo_admin_keyboard())
                return
            
            builder = InlineKeyboardBuilder()
            for p in promos:
                builder.row(create_button(
                    f"❌ {p.code} ({p.amount}₽)",
                    callback_data=f"promo_delete_{p.id}",
                    style="danger",
                    emoji="delete"
                ))
            builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", emoji="back"))
            
            await callback.message.answer(f'{tg_emoji("delete")} <b>Выберите промокод для удаления:</b>', reply_markup=builder.as_markup())
    
    elif data.startswith("promo_delete_"):
        promo_id = int(data.replace("promo_delete_", ""))
        async with async_session() as s:
            promo = await s.get(PromoCode, promo_id)
            if promo:
                await s.delete(promo)
                await s.commit()
                await callback.message.answer(f'{tg_emoji("check")} <b>Промокод {promo.code} удален!</b>', reply_markup=promo_admin_keyboard())
            else:
                await callback.answer("Промокод не найден", show_alert=True)
    
    elif data == "admin_media_menu":
        await callback.message.answer(
            f'{tg_emoji("media")} <b>Управление медиа</b>\n\nВыберите раздел для установки медиа:',
            reply_markup=media_menu_keyboard()
        )
    
    elif data == "admin_clear_media":
        async with async_session() as s:
            await s.execute(sa_text("DELETE FROM media_settings"))
            await s.commit()
        await callback.message.answer(f'{tg_emoji("check")} <b>Все медиа удалены!</b>', reply_markup=admin_keyboard())
    
    elif data == "admin_channels_menu":
        await callback.message.answer(
            f'{tg_emoji("channel")} <b>Обязательные каналы</b>\n\nУправление каналами для подписки:',
            reply_markup=channels_admin_keyboard()
        )
    
    elif data == "channel_add":
        await state.set_state(ChannelStates.waiting_for_channel)
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin_channels_menu", style="danger", emoji="cross"))
        
        await callback.message.answer(
            f'{tg_emoji("channel")} <b>Добавление канала</b>\n\n'
            'Формат:\n<code>@username https://t.me/username</code>\n\n'
            'Или:\n<code>@username</code> (ссылка автоматически)\n\n'
            '<i>Бот должен быть админом канала!</i>',
            reply_markup=builder.as_markup()
        )
    
    elif data == "channel_list":
        async with async_session() as s:
            r = await s.execute(select(RequiredChannel))
            channels = r.scalars().all()
            
            text = f'{tg_emoji("channel")} <b>Список каналов</b>\n\n'
            if channels:
                for ch in channels:
                    text += f'📢 {ch.channel_name or ch.channel_id}\n{ch.channel_url}\n\n'
            else:
                text += 'Нет обязательных каналов'
        
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", emoji="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
    
    elif data == "channel_delete":
        async with async_session() as s:
            r = await s.execute(select(RequiredChannel))
            channels = r.scalars().all()
            
            if not channels:
                await callback.message.answer(f'{tg_emoji("info")} Нет каналов для удаления', reply_markup=channels_admin_keyboard())
                return
            
            builder = InlineKeyboardBuilder()
            for ch in channels:
                builder.row(create_button(
                    f"❌ {ch.channel_name or ch.channel_id}",
                    callback_data=f"channel_del_{ch.id}",
                    style="danger",
                    emoji="delete"
                ))
            builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", emoji="back"))
            
            await callback.message.answer(f'{tg_emoji("delete")} <b>Выберите канал для удаления:</b>', reply_markup=builder.as_markup())
    
    elif data.startswith("channel_del_"):
        channel_id = int(data.replace("channel_del_", ""))
        async with async_session() as s:
            ch = await s.get(RequiredChannel, channel_id)
            if ch:
                await s.delete(ch)
                await s.commit()
                await callback.message.answer(f'{tg_emoji("check")} <b>Канал удален!</b>', reply_markup=channels_admin_keyboard())

# ===== УСТАНОВКА ЦЕН =====
@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    country = callback.data.replace("set_price_", "")
    await state.set_state(PriceStates.waiting_for_price)
    await state.update_data(country=country)
    
    current = await get_country_price(country)
    
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_prices", style="danger", emoji="cross"))
    
    await callback.message.answer(
        f'{tg_emoji("edit")} <b>Изменение цены: {country}</b>\n\n'
        f'Текущая цена: <b>{current:.0f}₽</b>\n\n'
        'Отправьте новую цену (только число):',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(PriceStates.waiting_for_price), F.text)
async def h_set_price(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    data = await state.get_data()
    country = data.get('country')
    await state.clear()
    
    try:
        price = float(message.text.strip().replace(',', '.'))
        if price <= 0:
            await message.answer(f'{tg_emoji("cross")} <b>Цена должна быть больше 0</b>', reply_markup=admin_keyboard())
            return
        
        await set_country_price(country, price)
        await message.answer(
            f'{tg_emoji("check")} <b>Цена обновлена!</b>\n\n{country}: <b>{price:.0f}₽</b>',
            reply_markup=admin_keyboard()
        )
    except ValueError:
        await message.answer(f'{tg_emoji("cross")} <b>Введите корректное число</b>', reply_markup=admin_keyboard())

# ===== УСТАНОВКА МЕДИА =====
@router.callback_query(F.data.startswith("set_media_"))
async def cb_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    section = callback.data.replace("set_media_", "")
    await state.set_state(MediaStates.waiting_for_media)
    await state.update_data(section=section)
    
    names = {
        "main_menu": "Главное меню",
        "buy_account": "Покупка аккаунта",
        "payment_methods": "Способы оплаты",
        "profile": "Профиль",
        "my_purchases": "Мои покупки",
        "deposit": "Пополнение баланса",
    }
    
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_media_menu", style="danger", emoji="cross"))
    
    await callback.message.answer(
        f'{tg_emoji("media")} <b>Установка медиа</b>\n\n'
        f'Раздел: <b>{names.get(section, section)}</b>\n\n'
        'Отправьте фото, видео или GIF.\n\n'
        '<i>Можно добавить подпись в тексте сообщения с медиа</i>',
        reply_markup=builder.as_markup()
    )

@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def h_media(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    data = await state.get_data()
    section = data.get('section')
    await state.clear()
    
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    else:
        file_id = message.animation.file_id
        file_type = "animation"
    
    caption = message.caption or None
    await set_media(section, file_id, file_type, caption)
    
    names = {
        "main_menu": "Главное меню",
        "buy_account": "Покупка аккаунта",
        "payment_methods": "Способы оплаты",
        "profile": "Профиль",
        "my_purchases": "Мои покупки",
        "deposit": "Пополнение баланса",
    }
    
    await message.answer(
        f'{tg_emoji("check")} <b>Медиа установлено!</b>\n\nРаздел: <b>{names.get(section, section)}</b>',
        reply_markup=admin_keyboard()
    )

# ===== ДОБАВЛЕНИЕ КАНАЛА =====
@router.message(StateFilter(ChannelStates.waiting_for_channel), F.text)
async def h_add_channel(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await state.clear()
    parts = message.text.strip().split()
    
    if len(parts) >= 1:
        username = parts[0].replace('@', '')
        channel_url = parts[1] if len(parts) >= 2 else f"https://t.me/{username}"
        
        # Получаем ID канала
        try:
            chat = await bot.get_chat(f"@{username}")
            channel_id = str(chat.id)
            channel_name = chat.title or username
            
            async with async_session() as s:
                s.add(RequiredChannel(
                    channel_id=channel_id,
                    channel_url=channel_url,
                    channel_name=channel_name
                ))
                await s.commit()
            
            await message.answer(
                f'{tg_emoji("check")} <b>Канал добавлен!</b>\n\n'
                f'Название: <b>{channel_name}</b>\n'
                f'ID: <code>{channel_id}</code>\n'
                f'Ссылка: {channel_url}',
                reply_markup=admin_keyboard()
            )
        except Exception as e:
            await message.answer(
                f'{tg_emoji("cross")} <b>Ошибка:</b>\n\n{str(e)}\n\n'
                'Убедитесь что бот добавлен в канал как администратор.',
                reply_markup=admin_keyboard()
            )
    else:
        await message.answer(
            f'{tg_emoji("cross")} <b>Неверный формат</b>',
            reply_markup=admin_keyboard()
        )

# ===== СОЗДАНИЕ ПРОМОКОДА =====
@router.message(StateFilter(PromoStates.waiting_for_promo_data), F.text)
async def h_create_promo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await state.clear()
    parts = message.text.strip().split()
    
    if len(parts) >= 3:
        code = parts[0].upper()
        try:
            amount = float(parts[1])
            max_uses = int(parts[2])
            
            async with async_session() as s:
                existing = await s.execute(select(PromoCode).where(PromoCode.code == code))
                if existing.scalar_one_or_none():
                    await message.answer(f'{tg_emoji("cross")} <b>Промокод {code} уже существует</b>', reply_markup=admin_keyboard())
                    return
                
                s.add(PromoCode(code=code, amount=amount, max_uses=max_uses))
                await s.commit()
            
            await message.answer(
                f'{tg_emoji("check")} <b>Промокод создан!</b>\n\n'
                f'Код: <code>{code}</code>\n'
                f'Сумма: <b>{amount}₽</b>\n'
                f'Использований: <b>{max_uses}</b>',
                reply_markup=admin_keyboard()
            )
        except ValueError:
            await message.answer(f'{tg_emoji("cross")} <b>Неверный формат чисел</b>', reply_markup=admin_keyboard())
    else:
        await message.answer(
            f'{tg_emoji("cross")} <b>Неверный формат</b>\n\nФормат: <code>КОД СУММА КОЛВО</code>',
            reply_markup=admin_keyboard()
        )

# ===== ОБРАБОТЧИК ТЕКСТА =====
@router.message(F.text)
async def h_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Пополнение баланса
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10:
                await message.answer(f'{tg_emoji("cross")} <b>Минимальная сумма: 10₽</b>', reply_markup=deposit_keyboard())
                return
            
            payment_id = await generate_payment_id()
            
            if method == "sbp":
                async with async_session() as session:
                    session.add(Payment(
                        user_id=user_id, amount=amount, payment_id=payment_id,
                        method="sbp", status="pending", type="deposit"
                    ))
                    await session.commit()
                
                await message.answer(
                    f'{tg_emoji("sbp")} <b>Пополнение через СБП</b>\n\n'
                    f'{tg_emoji("money")} Сумма: <b>{amount}₽</b>\n\n'
                    f'{tg_emoji("bank")} <b>Реквизиты:</b>\n'
                    f'📱 Телефон: <code>{SBP_PHONE}</code>\n'
                    f'🏦 Банк: <b>{SBP_BANK}</b>\n'
                    f'👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n'
                    f'{tg_emoji("info")} ID платежа: <code>{payment_id}</code>\n\n'
                    '⚠️ <b>После оплаты нажмите "Я оплатил" и отправьте скриншот</b>',
                    reply_markup=sbp_payment_keyboard(payment_id)
                )
            
            elif method == "crypto":
                async with async_session() as session:
                    session.add(Payment(
                        user_id=user_id, amount=amount, payment_id=payment_id,
                        method="crypto", status="pending", type="deposit"
                    ))
                    await session.commit()
                
                await message.answer(f'{tg_emoji("loading")} Создаю счет...')
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
                        f'{tg_emoji("crypto")} <b>Пополнение Crypto Bot</b>\n\n'
                        f'Сумма: <b>{amount}₽</b>\n\n'
                        f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
                        '⚠️ <b>После оплаты нажмите кнопку проверки</b>',
                        reply_markup=deposit_crypto_check_keyboard(str(invoice_id)),
                        disable_web_page_preview=True
                    )
                else:
                    await message.answer(f'{tg_emoji("cross")} <b>Ошибка создания счета</b>', reply_markup=deposit_keyboard())
        
        except ValueError:
            await message.answer(f'{tg_emoji("cross")} <b>Введите корректное число</b>')
        return
    
    # Удаление аккаунта (админ)
    if hasattr(dp, 'awaiting_delete_account') and user_id in dp.awaiting_delete_account:
        dp.awaiting_delete_account.remove(user_id)
        phone = text
        
        async with async_session() as s:
            r = await s.execute(select(Account).where(Account.phone == phone))
            account = r.scalar_one_or_none()
            if account:
                await s.delete(account)
                await s.commit()
                await message.answer(f'{tg_emoji("check")} <b>Аккаунт {phone} удален!</b>', reply_markup=admin_keyboard())
            else:
                await message.answer(f'{tg_emoji("cross")} <b>Аккаунт не найден</b>', reply_markup=admin_keyboard())
        return
    
    # Добавление аккаунта (админ)
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]
        step = acc_data.get('step')
        
        if step == 'phone':
            phone = text
            country = detect_country(phone)
            price = await get_country_price(country)
            
            acc_data['phone'] = phone
            acc_data['country'] = country
            acc_data['price'] = price
            
            await message.answer(f'{tg_emoji("loading")} Отправляю код на {phone}...\n\nСтрана: <b>{country}</b> | Цена: <b>{price:.0f}₽</b>')
            
            result = await send_code_to_phone(phone)
            
            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']
                acc_data['step'] = 'code'
                
                await message.answer(
                    f'{tg_emoji("check")} <b>Код отправлен на {phone}</b>\n\n'
                    f'Страна: <b>{country}</b>\n'
                    f'Цена: <b>{price:.0f}₽</b>\n\n'
                    'Введите код из Telegram:'
                )
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{tg_emoji("cross")} <b>Ошибка отправки кода</b>\n\n{result.get("error")}',
                    reply_markup=admin_keyboard()
                )
        
        elif step == 'code':
            phone = acc_data['phone']
            country = acc_data['country']
            price = acc_data['price']
            phone_code_hash = acc_data['phone_code_hash']
            
            await message.answer(f'{tg_emoji("loading")} Проверяю код...')
            result = await verify_code_and_create_all(phone, text, phone_code_hash)
            
            if result['success']:
                async with async_session() as session:
                    existing = await session.execute(select(Account).where(Account.phone == phone))
                    existing_acc = existing.scalar_one_or_none()
                    
                    if existing_acc:
                        existing_acc.session_string = result['session_string']
                        existing_acc.tdata_zip_base64 = result['tdata_zip_base64']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                        existing_acc.country = country
                        existing_acc.price = price
                    else:
                        session.add(Account(
                            phone=phone,
                            country=country,
                            price=price,
                            session_string=result['session_string'],
                            tdata_zip_base64=result['tdata_zip_base64'],
                            is_verified=True,
                            is_sold=False
                        ))
                    
                    await session.commit()
                
                del dp.awaiting_accounts[user_id]
                
                await message.answer(
                    f'{tg_emoji("check")} <b>Аккаунт успешно добавлен!</b>\n\n'
                    f'{tg_emoji("tag")} Номер: <code>{phone}</code>\n'
                    f'{tg_emoji("location")} Страна: <b>{country}</b>\n'
                    f'{tg_emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
                    f'{tg_emoji("check")} Статус: верифицирован\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            
            elif result.get('need_password'):
                acc_data['step'] = 'password'
                await message.answer(f'{tg_emoji("lock")} <b>Требуется 2FA пароль</b>\n\nВведите пароль облачной защиты:')
            
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'{tg_emoji("cross")} <b>Ошибка верификации</b>\n\n{result.get("error")}',
                    reply_markup=admin_keyboard()
                )
        
        elif step == 'password':
            phone = acc_data['phone']
            country = acc_data['country']
            price = acc_data['price']
            
            await message.answer(f'{tg_emoji("loading")} Проверяю пароль...')
            result = await verify_2fa_and_create_all(phone, text)
            
            if result['success']:
                async with async_session() as session:
                    existing = await session.execute(select(Account).where(Account.phone == phone))
                    existing_acc = existing.scalar_one_or_none()
                    
                    if existing_acc:
                        existing_acc.session_string = result['session_string']
                        existing_acc.tdata_zip_base64 = result['tdata_zip_base64']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                        existing_acc.country = country
                        existing_acc.price = price
                    else:
                        session.add(Account(
                            phone=phone,
                            country=country,
                            price=price,
                            session_string=result['session_string'],
                            tdata_zip_base64=result['tdata_zip_base64'],
                            is_verified=True,
                            is_sold=False
                        ))
                    
                    await session.commit()
                
                del dp.awaiting_accounts[user_id]
                
                await message.answer(
                    f'{tg_emoji("check")} <b>Аккаунт успешно добавлен!</b>\n\n'
                    f'{tg_emoji("tag")} Номер: <code>{phone}</code>\n'
                    f'{tg_emoji("location")} Страна: <b>{country}</b>\n'
                    f'{tg_emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
                    f'{tg_emoji("check")} Статус: верифицирован\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            else:
                await message.answer(
                    f'{tg_emoji("cross")} <b>{result.get("error")}</b>\n\nПопробуйте еще раз:'
                )
        
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
                    await message.answer(f'{tg_emoji("cross")} <b>Пользователь не найден</b>', reply_markup=admin_keyboard())
                    del dp.awaiting_balance[user_id]
                    return
                
                bal_data['target_id'] = target_id
                bal_data['step'] = 'amount'
                
                await message.answer(
                    f'{tg_emoji("edit")} <b>Изменение баланса</b>\n\n'
                    f'Пользователь: <code>{target_id}</code>\n'
                    f'Текущий баланс: <b>{target_user.balance:.0f}₽</b>\n\n'
                    'Отправьте сумму:\n'
                    '<code>+100</code> — пополнить\n'
                    '<code>-50</code> — списать\n'
                    '<code>500</code> — установить'
                )
            except ValueError:
                await message.answer(f'{tg_emoji("cross")} <b>Введите корректный ID</b>')
        
        elif step == 'amount':
            try:
                value = text
                target_id = bal_data['target_id']
                
                async with async_session() as session:
                    target_user = await session.execute(select(User).where(User.telegram_id == target_id))
                    target_user = target_user.scalar_one_or_none()
                    
                    if not target_user:
                        del dp.awaiting_balance[user_id]
                        await message.answer(f'{tg_emoji("cross")} <b>Пользователь не найден</b>', reply_markup=admin_keyboard())
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
                        f'{tg_emoji("check")} <b>Баланс изменен!</b>\n\n'
                        f'Пользователь: <code>{target_id}</code>\n'
                        f'Было: <b>{old_balance:.0f}₽</b>\n'
                        f'Стало: <b>{target_user.balance:.0f}₽</b>',
                        reply_markup=admin_keyboard()
                    )
            except ValueError:
                await message.answer(f'{tg_emoji("cross")} <b>Введите корректную сумму</b>')
        
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
            f'{tg_emoji("check")} <b>Рассылка завершена</b>\n\n'
            f'Отправлено: <b>{sent}</b> из <b>{len(users)}</b> пользователей',
            reply_markup=admin_keyboard()
        )
        return
    
    # Обычное сообщение
    await message.answer(
        f'{tg_emoji("info")} <b>Используйте кнопки меню для навигации</b>',
        reply_markup=main_menu_keyboard()
    )

# ===== ЗАПУСК =====
async def setup_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")

async def main():
    await setup_db()
    await run_migrations()
    
    # Инициализация хранилищ
    for attr in ['pending_accounts', 'awaiting_deposit', 'awaiting_accounts', 'awaiting_balance']:
        if not hasattr(dp, attr):
            setattr(dp, attr, {})
    if not hasattr(dp, 'awaiting_broadcast'):
        dp.awaiting_broadcast = set()
    if not hasattr(dp, 'awaiting_delete_account'):
        dp.awaiting_delete_account = set()
    
    dp.include_router(router)
    
    logger.info("=" * 50)
    logger.info("Vest Account Bot started!")
    logger.info(f"Admins: {ADMIN_IDS}")
    logger.info("=" * 50)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
