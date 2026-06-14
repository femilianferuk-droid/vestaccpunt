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
class Base(DeclarativeBase): pass

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
    session_json = Column(Text, nullable=True)
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
class MediaStates(StatesGroup): waiting_for_media = State()
class SBPStates(StatesGroup): waiting_for_screenshot = State()
class PriceStates(StatesGroup): waiting_for_price = State()
class PromoStates(StatesGroup): waiting_for_promo_data = State(); waiting_for_promo_code = State()
class ChannelStates(StatesGroup): waiting_for_channel = State()

try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
except Exception as e:
    logger.error(f"DB error: {e}"); sys.exit(1)

async def run_migrations():
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent FLOAT DEFAULT 0.0",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS session_string TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS session_json TEXT",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS type VARCHAR(50) DEFAULT 'deposit'",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS screenshot_file_id VARCHAR(255)",
    ]
    try:
        async with engine.begin() as conn:
            for m in migrations:
                try: await conn.execute(sa_text(m))
                except: pass
            await conn.commit()
    except: pass

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
pending_auth = {}

# ===== ПРЕМИУМ ЭМОДЗИ =====
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
    'reject': '5774077015388852135', 'json': '6035128606563241721',
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
    'channel': '📢', 'accept': '✅', 'reject': '❌', 'json': '📋',
}

def emoji(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI_ID.get(name, EMOJI_ID["info"])}">{EMOJI_CHAR.get(name, "📌")}</tg-emoji>'

def btn(text: str, callback_data: str = None, url: str = None, style: str = None, icon: str = None) -> InlineKeyboardButton:
    kwargs = {'text': text}
    if callback_data: kwargs['callback_data'] = callback_data
    if url: kwargs['url'] = url
    if style in ['primary', 'success', 'danger', 'default']: kwargs['style'] = style
    if icon and icon in EMOJI_ID: kwargs['icon_custom_emoji_id'] = EMOJI_ID[icon]
    return InlineKeyboardButton(**kwargs)

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("Купить аккаунт", callback_data="buy_account", style="primary", icon="buy"), btn("Мои покупки", callback_data="my_purchases", style="default", icon="box"))
    b.row(btn("Профиль", callback_data="profile", style="default", icon="profile"), btn("Пополнить", callback_data="deposit_balance", style="success", icon="wallet"))
    return b.as_markup()

async def countries_keyboard():
    b = InlineKeyboardBuilder()
    prices = await get_all_prices()
    flags, styles = {"США":"🇺🇸","Россия":"🇷🇺","Индия":"🇮🇳"}, ["primary","success","danger"]
    for i, c in enumerate(["США","Россия","Индия"]):
        b.row(btn(f"{flags[c]} {c} • {prices.get(c,20):.0f}₽", callback_data=f"country_{c}", style=styles[i], icon="location"))
    b.row(btn("Назад", callback_data="main_menu", style="default", icon="back"))
    return b.as_markup()

def account_found_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("КУПИТЬ", callback_data="show_payment_methods", style="success", icon="buy"))
    b.row(btn("Назад", callback_data="buy_account", style="default", icon="back"))
    return b.as_markup()

def payment_methods_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("Баланс бота", callback_data="pay_balance", style="primary", icon="wallet"))
    b.row(btn("СБП", callback_data="pay_sbp", style="default", icon="sbp"))
    b.row(btn("Crypto Bot", callback_data="pay_crypto", style="success", icon="crypto"))
    b.row(btn("Telegram Stars", callback_data="pay_stars", style="default", icon="star"))
    b.row(btn("Назад", callback_data="buy_account", style="default", icon="back"))
    return b.as_markup()

def check_crypto_keyboard(pid: str):
    b = InlineKeyboardBuilder()
    b.row(btn("Проверить оплату", callback_data=f"check_purchase_crypto_{pid}", style="primary", icon="loading"))
    b.row(btn("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return b.as_markup()

def get_code_keyboard(pid: int):
    b = InlineKeyboardBuilder()
    b.row(btn("Получить код", callback_data=f"get_code_{pid}", style="primary", icon="code"))
    b.row(btn("Получить .session", callback_data=f"get_session_{pid}", style="default", icon="file"))
    b.row(btn("Получить JSON", callback_data=f"get_json_{pid}", style="default", icon="json"))
    b.row(btn("К покупкам", callback_data="my_purchases", style="default", icon="box"))
    return b.as_markup()

def profile_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet"))
    b.row(btn("Мои покупки", callback_data="my_purchases", style="default", icon="box"))
    b.row(btn("Промокод", callback_data="activate_promo", style="primary", icon="promo"))
    b.row(btn("В меню", callback_data="main_menu", style="default", icon="home"))
    return b.as_markup()

def deposit_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("СБП", callback_data="deposit_sbp", style="default", icon="sbp"))
    b.row(btn("Crypto Bot", callback_data="deposit_crypto", style="success", icon="crypto"))
    b.row(btn("Назад", callback_data="profile", style="default", icon="back"))
    return b.as_markup()

def deposit_crypto_check_keyboard(pid: str):
    b = InlineKeyboardBuilder()
    b.row(btn("Проверить оплату", callback_data=f"check_deposit_crypto_{pid}", style="primary", icon="loading"))
    b.row(btn("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return b.as_markup()

def sbp_payment_keyboard(pid: str):
    b = InlineKeyboardBuilder()
    b.row(btn("Я оплатил", callback_data=f"sbp_paid_{pid}", style="success", icon="check"))
    b.row(btn("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    return b.as_markup()

def admin_keyboard():
    b = InlineKeyboardBuilder()
    btns = [
        ("Статистика","admin_stats","primary","stats"), ("Пользователи","admin_users","default","users"),
        ("Аккаунты","admin_accounts_list","default","box"), ("Рассылка","admin_broadcast","default","broadcast"),
        ("Добавить аккаунты","admin_add_accounts","success","add"), ("Управление балансом","admin_balance","default","edit"),
        ("Цены на аккаунты","admin_prices","default","money"), ("Промокоды","admin_promo_menu","default","promo"),
        ("Управление медиа","admin_media_menu","default","media"), ("Обязательные каналы","admin_channels_menu","default","channel"),
        ("Проверка СБП","admin_sbp_check","success","sbp"),
    ]
    for text, cb, style, icon in btns: b.row(btn(text, callback_data=cb, style=style, icon=icon))
    b.row(btn("В меню", callback_data="main_menu", style="danger", icon="home"))
    return b.as_markup()

def promo_admin_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("Создать промокод", callback_data="promo_create", style="success", icon="add"))
    b.row(btn("Список промокодов", callback_data="promo_list", style="default", icon="promo"))
    b.row(btn("Удалить промокод", callback_data="promo_delete_menu", style="danger", icon="delete"))
    b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
    return b.as_markup()

async def price_settings_keyboard():
    b = InlineKeyboardBuilder()
    prices = await get_all_prices()
    flags = {"США":"🇺🇸","Россия":"🇷🇺","Индия":"🇮🇳"}
    for c in ["США","Россия","Индия"]: b.row(btn(f"{flags[c]} {c}: {prices.get(c,20):.0f}₽", callback_data=f"set_price_{c}", style="default", icon="edit"))
    b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
    return b.as_markup()

def media_menu_keyboard():
    b = InlineKeyboardBuilder()
    for n, cb in [("Главное меню","main_menu"),("Покупка","buy_account"),("Оплата","payment_methods"),("Профиль","profile"),("Покупки","my_purchases"),("Пополнение","deposit")]:
        b.row(btn(n, callback_data=f"set_media_{cb}", style="default", icon="media"))
    b.row(btn("Удалить все медиа", callback_data="admin_clear_media", style="danger", icon="delete"))
    b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
    return b.as_markup()

def channels_admin_keyboard():
    b = InlineKeyboardBuilder()
    b.row(btn("Добавить канал", callback_data="channel_add", style="success", icon="add"))
    b.row(btn("Список каналов", callback_data="channel_list", style="default", icon="channel"))
    b.row(btn("Удалить канал", callback_data="channel_delete", style="danger", icon="delete"))
    b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
    return b.as_markup()

def sbp_approve_keyboard(pid: str, uid: int):
    b = InlineKeyboardBuilder()
    b.row(btn("Одобрить", callback_data=f"sbp_approve_{pid}_{uid}", style="success", icon="accept"), btn("Отклонить", callback_data=f"sbp_reject_{pid}_{uid}", style="danger", icon="reject"))
    return b.as_markup()

# ===== ПОДПИСКА =====
async def check_subscription(uid: int):
    async with async_session() as s:
        r = await s.execute(select(RequiredChannel)); chs = r.scalars().all()
    if not chs: return True, []
    ns = []
    for ch in chs:
        try:
            cid = ch.channel_id
            if not cid.startswith("-100") and cid.lstrip('-').isdigit(): cid = f"-100{cid}"
            m = await bot.get_chat_member(chat_id=cid, user_id=uid)
            if m.status in ["left","kicked"]: ns.append(ch)
        except: pass
    return len(ns)==0, ns

async def get_subscribe_keyboard(ns: list):
    b = InlineKeyboardBuilder()
    for ch in ns: b.row(btn(f"📢 {ch.channel_name or 'Канал'}", url=ch.channel_url, style="primary", icon="subscribe"))
    b.row(btn("Проверить подписку", callback_data="check_subscription", style="success", icon="loading"))
    return b.as_markup()

# ===== СТРАНА =====
def detect_country(phone: str) -> str:
    phone = phone.strip().lstrip('+')
    for code in sorted(COUNTRY_CODES, key=len, reverse=True):
        if phone.startswith(code): return COUNTRY_CODES[code]
    return "США"

# ===== ЦЕНЫ =====
async def get_country_price(country: str) -> float:
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country==country)); ps = r.scalar_one_or_none()
        if ps: return ps.price
    return DEFAULT_PRICES.get(country,20)

async def set_country_price(country: str, price: float):
    async with async_session() as s:
        r = await s.execute(select(PriceSettings).where(PriceSettings.country==country)); ps = r.scalar_one_or_none()
        if ps: ps.price=price; ps.updated_at=datetime.utcnow()
        else: s.add(PriceSettings(country=country,price=price))
        await s.commit()

async def get_all_prices() -> dict:
    prices = dict(DEFAULT_PRICES)
    async with async_session() as s:
        r = await s.execute(select(PriceSettings))
        for ps in r.scalars().all(): prices[ps.country]=ps.price
    return prices

# ===== МЕДИА =====
async def get_media(section: str):
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section==section)); return r.scalar_one_or_none()

async def set_media(section: str, fid: str, ftype: str, caption: str=None):
    async with async_session() as s:
        r = await s.execute(select(MediaSettings).where(MediaSettings.section==section)); m = r.scalar_one_or_none()
        if m: m.file_id=fid; m.file_type=ftype; m.caption=caption; m.updated_at=datetime.utcnow()
        else: s.add(MediaSettings(section=section,file_id=fid,file_type=ftype,caption=caption))
        await s.commit()

async def send_media_message(target, section: str, text: str, markup):
    media = await get_media(section)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        # Не удаляем сообщение, просто отправляем новое
        pass
    if media:
        cap = f"{text}\n\n{media.caption}" if media.caption else text
        if media.file_type=="photo": await msg.answer_photo(media.file_id, caption=cap, reply_markup=markup)
        elif media.file_type=="video": await msg.answer_video(media.file_id, caption=cap, reply_markup=markup)
        elif media.file_type=="animation": await msg.answer_animation(media.file_id, caption=cap, reply_markup=markup)
        else: await msg.answer(text, reply_markup=markup)
    else: await msg.answer(text, reply_markup=markup)

# ===== TELETHON + JSON =====
async def create_telethon_client(session_string: str=None):
    return TelegramClient(StringSession(session_string) if session_string else StringSession(), API_ID, API_HASH)

async def send_code_to_phone(phone: str) -> dict:
    try:
        client = await create_telethon_client(); await client.connect()
        sent = await client.send_code_request(phone)
        pending_auth[phone] = {'client':client,'phone_code_hash':sent.phone_code_hash,'phone':phone}
        return {'success':True,'phone_code_hash':sent.phone_code_hash}
    except Exception as e: return {'success':False,'error':str(e)}

async def verify_code_and_create_session_json(phone: str, code: str, phash: str) -> dict:
    try:
        ad = pending_auth.get(phone)
        if not ad: return {'success':False,'error':'Сессия не найдена'}
        client = ad['client']
        try: await client.sign_in(phone=phone,code=code,phone_code_hash=phash)
        except SessionPasswordNeededError: return {'success':False,'need_password':True,'error':'Требуется 2FA'}
        session_string = client.session.save()
        me = await client.get_me()
        session_json = json.dumps({"phone":phone,"session_string":session_string,"api_id":API_ID,"api_hash":API_HASH,"user_id":me.id,"username":me.username,"first_name":me.first_name,"created_at":datetime.utcnow().isoformat()},ensure_ascii=False,indent=2)
        await client.disconnect(); pending_auth.pop(phone,None)
        return {'success':True,'session_string':session_string,'session_json':session_json}
    except PhoneCodeInvalidError: return {'success':False,'error':'Неверный код'}
    except PhoneCodeExpiredError: return {'success':False,'error':'Код истек'}
    except Exception as e: return {'success':False,'error':str(e)}

async def verify_2fa_and_create_session_json(phone: str, password: str) -> dict:
    try:
        ad = pending_auth.get(phone)
        if not ad: return {'success':False,'error':'Сессия не найдена'}
        client = ad['client']; await client.sign_in(password=password)
        session_string = client.session.save()
        me = await client.get_me()
        session_json = json.dumps({"phone":phone,"session_string":session_string,"api_id":API_ID,"api_hash":API_HASH,"user_id":me.id,"username":me.username,"first_name":me.first_name,"created_at":datetime.utcnow().isoformat()},ensure_ascii=False,indent=2)
        await client.disconnect(); pending_auth.pop(phone,None)
        return {'success':True,'session_string':session_string,'session_json':session_json}
    except PasswordHashInvalidError: return {'success':False,'error':'Неверный пароль'}
    except Exception as e: return {'success':False,'error':str(e)}

async def get_code_from_session(ss: str) -> Optional[str]:
    client = None
    try:
        client = await create_telethon_client(ss); await client.connect()
        if not await client.is_user_authorized(): return None
        async for d in client.iter_dialogs():
            if d.name and any(x in (d.name or "").lower() for x in ["42777","telegram","код","code","login","verify"]):
                msgs = await client.get_messages(d,limit=10)
                for msg in msgs:
                    if msg.text:
                        codes = re.findall(r'\b\d{5}\b',msg.text)
                        if codes: return codes[0]
        async for d in client.iter_dialogs():
            msgs = await client.get_messages(d,limit=3)
            for msg in msgs:
                if msg.text:
                    codes = re.findall(r'\b\d{5}\b',msg.text)
                    if codes: return codes[0]
        return None
    except: return None
    finally:
        if client: await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
async def get_user(uid: int):
    async with async_session() as s:
        r = await s.execute(select(User).where(User.telegram_id==uid)); return r.scalar_one_or_none()

async def get_or_create_user(uid: int, username: str=None):
    u = await get_user(uid)
    if not u:
        async with async_session() as s:
            u = User(telegram_id=uid,username=username,is_admin=(uid in ADMIN_IDS)); s.add(u); await s.commit(); await s.refresh(u)
    return u

async def get_available_account(country: str=None):
    async with async_session() as s:
        q = select(Account).where(Account.is_sold==False,Account.is_verified==True,Account.session_string!=None,Account.session_string!="")
        if country: q = q.where(Account.country==country)
        r = await s.execute(q.limit(1)); return r.scalar_one_or_none()

async def get_available_countries() -> list:
    async with async_session() as s:
        r = await s.execute(select(Account.country,func.count(Account.id)).where(Account.is_sold==False,Account.is_verified==True).group_by(Account.country))
        return [(row[0],row[1]) for row in r.all()]

async def create_crypto_bot_invoice(amount: float, pid: str):
    try:
        url = "https://pay.crypt.bot/api/createInvoice"; headers = {"Crypto-Pay-API-Token":CRYPTO_BOT_TOKEN}
        payload = {"asset":"USDT","amount":str(round(amount/90,2)),"description":f"Vest #{pid}","payload":pid,"allow_comments":False,"allow_anonymous":False,"expires_in":3600}
        async with aiohttp.ClientSession() as s:
            async with s.post(url,json=payload,headers=headers,timeout=30) as resp: return await resp.json()
    except: return None

async def check_crypto_bot_invoice(inv_id: int):
    try:
        url = "https://pay.crypt.bot/api/getInvoices"; headers = {"Crypto-Pay-API-Token":CRYPTO_BOT_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(url,params={"invoice_ids":str(inv_id)},headers=headers,timeout=30) as resp:
                data = await resp.json()
                if data.get("ok") and data.get("result",{}).get("items"): return data["result"]["items"][0]
        return None
    except: return None

async def generate_payment_id(): return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

async def require_subscription(cb: CallbackQuery) -> bool:
    subbed, ns = await check_subscription(cb.from_user.id)
    if not subbed:
        await cb.message.answer(f'{emoji("subscribe")} <b>Подпишитесь на каналы:</b>', reply_markup=await get_subscribe_keyboard(ns))
        return False
    return True

# ===== ОБРАБОТЧИКИ =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    subbed, ns = await check_subscription(message.from_user.id)
    if not subbed: await message.answer(f'{emoji("subscribe")} <b>Подпишитесь на каналы</b>', reply_markup=await get_subscribe_keyboard(ns)); return
    await send_media_message(message, "main_menu", f'{emoji("bot")} <b>Vest Account</b>\n\n{emoji("lock")} Покупка аккаунтов\n{emoji("loading")} Быстро и безопасно\n\n<i>Выберите действие:</i>', main_menu_keyboard())

@router.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery):
    await callback.answer()
    subbed, ns = await check_subscription(callback.from_user.id)
    if subbed: await callback.message.answer(f'{emoji("check")} <b>Подписка проверена!</b>', reply_markup=main_menu_keyboard())
    else: await callback.message.answer(f'{emoji("cross")} <b>Вы не подписаны!</b>', reply_markup=await get_subscribe_keyboard(ns))

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: await message.answer(f'{emoji("cross")} <b>Доступ запрещен</b>'); return
    await message.answer(f'{emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    await send_media_message(callback, "main_menu", f'{emoji("home")} <b>Главное меню</b>\n\n<i>Выберите действие:</i>', main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def cb_buy_account(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    avail = await get_available_countries()
    text = f'{emoji("location")} <b>Выберите страну</b>\n\n' + (f'{emoji("check")} Доступные страны:' if avail else f'{emoji("cross")} Нет аккаунтов')
    await send_media_message(callback, "buy_account", text, await countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def cb_country(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    country = callback.data.replace("country_","")
    account = await get_available_account(country)
    if account:
        price = await get_country_price(country)
        if not hasattr(dp,'pending_accounts'): dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {'account_id':account.id,'price':price,'country':country}
        flags = {"США":"🇺🇸","Россия":"🇷🇺","Индия":"🇮🇳"}
        await callback.message.answer(f'{emoji("check")} <b>Аккаунт найден!</b>\n\n{emoji("location")} Страна: {flags.get(country,"")} <b>{country}</b>\n{emoji("money")} Цена: <b>{price:.0f}₽</b>\n\n<i>Нажмите КУПИТЬ</i>', reply_markup=account_found_keyboard())
    else: await callback.message.answer(f'{emoji("cross")} <b>Нет аккаунтов для {country}</b>', reply_markup=await countries_keyboard())

@router.callback_query(F.data == "show_payment_methods")
async def cb_show_payment(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id,{}) if hasattr(dp,'pending_accounts') else {}
    await send_media_message(callback, "payment_methods", f'{emoji("buy")} <b>Покупка</b>\n\n{emoji("money")} Сумма: <b>{pending.get("price",20):.0f}₽</b>\n\n<i>Выберите способ:</i>', payment_methods_keyboard())

@router.callback_query(F.data == "pay_balance")
async def cb_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id,{}) if hasattr(dp,'pending_accounts') else {}
    price, aid = pending.get('price',20), pending.get('account_id')
    if not aid: await callback.message.answer(f'{emoji("cross")} <b>Ошибка</b>', reply_markup=main_menu_keyboard()); return
    if user.balance >= price:
        async with async_session() as s:
            user = await s.get(User,user.id); account = await s.get(Account,aid)
            if account.is_sold: await callback.message.answer(f'{emoji("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard()); return
            user.balance -= price; user.total_spent = (user.total_spent or 0) + price; account.is_sold = True
            purchase = Purchase(user_id=callback.from_user.id,account_id=aid,amount=price,payment_method="balance")
            s.add(purchase); await s.commit(); await s.refresh(purchase)
            await callback.message.answer(f'{emoji("check")} <b>Оплата успешна!</b>\n\n{emoji("tag")} Номер: <code>{account.phone}</code>\n{emoji("money")} Сумма: <b>{price:.0f}₽</b>', reply_markup=get_code_keyboard(purchase.id))
    else: await callback.message.answer(f'{emoji("cross")} <b>Недостаточно средств</b>\n\n{emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>\n{emoji("money")} Нужно: <b>{price:.0f}₽</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_sbp")
async def cb_pay_sbp(callback: CallbackQuery):
    await callback.answer()
    b = InlineKeyboardBuilder(); b.row(btn("Пополнить баланс", callback_data="deposit_balance", style="success", icon="wallet")); b.row(btn("Назад", callback_data="show_payment_methods", style="default", icon="back"))
    await callback.message.answer(f'{emoji("sbp")} <b>Оплата через СБП</b>\n\n{emoji("info")} Для оплаты товара через СБП пополните баланс.', reply_markup=b.as_markup())

@router.callback_query(F.data == "pay_crypto")
async def cb_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id,{}) if hasattr(dp,'pending_accounts') else {}
    price = pending.get('price',20); pid = await generate_payment_id()
    async with async_session() as s:
        s.add(Payment(user_id=callback.from_user.id,amount=price,payment_id=pid,method="crypto",status="pending",type="purchase")); await s.commit()
    invoice = await create_crypto_bot_invoice(price,pid)
    if invoice and invoice.get("ok"):
        r = invoice.get("result",{})
        async with async_session() as s:
            p = await s.execute(select(Payment).where(Payment.payment_id==pid)); p = p.scalar_one_or_none()
            if p: p.payment_id=str(r.get("invoice_id")); await s.commit()
        await callback.message.answer(f'{emoji("crypto")} <b>Оплата Crypto Bot</b>\n\nСумма: <b>{price:.0f}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ После оплаты нажмите проверку', reply_markup=check_crypto_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
    else: await callback.message.answer(f'{emoji("cross")} <b>Ошибка</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{emoji("star")} <b>Оплата Telegram Stars</b>\n\nНапишите: <b>@v3estnikov</b>', reply_markup=payment_methods_keyboard())

@router.callback_query(F.data.startswith("check_purchase_crypto_"))
async def cb_check_purchase(callback: CallbackQuery):
    await callback.answer()
    pid = callback.data.replace("check_purchase_crypto_","")
    inv = await check_crypto_bot_invoice(int(pid))
    if inv and inv.get("status")=="paid":
        pending = dp.pending_accounts.get(callback.from_user.id,{}) if hasattr(dp,'pending_accounts') else {}
        aid, price = pending.get('account_id'), pending.get('price',20)
        if aid:
            async with async_session() as s:
                account = await s.get(Account,aid)
                if account.is_sold: await callback.message.answer(f'{emoji("cross")} <b>Продан</b>', reply_markup=main_menu_keyboard()); return
                pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); pr = pr.scalar_one_or_none()
                if pr: pr.status="completed"
                user = await s.get(User,callback.from_user.id)
                if user: user.total_spent = (user.total_spent or 0) + price
                account.is_sold = True
                purchase = Purchase(user_id=callback.from_user.id,account_id=aid,amount=price,payment_method="crypto")
                s.add(purchase); await s.commit(); await s.refresh(purchase)
                await callback.message.answer(f'{emoji("check")} <b>Оплата подтверждена!</b>\n\n{emoji("tag")} Номер: <code>{account.phone}</code>\n{emoji("money")} Сумма: <b>{price:.0f}₽</b>', reply_markup=get_code_keyboard(purchase.id))
    else: await callback.answer("⏳ Не найдено", show_alert=True)

@router.callback_query(F.data.startswith("check_deposit_crypto_"))
async def cb_check_deposit(callback: CallbackQuery):
    await callback.answer()
    pid = callback.data.replace("check_deposit_crypto_","")
    inv = await check_crypto_bot_invoice(int(pid))
    if inv and inv.get("status")=="paid":
        async with async_session() as s:
            pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); payment = pr.scalar_one_or_none()
            if payment and payment.status!="completed":
                payment.status="completed"; user = await s.get(User,callback.from_user.id); user.balance += payment.amount; await s.commit()
                b = InlineKeyboardBuilder(); b.row(btn("В меню", callback_data="main_menu", style="success", icon="home"))
                await callback.message.answer(f'{emoji("check")} <b>Баланс пополнен!</b>\n\n{emoji("money")} +{payment.amount:.2f}₽\n{emoji("wallet")} Баланс: <b>{user.balance:.2f}₽</b>', reply_markup=b.as_markup())
    else: await callback.answer("⏳ Не найдено", show_alert=True)

# ===== ПОЛУЧЕНИЕ ДАННЫХ =====
@router.callback_query(F.data.startswith("get_code_"))
async def cb_get_code(callback: CallbackQuery):
    await callback.answer()
    pid = int(callback.data.replace("get_code_",""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id==pid)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id!=callback.from_user.id: await callback.answer("Не найдена",show_alert=True); return
        account = await s.get(Account,purchase.account_id)
        if not account or not account.session_string: await callback.answer("Нет данных",show_alert=True); return
        st = await callback.message.answer(f'{emoji("loading")} <b>Получаю код...</b>')
        code = await get_code_from_session(account.session_string); await st.delete()
        if code:
            b = InlineKeyboardBuilder()
            b.row(btn("Получить еще раз", callback_data=f"get_code_{pid}", style="primary", icon="code"))
            b.row(btn("В меню", callback_data="main_menu", style="default", icon="home"))
            await callback.message.answer(f'{emoji("check")} <b>Код: <code>{code}</code></b>\n\n{emoji("tag")} Номер: <code>{account.phone}</code>', reply_markup=b.as_markup())
        else: await callback.message.answer(f'{emoji("cross")} <b>Не удалось</b>', reply_markup=get_code_keyboard(pid))

@router.callback_query(F.data.startswith("get_session_"))
async def cb_get_session(callback: CallbackQuery):
    await callback.answer()
    pid = int(callback.data.replace("get_session_",""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id==pid)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id!=callback.from_user.id: await callback.answer("Не найдена",show_alert=True); return
        account = await s.get(Account,purchase.account_id)
        if not account or not account.session_string: await callback.answer("Нет .session",show_alert=True); return
        await callback.message.answer_document(BufferedInputFile(account.session_string.encode(), filename=f"{account.phone}.session"), caption=f'{emoji("file")} .session для {account.phone}')
        b = InlineKeyboardBuilder(); b.row(btn("В меню", callback_data="main_menu", style="default", icon="home"))
        await callback.message.answer(f'{emoji("check")} <b>Файл отправлен!</b>', reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("get_json_"))
async def cb_get_json(callback: CallbackQuery):
    await callback.answer()
    pid = int(callback.data.replace("get_json_",""))
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.id==pid)); purchase = r.scalar_one_or_none()
        if not purchase or purchase.user_id!=callback.from_user.id: await callback.answer("Не найдена",show_alert=True); return
        account = await s.get(Account,purchase.account_id)
        if not account or not account.session_json:
            if account and account.session_string:
                session_json = json.dumps({"phone":account.phone,"session_string":account.session_string,"api_id":API_ID,"api_hash":API_HASH},ensure_ascii=False,indent=2)
                account.session_json = session_json; await s.commit()
            else: await callback.answer("Нет JSON данных",show_alert=True); return
        await callback.message.answer_document(BufferedInputFile((account.session_json or "{}").encode(), filename=f"{account.phone}_session.json"), caption=f'{emoji("json")} JSON для {account.phone}')
        b = InlineKeyboardBuilder(); b.row(btn("В меню", callback_data="main_menu", style="default", icon="home"))
        await callback.message.answer(f'{emoji("check")} <b>JSON отправлен!</b>', reply_markup=b.as_markup())

@router.callback_query(F.data == "my_purchases")
async def cb_my_purchases(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    async with async_session() as s:
        r = await s.execute(select(Purchase).where(Purchase.user_id==callback.from_user.id).order_by(Purchase.created_at.desc()))
        purchases = r.scalars().all()
        if purchases:
            text = f'{emoji("box")} <b>Ваши покупки</b>\n\n'; b = InlineKeyboardBuilder()
            for p in purchases:
                account = await s.get(Account,p.account_id)
                phone = account.phone if account else "Н/Д"
                text += f'📱 <code>{phone}</code> • {p.amount:.0f}₽ • {p.created_at.strftime("%d.%m.%y")}\n'
                b.row(btn("Код", callback_data=f"get_code_{p.id}", style="primary", icon="code"), btn(".session", callback_data=f"get_session_{p.id}", style="default", icon="file"), btn("JSON", callback_data=f"get_json_{p.id}", style="default", icon="json"))
            b.row(btn("В меню", callback_data="main_menu", style="default", icon="home"))
            await send_media_message(callback, "my_purchases", text, b.as_markup())
        else: await send_media_message(callback, "my_purchases", f'{emoji("box")} <b>Мои покупки</b>\n\nПока нет покупок.', main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    user = await get_user(callback.from_user.id)
    async with async_session() as s:
        cnt = (await s.execute(select(func.count(Purchase.id)).where(Purchase.user_id==callback.from_user.id))).scalar() or 0
    text = f'{emoji("profile")} <b>Профиль</b>\n\n{emoji("tag")} ID: <code>{user.telegram_id}</code>\n{emoji("profile")} @{user.username or "нет"}\n\n━━━ 💰 БАЛАНС ━━━\n{emoji("wallet")} <b>{user.balance:.0f}₽</b>\n━━━━━━━━━━━━━━\n\n━━ 📊 СТАТИСТИКА ━━\n{emoji("box")} Покупок: <b>{cnt}</b>\n{emoji("money")} Потрачено: <b>{(user.total_spent or 0):.0f}₽</b>\n{emoji("clock")} С нами: {user.created_at.strftime("%d.%m.%Y")}\n━━━━━━━━━━━━━━'
    await send_media_message(callback, "profile", text, profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def cb_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    if not await require_subscription(callback): return
    await send_media_message(callback, "deposit", f'{emoji("wallet")} <b>Пополнение баланса</b>\n\n{emoji("sbp")} <b>СБП</b>\n{emoji("crypto")} <b>Crypto Bot</b>\n\n<i>Минимум: 10₽</i>', deposit_keyboard())

@router.callback_query(F.data == "deposit_sbp")
async def cb_deposit_sbp(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{emoji("sbp")} <b>Введите сумму (от 10₽)</b>')
    if not hasattr(dp,'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'sbp'

@router.callback_query(F.data == "deposit_crypto")
async def cb_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(f'{emoji("crypto")} <b>Введите сумму (от 10₽)</b>')
    if not hasattr(dp,'awaiting_deposit'): dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

@router.callback_query(F.data == "activate_promo")
async def cb_activate_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_promo_code)
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="profile", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("promo")} <b>Введите промокод:</b>', reply_markup=b.as_markup())

# ===== СБП =====
@router.callback_query(F.data.startswith("sbp_paid_"))
async def cb_sbp_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    pid = callback.data.replace("sbp_paid_","")
    await state.set_state(SBPStates.waiting_for_screenshot); await state.update_data(payment_id=pid)
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="main_menu", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("photo")} <b>Отправьте скриншот оплаты</b>', reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("sbp_approve_"))
async def cb_sbp_approve(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_approve_","").rsplit("_",1); pid, uid = parts[0], int(parts[1])
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); payment = pr.scalar_one_or_none()
        if payment and payment.status!="completed":
            payment.status="completed"
            user = await s.execute(select(User).where(User.telegram_id==uid)); user = user.scalar_one_or_none()
            if user:
                old, new = user.balance, user.balance+payment.amount; user.balance=new; await s.commit()
                await callback.message.edit_caption(f'{callback.message.caption}\n\n{emoji("check")} <b>ОДОБРЕНО</b>\n💰 {old:.0f}₽ → {new:.0f}₽', reply_markup=None)
                try: await bot.send_message(uid, f'{emoji("check")} <b>Платеж одобрен!</b>\n\n{emoji("money")} +{payment.amount}₽\n{emoji("wallet")} Баланс: <b>{new:.0f}₽</b>')
                except: pass

@router.callback_query(F.data.startswith("sbp_reject_"))
async def cb_sbp_reject(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    parts = callback.data.replace("sbp_reject_","").rsplit("_",1); pid, uid = parts[0], int(parts[1])
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); payment = pr.scalar_one_or_none()
        if payment: payment.status="rejected"; await s.commit()
        await callback.message.edit_caption(f'{callback.message.caption}\n\n{emoji("cross")} <b>ОТКЛОНЕНО</b>', reply_markup=None)
        try: await bot.send_message(uid, f'{emoji("cross")} <b>Платеж отклонен</b>\n\n@v3estnikov')
        except: pass

@router.callback_query(F.data == "admin_sbp_check")
async def cb_admin_sbp(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(Payment).where(Payment.method=="sbp",Payment.status=="pending",Payment.screenshot_file_id!=None).order_by(Payment.created_at.desc()).limit(10))
        payments = r.scalars().all()
        if payments:
            await callback.message.answer(f'{emoji("sbp")} <b>Загружаю...</b>')
            for p in payments:
                user = await get_user(p.user_id)
                try: await bot.send_photo(callback.from_user.id, p.screenshot_file_id, caption=f'{emoji("sbp")} <b>СБП</b>\n{emoji("profile")} ID: <code>{p.user_id}</code>\n{emoji("money")} Сумма: <b>{p.amount}₽</b>\n{emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=sbp_approve_keyboard(p.payment_id,p.user_id))
                except: pass
            await callback.message.answer(f'{emoji("info")} Проверьте платежи', reply_markup=admin_keyboard())
        else: await callback.message.answer(f'{emoji("info")} <b>Нет платежей</b>', reply_markup=admin_keyboard())

# ===== АДМИН (admin_) =====
@router.callback_query(F.data == "admin")
async def cb_admin_return(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.message.answer(f'{emoji("stats")} <b>Админ-панель</b>', reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def cb_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: await callback.answer("❌",show_alert=True); return
    data = callback.data
    if data == "admin_stats":
        async with async_session() as s:
            uc = (await s.execute(select(func.count(User.id)))).scalar() or 0
            ac = (await s.execute(select(func.count(Account.id)))).scalar() or 0
            sc = (await s.execute(select(func.count(Account.id)).where(Account.is_sold==True))).scalar() or 0
            vc = (await s.execute(select(func.count(Account.id)).where(Account.is_verified==True))).scalar() or 0
            pc = (await s.execute(select(func.count(Purchase.id)))).scalar() or 0
            rev = (await s.execute(select(func.sum(Purchase.amount)))).scalar() or 0
            await callback.message.answer(f'{emoji("stats")} <b>Статистика</b>\n\n{emoji("profile")} Пользователей: <b>{uc}</b>\n{emoji("box")} Аккаунтов: <b>{ac}</b>\n{emoji("check")} Вериф: <b>{vc}</b>\n{emoji("buy")} Продано: <b>{sc}</b>\n{emoji("box")} Покупок: <b>{pc}</b>\n{emoji("money")} Выручка: <b>{rev:.0f}₽</b>', reply_markup=admin_keyboard())
    elif data == "admin_users":
        async with async_session() as s:
            r = await s.execute(select(User).order_by(User.created_at.desc()).limit(20)); users = r.scalars().all()
            text = f'{emoji("users")} <b>Пользователи</b>\n\n'
            for u in users: text += f'<code>{u.telegram_id}</code> @{u.username or "нет"} | {u.balance:.0f}₽ | {u.created_at.strftime("%d.%m")}\n'
        b = InlineKeyboardBuilder(); b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
        await callback.message.answer(text, reply_markup=b.as_markup())
    elif data == "admin_accounts_list":
        async with async_session() as s:
            r = await s.execute(select(Account).order_by(Account.created_at.desc()).limit(20)); accounts = r.scalars().all()
            text = f'{emoji("box")} <b>Аккаунты</b>\n\n'
            for a in accounts: text += f'{"✅" if a.is_verified else "⏳"} <code>{a.phone}</code> | {a.country} | {a.price:.0f}₽ | {"ПРОДАН" if a.is_sold else "в наличии"}\n'
        b = InlineKeyboardBuilder(); b.row(btn("Удалить аккаунт", callback_data="admin_delete_account", style="danger", icon="delete")); b.row(btn("Назад", callback_data="admin", style="danger", icon="back"))
        await callback.message.answer(text, reply_markup=b.as_markup())
    elif data == "admin_delete_account":
        await callback.message.answer(f'{emoji("delete")} <b>Отправьте номер для удаления:</b>')
        if not hasattr(dp,'awaiting_delete_account'): dp.awaiting_delete_account = set()
        dp.awaiting_delete_account.add(callback.from_user.id)
    elif data == "admin_broadcast":
        if not hasattr(dp,'awaiting_broadcast'): dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin", style="danger", icon="cross"))
        await callback.message.answer(f'{emoji("broadcast")} <b>Рассылка</b>\n\nОтправьте сообщение.', reply_markup=b.as_markup())
    elif data == "admin_add_accounts":
        if not hasattr(dp,'awaiting_accounts'): dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step':'phone'}
        b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin", style="danger", icon="cross"))
        await callback.message.answer(f'{emoji("add")} <b>Добавление аккаунта</b>\n\nОтправьте номер: <code>+79001234567</code>', reply_markup=b.as_markup())
    elif data == "admin_balance":
        if not hasattr(dp,'awaiting_balance'): dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step':'user_id'}
        b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin", style="danger", icon="cross"))
        await callback.message.answer(f'{emoji("edit")} <b>Изменение баланса</b>\n\nОтправьте ID.', reply_markup=b.as_markup())
    elif data == "admin_prices": await callback.message.answer(f'{emoji("settings")} <b>Цены</b>', reply_markup=await price_settings_keyboard())
    elif data == "admin_promo_menu": await callback.message.answer(f'{emoji("promo")} <b>Промокоды</b>', reply_markup=promo_admin_keyboard())
    elif data == "admin_media_menu": await callback.message.answer(f'{emoji("media")} <b>Управление медиа</b>', reply_markup=media_menu_keyboard())
    elif data == "admin_clear_media":
        async with async_session() as s: await s.execute(sa_text("DELETE FROM media_settings")); await s.commit()
        await callback.message.answer(f'{emoji("check")} <b>Медиа удалены!</b>', reply_markup=admin_keyboard())
    elif data == "admin_channels_menu": await callback.message.answer(f'{emoji("channel")} <b>Обязательные каналы</b>', reply_markup=channels_admin_keyboard())

# ===== ПРОМОКОДЫ (отдельно от admin_) =====
@router.callback_query(F.data == "promo_create")
async def cb_promo_create(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(PromoStates.waiting_for_promo_data)
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin_promo_menu", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("promo")} <b>Создание промокода</b>\n\nФормат: <code>КОД СУММА КОЛВО</code>', reply_markup=b.as_markup())

@router.callback_query(F.data == "promo_list")
async def cb_promo_list(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)); promos = r.scalars().all()
        text = f'{emoji("promo")} <b>Промокоды</b>\n\n'
        for p in promos: text += f'<code>{p.code}</code> | {p.amount}₽ | {p.used_count}/{p.max_uses} | {"✅" if p.is_active else "❌"}\n'
    b = InlineKeyboardBuilder(); b.row(btn("Назад", callback_data="admin_promo_menu", style="danger", icon="back"))
    await callback.message.answer(text, reply_markup=b.as_markup())

@router.callback_query(F.data == "promo_delete_menu")
async def cb_promo_delete_menu(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)); promos = r.scalars().all()
        if not promos: await callback.message.answer(f'{emoji("info")} Нет промокодов', reply_markup=promo_admin_keyboard()); return
        b = InlineKeyboardBuilder()
        for p in promos: b.row(btn(f"❌ {p.code}", callback_data=f"promo_delete_{p.id}", style="danger", icon="delete"))
        b.row(btn("Назад", callback_data="admin_promo_menu", style="danger", icon="back"))
        await callback.message.answer(f'{emoji("delete")} <b>Выберите для удаления:</b>', reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("promo_delete_"))
async def cb_promo_delete(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    pid = int(callback.data.replace("promo_delete_",""))
    async with async_session() as s:
        promo = await s.get(PromoCode,pid)
        if promo: await s.delete(promo); await s.commit(); await callback.message.answer(f'{emoji("check")} <b>Удален!</b>', reply_markup=promo_admin_keyboard())

# ===== КАНАЛЫ (отдельные обработчики) =====
@router.callback_query(F.data == "channel_add")
async def cb_channel_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(ChannelStates.waiting_for_channel)
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin_channels_menu", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("channel")} <b>Добавление канала</b>\n\nОтправьте @username или ссылку:\n<code>@durov</code> или <code>https://t.me/durov</code>', reply_markup=b.as_markup())

@router.callback_query(F.data == "channel_list")
async def cb_channel_list(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(RequiredChannel)); channels = r.scalars().all()
        text = f'{emoji("channel")} <b>Каналы</b>\n\n'
        if channels:
            for ch in channels: text += f'📢 {ch.channel_name or ch.channel_id}\n{ch.channel_url}\n\n'
        else: text += 'Нет обязательных каналов'
    b = InlineKeyboardBuilder(); b.row(btn("Назад", callback_data="admin_channels_menu", style="danger", icon="back"))
    await callback.message.answer(text, reply_markup=b.as_markup())

@router.callback_query(F.data == "channel_delete")
async def cb_channel_delete(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    async with async_session() as s:
        r = await s.execute(select(RequiredChannel)); channels = r.scalars().all()
        if not channels: await callback.message.answer(f'{emoji("info")} Нет каналов', reply_markup=channels_admin_keyboard()); return
        b = InlineKeyboardBuilder()
        for ch in channels: b.row(btn(f"❌ {ch.channel_name or ch.channel_id}", callback_data=f"channel_del_{ch.id}", style="danger", icon="delete"))
        b.row(btn("Назад", callback_data="admin_channels_menu", style="danger", icon="back"))
        await callback.message.answer(f'{emoji("delete")} <b>Выберите для удаления:</b>', reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("channel_del_"))
async def cb_channel_del(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    cid = int(callback.data.replace("channel_del_",""))
    async with async_session() as s:
        ch = await s.get(RequiredChannel,cid)
        if ch: await s.delete(ch); await s.commit(); await callback.message.answer(f'{emoji("check")} <b>Канал удален!</b>', reply_markup=channels_admin_keyboard())

# ===== ЦЕНЫ И МЕДИА =====
@router.callback_query(F.data.startswith("set_price_"))
async def cb_set_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    country = callback.data.replace("set_price_","")
    await state.set_state(PriceStates.waiting_for_price); await state.update_data(country=country)
    cur = await get_country_price(country)
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin_prices", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("edit")} <b>Цена: {country}</b>\n\nТекущая: <b>{cur:.0f}₽</b>\n\nОтправьте новую цену:', reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("set_media_"))
async def cb_set_media(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    section = callback.data.replace("set_media_","")
    await state.set_state(MediaStates.waiting_for_media); await state.update_data(section=section)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", callback_data="admin_media_menu", style="danger", icon="cross"))
    await callback.message.answer(f'{emoji("media")} <b>Установка медиа</b>\n\nРаздел: <b>{names.get(section,section)}</b>\n\nОтправьте фото/видео/GIF.', reply_markup=b.as_markup())

# ===== ОБРАБОТЧИКИ FSM =====
@router.message(StateFilter(PromoStates.waiting_for_promo_code), F.text)
async def h_activate_promo(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    async with async_session() as s:
        r = await s.execute(select(PromoCode).where(PromoCode.code==code,PromoCode.is_active==True)); promo = r.scalar_one_or_none()
        if not promo: await message.answer(f'{emoji("cross")} <b>Не найден</b>', reply_markup=profile_keyboard()); return
        if promo.used_count >= promo.max_uses: await message.answer(f'{emoji("cross")} <b>Исчерпан</b>', reply_markup=profile_keyboard()); return
        r = await s.execute(select(PromoUsage).where(PromoUsage.user_id==message.from_user.id,PromoUsage.promo_id==promo.id))
        if r.scalar_one_or_none(): await message.answer(f'{emoji("cross")} <b>Уже использован</b>', reply_markup=profile_keyboard()); return
        promo.used_count += 1; s.add(PromoUsage(user_id=message.from_user.id,promo_id=promo.id))
        user = await get_user(message.from_user.id)
        if user: user.balance += promo.amount; await s.commit()
        await message.answer(f'{emoji("check")} <b>Промокод активирован!</b>\n\n{emoji("money")} +{promo.amount}₽\n{emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=profile_keyboard())

@router.message(StateFilter(SBPStates.waiting_for_screenshot), F.photo)
async def h_sbp_screenshot(message: Message, state: FSMContext):
    data = await state.get_data(); pid = data.get('payment_id'); await state.clear()
    fid = message.photo[-1].file_id
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); payment = pr.scalar_one_or_none()
        if payment: payment.screenshot_file_id = fid; await s.commit()
    await message.answer(f'{emoji("check")} <b>Скриншот отправлен!</b>', reply_markup=main_menu_keyboard())
    async with async_session() as s:
        pr = await s.execute(select(Payment).where(Payment.payment_id==pid)); payment = pr.scalar_one_or_none()
        if payment:
            user = await get_user(payment.user_id)
            for aid in ADMIN_IDS:
                try: await bot.send_photo(aid, fid, caption=f'{emoji("sbp")} <b>СБП платеж</b>\n\n{emoji("profile")} ID: <code>{payment.user_id}</code>\n@{user.username or "нет"}\n{emoji("money")} Сумма: <b>{payment.amount}₽</b>\n{emoji("wallet")} Баланс: <b>{user.balance:.0f}₽</b>', reply_markup=sbp_approve_keyboard(pid,payment.user_id))
                except: pass

@router.message(StateFilter(PriceStates.waiting_for_price), F.text)
async def h_set_price(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); country = data.get('country'); await state.clear()
    try:
        price = float(message.text.strip().replace(',','.'))
        if price <= 0: await message.answer(f'{emoji("cross")} <b>Цена > 0</b>', reply_markup=admin_keyboard()); return
        await set_country_price(country, price)
        await message.answer(f'{emoji("check")} <b>Цена обновлена!</b>\n\n{country}: <b>{price:.0f}₽</b>', reply_markup=admin_keyboard())
    except: await message.answer(f'{emoji("cross")} <b>Введите число</b>', reply_markup=admin_keyboard())

@router.message(StateFilter(MediaStates.waiting_for_media), F.photo | F.video | F.animation)
async def h_media(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); section = data.get('section'); await state.clear()
    if message.photo: fid, ftype = message.photo[-1].file_id, "photo"
    elif message.video: fid, ftype = message.video.file_id, "video"
    else: fid, ftype = message.animation.file_id, "animation"
    await set_media(section, fid, ftype, message.caption)
    names = {"main_menu":"Главное меню","buy_account":"Покупка","payment_methods":"Оплата","profile":"Профиль","my_purchases":"Покупки","deposit":"Пополнение"}
    await message.answer(f'{emoji("check")} <b>Медиа установлено!</b>\n\nРаздел: <b>{names.get(section,section)}</b>', reply_markup=admin_keyboard())

@router.message(StateFilter(ChannelStates.waiting_for_channel), F.text)
async def h_add_channel(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    text = message.text.strip()
    # Извлекаем username
    username = text.replace('@','').replace('https://t.me/','').replace('http://t.me/','').strip('/').split()[0]
    channel_url = f"https://t.me/{username}"
    try:
        chat = await bot.get_chat(f"@{username}")
        async with async_session() as s:
            existing = await s.execute(select(RequiredChannel).where(RequiredChannel.channel_id==str(chat.id)))
            if existing.scalar_one_or_none(): await message.answer(f'{emoji("cross")} <b>Канал уже добавлен!</b>', reply_markup=admin_keyboard()); return
            s.add(RequiredChannel(channel_id=str(chat.id),channel_url=channel_url,channel_name=chat.title or username)); await s.commit()
        await message.answer(f'{emoji("check")} <b>Канал добавлен!</b>\n\nНазвание: <b>{chat.title}</b>\nID: <code>{chat.id}</code>\nСсылка: {channel_url}', reply_markup=admin_keyboard())
    except Exception as e:
        await message.answer(f'{emoji("cross")} <b>Не удалось добавить канал</b>\n\nОшибка: {str(e)[:100]}\n\nУбедитесь что:\n1. Канал публичный\n2. Бот администратор\n3. Формат: @username', reply_markup=admin_keyboard())

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
                if (await s.execute(select(PromoCode).where(PromoCode.code==code))).scalar_one_or_none(): await message.answer(f'{emoji("cross")} <b>Промокод {code} уже существует</b>', reply_markup=admin_keyboard()); return
                s.add(PromoCode(code=code,amount=amount,max_uses=max_uses)); await s.commit()
            await message.answer(f'{emoji("check")} <b>Промокод создан!</b>\n\n<code>{code}</code> | {amount}₽ | {max_uses} исп.', reply_markup=admin_keyboard())
        except: await message.answer(f'{emoji("cross")} <b>Неверный формат чисел</b>', reply_markup=admin_keyboard())

# ===== ОБРАБОТЧИК ТЕКСТА =====
@router.message(F.text)
async def h_text(message: Message):
    uid = message.from_user.id; text = message.text.strip()
    if hasattr(dp,'awaiting_deposit') and uid in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(uid)
        try:
            amount = float(text.replace(',','.'))
            if amount < 10: await message.answer(f'{emoji("cross")} <b>Минимум: 10₽</b>', reply_markup=deposit_keyboard()); return
            pid = await generate_payment_id()
            if method == "sbp":
                async with async_session() as s: s.add(Payment(user_id=uid,amount=amount,payment_id=pid,method="sbp",status="pending",type="deposit")); await s.commit()
                await message.answer(f'{emoji("sbp")} <b>Пополнение СБП</b>\n\n{emoji("money")} Сумма: <b>{amount}₽</b>\n\n{emoji("bank")} <b>Реквизиты:</b>\n📱 <code>{SBP_PHONE}</code>\n🏦 Банк: <b>{SBP_BANK}</b>\n👤 Получатель: <b>{SBP_RECEIVER}</b>\n\n⚠️ Нажмите "Я оплатил"', reply_markup=sbp_payment_keyboard(pid))
            elif method == "crypto":
                async with async_session() as s: s.add(Payment(user_id=uid,amount=amount,payment_id=pid,method="crypto",status="pending",type="deposit")); await s.commit()
                invoice = await create_crypto_bot_invoice(amount,pid)
                if invoice and invoice.get("ok"):
                    r = invoice.get("result",{})
                    async with async_session() as s:
                        p = await s.execute(select(Payment).where(Payment.payment_id==pid)); p = p.scalar_one_or_none()
                        if p: p.payment_id=str(r.get("invoice_id")); await s.commit()
                    await message.answer(f'{emoji("crypto")} <b>Пополнение Crypto Bot</b>\n\nСумма: <b>{amount}₽</b>\n\n<a href="{r.get("pay_url")}">💳 Нажмите для оплаты</a>\n\n⚠️ Нажмите проверку', reply_markup=deposit_crypto_check_keyboard(str(r.get("invoice_id"))), disable_web_page_preview=True)
        except: await message.answer(f'{emoji("cross")} <b>Введите число</b>')
        return
    if hasattr(dp,'awaiting_delete_account') and uid in dp.awaiting_delete_account:
        dp.awaiting_delete_account.remove(uid)
        async with async_session() as s:
            r = await s.execute(select(Account).where(Account.phone==text)); account = r.scalar_one_or_none()
            if account: await s.delete(account); await s.commit(); await message.answer(f'{emoji("check")} <b>Аккаунт {text} удален!</b>', reply_markup=admin_keyboard())
            else: await message.answer(f'{emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard())
        return
    if hasattr(dp,'awaiting_accounts') and uid in dp.awaiting_accounts:
        ad = dp.awaiting_accounts[uid]; step = ad.get('step')
        if step == 'phone':
            phone = text; country = detect_country(phone); price = await get_country_price(country)
            ad['phone']=phone; ad['country']=country; ad['price']=price
            res = await send_code_to_phone(phone)
            if res['success']: ad['phone_code_hash']=res['phone_code_hash']; ad['step']='code'; await message.answer(f'{emoji("check")} <b>Код отправлен на {phone}</b>\n\nСтрана: <b>{country}</b> | Цена: <b>{price:.0f}₽</b>\n\nВведите код:')
            else: del dp.awaiting_accounts[uid]; await message.answer(f'{emoji("cross")} <b>Ошибка</b>', reply_markup=admin_keyboard())
        elif step == 'code':
            res = await verify_code_and_create_session_json(ad['phone'],text,ad['phone_code_hash'])
            if res['success']:
                async with async_session() as s:
                    ex = await s.execute(select(Account).where(Account.phone==ad['phone'])); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string=res['session_string']; ex.session_json=res['session_json']; ex.is_verified=True; ex.is_sold=False; ex.country=ad['country']; ex.price=ad['price']
                    else: s.add(Account(phone=ad['phone'],country=ad['country'],price=ad['price'],session_string=res['session_string'],session_json=res['session_json'],is_verified=True,is_sold=False))
                    await s.commit()
                del dp.awaiting_accounts[uid]
                await message.answer(f'{emoji("check")} <b>Аккаунт добавлен!</b>\n\n{emoji("tag")} Номер: <code>{ad["phone"]}</code>\n{emoji("location")} Страна: <b>{ad["country"]}</b>\n{emoji("money")} Цена: <b>{ad["price"]:.0f}₽</b>\n<i>Доступен для покупки</i>', reply_markup=admin_keyboard())
            elif res.get('need_password'): ad['step']='password'; await message.answer(f'{emoji("lock")} <b>Введите 2FA пароль:</b>')
            else: del dp.awaiting_accounts[uid]; await message.answer(f'{emoji("cross")} <b>{res.get("error")}</b>', reply_markup=admin_keyboard())
        elif step == 'password':
            res = await verify_2fa_and_create_session_json(ad['phone'],text)
            if res['success']:
                async with async_session() as s:
                    ex = await s.execute(select(Account).where(Account.phone==ad['phone'])); ex = ex.scalar_one_or_none()
                    if ex: ex.session_string=res['session_string']; ex.session_json=res['session_json']; ex.is_verified=True; ex.is_sold=False; ex.country=ad['country']; ex.price=ad['price']
                    else: s.add(Account(phone=ad['phone'],country=ad['country'],price=ad['price'],session_string=res['session_string'],session_json=res['session_json'],is_verified=True,is_sold=False))
                    await s.commit()
                del dp.awaiting_accounts[uid]
                await message.answer(f'{emoji("check")} <b>Аккаунт добавлен!</b>\n\n{emoji("tag")} Номер: <code>{ad["phone"]}</code>\n<i>Доступен</i>', reply_markup=admin_keyboard())
            else: await message.answer(f'{emoji("cross")} <b>{res.get("error")}</b>\nПопробуйте еще раз:')
        return
    if hasattr(dp,'awaiting_balance') and uid in dp.awaiting_balance:
        bd = dp.awaiting_balance[uid]; step = bd.get('step')
        if step == 'user_id':
            try:
                target = await get_user(int(text))
                if not target: await message.answer(f'{emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); del dp.awaiting_balance[uid]; return
                bd['target_id']=int(text); bd['step']='amount'
                await message.answer(f'{emoji("edit")} <b>Изменение баланса</b>\n\nID: <code>{text}</code>\nБаланс: <b>{target.balance:.0f}₽</b>\n\n<code>+100</code> / <code>-50</code> / <code>500</code>')
            except: await message.answer(f'{emoji("cross")} <b>Введите ID</b>')
        elif step == 'amount':
            try:
                tid = bd['target_id']
                async with async_session() as s:
                    target = await s.execute(select(User).where(User.telegram_id==tid)); target = target.scalar_one_or_none()
                    if not target: del dp.awaiting_balance[uid]; await message.answer(f'{emoji("cross")} <b>Не найден</b>', reply_markup=admin_keyboard()); return
                    old = target.balance
                    if text.startswith('+'): target.balance += float(text[1:])
                    elif text.startswith('-'): target.balance = max(0,target.balance-float(text[1:]))
                    else: target.balance = float(text)
                    await s.commit(); del dp.awaiting_balance[uid]
                    await message.answer(f'{emoji("check")} <b>Баланс изменен!</b>\n\nID: <code>{tid}</code>\nБыло: <b>{old:.0f}₽</b>\nСтало: <b>{target.balance:.0f}₽</b>', reply_markup=admin_keyboard())
            except: await message.answer(f'{emoji("cross")} <b>Введите сумму</b>')
        return
    if hasattr(dp,'awaiting_broadcast') and uid in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(uid)
        async with async_session() as s:
            users = (await s.execute(select(User))).scalars().all(); sent = 0
            for u in users:
                try: await message.copy_to(chat_id=u.telegram_id); sent += 1; await asyncio.sleep(0.05)
                except: pass
        await message.answer(f'{emoji("check")} <b>Рассылка завершена</b>\n\n{sent}/{len(users)}', reply_markup=admin_keyboard())
        return
    await message.answer(f'{emoji("info")} <b>Используйте кнопки меню</b>', reply_markup=main_menu_keyboard())

# ===== ЗАПУСК =====
async def setup_db():
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)

async def main():
    await setup_db(); await run_migrations()
    for attr in ['pending_accounts','awaiting_deposit','awaiting_accounts','awaiting_balance']:
        if not hasattr(dp,attr): setattr(dp,attr,{})
    if not hasattr(dp,'awaiting_broadcast'): dp.awaiting_broadcast = set()
    if not hasattr(dp,'awaiting_delete_account'): dp.awaiting_delete_account = set()
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
