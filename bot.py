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

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
# Получаем настройки из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "6493670021"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-1003559804187"))
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "-1001234567890"))  # ЗАМЕНИ НА ID СВОЕГО ЧАТА!
PORT = int(os.getenv("PORT", "10000"))

# Проверка обязательных настроек
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не установлен! Добавь в Environment Variables на Render")
    exit(1)

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = "bot_database.db"

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Таблица для предупреждений
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            admin_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица для сообщений владельца
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS owner_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            owner_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица для обращений в поддержку
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            ticket_type TEXT,
            message TEXT,
            photo_id TEXT,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            admin_response TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# ==================== ФУНКЦИИ БАЗЫ ДАННЫХ ====================
def add_warn(chat_id: int, user_id: int, reason: str, admin_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO warns (chat_id, user_id, reason, admin_id) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, reason, admin_id)
    )
    conn.commit()
    conn.close()

def get_warns(chat_id: int, user_id: int) -> List[tuple]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, reason, admin_id, timestamp FROM warns WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC",
        (chat_id, user_id)
    )
    result = cursor.fetchall()
    conn.close()
    return result

def remove_warn(warn_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warns WHERE id = ?", (warn_id,))
    conn.commit()
    conn.close()

def clear_warns(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def set_owner_message(message: str, owner_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM owner_messages")
    cursor.execute("INSERT INTO owner_messages (message, owner_id) VALUES (?, ?)", (message, owner_id))
    conn.commit()
    conn.close()

def get_owner_message() -> Optional[str]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT message FROM owner_messages ORDER BY id DESC LIMIT 1")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def add_support_ticket(user_id: int, username: str, first_name: str, last_name: str,
                       ticket_type: str, message: str, photo_id: str = None) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO support_tickets (user_id, username, first_name, last_name, ticket_type, message, photo_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username or "", first_name or "", last_name or "", ticket_type, message, photo_id))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id

def get_ticket(ticket_id: int) -> Optional[tuple]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def update_ticket(ticket_id: int, admin_id: int, status: str, response: str = None):
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

# ==================== СОСТОЯНИЯ (FSM) ====================
class SupportStates(StatesGroup):
    waiting_for_appeal = State()
    waiting_for_complaint = State()
    waiting_for_suggestion = State()
    waiting_for_response = State()
    waiting_for_photo = State()
    waiting_for_text_with_photo = State()

# ==================== УТИЛИТЫ ====================
def is_allowed_chat(chat_id: int) -> bool:
    """Проверяет, разрешен ли этот чат для бота"""
    return chat_id == ALLOWED_CHAT_ID

async def check_permissions(message: types.Message) -> bool:
    """Проверяет права пользователя и бота"""
    if not is_allowed_chat(message.chat.id):
        await message.reply("❌ Этот бот работает только в одном конкретном чате!")
        return False
    
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return False
    
    # Проверяем права пользователя
    try:
        member = await message.chat.get_member(message.from_user.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
            await message.delete()
            return False
    except:
        await message.delete()
        return False
    
    # Проверяем права бота
    try:
        bot_member = await message.chat.get_member((await bot.me()).id)
        if not bot_member.can_restrict_members:
            await message.reply("❌ У бота нет прав для ограничения пользователей!")
            return False
    except:
        return False
    
    return True

async def get_target_user(message: types.Message, args: str = "") -> tuple[Optional[types.User], str]:
    """Получает целевого пользователя из команды"""
    chat = message.chat
    
    # Если команда вызвана ответом на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        reason = args.strip() or "Без указания причины"
        return target_user, reason
    
    # Если указаны аргументы
    if args:
        parts = args.split(maxsplit=1)
        identifier = parts[0]
        reason = parts[1] if len(parts) > 1 else "Без указания причины"
        
        # По username
        if identifier.startswith('@'):
            try:
                member = await chat.get_member(identifier[1:])
                return member.user, reason
            except:
                return None, reason
        # По ID
        elif identifier.isdigit():
            try:
                member = await chat.get_member(int(identifier))
                return member.user, reason
            except:
                return None, reason
    
    return None, "Без указания причины"

async def format_user(user: types.User) -> str:
    """Форматирует отображение пользователя"""
    if user.username:
        return f"@{user.username}"
    return f"<code>{user.id}</code>"

async def send_notification(chat_id: int, action: str, target_user: types.User, 
                           admin_user: types.User = None, reason: str = "", duration: str = ""):
    """Отправляет уведомление о действии"""
    try:
        admin_display = await format_user(admin_user) if admin_user else "Система"
        target_display = await format_user(target_user)
        
        actions = {
            "ban": f"💬 {admin_display} выдал блокировку пользователю {target_display}",
            "unban": f"💬 {admin_display} снял блокировку с пользователя {target_display}",
            "mute": f"💬 {admin_display} выдал мут пользователю {target_display}",
            "unmute": f"💬 {admin_display} снял мут с пользователя {target_display}",
            "warn": f"💬 {admin_display} выдал предупреждение пользователю {target_display}",
            "unwarn": f"💬 {admin_display} снял предупреждение с пользователя {target_display}"
        }
        
        notification = actions.get(action, f"💬 {admin_display} выполнил действие над {target_display}")
        
        if duration:
            notification += f" на {duration}"
        
        if reason and reason != "Без указания причины":
            notification += f"\nПричина: {reason}"
        
        # Добавляем сообщение владельца
        owner_msg = get_owner_message()
        if owner_msg:
            notification += f"\n\n{owner_msg}"
        
        await bot.send_message(
            chat_id=chat_id,
            text=notification,
            parse_mode="HTML",
            disable_notification=True
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")

# ==================== КЛАВИАТУРЫ ====================
def get_main_menu() -> ReplyKeyboardMarkup:
    """Главное меню (только для личных сообщений)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🆔 Мой ID")],
            [KeyboardButton(text="🆘 Поддержка")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )

def get_support_menu() -> ReplyKeyboardMarkup:
    """Меню поддержки"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Обжаловать наказание")],
            [KeyboardButton(text="⚠️ Пожаловаться на пользователя")],
            [KeyboardButton(text="💡 Предложение по улучшению")],
            [KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

# ---------- ОСНОВНЫЕ КОМАНДЫ ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Команда /start"""
    try:
        welcome = "🤖 *Добро пожаловать!*\n\n"
        welcome += "Я бот для модерации чата. Вот что я умею:\n\n"
        welcome += "📌 *В личных сообщениях:*\n"
        welcome += "• Узнать свой ID\n"
        welcome += "• Обратиться в поддержку\n"
        welcome += "• Получить помощь\n\n"
        welcome += "📌 *В группе (для админов):*\n"
        welcome += "• /ban - забанить пользователя\n"
        welcome += "• /mute - замутить пользователя\n"
        welcome += "• /warn - выдать предупреждение\n"
        welcome += "• /unwarn - снять предупреждение\n"
        welcome += "• /warns - посмотреть предупреждения\n"
        
        owner_msg = get_owner_message()
        if owner_msg:
            welcome += f"\n📢 *Сообщение владельца:*\n{owner_msg}"
        
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(welcome, parse_mode="Markdown", reply_markup=get_main_menu())
        else:
            await message.answer(welcome, parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Ошибка в /start: {e}")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Команда /help"""
    help_text = """
📋 *Доступные команды:*

*Для админов в группе:*
/ban [причина] - забанить пользователя (ответом на сообщение)
/mute [время] [причина] - замутить пользователя
/warn [причина] - выдать предупреждение
/unwarn - снять последнее предупреждение
/warns - посмотреть предупреждения пользователя

*Для всех:*
/start - информация о боте
/help - это сообщение
/id - узнать свой ID

*Для владельца бота:*
/add [текст] - установить сообщение владельца
/clear_msg - удалить сообщение владельца

*Примеры:*
/ban Нарушение правил
/mute 30 Спам
/warn Оскорбление
"""
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    """Команда /id - узнать свой ID"""
    user = message.from_user
    text = f"👤 *Ваши данные:*\n\n"
    text += f"🆔 ID: `{user.id}`\n"
    text += f"📛 Имя: {user.first_name or ''}\n"
    if user.last_name:
        text += f"📛 Фамилия: {user.last_name}\n"
    if user.username:
        text += f"🔗 Username: @{user.username}\n"
    
    await message.answer(text, parse_mode="Markdown")

# ---------- КНОПКИ МЕНЮ (ТОЛЬКО В ЛИЧКЕ) ----------
@dp.message(F.text == "🆔 Мой ID")
async def btn_my_id(message: types.Message):
    """Кнопка 'Мой ID'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    await cmd_id(message)

@dp.message(F.text == "ℹ️ Помощь")
async def btn_help(message: types.Message):
    """Кнопка 'Помощь'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    await cmd_help(message)

@dp.message(F.text == "🆘 Поддержка")
async def btn_support(message: types.Message):
    """Кнопка 'Поддержка'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    text = "🆘 *Поддержка*\n\n"
    text += "Выберите тип обращения:\n\n"
    text += "📝 *Обжаловать наказание* - если считаете наказание несправедливым\n"
    text += "⚠️ *Пожаловаться на пользователя* - жалоба на другого участника\n"
    text += "💡 *Предложение по улучшению* - ваши идеи для улучшения\n\n"
    text += "Ваше обращение будет отправлено модераторам."
    
    await message.answer(text, parse_mode="Markdown", reply_markup=get_support_menu())

@dp.message(F.text == "🔙 Назад")
async def btn_back(message: types.Message):
    """Кнопка 'Назад'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    await message.answer("Возвращаемся в главное меню", reply_markup=get_main_menu())

# ---------- КОМАНДЫ ВЛАДЕЛЬЦА ----------
@dp.message(Command("add"))
async def cmd_add(message: types.Message, command: CommandObject):
    """Команда /add - установить сообщение владельца"""
    try:
        if message.from_user.id != BOT_OWNER_ID:
            await message.reply("❌ Эта команда только для владельца бота!")
            return
        
        text = command.args
        if not text:
            await message.reply("❌ Укажите текст после команды: /add [текст]")
            return
        
        set_owner_message(text, message.from_user.id)
        await message.reply(f"✅ Сообщение владельца установлено:\n\n{text}")
        
    except Exception as e:
        logger.error(f"Ошибка в /add: {e}")

@dp.message(Command("clear_msg"))
async def cmd_clear_msg(message: types.Message):
    """Команда /clear_msg - удалить сообщение владельца"""
    try:
        if message.from_user.id != BOT_OWNER_ID:
            await message.reply("❌ Эта команда только для владельца бота!")
            return
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM owner_messages")
        conn.commit()
        conn.close()
        
        await message.reply("✅ Сообщение владельца удалено")
        
    except Exception as e:
        logger.error(f"Ошибка в /clear_msg: {e}")

# ---------- КОМАНДЫ МОДЕРАЦИИ ----------
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    """Команда /ban - забанить пользователя"""
    try:
        # Проверяем права
        if not await check_permissions(message):
            return
        
        # Получаем цель
        target_user, reason = await get_target_user(message, command.args or "")
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Проверяем что не себя и не бота
        if target_user.id == message.from_user.id:
            await message.reply("❌ Нельзя забанить себя!")
            return
        if target_user.is_bot:
            await message.reply("❌ Нельзя забанить другого бота!")
            return
        
        # Проверяем что цель не админ
        try:
            target_member = await message.chat.get_member(target_user.id)
            if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await message.reply("❌ Нельзя забанить администратора!")
                return
        except:
            pass
        
        # Выполняем бан
        await bot.ban_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            until_date=datetime.now() + timedelta(days=36500)
        )
        
        # Очищаем предупреждения
        clear_warns(message.chat.id, target_user.id)
        
        # Отправляем уведомление
        await send_notification(
            chat_id=message.chat.id,
            action="ban",
            target_user=target_user,
            admin_user=message.from_user,
            reason=reason
        )
        
        logger.info(f"Бан: {target_user.id} в чате {message.chat.id}")
        
    except Exception as e:
        logger.error(f"Ошибка в /ban: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    """Команда /mute - замутить пользователя"""
    try:
        if not await check_permissions(message):
            return
        
        args = command.args or ""
        target_user = None
        mute_time = "30"
        reason = "Без указания причины"
        
        # Парсим аргументы
        if message.reply_to_message:
            target_user = message.reply_to_message.from_user
            if args:
                parts = args.split(maxsplit=1)
                if parts[0].isdigit():
                    mute_time = parts[0]
                    reason = parts[1] if len(parts) > 1 else "Без указания причины"
                else:
                    reason = args
        elif args:
            parts = args.split(maxsplit=2)
            if len(parts) >= 1:
                identifier = parts[0]
                if identifier.startswith('@'):
                    try:
                        member = await message.chat.get_member(identifier[1:])
                        target_user = member.user
                    except:
                        pass
                elif identifier.isdigit():
                    try:
                        member = await message.chat.get_member(int(identifier))
                        target_user = member.user
                    except:
                        pass
                
                if target_user and len(parts) >= 2:
                    if parts[1].isdigit():
                        mute_time = parts[1]
                        reason = parts[2] if len(parts) > 2 else "Без указания причины"
                    else:
                        reason = parts[1]
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Проверки
        if target_user.id == message.from_user.id:
            await message.reply("❌ Нельзя замутить себя!")
            return
        if target_user.is_bot:
            await message.reply("❌ Нельзя замутить бота!")
            return
        
        try:
            target_member = await message.chat.get_member(target_user.id)
            if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await message.reply("❌ Нельзя замутить администратора!")
                return
        except:
            pass
        
        # Парсим время
        try:
            minutes = int(mute_time)
            if minutes <= 0:
                minutes = 30
            if minutes > 43200:  # 30 дней
                minutes = 43200
        except:
            minutes = 30
        
        until_date = datetime.now() + timedelta(minutes=minutes)
        
        # Выполняем мут
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        
        # Отправляем уведомление
        await send_notification(
            chat_id=message.chat.id,
            action="mute",
            target_user=target_user,
            admin_user=message.from_user,
            reason=reason,
            duration=f"{minutes} минут"
        )
        
        logger.info(f"Мут: {target_user.id} на {minutes} минут")
        
    except Exception as e:
        logger.error(f"Ошибка в /mute: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    """Команда /unmute - размутить пользователя"""
    try:
        if not await check_permissions(message):
            return
        
        target_user, reason = await get_target_user(message, command.args or "")
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Размучиваем
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        
        # Отправляем уведомление
        await send_notification(
            chat_id=message.chat.id,
            action="unmute",
            target_user=target_user,
            admin_user=message.from_user,
            reason=reason
        )
        
        logger.info(f"Размут: {target_user.id}")
        
    except Exception as e:
        logger.error(f"Ошибка в /unmute: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    """Команда /warn - выдать предупреждение"""
    try:
        if not await check_permissions(message):
            return
        
        target_user, reason = await get_target_user(message, command.args or "")
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Проверки
        if target_user.id == message.from_user.id:
            await message.reply("❌ Нельзя выдать предупреждение себе!")
            return
        if target_user.is_bot:
            await message.reply("❌ Нельзя выдать предупреждение боту!")
            return
        
        try:
            target_member = await message.chat.get_member(target_user.id)
            if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await message.reply("❌ Нельзя выдать предупреждение администратору!")
                return
        except:
            pass
        
        # Добавляем предупреждение
        add_warn(message.chat.id, target_user.id, reason, message.from_user.id)
        
        # Получаем количество предупреждений
        warns = get_warns(message.chat.id, target_user.id)
        warn_count = len(warns)
        
        # Отправляем уведомление
        await send_notification(
            chat_id=message.chat.id,
            action="warn",
            target_user=target_user,
            admin_user=message.from_user,
            reason=reason
        )
        
        # Сообщаем о количестве
        warn_msg = f"⚠️ У пользователя {await format_user(target_user)} теперь {warn_count}/3 предупреждений"
        if warn_count >= 3:
            warn_msg += "\n🚨 *Достигнут лимит! Рекомендуется забанить.*"
        
        await message.reply(warn_msg, parse_mode="HTML")
        
        logger.info(f"Варн: {target_user.id}, всего {warn_count}")
        
    except Exception as e:
        logger.error(f"Ошибка в /warn: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    """Команда /unwarn - снять предупреждение"""
    try:
        if not await check_permissions(message):
            return
        
        target_user, reason = await get_target_user(message, command.args or "")
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Получаем предупреждения
        warns = get_warns(message.chat.id, target_user.id)
        if not warns:
            await message.reply("❌ У пользователя нет предупреждений!")
            return
        
        # Удаляем последнее предупреждение
        last_warn_id = warns[0][0]
        remove_warn(last_warn_id)
        
        # Получаем оставшиеся
        remaining = get_warns(message.chat.id, target_user.id)
        remaining_count = len(remaining)
        
        # Отправляем уведомление
        await send_notification(
            chat_id=message.chat.id,
            action="unwarn",
            target_user=target_user,
            admin_user=message.from_user,
            reason=reason
        )
        
        await message.reply(f"✅ Предупреждение снято. Осталось: {remaining_count}/3")
        
        logger.info(f"Удален варн: {target_user.id}, осталось {remaining_count}")
        
    except Exception as e:
        logger.error(f"Ошибка в /unwarn: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

@dp.message(Command("warns"))
async def cmd_warns(message: types.Message, command: CommandObject):
    """Команда /warns - посмотреть предупреждения"""
    try:
        if not await check_permissions(message):
            return
        
        target_user, _ = await get_target_user(message, command.args or "")
        
        if not target_user:
            await message.reply("❌ Укажите пользователя (ответом на сообщение или @username/ID)")
            return
        
        # Получаем предупреждения
        warns = get_warns(message.chat.id, target_user.id)
        
        if not warns:
            await message.reply(f"✅ У пользователя {await format_user(target_user)} нет предупреждений.")
            return
        
        # Формируем сообщение
        warn_text = f"📋 *Предупреждения пользователя {await format_user(target_user)}:*\n"
        warn_text += f"Всего: {len(warns)}/3\n\n"
        
        for i, (warn_id, reason, admin_id, timestamp) in enumerate(warns[:10], 1):
            time_str = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            warn_text += f"{i}. *{reason}*\n   🕐 {time_str}\n"
        
        if len(warns) >= 3:
            warn_text += "\n⚠️ *Достигнут лимит предупреждений!*"
        
        await message.reply(warn_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Ошибка в /warns: {e}")
        await message.reply(f"❌ Ошибка: {str(e)}")

# ---------- ПОДДЕРЖКА (ТОЛЬКО В ЛИЧКЕ) ----------
@dp.message(F.text == "📝 Обжаловать наказание")
async def btn_appeal(message: types.Message, state: FSMContext):
    """Кнопка 'Обжаловать наказание'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    await state.update_data(ticket_type="Обжалование")
    await message.answer(
        "📝 *Обжалование наказания*\n\n"
        "Опишите подробно:\n"
        "1. Какое наказание вы получили\n"
        "2. Почему считаете его несправедливым\n"
        "3. Любые доказательства\n\n"
        "Вы можете приложить фото.\n"
        "Отправьте текст или фото с подписью.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(SupportStates.waiting_for_appeal)

@dp.message(F.text == "⚠️ Пожаловаться на пользователя")
async def btn_complaint(message: types.Message, state: FSMContext):
    """Кнопка 'Пожаловаться на пользователя'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    await state.update_data(ticket_type="Жалоба")
    await message.answer(
        "⚠️ *Жалоба на пользователя*\n\n"
        "Опишите подробно:\n"
        "1. На кого жалуетесь (ID или @username)\n"
        "2. Что произошло\n"
        "3. Когда это случилось\n"
        "4. Доказательства (скриншоты и т.д.)\n\n"
        "Вы можете приложить фото.\n"
        "Отправьте текст или фото с подписью.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(SupportStates.waiting_for_complaint)

@dp.message(F.text == "💡 Предложение по улучшению")
async def btn_suggestion(message: types.Message, state: FSMContext):
    """Кнопка 'Предложение по улучшению'"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    await state.update_data(ticket_type="Предложение")
    await message.answer(
        "💡 *Предложение по улучшению*\n\n"
        "Опишите подробно:\n"
        "1. Что вы предлагаете улучшить\n"
        "2. Как это поможет сообществу\n"
        "3. Конкретные детали реализации\n\n"
        "Вы можете приложить фото.\n"
        "Отправьте текст или фото с подписью.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(SupportStates.waiting_for_suggestion)

# Обработка обращений с фото
@dp.message(SupportStates.waiting_for_appeal, F.photo)
@dp.message(SupportStates.waiting_for_complaint, F.photo)
@dp.message(SupportStates.waiting_for_suggestion, F.photo)
async def handle_ticket_photo(message: types.Message, state: FSMContext):
    """Обработка фото в обращениях"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    try:
        photo_id = message.photo[-1].file_id
        await state.update_data(photo_id=photo_id)
        
        if message.caption:
            await process_ticket(message, state, message.caption)
        else:
            await message.answer("📷 Фото получено. Теперь отправьте текст обращения.")
            await state.set_state(SupportStates.waiting_for_text_with_photo)
            
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        await message.answer("❌ Ошибка. Попробуйте снова.", reply_markup=get_main_menu())
        await state.clear()

@dp.message(SupportStates.waiting_for_text_with_photo)
async def handle_ticket_text_with_photo(message: types.Message, state: FSMContext):
    """Обработка текста для фото"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    await process_ticket(message, state, message.text)

# Обработка текстовых обращений
@dp.message(SupportStates.waiting_for_appeal, F.text)
@dp.message(SupportStates.waiting_for_complaint, F.text)
@dp.message(SupportStates.waiting_for_suggestion, F.text)
async def handle_ticket_text(message: types.Message, state: FSMContext):
    """Обработка текста в обращениях"""
    if message.chat.type != ChatType.PRIVATE:
        return
    
    await process_ticket(message, state, message.text)

async def process_ticket(message: types.Message, state: FSMContext, text: str):
    """Обработка обращения"""
    try:
        data = await state.get_data()
        ticket_type = data.get('ticket_type', 'Обращение')
        photo_id = data.get('photo_id')
        user = message.from_user
        
        if not text.strip():
            await message.answer("❌ Текст не может быть пустым!", reply_markup=get_main_menu())
            await state.clear()
            return
        
        # Сохраняем обращение
        ticket_id = add_support_ticket(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            ticket_type=ticket_type,
            message=text,
            photo_id=photo_id
        )
        
        # Клавиатура для модераторов
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Рассмотрено", callback_data=f"resolve_{ticket_id}"),
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"respond_{ticket_id}")
            ]
        ])
        
        # Формируем сообщение для модераторов
        mod_text = f"🆕 *Обращение #{ticket_id}*\n"
        mod_text += f"📋 Тип: {ticket_type}\n"
        mod_text += f"👤 Пользователь: {user.first_name or ''}"
        if user.last_name:
            mod_text += f" {user.last_name}"
        mod_text += f"\n🆔 ID: `{user.id}`\n"
        if user.username:
            mod_text += f"🔗 @{user.username}\n"
        mod_text += f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        mod_text += f"📝 *Сообщение:*\n{text}"
        
        # Отправляем в чат поддержки
        try:
            if photo_id:
                await bot.send_photo(
                    chat_id=SUPPORT_CHAT_ID,
                    photo=photo_id,
                    caption=mod_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    text=mod_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Ошибка отправки в поддержку: {e}")
        
        # Подтверждение пользователю
        await message.answer(
            f"✅ Ваше обращение принято!\n"
            f"📋 ID: #{ticket_id}\n"
            f"⏳ Модераторы рассмотрят его в ближайшее время.\n"
            f"📨 Вы получите уведомление о результате.",
            reply_markup=get_main_menu()
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка обработки обращения: {e}")
        await message.answer("❌ Ошибка при отправке. Попробуйте позже.", reply_markup=get_main_menu())
        await state.clear()

# ---------- CALLBACK ОБРАБОТЧИКИ ----------
@dp.callback_query(F.data.startswith("resolve_"))
async def cb_resolve(callback: types.CallbackQuery):
    """Рассмотрение обращения"""
    try:
        ticket_id = int(callback.data.split("_")[1])
        ticket = get_ticket(ticket_id)
        
        if not ticket:
            await callback.answer("❌ Обращение не найдено")
            return
        
        # Обновляем статус
        update_ticket(ticket_id, callback.from_user.id, "resolved")
        
        # Уведомляем пользователя
        user_id = ticket[1]
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Ваше обращение #{ticket_id} рассмотрено модератором."
            )
        except:
            pass
        
        # Обновляем сообщение
        try:
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n✅ *Рассмотрено модератором*",
                parse_mode="Markdown",
                reply_markup=None
            )
        except:
            try:
                await callback.message.edit_text(
                    text=callback.message.text + "\n\n✅ *Рассмотрено модератором*",
                    parse_mode="Markdown",
                    reply_markup=None
                )
            except:
                pass
        
        await callback.answer("✅ Обращение отмечено как рассмотренное")
        
    except Exception as e:
        logger.error(f"Ошибка resolve: {e}")
        await callback.answer("❌ Ошибка")

# ==================== HTTP СЕРВЕР ====================
async def health_check_handler(request):
    """Проверка здоровья для Render"""
    return web.Response(text="OK")

# ==================== ЗАПУСК БОТА ====================
async def main():
    """Основная функция запуска"""
    logger.info("=" * 50)
    logger.info("🤖 ЗАПУСК ТЕЛЕГРАМ БОТА")
    logger.info("=" * 50)
    logger.info(f"Владелец: {BOT_OWNER_ID}")
    logger.info(f"Разрешенный чат: {ALLOWED_CHAT_ID}")
    logger.info(f"Чат поддержки: {SUPPORT_CHAT_ID}")
    logger.info("=" * 50)
    
    try:
        # Запускаем HTTP сервер для Render
        app = web.Application()
        app.router.add_get('/health', health_check_handler)
        app.router.add_get('/', health_check_handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 HTTP сервер запущен на порту {PORT}")
        
        # Очищаем вебхук (на всякий случай)
        await bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(1)
        
        # Запускаем бота
        logger.info("🚀 Запуск поллинга бота...")
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
    finally:
        logger.info("🛑 Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())
