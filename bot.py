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
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, CallbackQuery)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer,
                        String, Text, select, func)
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
    created_at = Column(DateTime, default=datetime.utcnow)

try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Failed to create database engine: {e}")
    DATABASE_URL = "sqlite+aiosqlite:///test.db"
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ===== ИНИЦИАЛИЗАЦИЯ =====
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# ===== ВРЕМЕННОЕ ХРАНИЛИЩЕ ДЛЯ АВТОРИЗАЦИЙ =====
pending_auth = {}

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Купить аккаунт",
            callback_data="buy_account",
            icon_custom_emoji_id="5963103826075456248"
        ),
        InlineKeyboardButton(
            text="Мои покупки",
            callback_data="my_purchases",
            icon_custom_emoji_id="5884479287171485878"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Профиль",
            callback_data="profile",
            icon_custom_emoji_id="5870994129244131212"
        ),
        InlineKeyboardButton(
            text="Пополнить",
            callback_data="deposit_balance",
            icon_custom_emoji_id="5769126056262898415"
        )
    )
    return builder.as_markup()

def countries_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="США • 20₽",
        callback_data="country_USA",
        icon_custom_emoji_id="6042011682497106307"
    ))
    builder.row(InlineKeyboardButton(
        text="Назад",
        callback_data="main_menu",
        icon_custom_emoji_id="5893057118545646106"
    ))
    return builder.as_markup()

def payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Баланс бота",
        callback_data="pay_balance",
        icon_custom_emoji_id="5769126056262898415"
    ))
    builder.row(InlineKeyboardButton(
        text="ЮMoney",
        callback_data="pay_yoomoney",
        icon_custom_emoji_id="5904462880941545555"
    ))
    builder.row(InlineKeyboardButton(
        text="Crypto Bot",
        callback_data="pay_crypto",
        icon_custom_emoji_id="5260752406890711732"
    ))
    builder.row(InlineKeyboardButton(
        text="Telegram Stars",
        callback_data="pay_stars",
        icon_custom_emoji_id="6041731551845159060"
    ))
    builder.row(InlineKeyboardButton(
        text="Назад",
        callback_data="buy_account",
        icon_custom_emoji_id="5893057118545646106"
    ))
    return builder.as_markup()

def check_payment_keyboard(payment_id: str, method: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Проверить оплату",
        callback_data=f"check_payment_{method}_{payment_id}",
        icon_custom_emoji_id="5345906554510012647"
    ))
    builder.row(InlineKeyboardButton(
        text="Отмена",
        callback_data="main_menu",
        icon_custom_emoji_id="5870657884844462243"
    ))
    return builder.as_markup()

def get_code_keyboard(purchase_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Получить код",
        callback_data=f"get_code_{purchase_id}",
        icon_custom_emoji_id="5940433880585605708"
    ))
    builder.row(InlineKeyboardButton(
        text="К покупкам",
        callback_data="my_purchases",
        icon_custom_emoji_id="5884479287171485878"
    ))
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Пополнить баланс",
        callback_data="deposit_balance",
        icon_custom_emoji_id="5769126056262898415"
    ))
    builder.row(InlineKeyboardButton(
        text="Мои покупки",
        callback_data="my_purchases",
        icon_custom_emoji_id="5884479287171485878"
    ))
    builder.row(InlineKeyboardButton(
        text="В меню",
        callback_data="main_menu",
        icon_custom_emoji_id="5873147866364514353"
    ))
    return builder.as_markup()

def deposit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="ЮMoney",
        callback_data="deposit_yoomoney",
        icon_custom_emoji_id="5904462880941545555"
    ))
    builder.row(InlineKeyboardButton(
        text="Crypto Bot",
        callback_data="deposit_crypto",
        icon_custom_emoji_id="5260752406890711732"
    ))
    builder.row(InlineKeyboardButton(
        text="Назад",
        callback_data="profile",
        icon_custom_emoji_id="5893057118545646106"
    ))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Статистика",
        callback_data="admin_stats",
        icon_custom_emoji_id="5870921681735781843"
    ))
    builder.row(InlineKeyboardButton(
        text="Рассылка",
        callback_data="admin_broadcast",
        icon_custom_emoji_id="6039422865189638057"
    ))
    builder.row(InlineKeyboardButton(
        text="Добавить аккаунты",
        callback_data="admin_add_accounts",
        icon_custom_emoji_id="5771851822897566479"
    ))
    builder.row(InlineKeyboardButton(
        text="В меню",
        callback_data="main_menu",
        icon_custom_emoji_id="5873147866364514353"
    ))
    return builder.as_markup()

# ===== TElethon ФУНКЦИИ =====
async def create_telethon_client(session_string: str = None) -> TelegramClient:
    """Создает клиент Telethon с сессией"""
    if session_string:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    else:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
    return client

async def send_code_to_phone(phone: str) -> dict:
    """Отправляет код на номер телефона"""
    try:
        client = await create_telethon_client()
        await client.connect()
        
        sent = await client.send_code_request(phone)
        
        pending_auth[phone] = {
            'client': client,
            'phone_code_hash': sent.phone_code_hash,
            'phone': phone
        }
        
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

async def verify_code_and_get_session(phone: str, code: str, phone_code_hash: str) -> dict:
    """Подтверждает код и возвращает сессию"""
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {'success': False, 'error': 'Сессия не найдена. Запросите код заново.'}
        
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
        
        # Проверяем что сессия работает
        me = await client.get_me()
        
        await client.disconnect()
        
        # Удаляем из временного хранилища
        pending_auth.pop(phone, None)
        
        return {
            'success': True,
            'session_string': session_string,
            'user_id': me.id,
            'username': me.username
        }
    except PhoneCodeInvalidError:
        return {'success': False, 'error': 'Неверный код'}
    except PhoneCodeExpiredError:
        return {'success': False, 'error': 'Код истек. Запросите новый.'}
    except Exception as e:
        logger.error(f"Error verifying code for {phone}: {e}")
        return {'success': False, 'error': str(e)}

async def verify_2fa_password(phone: str, password: str) -> dict:
    """Подтверждает 2FA пароль"""
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {'success': False, 'error': 'Сессия не найдена'}
        
        client = auth_data['client']
        
        await client.sign_in(password=password)
        
        session_string = client.session.save()
        me = await client.get_me()
        
        await client.disconnect()
        
        pending_auth.pop(phone, None)
        
        return {
            'success': True,
            'session_string': session_string,
            'user_id': me.id,
            'username': me.username
        }
    except PasswordHashInvalidError:
        return {'success': False, 'error': 'Неверный пароль 2FA'}
    except Exception as e:
        logger.error(f"Error verifying 2FA for {phone}: {e}")
        return {'success': False, 'error': str(e)}

async def get_code_from_session(session_string: str) -> Optional[str]:
    """Получает код из чата с +42777 используя сессию аккаунта"""
    client = None
    try:
        client = await create_telethon_client(session_string)
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.error("Session is not authorized")
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
        logger.error(f"Error getting code from session: {e}")
        return None
    finally:
        if client:
            await client.disconnect()

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
async def get_user(user_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        return result.scalar_one_or_none()

async def get_or_create_user(user_id: int, username: str = None):
    user = await get_user(user_id)
    if not user:
        async with async_session() as session:
            user = User(
                telegram_id=user_id,
                username=username,
                is_admin=(user_id in ADMIN_IDS)
            )
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
                Account.session_string != None
            ).limit(1)
        )
        return result.scalar_one_or_none()

async def get_user_purchases_count(user_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Purchase.id)).where(Purchase.user_id == user_id)
        )
        return result.scalar() or 0

async def create_yoomoney_payment(amount: float, payment_id: str) -> Optional[str]:
    try:
        payment_url = (
            f"https://yoomoney.ru/quickpay/confirm.xml?"
            f"receiver={YOOMONEY_WALLET}&"
            f"quickpay-form=shop&"
            f"targets=Оплата+заказа+{payment_id}&"
            f"paymentType=SB&"
            f"sum={amount}&"
            f"label={payment_id}"
        )
        return payment_url
    except Exception as e:
        logger.error(f"YooMoney payment creation error: {e}")
        return None

async def create_crypto_bot_invoice(amount: float, payment_id: str) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        
        usdt_amount = round(amount / 90, 2)
        
        payload = {
            "asset": "USDT",
            "amount": str(usdt_amount),
            "description": f"Vest Account #{payment_id}",
            "payload": payment_id,
            "allow_comments": False,
            "allow_anonymous": False
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
        return None
    except Exception as e:
        logger.error(f"Crypto Bot invoice creation error: {e}")
        return None

async def check_crypto_bot_invoice(invoice_id: int) -> Optional[dict]:
    try:
        url = "https://pay.crypt.bot/api/getInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        params = {"invoice_id": invoice_id}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("result", {})
        return None
    except Exception as e:
        logger.error(f"Crypto Bot invoice check error: {e}")
        return None

async def generate_payment_id():
    return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

# ===== ОБРАБОТЧИКИ КОМАНД =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    
    welcome_text = (
        '<b><tg-emoji emoji-id="6030400221232501136">🤖</tg-emoji> Vest Account</b>\n\n'
        '<tg-emoji emoji-id="6037249452824072506">🔒</tg-emoji> Покупка аккаунтов\n'
        '<tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> Быстро и безопасно\n'
        '<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Мгновенная выдача\n\n'
        '<i>Выберите действие:</i>'
    )
    
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Доступ запрещен</b>'
        )
        return
    
    await message.answer(
        '<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Админ-панель</b>',
        reply_markup=admin_keyboard()
    )

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    await callback.answer()
    text = (
        '<b><tg-emoji emoji-id="5873147866364514353">🏘</tg-emoji> Главное меню</b>\n\n'
        '<i>Выберите действие:</i>'
    )
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def callback_buy_account(callback: CallbackQuery):
    await callback.answer()
    text = (
        '<b><tg-emoji emoji-id="6042011682497106307">📍</tg-emoji> Выберите страну</b>\n\n'
        'Доступные направления:'
    )
    await callback.message.edit_text(text, reply_markup=countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def callback_country(callback: CallbackQuery):
    await callback.answer()
    country = callback.data.replace("country_", "")
    account = await get_available_account()
    
    if account:
        text = (
            '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Аккаунт найден!</b>\n\n'
            f'<tg-emoji emoji-id="6042011682497106307">📍</tg-emoji> Страна: <b>{country}</b>\n'
            f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Цена: <b>{account.price}₽</b>\n\n'
            '<i>Выберите способ оплаты:</i>'
        )
        
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {
            'account_id': account.id,
            'price': account.price
        }
        
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())
    else:
        text = (
            '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Нет доступных аккаунтов</b>\n\n'
            'К сожалению, все аккаунты распроданы.\n'
            'Попробуйте позже.'
        )
        await callback.message.edit_text(text, reply_markup=countries_keyboard())

@router.callback_query(F.data == "pay_balance")
async def callback_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    account_id = pending.get('account_id')
    
    if user.balance >= price and account_id:
        async with async_session() as session:
            user.balance -= price
            user.total_spent += price
            
            purchase = Purchase(
                user_id=callback.from_user.id,
                account_id=account_id,
                amount=price,
                payment_method="balance"
            )
            
            account = await session.get(Account, account_id)
            account.is_sold = True
            
            session.add(purchase)
            await session.commit()
            await session.refresh(purchase)
            
            text = (
                '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Оплата успешна!</b>\n\n'
                f'<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер: <code>{account.phone}</code>\n'
                f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Сумма: <b>{price}₽</b>\n\n'
                'Нажмите кнопку, чтобы получить код:'
            )
            
            await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = (
            '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Недостаточно средств</b>\n\n'
            f'<tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Ваш баланс: <b>{user.balance:.0f}₽</b>\n'
            f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Необходимо: <b>{price}₽</b>\n\n'
            '<i>Пополните баланс в профиле</i>'
        )
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_yoomoney")
async def callback_pay_yoomoney(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    payment_id = await generate_payment_id()
    
    async with async_session() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            amount=price,
            payment_id=payment_id,
            method="yoomoney",
            status="pending"
        )
        session.add(payment)
        await session.commit()
    
    payment_url = await create_yoomoney_payment(price, payment_id)
    
    if payment_url:
        text = (
            '<b><tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Оплата через ЮMoney</b>\n\n'
            f'Сумма: <b>{price}₽</b>\n'
            f'Кошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
            f'<a href="{payment_url}">Нажмите для оплаты</a>\n\n'
            '⚠️ После оплаты нажмите кнопку проверки'
        )
        
        await callback.message.edit_text(text, reply_markup=check_payment_keyboard(payment_id, "yoomoney"))
    else:
        await callback.message.edit_text(
            '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Ошибка создания платежа</b>',
            reply_markup=payment_methods_keyboard()
        )

@router.callback_query(F.data == "pay_crypto")
async def callback_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
    price = pending.get('price', 20)
    payment_id = await generate_payment_id()
    
    async with async_session() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            amount=price,
            payment_id=payment_id,
            method="crypto",
            status="pending"
        )
        session.add(payment)
        await session.commit()
    
    invoice = await create_crypto_bot_invoice(price, payment_id)
    
    if invoice and invoice.get("ok"):
        result = invoice.get("result", {})
        pay_url = result.get("pay_url")
        invoice_id = result.get("invoice_id")
        
        text = (
            '<b><tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> Оплата через Crypto Bot</b>\n\n'
            f'Сумма: <b>{price}₽</b>\n\n'
            f'<a href="{pay_url}">Нажмите для оплаты</a>\n\n'
            '⚠️ После оплаты нажмите кнопку проверки'
        )
        
        await callback.message.edit_text(text, reply_markup=check_payment_keyboard(str(invoice_id), "crypto"))
    else:
        await callback.message.edit_text(
            '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Ошибка создания счета</b>',
            reply_markup=payment_methods_keyboard()
        )

@router.callback_query(F.data == "pay_stars")
async def callback_pay_stars(callback: CallbackQuery):
    await callback.answer()
    text = (
        '<b><tg-emoji emoji-id="6041731551845159060">🎉</tg-emoji> Оплата Telegram Stars</b>\n\n'
        'Для покупки через Telegram Stars\n'
        'напишите: <b>@v3estnikov</b>'
    )
    
    await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data.startswith("check_payment_"))
async def callback_check_payment(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("check_payment_", "").split("_", 1)
    method = parts[0]
    payment_id = "_".join(parts[1:])
    
    success = False
    
    if method == "crypto":
        invoice = await check_crypto_bot_invoice(int(payment_id))
        if invoice and invoice.get("status") == "paid":
            success = True
    
    if success:
        pending = dp.pending_accounts.get(callback.from_user.id, {}) if hasattr(dp, 'pending_accounts') else {}
        account_id = pending.get('account_id')
        price = pending.get('price', 20)
        
        if account_id:
            async with async_session() as session:
                result = await session.execute(
                    select(Payment).where(Payment.payment_id == payment_id)
                )
                payment = result.scalar_one_or_none()
                if payment:
                    payment.status = "completed"
                
                user = await get_user(callback.from_user.id)
                user.total_spent += price
                
                purchase = Purchase(
                    user_id=callback.from_user.id,
                    account_id=account_id,
                    amount=price,
                    payment_method=method
                )
                
                account = await session.get(Account, account_id)
                account.is_sold = True
                
                session.add(purchase)
                await session.commit()
                await session.refresh(purchase)
                
                text = (
                    '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Оплата подтверждена!</b>\n\n'
                    f'<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер: <code>{account.phone}</code>\n'
                    f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Сумма: <b>{price}₽</b>\n\n'
                    'Нажмите кнопку, чтобы получить код:'
                )
                
                await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        await callback.answer("⏳ Платеж еще не получен. Попробуйте позже.", show_alert=True)

@router.callback_query(F.data.startswith("get_code_"))
async def callback_get_code(callback: CallbackQuery):
    await callback.answer()
    purchase_id = int(callback.data.replace("get_code_", ""))
    
    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.id == purchase_id)
        )
        purchase = result.scalar_one_or_none()
        
        if purchase and purchase.user_id == callback.from_user.id and not purchase.code_sent:
            account = await session.get(Account, purchase.account_id)
            
            if account and account.session_string:
                await callback.message.edit_text(
                    '<b><tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> Получаю код...</b>\n\n'
                    'Пожалуйста, подождите несколько секунд.'
                )
                
                # Получаем код через сессию аккаунта
                code = await get_code_from_session(account.session_string)
                
                if code:
                    purchase.code_sent = True
                    await session.commit()
                    
                    text = (
                        '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Код получен!</b>\n\n'
                        f'<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер: <code>{account.phone}</code>\n'
                        f'<tg-emoji emoji-id="6037249452824072506">🔒</tg-emoji> Код: <code>{code}</code>\n\n'
                        '⚠️ <i>Сохраните код в надежном месте</i>'
                    )
                    
                    builder = InlineKeyboardBuilder()
                    builder.row(InlineKeyboardButton(
                        text="В главное меню",
                        callback_data="main_menu",
                        icon_custom_emoji_id="5873147866364514353"
                    ))
                    
                    await callback.message.edit_text(text, reply_markup=builder.as_markup())
                else:
                    await callback.message.edit_text(
                        '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Не удалось получить код</b>\n\n'
                        'Попробуйте позже или обратитесь в поддержку: @v3estnikov',
                        reply_markup=get_code_keyboard(purchase_id)
                    )
            else:
                await callback.answer("Аккаунт не найден или не верифицирован", show_alert=True)
        else:
            await callback.answer("Код уже получен", show_alert=True)

@router.callback_query(F.data == "my_purchases")
async def callback_my_purchases(callback: CallbackQuery):
    await callback.answer()
    
    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc())
        )
        purchases = result.scalars().all()
        
        if purchases:
            text = '<b><tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Ваши покупки</b>\n\n'
            
            builder = InlineKeyboardBuilder()
            for purchase in purchases:
                account = await session.get(Account, purchase.account_id)
                status = "✅" if purchase.code_sent else "⏳"
                phone = account.phone if account else "Н/Д"
                date = purchase.created_at.strftime('%d.%m.%y')
                
                text += f'{status} <code>{phone}</code> • {purchase.amount}₽ • {date}\n'
                
                if not purchase.code_sent:
                    builder.row(InlineKeyboardButton(
                        text=f"Получить код • {phone}",
                        callback_data=f"get_code_{purchase.id}",
                        icon_custom_emoji_id="5940433880585605708"
                    ))
            
            text += f'\n<i>Всего покупок: {len(purchases)}</i>'
            
            builder.row(InlineKeyboardButton(
                text="В меню",
                callback_data="main_menu",
                icon_custom_emoji_id="5873147866364514353"
            ))
            
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        else:
            text = (
                '<b><tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Мои покупки</b>\n\n'
                'У вас пока нет покупок.\n'
                'Купите свой первый аккаунт!'
            )
            await callback.message.edit_text(text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    purchases_count = await get_user_purchases_count(callback.from_user.id)
    
    text = (
        '<b><tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Профиль</b>\n\n'
        f'<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> ID: <code>{user.telegram_id}</code>\n'
        f'<tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Логин: @{user.username or "нет"}\n\n'
        '━━━━ 💰 БАЛАНС ━━━━\n'
        f'<tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> <b>{user.balance:.0f}₽</b>\n'
        '━━━━━━━━━━━━━━━━\n\n'
        '━━━━ 📊 СТАТИСТИКА ━━━━\n'
        f'<tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Покупок: <b>{purchases_count}</b>\n'
        f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Потрачено: <b>{user.total_spent:.0f}₽</b>\n'
        f'<tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> С нами с: {user.created_at.strftime("%d.%m.%Y")}\n'
        '━━━━━━━━━━━━━━━━'
    )
    
    await callback.message.edit_text(text, reply_markup=profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def callback_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    text = (
        '<b><tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Пополнение баланса</b>\n\n'
        'Выберите способ:\n\n'
        '<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> <b>ЮMoney</b> — перевод на кошелек\n'
        '<tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> <b>Crypto Bot</b> — криптовалютой\n\n'
        '<i>Минимальная сумма: 10₽</i>'
    )
    await callback.message.edit_text(text, reply_markup=deposit_keyboard())

@router.callback_query(F.data == "deposit_yoomoney")
async def callback_deposit_yoomoney(callback: CallbackQuery):
    await callback.answer()
    
    await callback.message.answer(
        '<b><tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Введите сумму пополнения (от 10₽)</b>\n\n'
        '<i>Отправьте число в чат</i>'
    )
    
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'yoomoney'

@router.callback_query(F.data == "deposit_crypto")
async def callback_deposit_crypto(callback: CallbackQuery):
    await callback.answer()
    
    await callback.message.answer(
        '<b><tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> Введите сумму пополнения (от 10₽)</b>\n\n'
        '<i>Отправьте число в чат</i>'
    )
    
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

# ===== АДМИН ОБРАБОТЧИКИ =====
@router.callback_query(F.data.startswith("admin_"))
async def callback_admin(callback: CallbackQuery):
    await callback.answer()
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    data = callback.data
    
    if data == "admin_stats":
        async with async_session() as session:
            result = await session.execute(select(func.count(User.id)))
            total_users = result.scalar() or 0
            
            result = await session.execute(select(func.count(Account.id)))
            total_accounts = result.scalar() or 0
            
            result = await session.execute(select(func.count(Account.id)).where(Account.is_sold == True))
            sold_accounts = result.scalar() or 0
            
            result = await session.execute(select(func.count(Account.id)).where(Account.is_verified == True))
            verified_accounts = result.scalar() or 0
            
            result = await session.execute(select(func.count(Purchase.id)))
            total_purchases = result.scalar() or 0
            
            result = await session.execute(select(func.sum(Purchase.amount)))
            total_revenue = result.scalar() or 0
            
            stats_text = (
                '<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Статистика</b>\n\n'
                f'<tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Пользователей: <b>{total_users}</b>\n'
                f'<tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Аккаунтов: <b>{total_accounts}</b>\n'
                f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Верифицировано: <b>{verified_accounts}</b>\n'
                f'<tg-emoji emoji-id="5963103826075456248">⬆</tg-emoji> Продано: <b>{sold_accounts}</b>\n'
                f'<tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Покупок: <b>{total_purchases}</b>\n'
                f'<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Выручка: <b>{total_revenue:.0f}₽</b>'
            )
            
            await callback.message.edit_text(stats_text, reply_markup=admin_keyboard())
    
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'):
            dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="Отмена",
            callback_data="main_menu",
            icon_custom_emoji_id="5870657884844462243"
        ))
        
        await callback.message.edit_text(
            '<b><tg-emoji emoji-id="6039422865189638057">📣</tg-emoji> Рассылка</b>\n\n'
            'Отправьте сообщение для рассылки всем пользователям.\n'
            'Поддерживаются: текст, фото, видео, документы.',
            reply_markup=builder.as_markup()
        )
    
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="Отмена",
            callback_data="main_menu",
            icon_custom_emoji_id="5870657884844462243"
        ))
        
        await callback.message.edit_text(
            '<b><tg-emoji emoji-id="5771851822897566479">🔡</tg-emoji> Добавление аккаунта</b>\n\n'
            'Отправьте номер телефона в формате:\n'
            '<code>+79001234567</code>',
            reply_markup=builder.as_markup()
        )

# ===== ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ =====
@router.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Проверяем ожидание суммы депозита
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        
        try:
            amount = float(text.replace(',', '.'))
            if amount < 10:
                await message.answer(
                    '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Минимальная сумма: 10₽</b>\n\n'
                    'Введите сумму еще раз:',
                    reply_markup=deposit_keyboard()
                )
                return
            
            payment_id = await generate_payment_id()
            
            async with async_session() as session:
                payment = Payment(
                    user_id=user_id,
                    amount=amount,
                    payment_id=payment_id,
                    method=method,
                    status="pending"
                )
                session.add(payment)
                await session.commit()
            
            if method == "yoomoney":
                payment_url = await create_yoomoney_payment(amount, payment_id)
                
                if payment_url:
                    await message.answer(
                        f'<b><tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Пополнение через ЮMoney</b>\n\n'
                        f'Сумма: <b>{amount}₽</b>\n'
                        f'Кошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
                        f'<a href="{payment_url}">Нажмите для оплаты</a>\n\n'
                        '⚠️ После оплаты нажмите кнопку проверки',
                        reply_markup=check_payment_keyboard(payment_id, "yoomoney")
                    )
            
            elif method == "crypto":
                invoice = await create_crypto_bot_invoice(amount, payment_id)
                
                if invoice and invoice.get("ok"):
                    result = invoice.get("result", {})
                    pay_url = result.get("pay_url")
                    invoice_id = result.get("invoice_id")
                    
                    await message.answer(
                        f'<b><tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> Пополнение через Crypto Bot</b>\n\n'
                        f'Сумма: <b>{amount}₽</b>\n\n'
                        f'<a href="{pay_url}">Нажмите для оплаты</a>\n\n'
                        '⚠️ После оплаты нажмите кнопку проверки',
                        reply_markup=check_payment_keyboard(str(invoice_id), "crypto")
                    )
        
        except ValueError:
            await message.answer(
                '<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Введите корректную сумму</b>'
            )
        return
    
    # Проверяем добавление аккаунта (админ)
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]
        step = acc_data.get('step')
        
        if step == 'phone':
            # Сохраняем номер и запрашиваем код
            phone = text
            acc_data['phone'] = phone
            
            # Отправляем код через Telethon
            result = await send_code_to_phone(phone)
            
            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']
                acc_data['step'] = 'code'
                
                await message.answer(
                    f'<b><tg-emoji emoji-id="5940433880585605708">🔨</tg-emoji> Код отправлен на {phone}</b>\n\n'
                    'Введите код из Telegram:'
                )
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Ошибка отправки кода</b>\n\n'
                    f'{result.get("error", "Неизвестная ошибка")}',
                    reply_markup=admin_keyboard()
                )
        
        elif step == 'code':
            # Проверяем код
            code = text
            phone = acc_data['phone']
            phone_code_hash = acc_data['phone_code_hash']
            
            result = await verify_code_and_get_session(phone, code, phone_code_hash)
            
            if result['success']:
                # Сохраняем аккаунт
                async with async_session() as session:
                    account = Account(
                        phone=phone,
                        session_string=result['session_string'],
                        is_verified=True
                    )
                    session.add(account)
                    await session.commit()
                
                del dp.awaiting_accounts[user_id]
                
                await message.answer(
                    '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Аккаунт успешно добавлен!</b>\n\n'
                    f'Номер: <code>{phone}</code>\n'
                    f'Статус: верифицирован',
                    reply_markup=admin_keyboard()
                )
            elif result.get('need_password'):
                acc_data['step'] = 'password'
                await message.answer(
                    '<b><tg-emoji emoji-id="6037249452824072506">🔒</tg-emoji> Требуется 2FA пароль</b>\n\n'
                    'Введите пароль облачной защиты:'
                )
            else:
                del dp.awaiting_accounts[user_id]
                await message.answer(
                    f'<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Ошибка верификации</b>\n\n'
                    f'{result.get("error", "Неверный код")}',
                    reply_markup=admin_keyboard()
                )
        
        elif step == 'password':
            password = text
            phone = acc_data['phone']
            
            result = await verify_2fa_password(phone, password)
            
            if result['success']:
                async with async_session() as session:
                    account = Account(
                        phone=phone,
                        session_string=result['session_string'],
                        is_verified=True
                    )
                    session.add(account)
                    await session.commit()
                
                del dp.awaiting_accounts[user_id]
                
                await message.answer(
                    '<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Аккаунт успешно добавлен!</b>\n\n'
                    f'Номер: <code>{phone}</code>\n'
                    f'Статус: верифицирован',
                    reply_markup=admin_keyboard()
                )
            else:
                await message.answer(
                    f'<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Ошибка</b>\n\n'
                    f'{result.get("error", "Неверный пароль")}\n\n'
                    'Попробуйте еще раз:'
                )
        
        return
    
    # Проверяем ожидание рассылки (админ)
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
            f'<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Рассылка завершена</b>\n\n'
            f'Отправлено: <b>{sent}</b> из <b>{len(users)}</b>',
            reply_markup=admin_keyboard()
        )
        return
    
    # Обычное сообщение
    await message.answer(
        '<b><tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> Используйте кнопки меню для навигации</b>',
        reply_markup=main_menu_keyboard()
    )

# ===== ЗАПУСК БОТА =====
async def setup_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")

async def main():
    await setup_db()
    
    if not hasattr(dp, 'pending_accounts'):
        dp.pending_accounts = {}
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    if not hasattr(dp, 'awaiting_accounts'):
        dp.awaiting_accounts = {}
    if not hasattr(dp, 'awaiting_broadcast'):
        dp.awaiting_broadcast = set()
    
    dp.include_router(router)
    
    logger.info("Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
