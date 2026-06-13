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

# ============================================================
# НАСТРОЙКИ
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Автоматическая замена схемы для asyncpg
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# Платежные данные
YOOMONEY_WALLET = "4100119286550472"
CLIENT_ID = os.getenv("CLIENT_ID", "")
CRYPTO_BOT_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"

# Telethon данные
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"

# Администраторы
ADMIN_IDS = [7973988177]

# Логирование
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# МОДЕЛИ БАЗЫ ДАННЫХ
# ============================================================
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
    total_spent = Column(Float, default=0.0)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User(id={self.telegram_id}, balance={self.balance})>"


class Account(Base):
    """Аккаунт для продажи"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    country = Column(String(50), default="США")
    session_string = Column(Text, nullable=True)
    is_sold = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    price = Column(Float, default=20.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Account(phone={self.phone}, verified={self.is_verified}, sold={self.is_sold})>"


class Purchase(Base):
    """Покупка аккаунта"""
    __tablename__ = "purchases"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    account_id = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    payment_method = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    code_sent = Column(Boolean, default=False)

    def __repr__(self):
        return f"<Purchase(id={self.id}, user={self.user_id}, amount={self.amount})>"


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
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Payment(id={self.payment_id}, status={self.status})>"


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================================
try:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Failed to create database engine: {e}")
    sys.exit(1)


# ============================================================
# АВТОМАТИЧЕСКАЯ МИГРАЦИЯ
# ============================================================
async def run_migrations():
    """Добавляет недостающие колонки в существующие таблицы"""
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
                    logger.info(f"Migration executed: {migration[:50]}...")
                except Exception as e:
                    logger.debug(f"Migration skipped: {e}")
            await conn.commit()
        logger.info("All migrations completed successfully")
    except Exception as e:
        logger.error(f"Migration error: {e}")


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# Временное хранилище для авторизаций Telethon
pending_auth = {}


# ============================================================
# ПРЕМИУМ ЭМОДЗИ
# ============================================================
PREMIUM_EMOJI = {
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
    'link': '6039451237743595514',
    'gift': '6032644646587338669',
}


def premium_emoji(name: str) -> str:
    """
    Возвращает HTML-тег премиум эмодзи.
    Использует zero-width space (​) для избежания ошибки ENTITY_TEXT_INVALID.
    """
    emoji_id = PREMIUM_EMOJI.get(name, PREMIUM_EMOJI['info'])
    return f'<tg-emoji emoji-id="{emoji_id}">​</tg-emoji>'


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Купить аккаунт",
            callback_data="buy_account",
            icon_custom_emoji_id=PREMIUM_EMOJI['buy']
        ),
        InlineKeyboardButton(
            text="Мои покупки",
            callback_data="my_purchases",
            icon_custom_emoji_id=PREMIUM_EMOJI['box']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Профиль",
            callback_data="profile",
            icon_custom_emoji_id=PREMIUM_EMOJI['profile']
        ),
        InlineKeyboardButton(
            text="Пополнить",
            callback_data="deposit_balance",
            icon_custom_emoji_id=PREMIUM_EMOJI['wallet']
        )
    )
    return builder.as_markup()


def countries_keyboard() -> InlineKeyboardMarkup:
    """Выбор страны"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="США • 20₽",
            callback_data="country_USA",
            icon_custom_emoji_id=PREMIUM_EMOJI['location']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Назад",
            callback_data="main_menu",
            icon_custom_emoji_id=PREMIUM_EMOJI['back']
        )
    )
    return builder.as_markup()


def account_found_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой КУПИТЬ"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="КУПИТЬ",
            callback_data="show_payment_methods",
            icon_custom_emoji_id=PREMIUM_EMOJI['buy']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Назад",
            callback_data="buy_account",
            icon_custom_emoji_id=PREMIUM_EMOJI['back']
        )
    )
    return builder.as_markup()


def payment_methods_keyboard() -> InlineKeyboardMarkup:
    """Способы оплаты"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Баланс бота",
            callback_data="pay_balance",
            icon_custom_emoji_id=PREMIUM_EMOJI['wallet']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="ЮMoney",
            callback_data="pay_yoomoney",
            icon_custom_emoji_id=PREMIUM_EMOJI['money']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Crypto Bot",
            callback_data="pay_crypto",
            icon_custom_emoji_id=PREMIUM_EMOJI['crypto']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Telegram Stars",
            callback_data="pay_stars",
            icon_custom_emoji_id=PREMIUM_EMOJI['star']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Назад",
            callback_data="buy_account",
            icon_custom_emoji_id=PREMIUM_EMOJI['back']
        )
    )
    return builder.as_markup()


def check_payment_keyboard(
    payment_id: str,
    method: str,
    is_deposit: bool = True
) -> InlineKeyboardMarkup:
    """Клавиатура проверки платежа"""
    prefix = "check_deposit" if is_deposit else "check_purchase"
    callback_data = f"{prefix}_{method}_{payment_id}"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Проверить оплату",
            callback_data=callback_data,
            icon_custom_emoji_id=PREMIUM_EMOJI['loading']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data="main_menu",
            icon_custom_emoji_id=PREMIUM_EMOJI['cross']
        )
    )
    return builder.as_markup()


def get_code_keyboard(purchase_id: int) -> InlineKeyboardMarkup:
    """Клавиатура получения кода"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Получить код",
            callback_data=f"get_code_{purchase_id}",
            icon_custom_emoji_id=PREMIUM_EMOJI['code']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="К покупкам",
            callback_data="my_purchases",
            icon_custom_emoji_id=PREMIUM_EMOJI['box']
        )
    )
    return builder.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    """Профиль"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Пополнить баланс",
            callback_data="deposit_balance",
            icon_custom_emoji_id=PREMIUM_EMOJI['wallet']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Мои покупки",
            callback_data="my_purchases",
            icon_custom_emoji_id=PREMIUM_EMOJI['box']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="В меню",
            callback_data="main_menu",
            icon_custom_emoji_id=PREMIUM_EMOJI['home']
        )
    )
    return builder.as_markup()


def deposit_keyboard() -> InlineKeyboardMarkup:
    """Пополнение баланса"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="ЮMoney",
            callback_data="deposit_yoomoney",
            icon_custom_emoji_id=PREMIUM_EMOJI['money']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Crypto Bot",
            callback_data="deposit_crypto",
            icon_custom_emoji_id=PREMIUM_EMOJI['crypto']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Назад",
            callback_data="profile",
            icon_custom_emoji_id=PREMIUM_EMOJI['back']
        )
    )
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    """Админ-панель"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Статистика",
            callback_data="admin_stats",
            icon_custom_emoji_id=PREMIUM_EMOJI['stats']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Рассылка",
            callback_data="admin_broadcast",
            icon_custom_emoji_id=PREMIUM_EMOJI['broadcast']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Добавить аккаунты",
            callback_data="admin_add_accounts",
            icon_custom_emoji_id=PREMIUM_EMOJI['add']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Управление балансом",
            callback_data="admin_balance",
            icon_custom_emoji_id=PREMIUM_EMOJI['edit']
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="В меню",
            callback_data="main_menu",
            icon_custom_emoji_id=PREMIUM_EMOJI['home']
        )
    )
    return builder.as_markup()


# ============================================================
# TELETHON ФУНКЦИИ
# ============================================================
async def create_telethon_client(session_string: str = None) -> TelegramClient:
    """
    Создает клиент Telethon.
    Если передан session_string - используется существующая сессия,
    иначе создается новая.
    """
    if session_string:
        client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH
        )
    else:
        client = TelegramClient(
            StringSession(),
            API_ID,
            API_HASH
        )
    return client


async def send_code_to_phone(phone: str) -> dict:
    """
    Отправляет код подтверждения на номер телефона через Telethon.
    Возвращает словарь с результатом.
    """
    try:
        client = await create_telethon_client()
        await client.connect()

        sent = await client.send_code_request(phone)

        # Сохраняем данные для последующей верификации
        pending_auth[phone] = {
            'client': client,
            'phone_code_hash': sent.phone_code_hash,
            'phone': phone
        }

        logger.info(f"Verification code sent to {phone}")
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


async def verify_code_and_get_session(
    phone: str,
    code: str,
    phone_code_hash: str
) -> dict:
    """
    Проверяет код подтверждения и возвращает строку сессии.
    """
    try:
        auth_data = pending_auth.get(phone)
        if not auth_data:
            return {
                'success': False,
                'error': 'Сессия не найдена. Отправьте номер заново.'
            }

        client = auth_data['client']

        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash
            )
        except SessionPasswordNeededError:
            return {
                'success': False,
                'need_password': True,
                'error': 'Требуется 2FA пароль'
            }

        # Сохраняем сессию
        session_string = client.session.save()
        await client.disconnect()

        # Удаляем временные данные
        pending_auth.pop(phone, None)

        logger.info(f"Successfully verified {phone}")
        return {
            'success': True,
            'session_string': session_string
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


async def verify_2fa_password(phone: str, password: str) -> dict:
    """
    Подтверждает 2FA пароль и возвращает строку сессии.
    """
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
        await client.disconnect()

        pending_auth.pop(phone, None)

        logger.info(f"Successfully verified 2FA for {phone}")
        return {
            'success': True,
            'session_string': session_string
        }

    except PasswordHashInvalidError:
        return {
            'success': False,
            'error': 'Неверный пароль 2FA'
        }
    except Exception as e:
        logger.error(f"Error verifying 2FA for {phone}: {e}")
        return {
            'success': False,
            'error': str(e)
        }


async def get_code_from_session(session_string: str) -> Optional[str]:
    """
    Получает код подтверждения из чата с +42777,
    используя сохраненную сессию аккаунта.
    """
    client = None
    try:
        client = await create_telethon_client(session_string)
        await client.connect()

        # Проверяем авторизацию
        if not await client.is_user_authorized():
            logger.error("Session is not authorized")
            return None

        # Ищем диалог с +42777
        async for dialog in client.iter_dialogs():
            if dialog.name and "42777" in dialog.name:
                logger.info(f"Found dialog: {dialog.name}")

                # Получаем последние сообщения
                messages = await client.get_messages(dialog, limit=5)
                for message in messages:
                    if message.text:
                        # Ищем 5-значный код
                        codes = re.findall(r'\b\d{5}\b', message.text)
                        if codes:
                            logger.info(f"Found code: {codes[0]}")
                            return codes[0]

        logger.info("No code found in any dialog")
        return None

    except Exception as e:
        logger.error(f"Error getting code from session: {e}")
        return None

    finally:
        if client:
            await client.disconnect()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
async def get_user(user_id: int) -> Optional[User]:
    """Получает пользователя по Telegram ID"""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        return result.scalar_one_or_none()


async def get_or_create_user(
    user_id: int,
    username: str = None
) -> User:
    """Получает или создает пользователя"""
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
            logger.info(f"New user created: {user_id}")
    return user


async def get_available_account() -> Optional[Account]:
    """Получает первый доступный для продажи аккаунт"""
    async with async_session() as session:
        result = await session.execute(
            select(Account).where(
                Account.is_sold == False,
                Account.is_verified == True,
                Account.session_string != None,
                Account.session_string != ""
            ).limit(1)
        )
        account = result.scalar_one_or_none()
        if account:
            logger.info(f"Found available account: {account.phone}")
        else:
            logger.info("No available accounts found")
        return account


async def create_yoomoney_payment(
    amount: float,
    payment_id: str
) -> Optional[str]:
    """Создает ссылку на оплату через YooMoney"""
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
    """
    Проверяет статус платежа через YooMoney API.
    Требует CLIENT_ID в переменных окружения.
    """
    if not CLIENT_ID:
        logger.error("CLIENT_ID not set in environment")
        return False

    try:
        url = "https://yoomoney.ru/api/operation-history"
        headers = {
            "Authorization": f"Bearer {CLIENT_ID}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "label": payment_id,
            "records": 10,
            "type": "deposition"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                data=data,
                timeout=30
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    operations = result.get("operations", [])
                    for op in operations:
                        if (
                            op.get("label") == payment_id
                            and op.get("status") == "success"
                        ):
                            logger.info(f"Payment {payment_id} found and confirmed")
                            return True
                    logger.info(f"Payment {payment_id} not found in operations")
                else:
                    logger.error(f"YooMoney API error: {response.status}")

        return False

    except Exception as e:
        logger.error(f"YooMoney check error: {e}")
        return False


async def create_crypto_bot_invoice(
    amount: float,
    payment_id: str
) -> Optional[dict]:
    """Создает счет в Crypto Bot"""
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}

        # Конвертируем рубли в USDT (курс 90)
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
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=30
            ) as response:
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
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=30
            ) as response:
                data = await response.json()
                if data.get("ok") and data.get("result", {}).get("items"):
                    invoice = data["result"]["items"][0]
                    logger.info(f"Invoice {invoice_id} status: {invoice.get('status')}")
                    return invoice

        return None

    except Exception as e:
        logger.error(f"Crypto Bot check error: {e}")
        return None


async def generate_payment_id() -> str:
    """Генерирует уникальный ID платежа"""
    timestamp = int(datetime.now().timestamp())
    random_bytes = os.urandom(4).hex()
    return f"vest_{timestamp}_{random_bytes}"


# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username
    )

    welcome_text = (
        f'{premium_emoji("bot")}<b>Vest Account</b>\n\n'
        f'{premium_emoji("lock")}Покупка аккаунтов\n'
        f'{premium_emoji("loading")}Быстро и безопасно\n\n'
        '<i>Выберите действие:</i>'
    )

    await message.answer(
        welcome_text,
        reply_markup=main_menu_keyboard()
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Обработчик команды /admin"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            f'{premium_emoji("cross")}<b>Доступ запрещен</b>'
        )
        return

    await message.answer(
        f'{premium_emoji("stats")}<b>Админ-панель</b>',
        reply_markup=admin_keyboard()
    )


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - НАВИГАЦИЯ
# ============================================================
@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()

    text = (
        f'{premium_emoji("home")}<b>Главное меню</b>\n\n'
        '<i>Выберите действие:</i>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=main_menu_keyboard()
    )


@router.callback_query(F.data == "buy_account")
async def callback_buy_account(callback: CallbackQuery):
    """Покупка аккаунта - выбор страны"""
    await callback.answer()

    account = await get_available_account()

    text = f'{premium_emoji("location")}<b>Выберите страну</b>\n\n'
    if account:
        text += f'{premium_emoji("check")}Аккаунты в наличии'
    else:
        text += f'{premium_emoji("cross")}Нет доступных аккаунтов'

    await callback.message.edit_text(
        text,
        reply_markup=countries_keyboard()
    )


@router.callback_query(F.data.startswith("country_"))
async def callback_country(callback: CallbackQuery):
    """Выбор страны - показ найденного аккаунта"""
    await callback.answer()

    country = callback.data.replace("country_", "")
    account = await get_available_account()

    if account:
        # Сохраняем данные аккаунта для покупки
        if not hasattr(dp, 'pending_accounts'):
            dp.pending_accounts = {}
        dp.pending_accounts[callback.from_user.id] = {
            'account_id': account.id,
            'price': account.price
        }

        text = (
            f'{premium_emoji("check")}<b>Аккаунт найден!</b>\n\n'
            f'{premium_emoji("location")}Страна: <b>{country}</b>\n'
            f'{premium_emoji("money")}Цена: <b>{account.price}₽</b>\n\n'
            '<i>Нажмите КУПИТЬ для продолжения</i>'
        )

        await callback.message.edit_text(
            text,
            reply_markup=account_found_keyboard()
        )
    else:
        text = (
            f'{premium_emoji("cross")}<b>Нет доступных аккаунтов</b>\n\n'
            'Все аккаунты распроданы. Попробуйте позже.'
        )

        await callback.message.edit_text(
            text,
            reply_markup=countries_keyboard()
        )


@router.callback_query(F.data == "show_payment_methods")
async def callback_show_payment_methods(callback: CallbackQuery):
    """Показ способов оплаты после нажатия КУПИТЬ"""
    await callback.answer()

    # Получаем сохраненные данные
    pending = {}
    if hasattr(dp, 'pending_accounts'):
        pending = dp.pending_accounts.get(callback.from_user.id, {})

    price = pending.get('price', 20)

    text = (
        f'{premium_emoji("buy")}<b>Покупка аккаунта</b>\n\n'
        f'{premium_emoji("money")}Сумма к оплате: <b>{price}₽</b>\n\n'
        '<i>Выберите способ оплаты:</i>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=payment_methods_keyboard()
    )


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - ОПЛАТА
# ============================================================
@router.callback_query(F.data == "pay_balance")
async def callback_pay_balance(callback: CallbackQuery):
    """Оплата с баланса бота"""
    await callback.answer()

    user = await get_user(callback.from_user.id)

    pending = {}
    if hasattr(dp, 'pending_accounts'):
        pending = dp.pending_accounts.get(callback.from_user.id, {})

    price = pending.get('price', 20)
    account_id = pending.get('account_id')

    if not account_id:
        await callback.message.edit_text(
            f'{premium_emoji("cross")}<b>Ошибка. Начните заново.</b>',
            reply_markup=main_menu_keyboard()
        )
        return

    if user.balance >= price:
        async with async_session() as session:
            # Загружаем объекты в сессию
            user = await session.get(User, user.id)
            account = await session.get(Account, account_id)

            if account.is_sold:
                await callback.message.edit_text(
                    f'{premium_emoji("cross")}<b>Аккаунт уже продан.</b>',
                    reply_markup=main_menu_keyboard()
                )
                return

            # Списываем средства
            user.balance -= price
            user.total_spent = (user.total_spent or 0) + price
            account.is_sold = True

            # Создаем запись о покупке
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
                f'{premium_emoji("check")}<b>Оплата успешна!</b>\n\n'
                f'{premium_emoji("tag")}Номер: <code>{account.phone}</code>\n'
                f'{premium_emoji("money")}Сумма: <b>{price}₽</b>\n\n'
                'Нажмите чтобы получить код:'
            )

            await callback.message.edit_text(
                text,
                reply_markup=get_code_keyboard(purchase.id)
            )
    else:
        text = (
            f'{premium_emoji("cross")}<b>Недостаточно средств</b>\n\n'
            f'{premium_emoji("wallet")}Баланс: <b>{user.balance:.0f}₽</b>\n'
            f'{premium_emoji("money")}Нужно: <b>{price}₽</b>'
        )

        await callback.message.edit_text(
            text,
            reply_markup=payment_methods_keyboard()
        )


@router.callback_query(F.data == "pay_yoomoney")
async def callback_pay_yoomoney(callback: CallbackQuery):
    """Оплата через YooMoney"""
    await callback.answer()

    pending = {}
    if hasattr(dp, 'pending_accounts'):
        pending = dp.pending_accounts.get(callback.from_user.id, {})

    price = pending.get('price', 20)
    payment_id = await generate_payment_id()

    # Сохраняем платеж в БД
    async with async_session() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            amount=price,
            payment_id=payment_id,
            method="yoomoney",
            status="pending",
            type="purchase"
        )
        session.add(payment)
        await session.commit()

    # Создаем ссылку на оплату
    payment_url = await create_yoomoney_payment(price, payment_id)

    text = (
        f'{premium_emoji("money")}<b>Оплата через ЮMoney</b>\n\n'
        f'Сумма: <b>{price}₽</b>\n'
        f'Кошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
        f'<a href="{payment_url}">💳 Нажмите для оплаты</a>\n\n'
        '⚠️ <b>После оплаты нажмите кнопку проверки</b>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=check_payment_keyboard(payment_id, "yoomoney", is_deposit=False),
        disable_web_page_preview=True
    )


@router.callback_query(F.data == "pay_crypto")
async def callback_pay_crypto(callback: CallbackQuery):
    """Оплата через Crypto Bot"""
    await callback.answer()

    pending = {}
    if hasattr(dp, 'pending_accounts'):
        pending = dp.pending_accounts.get(callback.from_user.id, {})

    price = pending.get('price', 20)
    payment_id = await generate_payment_id()

    # Сохраняем платеж в БД
    async with async_session() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            amount=price,
            payment_id=payment_id,
            method="crypto",
            status="pending",
            type="purchase"
        )
        session.add(payment)
        await session.commit()

    # Показываем статус создания
    await callback.message.edit_text(
        f'{premium_emoji("loading")}<b>Создаю счет...</b>'
    )

    # Создаем счет в Crypto Bot
    invoice = await create_crypto_bot_invoice(price, payment_id)

    if invoice and invoice.get("ok"):
        result = invoice.get("result", {})
        pay_url = result.get("pay_url")
        invoice_id = result.get("invoice_id")

        # Обновляем payment_id на invoice_id
        async with async_session() as session:
            stmt = select(Payment).where(Payment.payment_id == payment_id)
            exec_result = await session.execute(stmt)
            payment_record = exec_result.scalar_one_or_none()
            if payment_record:
                payment_record.payment_id = str(invoice_id)
                await session.commit()

        text = (
            f'{premium_emoji("crypto")}<b>Оплата через Crypto Bot</b>\n\n'
            f'Сумма: <b>{price}₽</b>\n\n'
            f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
            '⚠️ <b>После оплаты нажмите кнопку проверки</b>'
        )

        await callback.message.edit_text(
            text,
            reply_markup=check_payment_keyboard(
                str(invoice_id),
                "crypto",
                is_deposit=False
            ),
            disable_web_page_preview=True
        )
    else:
        await callback.message.edit_text(
            f'{premium_emoji("cross")}<b>Ошибка создания счета</b>\n\n'
            'Попробуйте другой способ оплаты.',
            reply_markup=payment_methods_keyboard()
        )


@router.callback_query(F.data == "pay_stars")
async def callback_pay_stars(callback: CallbackQuery):
    """Оплата через Telegram Stars"""
    await callback.answer()

    text = (
        f'{premium_emoji("star")}<b>Оплата Telegram Stars</b>\n\n'
        'Для покупки через Telegram Stars\n'
        'напишите: <b>@v3estnikov</b>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=payment_methods_keyboard()
    )


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - ПРОВЕРКА ОПЛАТЫ
# ============================================================
@router.callback_query(F.data.startswith("check_purchase_"))
async def callback_check_purchase(callback: CallbackQuery):
    """Проверка оплаты для покупки аккаунта"""
    await callback.answer()

    # Извлекаем метод и ID платежа
    parts = callback.data.replace("check_purchase_", "").split("_", 1)
    method = parts[0]
    payment_id = "_".join(parts[1:])

    # Обновляем сообщение
    await callback.message.edit_text(
        f'{premium_emoji("loading")}<b>Проверяю оплату...</b>\n\n'
        'Пожалуйста, подождите.',
        reply_markup=check_payment_keyboard(
            payment_id,
            method,
            is_deposit=False
        )
    )

    # Проверяем оплату
    success = False

    if method == "yoomoney":
        success = await check_yoomoney_payment(payment_id)
        logger.info(f"YooMoney purchase check for {payment_id}: {success}")
    elif method == "crypto":
        invoice = await check_crypto_bot_invoice(int(payment_id))
        if invoice and invoice.get("status") == "paid":
            success = True
            logger.info(f"Crypto Bot purchase check for {payment_id}: paid")

    if success:
        # Получаем данные заказа
        pending = {}
        if hasattr(dp, 'pending_accounts'):
            pending = dp.pending_accounts.get(callback.from_user.id, {})

        account_id = pending.get('account_id')
        price = pending.get('price', 20)

        if account_id:
            async with async_session() as session:
                # Проверяем, не продан ли уже аккаунт
                account = await session.get(Account, account_id)
                if account.is_sold:
                    await callback.message.edit_text(
                        f'{premium_emoji("cross")}<b>Аккаунт уже продан.</b>\n\n'
                        'Обратитесь в поддержку.',
                        reply_markup=main_menu_keyboard()
                    )
                    return

                # Обновляем статус платежа
                stmt = select(Payment).where(Payment.payment_id == payment_id)
                exec_result = await session.execute(stmt)
                payment_record = exec_result.scalar_one_or_none()
                if payment_record:
                    payment_record.status = "completed"

                # Обновляем пользователя
                user = await session.get(User, callback.from_user.id)
                if user:
                    user.total_spent = (user.total_spent or 0) + price

                # Отмечаем аккаунт как проданный
                account.is_sold = True

                # Создаем запись о покупке
                purchase = Purchase(
                    user_id=callback.from_user.id,
                    account_id=account_id,
                    amount=price,
                    payment_method=method
                )
                session.add(purchase)
                await session.commit()
                await session.refresh(purchase)

                text = (
                    f'{premium_emoji("check")}<b>Оплата подтверждена!</b>\n\n'
                    f'{premium_emoji("tag")}Номер: <code>{account.phone}</code>\n'
                    f'{premium_emoji("money")}Сумма: <b>{price}₽</b>\n\n'
                    'Нажмите чтобы получить код:'
                )

                await callback.message.edit_text(
                    text,
                    reply_markup=get_code_keyboard(purchase.id)
                )
        else:
            await callback.message.edit_text(
                f'{premium_emoji("cross")}<b>Данные заказа утеряны.</b>\n\n'
                'Пожалуйста, начните заново.',
                reply_markup=main_menu_keyboard()
            )
    else:
        # Оплата не найдена
        await callback.answer(
            "⏳ Оплата не найдена. Попробуйте позже.",
            show_alert=True
        )


@router.callback_query(F.data.startswith("check_deposit_"))
async def callback_check_deposit(callback: CallbackQuery):
    """Проверка пополнения баланса"""
    await callback.answer()

    # Извлекаем метод и ID платежа
    parts = callback.data.replace("check_deposit_", "").split("_", 1)
    method = parts[0]
    payment_id = "_".join(parts[1:])

    # Обновляем сообщение
    await callback.message.edit_text(
        f'{premium_emoji("loading")}<b>Проверяю пополнение...</b>',
        reply_markup=check_payment_keyboard(
            payment_id,
            method,
            is_deposit=True
        )
    )

    # Проверяем оплату
    success = False

    if method == "yoomoney":
        success = await check_yoomoney_payment(payment_id)
        logger.info(f"YooMoney deposit check for {payment_id}: {success}")
    elif method == "crypto":
        invoice = await check_crypto_bot_invoice(int(payment_id))
        if invoice and invoice.get("status") == "paid":
            success = True
            logger.info(f"Crypto Bot deposit check for {payment_id}: paid")

    if success:
        async with async_session() as session:
            # Ищем платеж в БД
            stmt = select(Payment).where(Payment.payment_id == payment_id)
            exec_result = await session.execute(stmt)
            payment_record = exec_result.scalar_one_or_none()

            if payment_record and payment_record.status != "completed":
                # Обновляем статус
                payment_record.status = "completed"

                # Зачисляем средства
                user = await session.get(User, callback.from_user.id)
                deposit_amount = payment_record.amount
                user.balance += deposit_amount

                await session.commit()

                text = (
                    f'{premium_emoji("check")}<b>Баланс пополнен!</b>\n\n'
                    f'{premium_emoji("money")}Зачислено: '
                    f'<b>{deposit_amount:.2f}₽</b>\n'
                    f'{premium_emoji("wallet")}Баланс: '
                    f'<b>{user.balance:.2f}₽</b>'
                )

                # Кнопка возврата в меню
                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(
                        text="В меню",
                        callback_data="main_menu",
                        icon_custom_emoji_id=PREMIUM_EMOJI['home']
                    )
                )

                await callback.message.edit_text(
                    text,
                    reply_markup=builder.as_markup()
                )

            elif payment_record and payment_record.status == "completed":
                await callback.answer(
                    "ℹ️ Этот платеж уже был зачислен",
                    show_alert=True
                )
            else:
                await callback.answer(
                    "❌ Платеж не найден в базе",
                    show_alert=True
                )
    else:
        await callback.answer(
            "⏳ Пополнение не найдено. Попробуйте позже.",
            show_alert=True
        )


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - КОД И ПОКУПКИ
# ============================================================
@router.callback_query(F.data.startswith("get_code_"))
async def callback_get_code(callback: CallbackQuery):
    """Получение кода подтверждения"""
    await callback.answer()

    purchase_id = int(callback.data.replace("get_code_", ""))

    async with async_session() as session:
        # Ищем покупку
        stmt = select(Purchase).where(Purchase.id == purchase_id)
        exec_result = await session.execute(stmt)
        purchase = exec_result.scalar_one_or_none()

        # Проверки
        if not purchase:
            await callback.answer("Покупка не найдена", show_alert=True)
            return

        if purchase.user_id != callback.from_user.id:
            await callback.answer("Это не ваша покупка", show_alert=True)
            return

        if purchase.code_sent:
            await callback.answer("Код уже был получен", show_alert=True)
            return

        # Получаем аккаунт
        account = await session.get(Account, purchase.account_id)

        if not account or not account.session_string:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        # Показываем статус
        await callback.message.edit_text(
            f'{premium_emoji("loading")}<b>Получаю код...</b>\n\n'
            'Пожалуйста, подождите.'
        )

        # Получаем код через сессию
        code = await get_code_from_session(account.session_string)

        if code:
            # Отмечаем код как отправленный
            purchase.code_sent = True
            await session.commit()

            text = (
                f'{premium_emoji("check")}<b>Код получен!</b>\n\n'
                f'{premium_emoji("tag")}Номер: <code>{account.phone}</code>\n'
                f'{premium_emoji("lock")}Код: <code>{code}</code>\n\n'
                '⚠️ <i>Сохраните код в надежном месте</i>'
            )

            # Кнопка возврата в меню
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(
                    text="В меню",
                    callback_data="main_menu",
                    icon_custom_emoji_id=PREMIUM_EMOJI['home']
                )
            )

            await callback.message.edit_text(
                text,
                reply_markup=builder.as_markup()
            )
        else:
            # Код не найден
            await callback.message.edit_text(
                f'{premium_emoji("cross")}<b>Не удалось получить код</b>\n\n'
                'Попробуйте позже или обратитесь в поддержку:\n'
                '<b>@v3estnikov</b>',
                reply_markup=get_code_keyboard(purchase_id)
            )


@router.callback_query(F.data == "my_purchases")
async def callback_my_purchases(callback: CallbackQuery):
    """Список покупок пользователя"""
    await callback.answer()

    async with async_session() as session:
        # Получаем все покупки пользователя
        stmt = (
            select(Purchase)
            .where(Purchase.user_id == callback.from_user.id)
            .order_by(Purchase.created_at.desc())
        )
        exec_result = await session.execute(stmt)
        purchases = exec_result.scalars().all()

        if purchases:
            text = (
                f'{premium_emoji("box")}<b>Ваши покупки</b>\n\n'
            )

            builder = InlineKeyboardBuilder()

            for purchase in purchases:
                account = await session.get(Account, purchase.account_id)

                # Статус
                if purchase.code_sent:
                    status = "✅"
                else:
                    status = "⏳"

                # Телефон
                if account:
                    phone = account.phone
                else:
                    phone = "Н/Д"

                # Дата
                date = purchase.created_at.strftime('%d.%m.%y')

                # Добавляем строку
                text += (
                    f'{status} <code>{phone}</code> • '
                    f'{purchase.amount}₽ • {date}\n'
                )

                # Кнопка получения кода
                if not purchase.code_sent:
                    builder.row(
                        InlineKeyboardButton(
                            text=f"Получить код • {phone}",
                            callback_data=f"get_code_{purchase.id}",
                            icon_custom_emoji_id=PREMIUM_EMOJI['code']
                        )
                    )

            # Кнопка возврата
            builder.row(
                InlineKeyboardButton(
                    text="В меню",
                    callback_data="main_menu",
                    icon_custom_emoji_id=PREMIUM_EMOJI['home']
                )
            )

            await callback.message.edit_text(
                text,
                reply_markup=builder.as_markup()
            )
        else:
            text = (
                f'{premium_emoji("box")}<b>Мои покупки</b>\n\n'
                'У вас пока нет покупок.\n'
                'Купите свой первый аккаунт! 🚀'
            )

            await callback.message.edit_text(
                text,
                reply_markup=main_menu_keyboard()
            )


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - ПРОФИЛЬ И ПОПОЛНЕНИЕ
# ============================================================
@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    """Профиль пользователя"""
    await callback.answer()

    user = await get_user(callback.from_user.id)

    # Считаем количество покупок
    async with async_session() as session:
        stmt = (
            select(func.count(Purchase.id))
            .where(Purchase.user_id == callback.from_user.id)
        )
        exec_result = await session.execute(stmt)
        purchases_count = exec_result.scalar() or 0

    # Формируем текст профиля
    text = (
        f'{premium_emoji("profile")}<b>Профиль</b>\n\n'
        f'{premium_emoji("tag")}ID: <code>{user.telegram_id}</code>\n'
        f'{premium_emoji("profile")}@{user.username or "нет"}\n\n'
        '━━━ 💰 БАЛАНС ━━━\n'
        f'{premium_emoji("wallet")}<b>{user.balance:.0f}₽</b>\n'
        '━━━━━━━━━━━━━━\n\n'
        '━━ 📊 СТАТИСТИКА ━━\n'
        f'{premium_emoji("box")}Покупок: <b>{purchases_count}</b>\n'
        f'{premium_emoji("money")}Потрачено: '
        f'<b>{(user.total_spent or 0):.0f}₽</b>\n'
        f'{premium_emoji("clock")}С нами с: '
        f'{user.created_at.strftime("%d.%m.%Y")}\n'
        '━━━━━━━━━━━━━━'
    )

    await callback.message.edit_text(
        text,
        reply_markup=profile_keyboard()
    )


@router.callback_query(F.data == "deposit_balance")
async def callback_deposit_balance(callback: CallbackQuery):
    """Меню пополнения баланса"""
    await callback.answer()

    text = (
        f'{premium_emoji("wallet")}<b>Пополнение баланса</b>\n\n'
        f'{premium_emoji("money")}<b>ЮMoney</b> — перевод на кошелек\n'
        f'{premium_emoji("crypto")}<b>Crypto Bot</b> — криптовалютой\n\n'
        '<i>Минимальная сумма: 10₽</i>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=deposit_keyboard()
    )


@router.callback_query(F.data == "deposit_yoomoney")
async def callback_deposit_yoomoney(callback: CallbackQuery):
    """Запрос суммы для пополнения через YooMoney"""
    await callback.answer()

    # Отправляем новое сообщение с запросом
    await callback.message.answer(
        f'{premium_emoji("money")}<b>Введите сумму пополнения '
        f'(от 10₽)</b>\n\n'
        '<i>Отправьте число в чат</i>'
    )

    # Устанавливаем состояние ожидания
    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'yoomoney'


@router.callback_query(F.data == "deposit_crypto")
async def callback_deposit_crypto(callback: CallbackQuery):
    """Запрос суммы для пополнения через Crypto Bot"""
    await callback.answer()

    await callback.message.answer(
        f'{premium_emoji("crypto")}<b>Введите сумму пополнения '
        f'(от 10₽)</b>\n\n'
        '<i>Отправьте число в чат</i>'
    )

    if not hasattr(dp, 'awaiting_deposit'):
        dp.awaiting_deposit = {}
    dp.awaiting_deposit[callback.from_user.id] = 'crypto'


# ============================================================
# ОБРАБОТЧИКИ CALLBACK - АДМИН-ПАНЕЛЬ
# ============================================================
@router.callback_query(F.data.startswith("admin_"))
async def callback_admin(callback: CallbackQuery):
    """Обработчики админ-панели"""
    await callback.answer()

    # Проверка прав
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return

    data = callback.data

    # --- Статистика ---
    if data == "admin_stats":
        async with async_session() as session:
            # Пользователи
            stmt = select(func.count(User.id))
            exec_result = await session.execute(stmt)
            total_users = exec_result.scalar() or 0

            # Аккаунты
            stmt = select(func.count(Account.id))
            exec_result = await session.execute(stmt)
            total_accounts = exec_result.scalar() or 0

            # Продано
            stmt = select(func.count(Account.id)).where(
                Account.is_sold == True
            )
            exec_result = await session.execute(stmt)
            sold_accounts = exec_result.scalar() or 0

            # Верифицировано
            stmt = select(func.count(Account.id)).where(
                Account.is_verified == True
            )
            exec_result = await session.execute(stmt)
            verified_accounts = exec_result.scalar() or 0

            # Покупки
            stmt = select(func.count(Purchase.id))
            exec_result = await session.execute(stmt)
            total_purchases = exec_result.scalar() or 0

            # Выручка
            stmt = select(func.sum(Purchase.amount))
            exec_result = await session.execute(stmt)
            total_revenue = exec_result.scalar() or 0

            stats_text = (
                f'{premium_emoji("stats")}<b>Статистика</b>\n\n'
                f'{premium_emoji("profile")}Пользователей: '
                f'<b>{total_users}</b>\n'
                f'{premium_emoji("box")}Аккаунтов: '
                f'<b>{total_accounts}</b>\n'
                f'{premium_emoji("check")}Верифицировано: '
                f'<b>{verified_accounts}</b>\n'
                f'{premium_emoji("buy")}Продано: '
                f'<b>{sold_accounts}</b>\n'
                f'{premium_emoji("box")}Покупок: '
                f'<b>{total_purchases}</b>\n'
                f'{premium_emoji("money")}Выручка: '
                f'<b>{total_revenue:.0f}₽</b>'
            )

            await callback.message.edit_text(
                stats_text,
                reply_markup=admin_keyboard()
            )

    # --- Рассылка ---
    elif data == "admin_broadcast":
        if not hasattr(dp, 'awaiting_broadcast'):
            dp.awaiting_broadcast = set()
        dp.awaiting_broadcast.add(callback.from_user.id)

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="Отмена",
                callback_data="main_menu",
                icon_custom_emoji_id=PREMIUM_EMOJI['cross']
            )
        )

        await callback.message.edit_text(
            f'{premium_emoji("broadcast")}<b>Рассылка</b>\n\n'
            'Отправьте сообщение для рассылки всем пользователям.\n'
            'Поддерживаются: текст, фото, видео, документы.',
            reply_markup=builder.as_markup()
        )

    # --- Добавление аккаунтов ---
    elif data == "admin_add_accounts":
        if not hasattr(dp, 'awaiting_accounts'):
            dp.awaiting_accounts = {}
        dp.awaiting_accounts[callback.from_user.id] = {'step': 'phone'}

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="Отмена",
                callback_data="main_menu",
                icon_custom_emoji_id=PREMIUM_EMOJI['cross']
            )
        )

        await callback.message.edit_text(
            f'{premium_emoji("add")}<b>Добавление аккаунта</b>\n\n'
            'Отправьте номер телефона в формате:\n'
            '<code>+79001234567</code>',
            reply_markup=builder.as_markup()
        )

    # --- Управление балансом ---
    elif data == "admin_balance":
        if not hasattr(dp, 'awaiting_balance'):
            dp.awaiting_balance = {}
        dp.awaiting_balance[callback.from_user.id] = {'step': 'user_id'}

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="Отмена",
                callback_data="main_menu",
                icon_custom_emoji_id=PREMIUM_EMOJI['cross']
            )
        )

        await callback.message.edit_text(
            f'{premium_emoji("edit")}<b>Изменение баланса</b>\n\n'
            'Отправьте ID пользователя:\n'
            '<i>Можно получить в профиле пользователя</i>',
            reply_markup=builder.as_markup()
        )


# ============================================================
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ
# ============================================================
@router.message(F.text)
async def handle_text(message: Message):
    """
    Универсальный обработчик текстовых сообщений.
    Обрабатывает: пополнение, добавление аккаунтов,
    изменение баланса, рассылку.
    """
    user_id = message.from_user.id
    text = message.text.strip()

    # ========================================
    # ПОПОЛНЕНИЕ БАЛАНСА
    # ========================================
    if hasattr(dp, 'awaiting_deposit') and user_id in dp.awaiting_deposit:
        method = dp.awaiting_deposit.pop(user_id)

        try:
            # Парсим сумму
            amount = float(text.replace(',', '.'))

            # Проверяем минимальную сумму
            if amount < 10:
                await message.answer(
                    f'{premium_emoji("cross")}<b>Минимальная сумма: '
                    f'10₽</b>\n\nВведите сумму еще раз:',
                    reply_markup=deposit_keyboard()
                )
                return

            # Генерируем ID платежа
            payment_id = await generate_payment_id()

            # Сохраняем платеж в БД
            async with async_session() as session:
                payment = Payment(
                    user_id=user_id,
                    amount=amount,
                    payment_id=payment_id,
                    method=method,
                    status="pending",
                    type="deposit"
                )
                session.add(payment)
                await session.commit()

            # Обработка в зависимости от метода
            if method == "yoomoney":
                payment_url = await create_yoomoney_payment(
                    amount,
                    payment_id
                )

                deposit_text = (
                    f'{premium_emoji("money")}<b>Пополнение через '
                    f'ЮMoney</b>\n\n'
                    f'Сумма: <b>{amount}₽</b>\n'
                    f'Кошелек: <code>{YOOMONEY_WALLET}</code>\n\n'
                    f'<a href="{payment_url}">💳 Нажмите для оплаты</a>\n\n'
                    '⚠️ <b>После оплаты нажмите кнопку проверки</b>'
                )

                await message.answer(
                    deposit_text,
                    reply_markup=check_payment_keyboard(
                        payment_id,
                        "yoomoney",
                        is_deposit=True
                    ),
                    disable_web_page_preview=True
                )

            elif method == "crypto":
                # Уведомляем о создании счета
                status_msg = await message.answer(
                    f'{premium_emoji("loading")}Создаю счет...'
                )

                # Создаем счет в Crypto Bot
                invoice = await create_crypto_bot_invoice(
                    amount,
                    payment_id
                )

                if invoice and invoice.get("ok"):
                    result = invoice.get("result", {})
                    pay_url = result.get("pay_url")
                    invoice_id = result.get("invoice_id")

                    # Обновляем payment_id в БД
                    async with async_session() as session:
                        stmt = select(Payment).where(
                            Payment.payment_id == payment_id
                        )
                        exec_result = await session.execute(stmt)
                        payment_record = exec_result.scalar_one_or_none()
                        if payment_record:
                            payment_record.payment_id = str(invoice_id)
                            await session.commit()

                    # Удаляем статусное сообщение
                    await status_msg.delete()

                    deposit_text = (
                        f'{premium_emoji("crypto")}<b>Пополнение через '
                        f'Crypto Bot</b>\n\n'
                        f'Сумма: <b>{amount}₽</b>\n\n'
                        f'<a href="{pay_url}">💳 Нажмите для оплаты</a>\n\n'
                        '⚠️ <b>После оплаты нажмите кнопку проверки</b>'
                    )

                    await message.answer(
                        deposit_text,
                        reply_markup=check_payment_keyboard(
                            str(invoice_id),
                            "crypto",
                            is_deposit=True
                        ),
                        disable_web_page_preview=True
                    )
                else:
                    await status_msg.edit_text(
                        f'{premium_emoji("cross")}<b>Ошибка создания '
                        f'счета</b>\n\nПопробуйте позже.',
                        reply_markup=deposit_keyboard()
                    )

        except ValueError:
            await message.answer(
                f'{premium_emoji("cross")}<b>Введите корректное число</b>'
            )

        return

    # ========================================
    # ДОБАВЛЕНИЕ АККАУНТА (АДМИН)
    # ========================================
    if hasattr(dp, 'awaiting_accounts') and user_id in dp.awaiting_accounts:
        acc_data = dp.awaiting_accounts[user_id]
        step = acc_data.get('step')

        # --- Шаг 1: Номер телефона ---
        if step == 'phone':
            phone = text
            acc_data['phone'] = phone

            # Отправляем код
            status_msg = await message.answer(
                f'{premium_emoji("loading")}Отправляю код на '
                f'{phone}...'
            )

            result = await send_code_to_phone(phone)

            if result['success']:
                acc_data['phone_code_hash'] = result['phone_code_hash']
                acc_data['step'] = 'code'

                await status_msg.edit_text(
                    f'{premium_emoji("check")}<b>Код отправлен на '
                    f'{phone}</b>\n\n'
                    'Введите код из Telegram:'
                )
            else:
                del dp.awaiting_accounts[user_id]
                await status_msg.edit_text(
                    f'{premium_emoji("cross")}<b>Ошибка отправки '
                    f'кода</b>\n\n{result.get("error", "Неизвестная ошибка")}',
                    reply_markup=admin_keyboard()
                )

        # --- Шаг 2: Код подтверждения ---
        elif step == 'code':
            code = text
            phone = acc_data['phone']
            phone_code_hash = acc_data['phone_code_hash']

            status_msg = await message.answer(
                f'{premium_emoji("loading")}Проверяю код...'
            )

            result = await verify_code_and_get_session(
                phone,
                code,
                phone_code_hash
            )

            if result['success']:
                # Сохраняем аккаунт в БД
                async with async_session() as session:
                    # Проверяем, существует ли уже
                    stmt = select(Account).where(Account.phone == phone)
                    exec_result = await session.execute(stmt)
                    existing_acc = exec_result.scalar_one_or_none()

                    if existing_acc:
                        # Обновляем существующий
                        existing_acc.session_string = result['session_string']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                        logger.info(f"Updated existing account: {phone}")
                    else:
                        # Создаем новый
                        account = Account(
                            phone=phone,
                            session_string=result['session_string'],
                            is_verified=True,
                            is_sold=False
                        )
                        session.add(account)
                        logger.info(f"Created new account: {phone}")

                    await session.commit()

                # Удаляем состояние ожидания
                del dp.awaiting_accounts[user_id]

                await status_msg.edit_text(
                    f'{premium_emoji("check")}<b>Аккаунт успешно '
                    f'добавлен!</b>\n\n'
                    f'{premium_emoji("tag")}Номер: <code>{phone}</code>\n'
                    f'{premium_emoji("check")}Статус: верифицирован\n'
                    f'{premium_emoji("money")}Цена: 20₽\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )

            elif result.get('need_password'):
                # Требуется 2FA
                acc_data['step'] = 'password'
                await status_msg.edit_text(
                    f'{premium_emoji("lock")}<b>Требуется 2FA пароль</b>\n\n'
                    'Введите пароль облачной защиты:'
                )
            else:
                # Ошибка
                del dp.awaiting_accounts[user_id]
                await status_msg.edit_text(
                    f'{premium_emoji("cross")}<b>Ошибка верификации</b>\n\n'
                    f'{result.get("error", "Неизвестная ошибка")}',
                    reply_markup=admin_keyboard()
                )

        # --- Шаг 3: 2FA пароль ---
        elif step == 'password':
            password = text
            phone = acc_data['phone']

            status_msg = await message.answer(
                f'{premium_emoji("loading")}Проверяю пароль...'
            )

            result = await verify_2fa_password(phone, password)

            if result['success']:
                # Сохраняем аккаунт
                async with async_session() as session:
                    stmt = select(Account).where(Account.phone == phone)
                    exec_result = await session.execute(stmt)
                    existing_acc = exec_result.scalar_one_or_none()

                    if existing_acc:
                        existing_acc.session_string = result['session_string']
                        existing_acc.is_verified = True
                        existing_acc.is_sold = False
                    else:
                        account = Account(
                            phone=phone,
                            session_string=result['session_string'],
                            is_verified=True,
                            is_sold=False
                        )
                        session.add(account)

                    await session.commit()

                del dp.awaiting_accounts[user_id]

                await status_msg.edit_text(
                    f'{premium_emoji("check")}<b>Аккаунт успешно '
                    f'добавлен!</b>\n\n'
                    f'{premium_emoji("tag")}Номер: <code>{phone}</code>\n'
                    f'{premium_emoji("check")}Статус: верифицирован\n\n'
                    '<i>Аккаунт доступен для покупки</i>',
                    reply_markup=admin_keyboard()
                )
            else:
                await status_msg.edit_text(
                    f'{premium_emoji("cross")}<b>'
                    f'{result.get("error", "Ошибка")}</b>\n\n'
                    'Попробуйте еще раз:'
                )

        return

    # ========================================
    # ИЗМЕНЕНИЕ БАЛАНСА (АДМИН)
    # ========================================
    if hasattr(dp, 'awaiting_balance') and user_id in dp.awaiting_balance:
        bal_data = dp.awaiting_balance[user_id]
        step = bal_data.get('step')

        # --- Шаг 1: ID пользователя ---
        if step == 'user_id':
            try:
                target_id = int(text)
                target_user = await get_user(target_id)

                if not target_user:
                    await message.answer(
                        f'{premium_emoji("cross")}<b>Пользователь '
                        f'не найден</b>',
                        reply_markup=admin_keyboard()
                    )
                    del dp.awaiting_balance[user_id]
                    return

                bal_data['target_id'] = target_id
                bal_data['step'] = 'amount'

                await message.answer(
                    f'{premium_emoji("edit")}<b>Изменение баланса</b>\n\n'
                    f'Пользователь: <code>{target_id}</code>\n'
                    f'Текущий баланс: <b>{target_user.balance:.0f}₽</b>\n\n'
                    'Отправьте сумму:\n'
                    '<code>+100</code> — пополнить\n'
                    '<code>-50</code> — списать\n'
                    '<code>500</code> — установить'
                )

            except ValueError:
                await message.answer(
                    f'{premium_emoji("cross")}<b>Введите корректный '
                    f'ID пользователя</b>'
                )

        # --- Шаг 2: Сумма ---
        elif step == 'amount':
            try:
                value = text
                target_id = bal_data['target_id']

                async with async_session() as session:
                    stmt = select(User).where(
                        User.telegram_id == target_id
                    )
                    exec_result = await session.execute(stmt)
                    target_user = exec_result.scalar_one_or_none()

                    if not target_user:
                        del dp.awaiting_balance[user_id]
                        await message.answer(
                            f'{premium_emoji("cross")}<b>Пользователь '
                            f'не найден</b>',
                            reply_markup=admin_keyboard()
                        )
                        return

                    # Запоминаем старый баланс
                    old_balance = target_user.balance

                    # Изменяем баланс
                    if value.startswith('+'):
                        target_user.balance += float(value[1:])
                    elif value.startswith('-'):
                        target_user.balance -= float(value[1:])
                        if target_user.balance < 0:
                            target_user.balance = 0
                    else:
                        target_user.balance = float(value)

                    await session.commit()

                    # Удаляем состояние
                    del dp.awaiting_balance[user_id]

                    await message.answer(
                        f'{premium_emoji("check")}<b>Баланс изменен!</b>\n\n'
                        f'Пользователь: <code>{target_id}</code>\n'
                        f'Было: <b>{old_balance:.0f}₽</b>\n'
                        f'Стало: <b>{target_user.balance:.0f}₽</b>',
                        reply_markup=admin_keyboard()
                    )

            except ValueError:
                await message.answer(
                    f'{premium_emoji("cross")}<b>Введите корректную '
                    f'сумму</b>'
                )

        return

    # ========================================
    # РАССЫЛКА (АДМИН)
    # ========================================
    if hasattr(dp, 'awaiting_broadcast') and user_id in dp.awaiting_broadcast:
        dp.awaiting_broadcast.remove(user_id)

        # Получаем всех пользователей
        async with async_session() as session:
            stmt = select(User)
            exec_result = await session.execute(stmt)
            users = exec_result.scalars().all()

            sent = 0
            total = len(users)

            # Отправляем сообщение каждому
            for user in users:
                try:
                    await message.copy_to(chat_id=user.telegram_id)
                    sent += 1
                    await asyncio.sleep(0.05)  # Задержка для избежания флуда
                except Exception as e:
                    logger.error(
                        f"Failed to send broadcast to {user.telegram_id}: {e}"
                    )
                    continue

        await message.answer(
            f'{premium_emoji("check")}<b>Рассылка завершена</b>\n\n'
            f'Отправлено: <b>{sent}</b> из <b>{total}</b> пользователей',
            reply_markup=admin_keyboard()
        )

        return

    # ========================================
    # ОБЫЧНОЕ СООБЩЕНИЕ
    # ========================================
    await message.answer(
        f'{premium_emoji("info")}<b>Используйте кнопки меню '
        f'для навигации</b>',
        reply_markup=main_menu_keyboard()
    )


# ============================================================
# ЗАПУСК БОТА
# ============================================================
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
    if not hasattr(dp, 'awaiting_broadcast'):
        dp.awaiting_broadcast = set()
    if not hasattr(dp, 'awaiting_balance'):
        dp.awaiting_balance = {}

    # Подключаем роутер
    dp.include_router(router)

    logger.info("=" * 50)
    logger.info("Vest Account Bot started!")
    logger.info(f"Admins: {ADMIN_IDS}")
    logger.info(f"YooMoney wallet: {YOOMONEY_WALLET}")
    logger.info(f"CLIENT_ID set: {bool(CLIENT_ID)}")
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
