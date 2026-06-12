import asyncio
import logging
import os
import re
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
                        String, select)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from telethon import TelegramClient

# ===== НАСТРОЙКИ =====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
YOOMONEY_WALLET = "4100119286550472"
CRYPTO_BOT_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_IDS = [7973988177]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== TELETHON CLIENT =====
telethon_client = TelegramClient('vest_bot_session', API_ID, API_HASH)

# ===== БАЗА ДАННЫХ =====
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    balance = Column(Float, default=0.0)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    country = Column(String(50), default="США")
    is_sold = Column(Boolean, default=False)
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

engine = create_async_engine(DATABASE_URL)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ===== ИНИЦИАЛИЗАЦИЯ =====
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# ===== КЛАВИАТУРЫ =====
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Купить аккаунт",
        callback_data="buy_account",
        icon_custom_emoji_id="5963103826075456248"
    ))
    builder.row(InlineKeyboardButton(
        text="Мои покупки",
        callback_data="my_purchases",
        icon_custom_emoji_id="5884479287171485878"
    ))
    builder.row(InlineKeyboardButton(
        text="Профиль",
        callback_data="profile",
        icon_custom_emoji_id="5870994129244131212"
    ))
    return builder.as_markup()

def countries_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="США - 20₽",
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
        text="Назад",
        callback_data="my_purchases",
        icon_custom_emoji_id="5893057118545646106"
    ))
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Пополнить баланс",
        callback_data="deposit_balance",
        icon_custom_emoji_id="5879814368572478751"
    ))
    builder.row(InlineKeyboardButton(
        text="Назад",
        callback_data="main_menu",
        icon_custom_emoji_id="5893057118545646106"
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
        text="Назад",
        callback_data="main_menu",
        icon_custom_emoji_id="5893057118545646106"
    ))
    return builder.as_markup()

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
            select(Account).where(Account.is_sold == False).limit(1)
        )
        return result.scalar_one_or_none()

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
        payload = {
            "asset": "USDT",
            "amount": str(amount),
            "description": f"Покупка аккаунта #{payment_id}",
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

async def get_telegram_code(phone: str) -> Optional[str]:
    try:
        if not telethon_client.is_connected():
            await telethon_client.start()
        
        async for dialog in telethon_client.iter_dialogs():
            if dialog.name and "42777" in dialog.name:
                messages = await telethon_client.get_messages(dialog, limit=1)
                if messages and len(messages) > 0:
                    text = messages[0].text
                    if text:
                        codes = re.findall(r'\b\d{5}\b', text)
                        if codes:
                            return codes[0]
        return None
    except Exception as e:
        logger.error(f"Error getting Telegram code: {e}")
        return None

async def generate_payment_id():
    return f"vest_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"

# ===== ОБРАБОТЧИКИ =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    
    welcome_text = f"""<b><tg-emoji emoji-id="6030400221232501136">🤖</tg-emoji> Vest Account</b>

<tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Покупайте аккаунты быстро и безопасно

Выберите действие:"""
    
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            "<b><tg-emoji emoji-id=\"5870657884844462243\">❌</tg-emoji> У вас нет доступа к админ-панели</b>"
        )
        return
    
    await message.answer(
        "<b><tg-emoji emoji-id=\"5870921681735781843\">📊</tg-emoji> Админ-панель</b>",
        reply_markup=admin_keyboard()
    )

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    await callback.answer()
    text = f"""<b><tg-emoji emoji-id="5873147866364514353">🏘</tg-emoji> Главное меню</b>

Выберите действие:"""
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "buy_account")
async def callback_buy_account(callback: CallbackQuery):
    await callback.answer()
    text = f"""<b><tg-emoji emoji-id="6042011682497106307">📍</tg-emoji> Выберите страну:</b>"""
    await callback.message.edit_text(text, reply_markup=countries_keyboard())

@router.callback_query(F.data.startswith("country_"))
async def callback_country(callback: CallbackQuery):
    await callback.answer()
    country = callback.data.replace("country_", "")
    account = await get_available_account()
    
    if account:
        # Сохраняем данные в состоянии пользователя
        # В aiogram 3 используем callback.message для хранения временных данных
        text = f"""<b><tg-emoji emoji-id="6035128606563241721">🖼</tg-emoji> Найден аккаунт!</b>

<tg-emoji emoji-id="6042011682497106307">📍</tg-emoji> Страна: {country}
<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Цена: {account.price}₽

Выберите способ оплаты:"""
        
        # Сохраняем ID аккаунта в тексте кнопок
        keyboard = payment_methods_keyboard()
        # Добавляем account_id в callback_data для отслеживания
        await callback.message.edit_text(text, reply_markup=keyboard)
        
        # Сохраняем в глобальную переменную (временное решение)
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {
            'account_id': account.id,
            'price': account.price
        }
    else:
        text = f"""<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Нет доступных аккаунтов</b>

К сожалению, все аккаунты {country} распроданы."""
        await callback.message.edit_text(text, reply_markup=countries_keyboard())

@router.callback_query(F.data == "pay_balance")
async def callback_pay_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    pending = dp.pending_accounts.get(callback.from_user.id, {})
    price = pending.get('price', 20)
    account_id = pending.get('account_id')
    
    if user.balance >= price and account_id:
        async with async_session() as session:
            user.balance -= price
            
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
            
            text = f"""<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Оплата успешна!</b>

<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер аккаунта: <code>{account.phone}</code>
<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Сумма: {price}₽

Нажмите кнопку, чтобы получить код:"""
            
            await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        text = f"""<b><tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Недостаточно средств</b>

Ваш баланс: {user.balance:.2f}₽
Необходимо: {price}₽

Пополните баланс в профиле."""
        await callback.message.edit_text(text, reply_markup=payment_methods_keyboard())

@router.callback_query(F.data == "pay_yoomoney")
async def callback_pay_yoomoney(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {})
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
        text = f"""<b><tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Оплата через ЮMoney</b>

Сумма: {price}₽
Кошелек: <code>{YOOMONEY_WALLET}</code>

<tg-emoji emoji-id="6039451237743595514">📎</tg-emoji> <a href='{payment_url}'>Ссылка на оплату</a>
<tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> ID платежа: <code>{payment_id}</code>

⚠️ После оплаты нажмите кнопку "Проверить оплату\""""
        
        await callback.message.edit_text(text, reply_markup=check_payment_keyboard(payment_id, "yoomoney"))
    else:
        await callback.message.edit_text(
            "<b><tg-emoji emoji-id=\"5870657884844462243\">❌</tg-emoji> Ошибка создания платежа</b>",
            reply_markup=payment_methods_keyboard()
        )

@router.callback_query(F.data == "pay_crypto")
async def callback_pay_crypto(callback: CallbackQuery):
    await callback.answer()
    pending = dp.pending_accounts.get(callback.from_user.id, {})
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
        
        text = f"""<b><tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> Оплата через Crypto Bot</b>

Сумма: {price}₽

<tg-emoji emoji-id="6039451237743595514">📎</tg-emoji> <a href='{pay_url}'>Ссылка на оплату</a>
<tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> ID платежа: <code>{invoice_id}</code>

⚠️ После оплаты нажмите кнопку "Проверить оплату\""""
        
        await callback.message.edit_text(text, reply_markup=check_payment_keyboard(str(invoice_id), "crypto"))
    else:
        await callback.message.edit_text(
            "<b><tg-emoji emoji-id=\"5870657884844462243\">❌</tg-emoji> Ошибка создания счета</b>",
            reply_markup=payment_methods_keyboard()
        )

@router.callback_query(F.data == "pay_stars")
async def callback_pay_stars(callback: CallbackQuery):
    await callback.answer()
    text = f"""<b><tg-emoji emoji-id="6041731551845159060">🎉</tg-emoji> Оплата Telegram Stars</b>

Для покупки аккаунта через Telegram Stars, напишите: @v3estnikov"""
    
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
        pending = dp.pending_accounts.get(callback.from_user.id, {})
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
                
                text = f"""<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Оплата подтверждена!</b>

<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер аккаунта: <code>{account.phone}</code>
<tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Сумма: {price}₽

Нажмите кнопку, чтобы получить код:"""
                
                await callback.message.edit_text(text, reply_markup=get_code_keyboard(purchase.id))
    else:
        await callback.answer("Платеж еще не получен. Попробуйте позже.", show_alert=True)

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
            
            if account:
                code = await get_telegram_code(account.phone)
                
                if code:
                    purchase.code_sent = True
                    await session.commit()
                    
                    text = f"""<b><tg-emoji emoji-id="5940433880585605708">🔨</tg-emoji> Код подтверждения</b>

<tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Номер: <code>{account.phone}</code>
<tg-emoji emoji-id="6037249452824072506">🔒</tg-emoji> Код: <code>{code}</code>

Сохраните код в надежном месте."""
                    
                    builder = InlineKeyboardBuilder()
                    builder.row(InlineKeyboardButton(
                        text="В главное меню",
                        callback_data="main_menu",
                        icon_custom_emoji_id="5873147866364514353"
                    ))
                    
                    await callback.message.edit_text(text, reply_markup=builder.as_markup())
                else:
                    await callback.answer("Не удалось получить код. Попробуйте позже.", show_alert=True)
            else:
                await callback.answer("Аккаунт не найден.", show_alert=True)
        else:
            await callback.answer("Код уже получен или покупка не ваша.", show_alert=True)

@router.callback_query(F.data == "my_purchases")
async def callback_my_purchases(callback: CallbackQuery):
    await callback.answer()
    
    async with async_session() as session:
        result = await session.execute(
            select(Purchase).where(Purchase.user_id == callback.from_user.id).order_by(Purchase.created_at.desc())
        )
        purchases = result.scalars().all()
        
        if purchases:
            text = f"<b><tg-emoji emoji-id=\"5884479287171485878\">📦</tg-emoji> Ваши покупки:</b>\n\n"
            
            builder = InlineKeyboardBuilder()
            for purchase in purchases:
                account = await session.get(Account, purchase.account_id)
                status = "✅ Получен" if purchase.code_sent else "⏳ Ожидает"
                phone = account.phone if account else "Н/Д"
                
                text += f"<tg-emoji emoji-id=\"5886285355279193209\">🏷</tg-emoji> {phone} - {purchase.amount}₽ ({status})\n"
                
                if not purchase.code_sent:
                    builder.row(InlineKeyboardButton(
                        text=f"Получить код для {phone}",
                        callback_data=f"get_code_{purchase.id}",
                        icon_custom_emoji_id="5940433880585605708"
                    ))
            
            builder.row(InlineKeyboardButton(
                text="Назад",
                callback_data="main_menu",
                icon_custom_emoji_id="5893057118545646106"
            ))
            
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        else:
            text = f"""<b><tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Мои покупки</b>

У вас пока нет покупок."""
            await callback.message.edit_text(text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    
    text = f"""<b><tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Профиль</b>

<tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Баланс: <b>{user.balance:.2f}₽</b>
<tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> Дата регистрации: {user.created_at.strftime('%d.%m.%Y')}"""
    
    await callback.message.edit_text(text, reply_markup=profile_keyboard())

@router.callback_query(F.data == "deposit_balance")
async def callback_deposit_balance(callback: CallbackQuery):
    await callback.answer()
    text = f"""<b><tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Пополнение баланса</b>

Выберите способ пополнения:"""
    await callback.message.edit_text(text, reply_markup=deposit_keyboard())

@router.callback_query(F.data == "deposit_yoomoney")
async def callback_deposit_yoomoney(callback: CallbackQuery):
    await callback.answer("Введите сумму пополнения в чат (от 100₽)", show_alert=True)
    # Устанавливаем состояние ожидания
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'yoomoney'

@router.callback_query(F.data == "deposit_crypto")
async def callback_deposit_crypto(callback: CallbackQuery):
    await callback.answer("Введите сумму пополнения в чат (от 100₽)", show_alert=True)
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'

@router.callback_query(F.data.startswith("admin_"))
async def callback_admin(callback: CallbackQuery):
    await callback.answer()
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет доступа", show_alert=True)
        return
    
    data = callback.data
    
    if data == "admin_stats":
        async with async_session() as session:
            result = await session.execute(select(User))
            total_users = len(result.scalars().all())
            
            result = await session.execute(select(Account))
            total_accounts = len(result.scalars().all())
            
            result = await session.execute(select(Account).where(Account.is_sold == True))
            sold_accounts = len(result.scalars().all())
            
            result = await session.execute(select(Purchase))
            total_purchases = len(result.scalars().all())
            
            stats_text = f"""<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Статистика</b>

<tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Пользователей: {total_users}
<tg-emoji emoji-id="5884479287171485878">📦</tg-emoji> Аккаунтов: {total_accounts} (продано: {sold_accounts})
<tg-emoji emoji-id="5963103826075456248">⬆</tg-emoji> Покупок: {total_purchases}"""
            
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
            """<b><tg-emoji emoji-id="6039422865189638057">📣</tg-emoji> Отправьте сообщение для рассылки</b>

Отправьте текст, фото или другое сообщение, которое нужно разослать всем пользователям.""",
            reply_markup=builder.as_markup()
        )
    
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = set()
        dp.awaiting_accounts.add(callback.from_user.id)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="Отмена",
            callback_data="main_menu",
            icon_custom_emoji_id="5870657884844462243"
        ))
        
        await callback.message.edit_text(
            """<b><tg-emoji emoji-id="5771851822897566479">🔡</tg-emoji> Добавление аккаунтов</b>

Отправьте номера телефонов (каждый с новой строки):
<code>+79001234567
+79007654321</code>""",
            reply_markup=builder.as_markup()
        )

@router.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    
    # Проверяем ожидание суммы депозита
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)
        
        try:
            amount = float(message.text)
            if amount < 100:
                await message.answer(
                    "<b><tg-emoji emoji-id=\"5870657884844462243\">❌</tg-emoji> Минимальная сумма: 100₽</b>",
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
                        f"""<b><tg-emoji emoji-id="5904462880941545555">💵</tg-emoji> Пополнение через ЮMoney</b>

Сумма: {amount}₽
Кошелек: <code>{YOOMONEY_WALLET}</code>

<tg-emoji emoji-id="6039451237743595514">📎</tg-emoji> <a href='{payment_url}'>Ссылка на оплату</a>
<tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> ID: <code>{payment_id}</code>

⚠️ После оплаты нажмите кнопку "Проверить оплату\"""",
                        reply_markup=check_payment_keyboard(payment_id, "yoomoney")
                    )
            
            elif method == "crypto":
                invoice = await create_crypto_bot_invoice(amount, payment_id)
                
                if invoice and invoice.get("ok"):
                    result = invoice.get("result", {})
                    pay_url = result.get("pay_url")
                    invoice_id = result.get("invoice_id")
                    
                    await message.answer(
                        f"""<b><tg-emoji emoji-id="5260752406890711732">👾</tg-emoji> Пополнение через Crypto Bot</b>

Сумма: {amount}₽

<tg-emoji emoji-id="6039451237743595514">📎</tg-emoji> <a href='{pay_url}'>Ссылка на оплату</a>
<tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> ID: <code>{invoice_id}</code>

⚠️ После оплаты нажмите кнопку "Проверить оплату\"""",
                        reply_markup=check_payment_keyboard(str(invoice_id), "crypto")
                    )
        
        except ValueError:
            await message.answer(
                "<b><tg-emoji emoji-id=\"5870657884844462243\">❌</tg-emoji> Введите корректную сумму</b>"
            )
        return
    
    # Проверяем ожидание аккаунтов (админ)
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        dp.awaiting_accounts.remove(user_id)
        
        phones = [phone.strip() for phone in message.text.split('\n') if phone.strip()]
        
        async with async_session() as session:
            added = 0
            for phone in phones:
                result = await session.execute(
                    select(Account).where(Account.phone == phone)
                )
                if not result.scalar_one_or_none():
                    account = Account(phone=phone, country="США")
                    session.add(account)
                    added += 1
            
            await session.commit()
        
        await message.answer(
            f"""<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Аккаунты добавлены!</b>

Добавлено: {added} из {len(phones)} аккаунтов.""",
            reply_markup=admin_keyboard()
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
                except:
                    continue
        
        await message.answer(
            f"""<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Рассылка завершена</b>

Отправлено: {sent} из {len(users)} пользователей""",
            reply_markup=admin_keyboard()
        )
        return
    
    # Обычное сообщение
    await message.answer(
        "<b><tg-emoji emoji-id=\"6028435952299413210\">ℹ</tg-emoji> Используйте кнопки меню для навигации</b>",
        reply_markup=main_menu_keyboard()
    )

# ===== ЗАПУСК БОТА =====
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def main():
    await setup_db()
    
    # Запускаем Telethon
    try:
        await telethon_client.start()
        logger.info("Telethon client started")
    except Exception as e:
        logger.error(f"Telethon client error: {e}")
    
    # Инициализируем хранилища
    dp.pending_accounts = {}
    dp.awaiting_deposit = {}
    dp.awaiting_accounts = set()
    dp.awaiting_broadcast = set()
    
    dp.include_router(router)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
