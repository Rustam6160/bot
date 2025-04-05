from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
import asyncio
import logging
from datetime import datetime, timedelta
import re
import os
import aiosqlite
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, FloodWaitError, PhoneCodeInvalidError
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator, Channel
from telethon import types


from proxy_config import proxy, proxy2

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_CAPTION_LENGTH = 1024
MAX_TEXT_LENGTH = 4096

class BotRunner:
    def __init__(self, config):
        self.config = config
        self.user_states = {}
        self.phone_codes = {}
        self.session_folder = os.path.join("user_sessions", config['bot_name'])
        os.makedirs(self.session_folder, exist_ok=True)

        self.client = TelegramClient(
            os.path.join(self.session_folder, 'bot_session'),
            config['api_id'],
            config['api_hash'],
            proxy=config['proxy']
        )
        self.client.parse_mode = 'html'
        self.OWNER_ID = config['owner_id']
        self.API_ID = config['api_id']
        self.API_HASH = config['api_hash']
        self.BOT_TOKEN = config['bot_token']

    def get_session_path(self, user_id):
        return os.path.join(self.session_folder, f"user_{user_id}.session")

    async def init_db(self):
        conn = await aiosqlite.connect(self.config['db_file'])
        try:
            await conn.executescript('''
                CREATE TABLE IF NOT EXISTS mailings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    group_names TEXT,
                    group_ids TEXT,
                    message TEXT,
                    photo_path TEXT,
                    interval INTEGER
                );

                -- Изменённая таблица для хранения времени в часах и минутах
                CREATE TABLE IF NOT EXISTS mailing_times (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mailing_id INTEGER,
                    hour INTEGER,
                    minute INTEGER,
                    FOREIGN KEY(mailing_id) REFERENCES mailings(id)
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registration_date TEXT,
                    is_active INTEGER DEFAULT 1
                );
            ''')
            await conn.commit()
        finally:
            await conn.close()

    async def save_user(self, user_id, username, first_name, last_name):
        conn = await aiosqlite.connect(self.config['db_file'])
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, registration_date, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, first_name, last_name, datetime.now().strftime('%Y-%m-%d %H:%M'), 1))
            await conn.commit()
        finally:
            await conn.close()

    async def run(self):
        await self.init_db()
        await self.client.start(bot_token=self.config['bot_token'])

        self.client.add_event_handler(self.start_handler, events.NewMessage(pattern='/start'))
        self.client.add_event_handler(self.callback_handler, events.CallbackQuery())
        self.client.add_event_handler(self.handle_response, events.NewMessage())

        self.client.add_event_handler(self.back_to_mailing_list, events.CallbackQuery(pattern=b"back_to_mailing_list"))
        self.client.add_event_handler(self.show_mailing_details, events.CallbackQuery(pattern=r"show_mailing_(\d+)"))
        self.client.add_event_handler(self.delete_mailing_handler,
                                      events.CallbackQuery(pattern=r"delete_mailing_(\d+)"))

        # Запускаем фоновую задачу по проверке незавершённых рассылок
        asyncio.create_task(self.process_pending_mailings())

        logger.info(f"Бот {self.config['bot_name']} запущен")
        await self.client.run_until_disconnected()

        # Все обработчики должны быть методами класса
    async def start_handler(self, event):
        user_id = event.sender_id
        logger.info(f"{self.config['bot_name']}: User {user_id} started")

        current_time = datetime.now()
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
            user = await cursor.fetchone()
            if user and user[0] == 0:
                await event.respond("⛔ Ваш доступ заблокирован. Обратитесь к администратору @JerdeshMoskva_admin")
                return
        finally:
            await conn.close()

        if user_id in self.user_states and 'client' in self.user_states[user_id]:
            client = self.user_states[user_id]['client']
            if await client.is_user_authorized():
                buttons = [
                    [Button.inline("Создать рассылку", b"create_mailing")],
                    [Button.inline("Список рассылок", b"mailing_list")]
                ]
                if user_id == self.config['owner_id']:
                    buttons.append([Button.inline("Список пользователей", b"user_list")])
                await event.respond("Вы уже авторизованы! Выберите действие:", buttons=buttons)
                return

        client = await self.load_user_session(user_id)
        if client:
            self.user_states[user_id] = {'stage': 'authorized', 'client': client}
            if user_id == self.config['owner_id']:
                buttons = [
                    [Button.inline("Создать рассылку", b"create_mailing")],
                    [Button.inline("Список рассылок", b"mailing_list")],
                    [Button.inline("Список пользователей", b"user_list")]
                ]
            else:
                conn = await self.get_db_connection()
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

        self.user_states[user_id] = {'stage': 'start'}
        logger.info(f"User {user_id} started the bot.")
        conn = None
        try:
            conn = await self.get_db_connection()
            cursor = await conn.cursor()
            await cursor.execute("SELECT id, is_active FROM users WHERE user_id = ?", (user_id,))
            user = await cursor.fetchone()
            if user:
                user_db_id, is_active = user
                if not is_active:
                    await event.respond("⛔ Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору @JerdeshMoskva_admin, затем снова нажмите /start")
                    return
                else:
                    client = await self.load_user_session(user_id)
                    if client:
                        self.user_states[user_id]['stage'] = 'authorized'
                        self.user_states[user_id]['client'] = client
                        buttons = [
                            [Button.inline("Создать рассылку", b"create_mailing")],
                            [Button.inline("Список рассылок", b"mailing_list")]
                        ]
                        if user_id == self.config['owner_id']:
                            buttons.append([Button.inline("Список пользователей", b"user_list")])
                        await event.respond("Вы уже авторизованы! Выберите действие:", buttons=buttons)
                        return
                    else:
                        await event.respond("Привет! Для использования бота введите свой номер телефона в формате +XXXXXXXXXXX.")
                        self.user_states[user_id]['stage'] = 'waiting_phone'
            else:
                await event.respond("Привет! Для использования бота введите свой номер телефона в формате +XXXXXXXXXXX.")
                self.user_states[user_id]['stage'] = 'waiting_phone'
        except Exception as e:
            logger.error(f"Ошибка при обработке команды /start для пользователя {user_id}: {e}")
            await event.respond("⚠️ Произошла ошибка. Пожалуйста, попробуйте снова.")
        finally:
            if conn:
                await conn.close()

    async def callback_handler(self, event):
        user_id = event.sender_id
        if user_id not in self.user_states:
            await event.answer("Сначала введите /start")
            return

        state = self.user_states[user_id]

        if event.data == b"cancel_user_selection":
            state['stage'] = 'authorized'
            await event.edit("Выбор пользователей отменён.", buttons=[
                [Button.inline("Создать рассылку", b"create_mailing")],
                [Button.inline("Список рассылок", b"mailing_list")],
                [Button.inline("Список пользователей", b"user_list")]
            ])
            return

        elif event.data == b"create_mailing":
            if 'client' not in state:
                await event.answer("Сначала авторизуйтесь.")
                return
            state['stage'] = 'choosing_group_type'
            await event.edit("Выберите тип групп:", buttons=[
                [Button.inline("Где я админ", b"admin_groups")],
                [Button.inline("Где я не админ", b"non_admin_groups")],
                [Button.inline("Назад", b"back")]
            ])
            return

        # Обработка кнопки "Где я админ"
        elif event.data == b"admin_groups":
            if 'client' not in state:
                await event.answer("Сначала авторизуйтесь.")
                return

            client = state['client']
            groups_admin = []

            async with client:
                async for dialog in client.iter_dialogs(limit=1000):
                    if isinstance(dialog.entity, Channel) and dialog.entity.megagroup:
                        try:
                            participant = await client(GetParticipantRequest(dialog.entity, user_id))
                            if isinstance(participant.participant,
                                          (ChannelParticipantAdmin, ChannelParticipantCreator)):
                                groups_admin.append(dialog)
                        except Exception as e:
                            logger.error(f"Ошибка при проверке прав администратора: {e}")

            # Очищаем предыдущий тип групп
            if 'non_admin_groups' in state:
                del state['non_admin_groups']

            # Автоматически выбираем все группы
            state['admin_groups'] = groups_admin
            state['selected'] = [g.id for g in groups_admin]  # Сохраняем ID выбранных групп
            state['stage'] = 'choosing_groups'

            await self.show_group_selection(event, state)

        elif event.data == b"non_admin_groups":
            if 'client' not in state:
                await event.answer("Сначала авторизуйтесь.")
                return

            client = state['client']
            groups_non_admin = []

            async with client:
                async for dialog in client.iter_dialogs(limit=1000):
                    if isinstance(dialog.entity, Channel) and dialog.entity.megagroup:
                        try:
                            participant = await client(GetParticipantRequest(dialog.entity, user_id))
                            if not isinstance(participant.participant,
                                              (ChannelParticipantAdmin, ChannelParticipantCreator)):
                                groups_non_admin.append(dialog)
                        except Exception as e:
                            groups_non_admin.append(dialog)
                            logger.error(f"Ошибка при проверке прав администратора: {e}")

            # Очищаем предыдущий тип групп
            if 'admin_groups' in state:
                del state['admin_groups']

            # Автоматически выбираем все группы
            state['non_admin_groups'] = groups_non_admin
            state['selected'] = [g.id for g in groups_non_admin]  # Сохраняем ID выбранных групп
            state['stage'] = 'choosing_groups'

            await self.show_group_selection(event, state)

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
            await self.show_mailing_list(event, user_id)

        elif event.data == b"user_list":
            if user_id != self.config['owner_id']:
                await event.answer("Эта функция доступна только владельцу бота.")
                return
            # Загружаем список пользователей
            users = await self.fetch_users()
            if not users:
                await event.respond("Список пользователей пуст.")
            else:
                # Сохраняем список пользователей в состояние
                state['users'] = users
                state['selected_users'] = []
                state['stage'] = 'authorized'
                await self.show_user_selection(event, state)

        elif event.data == b"confirm_mailing":
            if 'selected_times' not in state or not state['selected_times']:
                await event.answer("Выберите хотя бы одно время!")
                return
            selected_times = state['selected_times']
            text = state['text']
            selected_groups = state.get('selected_groups', [])
            media = state.get('media', None)
            mailing_name = state.get('mailing_name', f"Рассылка {datetime.now().strftime('%Y%m%d%H%M%S')}")
            # Сохраняем рассылку в БД (сохраняются group_names и group_ids)
            mailing_id = await self.save_mailing(
                user_id,
                mailing_name,
                selected_groups,
                text,
                media['path'] if media else None,
                selected_times,
                state.get('interval', 30)  # Добавляем интервал из состояния
            )
            buttons = [
                [Button.inline("Создать рассылку", b"create_mailing")],
                [Button.inline("Список рассылок", b"mailing_list")]
            ]
            if user_id == self.config['owner_id']:
                buttons.append([Button.inline("Список пользователей", b"user_list")])
            logger.info(f"Состояние пользователя {user_id}: {state}")
            await event.respond("Рассылка успешно запланирована! Она будет отправлена в указанные времена.",
                                buttons=buttons)
            # Здесь больше не производится непосредственная отправка –
            # отправка будет выполнена фоновым процессом process_pending_mailings().
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
            await self.show_user_selection(event, state)

        elif event.data == b"ban_selected_users":
            # Блокировка выбранных пользователей
            selected_users = state.get('selected_users', [])
            if not selected_users:
                await event.answer("Выберите хотя бы одного пользователя!")
                return
            for user_db_id in selected_users:
                # Получаем user_id (Telegram ID) по user_db_id
                conn = await self.get_db_connection()
                try:
                    cursor = await conn.cursor()
                    await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
                    user = await cursor.fetchone()
                    if user:
                        user_id = user[0]
                        await self.ban_user(user_id)  # Блокируем по user_id
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
                conn = await self.get_db_connection()
                try:
                    cursor = await conn.cursor()
                    await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
                    user = await cursor.fetchone()
                    if user:
                        user_id = user[0]
                        await self.unban_user(user_id)  # Разблокируем по user_id
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
            state['interval'] = interval  # Сохраняем интервал в состоянии
            state['selected_times'] = []
            await self.show_time_selection(event, state)
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

            await self.show_time_selection(event, state)
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
            await event.respond("Введите интервал в минутах (например, 45). Времена меньше 15 минут будут обрезаный изза огроничений телеграм на количество кнопок:")
            return

        # Обработка нажатия на кнопку выбора группы
        elif event.data.startswith(b"select_"):
            group_id = int(event.data.decode().replace("select_", ""))
            selected = state.get('selected', [])
            if group_id in selected:
                selected.remove(group_id)
            else:
                selected.append(group_id)
            state['selected'] = selected
            await self.show_group_selection(event, state)

        elif event.data == b"confirm_selection":
            selected_ids = state.get("selected", [])
            if 'admin_groups' in state:
                all_groups = state['admin_groups']
            elif 'non_admin_groups' in state:
                all_groups = state['non_admin_groups']
            else:
                await event.answer("Ошибка: группы не загружены")
                return
            selected_groups = [g for g in all_groups if g.id in selected_ids]
            if not selected_groups:
                await event.answer("Выберите хотя бы одну группу!")
                return
            state['selected_groups'] = selected_groups
            state['stage'] = 'entering_mailing_title'
            await event.respond("Введите название рассылки (максимум 10 символов):")

    # Папка для хранения сессий пользователей
    SESSION_FOLDER = "user_sessions"
    if not os.path.exists(SESSION_FOLDER):
        os.makedirs(SESSION_FOLDER)

    # Подключение к базе данных
    DB_FILE = "mailing.db"

    async def get_db_connection(self):
        """Возвращает асинхронное соединение с базой данных."""
        return await aiosqlite.connect(self.config['db_file'])

    async def send_with_retry(self, client, group, text, media, max_attempts=3):
        group_name = getattr(group, 'title', f'ID {group.id}')

        for attempt in range(max_attempts):
            try:
                if media:
                    if media['type'] == 'photo':
                        if len(text) <= MAX_CAPTION_LENGTH:
                            await client.send_file(group.id, media['path'], caption=text)
                        else:
                            caption = text[:MAX_CAPTION_LENGTH]
                            await client.send_file(group.id, media['path'], caption=caption)
                            remaining_text = text[MAX_CAPTION_LENGTH:]
                            for chunk in self.split_text(remaining_text):
                                await client.send_message(group.id, chunk)
                    elif media['type'] == 'video':
                        if len(text) <= MAX_CAPTION_LENGTH:
                            await client.send_file(group.id, media['path'], caption=text, supports_streaming=True)
                        else:
                            caption = text[:MAX_CAPTION_LENGTH]
                            await client.send_file(group.id, media['path'], caption=caption, supports_streaming=True)
                            remaining_text = text[MAX_CAPTION_LENGTH:]
                            for chunk in self.split_text(remaining_text):
                                await client.send_message(group.id, chunk)
                else:
                    if len(text) <= MAX_TEXT_LENGTH:
                        await client.send_message(group.id, text)
                    else:
                        for chunk in self.split_text(text):
                            await client.send_message(group.id, chunk)

                logger.info(f"Сообщение успешно отправлено в группу {group_name} (ID: {group.id})")
                return True

            except Exception as e:
                logger.error(f"Ошибка при отправке в группу {group_name} (попытка {attempt + 1}): {e}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(5)
                else:
                    logger.error(f"Не удалось отправить сообщение в группу {group_name} после {max_attempts} попыток")
                    return False

    async def is_owner_in_db(self):
        """Проверяет, есть ли владелец в базе данных."""
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id FROM users WHERE user_id = ?", (self.config['owner_id'],))
            owner = await cursor.fetchone()
            return owner is not None
        finally:
            await conn.close()

    async def load_user_session(self, user_id):
        session_path = self.get_session_path(user_id)
        if os.path.exists(session_path):
            client = TelegramClient(
                session_path,
                self.config['api_id'],
                self.config['api_hash'],
                proxy=self.config['proxy']
            )
            await client.connect()
            if await client.is_user_authorized():
                return client
            await client.disconnect()
        return None

    async def ban_user(self, user_id):
        conn = await self.get_db_connection()
        try:
            await conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
            await conn.commit()
            logger.info(f"Пользователь с ID {user_id} заблокирован.")
        finally:
            await conn.close()

    async def unban_user(self, user_id):
        conn = await self.get_db_connection()
        try:
            await conn.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
            await conn.commit()
            logger.info(f"Пользователь с ID {user_id} разблокирован.")
        finally:
            await conn.close()

    async def save_mailing(self, user_id, mailing_name, groups, message, photo_path, selected_times, interval):
        conn = await self.get_db_connection()
        try:
            group_names = []
            group_ids = []
            for group in groups:
                if hasattr(group.entity, 'title'):
                    group_names.append(group.entity.title)
                else:
                    group_names.append(str(group.id))
                group_ids.append(str(group.id))
            group_names_str = ', '.join(group_names)
            group_ids_str = ','.join(group_ids)

            cursor = await conn.cursor()
            await cursor.execute(
                """INSERT INTO mailings 
                (user_id, name, group_names, group_ids, message, photo_path, interval) 
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, mailing_name, group_names_str, group_ids_str, message, photo_path, interval)
            )
            mailing_id = cursor.lastrowid

            for hour, minute in selected_times:
                await cursor.execute(
                    "INSERT INTO mailing_times (mailing_id, hour, minute) VALUES (?, ?, ?)",
                    (mailing_id, hour, minute)
                )
            await conn.commit()
            return mailing_id
        finally:
            await conn.close()

    async def fetch_mailings(self, user_id):
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id, group_names, message, photo_path FROM mailings WHERE user_id = ?", (user_id,))
            mailings = await cursor.fetchall()
            for mailing in mailings:
                mailing_id = mailing[0]
                await cursor.execute("SELECT send_time FROM mailing_times WHERE mailing_id = ?", (mailing_id,))
                times = await cursor.fetchall()
                mailing += (times,)
            return mailings
        finally:
            await conn.close()

    async def show_mailing_list(self, event, user_id):
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id, name, group_names, message, photo_path FROM mailings WHERE user_id = ?",
                                 (user_id,))
            mailings = await cursor.fetchall()
            buttons = []  # Инициализируем список кнопок
            for mailing in mailings:
                mailing_id = mailing[0]
                await cursor.execute(
                    "SELECT hour, minute FROM mailing_times WHERE mailing_id = ? LIMIT 1",
                    (mailing_id,))
                first_send_time = await cursor.fetchone()
                if first_send_time:
                    hour, minute = first_send_time
                    first_send_time_dt = datetime.now().replace(hour=hour, minute=minute, second=0)
                    if first_send_time_dt < datetime.now():
                        first_send_time_dt += timedelta(days=1)
                    if (datetime.now() - first_send_time_dt).days > 30:
                        photo_path = mailing[4]
                        if photo_path and os.path.exists(photo_path):
                            os.remove(photo_path)
                            logger.info(f"Файл {photo_path} удалён.")
                        await self.delete_mailing(mailing_id, user_id)
                        logger.info(f"Рассылка {mailing_id} удалена (старше месяца).")
                        continue
                display = mailing[1] if mailing[1] and mailing[1].strip() else f"Рассылка {mailing_id}"
                buttons.append([Button.inline(display, f"show_mailing_{mailing_id}")])
            if not buttons:
                buttons_empty = [[Button.inline("Назад", b"back")]]
                await event.respond("История рассылок пуста.", buttons=buttons_empty)
                return
            buttons.append([Button.inline("Назад", b"back")])
            await event.respond("Выберите рассылку для просмотра:", buttons=buttons)
        finally:
            await conn.close()

    async def delete_mailing(self, mailing_id, user_id):
        conn = await self.get_db_connection()
        try:
            await conn.execute("DELETE FROM mailings WHERE id = ? AND user_id = ?", (mailing_id, user_id))
            await conn.commit()
        finally:
            await conn.close()

    async def back_to_mailing_list(self, event):
        user_id = event.sender_id
        await self.show_mailing_list(event, user_id)

    async def show_mailing_details(self, event):
        mailing_id = int(event.pattern_match.group(1))
        user_id = event.sender_id
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("""
                SELECT m.name, m.group_names, m.message, m.photo_path, m.interval,
                       GROUP_CONCAT(mt.hour || ':' || mt.minute, ', ') 
                FROM mailings m
                LEFT JOIN mailing_times mt ON m.id = mt.mailing_id
                WHERE m.id = ? AND m.user_id = ?
                GROUP BY m.id
            """, (mailing_id, user_id))

            mailing = await cursor.fetchone()
            if not mailing:
                await event.answer("Рассылка не найдена.")
                return

            name, groups, message, photo_path, interval, times = mailing
            response = (
                f"📌 Название: {name}\n"
                f"👥 Группы: {groups}\n"
                f"⏱ Интервал: {interval} мин\n"
                f"⏰ Времена отправки: {times}\n"
                f"📝 Сообщение: {message}"  # Обрезаем сообщение
            )

            try:
                if photo_path:
                    # Отправляем фото с обрезанной подписью
                    await event.client.send_file(
                        event.chat_id,
                        photo_path,
                        caption=response[:1024],  # Ограничение Telegram
                        parse_mode='html'
                    )
                    # Отправляем остаток сообщения текстом
                    if len(response) > 1024:
                        await event.respond(response[1024:])
                else:
                    # Разбиваем длинное сообщение на части
                    parts = [response[i:i + 4096] for i in range(0, len(response), 4096)]
                    for part in parts:
                        await event.respond(part)
            except Exception as e:
                logger.error(f"Ошибка отправки деталей рассылки: {str(e)}")
                await event.respond("⚠️ Ошибка при отображении рассылки")

            await event.respond("Выберите действие:", buttons=[
                [Button.inline("Удалить рассылку", f"delete_mailing_{mailing_id}")],
                [Button.inline("Назад", b"back_to_mailing_list")]
            ])

        finally:
            await conn.close()

    async def delete_mailing_handler(self, event):
        mailing_id = int(event.pattern_match.group(1))
        user_id = event.sender_id
        await self.delete_mailing(mailing_id, user_id)
        await event.respond(f"Рассылка {mailing_id} удалена.")
        await self.show_mailing_list(event, user_id)

    async def fetch_users(self):
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id, user_id, username, first_name, last_name, is_active FROM users")
            users = await cursor.fetchall()
            return users
        finally:
            await conn.close()

    async def save_user(self, user_id, username, first_name, last_name):
        """Сохраняет информацию о пользователе в базу данных."""
        conn = await self.get_db_connection()
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, registration_date, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, first_name, last_name, datetime.now().strftime('%Y-%m-%d %H:%M'), 1))
            await conn.commit()
        finally:
            await conn.close()

    async def delete_user(self, user_db_id):
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            logger.info(f"Попытка удалить пользователя с ID: {user_db_id}")
            await cursor.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
            user = await cursor.fetchone()
            if user:
                user_id = user[0]
                await cursor.execute("DELETE FROM users WHERE id = ?", (user_db_id,))
                await conn.commit()
                logger.info(f"Пользователь с ID {user_db_id} удалён из базы данных.")
                self.delete_user_session(user_id)
                if user_id in self.user_states:
                    del self.user_states[user_id]
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

    async def user_exists(self, username):
        normalized_username = username.strip().lower()
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT username FROM users")
            users = await cursor.fetchall()
            for user in users:
                if user[0].strip().lower() == normalized_username:
                    return True
            return False
        finally:
            await conn.close()

    async def show_user_selection(self, event, state):
        users = state['users']
        selected_users = state.get('selected_users', [])
        buttons = []
        for user in users:
            user_db_id = user[0]
            user_id = user[1]
            username = user[2] if user[2] else "Без username"
            first_name = user[3] if user[3] else ""
            last_name = user[4] if user[4] else ""
            is_active = user[5]
            display_name = f"{user_db_id}: {username} ({first_name} {last_name})".strip()
            mark = "✅" if user_db_id in selected_users else "🔲"
            status = "🟢" if is_active else "🔴"
            buttons.append([Button.inline(f"{mark} {status} {display_name}", f"select_user_{user_db_id}")])
        buttons.append([
            Button.inline("Заблокировать", b"ban_selected_users"),
            Button.inline("Разблокировать", b"unban_selected_users"),
            Button.inline("Отмена", b"cancel_user_selection")
        ])
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit("Выберите пользователей для управления доступом:", buttons=buttons)
        else:
            await event.respond("Выберите пользователей для управления доступом:", buttons=buttons)

    def delete_user_session(self, user_id):
        session_path = self.get_session_path(user_id)
        if os.path.exists(session_path):
            os.remove(session_path)
            logger.info(f"Сессия пользователя {user_id} удалена.")
        else:
            logger.info(f"Файл сессии пользователя {user_id} не найден.")
    async def print_all_users(self):
        """Выводит всех пользователей из базы данных."""
        conn = await self.get_db_connection()
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



    MAX_CAPTION_LENGTH = 1024  # Лимит для подписи к медиа
    MAX_TEXT_LENGTH = 4096  # Лимит для обычного текстового сообщения

    @staticmethod
    def split_text(text, chunk_size=MAX_TEXT_LENGTH):
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    async def help_command(self, event):
        user_id = event.sender_id
        logger.info(f"User {user_id} requested help.")
        video_path = "help_video/IMG_7569.MOV"
        if not os.path.exists(video_path):
            await event.respond("Видео не найдено. Пожалуйста, свяжитесь с администратором.")
            logger.error(f"Video file not found: {video_path}")
            return
        try:
            await event.respond("Загрузка видео... (это может занять несколько минут)")
            await event.respond("Вот видео с инструкцией:", file=video_path)
            logger.info(f"Video sent to user {user_id}.")
        except Exception as e:
            await event.respond("Ошибка при отправке видео. Пожалуйста, попробуйте снова.")
            logger.error(f"Error sending video to user {user_id}: {e}")

    async def is_user_authorized(self, user_id):
        conn = await self.get_db_connection()
        try:
            cursor = await conn.cursor()
            await cursor.execute("SELECT id FROM users WHERE user_id = ?", (user_id,))
            user_exists = await cursor.fetchone()
            return user_exists is not None
        finally:
            await conn.close()

    async def show_time_selection(self, event, state):
        interval = state.get('interval', 30)
        selected_times = state.get('selected_times', [])

        # Генерируем временные слоты на основе интервала в течение 24 часов
        start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=1)
        current_time = start_time

        buttons = []
        time_slots = []

        # Автоматически выбираем все времена при первом открытии
        if not selected_times:
            while current_time < end_time:
                hour = current_time.hour
                minute = current_time.minute
                selected_times.append((hour, minute))
                current_time += timedelta(minutes=interval)
            state['selected_times'] = selected_times


        # Создаём кнопки с учётом выбранных времён
        current_time = start_time
        row = []
        while current_time < end_time:
            hour = current_time.hour
            minute = current_time.minute
            mark = "✅" if (hour, minute) in selected_times else "🕒"
            btn = Button.inline(f"{mark} {hour:02d}:{minute:02d}", f"select_hour_{hour}_{minute}")

            # Группируем по 4 кнопки в строку
            if len(row) == 3:
                buttons.append(row)
                row = []
            row.append(btn)

            current_time += timedelta(minutes=interval)

        # Добавляем последнюю неполную строку
        if row:
            buttons.append(row)

        control_buttons = [
            [Button.inline("✅ Подтвердить", b"confirm_mailing")],
            [Button.inline("🔙 Назад", b"back_to_interval")]
        ]

        MAX_BUTTONS = 98  # 100 - 2 управляющие кнопки

        # Проверяем общее количество кнопок
        total_buttons = sum(len(row) for row in buttons)

        if total_buttons + 2 > 100:
            # Удаляем лишние кнопки, оставляя место для управляющих
            new_buttons = []
            count = 0
            for row in buttons:
                if count + len(row) + 2 <= MAX_BUTTONS:
                    new_buttons.append(row)
                    count += len(row)
                else:
                    break

            # Добавляем индикатор обрезки
            new_buttons.append([Button.inline("...", b"noop")])
            buttons = new_buttons

        # Добавляем управляющие кнопки
        buttons.extend(control_buttons)

        # Формируем сообщение
        message = (
            "⏰ Все времена выбраны автоматически!\n"
            "Можете снять выбор с ненужных времен\n"
            f"Интервал: {interval} минут\n"
            f"Выбрано времен: {len(selected_times)}"
        )

        try:
            if isinstance(event, events.CallbackQuery.Event):
                await event.edit(message, buttons=buttons)
            else:
                await event.respond(message, buttons=buttons)
        except Exception as e:
            error_msg = (
                "⚠️ Слишком много вариантов времени. "
                "Увеличьте интервал или выберите меньшее количество времен вручную."
            )
            logger.error(f"Ошибка отображения кнопок: {str(e)}")
            await event.respond(error_msg)

    async def show_group_selection(self, event, state):
        if 'admin_groups' in state:
            all_groups = state['admin_groups']
            group_type = 'Админские группы'
        elif 'non_admin_groups' in state:
            all_groups = state['non_admin_groups']
            group_type = 'Неадминские группы'
        else:
            await event.answer("Ошибка: группы не загружены")
            return
        selected_ids = state.get('selected', [])
        buttons = []
        for group in all_groups:
            group_id = group.id
            group_name = getattr(group.entity, 'title', f"Группа {group_id}")[:20]
            mark = "✅" if group_id in selected_ids else "🔲"
            buttons.append([Button.inline(f"{mark} {group_name}", f"select_{group_id}")])
        buttons.append([Button.inline("Назад", b"back"),
                        Button.inline(f"Подтвердить ({len(selected_ids)} выбрано)", b"confirm_selection")])
        message = f"<b>{group_type}</b>\nВыбрано: {len(selected_ids)} из {len(all_groups)}"
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(message, parse_mode='HTML', buttons=buttons)
        else:
            await event.respond(message, parse_mode='HTML', buttons=buttons)

    async def process_pending_mailings(self):
        """Фоновая задача: каждую минуту проверяет текущее время и отправляет рассылки"""
        while True:
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute

            conn = await self.get_db_connection()
            try:
                # Получаем все активные рассылки с текущим временем
                cursor = await conn.cursor()
                await cursor.execute('''
                    SELECT m.id, m.user_id, m.group_ids, m.message, m.photo_path, m.interval 
                    FROM mailings m
                    JOIN mailing_times mt ON m.id = mt.mailing_id
                    WHERE mt.hour = ? AND mt.minute = ?
                ''', (current_hour, current_minute))
                mailings = await cursor.fetchall()

                for mailing in mailings:
                    mailing_id, user_id, group_ids_str, message, photo_path, interval = mailing
                    client = await self.load_user_session(user_id)

                    if not client:
                        continue

                    try:
                        group_ids = [int(x) for x in group_ids_str.split(",")]
                        groups = []
                        for gid in group_ids:
                            try:
                                entity = await client.get_entity(gid)
                                groups.append(entity)
                            except Exception as e:
                                logger.error(f"Ошибка получения группы {gid}: {e}")

                        media = {'type': 'photo', 'path': photo_path} if photo_path else None

                        async with client:
                            for group in groups:
                                await self.send_with_retry(client, group, message, media)

                        logger.info(f"Рассылка {mailing_id} отправлена в {current_hour:02d}:{current_minute:02d}")

                    except Exception as e:
                        logger.error(f"Ошибка при отправке рассылки {mailing_id}: {e}")

            finally:
                await conn.close()

            await asyncio.sleep(60 - datetime.now().second)  # Проверяем каждую минуту

    async def handle_response(self, event):
        user_id = event.sender_id
        if user_id not in self.user_states:
            logger.info(f"Ignoring message from user {user_id} (no state).")
            return
        state = self.user_states[user_id]
        logger.info(f"User {user_id} is at stage: {state['stage']}")
        if event.raw_text.startswith('/') and event.raw_text.strip().lower() != '/confirm':
            logger.info(f"Ignoring command from user {user_id}: {event.raw_text}")
            return
        if state['stage'] == 'waiting_phone':
            phone_number = event.raw_text.strip()
            if not re.match(r'^\+\d{11,12}$', phone_number):
                await event.respond("Ошибка! Введите номер телефона в формате +XXXXXXXXXXX.")
                return
            state['stage'] = 'waiting_code'
            logger.info(f"User {user_id} entered phone number: {phone_number}")
            session_path = self.get_session_path(user_id)
            client = None
            try:
                client = TelegramClient(
                    session_path,
                    self.config['api_id'],
                    self.config['api_hash'],
                    proxy=self.config['proxy']
                )
                await client.connect()
                if not client.is_connected():
                    await event.respond("Ошибка подключения к серверам Telegram.")
                    return
                self.phone_codes[user_id] = {
                    'client': client,
                    'phone_number': phone_number,
                    'phone_code_hash': None,
                    'current_code': ''
                }
                code_request = await client.send_code_request(phone_number)
                self.phone_codes[user_id]['phone_code_hash'] = code_request.phone_code_hash
                logger.info(f"Connected: {client.is_connected()}")
                logger.info(f"Authorized: {await client.is_user_authorized()}")
                await event.respond("✅ Код авторизации отправлен. Вводите цифры по одной.")
            except FloodWaitError as e:
                wait_time = e.seconds
                error_msg = f"⚠️ Слишком много попыток. Попробуйте через {wait_time // 60} минут."
                logger.error(f"FloodWaitError: {error_msg}")
                await event.respond(error_msg)
                if client:
                    await client.disconnect()
                return
            except Exception as e:
                error_msg = f"Ошибка: {str(e)}"
                logger.error(f"Error sending code: {error_msg}")
                await event.respond("🚫 Ошибка при отправке кода. Попробуйте позже.")
                if client and client.is_connected():
                    await client.disconnect()
                return
        elif state['stage'] == 'waiting_code':
            digit = event.raw_text.strip()
            if not digit.isdigit() or len(digit) != 1:
                await event.respond("Ошибка! Введите одну цифру.")
                return
            if user_id not in self.phone_codes:
                await event.respond("🚫 Сессия устарела. Начните заново.")
                state['stage'] = 'waiting_phone'
                return
            phone_data = self.phone_codes[user_id]
            phone_data['current_code'] += digit
            current_code = phone_data['current_code']
            if len(current_code) < 5:
                await event.respond(f"Введено цифр: {len(current_code)}. Введите следующую цифру.")
                return
            client = phone_data.get('client')
            if not client or not isinstance(client, TelegramClient):
                await event.respond("🚫 Ошибка клиента. Начните заново.")
                state['stage'] = 'waiting_phone'
                del self.phone_codes[user_id]
                return
            try:
                if not client.is_connected():
                    await client.connect(timeout=10)
                result = await client.sign_in(
                    phone=phone_data['phone_number'],
                    code=current_code,
                    phone_code_hash=phone_data['phone_code_hash']
                )
                if isinstance(result, types.User):
                    state['client'] = client
                    state['stage'] = 'authorized'
                    client.session.save()
                    user_info = await client.get_me()
                    await self.save_user(
                        user_id,
                        user_info.username,
                        user_info.first_name,
                        user_info.last_name
                    )
                    if user_id != self.config['owner_id']:
                        await self.ban_user(user_id)
                        await event.respond("Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору для получения доступа. @JerdeshMoskva_admin затем снова нажмите /start")
                    else:
                        await event.respond("✅ Авторизация успешна! Теперь вы можете использовать бота.")
                        await event.respond("Выберите действие:", buttons=[
                            [Button.inline("Создать рассылку", b"create_mailing")],
                            [Button.inline("Список рассылок", b"mailing_list")]
                        ])
                    del self.phone_codes[user_id]
            except SessionPasswordNeededError:
                state['stage'] = 'waiting_password'
                state['client'] = client
                await event.respond("🔐 Введите пароль двухфакторной аутентификации:")
            except PhoneCodeInvalidError:
                await event.respond("❌ Неверный код. Попробуйте снова.")
                await client.disconnect()
                state['stage'] = 'waiting_phone'
                del self.phone_codes[user_id]
            except Exception as e:
                logger.error(f"Critical sign-in error: {str(e)}")
                await event.respond("⚠️ Критическая ошибка. Начните процесс заново.")
                if client.is_connected():
                    await client.disconnect()
                state['stage'] = 'waiting_phone'
                del self.phone_codes[user_id]
        elif state['stage'] == 'waiting_password':
            password = event.raw_text.strip()
            try:
                client = self.phone_codes[user_id]['client']
                await client.sign_in(password=password)
                state['client'] = client
                state['stage'] = 'authorized'
                user_info = await client.get_me()
                await self.save_user(user_id, user_info.username, user_info.first_name, user_info.last_name)
                conn = await self.get_db_connection()
                try:
                    cursor = await conn.cursor()
                    await cursor.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
                    user = await cursor.fetchone()
                    if user_id != self.config['owner_id']:
                        await self.ban_user(user_id)
                        await event.respond("Вы успешно авторизованы, но ваш доступ ограничен. Обратитесь к администратору для получения доступа. @JerdeshMoskva_admin затем снова нажмите /start")
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
            mailing_name = event.raw_text.strip()
            if len(mailing_name) > 10:
                await event.respond("❌ Название не может быть длиннее 10 символов. Введите снова:")
                return
            state['mailing_name'] = mailing_name[:10]  # Обрезаем до 10 символов
            state['stage'] = 'waiting_media'
            await event.respond("Отправьте фото или медиа для рассылки или введите 'пропустить'.")

        elif state['stage'] == 'waiting_media':
            if event.raw_text.lower() == 'пропустить':
                state['media'] = None
                state['stage'] = 'entering_text'
                await event.respond("Введите текст рассылки:")
                logger.info(f"User {user_id} skipped media. Moving to 'entering_text' stage.")
                return
            elif event.photo or event.video or event.document:
                try:
                    await event.respond("Обработка...")
                    if event.photo:
                        media_path = await event.download_media(file="media/")
                        state['media'] = {'type': 'photo', 'path': media_path}
                        logger.info(f"[DEBUG] Фото сохранено в: {media_path}")
                    elif event.video or (event.document and event.document.mime_type.startswith('video/')):
                        media_path = await event.download_media(file="media/")
                        state['media'] = {'type': 'video', 'path': media_path}
                        logger.info(f"[DEBUG] Видео сохранено в: {media_path}")
                    else:
                        await event.respond("Ошибка! Отправьте фото или видео.")
                        return
                    state['stage'] = 'entering_text'
                    logger.info(f"User {user_id} media processed. Moving to 'entering_text' stage.")
                except Exception as e:
                    logger.error(f"Ошибка при обработке медиа: {e}")
                    await event.respond("Ошибка! Не удалось обработать медиафайл. Попробуйте снова.")
            else:
                await event.respond("Ошибка! Отправьте фото, видео или введите 'пропустить'.")
        if state['stage'] == 'entering_text':
            state['text'] = event.raw_text
            state['stage'] = 'choosing_interval'
            await event.respond("Выберите интервал отправки (не меньше 15 минут):", buttons=[
                [Button.inline("15 минут", b"select_interval_15")],
                [Button.inline("20 минут", b"select_interval_20")],
                [Button.inline("30 минут", b"select_interval_30")],
                [Button.inline("1 час", b"select_interval_60")],
                [Button.inline("Другое время", b"custom_interval")]
            ])
            logger.info(f"User {user_id} entered text. Moving to 'choosing_interval' stage.")
        elif state['stage'] == 'waiting_custom_interval':
            try:
                interval = int(event.raw_text.strip())
                if interval <= 0:
                    await event.respond("Интервал должен быть положительным числом.")
                    return
                state['interval'] = interval
                state['selected_times'] = []
                await self.show_time_selection(event, state)
            except ValueError:
                await event.respond("Ошибка! Введите число (например, 45 и не меньше 15).")
            return
        elif state['stage'] == 'waiting_user_to_delete':
            if user_id != self.config['owner_id']:
                await event.respond("Эта функция доступна только владельцу бота.")
                return
            username_to_delete = event.raw_text.strip()
            logger.info(f"Введённое имя пользователя: '{username_to_delete}'")
            if await self.user_exists(username_to_delete):
                await self.delete_user(username_to_delete)
                await event.respond(f"Пользователь {username_to_delete} удалён.")
            else:
                await event.respond(f"Пользователь {username_to_delete} не найден.")
            state['stage'] = 'authorized'
        logger.info(f"Бот {self.config['bot_name']} запущен")
        await self.client.run_until_disconnected()


async def main():
    # Конфигурация для первого бота
    config1 = {
        'bot_name': 'Botkg',
        'api_id': 25188844,
        'api_hash': '7c8965cac5439d5f88c3ab6ac29f394b',
        'bot_token': '7526490262:AAFPGLhrcScaRxhPMsPWDUfCXKJdhtAWiuY',
        'proxy': proxy,
        'db_file': 'mailing1.db',
        'owner_id': 6351807167
    }

    # Конфигурация для второго бота
    config2 = {
        'bot_name': 'Botru',
        'api_id': 20541974,
        'api_hash': '9c41bf75f6d30195032966367eff1f66',
        'bot_token': '8188877991:AAHoRHbgoyl4wxvbnAXSJhEUo3jskzpDseY',
        'proxy': proxy2,
        'db_file': 'mailing2.db',
        'owner_id': 7111113380
    }

    # Запускаем ботов параллельно
    # bot1 = BotRunner(config1)
    bot2 = BotRunner(config2)

    await asyncio.gather(
        # bot1.run(),
        bot2.run()
    )


if __name__ == "__main__":
    asyncio.run(main())
