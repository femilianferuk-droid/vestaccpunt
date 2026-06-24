import asyncio
import logging
import os
import re
import sys
import io
import json
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
    BufferedInputFile,
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

# Реквизиты СБП
SBP_PHONE = "+79818376180"
SBP_BANK = "ЮMoney"
SBP_RECEIVER = "Иван Б"

# Токены и ключи
CRYPTO_BOT_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_IDS = [7973988177]

# Минимальное количество аккаунтов для уведомления админа
LOW_ACCOUNTS_THRESHOLD = 3

# ===== НАСТРОЙКИ МАРКЕТПЛЕЙСА =====
COMMISSION_PERCENT = 7.0                # комиссия платформы с продажи
HOLD_PERIOD_HOURS = 24                  # сколько часов деньги лежат в холде
HOLD_RELEASE_CHECK_INTERVAL = 300       # интервал проверки холдов (сек) - 5 минут
MIN_LISTING_PRICE = 10.0                # минимальная цена объявления
MAX_LISTING_PRICE = 50000.0             # максимальная цена объявления
LISTINGS_PAGE_SIZE = 5                  # объявлений на одной странице
COMMISSION_ACCOUNT_ID = None            # куда капает комиссия (None = общий фонд платформы)

# Цены по умолчанию (6 стран)
DEFAULT_PRICES = {
    "США": 25.0,
    "Россия": 150.0,
    "Индия": 25.0,
    "Германия": 65.0,
    "Бразилия": 50.0,
    "Индонезия": 30.0,
    "Казахстан": 120.0,
    "Украина": 130.0,
    "Беларусь": 130.0,
    "Вьетнам": 40.0,
    "Филиппины": 30.0,
    "Мьянма": 30.0,
}

# Коды стран для определения по номеру телефона
COUNTRY_CODES = {
    "1": "США",
    "7": "Россия",
    "91": "Индия",
    "49": "Германия",
    "55": "Бразилия",
    "62": "Индонезия",
    "77": "Казахстан",  # +7 7xx
    "380": "Украина",
    "375": "Беларусь",
    "84": "Вьетнам",
    "63": "Филиппины",
    "95": "Мьянма",
}

# Флаги стран
COUNTRY_FLAGS = {
    "США": "🇺🇸", "Россия": "🇷🇺", "Индия": "🇮🇳",
    "Германия": "🇩🇪", "Бразилия": "🇧🇷", "Индонезия": "🇮🇩",
    "Казахстан": "🇰🇿", "Украина": "🇺🇦", "Беларусь": "🇧🇾",
    "Вьетнам": "🇻🇳", "Филиппины": "🇵🇭", "Мьянма": "🇲🇲",
}

COUNTRY_NAMES = [
    "США", "Россия", "Индия", "Германия", "Бразилия", "Индонезия",
    "Казахстан", "Украина", "Беларусь", "Вьетнам", "Филиппины", "Мьянма",
]

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ===== БАЗА ДАННЫХ =====
class Base(DeclarativeBase):
    """Базовый класс для всех моделей"""
    pass

class User(Base):
    """Пользователь бота"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Float, default=0.0)
    hold_balance = Column(Float, default=0.0)  # деньги в холде (от проданных объявлений)
    total_spent = Column(Float, default=0.0)
    total_earned = Column(Float, default=0.0)  # сколько всего заработал продаж
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Account(Base):
    """Аккаунт для продажи"""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    country = Column(String(50), default="США")
    session_string = Column(Text, nullable=True)
    session_json = Column(Text, nullable=True)
    is_sold = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    price = Column(Float, default=20.0)
    created_at = Column(DateTime, default=datetime.utcnow)

class Purchase(Base):
    """Покупка аккаунта"""
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    account_id = Column(Integer, nullable=False)
    listing_id = Column(Integer, nullable=True)  # ID объявления (P2P)
    amount = Column(Float, nullable=False)
    payment_method = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)


class Listing(Base):
    """Объявление о продаже аккаунта (P2P маркетплейс)"""
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True)
    seller_id = Column(BigInteger, nullable=False)  # telegram_id продавца
    account_id = Column(Integer, nullable=False)   # ID аккаунта (session, phone и т.п.)
    title = Column(String(255), nullable=False)     # название объявления
    description = Column(Text, default="")          # описание
    price = Column(Float, nullable=False)           # цена от продавца
    status = Column(String(20), default="active")   # active / sold / cancelled
    buyer_id = Column(BigInteger, nullable=True)     # telegram_id покупателя
    created_at = Column(DateTime, default=datetime.utcnow)
    sold_at = Column(DateTime, nullable=True)


class Hold(Base):
    """Холд средств продавца после продажи.
    Деньги лежат 1 день, потом зачисляются на баланс продавца за вычетом 7% комиссии."""
    __tablename__ = "holds"
    id = Column(Integer, primary_key=True)
    seller_id = Column(BigInteger, nullable=False)
    listing_id = Column(Integer, nullable=False)
    purchase_id = Column(Integer, nullable=False)
    gross_amount = Column(Float, nullable=False)     # сумма продажи
    commission = Column(Float, nullable=False)       # 7% комиссия
    net_amount = Column(Float, nullable=False)       # сколько получит продавец (93%)
    status = Column(String(20), default="hold")      # hold / released / cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    release_at = Column(DateTime, nullable=False)    # когда отпустить деньги
    released_at = Column(DateTime, nullable=True)

class Payment(Base):
    """Платеж (пополнение или покупка)"""
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
    """Настройки медиа для разделов бота"""
    __tablename__ = "media_settings"
    id = Column(Integer, primary_key=True)
    section = Column(String(50), unique=True, nullable=False)
    file_id = Column(String(255), nullable=False)
    file_type = Column(String(20), default="photo")
    caption = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

class PriceSettings(Base):
    """Настройки цен по странам"""
    __tablename__ = "price_settings"
    id = Column(Integer, primary_key=True)
    country = Column(String(50), unique=True, nullable=False)
    price = Column(Float, default=20.0)
    updated_at = Column(DateTime, default=datetime.utcnow)

class PromoCode(Base):
    """Промокоды"""
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    amount = Column(Float, default=0.0)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PromoUsage(Base):
    """Использование промокодов"""
    __tablename__ = "promo_usages"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    promo_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class RequiredChannel(Base):
    """Обязательные каналы для подписки"""
    __tablename__ = "required_channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(255), nullable=False)
    channel_url = Column(String(255), nullable=False)
    channel_name = Column(String(255), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

# ===== FSM (Finite State Machine) =====
class MediaStates(StatesGroup):
    """Состояния для загрузки медиа"""
    waiting_for_media = State()

class SBPStates(StatesGroup):
    """Состояния для СБП"""
    waiting_for_screenshot = State()

class PriceStates(StatesGroup):
    """Состояния для изменения цен"""
    waiting_for_price = State()

class PromoStates(StatesGroup):
    """Состояния для промокодов"""
    waiting_for_promo_data = State()
    waiting_for_promo_code = State()

class ChannelStates(StatesGroup):
    """Состояния для добавления каналов"""
    waiting_for_channel = State()

class SessionFileStates(StatesGroup):
    """Состояния для загрузки .session файла"""
    waiting_for_session_file = State()

class SellStates(StatesGroup):
    """Состояния для создания объявления о продаже (P2P)"""
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_session = State()
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class ListingBrowseStates(StatesGroup):
    """Состояния для просмотра списка объявлений"""
    browsing = State()

# ===== ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ =====
try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Failed to create database engine: {e}")
    sys.exit(1)

# ===== АВТОМАТИЧЕСКИЕ МИГРАЦИИ =====
async def run_migrations():
    """Добавляет недостающие колонки в существующие таблицы"""
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent FLOAT DEFAULT 0.0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS hold_balance FLOAT DEFAULT 0.0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_earned FLOAT DEFAULT 0.0",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS session_string TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS session_json TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS seller_id BIGINT",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS listing_id INTEGER",
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

# ===== ИНИЦИАЛИЗАЦИЯ БОТА =====
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
    'json': '6035128606563241721',
    'session': '5870528606328852614',
    'auto': '5345906554510012647',
    'phone': '5870994129244131212',
    'search': '5345906554510012647',
    'alert': '5870657884844462243',
    'country': '6042011682497106307',
    'price': '5904462880941545555',
}

EMOJI_CHARS = {
    'bot': '🤖', 'lock': '🔒', 'loading': '🔄', 'check': '✅',
    'cross': '❌', 'home': '🏘️', 'profile': '👤', 'wallet': '💰',
    'money': '💵', 'crypto': '🪙', 'star': '⭐', 'location': '📍',
    'box': '📦', 'tag': '🏷️', 'code': '🔐', 'stats': '📊',
    'broadcast': '📣', 'add': '➕', 'back': '◀️', 'clock': '⏰',
    'buy': '🛒', 'info': 'ℹ️', 'edit': '✏️', 'media': '🖼️',
    'sbp': '💳', 'photo': '📸', 'bank': '🏦', 'settings': '⚙️',
    'gift': '🎁', 'users': '👥', 'delete': '🗑️', 'subscribe': '🔔',
    'promo': '🎟️', 'file': '📁', 'download': '⬇️', 'key': '🔑',
    'channel': '📢', 'accept': '✅', 'reject': '❌', 'json': '📋',
    'session': '📁', 'auto': '🤖', 'phone': '📱',
    'search': '🔍', 'alert': '⚠️', 'country': '🌍',
    'price': '💲',
    'sell': '💼', 'market': '🛍️', 'hold': '🔒',
    'payout': '💸', 'list': '📋', 'next': '➡️',
    'prev': '⬅️', 'seller': '👨',
    'description': '📝', 'title': '🏷️', 'question': '❓',
}

def emoji(name: str) -> str:
    """Возвращает HTML-тег премиум эмодзи"""
    eid = PREMIUM_EMOJI_IDS.get(name, PREMIUM_EMOJI_IDS['info'])
    char = EMOJI_CHARS.get(name, '📌')
    return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'

_LEADING_EMOJI_RE = re.compile(
    r"^\s*(?:"
    r"[\U0001F1E6-\U0001F1FF]{2}"
    r"|"
    r"[0-9*#]\uFE0F\u20E3"
    r"|"
    r"[\U0001F000-\U0001FFFF"
    r"\u2600-\u27BF"
    r"\u2300-\u23FF"
    r"\u2B00-\u2BFF"
    r"\u2100-\u21FF"
    r"\u25A0-\u25FF"
    r"\u2900-\u297F"
    r"\u2E80-\u2EFF"
    r"]"
    r"(?:[\uFE0F\u200D\u20E3](?:[\U0001F000-\U0001FFFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2100-\u21FF\u25A0-\u25FF])*)*"
    r")\s*",
    flags=re.UNICODE,
)


def create_button(
    text: str,
    callback_data: str = None,
    url: str = None,
    style: str = None,
    icon: str = None
) -> InlineKeyboardButton:
    """
    Создает цветную кнопку с премиум эмодзи в начале текста.

    Args:
        text: Текст кнопки (может начинаться с обычного эмодзи — он будет заменён на премиум)
        callback_data: Данные для callback
        url: Ссылка
        style: Стиль кнопки (primary, success, danger, default)
        icon: Ключ иконки из PREMIUM_EMOJI_IDS
    """
    if icon and icon in PREMIUM_EMOJI_IDS:
        # Убираем ведущий обычный эмодзи, чтобы не дублировать с премиум
        text = _LEADING_EMOJI_RE.sub('', text, count=1).lstrip()
        # Встраиваем премиум-эмодзи прямо в текст кнопки через <tg-emoji>
        text = f'{emoji(icon)} {text}'

    kwargs = {'text': text}
    if callback_data:
        kwargs['callback_data'] = callback_data
    if url:
        kwargs['url'] = url
    if style and style in ['primary', 'success', 'danger', 'default']:
        kwargs['style'] = style
    if icon and icon in PREMIUM_EMOJI_IDS:
        kwargs['icon_custom_emoji_id'] = PREMIUM_EMOJI_IDS[icon]
    return InlineKeyboardButton(**kwargs)

# ===== КЛАВИАТУРЫ =====

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота"""
    builder = InlineKeyboardBuilder()
    builder.row(
        create_button("Купить аккаунт", callback_data="buy_account", style="primary", icon="buy"),
        create_button("Продать аккаунт", callback_data="sell_account", style="success", icon="sell")
    )
    builder.row(
        create_button("Мои покупки", callback_data="my_purchases", style="default", icon="box"),
        create_button("Мои продажи", callback_data="my_sales", style="default", icon="market")
    )
    builder.row(
        create_button("Профиль", callback_data="profile", style="default", icon="profile"),
        create_button("Пополнить", callback_data="deposit_balance", style="success", icon="wallet")
    )
    builder.row(
        create_button("Поддержка", url="https://t.me/VestGameSupport", style="danger", icon="subscribe")
    )
    return builder.as_markup()


async def countries_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора страны с актуальными ценами"""
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()

    # Стили для 12 стран
    styles = [
        "primary", "primary", "primary", "primary",
        "default", "default", "default", "default",
        "default", "default", "default", "default",
    ]

    for i, country in enumerate(COUNTRY_NAMES):
        price = prices.get(country, DEFAULT_PRICES.get(country, 20))
        flag = COUNTRY_FLAGS.get(country, "")

        builder.row(
            create_button(
                f"{flag} {country} • {price:.0f}₽",
                callback_data=f"country_{country}",
                style=styles[i],
                icon="location"
            )
        )

    builder.row(create_button("Назад", callback_data="main_menu", style="default", icon="back"))
    return builder.as_markup()


def account_found_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура после нахождения аккаунта"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("КУПИТЬ", callback_data="show_payment_methods", style="success", icon="buy"))
    builder.row(create_button("Назад", callback_data="buy_account", style="default", icon="back"))
    return builder.as_markup()


def payment_methods_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Баланс бота", callback_data="pay_balance", style="primary", icon="wallet"))
    builder.row(create_button("СБП", callback_data="pay_sbp", style="default", icon="sbp"))
    builder.row(create_button("Crypto Bot", callback_data="pay_crypto", style="success", icon="crypto"))
    builder.row(create_button("Telegram Stars", callback_data="pay_stars", style="default", icon="star"))
    builder.row(create_button("Назад", callback_data="buy_account", style="default", icon="back"))
    return builder.as_markup()


def check_crypto_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    """Клавиатура проверки Crypto Bot оплаты"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Проверить оплату", callback_data=f"check_purchase_crypto_{payment_id}", style="primary", icon="loading"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return builder.as_markup()


def get_code_keyboard(purchase_id: int) -> InlineKeyboardMarkup:
    """Клавиатура получения данных после покупки"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Получить код", callback_data=f"get_code_{purchase_id}", style="primary", icon="code"))
    builder.row(create_button("Получить .session", callback_data=f"get_session_{purchase_id}", style="default", icon="file"))
    builder.row(create_button("Получить JSON", callback_data=f"get_json_{purchase_id}", style="default", icon="json"))
    builder.row(create_button("К покупкам", callback_data="my_purchases", style="default", icon="box"))
    return builder.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура профиля"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet"))
    builder.row(create_button("Мои покупки", callback_data="my_purchases", style="default", icon="box"))
    builder.row(create_button("Промокод", callback_data="activate_promo", style="primary", icon="promo"))
    builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))
    return builder.as_markup()


def deposit_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура пополнения баланса"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("СБП", callback_data="deposit_sbp", style="default", icon="sbp"))
    builder.row(create_button("Crypto Bot", callback_data="deposit_crypto", style="success", icon="crypto"))
    builder.row(create_button("Назад", callback_data="profile", style="default", icon="back"))
    return builder.as_markup()


def deposit_crypto_check_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    """Клавиатура проверки пополнения через Crypto Bot"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Проверить оплату", callback_data=f"check_deposit_crypto_{payment_id}", style="primary", icon="loading"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return builder.as_markup()


def sbp_payment_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    """Клавиатура СБП оплаты"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Я оплатил", callback_data=f"sbp_paid_{payment_id}", style="success", icon="check"))
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура админ-панели"""
    builder = InlineKeyboardBuilder()
    buttons = [
        ("Статистика", "admin_stats", "primary", "stats"),
        ("Пользователи", "admin_users", "default", "users"),
        ("Аккаунты", "admin_accounts_list", "default", "box"),
        ("Рассылка", "admin_broadcast", "default", "broadcast"),
        ("Добавить аккаунт (код)", "admin_add_accounts", "success", "add"),
        ("Добавить .session", "admin_add_session", "success", "session"),
        ("Управление балансом", "admin_balance", "default", "edit"),
        ("Цены на аккаунты", "admin_prices", "default", "money"),
        ("Промокоды", "admin_promo_menu", "default", "promo"),
        ("Управление медиа", "admin_media_menu", "default", "media"),
        ("Обязательные каналы", "admin_channels_menu", "default", "channel"),
        ("Проверка СБП", "admin_sbp_check", "success", "sbp"),
    ]
    for text, callback_data, style, icon in buttons:
        builder.row(create_button(text, callback_data=callback_data, style=style, icon=icon))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    return builder.as_markup()


def promo_admin_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления промокодами"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Создать промокод", callback_data="promo_create", style="success", icon="add"))
    builder.row(create_button("Список промокодов", callback_data="promo_list", style="default", icon="promo"))
    builder.row(create_button("Удалить промокод", callback_data="promo_delete_menu", style="danger", icon="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
    return builder.as_markup()


async def price_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настройки цен"""
    builder = InlineKeyboardBuilder()
    prices = await get_all_prices()

    for country in COUNTRY_NAMES:
        price = prices.get(country, DEFAULT_PRICES.get(country, 20))
        flag = COUNTRY_FLAGS.get(country, "")

        builder.row(
            create_button(
                f"{flag} {country}: {price:.0f}₽",
                callback_data=f"set_price_{country}",
                style="default",
                icon="edit"
            )
        )

    builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
    return builder.as_markup()


def media_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления медиа"""
    builder = InlineKeyboardBuilder()
    sections = [
        ("Главное меню", "main_menu"),
        ("Покупка аккаунта", "buy_account"),
        ("Способы оплаты", "payment_methods"),
        ("Профиль", "profile"),
        ("Мои покупки", "my_purchases"),
        ("Пополнение баланса", "deposit"),
    ]
    for name, callback_data in sections:
        builder.row(create_button(name, callback_data=f"set_media_{callback_data}", style="default", icon="media"))
    builder.row(create_button("Удалить все медиа", callback_data="admin_clear_media", style="danger", icon="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
    return builder.as_markup()


def channels_admin_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления обязательными каналами"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Добавить канал", callback_data="channel_add", style="success", icon="add"))
    builder.row(create_button("Список каналов", callback_data="channel_list", style="default", icon="channel"))
    builder.row(create_button("Удалить канал", callback_data="channel_delete", style="danger", icon="delete"))
    builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
    return builder.as_markup()


def sbp_approve_keyboard(payment_id: str, user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура одобрения/отклонения СБП платежа"""
    builder = InlineKeyboardBuilder()
    builder.row(
        create_button("Одобрить", callback_data=f"sbp_approve_{payment_id}_{user_id}", style="success", icon="accept"),
        create_button("Отклонить", callback_data=f"sbp_reject_{payment_id}_{user_id}", style="danger", icon="reject")
    )
    return builder.as_markup()


# ===== КЛАВИАТУРЫ МАРКЕТПЛЕЙСА =====

def sell_start_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура старта продажи (выбор способа загрузки аккаунта)"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("📁 Загрузить .session файл", callback_data="sell_session", style="primary", icon="session"))
    builder.row(create_button("📱 Через код подтверждения", callback_data="sell_phone", style="default", icon="phone"))
    builder.row(create_button("◀️ Назад", callback_data="main_menu", style="danger", icon="back"))
    return builder.as_markup()


def sell_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение создания объявления"""
    builder = InlineKeyboardBuilder()
    builder.row(create_button("✅ Опубликовать", callback_data="sell_publish", style="success", icon="check"))
    builder.row(create_button("✏️ Изменить название", callback_data="sell_edit_title", style="default", icon="edit"))
    builder.row(create_button("✏️ Изменить описание", callback_data="sell_edit_description", style="default", icon="edit"))
    builder.row(create_button("✏️ Изменить цену", callback_data="sell_edit_price", style="default", icon="edit"))
    builder.row(create_button("❌ Отменить", callback_data="sell_cancel", style="danger", icon="cross"))
    return builder.as_markup()


def listings_keyboard(listings: list, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """
    Клавиатура списка объявлений на маркетплейсе.
    listings - список объектов Listing.
    """
    builder = InlineKeyboardBuilder()
    for listing in listings:
        # Кнопка-объявление: эмодзи "📦" + название + цена
        title = listing.title[:32] + ("..." if len(listing.title) > 32 else "")
        builder.row(
            create_button(
                f"📦 {title} - {listing.price:.0f}₽",
                callback_data=f"listing_view_{listing.id}",
                style="primary",
                icon="box"
            )
        )
    # Пагинация
    nav = []
    if page > 0:
        nav.append(create_button("◀️ Назад", callback_data=f"listings_page_{page-1}", style="default", icon="prev"))
    if has_next:
        nav.append(create_button("Вперёд ▶️", callback_data=f"listings_page_{page+1}", style="default", icon="next"))
    if nav:
        builder.row(*nav)
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    return builder.as_markup()


def listing_detail_keyboard(listing_id: int, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура детального просмотра объявления"""
    builder = InlineKeyboardBuilder()
    if not is_owner:
        builder.row(create_button("🛒 КУПИТЬ", callback_data=f"listing_buy_{listing_id}", style="success", icon="buy"))
    else:
        builder.row(create_button("📊 Моё объявление", callback_data=f"listing_manage_{listing_id}", style="default", icon="info"))
    builder.row(create_button("◀️ К списку", callback_data="listings_page_0", style="default", icon="back"))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    return builder.as_markup()


def my_listings_keyboard(listings: list) -> InlineKeyboardMarkup:
    """Клавиатура со списком моих объявлений (для продавца)"""
    builder = InlineKeyboardBuilder()
    for listing in listings:
        status_icon = "🟢" if listing.status == "active" else ("🔴" if listing.status == "sold" else "⚪️")
        title = listing.title[:30] + ("..." if len(listing.title) > 30 else "")
        builder.row(
            create_button(
                f"{status_icon} {title} - {listing.price:.0f}₽",
                callback_data=f"my_listing_{listing.id}",
                style="default",
                icon="box"
            )
        )
    builder.row(create_button("➕ Новое объявление", callback_data="sell_account", style="success", icon="add"))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    return builder.as_markup()


def my_listing_manage_keyboard(listing_id: int, status: str) -> InlineKeyboardMarkup:
    """Управление конкретным своим объявлением"""
    builder = InlineKeyboardBuilder()
    if status == "active":
        builder.row(create_button("❌ Снять с продажи", callback_data=f"my_listing_cancel_{listing_id}", style="danger", icon="cross"))
    builder.row(create_button("◀️ К моим объявлениям", callback_data="my_sales", style="default", icon="back"))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    return builder.as_markup()


# ===== ПРОВЕРКА ПОДПИСКИ =====

async def check_subscription(user_id: int) -> tuple:
    """
    Проверяет подписку пользователя на все обязательные каналы.
    Возвращает (bool, list) - (все_подписаны, список_неподписанных)
    """
    async with async_session() as session:
        result = await session.execute(select(RequiredChannel))
        channels = result.scalars().all()

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
    """Клавиатура с кнопками для подписки на каналы"""
    builder = InlineKeyboardBuilder()
    for channel in not_subscribed:
        builder.row(create_button(
            f"📢 {channel.channel_name or 'Канал'}",
            url=channel.channel_url,
            style="primary",
            icon="subscribe"
        ))
    builder.row(create_button(
        "Проверить подписку",
        callback_data="check_subscription",
        style="success",
        icon="loading"
    ))
    return builder.as_markup()


async def require_subscription(callback: CallbackQuery) -> bool:
    """Проверяет подписку и отправляет сообщение если не подписан"""
    subbed, ns = await check_subscription(callback.from_user.id)
    if not subbed:
        await callback.message.answer(
            f'{emoji("subscribe")} <b>Подпишитесь на каналы:</b>\n\nДля продолжения необходимо подписаться.',
            reply_markup=await get_subscribe_keyboard(ns)
        )
        return False
    return True


# ===== ОПРЕДЕЛЕНИЕ СТРАНЫ =====

def detect_country(phone: str) -> str:
    """Определяет страну по номеру телефона"""
    phone = phone.strip().lstrip('+')

    # Сначала проверяем Казахстан (+77...)
    if phone.startswith("77"):
        return "Казахстан"
    # Потом Россию (+7...) - все остальные номера на +7
    if phone.startswith("7"):
        return "Россия"

    # Остальные страны по коду
    for code in sorted(COUNTRY_CODES.keys(), key=len, reverse=True):
        if phone.startswith(code):
            return COUNTRY_CODES[code]

    return "США"


# ===== РАБОТА С ЦЕНАМИ =====

async def get_country_price(country: str) -> float:
    """Получает цену для страны из БД или возвращает default"""
    async with async_session() as session:
        result = await session.execute(
            select(PriceSettings).where(PriceSettings.country == country)
        )
        price_setting = result.scalar_one_or_none()
        if price_setting:
            return price_setting.price
    return DEFAULT_PRICES.get(country, 20.0)


async def set_country_price(country: str, price: float):
    """Устанавливает цену для страны"""
    async with async_session() as session:
        result = await session.execute(
            select(PriceSettings).where(PriceSettings.country == country)
        )
        price_setting = result.scalar_one_or_none()
        if price_setting:
            price_setting.price = price
            price_setting.updated_at = datetime.utcnow()
        else:
            session.add(PriceSettings(country=country, price=price))
        await session.commit()


async def get_all_prices() -> dict:
    """Возвращает словарь со всеми ценами"""
    prices = dict(DEFAULT_PRICES)
    async with async_session() as session:
        result = await session.execute(select(PriceSettings))
        for ps in result.scalars().all():
            prices[ps.country] = ps.price
    return prices


# ===== УВЕДОМЛЕНИЯ АДМИНУ О НЕХВАТКЕ АККАУНТОВ =====

async def check_low_accounts_and_notify():
    """
    Проверяет количество доступных аккаунтов по всем странам.
    Если каких-то аккаунтов меньше LOW_ACCOUNTS_THRESHOLD - уведомляет админов.
    """
    try:
        async with async_session() as session:
            # Получаем статистику по всем странам
            result = await session.execute(
                select(Account.country, func.count(Account.id))
                .where(Account.is_sold == False, Account.is_verified == True)
                .group_by(Account.country)
            )
            stats = result.all()

            # Формируем словарь для быстрого поиска
            stats_dict = {country: count for country, count in stats}

            # Проверяем все страны
            low_categories = []

            for country in COUNTRY_NAMES:
                count = stats_dict.get(country, 0)

                if count <= LOW_ACCOUNTS_THRESHOLD:
                    flag = COUNTRY_FLAGS.get(country, "")
                    low_categories.append(
                        f"{flag} <b>{country}</b>: <b>{count} шт.</b>"
                    )

            # Если есть страны с низким количеством - отправляем уведомление
            if low_categories:
                alert_text = (
                    f'{emoji("alert")} <b>⚠️ ВНИМАНИЕ! Заканчиваются аккаунты!</b>\n\n'
                    f'Следующие страны имеют ≤{LOW_ACCOUNTS_THRESHOLD} аккаунтов:\n\n'
                    + '\n'.join(low_categories) +
                    f'\n\n{emoji("add")} <i>Добавьте новые аккаунты в админ-панели</i>\n'
                    f'{emoji("clock")} <i>Проверка происходит при каждой покупке</i>'
                )

                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, alert_text)
                        logger.info(f"Low accounts alert sent to admin {admin_id}")
                    except Exception as e:
                        logger.error(f"Failed to send low accounts alert to admin {admin_id}: {e}")

                logger.info(f"Low accounts alert: {len(low_categories)} countries have low stock")

    except Exception as e:
        logger.error(f"Error in check_low_accounts_and_notify: {e}")


# ===== РЕЛИЗ ХОЛДОВ (МАРКЕТПЛЕЙС) =====

async def release_due_holds() -> dict:
    """
    Находит все Hold в статусе 'hold' у которых release_at <= now.
    Для каждого:
      - снимает деньги с hold_balance продавца
      - зачисляет net_amount на основной баланс
      - увеличивает total_earned
      - помечает Hold как 'released'
    Возвращает статистику: {released: int, total: float}
    """
    now = datetime.utcnow()
    stats = {"released": 0, "total_net": 0.0, "total_commission": 0.0}

    try:
        async with async_session() as session:
            result = await session.execute(
                select(Hold).where(
                    Hold.status == "hold",
                    Hold.release_at <= now
                )
            )
            holds = result.scalars().all()

            for hold in holds:
                # Получаем продавца
                result = await session.execute(
                    select(User).where(User.telegram_id == hold.seller_id)
                )
                seller = result.scalar_one_or_none()
                if not seller:
                    logger.error(f"release_due_holds: seller {hold.seller_id} not found")
                    continue

                # Снимаем с холда и кладём на баланс
                if (seller.hold_balance or 0) < hold.net_amount:
                    # На случай рассинхрона - фиксируем по факту
                    seller.hold_balance = max(0, (seller.hold_balance or 0) - hold.net_amount)
                else:
                    seller.hold_balance = (seller.hold_balance or 0) - hold.net_amount

                seller.balance = (seller.balance or 0) + hold.net_amount
                seller.total_earned = (seller.total_earned or 0) + hold.net_amount

                hold.status = "released"
                hold.released_at = now

                stats["released"] += 1
                stats["total_net"] += hold.net_amount
                stats["total_commission"] += hold.commission

                # Уведомляем продавца
                try:
                    text = (
                        f'{emoji("payout")} <b>Деньги зачислены на баланс!</b>\n\n'
                        f'{emoji("money")} Сумма: <b>{hold.net_amount:.0f}₽</b>\n'
                        f'{emoji("info")} (комиссия {hold.commission:.0f}₽ удержана)\n'
                        f'{emoji("wallet")} Текущий баланс: <b>{seller.balance:.0f}₽</b>'
                    )
                    await bot.send_message(hold.seller_id, text)
                except Exception as e:
                    logger.error(f"Failed to notify seller about hold release: {e}")

            await session.commit()
    except Exception as e:
        logger.error(f"release_due_holds error: {e}")

    return stats


async def hold_releaser_loop():
    """Фоновый цикл: раз в HOLD_RELEASE_CHECK_INTERVAL секунд релизит холды"""
    logger.info(f"hold_releaser_loop started (interval={HOLD_RELEASE_CHECK_INTERVAL}s)")
    while True:
        try:
            stats = await release_due_holds()
            if stats["released"]:
                logger.info(
                    f"hold_releaser: released {stats['released']} holds, "
                    f"net={stats['total_net']:.2f}, commission={stats['total_commission']:.2f}"
                )
        except Exception as e:
            logger.error(f"hold_releaser_loop error: {e}")
        await asyncio.sleep(HOLD_RELEASE_CHECK_INTERVAL)


# ===== РАБОТА С МЕДИА =====

async def get_media(section: str) -> Optional[MediaSettings]:
    """Получает настройки медиа для раздела"""
    async with async_session() as session:
        result = await session.execute(
            select(MediaSettings).where(MediaSettings.section == section)
        )
        return result.scalar_one_or_none()


async def set_media(section: str, file_id: str, file_type: str, caption: str = None):
    """Сохраняет медиа для раздела"""
    async with async_session() as session:
        result = await session.execute(
            select(MediaSettings).where(MediaSettings.section == section)
        )
        media = result.scalar_one_or_none()
        if media:
            media.file_id = file_id
            media.file_type = file_type
            media.caption = caption
            media.updated_at = datetime.utcnow()
        else:
            session.add(MediaSettings(
                section=section,
                file_id=file_id,
                file_type=file_type,
                caption=caption
            ))
        await session.commit()


async def send_media_message(target, section: str, text: str, markup: InlineKeyboardMarkup):
    """Отправляет сообщение с медиа если оно настроено для раздела"""
    media = await get_media(section)
    msg = target.message if isinstance(target, CallbackQuery) else target

    if media:
        caption = f"{text}\n\n{media.caption}" if media.caption else text
        try:
            if media.file_type == "photo":
                await msg.answer_photo(media.file_id, caption=caption, reply_markup=markup)
            elif media.file_type == "video":
                await msg.answer_video(media.file_id, caption=caption, reply_markup=markup)
            elif media.file_type == "animation":
                await msg.answer_animation(media.file_id, caption=caption, reply_markup=markup)
            else:
                await msg.answer(text, reply_markup=markup)
        except Exception as e:
            logger.error(f"Error sending media for section {section}: {e}")
            await msg.answer(text, reply_markup=markup)
    else:
        await msg.answer(text, reply_markup=markup)


_TG_EMOJI_TAG_RE = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", flags=re.DOTALL)


def strip_tg_emoji_tags(text: str) -> str:
    """Убирает <tg-emoji>...</tg-emoji> из текста, оставляя fallback-символ."""
    def _repl(m: re.Match) -> str:
        inner = re.sub(r"</?tg-emoji[^>]*>", "", m.group(0))
        return inner
    return _TG_EMOJI_TAG_RE.sub(_repl, text)


async def safe_answer(target, text: str, reply_markup=None, **kwargs):
    """
    Безопасная отправка текстового сообщения.
    Если Telegram отвергает HTML (ENTITY_TEXT_INVALID и подобные),
    автоматически убирает <tg-emoji> теги и пробует снова.
    """
    msg = target.message if isinstance(target, CallbackQuery) else target
    try:
        return await msg.answer(text, reply_markup=reply_markup, **kwargs)
    except Exception as e:
        err_text = str(e)
        if "ENTITY_TEXT_INVALID" in err_text or "can't parse entities" in err_text.lower():
            logger.warning(f"safe_answer: entities rejected, falling back to plain text: {e}")
            fallback = strip_tg_emoji_tags(text)
            try:
                return await msg.answer(
                    fallback,
                    reply_markup=reply_markup,
                    **kwargs,
                )
            except TypeError:
                # если kwargs содержат parse_mode — снимаем
                kwargs.pop("parse_mode", None)
                return await msg.answer(
                    fallback,
                    reply_markup=reply_markup,
                    **kwargs,
                )
        raise


# ===== TELETHON ФУНКЦИИ =====

async def create_telethon_client(session_string: str = None) -> TelegramClient:
    """Создает клиент Telethon"""
    return TelegramClient(
        StringSession(session_string) if session_string else StringSession(),
        API_ID,
        API_HASH
    )


async def verify_session_file(file_content: bytes) -> dict:
    """
    Проверяет .session файл и возвращает данные аккаунта.
    Пробует разные форматы декодирования сессии.
    """
    client = None
    try:
        # Пробуем разные форматы декодирования
        try:
            session_str = file_content.decode('utf-8').strip()
        except UnicodeDecodeError:
            try:
                session_str = file_content.decode('latin-1').strip()
            except:
                session_str = file_content.decode('utf-8', errors='replace').strip()

        # Убираем BOM если есть
        session_str = session_str.lstrip('\ufeff').strip()

        logger.info(f"Session string length: {len(session_str)}")

        if not session_str or len(session_str) < 5:
            return {
                'success': False,
                'error': 'Файл сессии пуст или слишком короткий. Проверьте файл.'
            }

        # Создаем клиент и подключаемся
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)

        try:
            await client.connect()
        except Exception as conn_err:
            logger.error(f"Connection error: {conn_err}")
            return {
                'success': False,
                'error': f'Ошибка подключения к Telegram: {str(conn_err)[:100]}'
            }

        # Проверяем авторизацию
        try:
            is_authorized = await client.is_user_authorized()
        except Exception as auth_err:
            logger.error(f"Auth check error: {auth_err}")
            await client.disconnect()
            return {
                'success': False,
                'error': f'Ошибка проверки авторизации: {str(auth_err)[:100]}'
            }

        if not is_authorized:
            await client.disconnect()
            return {
                'success': False,
                'error': 'Сессия не авторизована. Аккаунт разлогинен или файл поврежден.'
            }

        # Получаем информацию об аккаунте
        try:
            me = await client.get_me()
        except Exception as me_err:
            logger.error(f"Get me error: {me_err}")
            await client.disconnect()
            return {
                'success': False,
                'error': f'Не удалось получить данные аккаунта: {str(me_err)[:100]}'
            }

        phone = me.phone
        if not phone:
            await client.disconnect()
            return {
                'success': False,
                'error': 'Не удалось определить номер телефона в сессии.'
            }

        # Определяем страну
        country = detect_country(phone)

        # Сохраняем сессию
        session_string = client.session.save()

        # Создаем JSON с данными
        session_json = json.dumps({
            "phone": phone,
            "session_string": session_string,
            "api_id": API_ID,
            "api_hash": API_HASH,
            "user_id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or "",
            "created_at": datetime.utcnow().isoformat()
        }, ensure_ascii=False, indent=2)

        await client.disconnect()

        logger.info(f"Session verified successfully for {phone}, country: {country}")

        return {
            'success': True,
            'phone': phone,
            'country': country,
            'session_string': session_string,
            'session_json': session_json
        }

    except Exception as e:
        logger.error(f"Session file verification error: {e}")
        return {
            'success': False,
            'error': f'Критическая ошибка проверки сессии: {str(e)[:150]}'
        }
    finally:
        if client:
            try:
                await client.disconnect()
            except:
                pass


async def send_code_to_phone(phone: str) -> dict:
    """Отправляет код подтверждения на номер телефона"""
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
        return {
            'success': True,
            'phone_code_hash': sent.phone_code_hash
        }
    except Exception as e:
        logger.error(f"Error sending code to {phone}: {e}")
        return {
            'success': False,
            'error': str(e)
        }


async def verify_code_and_create_session_json(phone: str, code: str, phone_code_hash: str) -> dict:
    """Проверяет код и создает сессию + JSON"""
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {
                'success': False,
                'error': 'Сессия не найдена. Отправьте номер заново.'
            }

        client = auth_data['client']

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return {
                'success': False,
                'need_password': True,
                'error': 'Требуется 2FA пароль'
            }

        session_string = client.session.save()
        me = await client.get_me()

        # Создаем JSON
        session_json = json.dumps({
            "phone": phone,
            "session_string": session_string,
            "api_id": API_ID,
            "api_hash": API_HASH,
            "user_id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or "",
            "created_at": datetime.utcnow().isoformat()
        }, ensure_ascii=False, indent=2)

        await client.disconnect()
        pending_auth.pop(phone, None)

        logger.info(f"Session+JSON created for {phone}")
        return {
            'success': True,
            'session_string': session_string,
            'session_json': session_json
        }
    except PhoneCodeInvalidError:
        return {
            'success': False,
            'error': 'Неверный код. Проверьте и попробуйте снова.'
        }
    except PhoneCodeExpiredError:
        return {
            'success': False,
            'error': 'Код истек. Отправьте номер заново.'
        }
    except Exception as e:
        logger.error(f"Error verifying code for {phone}: {e}")
        return {
            'success': False,
            'error': f'Ошибка: {str(e)}'
        }


async def verify_2fa_and_create_session_json(phone: str, password: str) -> dict:
    """Подтверждает 2FA пароль и создает сессию + JSON"""
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {
                'success': False,
                'error': 'Сессия не найдена'
            }

        client = auth_data['client']
        await client.sign_in(password=password)

        session_string = client.session.save()
        me = await client.get_me()

        session_json = json.dumps({
            "phone": phone,
            "session_string": session_string,
            "api_id": API_ID,
            "api_hash": API_HASH,
            "user_id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or "",
            "created_at": datetime.utcnow().isoformat()
        }, ensure_ascii=False, indent=2)

        await client.disconnect()
        pending_auth.pop(phone, None)

        return {
            'success': True,
            'session_string': session_string,
            'session_json': session_json
        }
    except PasswordHashInvalidError:
        return {
            'success': False,
            'error': 'Неверный пароль 2FA'
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

async def get_code_from_session(session_string: str, phone: str = None) -> Optional[str]:
    """
    Поиск кода подтверждения в диалогах.
    При каждом вызове создаёт НОВОЕ подключение и читает свежие сообщения.
    """
    client = None
    try:
        logger.info(f"Creating NEW connection to search code for {phone or 'unknown'}...")

        # ВАЖНО: Создаём новый клиент с этой же сессией
        # StringSession каждый раз создаёт новое подключение
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Session not authorized")
            return None

        # ВАЖНО: Принудительно обновляем состояние диалогов
        # Это заставляет Telethon заново загрузить сообщения с сервера
        await client.get_dialogs(limit=1)

        code_keywords = [
            "telegram", "код", "code", "login", "verify", "подтверждени",
            "авторизаци", "вход", "42777", "служебны", "service",
            "верификаци", "verification"
        ]

        all_codes = []

        # Проходим по диалогам и принудительно читаем свежие сообщения
        async for dialog in client.iter_dialogs(limit=100):
            dialog_name = (dialog.name or "").lower()
            is_service = any(kw in dialog_name for kw in code_keywords)
            msg_limit = 50 if is_service else 10  # Увеличено для лучшего поиска

            try:
                # ВАЖНО: Используем limit=None чтобы читать ВСЕ сообщения, а не кэш
                messages = await client.get_messages(dialog, limit=msg_limit)
                for msg in messages:
                    if msg.text:
                        codes_5 = re.findall(r'(?<!\d)\d{5}(?!\d)', msg.text)
                        codes_login = re.findall(r'(?:login|code|код)\s*(?:code|код|:)?\s*(\d{5})', msg.text.lower())
                        codes_is = re.findall(r'(\d{5})\s*is\s*your', msg.text.lower())

                        for code in codes_5 + codes_login + codes_is:
                            code_str = str(code)
                            if len(code_str) == 5 and code_str.isdigit():
                                all_codes.append({
                                    'code': code_str,
                                    'dialog': dialog.name or 'Unknown',
                                    'date': msg.date,
                                    'is_service': is_service
                                })
            except Exception as e:
                logger.error(f"Error reading {dialog.name}: {e}")
                continue

        if not all_codes:
            logger.info("No codes found")
            return None

        # Сортируем: служебные чаты приоритетнее, новые сообщения первее
        all_codes.sort(key=lambda x: (not x['is_service'], x['date']), reverse=False)

        # Берём САМЫЙ НОВЫЙ код (последний по дате)
        best_code = all_codes[0]['code']
        logger.info(f"Returning FRESH code: {best_code} from {all_codes[0]['dialog']}, date={all_codes[0]['date']}")
        return best_code

    except Exception as e:
        logger.error(f"Code search error: {e}")
        return None
    finally:
        if client:
            try:
                await client.disconnect()
            except:
                pass


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

async def get_user(telegram_id: int) -> Optional[User]:
    """Получает пользователя по Telegram ID"""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def get_or_create_user(telegram_id: int, username: str = None) -> User:
    """Получает или создает пользователя"""
    user = await get_user(telegram_id)
    if not user:
        async with async_session() as session:
            user = User(
                telegram_id=telegram_id,
                username=username,
                is_admin=(telegram_id in ADMIN_IDS)
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info(f"New user created: {telegram_id}")
    return user


async def get_available_account(country: str = None) -> Optional[Account]:
    """Получает первый доступный для продажи аккаунт"""
    async with async_session() as session:
        query = select(Account).where(
            Account.is_sold == False,
            Account.is_verified == True,
            Account.session_string != None,
            Account.session_string != ""
        )
        if country:
            query = query.where(Account.country == country)

        result = await session.execute(query.limit(1))
        return result.scalar_one_or_none()


async def get_available_countries() -> list:
    """Возвращает список стран с количеством доступных аккаунтов"""
    async with async_session() as session:
        result = await session.execute(
            select(Account.country, func.count(Account.id))
            .where(Account.is_sold == False, Account.is_verified == True)
            .group_by(Account.country)
        )
        return [(row[0], row[1]) for row in result.all()]


async def create_crypto_bot_invoice(amount: float, payment_id: str) -> Optional[dict]:
    """Создает счет в Crypto Bot"""
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}

        # Конвертируем рубли в USDT (курс ~90)
        usdt_amount = round(amount / 90, 2)

        payload = {
            "asset": "USDT",
            "amount": str(usdt_amount),
            "description": f"Vest Account #{payment_id}",
            "payload": payment_id,
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=30) as response:
                result = await response.json()
                logger.info(f"Crypto Bot invoice created: {result.get('ok')}")
                return result
    except Exception as e:
        logger.error(f"Crypto Bot invoice creation error: {e}")
        return None


async def check_crypto_bot_invoice(invoice_id: int) -> Optional[dict]:
    """Проверяет статус счета в Crypto Bot"""
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


async def generate_payment_id() -> str:
    """Генерирует уникальный ID платежа"""
    timestamp = int(datetime.now().timestamp())
    random_bytes = os.urandom(4).hex()
    return f"vest_{timestamp}_{random_bytes}"


async def process_purchase(telegram_id: int, account_id: int, price: float, method: str) -> Optional[Purchase]:
    """Обрабатывает покупку: списывает средства и обновляет БД"""
    async with async_session() as session:
        # Ищем аккаунт
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account or account.is_sold:
            logger.error(f"Account {account_id} not available")
            return None

        # Ищем пользователя по telegram_id
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            logger.error(f"User {telegram_id} not found")
            return None

        # Списываем средства если оплата с баланса
        if method == "balance":
            if user.balance < price:
                return None
            user.balance -= price

        # Обновляем статистику
        user.total_spent = (user.total_spent or 0) + price

        # Отмечаем аккаунт как проданный
        account.is_sold = True

        # Создаем запись о покупке
        purchase = Purchase(
            user_id=telegram_id,
            account_id=account_id,
            amount=price,
            payment_method=method
        )
        session.add(purchase)
        await session.commit()
        await session.refresh(purchase)

        logger.info(f"Purchase processed: user={telegram_id}, account={account_id}, amount={price}, method={method}")

        # Проверяем количество оставшихся аккаунтов и уведомляем админов
        asyncio.create_task(check_low_accounts_and_notify())

        return purchase


# ===== АВТОМАТИЧЕСКАЯ ПРОВЕРКА CRYPTO BOT =====

async def auto_check_crypto_payment(payment_id: str, user_id: int, account_id: int = None, amount: float = 0):
    """
    Автоматически проверяет оплату Crypto Bot каждые 5 секунд в течение 10 минут.
    При нахождении оплаты:
    - Если тип покупка - выдает аккаунт
    - Если тип пополнение - зачисляет на баланс
    """
    logger.info(f"Starting auto-check for payment {payment_id}, user={user_id}")

    for attempt in range(120):  # 120 * 5 = 600 секунд = 10 минут
        await asyncio.sleep(5)

        try:
            invoice = await check_crypto_bot_invoice(int(payment_id))

            if invoice and invoice.get("status") == "paid":
                logger.info(f"Payment {payment_id} found! Status: paid")

                async with async_session() as session:
                    # Проверяем не обработан ли уже этот платеж
                    result = await session.execute(
                        select(Payment).where(Payment.payment_id == payment_id)
                    )
                    payment = result.scalar_one_or_none()

                    if payment and payment.status == "completed":
                        logger.info(f"Payment {payment_id} already processed")
                        return True

                    # Обновляем статус платежа
                    if payment:
                        payment.status = "completed"

                    if payment and payment.type == "deposit":
                        # Пополнение баланса - ищем пользователя по telegram_id
                        result = await session.execute(
                            select(User).where(User.telegram_id == user_id)
                        )
                        user = result.scalar_one_or_none()
                        if user:
                            deposit_amount = payment.amount
                            user.balance += deposit_amount
                            await session.commit()

                            try:
                                builder = InlineKeyboardBuilder()
                                builder.row(create_button("В меню", callback_data="main_menu", style="success", icon="home"))
                                await bot.send_message(
                                    user_id,
                                    f'{emoji("check")} <b>Баланс пополнен автоматически!</b>\n\n'
                                    f'{emoji("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n'
                                    f'{emoji("wallet")} Баланс: <b>{user.balance:.2f}₽</b>',
                                    reply_markup=builder.as_markup()
                                )
                                logger.info(f"Deposit notification sent to user {user_id}")
                            except Exception as e:
                                logger.error(f"Failed to notify user {user_id}: {e}")
                    else:
                        # Покупка аккаунта
                        if account_id:
                            purchase = await process_purchase(user_id, account_id, amount, "crypto")
                            if purchase:
                                result = await session.execute(
                                    select(Account).where(Account.id == account_id)
                                )
                                account = result.scalar_one_or_none()
                                await session.commit()

                                try:
                                    await bot.send_message(
                                        user_id,
                                        f'{emoji("check")} <b>Оплата подтверждена автоматически!</b>\n\n'
                                        f'{emoji("tag")} Номер: <code>{account.phone}</code>\n'
                                        f'{emoji("money")} Сумма: <b>{amount:.0f}₽</b>\n\n'
                                        'Нажмите для получения данных:',
                                        reply_markup=get_code_keyboard(purchase.id)
                                    )
                                    logger.info(f"Purchase notification sent to user {user_id}")
                                except Exception as e:
                                    logger.error(f"Failed to notify user {user_id}: {e}")

                    return True

            if attempt % 12 == 0 and attempt > 0:  # Каждую минуту логируем
                logger.info(f"Still waiting for payment {payment_id}, attempt {attempt}")

        except Exception as e:
            logger.error(f"Auto-check error for payment {payment_id}: {e}")

    logger.info(f"Payment {payment_id} not found after 10 minutes")
    return False


# ===== ОБРАБОТЧИКИ КОМАНД =====

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    await get_or_create_user(message.from_user.id, message.from_user.username)

    # Проверяем подписку
    subbed, ns = await check_subscription(message.from_user.id)
    if not subbed:
        await message.answer(
            f'{emoji("subscribe")} <b>Подпишитесь на каналы</b>\n\n'
            'Для использования бота необходима подписка на обязательные каналы.',
            reply_markup=await get_subscribe_keyboard(ns)
        )
        return

    # Приветственное сообщение
    welcome_text = (
        f'{emoji("bot")} <b>Vest Account</b>\n\n'
        f'{emoji("lock")} Покупка аккаунтов Telegram\n'
        f'{emoji("loading")} Быстро, безопасно, анонимно\n'
        f'{emoji("location")} 12 стран доступно\n\n'
        '<i>Выберите действие в меню ниже:</i>'
    )
    await send_media_message(message, "main_menu", welcome_text, main_menu_keyboard())


@router.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery):
    """Проверка подписки"""
    await callback.answer()
    subbed, ns = await check_subscription(callback.from_user.id)
    if subbed:
        await callback.message.answer(
            f'{emoji("check")} <b>Подписка проверена!</b>\n\nТеперь вы можете использовать бота.',
            reply_markup=main_menu_keyboard()
        )
    else:
        await callback.message.answer(
            f'{emoji("cross")} <b>Вы не подписаны на все каналы!</b>',
            reply_markup=await get_subscribe_keyboard(ns)
        )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Обработчик команды /admin - вход в админ-панель"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(f'{emoji("cross")} <b>Доступ запрещен</b>')
        return

    await message.answer(
        f'{emoji("stats")} <b>Админ-панель Vest Account</b>\n\n'
        f'{emoji("info")} Выберите раздел управления:',
        reply_markup=admin_keyboard()
    )


# ===== ОБРАБОТЧИКИ НАВИГАЦИИ =====

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()
    text = f'{emoji("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>'
    await send_media_message(callback, "main_menu", text, main_menu_keyboard())


@router.callback_query(F.data == "buy_account")
async def cb_buy_account(callback: CallbackQuery):
    """Покупка аккаунта - показывает список активных объявлений (P2P маркетплейс)"""
    await callback.answer()

    # Проверяем подписку
    if not await require_subscription(callback):
        return

    await show_listings_page(callback, page=0)


# ===== МАРКЕТПЛЕЙС: СПИСОК ОБЪЯВЛЕНИЙ, ДЕТАЛИ, ПОКУПКА =====

async def show_listings_page(callback: CallbackQuery, page: int = 0):
    """
    Показывает страницу списка активных объявлений.
    Шаг 1 покупки: юзер видит список и кликает по объявлению.
    """
    offset = page * LISTINGS_PAGE_SIZE

    async with async_session() as session:
        # Считаем общее число активных объявлений
        total = (await session.execute(
            select(func.count(Listing.id)).where(Listing.status == "active")
        )).scalar() or 0

        result = await session.execute(
            select(Listing)
            .where(Listing.status == "active")
            .order_by(Listing.created_at.desc())
            .offset(offset)
            .limit(LISTINGS_PAGE_SIZE)
        )
        listings = result.scalars().all()

    has_next = (offset + len(listings)) < total

    if total == 0:
        text = (
            f'{emoji("market")} <b>Маркетплейс аккаунтов</b>\n\n'
            f'{emoji("cross")} <b>Пока нет активных объявлений</b>\n\n'
            f'{emoji("sell")} Будь первым! Выстави свой аккаунт на продажу:\n'
            f'  Главное меню → <b>Продать аккаунт</b>'
        )
        builder = InlineKeyboardBuilder()
        builder.row(create_button("💼 Продать свой аккаунт", callback_data="sell_account", style="success", icon="sell"))
        builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
        await send_media_message(callback, "buy_account", text, builder.as_markup())
        return

    text = (
        f'{emoji("market")} <b>Маркетплейс аккаунтов</b>\n\n'
        f'{emoji("box")} Активных объявлений: <b>{total}</b>\n'
        f'{emoji("info")} Страница <b>{page+1}</b> из <b>{(total + LISTINGS_PAGE_SIZE - 1) // LISTINGS_PAGE_SIZE}</b>\n\n'
        f'{emoji("buy")} <i>Нажмите на объявление, чтобы посмотреть детали и купить</i>'
    )
    await send_media_message(callback, "buy_account", text, listings_keyboard(listings, page, has_next))


@router.callback_query(F.data.startswith("listings_page_"))
async def cb_listings_page(callback: CallbackQuery):
    """Пагинация по списку объявлений"""
    await callback.answer()
    if not await require_subscription(callback):
        return
    try:
        page = int(callback.data.replace("listings_page_", ""))
    except ValueError:
        page = 0
    await show_listings_page(callback, page=page)


@router.callback_query(F.data.startswith("listing_view_"))
async def cb_listing_view(callback: CallbackQuery):
    """Шаг 2: детальный просмотр объявления"""
    await callback.answer()
    if not await require_subscription(callback):
        return
    listing_id = int(callback.data.replace("listing_view_", ""))

    async with async_session() as session:
        result = await session.execute(select(Listing).where(Listing.id == listing_id))
        listing = result.scalar_one_or_none()

    if not listing or listing.status != "active":
        await callback.message.answer(
            f'{emoji("cross")} <b>Объявление больше не доступно</b>\n\n'
            'Возможно, его уже купили или сняли с продажи.',
            reply_markup=main_menu_keyboard()
        )
        return

    seller = await get_user(listing.seller_id)
    seller_label = f"@{seller.username}" if seller and seller.username else f"id{listing.seller_id}"

    is_owner = (listing.seller_id == callback.from_user.id)

    text = (
        f'{emoji("title")} <b>{listing.title}</b>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji("description")} <b>Описание:</b>\n'
        f'<i>{listing.description or "- без описания -"}</i>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji("money")} <b>Цена:</b> <code>{listing.price:.0f}₽</code>\n'
        f'{emoji("seller")} <b>Продавец:</b> {seller_label}\n'
        f'{emoji("clock")} <i>Опубликовано: {listing.created_at.strftime("%d.%m.%Y %H:%M")}</i>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
    )

    if not is_owner:
        text += f'\n{emoji("buy")} <b>Нажмите «КУПИТЬ» чтобы перейти к оплате</b>'
    else:
        text += f'\n{emoji("info")} <i>Это ваше объявление. Управлять им можно в разделе «Мои продажи».</i>'

    await send_media_message(callback, "buy_account", text, listing_detail_keyboard(listing.id, is_owner=is_owner))


@router.callback_query(F.data.startswith("listing_buy_"))
async def cb_listing_buy(callback: CallbackQuery):
    """Покупка объявления: проверяем баланс, списываем, создаём Hold для продавца"""
    await callback.answer()
    if not await require_subscription(callback):
        return
    listing_id = int(callback.data.replace("listing_buy_", ""))

    async with async_session() as session:
        # Берём объявление
        result = await session.execute(select(Listing).where(Listing.id == listing_id))
        listing = result.scalar_one_or_none()

        if not listing or listing.status != "active":
            await callback.message.answer(
                f'{emoji("cross")} <b>Объявление больше не доступно.</b>',
                reply_markup=main_menu_keyboard()
            )
            return

        # Нельзя купить своё объявление
        if listing.seller_id == callback.from_user.id:
            await callback.answer("Нельзя купить собственное объявление", show_alert=True)
            return

        # Получаем покупателя
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        buyer = result.scalar_one_or_none()
        if not buyer:
            await callback.message.answer(
                f'{emoji("cross")} <b>Покупатель не найден.</b> Нажмите /start',
                reply_markup=main_menu_keyboard()
            )
            return

        if buyer.balance < listing.price:
            need = listing.price - buyer.balance
            builder = InlineKeyboardBuilder()
            builder.row(create_button("💰 Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet"))
            builder.row(create_button("◀️ К объявлениям", callback_data="listings_page_0", style="default", icon="back"))
            await callback.message.answer(
                f'{emoji("cross")} <b>Недостаточно средств</b>\n\n'
                f'{emoji("wallet")} Баланс: <b>{buyer.balance:.0f}₽</b>\n'
                f'{emoji("money")} Цена: <b>{listing.price:.0f}₽</b>\n'
                f'{emoji("info")} Не хватает: <b>{need:.0f}₽</b>',
                reply_markup=builder.as_markup()
            )
            return

        # Списываем деньги у покупателя
        buyer.balance -= listing.price
        buyer.total_spent = (buyer.total_spent or 0) + listing.price

        # Помечаем объявление проданным
        listing.status = "sold"
        listing.buyer_id = callback.from_user.id
        listing.sold_at = datetime.utcnow()

        # Помечаем аккаунт проданным
        result = await session.execute(select(Account).where(Account.id == listing.account_id))
        account = result.scalar_one_or_none()
        if account:
            account.is_sold = True

        # Создаём Purchase
        purchase = Purchase(
            user_id=callback.from_user.id,
            account_id=listing.account_id,
            listing_id=listing.id,
            amount=listing.price,
            payment_method="balance"
        )
        session.add(purchase)
        await session.flush()  # получаем purchase.id

        # Считаем комиссию и холд для продавца
        gross = listing.price
        commission = round(gross * COMMISSION_PERCENT / 100.0, 2)
        net = round(gross - commission, 2)

        # Зачисляем продавцу в холд
        result = await session.execute(
            select(User).where(User.telegram_id == listing.seller_id)
        )
        seller = result.scalar_one_or_none()
        if seller:
            seller.hold_balance = (seller.hold_balance or 0) + net

        # Создаём запись Hold
        release_at = datetime.utcnow() + timedelta(hours=HOLD_PERIOD_HOURS)
        hold = Hold(
            seller_id=listing.seller_id,
            listing_id=listing.id,
            purchase_id=purchase.id,
            gross_amount=gross,
            commission=commission,
            net_amount=net,
            status="hold",
            release_at=release_at,
        )
        session.add(hold)
        await session.commit()
        await session.refresh(purchase)

        # Уведомляем продавца о продаже
        if seller:
            try:
                seller_text = (
                    f'{emoji("sell")} <b>Ваш аккаунт купили!</b>\n\n'
                    f'{emoji("title")} Объявление: <b>{listing.title}</b>\n'
                    f'{emoji("money")} Сумма: <b>{gross:.0f}₽</b>\n'
                    f'{emoji("info")} Комиссия {COMMISSION_PERCENT:.0f}%: <b>{commission:.0f}₽</b>\n'
                    f'{emoji("wallet")} Вам поступит: <b>{net:.0f}₽</b>\n\n'
                    f'{emoji("hold")} <b>Деньги в холде {HOLD_PERIOD_HOURS}ч.</b>\n'
                    f'{emoji("clock")} Зачисление: <b>{release_at.strftime("%d.%m.%Y %H:%M")}</b>\n'
                    f'После проверки средства поступят на ваш баланс.'
                )
                await bot.send_message(listing.seller_id, seller_text)
            except Exception as e:
                logger.error(f"Failed to notify seller {listing.seller_id}: {e}")

        # Подтверждение покупателю
        confirm_text = (
            f'{emoji("check")} <b>Покупка успешна!</b>\n\n'
            f'{emoji("title")} Объявление: <b>{listing.title}</b>\n'
            f'{emoji("money")} Списано: <b>{listing.price:.0f}₽</b>\n'
            f'{emoji("wallet")} Остаток: <b>{buyer.balance:.0f}₽</b>\n\n'
            f'{emoji("code")} <i>Ниже - данные аккаунта.</i>'
        )
        await callback.message.answer(confirm_text, reply_markup=get_code_keyboard(purchase.id))


# ===== СТАРЫЙ FLOW (оставлен как fallback) =====

@router.callback_query(F.data.startswith("country_"))
async def cb_country(callback: CallbackQuery):
    """Legacy: выбор страны по старой схеме (admin-аккаунты по странам)."""
    await callback.answer()

    if not await require_subscription(callback):
        return

    country = callback.data.replace("country_", "")
    logger.info(f"User {callback.from_user.id} selected country (legacy): {country}")

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

        flag = COUNTRY_FLAGS.get(country, "")

        text = (
            f'{emoji("check")} <b>Аккаунт найден!</b>\n\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'{emoji("location")} <b>Страна:</b> {flag} {country}\n'
            f'{emoji("money")} <b>Цена:</b> <code>{price:.0f}₽</code>\n'
            f'━━━━━━━━━━━━━━━━━━\n\n'
            f'{emoji("clock")} <i>Аккаунт верифицирован и готов к покупке</i>\n\n'
            f'{emoji("buy")} <b>Нажмите КУПИТЬ для продолжения</b>'
        )
        await callback.message.answer(text, reply_markup=account_found_keyboard())
    else:
        logger.info(f"No accounts available for {country}")
        await callback.message.answer(
            f'{emoji("cross")} <b>Нет доступных аккаунтов для {country}</b>\n\n'
            'Попробуйте выбрать другую страну или подождите поступления.',
            reply_markup=await countries_keyboard()
        )


@router.callback_query(F.data == "show_payment_methods")
async def cb_show_payment_methods(callback: CallbackQuery):
    """Показ способов оплаты"""
    await callback.answer()

    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    country = pending.get('country', 'Неизвестно')

    flag = COUNTRY_FLAGS.get(country, "")

    text = (
        f'{emoji("buy")} <b>Оформление покупки</b>\n\n'
        f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
        f'{emoji("money")} Сумма к оплате: <b>{price:.0f}₽</b>\n\n'
        '<i>Выберите удобный способ оплаты:</i>'
    )
    await send_media_message(callback, "payment_methods", text, payment_methods_keyboard())


# ===== МАРКЕТПЛЕЙС: ПОТОК ПРОДАЖИ (P2P) =====

@router.callback_query(F.data == "sell_account")
async def cb_sell_account(callback: CallbackQuery, state: FSMContext):
    """Старт продажи: выбор способа загрузки аккаунта"""
    await callback.answer()
    if not await require_subscription(callback):
        return
    await state.clear()

    text = (
        f'{emoji("sell")} <b>Продать аккаунт</b>\n\n'
        f'{emoji("info")} <b>Как это работает:</b>\n'
        f'1️⃣ Указываете <b>название</b> объявления\n'
        f'2️⃣ Пишете <b>описание</b>\n'
        f'3️⃣ Ставите свою <b>цену</b>\n'
        f'4️⃣ Загружаете аккаунт (.session или через код)\n'
        f'5️⃣ Объявление появляется в маркетплейсе\n\n'
        f'{emoji("money")} <b>Комиссия платформы:</b> {COMMISSION_PERCENT:.0f}%\n'
        f'{emoji("hold")} <b>Деньги в холде:</b> {HOLD_PERIOD_HOURS} ч. после продажи\n\n'
        f'{emoji("wallet")} <b>Пример расчёта:</b>\n'
        f'  Цена 1000₽ → комиссия {COMMISSION_PERCENT*10:.0f}₽ → вам поступит {1000-COMMISSION_PERCENT*10:.0f}₽\n\n'
        f'{emoji("question")} <i>Выберите способ загрузки аккаунта:</i>'
    )
    await safe_answer(callback.message, text, reply_markup=sell_start_keyboard())


@router.callback_query(F.data == "sell_session")
async def cb_sell_session(callback: CallbackQuery, state: FSMContext):
    """Начало ввода: ждём название, фиксируем способ — .session"""
    await callback.answer()
    await state.clear()
    await state.update_data(sell_mode="session")
    await state.set_state(SellStates.waiting_for_title)
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="main_menu", style="danger", icon="back"))
    await callback.message.answer(
        f'{emoji("title")} <b>Шаг 1 из 4: Название объявления</b>\n\n'
        f'{emoji("info")} <i>Придумайте короткое название, которое увидит покупатель.</i>\n\n'
        f'Примеры:\n'
        f'  • <i>Telegram Premium 2025</i>\n'
        f'  • <i>Аккаунт с каналом 10к подписчиков</i>\n'
        f'  • <i>Номер РФ, без ограничений</i>\n\n'
        f'{emoji("clock")} <i>Отправьте название одним сообщением (до 100 символов).</i>',
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "sell_phone")
async def cb_sell_phone(callback: CallbackQuery, state: FSMContext):
    """Выбран вход через телефон+код — просим название, фиксируем способ"""
    await callback.answer()
    await state.clear()
    await state.update_data(sell_mode="phone")
    await state.set_state(SellStates.waiting_for_title)
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="main_menu", style="danger", icon="back"))
    await callback.message.answer(
        f'{emoji("title")} <b>Шаг 1 из 4: Название объявления</b>\n\n'
        f'<i>Вы выбрали вход через код подтверждения.\n'
        f'Сначала введите название объявления:</i>',
        reply_markup=builder.as_markup()
    )


@router.message(SellStates.waiting_for_title, F.text)
async def h_sell_title(message: Message, state: FSMContext):
    """Обработка ввода названия"""
    title = message.text.strip()
    if not title or len(title) > 100:
        await message.answer(
            f'{emoji("cross")} <b>Название должно быть от 1 до 100 символов.</b>\n'
            f'<i>Попробуйте ещё раз:</i>'
        )
        return
    await state.update_data(title=title)
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="main_menu", style="danger", icon="back"))
    await message.answer(
        f'{emoji("check")} <b>Название сохранено:</b> <i>{title}</i>\n\n'
        f'{emoji("description")} <b>Шаг 2 из 4: Описание</b>\n\n'
        f'{emoji("info")} <i>Расскажите про аккаунт: возраст, активность, подписчики, особенности.</i>\n\n'
        f'Пример:\n'
        f'  <i>Регистрация 2022, есть 2FA, активный, подписан на 50 каналов.</i>\n\n'
        f'{emoji("clock")} <i>Отправьте описание (до 1000 символов). Можно отправить «-» чтобы пропустить.</i>',
        reply_markup=builder.as_markup()
    )
    await state.set_state(SellStates.waiting_for_description)


@router.message(SellStates.waiting_for_description, F.text)
async def h_sell_description(message: Message, state: FSMContext):
    """Обработка ввода описания"""
    description = message.text.strip()
    if description == "-":
        description = ""
    elif len(description) > 1000:
        await message.answer(
            f'{emoji("cross")} <b>Слишком длинное описание (макс 1000 символов).</b>\n'
            f'<i>Попробуйте ещё раз или отправьте «-» чтобы пропустить.</i>'
        )
        return
    await state.update_data(description=description)
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="main_menu", style="danger", icon="back"))
    await message.answer(
        f'{emoji("description")} <b>Описание сохранено.</b>\n\n'
        f'{emoji("money")} <b>Шаг 3 из 4: Цена</b>\n\n'
        f'{emoji("info")} <i>Укажите цену в рублях, за которую хотите продать аккаунт.</i>\n\n'
        f'💸 <b>Помните:</b> с продажи удержится комиссия {COMMISSION_PERCENT:.0f}%.\n'
        f'   Вы получите: цена - {COMMISSION_PERCENT:.0f}% (через {HOLD_PERIOD_HOURS} ч.)\n\n'
        f'Пример: <code>500</code> → вы получите <b>{round(500*(100-COMMISSION_PERCENT)/100, 2):.0f}₽</b>\n\n'
        f'{emoji("clock")} <i>Отправьте число от {MIN_LISTING_PRICE:.0f} до {MAX_LISTING_PRICE:.0f}.</i>',
        reply_markup=builder.as_markup()
    )
    await state.set_state(SellStates.waiting_for_price)


@router.message(SellStates.waiting_for_price, F.text)
async def h_sell_price(message: Message, state: FSMContext):
    """Обработка ввода цены"""
    raw = message.text.strip().replace(',', '.')
    try:
        price = float(raw)
    except ValueError:
        await message.answer(
            f'{emoji("cross")} <b>Некорректная цена.</b> Отправьте число, например <code>500</code>.'
        )
        return
    if price < MIN_LISTING_PRICE or price > MAX_LISTING_PRICE:
        await message.answer(
            f'{emoji("cross")} <b>Цена должна быть от {MIN_LISTING_PRICE:.0f} до {MAX_LISTING_PRICE:.0f} ₽.</b>'
        )
        return

    data = await state.get_data()
    title = data.get("title", "")
    description = data.get("description", "")
    sell_mode = data.get("sell_mode", "session")

    commission = round(price * COMMISSION_PERCENT / 100.0, 2)
    net = round(price - commission, 2)

    await state.update_data(price=price)

    text = (
        f'{emoji("check")} <b>Проверьте объявление</b>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji("title")} <b>Название:</b> {title}\n\n'
        f'{emoji("description")} <b>Описание:</b>\n'
        f'<i>{description or "- без описания -"}</i>\n\n'
        f'{emoji("money")} <b>Цена:</b> <code>{price:.0f}₽</code>\n'
        f'   Комиссия {COMMISSION_PERCENT:.0f}%: <code>{commission:.0f}₽</code>\n'
        f'   Вам поступит: <b>{net:.0f}₽</b> (через {HOLD_PERIOD_HOURS} ч.)\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
    )

    if sell_mode == "phone":
        text += f'{emoji("phone")} <b>Сейчас нужно будет пройти вход через код Telegram.</b>'
        builder = InlineKeyboardBuilder()
        builder.row(create_button("📱 Ввести номер", callback_data="sell_phone_enter", style="primary", icon="phone"))
        builder.row(create_button("✏️ Изменить название", callback_data="sell_edit_title", style="default", icon="edit"))
        builder.row(create_button("✏️ Изменить описание", callback_data="sell_edit_description", style="default", icon="edit"))
        builder.row(create_button("✏️ Изменить цену", callback_data="sell_edit_price", style="default", icon="edit"))
        builder.row(create_button("❌ Отменить", callback_data="sell_cancel", style="danger", icon="cross"))
        await message.answer(text, reply_markup=builder.as_markup())
    else:
        text += f'{emoji("session")} <b>Сейчас нужно отправить .session файл аккаунта.</b>'
        builder = InlineKeyboardBuilder()
        builder.row(create_button("📁 Отправить .session", callback_data="sell_send_session", style="primary", icon="session"))
        builder.row(create_button("✏️ Изменить название", callback_data="sell_edit_title", style="default", icon="edit"))
        builder.row(create_button("✏️ Изменить описание", callback_data="sell_edit_description", style="default", icon="edit"))
        builder.row(create_button("✏️ Изменить цену", callback_data="sell_edit_price", style="default", icon="edit"))
        builder.row(create_button("❌ Отменить", callback_data="sell_cancel", style="danger", icon="cross"))
        await message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "sell_edit_title")
async def cb_sell_edit_title(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SellStates.waiting_for_title)
    await callback.message.answer(
        f'{emoji("edit")} <b>Введите новое название:</b>'
    )


@router.callback_query(F.data == "sell_edit_description")
async def cb_sell_edit_description(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SellStates.waiting_for_description)
    await callback.message.answer(
        f'{emoji("edit")} <b>Введите новое описание</b> (или «-» чтобы очистить):'
    )


@router.callback_query(F.data == "sell_edit_price")
async def cb_sell_edit_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SellStates.waiting_for_price)
    await callback.message.answer(
        f'{emoji("edit")} <b>Введите новую цену:</b>'
    )


@router.callback_query(F.data == "sell_cancel")
async def cb_sell_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    if hasattr(dp, 'sell_drafts'):
        dp.sell_drafts.pop(callback.from_user.id, None)
    await callback.message.answer(
        f'{emoji("cross")} <b>Создание объявления отменено.</b>',
        reply_markup=main_menu_keyboard()
    )


@router.callback_query(F.data == "sell_send_session")
async def cb_sell_send_session(callback: CallbackQuery, state: FSMContext):
    """Запрашиваем .session файл"""
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="sell_cancel", style="danger", icon="cross"))
    await callback.message.answer(
        f'{emoji("session")} <b>Отправьте .session файл аккаунта</b>\n\n'
        f'{emoji("info")} <i>Файл должен быть получен из Telethon / Pyrogram.\n'
        f'Бот проверит сессию и сохранит её.</i>\n\n'
        f'{emoji("clock")} <i>Жду файл...</i>',
        reply_markup=builder.as_markup()
    )
    await state.set_state(SellStates.waiting_for_session)


@router.message(SellStates.waiting_for_session, F.document)
async def h_sell_session_file(message: Message, state: FSMContext):
    """Приём .session файла от продавца"""
    document = message.document
    if not document or not document.file_name or not document.file_name.endswith(".session"):
        await message.answer(
            f'{emoji("cross")} <b>Это не .session файл.</b>\n'
            f'Отправьте именно файл с расширением .session.'
        )
        return

    status = await message.answer(f'{emoji("loading")} <b>Загружаю файл...</b>')

    file = await bot.get_file(document.file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    session_str = bio.getvalue().decode('utf-8', errors='ignore').strip()

    # Валидируем сессию через Telethon
    valid = await validate_session_string(session_str)
    if not valid['ok']:
        await status.edit_text(
            f'{emoji("cross")} <b>Невалидная сессия.</b>\n\n{valid["error"]}\n\n'
            f'{emoji("info")} Отправьте другой файл или нажмите «Отмена».',
            reply_markup=InlineKeyboardBuilder().row(
                create_button("◀️ Отмена", callback_data="sell_cancel", style="danger", icon="cross")
            ).as_markup()
        )
        return

    phone = valid['phone']
    country = detect_country(phone)
    flag = COUNTRY_FLAGS.get(country, "")

    data = await state.get_data()
    title = data['title']
    description = data.get('description', '')
    price = data['price']

    # Сохраняем аккаунт
    async with async_session() as session:
        # Проверяем что такого номера ещё нет
        existing = (await session.execute(
            select(Account).where(Account.phone == phone)
        )).scalar_one_or_none()
        if existing:
            await status.edit_text(
                f'{emoji("cross")} <b>Аккаунт с номером {phone} уже есть в базе.</b>\n'
                f'Возможно, он уже выставлен на продажу.',
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        account = Account(
            phone=phone,
            country=country,
            price=price,
            session_string=session_str,
            is_verified=True,
            is_sold=False,
            seller_id=message.from_user.id,
        )
        session.add(account)
        await session.flush()

        listing = Listing(
            seller_id=message.from_user.id,
            account_id=account.id,
            title=title,
            description=description,
            price=price,
            status="active",
        )
        session.add(listing)
        await session.commit()
        await session.refresh(listing)

    await state.clear()

    commission = round(price * COMMISSION_PERCENT / 100.0, 2)
    net = round(price - commission, 2)

    await status.edit_text(
        f'{emoji("check")} <b>Объявление опубликовано!</b>\n\n'
        f'{emoji("title")} {title}\n'
        f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
        f'{emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
        f'{emoji("info")} Комиссия {COMMISSION_PERCENT:.0f}%: <b>{commission:.0f}₽</b>\n'
        f'{emoji("wallet")} Вам поступит: <b>{net:.0f}₽</b>\n\n'
        f'{emoji("hold")} <i>Деньги попадут в холд на {HOLD_PERIOD_HOURS} ч. после продажи.</i>\n\n'
        f'{emoji("market")} <i>Объявление #{listing.id} уже в маркетплейсе.</i>',
        reply_markup=InlineKeyboardBuilder().row(
            create_button("📦 Мои продажи", callback_data="my_sales", style="primary", icon="market")
        ).row(
            create_button("В меню", callback_data="main_menu", style="danger", icon="home")
        ).as_markup()
    )


async def validate_session_string(session_str: str) -> dict:
    """
    Проверяет .session файл: создаёт StringSession, пробует подключиться, извлекает номер.
    Возвращает {'ok': bool, 'phone': str, 'error': str}
    """
    if not session_str or len(session_str) < 50:
        return {'ok': False, 'error': 'Файл пустой или слишком короткий'}
    try:
        from telethon.sessions import StringSession
        from telethon import TelegramClient
        sess = StringSession(session_str)
        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {'ok': False, 'error': 'Сессия не авторизована'}
        me = await client.get_me()
        phone = getattr(me, 'phone', None)
        await client.disconnect()
        if not phone:
            return {'ok': False, 'error': 'Не удалось получить номер телефона'}
        return {'ok': True, 'phone': '+' + phone}
    except Exception as e:
        return {'ok': False, 'error': f'Ошибка Telethon: {e}'}


@router.callback_query(F.data == "sell_phone_enter")
async def cb_sell_phone_enter(callback: CallbackQuery, state: FSMContext):
    """Просим ввести номер телефона"""
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.row(create_button("◀️ Отмена", callback_data="sell_cancel", style="danger", icon="cross"))
    await callback.message.answer(
        f'{emoji("phone")} <b>Введите номер телефона</b> в международном формате:\n\n'
        f'Пример: <code>+79001234567</code>\n\n'
        f'{emoji("info")} <i>На этот номер придёт код подтверждения из Telegram.</i>',
        reply_markup=builder.as_markup()
    )
    await state.set_state(SellStates.waiting_for_phone)


@router.message(SellStates.waiting_for_phone, F.text)
async def h_sell_phone(message: Message, state: FSMContext):
    """Получили номер - отправляем код"""
    phone = message.text.strip()
    if not phone.startswith('+') or len(phone) < 8:
        await message.answer(
            f'{emoji("cross")} <b>Некорректный формат номера.</b> Пример: <code>+79001234567</code>'
        )
        return

    status = await message.answer(f'{emoji("loading")} <b>Отправляю код на {phone}...</b>')
    result = await send_code_to_phone(phone)

    if not result['success']:
        await status.edit_text(
            f'{emoji("cross")} <b>Не удалось отправить код.</b>\n\n{result.get("error", "")}',
            reply_markup=InlineKeyboardBuilder().row(
                create_button("◀️ Отмена", callback_data="sell_cancel", style="danger", icon="cross")
            ).as_markup()
        )
        return

    await state.update_data(sell_phone=phone, sell_phone_hash=result['phone_code_hash'])
    await status.edit_text(
        f'{emoji("check")} <b>Код отправлен на {phone}.</b>\n\n'
        f'📨 <b>Введите код из Telegram:</b>'
    )
    await state.set_state(SellStates.waiting_for_code)


@router.message(SellStates.waiting_for_code, F.text)
async def h_sell_code(message: Message, state: FSMContext):
    """Получили код - создаём сессию"""
    code = message.text.strip()
    data = await state.get_data()
    phone = data['sell_phone']
    phone_hash = data['sell_phone_hash']

    status = await message.answer(f'{emoji("loading")} <b>Проверяю код...</b>')
    result = await verify_code_and_create_session_json(phone, code, phone_hash)

    if result.get('need_password'):
        await status.edit_text(
            f'{emoji("lock")} <b>Включена 2FA.</b> Введите пароль облачной защиты:'
        )
        await state.set_state(SellStates.waiting_for_2fa)
        return

    if not result['success']:
        await status.edit_text(
            f'{emoji("cross")} <b>Ошибка:</b> {result.get("error", "неизвестно")}\n\n'
            f'<i>Попробуйте ещё раз или отмените.</i>',
            reply_markup=InlineKeyboardBuilder().row(
                create_button("◀️ Отмена", callback_data="sell_cancel", style="danger", icon="cross")
            ).as_markup()
        )
        return

    # Успех - создаём объявление
    await _publish_listing_from_session(message, state, result, phone)


@router.message(SellStates.waiting_for_2fa, F.text)
async def h_sell_2fa(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data['sell_phone']
    status = await message.answer(f'{emoji("loading")} <b>Проверяю 2FA пароль...</b>')
    result = await verify_2fa_and_create_session_json(phone, password)
    if not result['success']:
        await status.edit_text(
            f'{emoji("cross")} <b>Ошибка:</b> {result.get("error", "неизвестно")}'
        )
        return
    await _publish_listing_from_session(message, state, result, phone)


async def _publish_listing_from_session(message: Message, state: FSMContext, result: dict, phone: str):
    """Публикует объявление, когда сессия уже валидна"""
    data = await state.get_data()
    title = data['title']
    description = data.get('description', '')
    price = data['price']
    seller_id = message.from_user.id

    country = detect_country(phone)
    flag = COUNTRY_FLAGS.get(country, "")

    async with async_session() as session:
        existing = (await session.execute(
            select(Account).where(Account.phone == phone)
        )).scalar_one_or_none()
        if existing:
            await message.answer(
                f'{emoji("cross")} <b>Аккаунт {phone} уже есть в базе.</b>',
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        account = Account(
            phone=phone,
            country=country,
            price=price,
            session_string=result['session_string'],
            session_json=result.get('session_json'),
            is_verified=True,
            is_sold=False,
            seller_id=seller_id,
        )
        session.add(account)
        await session.flush()

        listing = Listing(
            seller_id=seller_id,
            account_id=account.id,
            title=title,
            description=description,
            price=price,
            status="active",
        )
        session.add(listing)
        await session.commit()
        await session.refresh(listing)

    await state.clear()

    commission = round(price * COMMISSION_PERCENT / 100.0, 2)
    net = round(price - commission, 2)

    await message.answer(
        f'{emoji("check")} <b>Объявление опубликовано!</b>\n\n'
        f'{emoji("title")} {title}\n'
        f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
        f'{emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
        f'{emoji("info")} Комиссия {COMMISSION_PERCENT:.0f}%: <b>{commission:.0f}₽</b>\n'
        f'{emoji("wallet")} Вам поступит: <b>{net:.0f}₽</b>\n\n'
        f'{emoji("hold")} <i>Деньги в холде {HOLD_PERIOD_HOURS} ч. после продажи.</i>\n\n'
        f'{emoji("market")} <i>Объявление #{listing.id} в маркетплейсе.</i>',
        reply_markup=InlineKeyboardBuilder().row(
            create_button("📦 Мои продажи", callback_data="my_sales", style="primary", icon="market")
        ).row(
            create_button("В меню", callback_data="main_menu", style="danger", icon="home")
        ).as_markup()
    )


# ===== МОИ ПРОДАЖИ =====

@router.callback_query(F.data == "my_sales")
async def cb_my_sales(callback: CallbackQuery):
    """Список своих объявлений + холды"""
    await callback.answer()
    if not await require_subscription(callback):
        return

    seller_id = callback.from_user.id

    async with async_session() as session:
        result = await session.execute(
            select(Listing).where(Listing.seller_id == seller_id)
            .order_by(Listing.created_at.desc()).limit(30)
        )
        listings = result.scalars().all()

        # Сумма активных холдов
        hold_sum = (await session.execute(
            select(func.coalesce(func.sum(Hold.net_amount), 0))
            .where(Hold.seller_id == seller_id, Hold.status == "hold")
        )).scalar() or 0

    text = (
        f'{emoji("market")} <b>Мои продажи</b>\n\n'
        f'{emoji("box")} Всего объявлений: <b>{len(listings)}</b>\n'
        f'{emoji("hold")} В холде: <b>{hold_sum:.0f}₽</b>\n'
        f'{emoji("clock")} <i>Деньги из холда поступят на баланс автоматически.</i>\n\n'
    )

    if not listings:
        text += f'{emoji("info")} <i>У вас пока нет объявлений.</i>'
        builder = InlineKeyboardBuilder()
        builder.row(create_button("💼 Новое объявление", callback_data="sell_account", style="success", icon="add"))
        builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
        await callback.message.answer(text, reply_markup=builder.as_markup())
        return

    builder = InlineKeyboardBuilder()
    for l in listings[:15]:
        status_icon = {"active": "🟢", "sold": "🔴", "cancelled": "⚪️"}.get(l.status, "•")
        title_short = l.title[:28] + ("..." if len(l.title) > 28 else "")
        builder.row(
            create_button(
                f"{status_icon} {title_short} - {l.price:.0f}₽",
                callback_data=f"my_listing_{l.id}",
                style="default", icon="box"
            )
        )
    builder.row(create_button("➕ Новое объявление", callback_data="sell_account", style="success", icon="add"))
    builder.row(create_button("В меню", callback_data="main_menu", style="danger", icon="home"))
    await callback.message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("my_listing_"))
async def cb_my_listing_manage(callback: CallbackQuery):
    """Карточка моего объявления"""
    await callback.answer()
    listing_id = int(callback.data.replace("my_listing_", ""))

    async with async_session() as session:
        result = await session.execute(select(Listing).where(Listing.id == listing_id))
        listing = result.scalar_one_or_none()

    if not listing or listing.seller_id != callback.from_user.id:
        await callback.answer("Объявление не найдено", show_alert=True)
        return

    status_label = {"active": "🟢 Активно", "sold": "🔴 Продано", "cancelled": "⚪️ Снято"}.get(listing.status, listing.status)

    text = (
        f'{emoji("title")} <b>{listing.title}</b>\n\n'
        f'{emoji("description")} <i>{listing.description or "- без описания -"}</i>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji("money")} Цена: <b>{listing.price:.0f}₽</b>\n'
        f'   (после комиссии вам: <b>{listing.price * (100 - COMMISSION_PERCENT) / 100:.0f}₽</b>)\n'
        f'{emoji("info")} Статус: <b>{status_label}</b>\n'
        f'{emoji("clock")} Создано: {listing.created_at.strftime("%d.%m.%Y %H:%M")}\n'
    )

    if listing.status == "sold":
        text += f'{emoji("check")} Продано: {listing.sold_at.strftime("%d.%m.%Y %H:%M") if listing.sold_at else "-"}\n'
        # Ищем холд
        async with async_session() as session:
            h = (await session.execute(
                select(Hold).where(Hold.listing_id == listing.id)
            )).scalar_one_or_none()
        if h:
            text += f'{emoji("hold")} Холд: <b>{h.status}</b>, отпустится {h.release_at.strftime("%d.%m.%Y %H:%M")}\n'
            text += f'   Вам поступит: <b>{h.net_amount:.0f}₽</b> (комиссия {h.commission:.0f}₽)'

    await callback.message.answer(text, reply_markup=my_listing_manage_keyboard(listing.id, listing.status))


@router.callback_query(F.data.startswith("my_listing_cancel_"))
async def cb_my_listing_cancel(callback: CallbackQuery):
    """Снять своё объявление с продажи"""
    await callback.answer()
    listing_id = int(callback.data.replace("my_listing_cancel_", ""))

    async with async_session() as session:
        result = await session.execute(select(Listing).where(Listing.id == listing_id))
        listing = result.scalar_one_or_none()
        if not listing or listing.seller_id != callback.from_user.id:
            await callback.answer("Не найдено", show_alert=True)
            return
        if listing.status != "active":
            await callback.answer("Нельзя снять - объявление уже не активно", show_alert=True)
            return
        listing.status = "cancelled"
        # Аккаунт освобождаем, чтобы можно было перепродать
        result2 = await session.execute(select(Account).where(Account.id == listing.account_id))
        account = result2.scalar_one_or_none()
        await session.commit()

    await callback.message.answer(
        f'{emoji("check")} <b>Объявление снято с продажи.</b>',
        reply_markup=main_menu_keyboard()
    )


# ===== ОБРАБОТЧИКИ ОПЛАТЫ (legacy) =====

@router.callback_query(F.data == "pay_balance")
async def cb_pay_balance(callback: CallbackQuery):
    """Оплата с баланса бота"""
    await callback.answer()

    # Получаем пользователя через get_user (ищет по telegram_id)
    user = await get_user(callback.from_user.id)

    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    account_id = pending.get('account_id')

    if not account_id:
        await callback.message.answer(
            f'{emoji("cross")} <b>Ошибка. Данные заказа утеряны.</b>\n\n'
            'Пожалуйста, начните заново.',
            reply_markup=main_menu_keyboard()
        )
        return

    if not user:
        await callback.message.answer(
            f'{emoji("cross")} <b>Пользователь не найден.</b>\n\n'
            'Пожалуйста, перезапустите бота командой /start',
            reply_markup=main_menu_keyboard()
        )
        return

    if user.balance >= price:
        # Обрабатываем покупку
        purchase = await process_purchase(callback.from_user.id, account_id, price, "balance")

        if purchase:
            # Получаем данные аккаунта для отображения
            async with async_session() as session:
                result = await session.execute(
                    select(Account).where(Account.id == account_id)
                )
                account = result.scalar_one_or_none()

                if account:
                    text = (
                        f'{emoji("check")} <b>Оплата успешна!</b>\n\n'
                        f'{emoji("tag")} Номер: <code>{account.phone}</code>\n'
                        f'{emoji("money")} Списано с баланса: <b>{price:.0f}₽</b>\n'
                        f'{emoji("wallet")} Остаток на балансе: <b>{user.balance - price:.0f}₽</b>\n\n'
                        'Нажмите для получения данных:'
                    )
                    await callback.message.answer(text, reply_markup=get_code_keyboard(purchase.id))
                else:
                    await callback.message.answer(
                        f'{emoji("cross")} <b>Ошибка получения данных аккаунта</b>',
                        reply_markup=main_menu_keyboard()
                    )
        else:
            await callback.message.answer(
                f'{emoji("cross")} <b>Ошибка обработки покупки</b>\n\n'
                'Возможно аккаунт уже продан. Попробуйте выбрать другой.',
                reply_markup=main_menu_keyboard()
            )
    else:
        text = (
            f'{emoji("cross")} <b>Недостаточно средств</b>\n\n'
            f'{emoji("wallet")} Ваш баланс: <b>{user.balance:.0f}₽</b>\n'
            f'{emoji("money")} Необходимо: <b>{price:.0f}₽</b>\n'
            f'{emoji("info")} Не хватает: <b>{price - user.balance:.0f}₽</b>\n\n'
            '<i>Пополните баланс в профиле или выберите другой способ оплаты</i>'
        )
        builder = InlineKeyboardBuilder()
        builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet"))
        builder.row(create_button("Другие способы", callback_data="show_payment_methods", style="default", icon="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "pay_sbp")
async def cb_pay_sbp(callback: CallbackQuery):
    """Оплата через СБП (только пополнение)"""
    await callback.answer()

    text = (
        f'{emoji("sbp")} <b>Оплата через СБП</b>\n\n'
        f'{emoji("info")} Для оплаты товара через СБП необходимо сначала пополнить баланс бота.\n\n'
        'Перейдите в раздел "Пополнить" в главном меню и выберите СБП.\n\n'
        f'{emoji("money")} После пополнения баланса вы сможете оплатить товар с баланса.'
    )
    builder = InlineKeyboardBuilder()
    builder.row(create_button("Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet"))
    builder.row(create_button("Назад", callback_data="show_payment_methods", style="default", icon="back"))
    await callback.message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery):
    """Оплата через Crypto Bot"""
    await callback.answer()

    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    account_id = pending.get('account_id')

    # Генерируем ID платежа
    payment_id = await generate_payment_id()

    # Сохраняем платеж в БД
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

    # Создаем счет в Crypto Bot
    status_msg = await callback.message.answer(
        f'{emoji("loading")} <b>Создаю счет в Crypto Bot...</b>\n\n'
        'Пожалуйста, подождите несколько секунд.'
    )

    invoice = await create_crypto_bot_invoice(price, payment_id)

    await status_msg.delete()

    if invoice and invoice.get("ok"):
        result = invoice.get("result", {})
        pay_url = result.get("pay_url")
        invoice_id = str(result.get("invoice_id"))

        # Обновляем payment_id на invoice_id
        async with async_session() as session:
            payment_result = await session.execute(
                select(Payment).where(Payment.payment_id == payment_id)
            )
            payment = payment_result.scalar_one_or_none()
            if payment:
                payment.payment_id = invoice_id
                await session.commit()

        usdt_amount = round(price / 90, 2)

        text = (
            f'{emoji("crypto")} <b>Оплата через Crypto Bot</b>\n\n'
            f'{emoji("money")} Сумма: <b>{price:.0f}₽</b> (~{usdt_amount} USDT)\n\n'
            f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
            f'{emoji("auto")} <b>Оплата проверяется автоматически!</b>\n'
            'Вам не нужно нажимать кнопку проверки.\n'
            'После поступления оплаты бот сам выдаст аккаунт.\n\n'
            f'{emoji("clock")} <i>Ожидание до 10 минут</i>'
        )

        await callback.message.answer(
            text,
            reply_markup=check_crypto_keyboard(invoice_id),
            disable_web_page_preview=True
        )

        # Запускаем автоматическую проверку в фоне
        asyncio.create_task(auto_check_crypto_payment(invoice_id, callback.from_user.id, account_id, price))
    else:
        await callback.message.answer(
            f'{emoji("cross")} <b>Ошибка создания счета</b>\n\n'
            'Попробуйте другой способ оплаты или повторите попытку позже.',
            reply_markup=payment_methods_keyboard()
        )


@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery):
    """Оплата через Telegram Stars"""
    await callback.answer()

    text = (
        f'{emoji("star")} <b>Оплата Telegram Stars</b>\n\n'
        'Для покупки аккаунта через Telegram Stars\n'
        'напишите нашему менеджеру:\n\n'
        f'{emoji("profile")} <b>@VestGameSupport</b>\n\n'
        '<i>В сообщении укажите:\n'
        '- Страну аккаунта\n'
        '- Количество аккаунтов</i>'
    )

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Назад к способам оплаты", callback_data="show_payment_methods", style="default", icon="back"))
    await callback.message.answer(text, reply_markup=builder.as_markup())


# ===== ПРОВЕРКА ОПЛАТЫ CRYPTO BOT (РУЧНАЯ) =====

@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def cb_check_purchase_crypto(callback: CallbackQuery):
    """Ручная проверка оплаты Crypto Bot (на случай если авто не сработала)"""
    await callback.answer()

    payment_id = callback.data.replace("check_purchase_crypto_", "")

    status_msg = await callback.message.answer(
        f'{emoji("loading")} <b>Проверяю оплату...</b>\n\n'
        'Запрашиваю статус платежа в Crypto Bot...'
    )

    invoice = await check_crypto_bot_invoice(int(payment_id))

    await status_msg.delete()

    if invoice and invoice.get("status") == "paid":
        # Проверяем не обработан ли уже
        async with async_session() as session:
            result = await session.execute(
                select(Payment).where(Payment.payment_id == payment_id)
            )
            payment = result.scalar_one_or_none()

            if payment and payment.status == "completed":
                await callback.message.answer(
                    f'{emoji("check")} <b>Платеж уже обработан!</b>\n\n'
                    'Проверьте раздел "Мои покупки".',
                    reply_markup=main_menu_keyboard()
                )
                return

            pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
            account_id = pending.get('account_id')
            price = pending.get('price', 20)

            if account_id:
                purchase = await process_purchase(callback.from_user.id, account_id, price, "crypto")
                if purchase:
                    if payment:
                        payment.status = "completed"
                    await session.commit()

                    result = await session.execute(
                        select(Account).where(Account.id == account_id)
                    )
                    account = result.scalar_one_or_none()
                    if account:
                        await callback.message.answer(
                            f'{emoji("check")} <b>✅ Оплата подтверждена!</b>\n\n'
                            f'{emoji("tag")} Номер: <code>{account.phone}</code>\n'
                            f'{emoji("money")} Сумма: <b>{price:.0f}₽</b>\n\n'
                            'Нажмите для получения данных:',
                            reply_markup=get_code_keyboard(purchase.id)
                        )
                else:
                    await callback.message.answer(
                        f'{emoji("cross")} <b>Ошибка обработки покупки</b>',
                        reply_markup=main_menu_keyboard()
                    )
    elif invoice:
        status = invoice.get("status", "unknown")
        await callback.message.answer(
            f'{emoji("info")} <b>Статус платежа:</b> {status}\n\n'
            'Оплата еще не поступила. Дождитесь автоматической проверки.',
            reply_markup=check_crypto_keyboard(payment_id)
        )
    else:
        await callback.answer("⏳ Оплата не найдена. Попробуйте позже или дождитесь авто-проверки.", show_alert=True)


@router.callback_query(F.data.startswith("check_deposit_crypto_"))
async def cb_check_deposit_crypto(callback: CallbackQuery):
    """Ручная проверка пополнения через Crypto Bot"""
    await callback.answer()

    payment_id = callback.data.replace("check_deposit_crypto_", "")

    status_msg = await callback.message.answer(
        f'{emoji("loading")} <b>Проверяю пополнение...</b>'
    )

    invoice = await check_crypto_bot_invoice(int(payment_id))

    await status_msg.delete()

    if invoice and invoice.get("status") == "paid":
        async with async_session() as session:
            result = await session.execute(
                select(Payment).where(Payment.payment_id == payment_id)
            )
            payment = result.scalar_one_or_none()

            if payment and payment.status != "completed":
                payment.status = "completed"
                # Ищем пользователя по telegram_id
                result = await session.execute(
                    select(User).where(User.telegram_id == callback.from_user.id)
                )
                user = result.scalar_one_or_none()
                if user:
                    deposit_amount = payment.amount
                    user.balance += deposit_amount
                await session.commit()

                builder = InlineKeyboardBuilder()
                builder.row(create_button("В меню", callback_data="main_menu", style="success", icon="home"))

                await callback.message.answer(
                    f'{emoji("check")} <b>✅ Баланс пополнен!</b>\n\n'
                    f'{emoji("money")} Зачислено: <b>{deposit_amount:.2f}₽</b>\n'
                    f'{emoji("wallet")} Баланс: <b>{user.balance:.2f}₽</b>',
                    reply_markup=builder.as_markup()
                )
            elif payment and payment.status == "completed":
                await callback.message.answer(
                    f'{emoji("info")} <b>Платеж уже зачислен</b>',
                    reply_markup=main_menu_keyboard()
                )
    else:
        await callback.answer("⏳ Пополнение не найдено. Дождитесь авто-проверки или попробуйте позже.", show_alert=True)


# ===== ПОЛУЧЕНИЕ ДАННЫХ ПОСЛЕ ПОКУПКИ =====

@router.callback_query(F.data.startswith("get_code_"))
async def cb_get_code(callback: CallbackQuery):
    """Получение кода подтверждения из сессии"""
    await callback.answer()

    purchase_id = int(callback.data.replace("get_code_", ""))

    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        )
        purchase = result.scalar_one_or_none()

        # Проверки
        if not purchase:
            await callback.answer("❌ Покупка не найдена", show_alert=True)
            return

        if purchase.user_id != callback.from_user.id:
            await callback.answer("❌ Это не ваша покупка", show_alert=True)
            return

        result = await session.execute(
            select(Account).where(Account.id == purchase.account_id)
        )
        account = result.scalar_one_or_none()
        if not account or not account.session_string:
            await callback.answer("❌ Аккаунт не найден или нет данных сессии", show_alert=True)
            return

        # Отправляем статус поиска
        status_msg = await callback.message.answer(
            f'{emoji("loading")} <b>Ищем код подтверждения...</b>\n\n'
            f'{emoji("search")} Проверяю все диалоги аккаунта\n'
            f'{emoji("clock")} Это может занять до 15 секунд\n\n'
            '<i>Пожалуйста, подождите...</i>'
        )

        # Используем улучшенный поиск кода
        code = await get_code_from_session(account.session_string, account.phone)

        await status_msg.delete()

        if code:
            builder = InlineKeyboardBuilder()
            builder.row(create_button("Получить еще раз", callback_data=f"get_code_{purchase_id}", style="primary", icon="code"))
            builder.row(create_button("Получить .session", callback_data=f"get_session_{purchase_id}", style="default", icon="file"))
            builder.row(create_button("Получить JSON", callback_data=f"get_json_{purchase_id}", style="default", icon="json"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))

            await callback.message.answer(
                f'{emoji("check")} <b>Код успешно получен!</b>\n\n'
                f'{emoji("tag")} Номер телефона:\n<code>{account.phone}</code>\n\n'
                f'{emoji("lock")} Код подтверждения:\n<code>{code}</code>\n\n'
                f'{emoji("info")} <i>Код действителен ограниченное время</i>\n'
                f'{emoji("clock")} <i>При необходимости можно получить повторно</i>',
                reply_markup=builder.as_markup()
            )
        else:
            builder = InlineKeyboardBuilder()
            builder.row(create_button("Попробовать снова", callback_data=f"get_code_{purchase_id}", style="primary", icon="loading"))
            builder.row(create_button("Получить .session", callback_data=f"get_session_{purchase_id}", style="default", icon="file"))
            builder.row(create_button("Получить JSON", callback_data=f"get_json_{purchase_id}", style="default", icon="json"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))

            await callback.message.answer(
                f'{emoji("cross")} <b>Код не найден</b>\n\n'
                f'{emoji("info")} Возможные причины:\n'
                '• Код подтверждения еще не пришел\n'
                '• Сессия временно не активна\n'
                '• Сообщение с кодом было удалено\n\n'
                f'{emoji("clock")} <b>Рекомендации:</b>\n'
                '1. Подождите 1-2 минуты\n'
                '2. Нажмите "Попробовать снова"\n'
                '3. Используйте .session файл для ручного входа\n'
                '4. Если проблема persists - обратитесь в поддержку\n\n'
                f'{emoji("profile")} Поддержка: @v3estnikov',
                reply_markup=builder.as_markup()
            )


@router.callback_query(F.data.startswith("get_session_"))
async def cb_get_session(callback: CallbackQuery):
    """Получение .session файла"""
    await callback.answer()

    purchase_id = int(callback.data.replace("get_session_", ""))

    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        )
        purchase = result.scalar_one_or_none()

        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("❌ Покупка не найдена", show_alert=True)
            return

        result = await session.execute(
            select(Account).where(Account.id == purchase.account_id)
        )
        account = result.scalar_one_or_none()
        if not account or not account.session_string:
            await callback.answer("❌ Нет данных сессии", show_alert=True)
            return

        # Отправляем .session файл
        await callback.message.answer(
            f'{emoji("loading")} <b>Подготовка файла...</b>'
        )

        session_bytes = account.session_string.encode()
        await callback.message.answer_document(
            BufferedInputFile(session_bytes, filename=f"{account.phone}.session"),
            caption=(
                f'{emoji("file")} <b>.session файл</b>\n\n'
                f'{emoji("tag")} Номер: <code>{account.phone}</code>\n'
                f'{emoji("info")} Используйте этот файл для входа в аккаунт\n\n'
                '<i>Файл содержит авторизованную сессию Telegram</i>'
            )
        )

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Получить код", callback_data=f"get_code_{purchase_id}", style="primary", icon="code"))
        builder.row(create_button("Получить JSON", callback_data=f"get_json_{purchase_id}", style="default", icon="json"))
        builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))

        await callback.message.answer(
            f'{emoji("check")} <b>Файл .session отправлен!</b>\n\n'
            'Выше находится файл с сессией аккаунта.',
            reply_markup=builder.as_markup()
        )


@router.callback_query(F.data.startswith("get_json_"))
async def cb_get_json(callback: CallbackQuery):
    """Получение JSON файла с данными сессии"""
    await callback.answer()

    purchase_id = int(callback.data.replace("get_json_", ""))
    logger.info(f"JSON requested for purchase {purchase_id}")

    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        )
        purchase = result.scalar_one_or_none()

        if not purchase or purchase.user_id != callback.from_user.id:
            await callback.answer("❌ Покупка не найдена", show_alert=True)
            return

        result = await session.execute(
            select(Account).where(Account.id == purchase.account_id)
        )
        account = result.scalar_one_or_none()

        if not account or not account.session_json:
            # Если нет готового JSON, создаем из session_string
            if account and account.session_string:
                session_json = json.dumps({
                    "phone": account.phone,
                    "session_string": account.session_string,
                    "api_id": API_ID,
                    "api_hash": API_HASH,
                    "country": account.country
                }, ensure_ascii=False, indent=2)
                account.session_json = session_json
                await session.commit()
            else:
                await callback.answer("❌ Нет JSON данных", show_alert=True)
                return

        # Отправляем JSON файл
        await callback.message.answer(
            f'{emoji("loading")} <b>Подготовка JSON...</b>'
        )

        json_bytes = (account.session_json or "{}").encode()
        await callback.message.answer_document(
            BufferedInputFile(json_bytes, filename=f"{account.phone}_session.json"),
            caption=(
                f'{emoji("json")} <b>JSON данные сессии</b>\n\n'
                f'{emoji("tag")} Номер: <code>{account.phone}</code>\n'
                f'{emoji("info")} Содержит все данные для входа в аккаунт\n\n'
                '<i>JSON можно импортировать в Telethon</i>'
            )
        )

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Получить код", callback_data=f"get_code_{purchase_id}", style="primary", icon="code"))
        builder.row(create_button("Получить .session", callback_data=f"get_session_{purchase_id}", style="default", icon="file"))
        builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))

        await callback.message.answer(
            f'{emoji("check")} <b>JSON файл отправлен!</b>\n\n'
            'Выше находится файл с данными сессии в формате JSON.',
            reply_markup=builder.as_markup()
        )


# ===== МОИ ПОКУПКИ =====

@router.callback_query(F.data == "my_purchases")
async def cb_my_purchases(callback: CallbackQuery):
    """Список покупок пользователя"""
    await callback.answer()

    if not await require_subscription(callback):
        return

    async with async_session() as session:
        result = await session.execute(
            select(Purchase)
            .where(Purchase.user_id == callback.from_user.id)
            .order_by(Purchase.created_at.desc())
        )
        purchases = result.scalars().all()

        if purchases:
            text = f'{emoji("box")} <b>Ваши покупки</b>\n\n'
            text += f'Всего покупок: <b>{len(purchases)}</b>\n\n'

            builder = InlineKeyboardBuilder()

            for purchase in purchases:
                result = await session.execute(
                    select(Account).where(Account.id == purchase.account_id)
                )
                account = result.scalar_one_or_none()
                phone = account.phone if account else "Н/Д"
                country = account.country if account else "Н/Д"
                flag = COUNTRY_FLAGS.get(country, "")
                date = purchase.created_at.strftime('%d.%m.%y %H:%M')

                text += (
                    f'📱 <code>{phone}</code>\n'
                    f'   {flag} {country} • {purchase.amount:.0f}₽ • {date}\n\n'
                )

                builder.row(
                    create_button("Код", callback_data=f"get_code_{purchase.id}", style="primary", icon="code"),
                    create_button(".session", callback_data=f"get_session_{purchase.id}", style="default", icon="file"),
                    create_button("JSON", callback_data=f"get_json_{purchase.id}", style="default", icon="json")
                )

            builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())
        else:
            text = (
                f'{emoji("box")} <b>Мои покупки</b>\n\n'
                'У вас пока нет покупок.\n\n'
                f'{emoji("buy")} <b>Купите свой первый аккаунт!</b>\n'
                f'{emoji("location")} Доступны аккаунты из 6 стран'
            )

            builder = InlineKeyboardBuilder()
            builder.row(create_button("Купить аккаунт", callback_data="buy_account", style="success", icon="buy"))
            builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))
            await send_media_message(callback, "my_purchases", text, builder.as_markup())


# ===== ПРОФИЛЬ =====

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    """Профиль пользователя"""
    await callback.answer()

    if not await require_subscription(callback):
        return

    user = await get_user(callback.from_user.id)

    if not user:
        await callback.message.answer(
            f'{emoji("cross")} <b>Пользователь не найден</b>\n\n'
            'Пожалуйста, перезапустите бота командой /start',
            reply_markup=main_menu_keyboard()
        )
        return

    # Считаем количество покупок
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Purchase.id))
            .where(Purchase.user_id == callback.from_user.id)
        )
        purchases_count = result.scalar() or 0

        # Считаем общую сумму покупок
        result = await session.execute(
            select(func.sum(Purchase.amount))
            .where(Purchase.user_id == callback.from_user.id)
        )
        total_purchases = result.scalar() or 0

    # Дата регистрации
    reg_date = user.created_at.strftime('%d.%m.%Y')
    days_ago = (datetime.utcnow() - user.created_at).days

    text = (
        f'{emoji("profile")} <b>Профиль пользователя</b>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji("tag")} <b>ID:</b> <code>{user.telegram_id}</code>\n'
        f'{emoji("profile")} <b>Username:</b> @{user.username or "не указан"}\n'
        f'{emoji("clock")} <b>С нами:</b> {reg_date} ({days_ago} дн.)\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'━━━ 💰 БАЛАНС ━━━\n'
        f'{emoji("wallet")} <b>{user.balance:.0f}₽</b>\n'
        f'{emoji("hold")} <b>В холде:</b> <code>{user.hold_balance or 0:.0f}₽</code>\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'━━ 📊 СТАТИСТИКА ━━\n'
        f'{emoji("box")} <b>Покупок:</b> {purchases_count} шт.\n'
        f'{emoji("money")} <b>Потрачено:</b> {total_purchases:.0f}₽\n'
        f'{emoji("sell")} <b>Заработано:</b> {user.total_earned or 0:.0f}₽\n'
        f'━━━━━━━━━━━━━━━━━━'
    )
    await send_media_message(callback, "profile", text, profile_keyboard())


# ===== ПОПОЛНЕНИЕ БАЛАНСА =====

@router.callback_query(F.data == "deposit_balance")
async def cb_deposit_balance(callback: CallbackQuery):
    """Меню пополнения баланса"""
    await callback.answer()

    if not await require_subscription(callback):
        return

    user = await get_user(callback.from_user.id)
    balance = user.balance if user else 0

    text = (
        f'{emoji("wallet")} <b>Пополнение баланса</b>\n\n'
        f'{emoji("wallet")} Текущий баланс: <b>{balance:.0f}₽</b>\n\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'{emoji("sbp")} <b>СБП</b> - перевод по номеру телефона\n'
        f'  • Мгновенное зачисление после проверки\n\n'
        f'{emoji("crypto")} <b>Crypto Bot</b> - криптовалютой USDT\n'
        f'  • Автоматическое зачисление\n\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'{emoji("info")} <i>Минимальная сумма пополнения: 10₽</i>\n'
        f'{emoji("clock")} <i>СБП проверяется администратором вручную</i>'
    )
    await send_media_message(callback, "deposit", text, deposit_keyboard())


@router.callback_query(F.data == "deposit_sbp")
async def cb_deposit_sbp(callback: CallbackQuery):
    """Запрос суммы для пополнения через СБП"""
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="deposit_balance", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("sbp")} <b>Пополнение через СБП</b>\n\n'
        f'{emoji("money")} Введите сумму пополнения (от 10₽):\n\n'
        '<i>Отправьте число в чат</i>',
        reply_markup=builder.as_markup()
    )

    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'


@router.callback_query(F.data == "deposit_crypto")
async def cb_deposit_crypto(callback: CallbackQuery):
    """Запрос суммы для пополнения через Crypto Bot"""
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="deposit_balance", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("crypto")} <b>Пополнение через Crypto Bot</b>\n\n'
        f'{emoji("money")} Введите сумму пополнения (от 10₽):\n\n'
        '<i>Отправьте число в чат</i>',
        reply_markup=builder.as_markup()
    )

    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'


# ===== АКТИВАЦИЯ ПРОМОКОДА =====

@router.callback_query(F.data == "activate_promo")
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    """Запрос промокода"""
    await callback.answer()

    await state.set_state(PromoStates.waiting_for_promo_code)

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="profile", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("promo")} <b>Активация промокода</b>\n\n'
        f'{emoji("info")} Введите промокод для получения бонуса:\n\n'
        '<i>Отправьте код в чат</i>',
        reply_markup=builder.as_markup()
    )


# ===== СБП ПЛАТЕЖИ =====

@router.callback_query(F.data.startswith("sbp_paid_"))
async def cb_sbp_paid(callback: CallbackQuery, state: FSMContext):
    """Нажата кнопка 'Я оплатил' для СБП"""
    await callback.answer()

    payment_id = callback.data.replace("sbp_paid_", "")

    await state.set_state(SBPStates.waiting_for_screenshot)
    await state.update_data(payment_id=payment_id)

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="main_menu", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("photo")} <b>Отправьте скриншот оплаты</b>\n\n'
        'Сделайте скриншот перевода и отправьте его сюда.\n'
        'Администратор проверит платеж и зачислит средства.\n\n'
        f'{emoji("info")} <i>На скриншоте должно быть видно:\n'
        '- Сумму перевода\n'
        '- Номер получателя\n'
        '- Дату и время</i>',
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.startswith("sbp_approve_"))
async def cb_sbp_approve(callback: CallbackQuery):
    """Админ одобряет СБП платеж"""
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        return

    parts = callback.data.replace("sbp_approve_", "").rsplit("_", 1)
    payment_id = parts[0]
    user_id = int(parts[1])

    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.payment_id == payment_id)
        )
        payment = result.scalar_one_or_none()

        if payment and payment.status != "completed":
            payment.status = "completed"

            # Ищем пользователя по telegram_id
            result = await session.execute(
                select(User).where(User.telegram_id == user_id)
            )
            user = result.scalar_one_or_none()

            if user:
                old_balance = user.balance
                user.balance += payment.amount
                new_balance = user.balance
                await session.commit()

                # Обновляем сообщение админу
                await callback.message.edit_caption(
                    f'{callback.message.caption}\n\n'
                    f'{emoji("check")} <b>ОДОБРЕНО</b>\n'
                    f'💰 Баланс пользователя: <b>{old_balance:.0f}₽ → {new_balance:.0f}₽</b>',
                    reply_markup=None
                )

                # Уведомляем пользователя
                try:
                    builder = InlineKeyboardBuilder()
                    builder.row(create_button("Купить аккаунт", callback_data="buy_account", style="success", icon="buy"))
                    builder.row(create_button("В меню", callback_data="main_menu", style="default", icon="home"))

                    await bot.send_message(
                        user_id,
                        f'{emoji("check")} <b>Платеж одобрен!</b>\n\n'
                        f'{emoji("money")} Зачислено: <b>{payment.amount}₽</b>\n'
                        f'{emoji("wallet")} Ваш баланс: <b>{new_balance:.0f}₽</b>\n\n'
                        '<i>Средства зачислены на баланс бота</i>',
                        reply_markup=builder.as_markup()
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {user_id}: {e}")


@router.callback_query(F.data.startswith("sbp_reject_"))
async def cb_sbp_reject(callback: CallbackQuery):
    """Админ отклоняет СБП платеж"""
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        return

    parts = callback.data.replace("sbp_reject_", "").rsplit("_", 1)
    payment_id = parts[0]
    user_id = int(parts[1])

    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.payment_id == payment_id)
        )
        payment = result.scalar_one_or_none()

        if payment:
            payment.status = "rejected"
            await session.commit()

            await callback.message.edit_caption(
                f'{callback.message.caption}\n\n'
                f'{emoji("cross")} <b>ОТКЛОНЕНО</b>',
                reply_markup=None
            )

            try:
                await bot.send_message(
                    user_id,
                    f'{emoji("cross")} <b>Платеж отклонен</b>\n\n'
                    'К сожалению, ваш платеж не прошел проверку.\n'
                    'Свяжитесь с поддержкой: <b>@VestGameSupport</b>'
                )
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")


@router.callback_query(F.data == "admin_sbp_check")
async def cb_admin_sbp_check(callback: CallbackQuery):
    """Админ проверяет список СБП платежей"""
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        return

    async with async_session() as session:
        result = await session.execute(
            select(Payment)
            .where(
                Payment.method == "sbp",
                Payment.status == "pending",
                Payment.screenshot_file_id != None
            )
            .order_by(Payment.created_at.desc())
            .limit(10)
        )
        payments = result.scalars().all()

        if payments:
            await callback.message.answer(
                f'{emoji("sbp")} <b>Загружаю платежи на проверку...</b>\n\n'
                f'Найдено платежей: <b>{len(payments)}</b>'
            )

            for payment in payments:
                user = await get_user(payment.user_id)
                if user:
                    try:
                        await bot.send_photo(
                            callback.from_user.id,
                            payment.screenshot_file_id,
                            caption=(
                                f'{emoji("sbp")} <b>СБП платеж</b>\n\n'
                                f'{emoji("profile")} ID пользователя: <code>{payment.user_id}</code>\n'
                                f'Username: @{user.username or "нет"}\n'
                                f'{emoji("money")} Сумма: <b>{payment.amount}₽</b>\n'
                                f'{emoji("wallet")} Баланс пользователя: <b>{user.balance:.0f}₽</b>\n'
                                f'{emoji("clock")} Создан: {payment.created_at.strftime("%d.%m.%y %H:%M")}\n'
                                f'{emoji("info")} ID платежа: <code>{payment.payment_id}</code>'
                            ),
                            reply_markup=sbp_approve_keyboard(payment.payment_id, payment.user_id)
                        )
                    except Exception as e:
                        logger.error(f"Failed to send payment to admin: {e}")

            await callback.message.answer(
                f'{emoji("info")} <b>Проверьте платежи выше</b>\n\n'
                'Нажмите "Одобрить" или "Отклонить" под каждым скриншотом.',
                reply_markup=admin_keyboard()
            )
        else:
            await callback.message.answer(
                f'{emoji("info")} <b>Нет платежей для проверки</b>\n\n'
                'Все скриншоты обработаны.',
                reply_markup=admin_keyboard()
            )


# ===== АДМИН-ПАНЕЛЬ =====

@router.callback_query(F.data == "admin")
async def cb_admin_return(callback: CallbackQuery):
    """Возврат в админ-панель"""
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        return

    await callback.message.answer(
        f'{emoji("stats")} <b>Админ-панель Vest Account</b>',
        reply_markup=admin_keyboard()
    )


@router.callback_query(F.data.startswith("admin_"))
async def cb_admin(callback: CallbackQuery, state: FSMContext):
    """Обработчики админ-панели"""
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    data = callback.data

    # --- Статистика ---
    if data == "admin_stats":
        async with async_session() as session:
            # Общая статистика
            users_count = (await session.execute(select(func.count(User.id)))).scalar() or 0
            accounts_total = (await session.execute(select(func.count(Account.id)))).scalar() or 0
            accounts_sold = (await session.execute(
                select(func.count(Account.id)).where(Account.is_sold == True)
            )).scalar() or 0
            accounts_verified = (await session.execute(
                select(func.count(Account.id)).where(Account.is_verified == True)
            )).scalar() or 0
            accounts_available = (await session.execute(
                select(func.count(Account.id)).where(Account.is_sold == False, Account.is_verified == True)
            )).scalar() or 0
            purchases_count = (await session.execute(select(func.count(Purchase.id)))).scalar() or 0
            total_revenue = (await session.execute(select(func.sum(Purchase.amount)))).scalar() or 0

            # Статистика по странам
            result = await session.execute(
                select(Account.country, func.count(Account.id))
                .where(Account.is_sold == False, Account.is_verified == True)
                .group_by(Account.country)
                .order_by(Account.country)
            )
            country_stats = result.all()

            text = (
                f'{emoji("stats")} <b>📊 Статистика бота</b>\n\n'
                f'━━━ 👥 ПОЛЬЗОВАТЕЛИ ━━━\n'
                f'{emoji("profile")} Всего: <b>{users_count}</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n\n'
                f'━━━ 📦 АККАУНТЫ ━━━\n'
                f'{emoji("box")} Всего в базе: <b>{accounts_total}</b>\n'
                f'{emoji("check")} Верифицировано: <b>{accounts_verified}</b>\n'
                f'{emoji("buy")} Продано: <b>{accounts_sold}</b>\n'
                f'{emoji("location")} Доступно: <b>{accounts_available}</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n\n'
                f'━━━ 💰 ФИНАНСЫ ━━━\n'
                f'{emoji("box")} Всего покупок: <b>{purchases_count}</b>\n'
                f'{emoji("money")} Выручка: <b>{total_revenue:.0f}₽</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n\n'
                f'{emoji("location")} <b>Доступные аккаунты по странам:</b>\n'
            )

            if country_stats:
                for country, count in country_stats:
                    flag = COUNTRY_FLAGS.get(country, "")
                    text += f'{flag} {country}: <b>{count} шт.</b>\n'
            else:
                text += 'Нет доступных аккаунтов\n'

            builder = InlineKeyboardBuilder()
            builder.row(create_button("Обновить", callback_data="admin_stats", style="primary", icon="loading"))
            builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
            await callback.message.answer(text, reply_markup=builder.as_markup())

    # --- Пользователи ---
    elif data == "admin_users":
        async with async_session() as session:
            result = await session.execute(
                select(User).order_by(User.created_at.desc()).limit(20)
            )
            users = result.scalars().all()

            text = f'{emoji("users")} <b>Пользователи (последние 20)</b>\n\n'
            for user in users:
                admin_badge = " 👑" if user.is_admin else ""
                text += (
                    f'<code>{user.telegram_id}</code> | '
                    f'@{user.username or "нет"}{admin_badge} | '
                    f'{user.balance:.0f}₽ | '
                    f'{user.created_at.strftime("%d.%m")}\n'
                )

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())

    # --- Аккаунты ---
    elif data == "admin_accounts_list":
        async with async_session() as session:
            result = await session.execute(
                select(Account).order_by(Account.created_at.desc()).limit(20)
            )
            accounts = result.scalars().all()

            text = f'{emoji("box")} <b>Аккаунты (последние 20)</b>\n\n'
            for account in accounts:
                status = "✅" if account.is_verified else "⏳"
                sold = "🔴 ПРОДАН" if account.is_sold else "🟢 в наличии"
                flag = COUNTRY_FLAGS.get(account.country, "")
                text += (
                    f'{status} <code>{account.phone}</code> | '
                    f'{flag} {account.country} | '
                    f'{account.price:.0f}₽ | '
                    f'{sold}\n'
                )

        builder = InlineKeyboardBuilder()
        builder.row(create_button("🗑️ Удалить аккаунт", callback_data="admin_delete_account", style="danger", icon="delete"))
        builder.row(create_button("Назад", callback_data="admin", style="danger", icon="back"))
        await callback.message.answer(text, reply_markup=builder.as_markup())

    # --- Удаление аккаунта ---
    elif data == "admin_delete_account":
        await callback.message.answer(
            f'{emoji("delete")} <b>🗑️ Удаление аккаунта</b>\n\n'
            'Отправьте номер телефона для удаления:\n\n'
            '<i>Формат: +79001234567</i>'
        )
        if not hasattr(dp, 'awaiting_delete_account'):
            dp.awaiting_delete_account = set()
        dp.awaiting_delete_account.add(callback.from_user.id)

    # --- Рассылка ---
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'):
            dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", icon="cross"))

        await callback.message.answer(
            f'{emoji("broadcast")} <b>📢 Рассылка</b>\n\n'
            'Отправьте сообщение, которое нужно разослать всем пользователям.\n\n'
            'Поддерживаются:\n'
            '• Текст\n'
            '• Фото\n'
            '• Видео\n'
            '• Документы\n\n'
            '<i>Сообщение будет отправлено всем пользователям бота</i>',
            reply_markup=builder.as_markup()
        )

    # --- Добавление аккаунта через код ---
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", icon="cross"))

        await callback.message.answer(
            f'{emoji("add")} <b>📱 Добавление аккаунта через код</b>\n\n'
            'Отправьте номер телефона в формате:\n'
            '<code>+79001234567</code>\n\n'
            f'{emoji("info")} <i>Страна и цена определятся автоматически\n'
            'Бот отправит код подтверждения на номер</i>',
            reply_markup=builder.as_markup()
        )

    # --- Добавление аккаунта через .session файл ---
    elif data == "admin_add_session":
        await state.set_state(SessionFileStates.waiting_for_session_file)

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", icon="cross"))

        await callback.message.answer(
            f'{emoji("session")} <b>📁 Добавление через .session файл</b>\n\n'
            'Отправьте файл .session\n\n'
            f'{emoji("auto")} <b>Бот автоматически:</b>\n'
            '1. Проверит валидность сессии\n'
            '2. Определит номер телефона\n'
            '3. Определит страну по коду\n'
            '4. Сохранит сессию и JSON\n'
            '5. Установит цену для страны',
            reply_markup=builder.as_markup()
        )

    # --- Управление балансом ---
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'):
            dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}

        builder = InlineKeyboardBuilder()
        builder.row(create_button("Отмена", callback_data="admin", style="danger", icon="cross"))

        await callback.message.answer(
            f'{emoji("edit")} <b>💰 Изменение баланса пользователя</b>\n\n'
            'Отправьте ID пользователя:\n\n'
            '<i>Можно получить в профиле пользователя или в списке пользователей</i>',
            reply_markup=builder.as_markup()
        )

    # --- Цены ---
    elif data == "admin_prices":
        await callback.message.answer(
            f'{emoji("settings")} <b>💵 Цены на аккаунты</b>\n\n'
            'Выберите страну для изменения цены:\n\n'
            f'{emoji("info")} <i>Цены применяются ко всем новым аккаунтам</i>',
            reply_markup=await price_settings_keyboard()
        )

    # --- Промокоды ---
    elif data == "admin_promo_menu":
        await callback.message.answer(
            f'{emoji("promo")} <b>🎟️ Управление промокодами</b>\n\n'
            'Создавайте и управляйте промокодами для пользователей.',
            reply_markup=promo_admin_keyboard()
        )

    # --- Медиа ---
    elif data == "admin_media_menu":
        await callback.message.answer(
            f'{emoji("media")} <b>🖼️ Управление медиа</b>\n\n'
            'Выберите раздел для установки медиа-контента:\n\n'
            '<i>Медиа будет отображаться вместо обычного текста в выбранном разделе</i>',
            reply_markup=media_menu_keyboard()
        )

    # --- Очистка медиа ---
    elif data == "admin_clear_media":
        async with async_session() as session:
            await session.execute(sa_text("DELETE FROM media_settings"))
            await session.commit()

        await callback.message.answer(
            f'{emoji("check")} <b>✅ Все медиа удалены!</b>\n\n'
            'Теперь все разделы будут отображаться в текстовом режиме.',
            reply_markup=admin_keyboard()
        )

    # --- Обязательные каналы ---
    elif data == "admin_channels_menu":
        await callback.message.answer(
            f'{emoji("channel")} <b>📢 Обязательные каналы</b>\n\n'
            'Управление каналами для обязательной подписки пользователей.',
            reply_markup=channels_admin_keyboard()
        )


# ===== ОТДЕЛЬНЫЕ ОБРАБОТЧИКИ (не admin_) =====

# --- Промокоды ---
@router.callback_query(F.data == "promo_create")
async def cb_promo_create(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    await state.set_state(PromoStates.waiting_for_promo_data)

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_promo_menu", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("promo")} <b>🎟️ Создание промокода</b>\n\n'
        'Формат: <code>КОД СУММА КОЛВО</code>\n'
        'Пример: <code>HELLO 50 10</code>\n\n'
        '<i>КОД - текст промокода (без пробелов)\n'
        'СУММА - сумма начисления в ₽\n'
        'КОЛВО - максимальное количество активаций</i>',
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "promo_list")
async def cb_promo_list(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    async with async_session() as session:
        result = await session.execute(
            select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)
        )
        promos = result.scalars().all()

        text = f'{emoji("promo")} <b>🎟️ Список промокодов</b>\n\n'
        if promos:
            for promo in promos:
                status = "✅" if promo.is_active else "❌"
                text += (
                    f'<code>{promo.code}</code> | '
                    f'{promo.amount}₽ | '
                    f'{promo.used_count}/{promo.max_uses} | '
                    f'{status}\n'
                )
        else:
            text += 'Нет созданных промокодов'

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", icon="back"))
    await callback.message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "promo_delete_menu")
async def cb_promo_delete_menu(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    async with async_session() as session:
        result = await session.execute(
            select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)
        )
        promos = result.scalars().all()

        if not promos:
            await callback.message.answer(
                f'{emoji("info")} Нет промокодов для удаления',
                reply_markup=promo_admin_keyboard()
            )
            return

        builder = InlineKeyboardBuilder()
        for promo in promos:
            builder.row(create_button(
                f"❌ {promo.code} ({promo.amount}₽)",
                callback_data=f"promo_delete_{promo.id}",
                style="danger",
                icon="delete"
            ))
        builder.row(create_button("Назад", callback_data="admin_promo_menu", style="danger", icon="back"))

        await callback.message.answer(
            f'{emoji("delete")} <b>Выберите промокод для удаления:</b>',
            reply_markup=builder.as_markup()
        )


@router.callback_query(F.data.startswith("promo_delete_"))
async def cb_promo_delete(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    promo_id = int(callback.data.replace("promo_delete_", ""))

    async with async_session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo:
            code = promo.code
            await session.delete(promo)
            await session.commit()
            await callback.message.answer(
                f'{emoji("check")} <b>Промокод {code} удален!</b>',
                reply_markup=promo_admin_keyboard()
            )
        else:
            await callback.answer("Промокод не найден", show_alert=True)


# --- Каналы ---
@router.callback_query(F.data == "channel_add")
async def cb_channel_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    await state.set_state(ChannelStates.waiting_for_channel)

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_channels_menu", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("channel")} <b>📢 Добавление канала</b>\n\n'
        'Отправьте @username или ссылку на канал:\n'
        '<code>@durov</code> или <code>https://t.me/durov</code>\n\n'
        '<i>Бот должен быть администратором канала для проверки подписки!</i>',
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "channel_list")
async def cb_channel_list(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    async with async_session() as session:
        result = await session.execute(select(RequiredChannel))
        channels = result.scalars().all()

        text = f'{emoji("channel")} <b>📢 Список обязательных каналов</b>\n\n'
        if channels:
            for channel in channels:
                text += (
                    f'📢 {channel.channel_name or channel.channel_id}\n'
                    f'   {channel.channel_url}\n\n'
                )
        else:
            text += 'Нет обязательных каналов\n\n'
            text += '<i>Добавьте каналы для включения проверки подписки</i>'

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", icon="back"))
    await callback.message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "channel_delete")
async def cb_channel_delete(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    async with async_session() as session:
        result = await session.execute(select(RequiredChannel))
        channels = result.scalars().all()

        if not channels:
            await callback.message.answer(
                f'{emoji("info")} Нет каналов для удаления',
                reply_markup=channels_admin_keyboard()
            )
            return

        builder = InlineKeyboardBuilder()
        for channel in channels:
            builder.row(create_button(
                f"❌ {channel.channel_name or channel.channel_id}",
                callback_data=f"channel_del_{channel.id}",
                style="danger",
                icon="delete"
            ))
        builder.row(create_button("Назад", callback_data="admin_channels_menu", style="danger", icon="back"))

        await callback.message.answer(
            f'{emoji("delete")} <b>Выберите канал для удаления:</b>',
            reply_markup=builder.as_markup()
        )


@router.callback_query(F.data.startswith("channel_del_"))
async def cb_channel_del(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    channel_id = int(callback.data.replace("channel_del_", ""))

    async with async_session() as session:
        channel = await session.get(RequiredChannel, channel_id)
        if channel:
            await session.delete(channel)
            await session.commit()
            await callback.message.answer(
                f'{emoji("check")} <b>Канал удален!</b>',
                reply_markup=channels_admin_keyboard()
            )


# --- Цены ---
@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    country = callback.data.replace("set_price_", "")
    await state.set_state(PriceStates.waiting_for_price)
    await state.update_data(country=country)

    current_price = await get_country_price(country)

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_prices", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("edit")} <b>✏️ Изменение цены</b>\n\n'
        f'Страна: <b>{country}</b>\n'
        f'Текущая цена: <b>{current_price:.0f}₽</b>\n\n'
        'Отправьте новую цену (только число):\n\n'
        '<i>Например: 25.50</i>',
        reply_markup=builder.as_markup()
    )


# --- Медиа ---
@router.callback_query(F.data.startswith("set_media_"))
async def cb_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    section = callback.data.replace("set_media_", "")
    await state.set_state(MediaStates.waiting_for_media)
    await state.update_data(section=section)

    section_names = {
        "main_menu": "Главное меню",
        "buy_account": "Покупка аккаунта",
        "payment_methods": "Способы оплаты",
        "profile": "Профиль",
        "my_purchases": "Мои покупки",
        "deposit": "Пополнение баланса",
    }

    builder = InlineKeyboardBuilder()
    builder.row(create_button("Отмена", callback_data="admin_media_menu", style="danger", icon="cross"))

    await callback.message.answer(
        f'{emoji("media")} <b>🖼️ Установка медиа</b>\n\n'
        f'Раздел: <b>{section_names.get(section, section)}</b>\n\n'
        'Отправьте фото, видео или GIF.\n\n'
        '<i>Можно добавить подпись в тексте сообщения с медиа</i>',
        reply_markup=builder.as_markup()
    )


# ===== ОБРАБОТЧИКИ FSM (состояний) =====

@router.message(StateFilter(SessionFileStates.waiting_for_session_file), F.document)
async def h_session_file(message: Message, state: FSMContext):
    """Обработчик загрузки .session файла"""
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    # Проверяем расширение файла
    if not message.document.file_name or not message.document.file_name.endswith('.session'):
        await message.answer(
            f'{emoji("cross")} <b>❌ Отправьте файл с расширением .session!</b>',
            reply_markup=admin_keyboard()
        )
        return

    status_msg = await message.answer(
        f'{emoji("loading")} <b>🔍 Проверяю сессию...</b>\n\n'
        f'📁 Файл: <code>{message.document.file_name}</code>\n'
        f'📊 Размер: <code>{message.document.file_size} байт</code>\n\n'
        '<i>Подключаюсь к Telegram для проверки...</i>'
    )

    # Скачиваем файл
    try:
        file = await bot.get_file(message.document.file_id)
        file_content = await bot.download_file(file.file_path)
        file_bytes = file_content.read()
    except Exception as e:
        await status_msg.delete()
        await message.answer(
            f'{emoji("cross")} <b>❌ Ошибка загрузки файла:</b>\n{str(e)[:100]}',
            reply_markup=admin_keyboard()
        )
        return

    # Проверяем сессию
    result = await verify_session_file(file_bytes)

    await status_msg.delete()

    if result['success']:
        phone = result['phone']
        country = result['country']
        session_string = result['session_string']
        session_json = result['session_json']

        price = await get_country_price(country)

        # Сохраняем в БД
        async with async_session() as session:
            existing = await session.execute(
                select(Account).where(Account.phone == phone)
            )
            existing_account = existing.scalar_one_or_none()

            if existing_account:
                existing_account.session_string = session_string
                existing_account.session_json = session_json
                existing_account.is_verified = True
                existing_account.is_sold = False
                existing_account.country = country
                existing_account.price = price
            else:
                session.add(Account(
                    phone=phone,
                    country=country,
                    price=price,
                    session_string=session_string,
                    session_json=session_json,
                    is_verified=True,
                    is_sold=False
                ))

            await session.commit()

        flag = COUNTRY_FLAGS.get(country, "")

        await message.answer(
            f'{emoji("check")} <b>✅ Аккаунт успешно добавлен!</b>\n\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'{emoji("tag")} Номер: <code>{phone}</code>\n'
            f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
            f'{emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
            f'{emoji("check")} Статус: верифицирован\n'
            f'━━━━━━━━━━━━━━━━━━\n\n'
            '<i>Аккаунт доступен для покупки</i>',
            reply_markup=admin_keyboard()
        )
    else:
        await message.answer(
            f'{emoji("cross")} <b>❌ Ошибка проверки сессии</b>\n\n'
            f'{result["error"]}\n\n'
            'Возможные причины:\n'
            '• Файл поврежден или неполный\n'
            '• Сессия недействительна (аккаунт разлогинен)\n'
            '• Аккаунт заблокирован Telegram\n'
            '• Неверный формат файла\n\n'
            '<i>Проверьте файл и попробуйте снова</i>',
            reply_markup=admin_keyboard()
        )


@router.message(StateFilter(PromoStates.waiting_for_promo_code), F.text)
async def h_activate_promo(message: Message, state: FSMContext):
    """Активация промокода пользователем"""
    await state.clear()

    code = message.text.strip().upper()

    async with async_session() as session:
        # Ищем промокод
        result = await session.execute(
            select(PromoCode).where(
                PromoCode.code == code,
                PromoCode.is_active == True
            )
        )
        promo = result.scalar_one_or_none()

        if not promo:
            await message.answer(
                f'{emoji("cross")} <b>❌ Промокод не найден или неактивен</b>\n\n'
                'Проверьте правильность кода.',
                reply_markup=profile_keyboard()
            )
            return

        # Проверяем количество использований
        if promo.used_count >= promo.max_uses:
            await message.answer(
                f'{emoji("cross")} <b>❌ Промокод исчерпан</b>\n\n'
                'Количество активаций закончилось.',
                reply_markup=profile_keyboard()
            )
            return

        # Проверяем не использовал ли уже пользователь
        result = await session.execute(
            select(PromoUsage).where(
                PromoUsage.user_id == message.from_user.id,
                PromoUsage.promo_id == promo.id
            )
        )
        if result.scalar_one_or_none():
            await message.answer(
                f'{emoji("cross")} <b>❌ Вы уже использовали этот промокод</b>',
                reply_markup=profile_keyboard()
            )
            return

        # Активируем промокод
        promo.used_count += 1
        session.add(PromoUsage(user_id=message.from_user.id, promo_id=promo.id))

        # Ищем пользователя по telegram_id
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            old_balance = user.balance
            user.balance += promo.amount
            await session.commit()

            await message.answer(
                f'{emoji("check")} <b>✅ Промокод активирован!</b>\n\n'
                f'Код: <code>{promo.code}</code>\n'
                f'{emoji("money")} Зачислено: <b>{promo.amount}₽</b>\n'
                f'{emoji("wallet")} Баланс: <b>{old_balance:.0f}₽ → {user.balance:.0f}₽</b>',
                reply_markup=profile_keyboard()
            )


@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def h_sbp_screenshot(message: Message, state: FSMContext):
    """Обработчик скриншота СБП"""
    data = await state.get_data()
    payment_id = data.get('payment_id')
    await state.clear()

    file_id = message.photo[-1].file_id

    # Сохраняем скриншот в БД
    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.payment_id == payment_id)
        )
        payment = result.scalar_one_or_none()
        if payment:
            payment.screenshot_file_id = file_id
            await session.commit()

    await message.answer(
        f'{emoji("check")} <b>✅ Скриншот отправлен на проверку!</b>\n\n'
        'Администратор проверит ваш платеж и зачислит средства.\n'
        f'{emoji("clock")} <i>Обычно проверка занимает до 15 минут</i>',
        reply_markup=main_menu_keyboard()
    )

    # Отправляем админам на проверку
    async with async_session() as session:
        result = await session.execute(
            select(Payment).where(Payment.payment_id == payment_id)
        )
        payment = result.scalar_one_or_none()
        if payment:
            user = await get_user(payment.user_id)
            if user:
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_photo(
                            admin_id,
                            file_id,
                            caption=(
                                f'{emoji("sbp")} <b>💳 Новый СБП платеж</b>\n\n'
                                f'{emoji("profile")} ID: <code>{payment.user_id}</code>\n'
                                f'Username: @{user.username or "нет"}\n'
                                f'{emoji("money")} Сумма: <b>{payment.amount}₽</b>\n'
                                f'{emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n'
                                f'{emoji("clock")} {payment.created_at.strftime("%d.%m.%y %H:%M")}\n'
                                f'{emoji("info")} ID: <code>{payment_id}</code>'
                            ),
                            reply_markup=sbp_approve_keyboard(payment_id, payment.user_id)
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin {admin_id}: {e}")


@router.message(StateFilter(PriceStates.waiting_for_price), F.text)
async def h_set_price(message: Message, state: FSMContext):
    """Установка цены админом"""
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    country = data.get('country')
    await state.clear()

    try:
        price = float(message.text.strip().replace(',', '.'))
        if price <= 0:
            await message.answer(
                f'{emoji("cross")} <b>❌ Цена должна быть больше 0</b>',
                reply_markup=admin_keyboard()
            )
            return

        await set_country_price(country, price)
        await message.answer(
            f'{emoji("check")} <b>✅ Цена обновлена!</b>\n\n'
            f'Страна: <b>{country}</b>\n'
            f'Новая цена: <b>{price:.0f}₽</b>',
            reply_markup=admin_keyboard()
        )
    except ValueError:
        await message.answer(
            f'{emoji("cross")} <b>❌ Введите корректное число</b>',
            reply_markup=admin_keyboard()
        )


@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def h_media_upload(message: Message, state: FSMContext):
    """Загрузка медиа админом"""
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

    section_names = {
        "main_menu": "Главное меню",
        "buy_account": "Покупка аккаунта",
        "payment_methods": "Способы оплаты",
        "profile": "Профиль",
        "my_purchases": "Мои покупки",
        "deposit": "Пополнение баланса",
    }

    await message.answer(
        f'{emoji("check")} <b>✅ Медиа установлено!</b>\n\n'
        f'Раздел: <b>{section_names.get(section, section)}</b>\n'
        f'Тип: <b>{file_type}</b>',
        reply_markup=admin_keyboard()
    )


@router.message(StateFilter(ChannelStates.waiting_for_channel), F.text)
async def h_add_channel(message: Message, state: FSMContext):
    """Добавление обязательного канала"""
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    text = message.text.strip()
    username = text.replace('@', '').replace('https://t.me/', '').replace('http://t.me/', '').strip('/').split()[0]
    channel_url = f"https://t.me/{username}"

    try:
        chat = await bot.get_chat(f"@{username}")

        async with async_session() as session:
            existing = await session.execute(
                select(RequiredChannel).where(RequiredChannel.channel_id == str(chat.id))
            )
            if existing.scalar_one_or_none():
                await message.answer(
                    f'{emoji("cross")} <b>❌ Канал уже добавлен!</b>',
                    reply_markup=admin_keyboard()
                )
                return

            session.add(RequiredChannel(
                channel_id=str(chat.id),
                channel_url=channel_url,
                channel_name=chat.title or username
            ))
            await session.commit()

        await message.answer(
            f'{emoji("check")} <b>✅ Канал добавлен!</b>\n\n'
            f'Название: <b>{chat.title}</b>\n'
            f'ID: <code>{chat.id}</code>\n'
            f'Ссылка: {channel_url}',
            reply_markup=admin_keyboard()
        )
    except Exception as e:
        await message.answer(
            f'{emoji("cross")} <b>❌ Не удалось добавить канал</b>\n\n'
            f'Ошибка: {str(e)[:100]}\n\n'
            'Убедитесь что:\n'
            '1. Канал существует и публичный\n'
            '2. Бот добавлен в канал как администратор\n'
            '3. Формат: @username или https://t.me/username',
            reply_markup=admin_keyboard()
        )


@router.message(StateFilter(PromoStates.waiting_for_promo_data), F.text)
async def h_create_promo(message: Message, state: FSMContext):
    """Создание промокода админом"""
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    parts = message.text.strip().split()

    if len(parts) >= 3:
        code = parts[0].upper()
        try:
            amount = float(parts[1])
            max_uses = int(parts[2])

            if amount <= 0:
                await message.answer(
                    f'{emoji("cross")} <b>❌ Сумма должна быть больше 0</b>',
                    reply_markup=admin_keyboard()
                )
                return

            async with async_session() as session:
                existing = await session.execute(
                    select(PromoCode).where(PromoCode.code == code)
                )
                if existing.scalar_one_or_none():
                    await message.answer(
                        f'{emoji("cross")} <b>❌ Промокод {code} уже существует!</b>',
                        reply_markup=admin_keyboard()
                    )
                    return

                session.add(PromoCode(code=code, amount=amount, max_uses=max_uses))
                await session.commit()

            await message.answer(
                f'{emoji("check")} <b>✅ Промокод создан!</b>\n\n'
                f'Код: <code>{code}</code>\n'
                f'Сумма: <b>{amount}₽</b>\n'
                f'Количество использований: <b>{max_uses}</b>',
                reply_markup=admin_keyboard()
            )
        except ValueError:
            await message.answer(
                f'{emoji("cross")} <b>❌ Неверный формат чисел</b>\n\n'
                'Пример: <code>HELLO 50 10</code>',
                reply_markup=admin_keyboard()
            )
    else:
        await message.answer(
            f'{emoji("cross")} <b>❌ Неверный формат</b>\n\n'
            'Формат: <code>КОД СУММА КОЛВО</code>\n'
            'Пример: <code>HELLO 50 10</code>',
            reply_markup=admin_keyboard()
        )


# ===== ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ =====

@router.message(F.text)
async def h_text(message: Message):
    """
    Универсальный обработчик текстовых сообщений.
    Обрабатывает: пополнение, удаление аккаунтов, добавление через код,
    изменение баланса, рассылку.
    """
    user_id = message.from_user.id
    text = message.text.strip()

    # ===== ПОПОЛНЕНИЕ БАЛАНСА =====
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)

        try:
            amount = float(text.replace(',', '.'))

            if amount < 10:
                await message.answer(
                    f'{emoji("cross")} <b>❌ Минимальная сумма пополнения: 10₽</b>',
                    reply_markup=deposit_keyboard()
                )
                return

            payment_id = await generate_payment_id()

            if method == "sbp":
                async with async_session() as session:
                    session.add(Payment(
                        user_id=user_id,
                        amount=amount,
                        payment_id=payment_id,
                        method="sbp",
                        status="pending",
                        type="deposit"
                    ))
                    await session.commit()

                await message.answer(
                    f'{emoji("sbp")} <b>💳 Пополнение через СБП</b>\n\n'
                    f'{emoji("money")} Сумма к оплате: <b>{amount}₽</b>\n\n'
                    f'{emoji("bank")} <b>Реквизиты для перевода:</b>\n'
                    f'📱 Телефон: <code>{SBP_PHONE}</code>\n'
                    f'🏦 Банк: <b>{SBP_BANK}</b>\n'
                    f'👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n'
                    f'{emoji("info")} ID платежа: <code>{payment_id}</code>\n\n'
                    '⚠️ <b>Важно!</b>\n'
                    '1. Переведите точную сумму\n'
                    '2. После оплаты нажмите "Я оплатил"\n'
                    '3. Отправьте скриншот перевода\n\n'
                    f'{emoji("clock")} <i>Средства будут зачислены после проверки администратором</i>',
                    reply_markup=sbp_payment_keyboard(payment_id)
                )

            elif method == "crypto":
                async with async_session() as session:
                    session.add(Payment(
                        user_id=user_id,
                        amount=amount,
                        payment_id=payment_id,
                        method="crypto",
                        status="pending",
                        type="deposit"
                    ))
                    await session.commit()

                status_msg = await message.answer(
                    f'{emoji("loading")} <b>Создаю счет в Crypto Bot...</b>\n\n'
                    'Пожалуйста, подождите.'
                )

                invoice = await create_crypto_bot_invoice(amount, payment_id)

                if invoice and invoice.get("ok"):
                    result = invoice.get("result", {})
                    pay_url = result.get("pay_url")
                    invoice_id = str(result.get("invoice_id"))

                    async with async_session() as session:
                        payment_result = await session.execute(
                            select(Payment).where(Payment.payment_id == payment_id)
                        )
                        payment = payment_result.scalar_one_or_none()
                        if payment:
                            payment.payment_id = invoice_id
                            await session.commit()

                    await status_msg.delete()

                    usdt_amount = round(amount / 90, 2)

                    await message.answer(
                        f'{emoji("crypto")} <b>🪙 Пополнение через Crypto Bot</b>\n\n'
                        f'{emoji("money")} Сумма: <b>{amount}₽</b> (~{usdt_amount} USDT)\n\n'
                        f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
                        f'{emoji("auto")} <b>Оплата проверяется автоматически!</b>\n'
                        'Вам не нужно нажимать кнопку проверки.\n'
                        'Средства зачислятся на баланс автоматически.\n\n'
                        f'{emoji("clock")} <i>Ожидание до 10 минут</i>',
                        reply_markup=deposit_crypto_check_keyboard(invoice_id),
                        disable_web_page_preview=True
                    )

                    asyncio.create_task(auto_check_crypto_payment(invoice_id, user_id))
                else:
                    await status_msg.delete()
                    await message.answer(
                        f'{emoji("cross")} <b>❌ Ошибка создания счета</b>\n\n'
                        'Попробуйте позже или используйте другой способ.',
                        reply_markup=deposit_keyboard()
                    )

        except ValueError:
            await message.answer(
                f'{emoji("cross")} <b>❌ Введите корректное число</b>'
            )
        return

    # ===== УДАЛЕНИЕ АККАУНТА (админ) =====
    if hasattr(dp, 'awaiting_delete_account') and user_id in dp.awaiting_delete_account:
        dp.awaiting_delete_account.remove(user_id)

        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.phone == text)
            )
            account = result.scalar_one_or_none()

            if account:
                phone = account.phone
                await session.delete(account)
                await session.commit()
                await message.answer(
                    f'{emoji("check")} <b>✅ Аккаунт {phone} удален!</b>',
                    reply_markup=admin_keyboard()
                )
            else:
                await message.answer(
                    f'{emoji("cross")} <b>❌ Аккаунт с номером {text} не найден</b>',
                    reply_markup=admin_keyboard()
                )
        return

    # ===== ДОБАВЛЕНИЕ АККАУНТА ЧЕРЕЗ КОД (админ) =====
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]
        step = acc_data.get('step')

        if step == 'phone':
            phone = text
            country = detect_country(phone)
            flag = COUNTRY_FLAGS.get(country, "")

            acc_data['phone'] = phone
            acc_data['country'] = country

            status_msg = await message.answer(
                f'{emoji("loading")} <b>Отправляю код на номер...</b>\n\n'
                f'📱 <code>{phone}</code>\n'
                f'{emoji("location")} Страна: {flag} <b>{country}</b>'
            )

            result = await send_code_to_phone(phone)

            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']
                acc_data['step'] = 'code'

                await status_msg.edit_text(
                    f'{emoji("check")} <b>✅ Код отправлен!</b>\n\n'
                    f'📱 Номер: <code>{phone}</code>\n'
                    f'{emoji("location")} Страна: {flag} <b>{country}</b>\n\n'
                    '📨 <b>Введите код из Telegram:</b>\n\n'
                    '<i>Код придет в личные сообщения от Telegram</i>'
                )
            else:
                del dp.awaiting_accounts[user_id]
                await status_msg.edit_text(
                    f'{emoji("cross")} <b>❌ Ошибка отправки кода</b>\n\n'
                    f'{result.get("error")}\n\n'
                    'Проверьте правильность номера.',
                    reply_markup=admin_keyboard()
                )

        elif step == 'code':
            phone = acc_data['phone']
            country = acc_data['country']
            flag = COUNTRY_FLAGS.get(country, "")
            phone_code_hash = acc_data['phone_code_hash']

            status_msg = await message.answer(
                f'{emoji("loading")} <b>Проверяю код подтверждения...</b>'
            )

            result = await verify_code_and_create_session_json(phone, text, phone_code_hash)

            if result['success']:
                price = await get_country_price(country)

                # Сохраняем в БД
                async with async_session() as session:
                    existing = await session.execute(
                        select(Account).where(Account.phone == phone)
                    )
                    existing_account = existing.scalar_one_or_none()

                    if existing_account:
                        existing_account.session_string = result['session_string']
                        existing_account.session_json = result['session_json']
                        existing_account.is_verified = True
                        existing_account.is_sold = False
                        existing_account.country = country
                        existing_account.price = price
                    else:
                        session.add(Account(
                            phone=phone,
                            country=country,
                            price=price,
                            session_string=result['session_string'],
                            session_json=result['session_json'],
                            is_verified=True,
                            is_sold=False
                        ))
                    await session.commit()

                del dp.awaiting_accounts[user_id]

                await status_msg.edit_text(
                    f'{emoji("check")} <b>✅ Аккаунт успешно добавлен!</b>\n\n'
                    f'{emoji("tag")} Номер: <code>{phone}</code>\n'
                    f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
                    f'{emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
                    f'{emoji("check")} Статус: верифицирован\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )

            elif result.get('need_password'):
                acc_data['step'] = 'password'
                await status_msg.edit_text(
                    f'{emoji("lock")} <b>🔒 Требуется 2FA пароль</b>\n\n'
                    'Введите пароль облачной защиты:'
                )

            else:
                del dp.awaiting_accounts[user_id]
                await status_msg.edit_text(
                    f'{emoji("cross")} <b>❌ Ошибка верификации</b>\n\n'
                    f'{result.get("error")}',
                    reply_markup=admin_keyboard()
                )

        elif step == 'password':
            phone = acc_data['phone']
            country = acc_data['country']
            flag = COUNTRY_FLAGS.get(country, "")

            status_msg = await message.answer(
                f'{emoji("loading")} <b>Проверяю пароль 2FA...</b>'
            )

            result = await verify_2fa_and_create_session_json(phone, text)

            if result['success']:
                price = await get_country_price(country)

                # Сохраняем в БД
                async with async_session() as session:
                    existing = await session.execute(
                        select(Account).where(Account.phone == phone)
                    )
                    existing_account = existing.scalar_one_or_none()

                    if existing_account:
                        existing_account.session_string = result['session_string']
                        existing_account.session_json = result['session_json']
                        existing_account.is_verified = True
                        existing_account.is_sold = False
                        existing_account.country = country
                        existing_account.price = price
                    else:
                        session.add(Account(
                            phone=phone,
                            country=country,
                            price=price,
                            session_string=result['session_string'],
                            session_json=result['session_json'],
                            is_verified=True,
                            is_sold=False
                        ))
                    await session.commit()

                del dp.awaiting_accounts[user_id]

                await status_msg.edit_text(
                    f'{emoji("check")} <b>✅ Аккаунт успешно добавлен!</b>\n\n'
                    f'{emoji("tag")} Номер: <code>{phone}</code>\n'
                    f'{emoji("location")} Страна: {flag} <b>{country}</b>\n'
                    f'{emoji("money")} Цена: <b>{price:.0f}₽</b>\n'
                    f'{emoji("check")} Статус: верифицирован\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            else:
                await status_msg.edit_text(
                    f'{emoji("cross")} <b>❌ {result.get("error")}</b>\n\n'
                    'Попробуйте еще раз или отмените операцию.'
                )
        return

    # ===== ИЗМЕНЕНИЕ БАЛАНСА (админ) =====
    if hasattr(dp, 'awaiting_balance') and user_id in dp.awaiting_balance:
        bal_data = dp.awaiting_balance[user_id]
        step = bal_data.get('step')

        if step == 'user_id':
            try:
                target_id = int(text)
                target_user = await get_user(target_id)

                if not target_user:
                    await message.answer(
                        f'{emoji("cross")} <b>❌ Пользователь с ID {target_id} не найден</b>',
                        reply_markup=admin_keyboard()
                    )
                    del dp.awaiting_balance[user_id]
                    return

                bal_data['target_id'] = target_id
                bal_data['step'] = 'amount'

                await message.answer(
                    f'{emoji("edit")} <b>💰 Изменение баланса</b>\n\n'
                    f'Пользователь: <code>{target_id}</code>\n'
                    f'Username: @{target_user.username or "нет"}\n'
                    f'Текущий баланс: <b>{target_user.balance:.0f}₽</b>\n\n'
                    'Отправьте сумму:\n'
                    '<code>+100</code> - пополнить на 100₽\n'
                    '<code>-50</code> - списать 50₽\n'
                    '<code>500</code> - установить 500₽'
                )
            except ValueError:
                await message.answer(
                    f'{emoji("cross")} <b>❌ Введите корректный ID пользователя</b>'
                )

        elif step == 'amount':
            try:
                value = text
                target_id = bal_data['target_id']

                async with async_session() as session:
                    result = await session.execute(
                        select(User).where(User.telegram_id == target_id)
                    )
                    target_user = result.scalar_one_or_none()

                    if not target_user:
                        del dp.awaiting_balance[user_id]
                        await message.answer(
                            f'{emoji("cross")} <b>❌ Пользователь не найден</b>',
                            reply_markup=admin_keyboard()
                        )
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
                        f'{emoji("check")} <b>✅ Баланс изменен!</b>\n\n'
                        f'Пользователь: <code>{target_id}</code>\n'
                        f'Было: <b>{old_balance:.0f}₽</b>\n'
                        f'Стало: <b>{target_user.balance:.0f}₽</b>\n'
                        f'Изменение: <b>{target_user.balance - old_balance:+.0f}₽</b>',
                        reply_markup=admin_keyboard()
                    )
            except ValueError:
                await message.answer(
                    f'{emoji("cross")} <b>❌ Введите корректную сумму</b>'
                )
        return

    # ===== РАССЫЛКА (админ) =====
    if hasattr(dp, 'awaiting_broadcast') and user_id in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(user_id)

        await message.answer(
            f'{emoji("loading")} <b>📢 Выполняю рассылку...</b>\n\n'
            'Это может занять некоторое время.'
        )

        async with async_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

            sent = 0
            total = len(users)

            for user in users:
                try:
                    await message.copy_to(chat_id=user.telegram_id)
                    sent += 1
                    await asyncio.sleep(0.05)
                except:
                    continue

        await message.answer(
            f'{emoji("check")} <b>✅ Рассылка завершена!</b>\n\n'
            f'Отправлено: <b>{sent}</b> из <b>{total}</b> пользователей\n'
            f'Не доставлено: <b>{total - sent}</b>',
            reply_markup=admin_keyboard()
        )
        return

    # ===== ОБЫЧНОЕ СООБЩЕНИЕ =====
    await message.answer(
        f'{emoji("info")} <b>i️ Используйте кнопки меню для навигации</b>\n\n'
        'Доступные команды:\n'
        '/start - главное меню\n'
        '/admin - админ-панель (для администраторов)',
        reply_markup=main_menu_keyboard()
    )


# ===== ЗАПУСК БОТА =====

async def setup_database():
    """Создание таблиц в базе данных"""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        raise


async def main():
    """Главная функция запуска бота"""
    # Инициализация базы данных
    await setup_database()
    await run_migrations()

    # Инициализация хранилищ состояний
    if not hasattr(dp, 'pending_accounts'):
        dp.pending_accounts = {}
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    if not hasattr(dp, 'awaiting_accounts'):
        dp.awaiting_accounts = {}
    if not hasattr(dp, 'awaiting_balance'):
        dp.awaiting_balance = {}
    if not hasattr(dp, 'awaiting_broadcast'):
        dp.awaiting_broadcast = set()
    if not hasattr(dp, 'awaiting_delete_account'):
        dp.awaiting_delete_account = set()

    # Подключаем роутер
    dp.include_router(router)

    # Проверяем количество аккаунтов при старте
    asyncio.create_task(check_low_accounts_and_notify())

    # Запускаем фоновый цикл релиза холдов
    asyncio.create_task(hold_releaser_loop())

    logger.info("=" * 50)
    logger.info("Vest Account Bot started!")
    logger.info(f"Admins: {ADMIN_IDS}")
    logger.info(f"Countries: {COUNTRY_NAMES}")
    logger.info(f"SBP: {SBP_PHONE} ({SBP_BANK})")
    logger.info(f"Crypto Bot: {'Configured' if CRYPTO_BOT_TOKEN else 'Not configured'}")
    logger.info(f"Low accounts threshold: {LOW_ACCOUNTS_THRESHOLD}")
    logger.info(f"Marketplace commission: {COMMISSION_PERCENT}% / hold {HOLD_PERIOD_HOURS}h")
    logger.info("=" * 50)

    # Запуск поллинга
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
