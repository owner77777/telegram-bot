import os
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ChatPermissions, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove
)
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен бота (получаем из переменных окружения)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен! Добавь его в Environment Variables на Render")

# ID владельца бота
try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "6493670021"))
except:
    BOT_OWNER_ID = 6493670021

# ID чата для обращений
try:
    SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-1003559804187"))
except:
    SUPPORT_CHAT_ID = -1003559804187

# ID разрешенного чата (ЗАМЕНИ НА ID СВОЕГО ЧАТА!)
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "-1002287799491"))  # Замени на ID своего чата!

# Порт для Render
PORT = int(os.getenv("PORT", 10000))

# Инициализация бота и диспетчера
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)


# Состояния FSM
class SupportStates(StatesGroup):
    waiting_for_appeal = State()
    waiting_for_complaint = State()
    waiting_for_suggestion = State()
    waiting_for_response = State()
    waiting_for_photo = State()
    waiting_for_text_with_photo = State()


# Инициализация базы данных
DB_NAME = "bot_database.db"


def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_warns (
            chat_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS owner_message (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            owner_id INTEGER
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            ticket_type TEXT,
            message TEXT,
            photo_file_id TEXT,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            admin_response TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME
        )
    ''')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_warns ON user_warns(chat_id, user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_support_tickets ON support_tickets(user_id, status)')

    conn.commit()
    conn.close()


init_db()


# Функции для работы с БД
def add_warn_to_db(chat_id: int, user_id: int, reason: str):
    """Добавляет предупреждение в базу данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO user_warns (chat_id, user_id, reason) VALUES (?, ?, ?)",
        (chat_id, user_id, reason)
    )
    conn.commit()
    conn.close()


def get_user_warns_from_db(chat_id: int, user_id: int) -> List[str]:
    """Получает предупреждения пользователя из базы данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT reason FROM user_warns WHERE chat_id = ? AND user_id = ? ORDER BY timestamp",
        (chat_id, user_id)
    )
    results = cursor.fetchall()
    conn.close()
    return [row[0] for row in results]


def remove_last_warn_from_db(chat_id: int, user_id: int):
    """Удаляет последнее предупреждение пользователя из базы данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """DELETE FROM user_warns WHERE rowid = (
            SELECT rowid FROM user_warns 
            WHERE chat_id = ? AND user_id = ? 
            ORDER BY timestamp DESC LIMIT 1
        )""",
        (chat_id, user_id)
    )
    conn.commit()
    conn.close()


def clear_warns_from_db(chat_id: int, user_id: int):
    """Удаляет все предупреждения пользователя из базы данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_warns WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    )
    conn.commit()
    conn.close()


def set_owner_message(owner_id: int, message: str):
    """Устанавливает сообщение владельца"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM owner_message")
    cursor.execute(
        "INSERT INTO owner_message (message, owner_id) VALUES (?, ?)",
        (message, owner_id)
    )
    conn.commit()
    conn.close()


def get_owner_message() -> Optional[tuple]:
    """Получает сообщение владельца"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT message, owner_id FROM owner_message LIMIT 1")
    result = cursor.fetchone()
    conn.close()
    return result


def remove_owner_message():
    """Удаляет сообщение владельца"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM owner_message")
    conn.commit()
    conn.close()


def add_support_ticket(user_id: int, username: str, first_name: str, last_name: str,
                       ticket_type: str, message: str, photo_file_id: str = None) -> int:
    """Добавляет обращение в базу данных и возвращает ID обращения"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO support_tickets (user_id, username, first_name, last_name, ticket_type, message, photo_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name, ticket_type, message, photo_file_id))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id


def update_ticket_status(ticket_id: int, admin_id: int, status: str, response: str = None):
    """Обновляет статус обращения"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if status == 'resolved':
        cursor.execute('''
            UPDATE support_tickets 
            SET status = ?, admin_id = ?, admin_response = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, admin_id, response, ticket_id))
    else:
        cursor.execute('''
            UPDATE support_tickets 
            SET status = ?, admin_id = ?, admin_response = ?
            WHERE id = ?
        ''', (status, admin_id, response, ticket_id))
    conn.commit()
    conn.close()


def get_ticket_by_id(ticket_id: int) -> Optional[tuple]:
    """Получает обращение по ID"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
    result = cursor.fetchone()
    conn.close()
    return result


# Проверка разрешенного чата
async def is_allowed_chat(chat_id: int) -> bool:
    """Проверяет, разрешен ли этот чат для бота"""
    return chat_id == ALLOWED_CHAT_ID


async def silent_delete_service_messages(message: types.Message):
    """Тихо удаляет служебные сообщения о входе/выходе"""
    if not await is_allowed_chat(message.chat.id):
        return
    
    try:
        is_service_message = (
            message.new_chat_members or
            message.left_chat_member or
            message.group_chat_created or
            message.migrate_from_chat_id or
            message.migrate_to_chat_id or
            message.pinned_message
        )

        if is_service_message:
            try:
                await message.delete()
                logger.info(f"Удалено служебное сообщение в чате {message.chat.id}")
            except TelegramBadRequest as e:
                if "Message can't be deleted" in str(e):
                    logger.warning(f"Не удалось удалить сообщение: {e}")
            except Exception as e:
                logger.error(f"Ошибка при удалении: {e}")

    except Exception as e:
        logger.error(f"Ошибка в обработке сообщения: {e}")


async def get_target_user(message: types.Message, command: CommandObject) -> tuple[Optional[types.User], str]:
    """Получает целевого пользователя и причину из команды"""
    chat = message.chat
    args = command.args or ""

    # Если команда вызвана как ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        reason = args.strip()
        return target_user, reason or "Без указания причины"

    # Если указаны аргументы
    if args:
        parts = args.split(maxsplit=1)
        identifier = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else "Без указания причины"

        # Если это username
        if identifier.startswith('@'):
            username = identifier[1:]
            try:
                chat_member = await chat.get_member(username)
                return chat_member.user, reason
            except TelegramBadRequest:
                return None, reason
            except Exception as e:
                logger.error(f"Ошибка получения пользователя по username: {e}")
                return None, reason

        # Если это числовой ID
        elif identifier.isdigit():
            user_id = int(identifier)
            try:
                chat_member = await chat.get_member(user_id)
                return chat_member.user, reason
            except TelegramBadRequest:
                return None, reason
            except Exception as e:
                logger.error(f"Ошибка получения пользователя по ID: {e}")
                return None, reason

    return None, "Без указания причины"


async def is_user_admin(chat: types.Chat, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    try:
        member = await chat.get_member(user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except:
        return False


async def can_bot_restrict(chat: types.Chat) -> bool:
    """Проверяет, может ли бот ограничивать пользователей"""
    try:
        bot_member = await chat.get_member((await bot.me()).id)
        return bot_member.can_restrict_members
    except:
        return False


async def format_user_display(user: types.User) -> str:
    """Форматирует отображение пользователя"""
    if user.username:
        return f"@{user.username}"
    else:
        return f"<code>{user.id}</code>"


async def send_action_notification(chat_id: int, action: str, target_user: types.User,
                                   duration: str = "", reason: str = "", admin_user: types.User = None):
    """Отправляет уведомление о действии в чат"""
    try:
        admin_display = await format_user_display(admin_user) if admin_user else "Система"
        target_display = await format_user_display(target_user)

        if action == "ban":
            notification = f"💬 Пользователь {admin_display} выдал блокировку пользователю - {target_display}"
        elif action == "unban":
            notification = f"💬 Пользователь {admin_display} снял блокировку пользователю - {target_display}"
        elif action == "mute":
            notification = f"💬 Пользователь {admin_display} выдал мут пользователю - {target_display}"
        elif action == "unmute":
            notification = f"💬 Пользователь {admin_display} снял мут пользователю - {target_display}"
        elif action == "warn":
            notification = f"💬 Пользователь {admin_display} выдал предупреждение пользователю - {target_display}"
        elif action == "unwarn":
            notification = f"💬 Пользователь {admin_display} снял предупреждение пользователю - {target_display}"
        else:
            notification = f"💬 Пользователь {admin_display} выполнил действие над пользователем - {target_display}"

        if duration:
            notification += f" на {duration}"

        if reason and reason != "Без указания причины":
            notification += f" по причине: {reason}"
        else:
            notification += " без указания причины"

        owner_msg_data = get_owner_message()
        if owner_msg_data:
            owner_message_text, _ = owner_msg_data
            if owner_message_text:
                notification += f"\n\n{owner_message_text}"

        await bot.send_message(
            chat_id=chat_id,
            text=notification,
            parse_mode="HTML",
            disable_notification=True
        )

    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления: {e}")


# Функция для создания меню (ТОЛЬКО ДЛЯ ЛИЧКИ)
def get_main_menu() -> ReplyKeyboardMarkup:
    """Создает главное меню (только для личных сообщений)"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мой ID")],
            [KeyboardButton(text="Поддержка")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard


def get_support_menu() -> ReplyKeyboardMarkup:
    """Создает меню поддержки"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Обжаловать наказание")],
            [KeyboardButton(text="Жалоба")],
            [KeyboardButton(text="Предложение по улучшению")],
            [KeyboardButton(text="Назад")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard


# Обработчики команд
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Команда /start - работает везде, но меню только в личке"""
    try:
        text = "Добро пожаловать! Бот для модерации чата\n"
        text += "\nВ личных сообщениях доступно меню"
        text += "\nВ группе доступны команды модерации"

        owner_msg_data = get_owner_message()
        if owner_msg_data:
            owner_message_text, _ = owner_msg_data
            if owner_message_text:
                text += f"\n\n{owner_message_text}"

        # Меню показываем ТОЛЬКО в личке
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(text, reply_markup=get_main_menu())
        else:
            await message.answer(text, reply_markup=None)

    except Exception as e:
        logger.error(f"Ошибка в команде start: {e}")


@dp.message(F.text == "Мой ID")
async def my_id_handler(message: types.Message):
    """Обработчик кнопки Мой ID - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return  # Игнорируем в группах
    
    try:
        user = message.from_user
        text = f"ID пользователя: <code>{user.id}</code>\n"
        text += f"Username: @{user.username if user.username else 'отсутствует'}\n"
        text += f"Имя: {user.first_name or ''} {user.last_name or ''}".strip()

        owner_msg_data = get_owner_message()
        if owner_msg_data:
            owner_message_text, _ = owner_msg_data
            if owner_message_text:
                text += f"\n\n{owner_message_text}"

        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике моего ID: {e}")


@dp.message(F.text == "Поддержка")
async def support_handler(message: types.Message):
    """Обработчик кнопки Поддержка - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return  # Игнорируем в группах
    
    try:
        text = "Поддержка\n\n"
        text += "Выберите тип обращения:\n"
        text += "\n• Обжаловать наказание"
        text += "\n• Жалоба"
        text += "\n• Предложение по улучшению"
        text += "\n\nВнимание: Ваше обращение будет отправлено модераторам"

        await message.answer(text, reply_markup=get_support_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике поддержки: {e}")


@dp.message(F.text == "Обжаловать наказание")
async def appeal_handler(message: types.Message, state: FSMContext):
    """Обработчик обжалования наказания - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        await state.update_data(ticket_type="Обжалование")
        text = "Обжалование наказания\n\n"
        text += "Опишите подробно:\n"
        text += "1. Какое наказание вы получили\n"
        text += "2. Почему считаете его несправедливым\n"
        text += "3. Любые доказательства\n\n"
        text += "Вы можете приложить одно фото (не более).\n"
        text += "Отправьте текст или фото с подписью."

        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        await state.set_state(SupportStates.waiting_for_appeal)
    except Exception as e:
        logger.error(f"Ошибка в обработчике обжалования: {e}")


@dp.message(F.text == "Жалоба")
async def complaint_handler(message: types.Message, state: FSMContext):
    """Обработчик жалобы - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        await state.update_data(ticket_type="Жалоба")
        text = "Жалоба\n\n"
        text += "Опишите подробно:\n"
        text += "1. На кого жалуетесь\n"
        text += "2. Что произошло\n"
        text += "3. Когда это случилось\n"
        text += "4. Доказательства\n\n"
        text += "Вы можете приложить одно фото (не более).\n"
        text += "Отправьте текст или фото с подписью."

        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        await state.set_state(SupportStates.waiting_for_complaint)
    except Exception as e:
        logger.error(f"Ошибка в обработчике жалобы: {e}")


@dp.message(F.text == "Предложение по улучшению")
async def suggestion_handler(message: types.Message, state: FSMContext):
    """Обработчик предложения по улучшению - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        await state.update_data(ticket_type="Предложение")
        text = "Предложение по улучшению\n\n"
        text += "Опишите подробно:\n"
        text += "1. Что вы предлагаете улучшить\n"
        text += "2. Как это поможет\n"
        text += "3. Конкретные детали\n\n"
        text += "Вы можете приложить одно фото (не более).\n"
        text += "Отправьте текст или фото с подписью."

        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        await state.set_state(SupportStates.waiting_for_suggestion)
    except Exception as e:
        logger.error(f"Ошибка в обработчике предложения: {e}")


@dp.message(F.text == "Назад")
async def back_handler(message: types.Message):
    """Обработчик кнопки Назад - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        await message.answer("Возвращаемся в главное меню", reply_markup=get_main_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике назад: {e}")


# Обработчики для текста и фото в обращениях
@dp.message(SupportStates.waiting_for_appeal, F.photo)
@dp.message(SupportStates.waiting_for_complaint, F.photo)
@dp.message(SupportStates.waiting_for_suggestion, F.photo)
async def handle_support_photo(message: types.Message, state: FSMContext):
    """Обработка фото в обращениях - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        photo_file_id = message.photo[-1].file_id

        if message.caption:
            await state.update_data(photo_file_id=photo_file_id)
            await process_support_request(message, state, caption=message.caption)
        else:
            await state.update_data(photo_file_id=photo_file_id)
            await message.answer("Фото получено. Теперь отправьте текст обращения.")
            await state.set_state(SupportStates.waiting_for_text_with_photo)

    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await message.answer("Ошибка при обработке фото. Попробуйте снова.",
                             reply_markup=get_main_menu())
        await state.clear()


@dp.message(SupportStates.waiting_for_text_with_photo)
async def handle_text_with_photo(message: types.Message, state: FSMContext):
    """Обработка текста для фото - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        data = await state.get_data()
        ticket_type = data.get('ticket_type', 'Обращение')

        await process_support_request(message, state, ticket_type, caption=message.text)

    except Exception as e:
        logger.error(f"Ошибка при обработке текста с фото: {e}")
        await message.answer("Ошибка. Попробуйте снова.", reply_markup=get_main_menu())
        await state.clear()


@dp.message(SupportStates.waiting_for_appeal, F.text)
@dp.message(SupportStates.waiting_for_complaint, F.text)
@dp.message(SupportStates.waiting_for_suggestion, F.text)
async def handle_support_text(message: types.Message, state: FSMContext):
    """Обработка текста в обращениях - ТОЛЬКО В ЛИЧКЕ"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        data = await state.get_data()
        ticket_type = data.get('ticket_type', 'Обращение')
        await process_support_request(message, state, ticket_type, caption=message.text)
    except Exception as e:
        logger.error(f"Ошибка при обработке текста обращения: {e}")
        await message.answer("Ошибка. Попробуйте снова.", reply_markup=get_main_menu())
        await state.clear()


async def process_support_request(message: types.Message, state: FSMContext,
                                  ticket_type: str = None, caption: str = None):
    """Обработка запроса в поддержку"""
    try:
        data = await state.get_data()
        if not ticket_type:
            ticket_type = data.get('ticket_type', 'Обращение')

        photo_file_id = data.get('photo_file_id')
        user = message.from_user

        message_text = caption if caption else message.text

        if not message_text:
            await message.answer("Сообщение не может быть пустым. Попробуйте снова.",
                                 reply_markup=get_main_menu())
            await state.clear()
            return

        # Добавляем обращение в базу данных
        ticket_id = add_support_ticket(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            ticket_type=ticket_type,
            message=message_text,
            photo_file_id=photo_file_id
        )

        # Создаем клавиатуру для модераторов
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Рассмотрено", callback_data=f"resolve_{ticket_id}"),
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"respond_{ticket_id}")
            ]
        ])

        # Формируем сообщение для модераторов
        mod_text = f"<b>Новое обращение #{ticket_id}</b>\n"
        mod_text += f"<b>Тип:</b> {ticket_type}\n"
        mod_text += f"<b>Пользователь:</b> {user.first_name or ''} {user.last_name or ''}\n"
        mod_text += f"<b>ID:</b> <code>{user.id}</code>\n"
        if user.username:
            mod_text += f"<b>Username:</b> @{user.username}\n"
        mod_text += f"<b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
        mod_text += f"\n<b>Сообщение:</b>\n"
        mod_text += f"<i>{message_text}</i>"

        try:
            if photo_file_id:
                await bot.send_photo(
                    chat_id=SUPPORT_CHAT_ID,
                    photo=photo_file_id,
                    caption=mod_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    text=mod_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Ошибка при отправке в чат поддержки: {e}")

        # Отправляем подтверждение пользователю
        user_text = f"Ваше {ticket_type.lower()} принято.\n"
        user_text += f"ID обращения: #{ticket_id}\n"
        user_text += "Модераторы рассмотрят его в ближайшее время.\n"
        user_text += "Вы получите уведомление о результате."

        await message.answer(user_text, reply_markup=get_main_menu())
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка при обработке обращения: {e}")
        await message.answer("Произошла ошибка при отправке обращения. Попробуйте позже.",
                             reply_markup=get_main_menu())
        await state.clear()


@dp.callback_query(F.data.startswith("resolve_"))
async def resolve_ticket(callback: types.CallbackQuery):
    """Обработка кнопки Рассмотрено"""
    try:
        ticket_id = int(callback.data.split("_")[1])
        admin_id = callback.from_user.id

        update_ticket_status(ticket_id, admin_id, "resolved", "Рассмотрено модератором")

        ticket = get_ticket_by_id(ticket_id)
        if ticket:
            user_id = ticket[1]
            ticket_type = ticket[5]

            user_text = f"Ваше {ticket_type.lower()} #{ticket_id} рассмотрено.\n"
            user_text += "Рассмотрено модератором.\n"
            user_text += "Спасибо за обращение!"

            try:
                await bot.send_message(chat_id=user_id, text=user_text)
            except:
                pass

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n✅ Рассмотрено",
                parse_mode="HTML"
            )
        except:
            try:
                await callback.message.edit_text(
                    text=callback.message.text + "\n\n✅ Рассмотрено",
                    parse_mode="HTML"
                )
            except:
                pass

        await callback.answer("Обращение отмечено как рассмотренное")

    except Exception as e:
        logger.error(f"Ошибка при рассмотрении обращения: {e}")
        await callback.answer("Произошла ошибка")


@dp.callback_query(F.data.startswith("respond_"))
async def respond_ticket(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопки Ответить"""
    try:
        ticket_id = int(callback.data.split("_")[1])

        await state.update_data(
            ticket_id=ticket_id,
            message_id=callback.message.message_id,
            is_photo=hasattr(callback.message, 'photo') and callback.message.photo
        )

        await callback.message.answer(
            f"Ответ на обращение #{ticket_id}\n\n"
            "Напишите ваш ответ пользователю. Он получит его как сообщение от бота.\n"
            "Для отмены напишите /cancel"
        )

        await state.set_state(SupportStates.waiting_for_response)
        await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка при подготовке ответа: {e}")
        await callback.answer("Произошла ошибка")


@dp.message(SupportStates.waiting_for_response)
async def process_response(message: types.Message, state: FSMContext):
    """Обработка ответа модератора"""
    try:
        data = await state.get_data()
        ticket_id = data.get('ticket_id')
        message_id = data.get('message_id')

        if not ticket_id:
            await message.answer("Ошибка: ID обращения не найден", reply_markup=get_main_menu())
            await state.clear()
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"send_{ticket_id}_{message_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{ticket_id}")
            ]
        ])

        preview_text = f"Предпросмотр ответа для обращения #{ticket_id}\n\n"
        preview_text += f"Ваш ответ:\n{message.text}\n\n"
        preview_text += "Подтвердите отправку:"

        await message.answer(preview_text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Ошибка при обработке ответа: {e}")
        await message.answer("Произошла ошибка", reply_markup=get_main_menu())
        await state.clear()


@dp.callback_query(F.data.startswith("send_"))
async def send_response(callback: types.CallbackQuery, state: FSMContext):
    """Отправка ответа пользователю"""
    try:
        parts = callback.data.split("_")
        ticket_id = int(parts[1])
        message_id = int(parts[2])
        admin_id = callback.from_user.id

        response_message = callback.message.text
        lines = response_message.split("\n")
        response_text = ""
        in_response = False
        for line in lines:
            if "Ваш ответ:" in line:
                in_response = True
            elif in_response and line and not line.startswith("Подтвердите"):
                response_text += line + "\n"
            elif line.startswith("Подтвердите"):
                break

        response_text = response_text.strip()

        ticket = get_ticket_by_id(ticket_id)
        if not ticket:
            await callback.answer("Обращение не найдено")
            return

        user_id = ticket[1]
        ticket_type = ticket[5]

        update_ticket_status(ticket_id, admin_id, "responded", response_text)

        user_text = f"Ответ на ваше {ticket_type.lower()} #{ticket_id}\n\n"
        user_text += f"Сообщение от модератора:\n{response_text}\n\n"
        user_text += "Спасибо за обращение!"

        try:
            await bot.send_message(chat_id=user_id, text=user_text)
        except Exception as e:
            logger.error(f"Ошибка при отправке ответа пользователю: {e}")
            await callback.answer("Не удалось отправить ответ пользователю")
            return

        try:
            await bot.edit_message_caption(
                chat_id=SUPPORT_CHAT_ID,
                message_id=message_id,
                caption=callback.message.caption + f"\n\n💬 Ответ отправлен пользователю",
                reply_markup=None
            )
        except:
            try:
                await bot.edit_message_text(
                    chat_id=SUPPORT_CHAT_ID,
                    message_id=message_id,
                    text=callback.message.text + f"\n\n💬 Ответ отправлен пользователю",
                    reply_markup=None
                )
            except:
                pass

        await callback.message.edit_text("Ответ на обращение отправлен пользователю", reply_markup=None)

        await callback.answer("Ответ отправлен")
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка при отправке ответа: {e}")
        await callback.answer("Произошла ошибка")
        await state.clear()


@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_response(callback: types.CallbackQuery, state: FSMContext):
    """Отмена отправки ответа"""
    try:
        await callback.message.edit_text("Отправка ответа отменена", reply_markup=None)
        await callback.answer("Отправка отменена")
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка при отмене ответа: {e}")
        await callback.answer("Произошла ошибка")


# Команды владельца бота
@dp.message(Command("add"))
async def add_command(message: types.Message, command: CommandObject):
    """Команда /add для добавления сообщения владельца"""
    try:
        user = message.from_user

        if user.id != BOT_OWNER_ID:
            await message.reply("Эта команда доступна только владельцу бота")
            return

        text = command.args or ""

        if not text:
            await message.reply("Укажите текст сообщения после команды /add")
            return

        set_owner_message(user.id, text)

        response = f"Сообщение владельца установлено\n\n{text}"

        await message.reply(response)

    except Exception as e:
        logger.error(f"Ошибка в команде add: {e}")


@dp.message(Command("unadd"))
async def unadd_command(message: types.Message):
    """Команда /unadd для удаления сообщения владельца"""
    try:
        user = message.from_user

        if user.id != BOT_OWNER_ID:
            await message.reply("Эта команда доступна только владельцу бота")
            return

        remove_owner_message()

        response = "Сообщение владельца удалено"

        await message.reply(response)

    except Exception as e:
        logger.error(f"Ошибка в команде unadd: {e}")


# Основные команды модерации (РАБОТАЮТ ТОЛЬКО В РАЗРЕШЕННОМ ЧАТЕ)
@dp.message(Command("ban"))
async def ban_command(message: types.Message, command: CommandObject):
    """Команда /ban для бана пользователей"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            await message.reply("❌ Этот бот работает только в одном конкретном чате!")
            return

        # Проверяем что это группа
        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        # Проверяем права отправителя
        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        # Проверяем права бота
        if not await can_bot_restrict(chat):
            await message.reply("❌ У бота нет прав для ограничения пользователей!")
            return

        # Удаляем команду
        try:
            await message.delete()
        except:
            pass

        # Получаем цель и причину
        target_user, reason = await get_target_user(message, command.args or "")

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        # Проверки
        if target_user.id == user.id:
            return
        if target_user.is_bot:
            await message.answer("❌ Нельзя забанить другого бота!")
            return
        if await is_user_admin(chat, target_user.id):
            await message.answer("❌ Нельзя забанить администратора!")
            return

        # Выполняем бан
        try:
            await bot.ban_chat_member(
                chat_id=chat.id,
                user_id=target_user.id,
                until_date=datetime.now() + timedelta(days=36500)
            )

            logger.info(f"Пользователь {target_user.id} заблокирован в чате {chat.id}")
            clear_warns_from_db(chat.id, target_user.id)

            await send_action_notification(
                chat_id=chat.id,
                action="ban",
                target_user=target_user,
                reason=reason,
                admin_user=user
            )

        except Exception as e:
            logger.error(f"Ошибка при бане: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")

    except Exception as e:
        logger.error(f"Ошибка в команде ban: {e}")


@dp.message(Command("mute"))
async def mute_command(message: types.Message, command: CommandObject):
    """Команда /mute для мута пользователей"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            return

        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        if not await can_bot_restrict(chat):
            await message.reply("❌ У бота нет прав для ограничения пользователей!")
            return

        try:
            await message.delete()
        except:
            pass

        args = command.args or ""
        target_user = None
        mute_time = "30m"  # по умолчанию 30 минут
        reason = "Без указания причины"

        # Если ответ на сообщение
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            if args:
                # Парсим время и причину
                parts = args.split(maxsplit=1)
                if parts[0].isdigit():
                    mute_time = parts[0] + "m"
                    if len(parts) > 1:
                        reason = parts[1]
                else:
                    reason = args
        else:
            # Ищем пользователя в аргументах
            if args:
                parts = args.split(maxsplit=2)
                if len(parts) >= 1:
                    identifier = parts[0]
                    if identifier.startswith('@'):
                        try:
                            chat_member = await chat.get_member(identifier[1:])
                            target_user = chat_member.user
                        except:
                            pass
                    elif identifier.isdigit():
                        try:
                            chat_member = await chat.get_member(int(identifier))
                            target_user = chat_member.user
                        except:
                            pass
                    
                    if target_user and len(parts) >= 2:
                        if parts[1].isdigit():
                            mute_time = parts[1] + "m"
                            if len(parts) > 2:
                                reason = parts[2]
                        else:
                            reason = parts[1]

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        if target_user.id == user.id:
            return
        if target_user.is_bot:
            await message.answer("❌ Нельзя замутить другого бота!")
            return
        if await is_user_admin(chat, target_user.id):
            await message.answer("❌ Нельзя замутить администратора!")
            return

        # Парсим время мута
        try:
            if mute_time.endswith('m'):
                minutes = int(mute_time[:-1])
                until_date = datetime.now() + timedelta(minutes=minutes)
                duration_text = f"{minutes} минут"
            elif mute_time.endswith('h'):
                hours = int(mute_time[:-1])
                until_date = datetime.now() + timedelta(hours=hours)
                duration_text = f"{hours} часов"
            elif mute_time.endswith('d'):
                days = int(mute_time[:-1])
                until_date = datetime.now() + timedelta(days=days)
                duration_text = f"{days} дней"
            else:
                minutes = int(mute_time) if mute_time.isdigit() else 30
                until_date = datetime.now() + timedelta(minutes=minutes)
                duration_text = f"{minutes} минут"
        except:
            until_date = datetime.now() + timedelta(minutes=30)
            duration_text = "30 минут"

        try:
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=target_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )

            logger.info(f"Пользователь {target_user.id} замучен в чате {chat.id} на {duration_text}")

            await send_action_notification(
                chat_id=chat.id,
                action="mute",
                target_user=target_user,
                duration=duration_text,
                reason=reason,
                admin_user=user
            )

        except Exception as e:
            logger.error(f"Ошибка при муте: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")

    except Exception as e:
        logger.error(f"Ошибка в команде mute: {e}")


@dp.message(Command("unmute"))
async def unmute_command(message: types.Message, command: CommandObject):
    """Команда /unmute для размута пользователей"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            return

        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        if not await can_bot_restrict(chat):
            await message.reply("❌ У бота нет прав для ограничения пользователей!")
            return

        try:
            await message.delete()
        except:
            pass

        target_user, reason = await get_target_user(message, command.args or "")

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        try:
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=target_user.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
            )

            logger.info(f"Пользователь {target_user.id} размучен в чате {chat.id}")

            await send_action_notification(
                chat_id=chat.id,
                action="unmute",
                target_user=target_user,
                reason=reason,
                admin_user=user
            )

        except Exception as e:
            logger.error(f"Ошибка при размуте: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")

    except Exception as e:
        logger.error(f"Ошибка в команде unmute: {e}")


@dp.message(Command("warn"))
async def warn_command(message: types.Message, command: CommandObject):
    """Команда /warn для выдачи предупреждения"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            return

        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        try:
            await message.delete()
        except:
            pass

        target_user, reason = await get_target_user(message, command.args or "")

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        if target_user.id == user.id:
            return
        if target_user.is_bot:
            await message.answer("❌ Нельзя выдать предупреждение боту!")
            return
        if await is_user_admin(chat, target_user.id):
            await message.answer("❌ Нельзя выдать предупреждение администратору!")
            return

        # Добавляем предупреждение в БД
        add_warn_to_db(chat.id, target_user.id, reason)
        
        # Получаем все предупреждения пользователя
        warns = get_user_warns_from_db(chat.id, target_user.id)
        warn_count = len(warns)

        # Отправляем уведомление
        await send_action_notification(
            chat_id=chat.id,
            action="warn",
            target_user=target_user,
            reason=reason,
            admin_user=user
        )

        # Сообщаем о количестве предупреждений
        warn_info = f"\n\n⚠️ У пользователя {warn_count}/3 предупреждений"
        if warn_count >= 3:
            warn_info += "\n🚨 Достигнут лимит предупреждений! Рекомендуется забанить."
        
        await message.answer(f"✅ Предупреждение выдано.{warn_info}")

    except Exception as e:
        logger.error(f"Ошибка в команде warn: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(Command("unwarn"))
async def unwarn_command(message: types.Message, command: CommandObject):
    """Команда /unwarn для снятия предупреждения"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            return

        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        try:
            await message.delete()
        except:
            pass

        target_user, reason = await get_target_user(message, command.args or "")

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        # Проверяем есть ли предупреждения
        warns = get_user_warns_from_db(chat.id, target_user.id)
        if not warns:
            await message.answer("❌ У пользователя нет предупреждений!")
            return

        # Удаляем последнее предупреждение
        remove_last_warn_from_db(chat.id, target_user.id)
        
        # Получаем оставшиеся предупреждения
        remaining_warns = get_user_warns_from_db(chat.id, target_user.id)
        remaining_count = len(remaining_warns)

        # Отправляем уведомление
        await send_action_notification(
            chat_id=chat.id,
            action="unwarn",
            target_user=target_user,
            reason=reason,
            admin_user=user
        )

        await message.answer(f"✅ Предупреждение снято. Осталось: {remaining_count}/3")

    except Exception as e:
        logger.error(f"Ошибка в команде unwarn: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(Command("warns"))
async def warns_command(message: types.Message, command: CommandObject):
    """Команда /warns для просмотра предупреждений"""
    try:
        chat = message.chat
        
        # Проверяем разрешенный чат
        if not await is_allowed_chat(chat.id):
            return

        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return

        user = message.from_user

        if not await is_user_admin(chat, user.id):
            await message.delete()
            return

        target_user, _ = await get_target_user(message, command.args or "")

        if not target_user:
            await message.answer("❌ Укажите пользователя (ответом на сообщение или @username)")
            return

        # Получаем предупреждения
        warns = get_user_warns_from_db(chat.id, target_user.id)
        warn_count = len(warns)

        if warn_count == 0:
            await message.answer(f"✅ У пользователя {await format_user_display(target_user)} нет предупреждений.")
            return

        # Формируем список предупреждений
        warns_text = f"📋 Предупреждения пользователя {await format_user_display(target_user)}:\n"
        warns_text += f"Всего: {warn_count}/3\n\n"
        
        for i, warn in enumerate(warns, 1):
            warns_text += f"{i}. {warn}\n"

        if warn_count >= 3:
            warns_text += "\n⚠️ Достигнут лимит предупреждений! Рекомендуется забанить."

        await message.answer(warns_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка в команде warns: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")


# Обработчик сообщений в группах
@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def handle_group_messages(message: types.Message):
    """Обработчик сообщений в группах"""
    # Проверяем разрешенный чат
    if not await is_allowed_chat(message.chat.id):
        return
    
    await silent_delete_service_messages(message)


async def error_handler(update: types.Update, exception: Exception):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {exception}", exc_info=exception)
    return True


# HTTP сервер для Render
async def health_check(request):
    """Проверка здоровья сервера"""
    return web.Response(text="OK")


async def start_http_server():
    """Запуск HTTP сервера"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    print(f"HTTP сервер запущен на порту {PORT}")
    return runner


async def main():
    """Запуск бота"""
    # Подключаем обработчик ошибок
    dp.errors.register(error_handler)

    # Запускаем HTTP сервер (требуется для Render)
    http_server = await start_http_server()
    
    logger.info("=" * 50)
    logger.info("Бот запущен и настроен!")
    logger.info(f"Владелец бота: {BOT_OWNER_ID}")
    logger.info(f"Разрешенный чат: {ALLOWED_CHAT_ID}")
    logger.info(f"Чат поддержки: {SUPPORT_CHAT_ID}")
    logger.info("=" * 50)
    logger.info("Доступные команды в группе:")
    logger.info("/ban - забанить пользователя")
    logger.info("/mute - замутить пользователя")
    logger.info("/unmute - размутить пользователя")
    logger.info("/warn - выдать предупреждение")
    logger.info("/unwarn - снять предупреждение")
    logger.info("/warns - посмотреть предупреждения")
    logger.info("=" * 50)
    logger.info(f"HTTP сервер слушает порт: {PORT}")

    # Запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
    
    # Останавливаем HTTP сервер при завершении
    await http_server.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
