import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramNetworkError, TelegramAPIError
from typing import Callable, Dict, Any, Awaitable

from config import (
    TELEGRAM_BOT_TOKEN, PAYMENT_PROVIDER_TOKEN, MAX_REQUESTS_PER_USER, DEVELOPER_USER_IDS,
    SUBSCRIPTION_PRICE_1_MONTH, SUBSCRIPTION_PRICE_3_MONTHS, SUBSCRIPTION_PRICE_6_MONTHS, SUBSCRIPTION_PRICE_1_YEAR,
    EXTRA_REQUEST_PRICE, EXTRA_REQUESTS_PACK_10, EXTRA_REQUESTS_PACK_25, EXTRA_REQUESTS_PACK_50,
    REQUIRED_CHANNEL_USERNAME, REQUIRED_CHANNEL_URL,
    PAYMENT_SYSTEM, ROBOKASSA_MERCHANT_LOGIN, ROBOKASSA_PASSWORD1, ROBOKASSA_PASSWORD2,
    ROBOKASSA_IS_TEST, ROBOKASSA_TEST_PASSWORD1, ROBOKASSA_TEST_PASSWORD2,
    ROBOKASSA_SUCCESS_URL, ROBOKASSA_FAIL_URL, ROBOKASSA_RESULT_URL, WEBHOOK_PORT,
    ROBOKASSA_FISCAL_ENABLED, ROBOKASSA_TAX_RATE, CLCK_API_KEY, CLCK_ENABLED
)
from services.scenario_generator import ScenarioGenerator
from services.limits_manager import LimitsManager
from services.subscription_manager import SubscriptionManager
from services.rate_limiter import rate_limiter
from services.metrics import metrics_collector
from services.robokassa import RobokassaService
from services.url_shortener import URLShortener
from services.export_scenario import export_scenario_text, export_scenario_shooting_list, export_scenario_table
from services.scenario_templates import SCENARIO_TEMPLATES, get_template_info, get_template_prompt_modifier, get_all_templates
from services.priority_queue import PriorityQueue
from services.content_importer import ContentImporter
from database import Database, init_database, get_db_pool

_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

logging.getLogger('aiogram').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

# Оптимизация троттлинга для Telegram Bot API
# По умолчанию aiogram использует throttling ~0.75-0.9 секунды между запросами
# Telegram позволяет до 30 сообщений в секунду, но не более 1 сообщения в секунду на чат
# 
# В aiogram 3.x троттлинг контролируется автоматически через внутренние механизмы
# Прямое изменение через кастомную сессию сложно и может привести к ошибкам
# 
# Примечание: Троттлинг 0.9 секунды при отсутствии пользователей - это нормально,
# так как aiogram использует консервативные настройки для предотвращения rate limits.
# При появлении пользователей троттлинг может уменьшиться автоматически.

# Создаем бота со стандартными настройками
# В aiogram 3.x таймауты настраиваются автоматически
# Ошибки "Request timeout error" обрабатываются автоматически с повторными попытками
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scenario_generator = ScenarioGenerator()
content_importer = ContentImporter(scenario_generator)

# Инициализация платежной системы
robokassa_service = None
if PAYMENT_SYSTEM == "robokassa":
    # Выбираем пароли в зависимости от режима (тестовый или боевой)
    if ROBOKASSA_IS_TEST:
        # В тестовом режиме используем тестовые пароли, если они указаны, иначе боевые
        robokassa_password1 = ROBOKASSA_TEST_PASSWORD1 or ROBOKASSA_PASSWORD1
        robokassa_password2 = ROBOKASSA_TEST_PASSWORD2 or ROBOKASSA_PASSWORD2
    else:
        # В боевом режиме используем только боевые пароли
        robokassa_password1 = ROBOKASSA_PASSWORD1
        robokassa_password2 = ROBOKASSA_PASSWORD2
    
    # Проверяем, что пароли указаны
    if robokassa_password1 and robokassa_password2:
        robokassa_service = RobokassaService(
            merchant_login=ROBOKASSA_MERCHANT_LOGIN,
            password1=robokassa_password1,
            password2=robokassa_password2,
            is_test=ROBOKASSA_IS_TEST
        )
        logger.info(f"Robokassa сервис инициализирован: merchant_login={ROBOKASSA_MERCHANT_LOGIN}, test_mode={ROBOKASSA_IS_TEST}")
    else:
        if ROBOKASSA_IS_TEST:
            logger.warning(
                "Robokassa выбран в тестовом режиме, но тестовые пароли не настроены! "
                "Проверьте ROBOKASSA_TEST_PASSWORD1 и ROBOKASSA_TEST_PASSWORD2 в .env"
            )
        else:
            logger.warning(
                "Robokassa выбран в боевом режиме, но пароли не настроены! "
                "Проверьте ROBOKASSA_PASSWORD1 и ROBOKASSA_PASSWORD2 в .env"
            )

# Инициализация сервиса сокращения ссылок (clck.su)
_shortener_instance = None
if CLCK_ENABLED and CLCK_API_KEY:
    from services.url_shortener import URLShortener
    _shortener_instance = URLShortener(CLCK_API_KEY)
    logger.info("✅ Сервис сокращения ссылок (clck.su) инициализирован")
elif CLCK_ENABLED:
    logger.warning("CLCK_ENABLED=true, но CLCK_API_KEY не указан! Сокращение ссылок отключено.")
else:
    logger.info("Сокращение ссылок отключено (CLCK_ENABLED=false)")

import concurrent.futures
_scenario_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="scenario")

# Инициализация приоритетной очереди
_priority_queue = PriorityQueue(max_workers=3)

# ==================== MIDDLEWARE ====================

class RateLimitMiddleware:
    """Middleware для ограничения частоты запросов"""
    
    async def __call__(self, handler, event, data):
        user_id = None
        
        if isinstance(event, types.Message):
            user_id = event.from_user.id
            # Разработчики не ограничены rate limiting
            if user_id not in DEVELOPER_USER_IDS:
                allowed, error_msg = rate_limiter.check_message_rate(user_id)
                if not allowed:
                    await event.answer(error_msg, parse_mode="HTML")
                    return
        
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id
            # Разработчики не ограничены rate limiting
            if user_id not in DEVELOPER_USER_IDS:
                allowed, error_msg = rate_limiter.check_callback_rate(user_id)
                if not allowed:
                    await event.answer(error_msg, show_alert=True)
                    return
        
        return await handler(event, data)


class MetricsMiddleware:
    """Middleware для сбора метрик"""
    
    async def __call__(self, handler, event, data):
        start_time = time.time()
        success = False
        
        try:
            result = await handler(event, data)
            success = True
            
            # Записываем активность пользователя
            if isinstance(event, (types.Message, types.CallbackQuery)):
                user_id = event.from_user.id
                metrics_collector.record_user_activity(user_id)
                
                # Записываем использование команды
                if isinstance(event, types.Message) and event.text and event.text.startswith('/'):
                    command = event.text.split()[0].replace('/', '')
                    metrics_collector.record_command(command)
            
            return result
        finally:
            duration = time.time() - start_time
            metrics_collector.record_request(duration, success)

# Регистрируем middleware
dp.message.middleware(RateLimitMiddleware())
dp.callback_query.middleware(RateLimitMiddleware())
dp.message.middleware(MetricsMiddleware())
dp.callback_query.middleware(MetricsMiddleware())

_MAX_STORAGE_SIZE = 3000
def _cleanup_storage():
    try:
        if hasattr(storage, '_data'):
            if len(storage._data) > _MAX_STORAGE_SIZE:
                items = list(storage._data.items())
                to_remove = len(items) - _MAX_STORAGE_SIZE // 2
                for key, _ in list(items)[:to_remove]:
                    try:
                        del storage._data[key]
                    except:
                        pass
    except Exception:
        pass

_RE_BOLD_DOUBLE = re.compile(r'\*\*([^*]+?)\*\*', re.DOTALL)
_RE_BOLD_DOUBLE_SPACES = re.compile(r'\*\*\s*([^*]+?)\s*\*\*', re.DOTALL)
_RE_UNDERSCORE_DOUBLE = re.compile(r'__([^_]+?)__', re.DOTALL)
_RE_UNDERSCORE_DOUBLE_SPACES = re.compile(r'__\s*([^_]+?)\s*__', re.DOTALL)
_RE_ITALIC_STAR = re.compile(r'\*([^*\n]+?)\*')
_RE_ITALIC_STAR_SPACES = re.compile(r'\*\s*([^*\n]+?)\s*\*')
_RE_ITALIC_UNDERSCORE = re.compile(r'_([^_\n]+?)_')
_RE_ITALIC_UNDERSCORE_SPACES = re.compile(r'_\s*([^_\n]+?)\s*_')
_RE_LIST_MARKER = re.compile(r'^\s*[\*\-]\s+', re.MULTILINE)
_RE_UNDERSCORE_STANDALONE = re.compile(r'(?<!\w)_(?!\w)')
_RE_MULTIPLE_SPACES = re.compile(r' +')


def remove_markdown(text: str) -> str:
    if not text:
        return text
    
    for _ in range(10):
        if '**' not in text and '__' not in text:
            break
        text = _RE_BOLD_DOUBLE.sub(r'\1', text)
        text = _RE_UNDERSCORE_DOUBLE.sub(r'\1', text)
    
    for _ in range(10):
        if '*' not in text and '_' not in text:
            break
        text = _RE_ITALIC_STAR.sub(r'\1', text)
        text = _RE_ITALIC_UNDERSCORE.sub(r'\1', text)
    
    text = _RE_LIST_MARKER.sub('', text)
    
    for _ in range(3):
        text = text.replace("**", "").replace("*", "").replace("__", "")
    
    text = _RE_UNDERSCORE_STANDALONE.sub('', text)
    text = _RE_MULTIPLE_SPACES.sub(' ', text)
    
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    
    return text


_MAX_IN_MEMORY_USERS = 5000
active_users: set[int] = set()
registered_users: set[int] = set()

def _cleanup_user_sets():
    if len(active_users) > _MAX_IN_MEMORY_USERS:
        active_users.clear()
    if len(registered_users) > _MAX_IN_MEMORY_USERS:
        registered_users.clear()


class ScenarioStates(StatesGroup):
    waiting_for_niche = State()
    waiting_for_format = State()
    waiting_for_style = State()
    waiting_for_template = State()  # Новое состояние для выбора шаблона (Premium)
    waiting_for_template_name = State()  # Состояние для создания шаблона - название
    waiting_for_template_description = State()  # Состояние для создания шаблона - описание
    waiting_for_template_prompt = State()  # Состояние для создания шаблона - промпт
    waiting_for_tone = State()  # Новое состояние для Premium
    waiting_for_duration = State()  # Новое состояние для Premium
    waiting_for_platform = State()  # Новое состояние для Premium
    waiting_for_topic = State()
    waiting_for_additional_info = State()
    waiting_for_improvement = State()
    waiting_for_support_message = State() 
    waiting_broadcast_confirm = State()
    waiting_for_import_url = State()  # Состояние для импорта по ссылке
    waiting_for_import_text = State()  # Состояние для импорта текста
    waiting_for_import_niche = State()  # Состояние для выбора ниши после импорта


def get_main_keyboard():
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Создать сценарий")],
            [KeyboardButton(text="💎 Подписка"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="💬 Поддержка")]
        ],
        resize_keyboard=True
    )
    return keyboard


def get_subscribe_keyboard():
    """Клавиатура с кнопкой подписки на канал"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")]
        ]
    )
    return keyboard


async def check_channel_subscription(user_id: int) -> bool:
    """
    Проверить, подписан ли пользователь на канал
    
    ВАЖНО: Бот должен быть администратором канала для корректной проверки подписки!
    Добавьте бота как администратора в настройках канала @reelsAIcontent
    """
    try:
        member = await bot.get_chat_member(
            chat_id=f"@{REQUIRED_CHANNEL_USERNAME}",
            user_id=user_id
        )
        # Проверяем статус подписки
        # member.status может быть: 'member', 'administrator', 'creator', 'left', 'kicked', 'restricted'
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Ошибка при проверке подписки на канал для пользователя {user_id}: {e}")
        # В случае ошибки (например, бот не админ канала) разрешаем доступ
        # Но лучше, чтобы бот был админом канала для корректной проверки
        logger.warning("⚠️ Бот должен быть администратором канала для проверки подписки!")
        return False


def get_niche_keyboard():
    """Выбор ниши"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="😄 Юмор", callback_data="niche_юмор")],
            [InlineKeyboardButton(text="💡 Лайфхаки", callback_data="niche_лайфхаки")],
            [InlineKeyboardButton(text="🚀 Мотивация", callback_data="niche_мотивация")],
            [InlineKeyboardButton(text="📱 Обзоры", callback_data="niche_обзоры")],
            [InlineKeyboardButton(text="🎓 Образование", callback_data="niche_образование")],
            [InlineKeyboardButton(text="💼 Бизнес", callback_data="niche_бизнес")],
            [InlineKeyboardButton(text="👨‍💼 Эксперт", callback_data="niche_эксперт")],
            [InlineKeyboardButton(text="💪 Спорт", callback_data="niche_спорт")],
            [InlineKeyboardButton(text="🍔 Еда", callback_data="niche_еда")],
            [InlineKeyboardButton(text="✈️ Путешествия", callback_data="niche_путешествия")],
            [InlineKeyboardButton(text="💅 Красота", callback_data="niche_красота")],
            [InlineKeyboardButton(text="🎮 Игры", callback_data="niche_игры")],
            [InlineKeyboardButton(text="💬 Общее", callback_data="niche_общее")]
        ]
    )
    return keyboard


def get_format_keyboard():
    """Выбор формата"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ 15 секунд", callback_data="format_15 секунд")],
            [InlineKeyboardButton(text="⚡ 30 секунд", callback_data="format_30 секунд")],
            [InlineKeyboardButton(text="⚡ 60 секунд", callback_data="format_60 секунд")],
            [InlineKeyboardButton(text="📺 Longform", callback_data="format_longform")]
        ]
    )
    return keyboard


def get_style_keyboard():
    """Выбор стиля"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Динамичный", callback_data="style_динамичный")],
            [InlineKeyboardButton(text="😌 Спокойный", callback_data="style_спокойный")],
            [InlineKeyboardButton(text="🎭 Драматичный", callback_data="style_драматичный")],
            [InlineKeyboardButton(text="📚 Образовательный", callback_data="style_образовательный")],
            [InlineKeyboardButton(text="😄 Юмористический", callback_data="style_юмористический")],
            [InlineKeyboardButton(text="💫 Вдохновляющий", callback_data="style_вдохновляющий")],
            [InlineKeyboardButton(text="🎬 Кинематографичный", callback_data="style_кинематографичный")],
            [InlineKeyboardButton(text="💬 Разговорный", callback_data="style_разговорный")]
        ]
    )
    return keyboard


def get_tone_keyboard():
    """Выбор тона подачи (Premium)"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Официальный", callback_data="tone_официальный")],
            [InlineKeyboardButton(text="🤝 Неформальный", callback_data="tone_неформальный")],
            [InlineKeyboardButton(text="😊 Дружелюбный", callback_data="tone_дружелюбный")],
            [InlineKeyboardButton(text="💼 Профессиональный", callback_data="tone_профессиональный")],
            [InlineKeyboardButton(text="😄 Юмористический", callback_data="tone_юмористический")],
            [InlineKeyboardButton(text="🔥 Энергичный", callback_data="tone_энергичный")]
        ]
    )
    return keyboard


def get_duration_keyboard():
    """Выбор длительности (Premium)"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Короткий (15-30 сек)", callback_data="duration_короткий")],
            [InlineKeyboardButton(text="⏱️ Средний (30-60 сек)", callback_data="duration_средний")],
            [InlineKeyboardButton(text="📺 Длинный (60+ сек)", callback_data="duration_длинный")]
        ]
    )
    return keyboard


def get_platform_keyboard():
    """Выбор платформы (Premium)"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Reels", callback_data="platform_reels")],
            [InlineKeyboardButton(text="🎵 TikTok", callback_data="platform_tiktok")],
            [InlineKeyboardButton(text="▶️ Shorts", callback_data="platform_shorts")],
            [InlineKeyboardButton(text="🌐 Универсальный", callback_data="platform_универсальный")]
        ]
    )
    return keyboard


async def get_template_keyboard(user_id: int = None):
    """Выбор шаблона сценария (Premium)"""
    templates = get_all_templates()
    keyboard_buttons = []
    
    # Группируем кнопки по 2 в ряд для стандартных шаблонов
    template_items = list(templates.items())
    for i in range(0, len(template_items), 2):
        row = []
        for j in range(2):
            if i + j < len(template_items):
                template_id, template_data = template_items[i + j]
                name = template_data.get("name", template_id)
                row.append(InlineKeyboardButton(text=name, callback_data=f"template_{template_id}"))
        keyboard_buttons.append(row)
    
    # Добавляем пользовательские шаблоны, если они есть
    if user_id:
        user_templates = await Database.get_user_templates(user_id)
        if user_templates:
            keyboard_buttons.append([InlineKeyboardButton(text="─" * 20, callback_data="template_separator")])
            for template in user_templates[:6]:  # Максимум 6 пользовательских шаблонов
                name = template['name']
                if len(name) > 20:
                    name = name[:17] + "..."
                keyboard_buttons.append([
                    InlineKeyboardButton(text=f"⭐ {name}", callback_data=f"template_user_{template['id']}")
                ])
    
    # Добавляем кнопку "Без шаблона" и "Создать свой"
    keyboard_buttons.append([
        InlineKeyboardButton(text="❌ Без шаблона", callback_data="template_none"),
        InlineKeyboardButton(text="➕ Создать свой", callback_data="create_template")
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    return keyboard


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    active_users.add(user_id)
    registered_users.add(user_id)
    
    # Проверяем, является ли пользователь новым (первый запуск бота)
    # Важно: проверяем ДО регистрации, чтобы не засчитать повторный запуск как новый
    is_new_user = await Database.is_user_new(user_id)
    
    # Регистрируем пользователя (обновляем last_active)
    await Database.register_user(user_id)
    
    # Проверяем параметры команды
    param = None
    if message.text and len(message.text.split()) > 1:
        param = message.text.split()[1]
    
    # Обработка шаринг-ссылки на сценарий: /start view_TOKEN
    if param and param.startswith("view_"):
        share_token = param.replace("view_", "")
        
        try:
            scenario_data = await Database.get_scenario_by_share_token(share_token)
            
            if not scenario_data:
                await message.answer(
                    "❌ <b>Ссылка недействительна</b>\n\n"
                    "Возможно, ссылка истекла или была удалена.",
                    parse_mode="HTML"
                )
                # Показываем обычное приветствие
                await _send_welcome_message(message, False, False)
                return
            
            # Отправляем информацию о шаринг-ссылке
            owner_id = scenario_data.get('owner_id')
            created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data['created_at'] else "Дата неизвестна"
            
            info_text = (
                "🔗 <b>Сценарий от пользователя</b>\n\n"
                f"📋 <b>Ниша:</b> {scenario_data.get('niche') or 'Не указана'}\n"
                f"📝 <b>Формат:</b> {scenario_data.get('format_type') or 'Не указан'}\n"
                f"🎨 <b>Стиль:</b> {scenario_data.get('style') or 'Не указан'}\n"
            )
            
            if scenario_data.get('tone'):
                info_text += f"<b>Тон:</b> {scenario_data['tone']}\n"
            if scenario_data.get('duration'):
                info_text += f"<b>Длительность:</b> {scenario_data['duration']}\n"
            if scenario_data.get('platform'):
                platform_names = {
                    "reels": "Reels",
                    "tiktok": "TikTok",
                    "shorts": "Shorts",
                    "универсальный": "Универсальный"
                }
                platform_name = platform_names.get(scenario_data['platform'].lower(), scenario_data['platform'])
                info_text += f"<b>Платформа:</b> {platform_name}\n"
            if scenario_data.get('topic'):
                info_text += f"<b>Тема:</b> {scenario_data['topic']}\n"
            
            info_text += f"<b>Создан:</b> {created_at}\n\n"
            info_text += "=" * 30 + "\n\n"
            
            await message.answer(info_text, parse_mode="HTML")
            
            # Отправляем текст сценария
            scenario_text = scenario_data['scenario_text']
            if len(scenario_text) > 4000:
                chunks = [scenario_text[i:i+4000] for i in range(0, len(scenario_text), 4000)]
                for chunk in chunks:
                    await message.answer(chunk, parse_mode="HTML")
            else:
                await message.answer(scenario_text, parse_mode="HTML")
            
            # Предлагаем создать свой сценарий
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎬 Создать свой сценарий", callback_data="new_scenario")]
                ]
            )
            await message.answer(
                "💡 <b>Хочешь создать свой сценарий?</b>\n"
                "Используй /new или нажми кнопку ниже!",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return
            
        except Exception as e:
            logger.error(f"Ошибка при просмотре шаринг-ссылкой для пользователя {user_id}: {e}", exc_info=True)
            await message.answer(
                "❌ <b>Ошибка при загрузке сценария</b>\n\n"
                "Попробуй позже.",
                parse_mode="HTML"
            )
            # Показываем обычное приветствие
            await _send_welcome_message(message, False, is_new_user)
            return
    
    # Проверяем реферальный код (простой user_id)
    referral_code = param
    referrer_id = None
    
    referral_bonus_given = False
    
    # Реферальную связь регистрируем только для НОВЫХ пользователей
    if referral_code and is_new_user:
        try:
            # Пытаемся преобразовать в user_id
            referrer_id = int(referral_code)
            
            # Проверяем, есть ли уже запись о том, что этот пользователь был приглашен
            existing_referrer = await Database.get_referrer_id(user_id)
            if not existing_referrer and referrer_id != user_id:
                success = await Database.register_referral(referrer_id, user_id)
                if success:
                    referral_bonus_given = True
                    # Уведомляем пригласившего
                    try:
                        await bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 <b>Новый реферал!</b>\n\n"
                                 f"Пользователь присоединился по твоей ссылке!\n"
                                 f"Ты получил 1 дополнительную попытку! 🎁",
                            parse_mode="HTML"
                        )
                    except:
                        pass  # Игнорируем ошибки отправки уведомления
        except ValueError:
            logger.warning(f"Неверный формат реферального кода: {referral_code}")
        except Exception as e:
            logger.error(f"Ошибка при обработке реферального кода {referral_code}: {e}", exc_info=True)
    
    logger.info(f"Пользователь {user_id} зарегистрирован через /start" + (f" (реферал по коду {referral_code})" if referral_bonus_given else ""))
    
    # Используем общую функцию для отправки приветствия
    await _send_welcome_message(message, referral_bonus_given, is_new_user)


async def _send_welcome_message(message: types.Message, referral_bonus_given: bool = False, is_new_user: bool = False):
    """Общая функция для отправки приветственного сообщения"""
    welcome_text = (
        "🎬 <b>Добро пожаловать в ReelsScript Bot!</b>\n\n"
        "Я помогаю создавать сценарии для рилсов, TikTok и YouTube Shorts.\n\n"
        "Просто нажми <b>«Создать сценарий»</b> и я сгенерирую для тебя уникальный сценарий!\n\n"
        "Возможности:\n"
        "✨ Разные ниши (юмор, лайфхаки, мотивация и др.)\n"
        "⏱️ Разные форматы (15 сек, 30 сек, 60 сек, longform)\n"
        "🎨 Разные стили (динамичный, спокойный, драматичный)\n"
        "📝 Визуальные подсказки и хэштеги\n\n"
    )
    
    if referral_bonus_given:
        welcome_text += "🎁 <b>Ты присоединился по реферальной ссылке!</b>\n\n"
    
    welcome_text += "Начнем? 🚀"
    
    try:
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())
    except (TelegramNetworkError, TelegramAPIError) as e:
        logger.warning(f"Таймаут при отправке приветственного сообщения: {e}")
        # Пытаемся отправить без клавиатуры
        try:
            await message.answer(welcome_text, parse_mode="HTML")
        except:
            pass


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Обработчик команды /help - показывает только доступные команды"""
    active_users.add(message.from_user.id)
    user_id = message.from_user.id
    
    # Проверяем статус пользователя
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    is_admin = LimitsManager.is_developer(user_id)
    
    help_text = "📖 <b>Справка по использованию бота</b>\n\n"
    help_text += "<b>Команды:</b>\n"
    
    # Базовые команды (доступны всем)
    help_text += "/start - Начать работу с ботом\n"
    help_text += "/help - Показать эту справку\n"
    help_text += "/new - Создать новый сценарий\n"
    help_text += "/import - Импортировать видео/текст и создать сценарий\n"
    help_text += "/ref - Реферальная программа\n"
    help_text += "/subscribe - Информация о подписке\n"
    help_text += "/support - Связаться с поддержкой\n"
    
    # Premium команды
    if is_premium:
        help_text += "\n💎 <b>Premium команды:</b>\n"
        help_text += "/my_subscription - Моя подписка\n"
        help_text += "/my_scenarios - История сценариев\n"
        help_text += "/scenario_ID - Просмотр сценария (например: /scenario_123)\n"
        help_text += "/my_stats - Моя статистика и аналитика\n"
        help_text += "/share_scenario ID - Создать ссылку на сценарий\n"
        help_text += "/my_shares - Управление шаринг-ссылками\n"
    
    # Админ команды
    if is_admin:
        help_text += "\n🔧 <b>Админ команды:</b>\n"
        help_text += "/admin - Панель администратора\n"
        help_text += "/stats - Статистика бота\n"
        help_text += "/db_info - Информация о БД\n"
        help_text += "/user_info USER_ID - Информация о пользователе\n"
        help_text += "/give_sub USER_ID MONTHS - Выдать подписку\n"
        help_text += "/remove_sub USER_ID - Удалить подписку\n"
        help_text += "/ref_stats USER_ID - Статистика рефералов пользователя\n"
        help_text += "/delete_user USER_ID - Удалить пользователя\n"
        help_text += "/reset_user USER_ID - Сбросить запросы пользователя\n"
        help_text += "/broadcast - Рассылка сообщений\n"
        help_text += "/reply - Ответить на обращение поддержки\n"
    
    help_text += "\n<b>Как использовать:</b>\n"
    help_text += "1. Нажми «Создать сценарий»\n"
    help_text += "2. Выбери нишу контента\n"
    help_text += "3. Выбери формат видео\n"
    help_text += "4. Выбери стиль сценария\n"
    
    if is_premium:
        help_text += "5. (Premium) Выбери тон, длительность и платформу\n"
        help_text += "6. (Опционально) Укажи тему\n"
        help_text += "7. Получи детальный сценарий!\n"
    else:
        help_text += "5. (Опционально) Укажи тему\n"
        help_text += "6. Получи готовый сценарий!\n"
    
    help_text += "\n💡 <b>Совет:</b> Чем больше деталей ты укажешь, тем лучше будет сценарий!\n"
    
    if not is_premium:
        help_text += "\n💎 <b>Хочешь больше возможностей?</b>\n"
        help_text += "Оформи Premium подписку для доступа к:\n"
        help_text += "• Детальным сценариям с таймингами\n"
        help_text += "• Истории всех созданных сценариев\n"
        help_text += "• Расширенным настройкам генерации\n\n"
        help_text += "Используй /subscribe для оформления!\n"
    
    help_text += "\n🎁 <b>Бонусы:</b>\n"
    help_text += "• Подпишись на канал - получи 3 попытки\n"
    help_text += "• Пригласи друга - получи 1 попытку за каждого"
    
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command("ref"))
async def cmd_ref(message: types.Message):
    """Обработчик команды /ref - показать статистику реферальной программы"""
    user_id = message.from_user.id
    active_users.add(user_id)
    await Database.register_user(user_id)
    
    # Получаем статистику рефералов
    referral_stats = await Database.get_referral_stats(user_id)
    total_referrals = referral_stats["total_referrals"]
    earned_attempts = referral_stats["earned_attempts"]
    
    # Получаем текущее количество дополнительных попыток
    extra_requests = await Database.get_extra_requests_count(user_id)
    
    # Получаем реферальную ссылку (простой user_id)
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    
    ref_text = (
        "🎯 <b>Реферальная программа</b>\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"• Перешло по ссылке: {total_referrals} человек\n"
        f"• Попыток заработано: {earned_attempts}\n"
        f"• Всего дополнительных попыток: {extra_requests}\n\n"
        "💡 <b>Как это работает:</b>\n"
        "1. Поделись своей реферальной ссылкой с друзьями\n"
        "2. Когда друг активирует бота по твоей ссылке\n"
        "3. Ты автоматически получишь 1 дополнительную попытку! 🎁\n\n"
        f"🔗 <b>Твоя реферальная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "💬 <b>Совет:</b> Чем больше друзей пригласишь, тем больше попыток получишь!"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Поделиться ссылкой", url=f"https://t.me/share/url?url={referral_link}&text=🎬%20Создавай%20сценарии%20для%20рилсов%20с%20ReelsScript%20Bot!")]
        ]
    )
    
    await message.answer(ref_text, parse_mode="HTML", reply_markup=keyboard)


@dp.message(Command("new"))
async def cmd_new(message: types.Message, state: FSMContext):
    """Обработчик команды /new - начать создание нового сценария"""
    # Используем тот же обработчик, что и для кнопки
    await create_scenario(message, state)


@dp.message(F.text == "🎬 Создать сценарий")
async def create_scenario(message: types.Message, state: FSMContext):
    """Обработчик кнопки создания сценария"""
    user_id = message.from_user.id
    
    # Проверяем, первый ли раз пользователь создает сценарий
    is_first_time = not await Database.is_first_scenario_shown(user_id)
    
    if is_first_time:
        # Показываем сообщение с предложениями
        await Database.set_first_scenario_shown(user_id, True)
        
        # Получаем реферальную ссылку пользователя (простой user_id)
        bot_info = await bot.get_me()
        bot_username = bot_info.username
        referral_link = f"https://t.me/{bot_username}?start={user_id}"
        
        # Проверяем статус подписки
        is_channel_subscribed = await Database.is_channel_subscribed(user_id)
        
        bonus_text = (
            "🎁 <b>Получи дополнительные попытки!</b>\n\n"
            "📢 <b>Подпишись на канал</b> - получи <b>3 попытки</b>\n"
            f"👉 {REQUIRED_CHANNEL_URL}\n\n"
            "👥 <b>Пригласи друга</b> - получи <b>1 попытку</b> за каждого\n"
            "Поделись ссылкой с друзьями, и когда они активируют бота, ты получишь попытки!\n\n"
            "💡 <b>Совет:</b> Чем больше друзей пригласишь, тем больше попыток получишь!"
        )
        
        keyboard_buttons = []
        
        # Кнопка подписки на канал (если еще не подписан)
        if not is_channel_subscribed:
            keyboard_buttons.append([InlineKeyboardButton(text="📢 Подписаться на канал (+3 попытки)", url=REQUIRED_CHANNEL_URL)])
            keyboard_buttons.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")])
        
        # Кнопка поделиться ссылкой
        keyboard_buttons.append([InlineKeyboardButton(text="👥 Поделиться ссылкой", url=f"https://t.me/share/url?url={referral_link}&text=🎬%20Создавай%20сценарии%20для%20рилсов%20с%20ReelsScript%20Bot!")])
        
        # Кнопка пропустить
        keyboard_buttons.append([InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_bonus")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(bonus_text, parse_mode="HTML", reply_markup=keyboard)
        return
    
    # Проверяем лимиты запросов
    can_request, error_msg = await LimitsManager.can_make_request(user_id)
    if not can_request:
        await message.answer(error_msg, parse_mode="HTML")
        return
    
    await state.clear()
    # Устанавливаем начальное состояние для диалога
    await state.set_state(ScenarioStates.waiting_for_niche)
    await ask_for_niche(message)


@dp.callback_query(F.data == "skip_bonus")
async def skip_bonus_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопки пропустить при предложении дополнительных попыток"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    # Отвечаем на callback, чтобы убрать индикатор загрузки
    await callback.answer()
    
    # Проверяем лимиты запросов
    can_request, error_msg = await LimitsManager.can_make_request(user_id)
    if not can_request:
        await callback.message.answer(error_msg, parse_mode="HTML")
        return
    
    # Продолжаем создание сценария
    await state.clear()
    await state.set_state(ScenarioStates.waiting_for_niche)
    # Используем callback.message для отправки нового сообщения
    await ask_for_niche(callback.message)


@dp.message(F.text == "ℹ️ Помощь")
async def show_help(message: types.Message):
    """Обработчик кнопки помощи"""
    await cmd_help(message)


@dp.message(F.text == "💎 Подписка")
async def show_subscription(message: types.Message):
    """Обработчик кнопки подписки"""
    await cmd_subscribe(message)


@dp.message(Command("import"))
async def cmd_import(message: types.Message, state: FSMContext):
    """Обработчик команды /import - импорт видео или текста для создания сценария"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    # Проверяем лимиты запросов
    can_request, error_msg = await LimitsManager.can_make_request(user_id)
    if not can_request:
        await message.answer(error_msg, parse_mode="HTML")
        return
    
    text = (
        "📥 <b>Импорт контента для создания сценария</b>\n\n"
        "Ты можешь прислать:\n\n"
        "🎥 <b>Ссылку на видео</b> (YouTube, Instagram, TikTok)\n"
        "   Бот проанализирует структуру и стиль видео и создаст похожий сценарий\n\n"
        "📝 <b>Текст поста/сценария</b>\n"
        "   Бот проанализирует текст и создаст новый сценарий в похожем стиле\n\n"
        "Отправь ссылку или текст:"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_import")]
        ]
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.clear()
    await state.set_state(ScenarioStates.waiting_for_import_url)


@dp.callback_query(F.data == "cancel_import")
async def cancel_import_callback(callback: types.CallbackQuery, state: FSMContext):
    """Отмена импорта"""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Импорт отменен", parse_mode="HTML")


@dp.message(StateFilter(ScenarioStates.waiting_for_import_url))
async def process_import_input(message: types.Message, state: FSMContext):
    """Обработка ввода для импорта (ссылка или текст)"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    if not message.text:
        await message.answer("❌ Пожалуйста, отправь ссылку или текст")
        return
    
    if message.text.startswith('/'):
        return
    
    input_text = message.text.strip()
    
    # Проверяем, является ли это URL
    if content_importer.is_url(input_text):
        # Это ссылка на видео
        await message.answer("⏳ Обрабатываю ссылку...", parse_mode="HTML")
        
        try:
            # Извлекаем информацию из видео
            video_info = await content_importer.extract_video_info(input_text)
            
            if 'error' in video_info:
                await message.answer(
                    f"❌ <b>Ошибка</b>\n\n{video_info['error']}",
                    parse_mode="HTML"
                )
                await state.clear()
                return
            
            # Сохраняем информацию в состояние
            await state.update_data(
                import_type='url',
                import_url=input_text,
                import_platform=video_info.get('platform'),
                import_title=video_info.get('title', ''),
                import_text=video_info.get('text', '')
            )
            
            # Если текст есть, продолжаем с анализом
            if video_info.get('text'):
                # Формируем сообщение о том, что было извлечено
                info_text = f"✅ <b>Информация извлечена!</b>\n\n"
                info_text += f"📌 Платформа: {video_info.get('platform', 'неизвестна').upper()}\n"
                info_text += f"📝 Название: {video_info.get('title', 'не указано')}\n"
                
                # Показываем информацию о транскрипции
                if video_info.get('transcript'):
                    transcript_length = len(video_info.get('transcript', ''))
                    info_text += f"🎤 Транскрипция: найдена ({transcript_length} символов)\n"
                else:
                    info_text += f"⚠️ Транскрипция: не найдена (анализ будет на основе названия)\n"
                
                info_text += f"\n⏳ Анализирую структуру и стиль контента..."
                
                await message.answer(info_text, parse_mode="HTML")
                
                # Анализируем контент
                analysis = await content_importer.analyze_content_structure(
                    video_info.get('text', ''),
                    video_info.get('platform')
                )
                
                await state.update_data(import_analysis=analysis)
                
                # Переходим к выбору ниши
                await ask_for_import_niche(message, state)
            else:
                # Если текста нет, просим пользователя прислать текст вручную
                await message.answer(
                    "📝 <b>Текст не найден автоматически</b>\n\n"
                    "Пожалуйста, пришли описание видео или текст поста вручную:",
                    parse_mode="HTML"
                )
                await state.set_state(ScenarioStates.waiting_for_import_text)
                
        except Exception as e:
            logger.error(f"Ошибка при обработке ссылки: {e}", exc_info=True)
            await message.answer(
                f"❌ <b>Ошибка при обработке ссылки</b>\n\n{str(e)[:200]}",
                parse_mode="HTML"
            )
            await state.clear()
    else:
        # Это текст
        await message.answer("⏳ Анализирую структуру и стиль текста...", parse_mode="HTML")
        
        try:
            # Анализируем текст
            analysis = await content_importer.analyze_content_structure(input_text)
            
            if 'error' in analysis:
                await message.answer(
                    f"❌ <b>Ошибка при анализе</b>\n\n{analysis['error']}",
                    parse_mode="HTML"
                )
                await state.clear()
                return
            
            # Сохраняем в состояние
            await state.update_data(
                import_type='text',
                import_text=input_text,
                import_analysis=analysis
            )
            
            # Показываем краткую информацию об анализе
            analysis_summary = (
                f"✅ <b>Анализ завершен!</b>\n\n"
                f"📊 <b>Структура:</b> {analysis.get('structure', 'не определена')[:100]}...\n"
                f"🎨 <b>Стиль:</b> {analysis.get('style', 'не определен')[:100]}...\n"
                f"🎯 <b>Формат:</b> {analysis.get('format', 'не определен')}\n\n"
                f"Теперь выбери параметры для нового сценария:"
            )
            
            await message.answer(analysis_summary, parse_mode="HTML")
            
            # Переходим к выбору ниши
            await ask_for_import_niche(message, state)
            
        except Exception as e:
            logger.error(f"Ошибка при анализе текста: {e}", exc_info=True)
            await message.answer(
                f"❌ <b>Ошибка при анализе</b>\n\n{str(e)[:200]}",
                parse_mode="HTML"
            )
            await state.clear()


@dp.message(StateFilter(ScenarioStates.waiting_for_import_text))
async def process_import_text(message: types.Message, state: FSMContext):
    """Обработка текста, присланного вручную после ссылки"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    if not message.text:
        await message.answer("❌ Пожалуйста, отправь текст")
        return
    
    if message.text.startswith('/'):
        return
    
    input_text = message.text.strip()
    
    await message.answer("⏳ Анализирую структуру и стиль текста...", parse_mode="HTML")
    
    try:
        # Получаем данные из состояния
        data = await state.get_data()
        platform = data.get('import_platform')
        
        # Анализируем текст
        analysis = await content_importer.analyze_content_structure(input_text, platform)
        
        if 'error' in analysis:
            await message.answer(
                f"❌ <b>Ошибка при анализе</b>\n\n{analysis['error']}",
                parse_mode="HTML"
            )
            await state.clear()
            return
        
        # Обновляем состояние
        await state.update_data(
            import_text=input_text,
            import_analysis=analysis
        )
        
        # Переходим к выбору ниши
        await ask_for_import_niche(message, state)
        
    except Exception as e:
        logger.error(f"Ошибка при анализе текста: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>Ошибка при анализе</b>\n\n{str(e)[:200]}",
            parse_mode="HTML"
        )
        await state.clear()


async def ask_for_import_niche(message: types.Message, state: FSMContext):
    """Запрашивает выбор ниши для импортированного контента"""
    text = (
        "📋 <b>Выбери нишу для нового сценария</b>\n\n"
        "В какой нише ты хочешь создать сценарий в похожем стиле?"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💼 Бизнес", callback_data="import_niche_бизнес")],
            [InlineKeyboardButton(text="💄 Красота", callback_data="import_niche_красота")],
            [InlineKeyboardButton(text="🍔 Еда", callback_data="import_niche_еда")],
            [InlineKeyboardButton(text="🏋️ Спорт", callback_data="import_niche_спорт")],
            [InlineKeyboardButton(text="🎮 Игры", callback_data="import_niche_игры")],
            [InlineKeyboardButton(text="📚 Образование", callback_data="import_niche_образование")],
            [InlineKeyboardButton(text="😄 Юмор", callback_data="import_niche_юмор")],
            [InlineKeyboardButton(text="💡 Лайфхаки", callback_data="import_niche_лайфхаки")],
            [InlineKeyboardButton(text="🎬 Развлечения", callback_data="import_niche_развлечения")],
            [InlineKeyboardButton(text="📱 Технологии", callback_data="import_niche_технологии")],
            [InlineKeyboardButton(text="🌍 Путешествия", callback_data="import_niche_путешествия")],
            [InlineKeyboardButton(text="💬 Мотивация", callback_data="import_niche_мотивация")],
            [InlineKeyboardButton(text="🔍 Общее", callback_data="import_niche_общее")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_import")]
        ]
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(ScenarioStates.waiting_for_import_niche)


@dp.callback_query(F.data.startswith("import_niche_"))
async def process_import_niche(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора ниши для импорта"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    try:
        await callback.answer()
        
        niche = callback.data.replace("import_niche_", "")
        
        # Получаем данные из состояния
        data = await state.get_data()
        import_text = data.get('import_text', '')
        import_analysis = data.get('import_analysis', {})
        import_type = data.get('import_type', 'text')
        import_url = data.get('import_url', '')
        
        if not import_text or not import_analysis:
            await callback.message.edit_text(
                "❌ Ошибка: данные импорта не найдены. Начни заново командой /import",
                parse_mode="HTML"
            )
            await state.clear()
            return
        
        # Сохраняем нишу
        await state.update_data(niche=niche)
        
        # Запрашиваем формат
        text = (
            f"✅ Ниша: <b>{niche}</b>\n\n"
            "⏱️ <b>Выбери длительность видео:</b>"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="15 секунд", callback_data="import_format_15 секунд")],
                [InlineKeyboardButton(text="30 секунд", callback_data="import_format_30 секунд")],
                [InlineKeyboardButton(text="60 секунд", callback_data="import_format_60 секунд")],
                [InlineKeyboardButton(text="Longform (2+ минуты)", callback_data="import_format_longform")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_import")]
            ]
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора ниши: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("import_format_"))
async def process_import_format(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора формата для импорта"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    try:
        await callback.answer()
        
        format_type = callback.data.replace("import_format_", "")
        
        # Получаем данные из состояния
        data = await state.get_data()
        import_text = data.get('import_text', '')
        import_analysis = data.get('import_analysis', {})
        niche = data.get('niche', 'общее')
        
        # Сохраняем формат
        await state.update_data(format_type=format_type)
        
        # Проверяем лимиты
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        
        # Генерируем сценарий
        await callback.message.edit_text(
            "⏳ Создаю сценарий на основе проанализированного контента...\n\n"
            "Это может занять несколько секунд...",
            parse_mode="HTML"
        )
        
        try:
            # Создаем сценарий на основе импортированного контента
            scenario = await content_importer.create_scenario_from_content(
                content_text=import_text,
                analysis=import_analysis,
                niche=niche,
                format_type=format_type,
                style="динамичный",  # Можно добавить выбор стиля
                platform=None,
                user_id=user_id,
                is_premium=is_premium
            )
            
            if not scenario or len(scenario) < 50:
                await callback.message.edit_text(
                    "❌ <b>Ошибка при создании сценария</b>\n\n"
                    "Попробуй еще раз или свяжись с поддержкой.",
                    parse_mode="HTML"
                )
                await state.clear()
                return
            
            # Отправляем сценарий
            if len(scenario) > 4096:
                await callback.message.edit_text(scenario[:4096], parse_mode="HTML")
                await callback.message.answer(scenario[4096:], parse_mode="HTML")
            else:
                await callback.message.edit_text(scenario, parse_mode="HTML")
            
            # Сохраняем сценарий в историю для Premium
            if is_premium:
                try:
                    await Database.save_user_scenario(
                        user_id=user_id,
                        scenario_text=scenario,
                        niche=niche,
                        format_type=format_type,
                        style="динамичный",
                        topic=f"Импортировано из {data.get('import_type', 'контента')}",
                        is_premium=True
                    )
                except Exception as e:
                    logger.error(f"Ошибка при сохранении импортированного сценария: {e}", exc_info=True)
            
            # Вычитаем попытку
            await LimitsManager.increment_request(user_id, active_users)
            
            await state.clear()
            
        except Exception as e:
            logger.error(f"Ошибка при создании сценария из импорта: {e}", exc_info=True)
            await callback.message.edit_text(
                f"❌ <b>Ошибка при создании сценария</b>\n\n{str(e)[:200]}",
                parse_mode="HTML"
            )
            await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка при обработке формата: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)




@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    """Обработчик команды /subscribe - показать информацию о подписке"""
    user_id = message.from_user.id
    
    subscription_info = await SubscriptionManager.get_subscription_info(user_id)
    has_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    if has_premium and subscription_info:
        days_left = subscription_info["days_left"]
        expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y")
        
        text = (
            "💎 <b>У вас активная премиум подписка!</b>\n\n"
            f"📅 <b>Действует до:</b> {expires_at}\n"
            f"⏰ <b>Осталось дней:</b> {days_left}\n\n"
            "✅ <b>Преимущества:</b>\n"
            "• Безлимитное количество запросов\n"
            "• Детальные развернутые сценарии с таймингами и реквизитами\n"
            "• История всех созданных сценариев (/my_scenarios)\n"
            "• Расширенные настройки (тон, длительность, платформа)\n"
            "• Приоритетная поддержка\n"
            "• Доступ ко всем функциям\n\n"
            "Спасибо за поддержку! 🙏"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        remaining = await LimitsManager.get_remaining_requests(user_id)
        extra_requests = await Database.get_extra_requests_count(user_id)
        
        if remaining == -1:
            remaining_text = "Безлимит"
        else:
            requests_count = await Database.get_user_requests_count(user_id)
            free_remaining = max(0, MAX_REQUESTS_PER_USER - requests_count)
            remaining_text = f"Осталось: {remaining} запросов"
            if extra_requests > 0:
                remaining_text += f" ({free_remaining} бесплатных + {extra_requests} дополнительных)"
        
        text = (
            "💎 <b>Премиум подписка и дополнительные попытки</b>\n\n"
            f"📊 <b>Текущий статус:</b> Бесплатный тариф\n"
            f"📈 {remaining_text}\n\n"
            "✨ <b>Варианты:</b>\n"
            "• 💎 Премиум подписка (безлимит + расширенные функции)\n"
            "• 🎯 Дополнительные попытки (пополняемый баланс)\n\n"
            "💎 <b>Что дает Premium:</b>\n"
            "• Безлимит генерации сценариев\n"
            "• Детальные сценарии с таймингами и реквизитами\n"
            "• История всех сценариев (/my_scenarios)\n"
            "• Расширенные настройки (тон, длительность, платформа)"
        )
        
        # Проверяем, подписан ли пользователь на канал
        is_channel_subscribed = await Database.is_channel_subscribed(user_id)
        if not is_channel_subscribed:
            text += (
                "\n\n🎁 <b>Бесплатный бонус:</b>\n"
                "• Подпишись на канал @reelsAIcontent - получи 3 попытки!"
            )
        
        text += "\n\nВыберите вариант:"
        
        keyboard_buttons = [
            [InlineKeyboardButton(text="💎 Премиум подписка", callback_data="choose_subscription")],
            [InlineKeyboardButton(text="🎯 Дополнительные попытки", callback_data="choose_extra_requests")],
            [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="subscription_info")]
        ]
        
        if not is_channel_subscribed:
            keyboard_buttons.insert(1, [InlineKeyboardButton(text="📢 Подписаться на канал (+3 попытки)", callback_data="subscribe_channel")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.message(Command("my_subscription"))
async def cmd_my_subscription(message: types.Message):
    active_users.add(message.from_user.id)
    """Обработчик команды /my_subscription - показать информацию о текущей подписке"""
    user_id = message.from_user.id
    subscription_info = await SubscriptionManager.get_subscription_info(user_id)
    
    if subscription_info:
        days_left = subscription_info["days_left"]
        expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y %H:%M")
        purchased_at = subscription_info["purchased_at"].strftime("%d.%m.%Y %H:%M")
        
        text = (
            "💎 <b>Моя подписка</b>\n\n"
            f"📦 <b>Тариф:</b> Премиум\n"
            f"📅 <b>Куплена:</b> {purchased_at}\n"
            f"📅 <b>Действует до:</b> {expires_at}\n"
            f"⏰ <b>Осталось дней:</b> {days_left}\n\n"
            "✅ <b>Преимущества:</b>\n"
            "• Безлимитное количество запросов\n"
            "• Детальные развернутые сценарии с таймингами и реквизитами\n"
            "• История всех созданных сценариев (/my_scenarios)\n"
            "• Расширенные настройки (тон, длительность, платформа)\n"
            "• Приоритетная поддержка\n"
            "• Доступ ко всем функциям"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        text = (
            "💎 <b>У тебя нет активной подписки</b>\n\n"
            "Оформи подписку, чтобы получить безлимит и доступ к премиум функциям:\n"
            "• Детальные развернутые сценарии\n"
            "• История всех созданных сценариев\n"
            "• Расширенные настройки генерации\n\n"
            "Используй /subscribe для оформления подписки."
        )
        await message.answer(text, parse_mode="HTML")


@dp.message(Command("my_stats"))
async def cmd_stats_premium(message: types.Message):
    """Статистика и аналитика для Premium пользователей"""
    active_users.add(message.from_user.id)
    user_id = message.from_user.id
    
    # Проверяем Premium статус
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        await message.answer(
            "💎 <b>Статистика доступна только для Premium пользователей</b>\n\n"
            "Оформи подписку: /subscribe",
            parse_mode="HTML"
        )
        return
    
    try:
        # Получаем статистику
        stats = await Database.get_user_statistics(user_id)
        
        text = "📊 <b>Твоя статистика и аналитика</b>\n\n"
        
        # Общее количество
        text += f"📈 <b>Всего создано сценариев:</b> {stats['total_count']}\n\n"
        
        # Популярные ниши
        if stats['niche_stats']:
            text += "🎯 <b>Популярные ниши:</b>\n"
            for i, item in enumerate(stats['niche_stats'][:5], 1):
                text += f"{i}. {item['niche']} — {item['count']} раз(а)\n"
            text += "\n"
        
        # Популярные форматы
        if stats['format_stats']:
            text += "⏱️ <b>Популярные форматы:</b>\n"
            for i, item in enumerate(stats['format_stats'][:5], 1):
                text += f"{i}. {item['format_type']} — {item['count']} раз(а)\n"
            text += "\n"
        
        # Популярные стили
        if stats['style_stats']:
            text += "🎨 <b>Популярные стили:</b>\n"
            for i, item in enumerate(stats['style_stats'][:5], 1):
                text += f"{i}. {item['style']} — {item['count']} раз(а)\n"
            text += "\n"
        
        # Активность за последние 30 дней
        if stats['activity_stats']:
            text += "📅 <b>Активность за последние 30 дней:</b>\n"
            total_last_month = sum(item['count'] for item in stats['activity_stats'])
            text += f"Всего: {total_last_month} сценариев\n"
            
            # Показываем последние 7 дней
            recent_days = stats['activity_stats'][:7]
            if recent_days:
                text += "\nПоследние 7 дней:\n"
                for item in recent_days:
                    date_str = item['date'].strftime("%d.%m") if hasattr(item['date'], 'strftime') else str(item['date'])
                    text += f"• {date_str}: {item['count']} сценариев\n"
        
        if stats['total_count'] == 0:
            text = (
                "📊 <b>Твоя статистика</b>\n\n"
                "Пока нет данных для отображения.\n"
                "Создай свой первый сценарий: /new"
            )
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при получении статистики</b>\n\n"
            "Попробуйте позже.",
            parse_mode="HTML"
        )


@dp.message(Command("my_scenarios"))
async def cmd_my_scenarios(message: types.Message):
    active_users.add(message.from_user.id)
    """Обработчик команды /my_scenarios - показать историю сценариев (Premium)"""
    user_id = message.from_user.id
    
    # Проверяем, является ли пользователь Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    if not is_premium:
        text = (
            "💎 <b>История сценариев доступна только для Premium подписчиков</b>\n\n"
            "Оформи подписку, чтобы получить доступ к истории всех созданных сценариев!\n\n"
            "Используй /subscribe для оформления подписки."
        )
        await message.answer(text, parse_mode="HTML")
        return
    
    try:
        scenarios = await Database.get_user_scenarios(user_id, limit=10)
        total_count = await Database.get_user_scenarios_count(user_id)
        
        if not scenarios:
            text = (
                "📚 <b>Мои сценарии</b>\n\n"
                "У тебя пока нет сохраненных сценариев.\n\n"
                "Создай свой первый сценарий, и он появится здесь!"
            )
            await message.answer(text, parse_mode="HTML")
            return
        
        text = f"📚 <b>Мои сценарии</b>\n\nВсего сохранено: <b>{total_count}</b>\n\n"
        text += "Последние сценарии:\n\n"
        
        for i, scenario_data in enumerate(scenarios[:5], 1):
            created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data['created_at'] else "Дата неизвестна"
            niche = scenario_data.get('niche') or 'Не указана'
            topic = scenario_data.get('topic') or 'Без темы'
            platform = scenario_data.get('platform') or 'Не указана'
            
            text += f"{i}. <b>{niche}</b>\n"
            text += f"   Тема: {topic[:30]}{'...' if len(topic) > 30 else ''}\n"
            if platform and platform != 'Не указана':
                platform_names = {
                    "reels": "Reels",
                    "tiktok": "TikTok",
                    "shorts": "Shorts",
                    "универсальный": "Универсальный"
                }
                platform_name = platform_names.get(platform.lower(), platform)
                text += f"   Платформа: {platform_name}\n"
            text += f"   Дата: {created_at}\n"
            text += f"   <code>/scenario_{scenario_data['id']}</code>\n\n"
        
        if len(scenarios) > 5:
            text += f"И еще {len(scenarios) - 5} сценариев...\n\n"
        
        text += "Используй команду <code>/scenario_ID</code> для просмотра конкретного сценария."
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Посмотреть все", callback_data="view_all_scenarios")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
            ]
        )
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении истории сценариев для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при загрузке истории сценариев</b>\n\n"
            "Попробуй позже или обратись в поддержку: /support",
            parse_mode="HTML"
        )


async def _view_scenario_by_id(message: types.Message, user_id: int, scenario_id: int):
    """Общая функция для просмотра сценария по ID"""
    scenario_data = await Database.get_scenario_by_id(scenario_id, user_id)
    
    if not scenario_data:
        await message.answer(
            "❌ <b>Сценарий не найден</b>\n\n"
            "Возможно, ты указал неверный ID или этот сценарий не принадлежит тебе.\n\n"
            "Используй /my_scenarios для просмотра всех твоих сценариев.",
            parse_mode="HTML"
        )
        return
    
    # Формируем сообщение со сценарием
    created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data['created_at'] else "Дата неизвестна"
    
    info_text = (
        "🎬 <b>Сценарий #{}</b>\n\n"
        "<b>Ниша:</b> {}\n"
        "<b>Формат:</b> {}\n"
        "<b>Стиль:</b> {}\n"
    ).format(
        scenario_data['id'],
        scenario_data.get('niche') or 'Не указана',
        scenario_data.get('format_type') or 'Не указан',
        scenario_data.get('style') or 'Не указан'
    )
    
    if scenario_data.get('tone'):
        info_text += f"<b>Тон:</b> {scenario_data['tone']}\n"
    if scenario_data.get('duration'):
        info_text += f"<b>Длительность:</b> {scenario_data['duration']}\n"
    if scenario_data.get('platform'):
        platform_names = {
            "reels": "Reels",
            "tiktok": "TikTok",
            "shorts": "Shorts",
            "универсальный": "Универсальный"
        }
        platform_name = platform_names.get(scenario_data['platform'].lower(), scenario_data['platform'])
        info_text += f"<b>Платформа:</b> {platform_name}\n"
    if scenario_data.get('topic'):
        info_text += f"<b>Тема:</b> {scenario_data['topic']}\n"
    
    info_text += f"<b>Создан:</b> {created_at}\n\n"
    info_text += "=" * 30 + "\n\n"
    
    scenario_text = scenario_data['scenario_text']
    
    # Отправляем информацию и сценарий
    await message.answer(info_text, parse_mode="HTML")
    
    # Разбиваем длинный сценарий на части, если нужно
    if len(scenario_text) > 4000:
        chunks = [scenario_text[i:i+4000] for i in range(0, len(scenario_text), 4000)]
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")
    else:
        await message.answer(scenario_text, parse_mode="HTML")
    
    # Проверяем Premium статус для добавления кнопки "Поделиться"
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    keyboard_buttons = [
        [
            InlineKeyboardButton(text="📥 Экспорт", callback_data=f"export_scenario_{scenario_id}"),
            InlineKeyboardButton(text="📚 Все сценарии", callback_data="view_all_scenarios")
        ]
    ]
    
    if is_premium:
        keyboard_buttons.insert(0, [
            InlineKeyboardButton(text="🔗 Поделиться", callback_data=f"share_scenario_{scenario_id}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_main")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("Используй /my_scenarios для просмотра всех сценариев.", reply_markup=keyboard)


@dp.message(F.text.regexp(r'^/scenario_\d+'))
async def cmd_view_scenario_regexp(message: types.Message):
    """Обработка команды вида /scenario_123 (через regexp для текстовых сообщений)"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    # Проверяем, является ли пользователь Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    if not is_premium:
        text = (
            "💎 <b>Просмотр сценариев доступен только для Premium подписчиков</b>\n\n"
            "Оформи подписку: /subscribe"
        )
        await message.answer(text, parse_mode="HTML")
        return
    
    try:
        # Извлекаем ID из /scenario_123
        command_text = message.text.strip()
        match = re.match(r'^/scenario_(\d+)', command_text)
        if not match:
            await message.answer(
                "❌ Неверный формат команды.\n"
                "Используй: <code>/scenario_123</code>",
                parse_mode="HTML"
            )
            return
        
        scenario_id = int(match.group(1))
        
        # Используем общую функцию для просмотра сценария
        await _view_scenario_by_id(message, user_id, scenario_id)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке /scenario_* для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при загрузке сценария</b>\n\n"
            "Попробуй позже или обратись в поддержку: /support",
            parse_mode="HTML"
        )


@dp.message(Command("scenario"))
async def cmd_view_scenario(message: types.Message):
    """Просмотр конкретного сценария по ID (формат: /scenario 123 или /scenario_123)"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    # Проверяем, является ли пользователь Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    if not is_premium:
        text = (
            "💎 <b>Просмотр сценариев доступен только для Premium подписчиков</b>\n\n"
            "Оформи подписку: /subscribe"
        )
        await message.answer(text, parse_mode="HTML")
        return
    
    try:
        # Извлекаем ID из команды
        # Варианты: /scenario_123 или /scenario 123
        command_text = message.text.strip()
        scenario_id = None
        
        # Пробуем формат /scenario_123
        if '_' in command_text:
            command_parts = command_text.split('_')
            if len(command_parts) >= 2:
                try:
                    scenario_id = int(command_parts[1].split()[0])  # Берем первое число после подчеркивания
                except ValueError:
                    pass
        
        # Пробуем формат /scenario 123
        if scenario_id is None:
            parts = command_text.split()
            if len(parts) >= 2:
                try:
                    scenario_id = int(parts[1])
                except ValueError:
                    pass
        
        if scenario_id is None:
            await message.answer(
                "❌ Неверный формат команды.\n\n"
                "Используй один из форматов:\n"
                "• <code>/scenario_123</code>\n"
                "• <code>/scenario 123</code>\n\n"
                "(где 123 - ID сценария из /my_scenarios)",
                parse_mode="HTML"
            )
            return
        
        # Используем общую функцию для просмотра сценария
        await _view_scenario_by_id(message, user_id, scenario_id)
        
    except Exception as e:
        logger.error(f"Ошибка при просмотре сценария для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при загрузке сценария</b>\n\n"
            "Попробуй позже или обратись в поддержку: /support",
            parse_mode="HTML"
        )


@dp.message(Command("my_subscription"))
async def cmd_my_subscription(message: types.Message):
    active_users.add(message.from_user.id)
    """Обработчик команды /my_subscription - показать информацию о текущей подписке"""
    user_id = message.from_user.id
    subscription_info = await SubscriptionManager.get_subscription_info(user_id)
    
    if subscription_info:
        days_left = subscription_info["days_left"]
        expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y %H:%M")
        purchased_at = subscription_info["purchased_at"].strftime("%d.%m.%Y %H:%M")
        
        text = (
            "💎 <b>Моя подписка</b>\n\n"
            f"📦 <b>Тариф:</b> Премиум\n"
            f"📅 <b>Куплена:</b> {purchased_at}\n"
            f"📅 <b>Действует до:</b> {expires_at}\n"
            f"⏰ <b>Осталось дней:</b> {days_left}\n\n"
            "✅ <b>Преимущества:</b>\n"
            "• Безлимитное количество запросов\n"
            "• Детальные развернутые сценарии\n"
            "• История всех созданных сценариев\n"
            "• Расширенные настройки генерации\n"
            "• Приоритетная поддержка\n"
            "• Доступ ко всем функциям"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        text = (
            "📊 <b>Текущий тариф: Бесплатный</b>\n\n"
            "У вас нет активной подписки.\n"
            "Используйте /subscribe чтобы оформить премиум подписку."
        )
        await message.answer(text, parse_mode="HTML")


@dp.message(Command("share_scenario"))
async def cmd_share_scenario(message: types.Message):
    """Создать шаринг-ссылку на сценарий (Premium)"""
    active_users.add(message.from_user.id)
    user_id = message.from_user.id
    
    # Проверяем Premium статус
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        await message.answer(
            "💎 <b>Шаринг сценариев доступен только для Premium пользователей</b>\n\n"
            "Оформи подписку: /subscribe",
            parse_mode="HTML"
        )
        return
    
    try:
        # Извлекаем ID сценария из команды: /share_scenario 123
        command_text = message.text.strip()
        parts = command_text.split()
        
        if len(parts) < 2:
            await message.answer(
                "📤 <b>Создание шаринг-ссылки</b>\n\n"
                "Используй команду так:\n"
                "<code>/share_scenario 123</code>\n\n"
                "Где 123 - это ID сценария.\n"
                "ID можно узнать в /my_scenarios",
                parse_mode="HTML"
            )
            return
        
        try:
            scenario_id = int(parts[1])
        except ValueError:
            await message.answer(
                "❌ <b>Неверный формат ID</b>\n\n"
                "ID должен быть числом.\n"
                "Пример: <code>/share_scenario 123</code>",
                parse_mode="HTML"
            )
            return
        
        # Проверяем, что сценарий существует и принадлежит пользователю
        scenario = await Database.get_scenario_by_id(scenario_id, user_id)
        if not scenario:
            await message.answer(
                "❌ <b>Сценарий не найден</b>\n\n"
                "Возможно, ты указал неверный ID или этот сценарий не принадлежит тебе.\n\n"
                "Используй /my_scenarios для просмотра всех твоих сценариев.",
                parse_mode="HTML"
            )
            return
        
        # Генерируем уникальный токен
        share_token = secrets.token_urlsafe(16)
        
        # Создаем шаринг-ссылку (без срока действия)
        success = await Database.create_scenario_share(
            scenario_id=scenario_id,
            owner_id=user_id,
            share_token=share_token,
            expires_at=None
        )
        
        if not success:
            await message.answer(
                "❌ <b>Ошибка при создании ссылки</b>\n\n"
                "Попробуй позже.",
                parse_mode="HTML"
            )
            return
        
        # Формируем шаринг-ссылку
        bot_username = (await bot.get_me()).username
        share_url = f"https://t.me/{bot_username}?start=view_{share_token}"
        
        niche = scenario.get('niche') or 'Не указана'
        topic = scenario.get('topic') or 'Без темы'
        
        text = (
            "🔗 <b>Шаринг-ссылка создана!</b>\n\n"
            f"📋 <b>Сценарий #{scenario_id}</b>\n"
            f"Ниша: {niche}\n"
            f"Тема: {topic}\n\n"
            f"<b>Ссылка для просмотра:</b>\n"
            f"<code>{share_url}</code>\n\n"
            "Отправь эту ссылку другим пользователям, чтобы они могли посмотреть сценарий.\n\n"
            "Используй /my_shares для управления всеми шаринг-ссылками."
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои шаринг-ссылки", callback_data="my_shares")],
                [InlineKeyboardButton(text="📚 Все сценарии", callback_data="view_all_scenarios")]
            ]
        )
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при создании шаринг-ссылки для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при создании ссылки</b>\n\n"
            "Попробуй позже.",
            parse_mode="HTML"
        )


@dp.message(Command("my_shares"))
async def cmd_my_shares(message: types.Message):
    """Управление шаринг-ссылками (Premium)"""
    active_users.add(message.from_user.id)
    user_id = message.from_user.id
    
    # Проверяем Premium статус
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        await message.answer(
            "💎 <b>Шаринг сценариев доступен только для Premium пользователей</b>\n\n"
            "Оформи подписку: /subscribe",
            parse_mode="HTML"
        )
        return
    
    try:
        shares = await Database.get_user_scenario_shares(user_id)
        
        if not shares:
            text = (
                "📤 <b>Мои шаринг-ссылки</b>\n\n"
                "У тебя пока нет созданных шаринг-ссылок.\n\n"
                "Используй <code>/share_scenario ID</code> для создания ссылки на сценарий.\n\n"
                "Пример: <code>/share_scenario 123</code>"
            )
            await message.answer(text, parse_mode="HTML")
            return
        
        bot_username = (await bot.get_me()).username
        
        text = f"📤 <b>Мои шаринг-ссылки</b>\n\nВсего: <b>{len(shares)}</b>\n\n"
        
        keyboard_buttons = []
        for share in shares[:10]:  # Показываем до 10 ссылок
            scenario_id = share['scenario_id']
            niche = share.get('niche') or 'Не указана'
            topic = share.get('topic') or 'Без темы'
            created_at = share['created_at'].strftime("%d.%m.%Y") if share.get('created_at') else "Дата неизвестна"
            expires_at = share.get('expires_at')
            
            token = share['share_token']
            share_url = f"https://t.me/{bot_username}?start=view_{token}"
            
            text += f"<b>Сценарий #{scenario_id}</b>\n"
            text += f"Ниша: {niche}\n"
            text += f"Тема: {topic[:30]}{'...' if len(topic) > 30 else ''}\n"
            text += f"Создана: {created_at}\n"
            if expires_at:
                exp_date = expires_at.strftime("%d.%m.%Y") if hasattr(expires_at, 'strftime') else str(expires_at)
                text += f"Истекает: {exp_date}\n"
            text += f"<code>{share_url}</code>\n\n"
            
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"🗑️ Удалить #{scenario_id}",
                    callback_data=f"delete_share_{share['id']}"
                )
            ])
        
        if len(shares) > 10:
            text += f"И еще {len(shares) - 10} ссылок...\n\n"
        
        keyboard_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении шаринг-ссылок для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при загрузке шаринг-ссылок</b>\n\n"
            "Попробуй позже.",
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("share_scenario_"))
async def share_scenario_callback(callback: types.CallbackQuery):
    """Callback для кнопки 'Поделиться' сценарием"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        user_id = callback.from_user.id
        
        # Проверяем Premium статус
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        if not is_premium:
            await callback.answer("💎 Шаринг доступен только для Premium", show_alert=True)
            return
        
        # Извлекаем ID сценария
        scenario_id = int(callback.data.replace("share_scenario_", ""))
        
        # Проверяем, что сценарий принадлежит пользователю
        scenario = await Database.get_scenario_by_id(scenario_id, user_id)
        if not scenario:
            await callback.answer("❌ Сценарий не найден", show_alert=True)
            return
        
        # Генерируем уникальный токен
        share_token = secrets.token_urlsafe(16)
        
        # Создаем шаринг-ссылку
        success = await Database.create_scenario_share(
            scenario_id=scenario_id,
            owner_id=user_id,
            share_token=share_token,
            expires_at=None
        )
        
        if not success:
            await callback.answer("❌ Ошибка при создании ссылки", show_alert=True)
            return
        
        # Формируем шаринг-ссылку
        bot_username = (await bot.get_me()).username
        share_url = f"https://t.me/{bot_username}?start=view_{share_token}"
        
        niche = scenario.get('niche') or 'Не указана'
        topic = scenario.get('topic') or 'Без темы'
        
        text = (
            "🔗 <b>Шаринг-ссылка создана!</b>\n\n"
            f"📋 <b>Сценарий #{scenario_id}</b>\n"
            f"Ниша: {niche}\n"
            f"Тема: {topic}\n\n"
            f"<b>Ссылка для просмотра:</b>\n"
            f"<code>{share_url}</code>\n\n"
            "Отправь эту ссылку другим пользователям, чтобы они могли посмотреть сценарий.\n\n"
            "Используй /my_shares для управления всеми шаринг-ссылками."
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои шаринг-ссылки", callback_data="my_shares")],
                [InlineKeyboardButton(text="📚 Все сценарии", callback_data="view_all_scenarios")]
            ]
        )
        
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при создании шаринг-ссылки через callback: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("delete_share_"))
async def delete_share_callback(callback: types.CallbackQuery):
    """Удаление шаринг-ссылки"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        user_id = callback.from_user.id
        share_id = int(callback.data.replace("delete_share_", ""))
        
        success = await Database.delete_scenario_share(share_id, user_id)
        
        if success:
            await callback.message.edit_text(
                "✅ <b>Шаринг-ссылка удалена</b>",
                parse_mode="HTML"
            )
            # Показываем обновленный список
            await cmd_my_shares(callback.message)
        else:
            await callback.answer("❌ Не удалось удалить ссылку", show_alert=True)
            
    except Exception as e:
        logger.error(f"Ошибка при удалении шаринг-ссылки: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@dp.callback_query(F.data == "my_shares")
async def my_shares_callback(callback: types.CallbackQuery):
    """Callback для кнопки 'Мои шаринг-ссылки'"""
    try:
        await callback.answer()
    except:
        pass
    
    message = callback.message
    message.from_user = callback.from_user
    await cmd_my_shares(message)


@dp.callback_query(F.data == "choose_subscription")
async def choose_subscription_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Выбор периода подписки"""
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer(
            "❌ <b>Платежи не настроены</b>\n\n"
            "Для работы платежей необходимо настроить PAYMENT_PROVIDER_TOKEN в .env файле.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    if await SubscriptionManager.has_active_subscription(callback.from_user.id):
        await callback.message.answer(
            "✅ У вас уже есть активная премиум подписка!\n"
            "Используйте /my_subscription чтобы посмотреть детали.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = (
        "💎 <b>Выберите период подписки</b>\n\n"
        "✨ <b>Премиум подписка включает:</b>\n"
        "• 🚀 Безлимитное количество запросов\n"
        "• ⚡ Приоритетная обработка\n"
        "• 🎯 Доступ ко всем функциям\n"
        "• 💬 Приоритетная поддержка\n\n"
        "<b>Тарифы:</b>\n"
        f"• 1 месяц: {SUBSCRIPTION_PRICE_1_MONTH / 100:.0f} ₽\n"
        f"• 3 месяца: {SUBSCRIPTION_PRICE_3_MONTHS / 100:.0f} ₽ (экономия ~11%)\n"
        f"• 6 месяцев: {SUBSCRIPTION_PRICE_6_MONTHS / 100:.0f} ₽ (экономия ~16%)\n"
        f"• 1 год: {SUBSCRIPTION_PRICE_1_YEAR / 100:.0f} ₽ (экономия ~25%)\n"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"1 месяц - {SUBSCRIPTION_PRICE_1_MONTH / 100:.0f} ₽", callback_data="buy_sub_1")],
            [InlineKeyboardButton(text=f"3 месяца - {SUBSCRIPTION_PRICE_3_MONTHS / 100:.0f} ₽", callback_data="buy_sub_3")],
            [InlineKeyboardButton(text=f"6 месяцев - {SUBSCRIPTION_PRICE_6_MONTHS / 100:.0f} ₽", callback_data="buy_sub_6")],
            [InlineKeyboardButton(text=f"1 год - {SUBSCRIPTION_PRICE_1_YEAR / 100:.0f} ₽", callback_data="buy_sub_12")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_subscribe")]
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "choose_extra_requests")
async def choose_extra_requests_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Выбор пакета дополнительных попыток"""
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer(
            "❌ <b>Платежи не настроены</b>\n\n"
            "Для работы платежей необходимо настроить PAYMENT_PROVIDER_TOKEN в .env файле.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    extra_requests = await Database.get_extra_requests_count(callback.from_user.id)
    
    text = (
        "🎯 <b>Дополнительные попытки</b>\n\n"
        f"📊 <b>У вас сейчас:</b> {extra_requests} дополнительных попыток\n\n"
        "💡 <b>Как это работает:</b>\n"
        "• Дополнительные попытки не сгорают\n"
        "• Используются после исчерпания бесплатных\n"
        "• Можно докупать в любое время\n\n"
        "<b>Пакеты:</b>\n"
        f"• 1 попытка: {EXTRA_REQUEST_PRICE / 100:.0f} ₽\n"
        f"• 10 попыток: {EXTRA_REQUESTS_PACK_10 / 100:.0f} ₽ (экономия 20%)\n"
        f"• 25 попыток: {EXTRA_REQUESTS_PACK_25 / 100:.0f} ₽ (экономия 28%)\n"
        f"• 50 попыток: {EXTRA_REQUESTS_PACK_50 / 100:.0f} ₽ (экономия 36%)\n"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"1 попытка - {EXTRA_REQUEST_PRICE / 100:.0f} ₽", callback_data="buy_extra_1")],
            [InlineKeyboardButton(text=f"10 попыток - {EXTRA_REQUESTS_PACK_10 / 100:.0f} ₽", callback_data="buy_extra_10")],
            [InlineKeyboardButton(text=f"25 попыток - {EXTRA_REQUESTS_PACK_25 / 100:.0f} ₽", callback_data="buy_extra_25")],
            [InlineKeyboardButton(text=f"50 попыток - {EXTRA_REQUESTS_PACK_50 / 100:.0f} ₽", callback_data="buy_extra_50")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_subscribe")]
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "back_to_subscribe")
async def back_to_subscribe_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Вернуться к выбору типа покупки"""
    user_id = callback.from_user.id
    remaining = await LimitsManager.get_remaining_requests(user_id)
    extra_requests = await Database.get_extra_requests_count(user_id)
    
    if remaining == -1:
        remaining_text = "Безлимит"
    else:
        free_remaining = max(0, MAX_REQUESTS_PER_USER - await Database.get_user_requests_count(user_id))
        remaining_text = f"Осталось: {remaining} запросов"
        if extra_requests > 0:
            remaining_text += f" ({free_remaining} бесплатных + {extra_requests} дополнительных)"
    
    text = (
        "💎 <b>Премиум подписка и дополнительные попытки</b>\n\n"
        f"📊 <b>Текущий статус:</b> Бесплатный тариф\n"
        f"📈 {remaining_text}\n\n"
        "✨ <b>Варианты:</b>\n"
        "• 💎 Премиум подписка (безлимит на период)\n"
        "• 🎯 Дополнительные попытки (пополняемый баланс)\n\n"
        "Выберите вариант:"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Премиум подписка", callback_data="choose_subscription")],
            [InlineKeyboardButton(text="🎯 Дополнительные попытки", callback_data="choose_extra_requests")],
            [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="subscription_info")]
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_sub_"))
async def buy_subscription_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Обработка покупки подписки на выбранный период"""
    
    # Проверка настройки платежной системы
    if PAYMENT_SYSTEM == "robokassa":
        if not robokassa_service:
            await callback.message.answer(
                "❌ <b>Платежи не настроены</b>\n\n"
                "Для работы платежей необходимо настроить ROBOKASSA_PASSWORD1 и ROBOKASSA_PASSWORD2 в .env файле.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
    elif PAYMENT_SYSTEM == "telegram_payments":
        if not PAYMENT_PROVIDER_TOKEN:
            await callback.message.answer(
                "❌ <b>Платежи не настроены</b>\n\n"
                "Для работы платежей необходимо настроить PAYMENT_PROVIDER_TOKEN в .env файле.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
    else:
        await callback.message.answer(
            "❌ <b>Платежная система не настроена</b>\n\n"
            "Установите PAYMENT_SYSTEM=robokassa или PAYMENT_SYSTEM=telegram_payments в .env файле.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    if await SubscriptionManager.has_active_subscription(callback.from_user.id):
        await callback.message.answer(
            "✅ У вас уже есть активная премиум подписка!\n"
            "Используйте /my_subscription чтобы посмотреть детали.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    period_map = {
        "buy_sub_1": (1, SUBSCRIPTION_PRICE_1_MONTH, "1 месяц"),
        "buy_sub_3": (3, SUBSCRIPTION_PRICE_3_MONTHS, "3 месяца"),
        "buy_sub_6": (6, SUBSCRIPTION_PRICE_6_MONTHS, "6 месяцев"),
        "buy_sub_12": (12, SUBSCRIPTION_PRICE_1_YEAR, "1 год")
    }
    
    period_months, price, period_text = period_map.get(callback.data, (1, SUBSCRIPTION_PRICE_1_MONTH, "1 месяц"))
    duration_days = period_months * 30
    price_rub = price / 100  # Конвертируем из копеек в рубли
    
    try:
        if PAYMENT_SYSTEM == "robokassa":
            # Создаем платеж через Robokassa API
            inv_id = await Database.get_next_inv_id()
            description = f"Премиум подписка {period_text} - безлимитное количество запросов на {duration_days} дней"
            
            # Сохраняем платеж в БД
            await Database.create_payment(
                user_id=callback.from_user.id,
                inv_id=inv_id,
                payment_type="subscription",
                amount=price_rub,
                period_months=period_months
            )
            
            # Формируем Receipt для фискализации (Робочеки), если включено
            # Согласно документации: https://docs.robokassa.ru/fiscalization/
            receipt = None
            if ROBOKASSA_FISCAL_ENABLED:
                receipt = {
                    "items": [
                        {
                            "name": f"Премиум подписка {period_text}",
                            "quantity": "1",
                            "price": f"{price_rub:.2f}",
                            "tax": ROBOKASSA_TAX_RATE,
                            "payment_object": "service",  # Признак предмета расчета: услуга
                            "payment_method": "full_payment"  # Признак способа расчета: полная предоплата
                        }
                    ]
                }
                # Добавляем email пользователя для отправки чека, если доступен
                if hasattr(callback.from_user, 'email') and callback.from_user.email:
                    receipt["email"] = callback.from_user.email
            
            # Генерируем ссылку на оплату
            payment_url = robokassa_service.generate_payment_url(
                out_sum=price_rub,
                inv_id=inv_id,
                description=description,
                user_id=callback.from_user.id,
                receipt=receipt
            )
            
            # Сокращаем ссылку через clck.su, если включено
            if _shortener_instance and _shortener_instance.enabled:
                short_url = await _shortener_instance.shorten_url(payment_url)
                if short_url:
                    payment_url = short_url
                    logger.debug(f"Используется сокращенная ссылка: {short_url}")
            
            # Отправляем пользователю ссылку на оплату
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
                    [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_payment")]
                ]
            )
            
            await callback.message.answer(
                f"💎 <b>Оплата подписки</b>\n\n"
                f"<b>Период:</b> {period_text}\n"
                f"<b>Сумма:</b> {price_rub:.2f} ₽\n"
                f"<b>Описание:</b> {description}\n\n"
                f"Нажмите кнопку ниже для перехода на страницу оплаты:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            await callback.answer()
            logger.info(f"Создана ссылка на оплату Robokassa для подписки: пользователь {callback.from_user.id}, inv_id={inv_id}, период {period_months} месяцев, цена {price_rub}₽")
        
        else:
            # Используем Telegram Payments (PayMaster и другие)
            await bot.send_invoice(
                chat_id=callback.message.chat.id,
                title=f"💎 Премиум подписка ({period_text})",
                description=f"Безлимитное количество запросов на {duration_days} дней",
                payload=f"subscription_{callback.from_user.id}_{period_months}",
                provider_token=PAYMENT_PROVIDER_TOKEN,
                currency="RUB",
                prices=[types.LabeledPrice(label=f"Премиум подписка {period_text}", amount=price)],
                start_parameter=f"subscription_{period_months}",
                photo_url=None,
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
                send_phone_number_to_provider=False,
                send_email_to_provider=False,
                is_flexible=False
            )
            await callback.answer()
            logger.info(f"Создан invoice для подписки: пользователь {callback.from_user.id}, период {period_months} месяцев, цена {price_rub}₽")
    
    except Exception as e:
        logger.error(f"Ошибка при создании платежа для подписки: {e}", exc_info=True)
        await callback.message.answer(
            "❌ Произошла ошибка при создании платежа. Попробуйте позже.",
            parse_mode="HTML"
        )
        await callback.answer()


@dp.callback_query(F.data.startswith("buy_extra_"))
async def buy_extra_requests_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Обработка покупки дополнительных попыток"""
    
    # Проверка настройки платежной системы
    if PAYMENT_SYSTEM == "robokassa":
        if not robokassa_service:
            await callback.message.answer(
                "❌ <b>Платежи не настроены</b>\n\n"
                "Для работы платежей необходимо настроить ROBOKASSA_PASSWORD1 и ROBOKASSA_PASSWORD2 в .env файле.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
    elif PAYMENT_SYSTEM == "telegram_payments":
        if not PAYMENT_PROVIDER_TOKEN:
            await callback.message.answer(
                "❌ <b>Платежи не настроены</b>\n\n"
                "Для работы платежей необходимо настроить PAYMENT_PROVIDER_TOKEN в .env файле.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
    else:
        await callback.message.answer(
            "❌ <b>Платежная система не настроена</b>\n\n"
            "Установите PAYMENT_SYSTEM=robokassa или PAYMENT_SYSTEM=telegram_payments в .env файле.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    pack_map = {
        "buy_extra_1": (1, EXTRA_REQUEST_PRICE),
        "buy_extra_10": (10, EXTRA_REQUESTS_PACK_10),
        "buy_extra_25": (25, EXTRA_REQUESTS_PACK_25),
        "buy_extra_50": (50, EXTRA_REQUESTS_PACK_50)
    }
    
    count, price = pack_map.get(callback.data, (1, EXTRA_REQUEST_PRICE))
    price_rub = price / 100  # Конвертируем из копеек в рубли
    
    try:
        if PAYMENT_SYSTEM == "robokassa":
            # Создаем платеж через Robokassa API
            inv_id = await Database.get_next_inv_id()
            description = f"{count} дополнительных попыток для генерации сценариев"
            
            # Сохраняем платеж в БД
            await Database.create_payment(
                user_id=callback.from_user.id,
                inv_id=inv_id,
                payment_type="extra_requests",
                amount=price_rub,
                count=count
            )
            
            # Формируем Receipt для фискализации (Робочеки), если включено
            # Согласно документации: https://docs.robokassa.ru/fiscalization/
            receipt = None
            if ROBOKASSA_FISCAL_ENABLED:
                receipt = {
                    "items": [
                        {
                            "name": f"Дополнительные попытки ({count} шт.)",
                            "quantity": "1",
                            "price": f"{price_rub:.2f}",
                            "tax": ROBOKASSA_TAX_RATE,
                            "payment_object": "service",  # Признак предмета расчета: услуга
                            "payment_method": "full_payment"  # Признак способа расчета: полная предоплата
                        }
                    ]
                }
                # Добавляем email пользователя для отправки чека, если доступен
                if hasattr(callback.from_user, 'email') and callback.from_user.email:
                    receipt["email"] = callback.from_user.email
            
            # Генерируем ссылку на оплату
            payment_url = robokassa_service.generate_payment_url(
                out_sum=price_rub,
                inv_id=inv_id,
                description=description,
                user_id=callback.from_user.id,
                receipt=receipt
            )
            
            # Сокращаем ссылку через clck.su, если включено
            if _shortener_instance and _shortener_instance.enabled:
                short_url = await _shortener_instance.shorten_url(payment_url)
                if short_url:
                    payment_url = short_url
                    logger.debug(f"Используется сокращенная ссылка: {short_url}")
            
            # Отправляем пользователю ссылку на оплату
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
                    [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_payment")]
                ]
            )
            
            await callback.message.answer(
                f"🎯 <b>Оплата дополнительных попыток</b>\n\n"
                f"<b>Количество:</b> {count} попыток\n"
                f"<b>Сумма:</b> {price_rub:.2f} ₽\n"
                f"<b>Описание:</b> {description}\n\n"
                f"Нажмите кнопку ниже для перехода на страницу оплаты:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            await callback.answer()
            logger.info(f"Создана ссылка на оплату Robokassa для дополнительных попыток: пользователь {callback.from_user.id}, inv_id={inv_id}, количество {count}, цена {price_rub}₽")
        
        else:
            # Используем Telegram Payments (PayMaster и другие)
            await bot.send_invoice(
                chat_id=callback.message.chat.id,
                title=f"🎯 Дополнительные попытки ({count} шт.)",
                description=f"{count} дополнительных попыток для генерации сценариев",
                payload=f"extra_requests_{callback.from_user.id}_{count}",
                provider_token=PAYMENT_PROVIDER_TOKEN,
                currency="RUB",
                prices=[types.LabeledPrice(label=f"{count} дополнительных попыток", amount=price)],
                start_parameter=f"extra_{count}",
                photo_url=None,
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
                send_phone_number_to_provider=False,
                send_email_to_provider=False,
                is_flexible=False
            )
            await callback.answer()
            logger.info(f"Создан invoice для дополнительных попыток: пользователь {callback.from_user.id}, количество {count}, цена {price_rub}₽")
    
    except Exception as e:
        logger.error(f"Ошибка при создании платежа для дополнительных попыток: {e}", exc_info=True)
        await callback.message.answer(
            "❌ Произошла ошибка при создании платежа. Попробуйте позже.",
            parse_mode="HTML"
        )
        await callback.answer()


@dp.callback_query(F.data == "cancel_payment")
async def cancel_payment_callback(callback: types.CallbackQuery):
    """Обработка отмены платежа"""
    await callback.answer("Платеж отменен", show_alert=False)
    await callback.message.edit_text(
        "❌ <b>Платеж отменен</b>\n\n"
        "Вы можете вернуться к выбору подписки или дополнительных попыток.",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "subscription_info")
async def subscription_info_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Обработка кнопки информации о подписке"""
    text = (
        "ℹ️ <b>О подписке</b>\n\n"
        "<b>Бесплатный тариф:</b>\n"
        f"• {MAX_REQUESTS_PER_USER} запросов\n"
        "• Лимит сбрасывается при перезапуске бота\n\n"
        "<b>💎 Премиум подписка:</b>\n"
        "• Безлимитное количество запросов на период\n"
        "• Детальные развернутые сценарии с таймингами и реквизитами\n"
        "• История всех созданных сценариев (/my_scenarios)\n"
        "• Расширенные настройки (тон, длительность, платформа)\n"
        "• Приоритетная обработка\n"
        "• Приоритетная поддержка\n"
        "• Доступ ко всем функциям\n\n"
        "<b>Тарифы подписки:</b>\n"
        f"• 1 месяц: {SUBSCRIPTION_PRICE_1_MONTH / 100:.0f} ₽\n"
        f"• 3 месяца: {SUBSCRIPTION_PRICE_3_MONTHS / 100:.0f} ₽ (экономия ~11%)\n"
        f"• 6 месяцев: {SUBSCRIPTION_PRICE_6_MONTHS / 100:.0f} ₽ (экономия ~16%)\n"
        f"• 1 год: {SUBSCRIPTION_PRICE_1_YEAR / 100:.0f} ₽ (экономия ~25%)\n\n"
        "<b>🎯 Дополнительные попытки:</b>\n"
        "• Пополняемый баланс попыток\n"
        "• Не сгорают, накапливаются\n"
        "• Используются после бесплатных\n\n"
        "<b>Пакеты попыток:</b>\n"
        f"• 1 попытка: {EXTRA_REQUEST_PRICE / 100:.0f} ₽\n"
        f"• 10 попыток: {EXTRA_REQUESTS_PACK_10 / 100:.0f} ₽ (экономия 20%)\n"
        f"• 25 попыток: {EXTRA_REQUESTS_PACK_25 / 100:.0f} ₽ (экономия 28%)\n"
        f"• 50 попыток: {EXTRA_REQUESTS_PACK_50 / 100:.0f} ₽ (экономия 36%)\n"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    """Обработка pre-checkout запроса (перед оплатой)"""
    payload = pre_checkout_query.invoice_payload
    user_id = pre_checkout_query.from_user.id
    total_amount = pre_checkout_query.total_amount
    
    logger.info(f"Pre-checkout запрос: пользователь {user_id}, payload: {payload}, сумма: {total_amount/100}₽")
    
    # Проверяем, что payload соответствует ожидаемым форматам
    if payload.startswith("subscription_") or payload.startswith("extra_requests_"):
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        logger.info(f"Pre-checkout подтвержден для пользователя {user_id}")
    else:
        logger.warning(f"Неизвестный payload в pre-checkout: {payload} от пользователя {user_id}")
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неизвестный платеж")


@dp.message(F.content_type == "successful_payment")
async def successful_payment_handler(message: types.Message):
    """Обработка успешной оплаты"""
    user_id = message.from_user.id
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    logger.info(f"Получен успешный платеж: пользователь {user_id}, payload: {payload}, сумма: {payment.total_amount/100}₽, валюта: {payment.currency}, провайдер: {payment.provider_payment_charge_id if hasattr(payment, 'provider_payment_charge_id') else 'N/A'}")
    
    try:
        if payload.startswith("subscription_"):
            parts = payload.split("_")
            # Валидация: проверяем формат payload и извлекаем период безопасно
            if len(parts) >= 3:
                try:
                    period_months = int(parts[2])
                    # Ограничиваем максимальный период для безопасности
                    if period_months < 1 or period_months > 12:
                        period_months = 1
                except (ValueError, IndexError):
                    period_months = 1
            else:
                period_months = 1
            
            duration_days = period_months * 30
            
            logger.info(f"Активация подписки: пользователь {user_id}, период {period_months} месяцев ({duration_days} дней)")
            await SubscriptionManager.activate_subscription(user_id, duration_days)
            await LimitsManager.reset_user_requests(user_id)
            
            subscription_info = await SubscriptionManager.get_subscription_info(user_id)
            expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y")
            logger.info(f"Подписка активирована: пользователь {user_id}, действует до {expires_at}")
            
            text = (
                "🎉 <b>Спасибо за покупку!</b>\n\n"
                "✅ <b>Премиум подписка активирована!</b>\n\n"
                f"📅 <b>Действует до:</b> {expires_at}\n"
                f"⏰ <b>Период:</b> {period_months} {'месяц' if period_months == 1 else 'месяца' if period_months < 5 else 'месяцев'}\n\n"
                "Теперь у вас безлимитное количество запросов!\n"
                "Можете создавать сколько угодно сценариев! 🚀"
            )
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")]
                ]
            )
            
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
        elif payload.startswith("extra_requests_"):
            parts = payload.split("_")
            # Валидация: проверяем формат payload и извлекаем количество безопасно
            if len(parts) >= 3:
                try:
                    count = int(parts[2])
                    # Ограничиваем максимальное количество для безопасности
                    if count < 1 or count > 1000:
                        count = 1
                except (ValueError, IndexError):
                    count = 1
            else:
                count = 1
            
            logger.info(f"Добавление дополнительных попыток: пользователь {user_id}, количество {count}")
            await Database.add_extra_requests(user_id, count)
            total_extra = await Database.get_extra_requests_count(user_id)
            logger.info(f"Дополнительные попытки добавлены: пользователь {user_id}, всего попыток: {total_extra}")
            
            text = (
                "🎉 <b>Спасибо за покупку!</b>\n\n"
                f"✅ <b>Добавлено {count} дополнительных попыток!</b>\n\n"
                f"📊 <b>Всего дополнительных попыток:</b> {total_extra}\n\n"
                "💡 <b>Как это работает:</b>\n"
                "• Дополнительные попытки используются после исчерпания бесплатных\n"
                "• Они не сгорают и накапливаются\n"
                "• Можно докупать в любое время\n\n"
                "Можете продолжать создавать сценарии! 🚀"
            )
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")]
                ]
            )
            
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            logger.warning(f"Неизвестный формат payload платежа: {payload} от пользователя {user_id}")
            await message.answer(
                "⚠️ <b>Ошибка обработки платежа</b>\n\n"
                "Обратитесь в поддержку: /support",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Ошибка при обработке платежа для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Произошла ошибка при обработке платежа</b>\n\n"
            "Пожалуйста, обратитесь в поддержку: /support\n"
            "Мы обязательно решим эту проблему!",
            parse_mode="HTML"
        )


async def ask_for_niche(message: types.Message):
    """Спросить пользователя о нише"""
    text = "🎬 <b>Создание нового сценария</b>\n\n"
    text += "📋 <b>Шаг 1/4: Выбери нишу контента</b>\n\n"
    text += "Выбери нишу для твоего сценария:"
    
    await message.answer(text, parse_mode="HTML", reply_markup=get_niche_keyboard())


@dp.callback_query(F.data.startswith("niche_"))
async def process_niche(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора ниши"""
    # Всегда отвечаем на callback в начале, чтобы Telegram знал, что он обработан
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        niche = callback.data.replace("niche_", "")
        await state.update_data(niche=niche)
        
        user_id = callback.from_user.id
        # Убеждаемся, что пользователь зарегистрирован в БД
        await Database.register_user(user_id)
        
        text = (
            f"✅ Ниша: <b>{niche}</b>\n\n"
            "⏱️ <b>Шаг 2/4: Выбери формат видео</b>\n\n"
            "Какой длительности будет твое видео?"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_format_keyboard())
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора ниши: {e}", exc_info=True)
        try:
            await callback.message.answer(
                "❌ Произошла ошибка при выборе ниши. Попробуйте еще раз: /new",
                parse_mode="HTML"
            )
        except:
            pass


@dp.callback_query(F.data.startswith("format_"))
async def process_format(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора формата"""
    # Всегда отвечаем на callback, чтобы Telegram знал, что он обработан
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        format_type = callback.data.replace("format_", "")
        await state.update_data(format_type=format_type)
        
        data = await state.get_data()
        text = (
            f"✅ Ниша: <b>{data.get('niche')}</b>\n"
            f"✅ Формат: <b>{format_type}</b>\n\n"
            "🎨 <b>Шаг 3/4: Выбери стиль сценария</b>\n\n"
            "Какой стиль тебе больше подходит?"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_style_keyboard())
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора формата: {e}", exc_info=True)
        try:
            await callback.message.answer(
                "❌ Произошла ошибка при выборе формата. Попробуйте еще раз: /new",
                parse_mode="HTML"
            )
        except:
            pass


@dp.callback_query(F.data.startswith("style_"))
async def process_style(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора стиля"""
    # Всегда отвечаем на callback, чтобы Telegram знал, что он обработан
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        style = callback.data.replace("style_", "")
        await state.update_data(style=style)
        
        user_id = callback.from_user.id
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        
        data = await state.get_data()
        
        # Для Premium пользователей предлагаем сначала выбор шаблона, затем расширенные настройки
        if is_premium:
            text = (
                f"✅ Ниша: <b>{data.get('niche')}</b>\n"
                f"✅ Формат: <b>{data.get('format_type')}</b>\n"
                f"✅ Стиль: <b>{style}</b>\n\n"
                "💎 <b>Премиум настройки</b>\n\n"
                "📋 <b>Выбери шаблон сценария (опционально):</b>\n\n"
                "Шаблоны помогают структурировать контент по проверенным форматам."
            )
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await get_template_keyboard(user_id))
            await state.set_state(ScenarioStates.waiting_for_template)
        else:
            # Обычный путь для бесплатных пользователей
            text = (
                f"✅ Ниша: <b>{data.get('niche')}</b>\n"
                f"✅ Формат: <b>{data.get('format_type')}</b>\n"
                f"✅ Стиль: <b>{style}</b>\n\n"
                "📝 <b>Шаг 4/4: Укажи тему (опционально)</b>\n\n"
                "Напиши конкретную тему для сценария, или отправь /skip чтобы пропустить.\n"
                "Примеры: \"5 способов стать продуктивнее\", \"Как начать свой бизнес\", \"Смешные истории из жизни\""
            )
            await callback.message.edit_text(text, parse_mode="HTML")
            await state.set_state(ScenarioStates.waiting_for_topic)
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора стиля: {e}", exc_info=True)
        try:
            await callback.message.answer(
                "❌ Произошла ошибка при выборе стиля. Попробуйте еще раз: /new",
                parse_mode="HTML"
            )
        except:
            pass


@dp.callback_query(F.data == "create_template")
async def create_template_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обработка создания нового шаблона"""
    logger.info(f"[TEMPLATE] Обработчик create_template вызван для пользователя {callback.from_user.id}")
    try:
        await callback.answer()
    except Exception as e:
        logger.warning(f"[TEMPLATE] Ошибка при ответе на callback: {e}")
    
    try:
        user_id = callback.from_user.id
        active_users.add(user_id)
        logger.info(f"[TEMPLATE] Начало создания шаблона для пользователя {user_id}")
        
        # Проверяем Premium
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        if not is_premium:
            await callback.answer("💎 Создание шаблонов доступно только для Premium", show_alert=True)
            return
        
        # Сохраняем текущий контекст создания сценария для возврата
        data = await state.get_data()
        await state.update_data(_saved_scenario_context=data)
        
        # Переходим к созданию шаблона
        text = (
            "➕ <b>Создание своего шаблона</b>\n\n"
            "Шаблон поможет структурировать будущие сценарии по твоему формату.\n\n"
            "📝 <b>Шаг 1/3: Название шаблона</b>\n\n"
            "Придумай название для своего шаблона (например: \"Мой формат\", \"Продающий пост\", \"Обучающее видео\"):"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_template_creation")]
            ]
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await state.set_state(ScenarioStates.waiting_for_template_name)
        logger.info(f"[TEMPLATE] Переход в состояние waiting_for_template_name для пользователя {user_id}")
        
    except Exception as e:
        logger.error(f"[TEMPLATE] Ошибка при создании шаблона: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("template_"))
async def process_template(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора шаблона (Premium)"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        user_id = callback.from_user.id
        template_data = callback.data.replace("template_", "")
        
        # Игнорируем separator
        if template_data == "separator":
            await callback.answer()
            return
        
        # Если выбран "Без шаблона", пропускаем шаблон
        if template_data == "none":
            template_id = None
            template_name = "Без шаблона"
        # Если выбран пользовательский шаблон
        elif template_data.startswith("user_"):
            user_template_id = int(template_data.replace("user_", ""))
            user_template = await Database.get_user_template(user_template_id, user_id)
            if user_template:
                template_id = f"user_{user_template_id}"
                template_name = user_template['name']
                await state.update_data(template_id=template_id, template_prompt_modifier=user_template['prompt_modifier'])
            else:
                await callback.answer("❌ Шаблон не найден", show_alert=True)
                return
        else:
            # Стандартный шаблон
            template_info = get_template_info(template_data)
            if template_info:
                template_name = template_info.get("name", template_data)
                template_id = template_data
                await state.update_data(template_id=template_id)
            else:
                template_name = "Без шаблона"
                template_id = None
        
        data = await state.get_data()
        
        # Переходим к выбору тона
        text = (
            f"✅ Ниша: <b>{data.get('niche')}</b>\n"
            f"✅ Формат: <b>{data.get('format_type')}</b>\n"
            f"✅ Стиль: <b>{data.get('style')}</b>\n"
        )
        if template_id:
            text += f"✅ Шаблон: <b>{template_name}</b>\n"
        text += "\n💎 <b>Премиум настройки</b>\n\n"
        text += "🎭 <b>Выбери тон подачи:</b>"
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_tone_keyboard())
        await state.set_state(ScenarioStates.waiting_for_tone)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора шаблона: {e}", exc_info=True)


@dp.callback_query(F.data.startswith("tone_"))
async def process_tone(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора тона (Premium)"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        tone = callback.data.replace("tone_", "")
        await state.update_data(tone=tone)
        
        data = await state.get_data()
        text = (
            f"✅ Ниша: <b>{data.get('niche')}</b>\n"
            f"✅ Формат: <b>{data.get('format_type')}</b>\n"
            f"✅ Стиль: <b>{data.get('style')}</b>\n"
            f"✅ Тон: <b>{tone}</b>\n\n"
            "⏱️ <b>Выбери длительность:</b>"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_duration_keyboard())
        await state.set_state(ScenarioStates.waiting_for_duration)
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора тона: {e}", exc_info=True)


@dp.callback_query(F.data.startswith("duration_"))
async def process_duration(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора длительности (Premium)"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        duration = callback.data.replace("duration_", "")
        await state.update_data(duration=duration)
        
        data = await state.get_data()
        text = (
            f"✅ Ниша: <b>{data.get('niche')}</b>\n"
            f"✅ Формат: <b>{data.get('format_type')}</b>\n"
            f"✅ Стиль: <b>{data.get('style')}</b>\n"
            f"✅ Тон: <b>{data.get('tone')}</b>\n"
            f"✅ Длительность: <b>{duration}</b>\n\n"
            "📱 <b>Выбери платформу:</b>"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_platform_keyboard())
        await state.set_state(ScenarioStates.waiting_for_platform)
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора длительности: {e}", exc_info=True)


@dp.callback_query(F.data.startswith("platform_"))
async def process_platform(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора платформы (Premium)"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        active_users.add(callback.from_user.id)
        platform = callback.data.replace("platform_", "")
        await state.update_data(platform=platform)
        
        data = await state.get_data()
        text = (
            f"✅ Ниша: <b>{data.get('niche')}</b>\n"
            f"✅ Формат: <b>{data.get('format_type')}</b>\n"
            f"✅ Стиль: <b>{data.get('style')}</b>\n"
            f"✅ Тон: <b>{data.get('tone')}</b>\n"
            f"✅ Длительность: <b>{data.get('duration')}</b>\n"
            f"✅ Платформа: <b>{platform}</b>\n\n"
            "📝 <b>Укажи тему (опционально)</b>\n\n"
            "Напиши конкретную тему для сценария, или отправь /skip чтобы пропустить.\n"
            "Примеры: \"5 способов стать продуктивнее\", \"Как начать свой бизнес\", \"Смешные истории из жизни\""
        )
        await callback.message.edit_text(text, parse_mode="HTML")
        await state.set_state(ScenarioStates.waiting_for_topic)
    except Exception as e:
        logger.error(f"Ошибка при обработке выбора платформы: {e}", exc_info=True)


# Обработчики для создания шаблона
@dp.callback_query(F.data == "cancel_template_creation")
async def cancel_template_creation_callback(callback: types.CallbackQuery, state: FSMContext):
    """Отмена создания шаблона"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        # Восстанавливаем контекст создания сценария
        data = await state.get_data()
        saved_context = data.get('_saved_scenario_context', {})
        
        # Очищаем временные данные
        await state.update_data(_saved_scenario_context=None)
        
        # Возвращаемся к выбору шаблона
        user_id = callback.from_user.id
        text = (
            f"✅ Ниша: <b>{saved_context.get('niche', 'Не указана')}</b>\n"
            f"✅ Формат: <b>{saved_context.get('format_type', 'Не указан')}</b>\n"
            f"✅ Стиль: <b>{saved_context.get('style', 'Не указан')}</b>\n\n"
            "💎 <b>Премиум настройки</b>\n\n"
            "📋 <b>Выбери шаблон сценария (опционально):</b>\n\n"
            "Шаблоны помогают структурировать контент по проверенным форматам."
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await get_template_keyboard(user_id))
        await state.set_state(ScenarioStates.waiting_for_template)
        await state.update_data(**saved_context)
        
    except Exception as e:
        logger.error(f"Ошибка при отмене создания шаблона: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.message(StateFilter(ScenarioStates.waiting_for_template_name))
async def process_template_name(message: types.Message, state: FSMContext):
    """Обработка названия шаблона"""
    active_users.add(message.from_user.id)
    
    if not message.text:
        return
    
    if message.text.startswith('/'):
        return
    
    template_name = message.text.strip()
    
    if len(template_name) > 50:
        await message.answer("❌ Название слишком длинное (максимум 50 символов). Попробуй еще раз:")
        return
    
    await state.update_data(template_name=template_name)
    
    text = (
        f"✅ Название: <b>{template_name}</b>\n\n"
        "📝 <b>Шаг 2/3: Описание шаблона</b>\n\n"
        "Напиши краткое описание шаблона (опционально, для чего он используется):\n\n"
        "Или отправь /skip чтобы пропустить."
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_template_creation")]
        ]
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(ScenarioStates.waiting_for_template_description)


@dp.message(StateFilter(ScenarioStates.waiting_for_template_description), Command("skip"))
async def skip_template_description(message: types.Message, state: FSMContext):
    """Пропуск описания шаблона"""
    data = await state.get_data()
    template_name = data.get('template_name')
    
    text = (
        f"✅ Название: <b>{template_name}</b>\n\n"
        "📝 <b>Шаг 3/3: Промпт для шаблона</b>\n\n"
        "Опиши, как должен выглядеть сценарий по этому шаблону. Это будут инструкции для AI.\n\n"
        "<b>Примеры:</b>\n"
        "• \"Создай сценарий в формате 'Проблема-Решение'. Сначала покажи проблему, затем предложи решение.\"\n"
        "• \"Сценарий должен быть списком из 5 пунктов. Каждый пункт - отдельный кадр с визуальным примером.\"\n\n"
        "Опиши структуру и особенности твоего шаблона:"
    )
    
    await message.answer(text, parse_mode="HTML")
    await state.set_state(ScenarioStates.waiting_for_template_prompt)


@dp.message(StateFilter(ScenarioStates.waiting_for_template_description))
async def process_template_description(message: types.Message, state: FSMContext):
    """Обработка описания шаблона"""
    active_users.add(message.from_user.id)
    
    if not message.text:
        return
    
    if message.text.startswith('/'):
        return
    
    template_description = message.text.strip()
    await state.update_data(template_description=template_description)
    
    data = await state.get_data()
    template_name = data.get('template_name')
    
    text = (
        f"✅ Название: <b>{template_name}</b>\n"
        f"✅ Описание: <b>{template_description}</b>\n\n"
        "📝 <b>Шаг 3/3: Промпт для шаблона</b>\n\n"
        "Опиши, как должен выглядеть сценарий по этому шаблону. Это будут инструкции для AI.\n\n"
        "<b>Примеры:</b>\n"
        "• \"Создай сценарий в формате 'Проблема-Решение'. Сначала покажи проблему, затем предложи решение.\"\n"
        "• \"Сценарий должен быть списком из 5 пунктов. Каждый пункт - отдельный кадр с визуальным примером.\"\n\n"
        "Опиши структуру и особенности твоего шаблона:"
    )
    
    await message.answer(text, parse_mode="HTML")
    await state.set_state(ScenarioStates.waiting_for_template_prompt)


@dp.message(StateFilter(ScenarioStates.waiting_for_template_prompt))
async def process_template_prompt(message: types.Message, state: FSMContext):
    """Обработка промпта шаблона и сохранение"""
    active_users.add(message.from_user.id)
    
    if not message.text:
        return
    
    if message.text.startswith('/'):
        return
    
    user_id = message.from_user.id
    template_prompt = message.text.strip()
    
    if len(template_prompt) < 20:
        await message.answer("❌ Промпт слишком короткий (минимум 20 символов). Опиши подробнее:")
        return
    
    if len(template_prompt) > 1000:
        await message.answer("❌ Промпт слишком длинный (максимум 1000 символов). Сократи:")
        return
    
    data = await state.get_data()
    template_name = data.get('template_name')
    template_description = data.get('template_description', '')
    
    try:
        # Сохраняем шаблон
        template_id = await Database.save_user_template(
            user_id=user_id,
            name=template_name,
            description=template_description,
            prompt_modifier=template_prompt
        )
        
        # Восстанавливаем контекст создания сценария
        saved_context = data.get('_saved_scenario_context', {})
        
        # Очищаем временные данные
        await state.update_data(
            template_name=None,
            template_description=None,
            template_prompt=None,
            _saved_scenario_context=None
        )
        
        success_text = (
            "✅ <b>Шаблон создан!</b>\n\n"
            f"<b>Название:</b> {template_name}\n"
            f"<b>ID:</b> #{template_id}\n\n"
            "Теперь ты можешь использовать этот шаблон при создании сценариев!"
        )
        
        await message.answer(success_text, parse_mode="HTML")
        
        # Возвращаемся к выбору шаблона (теперь там будет новый шаблон)
        text = (
            f"✅ Ниша: <b>{saved_context.get('niche', 'Не указана')}</b>\n"
            f"✅ Формат: <b>{saved_context.get('format_type', 'Не указан')}</b>\n"
            f"✅ Стиль: <b>{saved_context.get('style', 'Не указан')}</b>\n\n"
            "💎 <b>Премиум настройки</b>\n\n"
            "📋 <b>Выбери шаблон сценария (опционально):</b>\n\n"
            "Шаблоны помогают структурировать контент по проверенным форматам."
        )
        
        await message.answer(text, parse_mode="HTML", reply_markup=await get_template_keyboard(user_id))
        await state.set_state(ScenarioStates.waiting_for_template)
        await state.update_data(**saved_context)
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении шаблона для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при создании шаблона</b>\n\n"
            "Попробуй позже или обратись в поддержку: /support",
            parse_mode="HTML"
        )


@dp.message(Command("my_templates"))
async def cmd_my_templates(message: types.Message):
    """Просмотр и управление пользовательскими шаблонами"""
    user_id = message.from_user.id
    active_users.add(user_id)
    
    # Проверяем Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        text = (
            "💎 <b>Управление шаблонами доступно только для Premium подписчиков</b>\n\n"
            "Оформи подписку: /subscribe"
        )
        await message.answer(text, parse_mode="HTML")
        return
    
    try:
        templates = await Database.get_user_templates(user_id)
        
        if not templates:
            text = (
                "📋 <b>Мои шаблоны</b>\n\n"
                "У тебя пока нет созданных шаблонов.\n\n"
                "💡 <b>Как создать шаблон:</b>\n"
                "1. Начни создание сценария (/new)\n"
                "2. При выборе шаблона нажми \"➕ Создать свой\"\n"
                "3. Следуй инструкциям"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")],
                    [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_main")]
                ]
            )
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
            return
        
        text = f"📋 <b>Мои шаблоны</b>\n\nВсего шаблонов: <b>{len(templates)}</b>\n\n"
        
        keyboard_buttons = []
        for template in templates[:10]:  # Показываем до 10 шаблонов
            created_at = template['created_at'].strftime("%d.%m.%Y") if template.get('created_at') else "Дата неизвестна"
            name = template['name']
            text += f"<b>{name}</b> (ID: #{template['id']})\n"
            if template.get('description'):
                text += f"   {template['description'][:50]}{'...' if len(template.get('description', '')) > 50 else ''}\n"
            text += f"   Создан: {created_at}\n\n"
            
            keyboard_buttons.append([
                InlineKeyboardButton(text=f"🗑️ {name}", callback_data=f"delete_template_{template['id']}")
            ])
        
        if len(templates) > 10:
            text += f"И еще {len(templates) - 10} шаблонов...\n\n"
        
        text += "Используй кнопки ниже для управления:"
        
        keyboard_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при получении шаблонов для пользователя {user_id}: {e}", exc_info=True)
        await message.answer(
            "❌ <b>Ошибка при загрузке шаблонов</b>\n\n"
            "Попробуй позже или обратись в поддержку: /support",
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("delete_template_"))
async def delete_template_callback(callback: types.CallbackQuery):
    """Удаление шаблона"""
    try:
        await callback.answer()
    except:
        pass
    
    try:
        user_id = callback.from_user.id
        
        # Проверяем Premium
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        if not is_premium:
            await callback.answer("💎 Доступно только для Premium", show_alert=True)
            return
        
        template_id_str = callback.data.replace("delete_template_", "")
        template_id = int(template_id_str)
        
        # Проверяем, существует ли шаблон и принадлежит ли он пользователю
        template = await Database.get_user_template(template_id, user_id)
        if not template:
            await callback.answer("❌ Шаблон не найден", show_alert=True)
            return
        
        # Удаляем шаблон
        success = await Database.delete_user_template(template_id, user_id)
        
        if success:
            await callback.answer(f"✅ Шаблон '{template['name']}' удален", show_alert=True)
            # Обновляем список шаблонов
            await cmd_my_templates(callback.message)
        else:
            await callback.answer("❌ Ошибка при удалении", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка при удалении шаблона: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.message(StateFilter(ScenarioStates.waiting_for_topic), Command("skip"))
async def skip_topic(message: types.Message, state: FSMContext):
    """Пропуск темы"""
    await generate_and_send_scenario(message, state)


@dp.message(StateFilter(ScenarioStates.waiting_for_topic))
async def process_topic(message: types.Message, state: FSMContext):
    active_users.add(message.from_user.id)
    """Обработка темы"""
    
    if not message.text:
        return
    
    if message.text.startswith('/'):
        return
    
    main_keyboard_buttons = [
        "🎬 Создать сценарий",
        "💎 Подписка",
        "ℹ️ Помощь",
        "💬 Поддержка"
    ]
    
    if message.text in main_keyboard_buttons:
        return
    
    topic = message.text
    await state.update_data(topic=topic)
    await generate_and_send_scenario(message, state)


async def generate_and_send_scenario(message: types.Message, state: FSMContext):
    """Генерация и отправка сценария"""
    user_id = message.from_user.id
    status_msg = None
    
    try:
        data = await state.get_data()
        
        niche = data.get('niche') or 'общее'
        style = data.get('style') or 'динамичный'
        format_type = data.get('format_type', '60 секунд')
        topic = data.get('topic')
        
        # Получаем паттерны редактирования пользователя
        user_patterns = await Database.get_user_editing_patterns(user_id)
        
        # Проверяем, является ли пользователь Premium
        is_premium = await SubscriptionManager.has_active_subscription(user_id)
        
        # Получаем расширенные настройки для Premium пользователей
        tone = data.get('tone') if is_premium else None
        duration = data.get('duration') if is_premium else None
        platform = data.get('platform') if is_premium else None
        template_id = data.get('template_id') if is_premium else None
        template_prompt_modifier = data.get('template_prompt_modifier') if is_premium else None
        
        # Определяем текст статусного сообщения в зависимости от приоритета
        if is_premium:
            status_msg = await message.answer("⚡ Генерирую сценарий с приоритетом Premium... Это может занять несколько секунд.")
        else:
            status_msg = await message.answer("⏳ Генерирую сценарий... Это может занять несколько секунд.")
        
        # Используем приоритетную очередь для Premium пользователей
        scenario = None
        if is_premium:
            # Premium: используем приоритетную очередь
            async def handle_scenario_result(result: str):
                nonlocal scenario
                scenario = result
            
            def generate_sync():
                return scenario_generator.generate_scenario(
                    niche=niche,
                    format_type=format_type,
                    style=style,
                    topic=topic,
                    additional_info=None,
                    user_patterns=user_patterns,
                    is_premium=is_premium,
                    tone=tone,
                    duration=duration,
                    platform=platform,
                    template_id=template_id,
                    template_prompt_modifier=template_prompt_modifier
                )
            
            try:
                task_id = await _priority_queue.add_task(
                    user_id=user_id,
                    is_premium=True,
                    generator_func=generate_sync,
                    callback=handle_scenario_result
                )
                logger.info(f"Задача {task_id} добавлена в приоритетную очередь для Premium пользователя {user_id}")
                
                # Ждем результат с таймаутом
                timeout = 90.0  # Больше времени для Premium
                start_time = asyncio.get_event_loop().time()
                while scenario is None:
                    await asyncio.sleep(0.5)
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        scenario = "⏳ <b>Превышено время ожидания</b>\n\nГенерация сценария заняла слишком много времени. Попробуйте еще раз."
                        logger.warning(f"Таймаут при генерации сценария для Premium пользователя {user_id}")
                        break
            except Exception as e:
                logger.error(f"Ошибка при добавлении задачи в очередь для Premium пользователя {user_id}: {e}", exc_info=True)
                scenario = f"❌ <b>Ошибка при генерации сценария</b>\n\nПроизошла неожиданная ошибка. Попробуйте позже.\n\nДетали: {str(e)[:200]}"
        else:
            # Free: используем обычный executor
            try:
                scenario = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        _scenario_executor,
                        lambda: scenario_generator.generate_scenario(
                            niche=niche,
                            format_type=format_type,
                            style=style,
                            topic=topic,
                            additional_info=None,
                            user_patterns=user_patterns,
                            is_premium=is_premium,
                            tone=tone,
                            duration=duration,
                            platform=platform,
                            template_id=template_id,
                            template_prompt_modifier=template_prompt_modifier
                        )
                    ),
                    timeout=60.0
                )
            except asyncio.TimeoutError:
                scenario = "⏳ <b>Превышено время ожидания</b>\n\nГенерация сценария заняла слишком много времени. Попробуйте еще раз."
                logger.warning(f"Таймаут при генерации сценария для пользователя {user_id}")
            except Exception as e:
                logger.error(f"Ошибка при генерации сценария для пользователя {user_id}: {e}", exc_info=True)
                scenario = f"❌ <b>Ошибка при генерации сценария</b>\n\nПроизошла неожиданная ошибка. Попробуйте позже.\n\nДетали: {str(e)[:200]}"
        
        try:
            await status_msg.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить статусное сообщение: {e}")
        
        is_error = scenario.startswith(("⚠️", "❌", "🔑", "🌐", "⏳"))
        
        if is_error:
            await message.answer(scenario, parse_mode="HTML")
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="new_scenario")]
                ]
            )
            await message.answer("Попробуй создать сценарий снова после решения проблемы.", reply_markup=keyboard)
            
            # Очищаем состояние FSM при ошибке
            await state.set_state(None)
        else:
            response_text = (
                "🎬 <b>Твой сценарий готов!</b>\n\n"
                f"<b>Ниша:</b> {data.get('niche')}\n"
                f"<b>Формат:</b> {data.get('format_type')}\n"
                f"<b>Стиль:</b> {data.get('style')}\n"
            )
            if data.get('topic'):
                response_text += f"<b>Тема:</b> {data.get('topic')}\n"
            response_text += "\n" + "="*30 + "\n\n"
            scenario_cleaned = await asyncio.get_event_loop().run_in_executor(
                _scenario_executor,
                lambda: remove_markdown(scenario)
            )
            response_text += scenario_cleaned
            
            if len(response_text) > 4096:
                await message.answer(response_text[:4096], parse_mode="HTML")
                await message.answer(response_text[4096:], parse_mode="HTML")
            else:
                await message.answer(response_text, parse_mode="HTML")
            
            await state.update_data(last_scenario=scenario)
            
            # Сохраняем сценарий в историю для Premium пользователей
            saved_scenario_id = None
            if is_premium:
                try:
                    saved_scenario_id = await Database.save_user_scenario(
                        user_id=user_id,
                        scenario_text=scenario,
                        niche=niche,
                        format_type=format_type,
                        style=style,
                        tone=tone,
                        duration=duration,
                        platform=platform,
                        topic=topic,
                        is_premium=True
                    )
                    logger.info(f"Сценарий сохранен в историю для Premium пользователя {user_id}, ID: {saved_scenario_id}")
                except Exception as e:
                    logger.error(f"Ошибка при сохранении сценария в историю для пользователя {user_id}: {e}", exc_info=True)
            
            # Сохраняем статистику генерации
            try:
                await Database.save_scenario_statistics(
                    user_id=user_id,
                    niche=niche,
                    format_type=format_type,
                    style=style
                )
            except Exception as e:
                logger.warning(f"Не удалось сохранить статистику для пользователя {user_id}: {e}")
            
            remaining = await LimitsManager.get_remaining_requests(message.from_user.id)
            limits_text = ""
            if remaining == -1:
                if LimitsManager.is_developer(message.from_user.id):
                    limits_text = "\n\n✅ У тебя безлимитный доступ (разработчик)"
                else:
                    limits_text = "\n\n💎 У тебя премиум подписка - безлимит!"
            else:
                limits_text = f"\n\n📊 Осталось запросов: {remaining}"
                if remaining <= 3:
                    limits_text += "\n💎 Оформи подписку для безлимита: /subscribe"
            
            # Формируем клавиатуру с учетом Premium статуса
            keyboard_buttons = [
                [
                    InlineKeyboardButton(text="✨ Развить идею", callback_data="improve_scenario"),
                    InlineKeyboardButton(text="🔄 Новый сценарий", callback_data="new_scenario")
                ]
            ]
            
            # Добавляем кнопку экспорта для Premium пользователей (если сценарий сохранен)
            if is_premium and saved_scenario_id:
                keyboard_buttons.insert(0, [
                    InlineKeyboardButton(text="📥 Экспорт", callback_data=f"export_scenario_{saved_scenario_id}")
                ])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await message.answer(f"Что дальше?{limits_text}", reply_markup=keyboard)
            
            active_users.add(message.from_user.id)
            await LimitsManager.increment_request(message.from_user.id, active_users)
            
            # Очищаем состояние FSM после успешной генерации
            await state.set_state(None)
    except Exception as e:
        logger.error(f"Критическая ошибка в generate_and_send_scenario для пользователя {user_id}: {e}", exc_info=True)
        try:
            await message.answer("❌ Произошла критическая ошибка при генерации сценария. Попробуйте позже.")
        except:
            pass
    finally:
        # Гарантируем очистку состояния FSM в любом случае
        try:
            current_state = await state.get_state()
            if current_state is not None:
                logger.info(f"Очистка состояния FSM для пользователя {user_id} (было: {current_state})")
                await state.set_state(None)
        except Exception as e:
            logger.error(f"Ошибка при очистке состояния FSM для пользователя {user_id}: {e}")
    


@dp.callback_query(F.data == "subscribe_channel")
async def subscribe_channel_callback(callback: types.CallbackQuery):
    """Обработка кнопки подписки на канал из меню подписки"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    subscribe_text = (
        "📢 <b>Подписка на канал</b>\n\n"
        "Подпишись на наш канал и получи <b>3 дополнительные попытки</b>! 🎁\n\n"
        f"👉 {REQUIRED_CHANNEL_URL}\n\n"
        "После подписки нажми кнопку <b>«Я подписался»</b> для проверки."
    )
    
    await callback.message.answer(
        subscribe_text,
        parse_mode="HTML",
        reply_markup=get_subscribe_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопки проверки подписки на канал"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    # Проверяем, получал ли уже пользователь бонус за подписку
    already_got_bonus = await Database.is_channel_subscribed(user_id)
    
    # Проверяем подписку
    is_subscribed = await check_channel_subscription(user_id)
    
    if is_subscribed:
        # Пользователь подписан - сохраняем статус
        if not already_got_bonus:
            # Начисляем 3 попытки за подписку (только один раз)
            await Database.add_extra_requests(user_id, 3)
            await Database.set_channel_subscribed(user_id, True)
            
            bonus_text = (
                "✅ <b>Отлично! Подписка подтверждена</b>\n\n"
                "🎁 <b>Ты получил 3 дополнительные попытки!</b>\n\n"
                "Теперь ты можешь создавать сценарии! 🎬"
            )
        else:
            bonus_text = (
                "✅ <b>Подписка подтверждена</b>\n\n"
                "Ты уже получал бонус за подписку ранее."
            )
        
        await callback.message.edit_text(bonus_text, parse_mode="HTML")
        await callback.answer("Подписка подтверждена! ✅")
        
        # Показываем сообщение о том, что можно продолжить
        await callback.message.answer(
            "✅ <b>Отлично! Ты получил 3 попытки!</b>\n\n"
            "Теперь можешь создать сценарий! Нажми кнопку «Создать сценарий» 🎬",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        # Пользователь не подписан
        await callback.answer("❌ Ты еще не подписан на канал. Пожалуйста, подпишись и попробуй снова.", show_alert=True)
        subscribe_text = (
            "❌ <b>Подписка не подтверждена</b>\n\n"
            "Пожалуйста, убедись, что ты подписался на канал:\n"
            f"👉 {REQUIRED_CHANNEL_URL}\n\n"
            "После подписки нажми кнопку <b>«Я подписался»</b> еще раз.\n\n"
            "💡 <b>Бонус:</b> За подписку ты получишь 3 дополнительные попытки!"
        )
        await callback.message.edit_text(
            subscribe_text,
            parse_mode="HTML",
            reply_markup=get_subscribe_keyboard()
        )


@dp.callback_query(F.data == "new_scenario")
async def new_scenario_callback(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка кнопки создания нового сценария"""
    can_request, error_msg = await LimitsManager.can_make_request(callback.from_user.id)
    if not can_request:
        await callback.message.answer(error_msg, parse_mode="HTML")
        await callback.answer()
        return
    
    await state.clear()
    await ask_for_niche(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "improve_scenario")
async def improve_scenario_callback(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка кнопки развития сценария"""
    can_request, error_msg = await LimitsManager.can_make_request(callback.from_user.id)
    if not can_request:
        await callback.message.answer(error_msg, parse_mode="HTML")
        await callback.answer()
        return
    
    data = await state.get_data()
    last_scenario = data.get('last_scenario')
    
    if not last_scenario:
        await callback.message.answer("❌ Не найден предыдущий сценарий. Создай новый сценарий сначала.")
        await callback.answer()
        return
    
    text = (
        "✨ <b>Развитие идеи</b>\n\n"
        "Напиши, как ты хочешь улучшить или доработать сценарий.\n\n"
        "Примеры:\n"
        "• \"Сделай более динамичным\"\n"
        "• \"Добавь больше юмора\"\n"
        "• \"Упрости текст\"\n"
        "• \"Сделай более драматичным\"\n"
        "• \"Добавь больше деталей в визуальные подсказки\"\n\n"
        "Или отправь /cancel чтобы отменить."
    )
    await callback.message.answer(text, parse_mode="HTML")
    await state.set_state(ScenarioStates.waiting_for_improvement)
    await callback.answer()


@dp.message(StateFilter(ScenarioStates.waiting_for_improvement), Command("cancel"))
async def cancel_improvement(message: types.Message, state: FSMContext):
    """Отмена улучшения сценария"""
    await state.set_state(None)
    await message.answer("❌ Развитие идеи отменено.", reply_markup=get_main_keyboard())


@dp.message(StateFilter(ScenarioStates.waiting_for_improvement))
async def process_improvement(message: types.Message, state: FSMContext):
    active_users.add(message.from_user.id)
    """Обработка запроса на улучшение сценария"""
    data = await state.get_data()
    last_scenario = data.get('last_scenario')
    improvement_request = message.text
    
    if not last_scenario:
        await message.answer("❌ Не найден предыдущий сценарий. Создай новый сценарий сначала.")
        await state.set_state(None)
        return
    
    status_msg = await message.answer("⏳ Улучшаю сценарий... Это может занять несколько секунд.")
    
    try:
        improved_scenario = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                _scenario_executor,
                lambda: scenario_generator.improve_scenario(last_scenario, improvement_request)
            ),
            timeout=60.0
        )
    except asyncio.TimeoutError:
        improved_scenario = "⏳ <b>Превышено время ожидания</b>\n\nУлучшение сценария заняло слишком много времени. Попробуйте еще раз."
    
    await status_msg.delete()
    
    is_error = improved_scenario.startswith(("⚠️", "❌", "🔑", "🌐", "⏳"))
    
    if is_error:
        await message.answer(improved_scenario, parse_mode="HTML")
        await state.set_state(None)
    else:
        await state.set_state(None)
        
        header_text = (
            "✨ <b>Улучшенный сценарий готов!</b>\n\n"
            f"<b>Твой запрос:</b> {improvement_request}"
        )
        await message.answer(header_text, parse_mode="HTML")
        
        improved_scenario_cleaned = await asyncio.get_event_loop().run_in_executor(
            _scenario_executor,
            lambda: remove_markdown(improved_scenario)
        )
        if len(improved_scenario_cleaned) > 4096:
            await message.answer(improved_scenario_cleaned[:4096], parse_mode="HTML")
            await message.answer(improved_scenario_cleaned[4096:], parse_mode="HTML")
        else:
            await message.answer(improved_scenario_cleaned, parse_mode="HTML")
        
        await state.update_data(last_scenario=improved_scenario_cleaned)
        
        # Сохраняем редактирование и анализируем паттерны
        try:
            user_id = message.from_user.id
            await Database.save_scenario_edit(
                user_id=user_id,
                original_scenario=last_scenario,
                improved_scenario=improved_scenario_cleaned,
                improvement_request=improvement_request
            )
            
            # Анализируем паттерны редактирования
            new_patterns = await asyncio.get_event_loop().run_in_executor(
                _scenario_executor,
                lambda: ScenarioGenerator.analyze_editing_patterns(
                    last_scenario,
                    improved_scenario_cleaned,
                    improvement_request
                )
            )
            
            # Получаем существующие паттерны и объединяем с новыми
            existing_patterns = await Database.get_user_editing_patterns(user_id)
            merged_patterns = await asyncio.get_event_loop().run_in_executor(
                _scenario_executor,
                lambda: ScenarioGenerator.merge_patterns(existing_patterns, new_patterns)
            )
            
            # Сохраняем обновленные паттерны
            await Database.save_editing_patterns(user_id, merged_patterns)
            logger.info(f"Сохранены паттерны редактирования для пользователя {user_id}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении паттернов редактирования: {e}", exc_info=True)
        
        remaining = await LimitsManager.get_remaining_requests(message.from_user.id)
        limits_text = ""
        if remaining == -1:
            if LimitsManager.is_developer(message.from_user.id):
                limits_text = "\n\n✅ У тебя безлимитный доступ (разработчик)"
            else:
                limits_text = "\n\n💎 У тебя премиум подписка - безлимит!"
        else:
            limits_text = f"\n\n📊 Осталось запросов: {remaining}"
            if remaining <= 3:
                limits_text += "\n💎 Оформи подписку для безлимита: /subscribe"
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✨ Развить еще", callback_data="improve_scenario"),
                    InlineKeyboardButton(text="🔄 Новый сценарий", callback_data="new_scenario")
                ]
            ]
        )
        await message.answer(f"Что дальше?{limits_text}", reply_markup=keyboard)
        
        active_users.add(message.from_user.id)
        await LimitsManager.increment_request(message.from_user.id, active_users)


@dp.message(Command("support"))
@dp.message(F.text == "💬 Поддержка")
async def cmd_support(message: types.Message, state: FSMContext):
    active_users.add(message.from_user.id)
    """Обработчик команды /support и кнопки поддержки"""
    support_text = (
        "💬 <b>Поддержка</b>\n\n"
        "Опиши свою проблему или вопрос, и мы обязательно поможем!\n\n"
        "Просто напиши сообщение, и оно будет отправлено в службу поддержки.\n\n"
        "Или отправь /cancel чтобы отменить."
    )
    await message.answer(support_text, parse_mode="HTML")
    await state.set_state(ScenarioStates.waiting_for_support_message)


@dp.message(StateFilter(ScenarioStates.waiting_for_support_message), Command("cancel"))
async def cancel_support(message: types.Message, state: FSMContext):
    """Отмена обращения в поддержку"""
    await state.set_state(None)
    await message.answer("❌ Обращение в поддержку отменено.", reply_markup=get_main_keyboard())


@dp.message(StateFilter(ScenarioStates.waiting_for_support_message))
async def process_support_message(message: types.Message, state: FSMContext):
    active_users.add(message.from_user.id)
    """Обработка сообщения пользователя в поддержку"""
    user_id = message.from_user.id
    user_name = message.from_user.full_name or f"User {user_id}"
    username = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    
    support_message = message.text or (message.caption if message.caption else "[Медиа-файл]")
    
    dev_message = (
        f"💬 <b>Новое обращение в поддержку</b>\n\n"
        f"<b>Пользователь:</b> {user_name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Сообщение:</b>\n{support_message}"
    )
    
    sent_count = 0
    send_tasks = []
    for dev_id in DEVELOPER_USER_IDS:
        try:
            send_tasks.append(bot.forward_message(
                chat_id=dev_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            ))
            send_tasks.append(bot.send_message(
                chat_id=dev_id,
                text=dev_message,
                parse_mode="HTML"
            ))
            sent_count += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения разработчику {dev_id}: {e}")
    
    if send_tasks:
        await asyncio.gather(*send_tasks, return_exceptions=True)
    
    if sent_count > 0:
        await message.answer(
            "✅ <b>Сообщение отправлено в поддержку!</b>\n\n"
            "Мы получили твое обращение и ответим в ближайшее время.\n"
            "Обычно мы отвечаем в течение нескольких часов.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "⚠️ <b>К сожалению, поддержка временно недоступна.</b>\n\n"
            "Попробуй позже или свяжись с администратором.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    
    await state.set_state(None)


@dp.message(F.reply_to_message)
async def handle_developer_reply(message: types.Message):
    """Обработка ответов разработчиков на сообщения пользователей"""
    if not LimitsManager.is_developer(message.from_user.id):
        return
    
    replied_message = message.reply_to_message
    
    if not replied_message:
        return
    
    if not replied_message.forward_from:
        if replied_message.text and "ID:" in replied_message.text:
            match = re.search(r'<code>(\d+)</code>', replied_message.text)
            if match:
                target_user_id = int(match.group(1))
            else:
                await message.answer(
                    "❌ Не удалось определить пользователя для ответа.\n"
                    "Используй команду /reply <user_id> <сообщение> для ответа."
                )
                return
        else:
            await message.answer(
                "❌ Это не пересланное сообщение от пользователя.\n"
                "Ответь на пересланное сообщение или используй /reply <user_id> <сообщение>"
            )
            return
    else:
        target_user_id = replied_message.forward_from.id
    
    reply_text = message.text or (message.caption if message.caption else "[Медиа-файл]")
    
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"💬 <b>Ответ от поддержки:</b>\n\n{reply_text}",
            parse_mode="HTML"
        )
        await message.answer("✅ Ответ отправлен пользователю!")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {target_user_id}: {e}")
        await message.answer(
            f"❌ Не удалось отправить ответ пользователю.\n"
            f"Возможно, пользователь заблокировал бота или удалил аккаунт.\n"
            f"Ошибка: {str(e)[:200]}"
        )


# ==================== ЭКСПОРТ СЦЕНАРИЕВ (PREMIUM) ====================

@dp.callback_query(F.data.startswith("export_scenario_"))
async def export_scenario_callback(callback: types.CallbackQuery):
    """Обработка экспорта сценария"""
    user_id = callback.from_user.id
    active_users.add(user_id)
    
    # Проверяем, является ли пользователь Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    
    if not is_premium:
        await callback.answer("💎 Экспорт доступен только для Premium подписчиков", show_alert=True)
        return
    
    try:
        # Извлекаем ID сценария
        scenario_id_str = callback.data.replace("export_scenario_", "")
        scenario_id = int(scenario_id_str)
        
        # Получаем сценарий из БД
        scenario_data = await Database.get_scenario_by_id(scenario_id, user_id)
        
        if not scenario_data:
            await callback.answer("❌ Сценарий не найден", show_alert=True)
            return
        
        # Показываем меню выбора формата экспорта
        text = (
            "📥 <b>Экспорт сценария</b>\n\n"
            f"Сценарий #{scenario_id}: {scenario_data.get('niche') or 'Без ниши'}\n\n"
            "Выбери формат экспорта:"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📄 Текст (.txt)", callback_data=f"export_txt_{scenario_id}"),
                    InlineKeyboardButton(text="🎬 Съемочный лист", callback_data=f"export_shooting_{scenario_id}")
                ],
                [
                    InlineKeyboardButton(text="📊 Таблица", callback_data=f"export_table_{scenario_id}"),
                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"view_scenario_{scenario_id}")
                ]
            ]
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка при экспорте сценария для пользователя {user_id}: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при экспорте", show_alert=True)


@dp.callback_query(F.data.startswith("export_txt_"))
async def export_txt_callback(callback: types.CallbackQuery):
    """Экспорт сценария в текстовый формат"""
    await _export_scenario(callback, "txt")


@dp.callback_query(F.data.startswith("export_shooting_"))
async def export_shooting_callback(callback: types.CallbackQuery):
    """Экспорт сценария в формат съемочного листа"""
    await _export_scenario(callback, "shooting")


@dp.callback_query(F.data.startswith("export_table_"))
async def export_table_callback(callback: types.CallbackQuery):
    """Экспорт сценария в табличный формат"""
    await _export_scenario(callback, "table")


async def _export_scenario(callback: types.CallbackQuery, export_format: str):
    """Общая функция для экспорта сценария"""
    user_id = callback.from_user.id
    
    # Проверяем Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        await callback.answer("💎 Экспорт доступен только для Premium", show_alert=True)
        return
    
    try:
        # Извлекаем ID сценария
        scenario_id_str = callback.data.replace(f"export_{export_format}_", "")
        scenario_id = int(scenario_id_str)
        
        # Получаем сценарий
        scenario_data = await Database.get_scenario_by_id(scenario_id, user_id)
        
        if not scenario_data:
            await callback.answer("❌ Сценарий не найден", show_alert=True)
            return
        
        # Генерируем файл в зависимости от формата
        if export_format == "txt":
            file_content = export_scenario_text(scenario_data)
            filename = f"scenario_{scenario_id}.txt"
            caption = f"📄 Сценарий #{scenario_id} (текстовый формат)"
        elif export_format == "shooting":
            file_content = export_scenario_shooting_list(scenario_data)
            filename = f"scenario_{scenario_id}_shooting_list.txt"
            caption = f"🎬 Съемочный лист #{scenario_id}"
        elif export_format == "table":
            file_content = export_scenario_table(scenario_data)
            filename = f"scenario_{scenario_id}_table.txt"
            caption = f"📊 Сценарий #{scenario_id} (таблица)"
        else:
            await callback.answer("❌ Неизвестный формат", show_alert=True)
            return
        
        # Создаем файл в памяти
        from io import BytesIO
        file_bytes = BytesIO(file_content.encode('utf-8'))
        file_data = file_bytes.read()
        
        # Отправляем файл
        await callback.message.answer_document(
            document=BufferedInputFile(
                file=file_data,
                filename=filename
            ),
            caption=caption
        )
        
        await callback.answer("✅ Файл отправлен!")
        
    except Exception as e:
        logger.error(f"Ошибка при экспорте сценария {export_format} для пользователя {user_id}: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при создании файла", show_alert=True)


@dp.callback_query(F.data.startswith("view_scenario_"))
async def view_scenario_from_callback(callback: types.CallbackQuery):
    """Просмотр сценария из callback (для кнопки "Назад" в экспорте)"""
    user_id = callback.from_user.id
    
    # Проверяем Premium
    is_premium = await SubscriptionManager.has_active_subscription(user_id)
    if not is_premium:
        await callback.answer("💎 Доступно только для Premium", show_alert=True)
        return
    
    try:
        scenario_id_str = callback.data.replace("view_scenario_", "")
        scenario_id = int(scenario_id_str)
        
        # Используем общую функцию просмотра
        await _view_scenario_by_id(callback.message, user_id, scenario_id)
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка при просмотре сценария из callback для пользователя {user_id}: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== АДМИНИСТРАТИВНЫЕ КОМАНДЫ ДЛЯ РАЗРАБОТЧИКОВ ====================

async def safe_send_message(
    message_or_chat_id: types.Message | int,
    text: str,
    parse_mode: str = "HTML",
    max_retries: int = 3,
    retry_delay: float = 1.0,
    **kwargs
) -> bool:
    """
    Безопасная отправка сообщения с обработкой таймаутов и retry логикой
    
    Returns:
        bool: True если сообщение отправлено успешно, False в противном случае
    """
    try:
        chat_id = message_or_chat_id if isinstance(message_or_chat_id, int) else message_or_chat_id.chat.id
        message_obj = message_or_chat_id if isinstance(message_or_chat_id, types.Message) else None
        
        logger.debug(f"Попытка отправить сообщение в чат {chat_id}, длина текста: {len(text)}")
        
        for attempt in range(max_retries):
            try:
                if message_obj:
                    await message_obj.answer(text, parse_mode=parse_mode, **kwargs)
                else:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs)
                logger.debug(f"Сообщение успешно отправлено в чат {chat_id} (попытка {attempt + 1})")
                return True
            except TelegramNetworkError as e:
                error_str = str(e).lower()
                if "timeout" in error_str or "request timeout" in error_str:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)
                        logger.warning(f"Таймаут при отправке сообщения в чат {chat_id} (попытка {attempt + 1}/{max_retries}), повтор через {wait_time}с. Ошибка: {e}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Не удалось отправить сообщение в чат {chat_id} после {max_retries} попыток из-за таймаута: {e}")
                        return False
                else:
                    logger.error(f"Ошибка сети Telegram при отправке сообщения в чат {chat_id}: {e}")
                    return False
            except TelegramAPIError as e:
                logger.error(f"Ошибка Telegram API при отправке сообщения в чат {chat_id}: {e}")
                return False
            except Exception as e:
                logger.error(f"Неожиданная ошибка при отправке сообщения в чат {chat_id}: {e}", exc_info=True)
                return False
        
        logger.error(f"Не удалось отправить сообщение в чат {chat_id} после всех попыток")
        return False
    except Exception as e:
        logger.error(f"Критическая ошибка в safe_send_message: {e}", exc_info=True)
        return False


class IsDeveloperFilter(BaseFilter):
    """Фильтр для проверки, является ли пользователь разработчиком"""
    async def __call__(self, message: types.Message) -> bool:
        try:
            user_id = message.from_user.id
            is_dev = LimitsManager.is_developer(user_id)
            logger.info(f"Проверка разработчика: user_id={user_id} (тип: {type(user_id)}), is_dev={is_dev}, DEVELOPER_USER_IDS={DEVELOPER_USER_IDS}")
            return is_dev
        except Exception as e:
            logger.error(f"Ошибка в IsDeveloperFilter: {e}", exc_info=True)
            return False


@dp.message(Command("admin"), IsDeveloperFilter())
async def cmd_admin(message: types.Message):
    """Список административных команд"""
    logger.info(f"[ADMIN] Начало выполнения команды /admin для пользователя {message.from_user.id}")
    try:
        logger.info(f"[ADMIN] Команда /admin выполнена пользователем {message.from_user.id}")
        admin_text = (
            "🔧 <b>Административные команды</b>\n\n"
            "<b>Управление подписками:</b>\n"
            "• <code>/give_sub &lt;user_id&gt; [days]</code> - Выдать подписку\n"
            "• <code>/remove_sub &lt;user_id&gt;</code> - Удалить подписку\n\n"
            "<b>Информация:</b>\n"
            "• <code>/user_info &lt;user_id&gt;</code> - Информация о пользователе\n"
            "• <code>/ref_stats &lt;user_id&gt;</code> - Статистика рефералов пользователя\n"
            "• <code>/stats</code> - Статистика бота\n"
            "• <code>/db_info</code> - Информация о базе данных\n\n"
            "<b>Управление лимитами:</b>\n"
            "• <code>/reset_user &lt;user_id&gt;</code> - Сбросить лимиты пользователя\n\n"
            "<b>Управление пользователями:</b>\n"
            "• <code>/delete_user &lt;user_id&gt;</code> - Удалить аккаунт пользователя из БД\n\n"
            "<b>Рассылка:</b>\n"
            "• <code>/broadcast &lt;сообщение&gt;</code> - Рассылка всем пользователям\n\n"
            "<b>Примеры:</b>\n"
            "<code>/give_sub 123456789 30</code> - Выдать подписку на 30 дней\n"
            "<code>/user_info 123456789</code> - Информация о пользователе\n"
            "<code>/stats</code> - Статистика"
        )
        logger.info(f"[ADMIN] Формирование ответа для команды /admin")
        success = await safe_send_message(message, admin_text)
        logger.info(f"[ADMIN] Результат отправки сообщения для /admin: {success}")
        if not success:
            logger.error(f"[ADMIN] КРИТИЧНО: Не удалось отправить ответ на команду /admin пользователю {message.from_user.id}")
            # Пытаемся отправить короткое сообщение об ошибке напрямую
            try:
                logger.info(f"[ADMIN] Попытка отправить fallback сообщение для /admin")
                await message.answer("⚠️ Произошла ошибка при отправке ответа. Попробуйте позже.")
                logger.info(f"[ADMIN] Fallback сообщение для /admin отправлено успешно")
            except Exception as fallback_error:
                logger.error(f"[ADMIN] Не удалось отправить даже fallback сообщение для /admin: {fallback_error}", exc_info=True)
        else:
            logger.info(f"[ADMIN] Команда /admin выполнена успешно для пользователя {message.from_user.id}")
    except Exception as e:
        logger.error(f"[ADMIN] Ошибка при выполнении команды /admin: {e}", exc_info=True)
        try:
            await message.answer(f"❌ Ошибка при выполнении команды: {str(e)[:200]}")
        except Exception as send_error:
            logger.error(f"[ADMIN] Не удалось отправить сообщение об ошибке для /admin: {send_error}", exc_info=True)


@dp.message(Command("give_sub"), IsDeveloperFilter())
async def cmd_give_sub(message: types.Message):
    """Выдача подписки пользователю: /give_sub <user_id> [days]"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/give_sub &lt;user_id&gt; [days]</code>\n\n"
            "Пример: <code>/give_sub 123456789 30</code>\n"
            "Если не указать days, будет использовано значение по умолчанию.",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else None
        
        await SubscriptionManager.activate_subscription(user_id, days)
        subscription_info = await SubscriptionManager.get_subscription_info(user_id)
        
        expires_at_str = subscription_info['expires_at'].strftime("%d.%m.%Y %H:%M")
        success_text = (
            f"✅ <b>Подписка выдана!</b>\n\n"
            f"<b>Пользователь:</b> {user_id}\n"
            f"<b>Действует до:</b> {expires_at_str}\n"
            f"<b>Осталось дней:</b> {subscription_info['days_left']}"
        )
        success = await safe_send_message(message, success_text)
        if not success:
            logger.warning("Не удалось отправить ответ на команду /give_sub из-за таймаута")
        
        # Уведомление пользователя (не критично, если не отправится)
        await safe_send_message(
            user_id,
            f"🎉 <b>Тебе выдана Премиум подписка!</b>\n\n"
            f"Теперь у тебя безлимитный доступ к генерации сценариев!",
            max_retries=1  # Одна попытка для уведомления
        )
            
    except ValueError:
        await safe_send_message(message, "❌ Неверный формат. user_id и days должны быть числами.")
    except Exception as e:
        logger.error(f"Ошибка при выдаче подписки: {e}")
        await safe_send_message(message, f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("remove_sub"), IsDeveloperFilter())
async def cmd_remove_sub(message: types.Message):
    """Удаление подписки пользователя: /remove_sub <user_id>"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/remove_sub &lt;user_id&gt;</code>\n\n"
            "Пример: <code>/remove_sub 123456789</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        had_subscription = await SubscriptionManager.has_active_subscription(user_id)
        
        await SubscriptionManager.cancel_subscription(user_id)
        
        if had_subscription:
            success = await safe_send_message(
                message,
                f"✅ <b>Подписка удалена</b>\n\n"
                f"Пользователь {user_id} больше не имеет премиум подписки."
            )
            if not success:
                logger.warning("Не удалось отправить ответ на команду /remove_sub из-за таймаута")
            
            # Уведомление пользователя (не критично)
            await safe_send_message(
                user_id,
                "ℹ️ <b>Твоя Премиум подписка была отменена.</b>\n\n"
                "Ты можешь оформить новую подписку через /subscribe",
                max_retries=1
            )
        else:
            await safe_send_message(message, f"ℹ️ У пользователя {user_id} не было активной подписки.")
            
    except ValueError:
        await safe_send_message(message, "❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при удалении подписки: {e}")
        await safe_send_message(message, f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("user_info"), IsDeveloperFilter())
async def cmd_user_info(message: types.Message):
    """Информация о пользователе: /user_info <user_id>"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/user_info &lt;user_id&gt;</code>\n\n"
            "Пример: <code>/user_info 123456789</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        
        is_dev = LimitsManager.is_developer(user_id)
        has_premium = await SubscriptionManager.has_active_subscription(user_id)
        requests_count = await Database.get_user_requests_count(user_id)
        remaining = await LimitsManager.get_remaining_requests(user_id)
        subscription_info = await SubscriptionManager.get_subscription_info(user_id)
        
        try:
            user = await bot.get_chat(user_id)
            user_name = user.full_name or "Неизвестно"
            username = f"@{user.username}" if user.username else "нет"
        except:
            user_name = "Неизвестно"
            username = "нет"
        
        info_text = (
            f"👤 <b>Информация о пользователе</b>\n\n"
            f"<b>ID:</b> <code>{user_id}</code>\n"
            f"<b>Имя:</b> {user_name}\n"
            f"<b>Username:</b> {username}\n\n"
            f"<b>Статус:</b>\n"
        )
        
        if is_dev:
            info_text += "• 👨‍💻 Разработчик (безлимит)\n"
        elif has_premium:
            expires_at_str = subscription_info['expires_at'].strftime("%d.%m.%Y %H:%M")
            info_text += (
                f"• 💎 Премиум подписка\n"
                f"  Действует до: {expires_at_str}\n"
                f"  Осталось дней: {subscription_info['days_left']}\n"
            )
        else:
            info_text += "• 🆓 Бесплатный тариф\n"
        
        info_text += (
            f"\n<b>Лимиты:</b>\n"
            f"• Использовано запросов: {requests_count}\n"
        )
        
        if remaining == -1:
            info_text += "• Осталось: безлимит\n"
        else:
            info_text += f"• Осталось: {remaining}\n"
            info_text += f"• Лимит: {MAX_REQUESTS_PER_USER}\n"
        
        success = await safe_send_message(message, info_text)
        if not success:
            logger.warning("Не удалось отправить ответ на команду /user_info из-за таймаута")
        
    except ValueError:
        await safe_send_message(message, "❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пользователе: {e}")
        await safe_send_message(message, f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("ref_stats"), IsDeveloperFilter())
async def cmd_ref_stats(message: types.Message):
    """Статистика рефералов пользователя: /ref_stats <user_id>"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/ref_stats &lt;user_id&gt;</code>\n\n"
            "Пример: <code>/ref_stats 123456789</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        
        # Получаем статистику рефералов
        referral_stats = await Database.get_referral_stats(user_id)
        total_referrals = referral_stats["total_referrals"]
        earned_attempts = referral_stats["earned_attempts"]
        
        # Получаем информацию о пользователе
        try:
            user = await bot.get_chat(user_id)
            user_name = user.full_name or "Неизвестно"
            username = f"@{user.username}" if user.username else "нет"
        except:
            user_name = "Неизвестно"
            username = "нет"
        
        # Получаем список рефералов
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            referrals = await conn.fetch("""
                SELECT referred_id, created_at 
                FROM referrals 
                WHERE referrer_id = $1 
                ORDER BY created_at DESC 
                LIMIT 10
            """, user_id)
        
        ref_text = (
            f"🎯 <b>Статистика рефералов</b>\n\n"
            f"<b>Пользователь:</b> {user_name}\n"
            f"<b>Username:</b> {username}\n"
            f"<b>ID:</b> <code>{user_id}</code>\n\n"
            f"<b>Статистика:</b>\n"
            f"• Перешло по ссылке: {total_referrals} человек\n"
            f"• Попыток заработано: {earned_attempts}\n\n"
        )
        
        if referrals:
            ref_text += "<b>Последние рефералы:</b>\n"
            for i, ref in enumerate(referrals[:5], 1):
                ref_date = ref['created_at'].strftime("%d.%m.%Y %H:%M") if ref['created_at'] else "неизвестно"
                ref_text += f"{i}. ID: <code>{ref['referred_id']}</code> ({ref_date})\n"
        else:
            ref_text += "Пока нет рефералов.\n"
        
        success = await safe_send_message(message, ref_text)
        if not success:
            logger.warning("Не удалось отправить ответ на команду /ref_stats из-за таймаута")
        
    except ValueError:
        await safe_send_message(message, "❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при получении статистики рефералов: {e}")
        await safe_send_message(message, f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("delete_user"), IsDeveloperFilter())
async def cmd_delete_user(message: types.Message):
    """Удаление аккаунта пользователя из БД: /delete_user <user_id>"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/delete_user &lt;user_id&gt;</code>\n\n"
            "Пример: <code>/delete_user 123456789</code>\n\n"
            "⚠️ <b>Внимание:</b> Это удалит все данные пользователя из БД!",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        
        # Получаем информацию о пользователе перед удалением
        try:
            user = await bot.get_chat(user_id)
            user_name = user.full_name or "Неизвестно"
            username = f"@{user.username}" if user.username else "нет"
        except:
            user_name = "Неизвестно"
            username = "нет"
        
        # Проверяем, существует ли пользователь в БД
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id = $1", user_id)
            
            if not exists or exists == 0:
                await message.answer(f"ℹ️ Пользователь {user_id} не найден в базе данных.")
                return
            
            # Удаляем пользователя (CASCADE удалит все связанные данные)
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            
            logger.info(f"Пользователь {user_id} удален из БД администратором {message.from_user.id}")
            
            await message.answer(
                f"✅ <b>Пользователь удален из БД</b>\n\n"
                f"<b>Пользователь:</b> {user_name}\n"
                f"<b>Username:</b> {username}\n"
                f"<b>ID:</b> <code>{user_id}</code>\n\n"
                f"Все связанные данные удалены (подписки, запросы, рефералы и т.д.)",
                parse_mode="HTML"
            )
            
    except ValueError:
        await message.answer("❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("stats"), IsDeveloperFilter())
async def cmd_stats(message: types.Message):
    """Статистика бота"""
    logger.info(f"[STATS] Начало выполнения команды /stats для пользователя {message.from_user.id}")
    try:
        logger.info(f"[STATS] Получение статистики для команды /stats")
        total_registered = await Database.get_registered_users_count()
        total_active = await Database.get_active_users_count()
        total_requests = await Database.get_total_requests_count()
        users_with_requests = await Database.get_users_with_requests_count()
        active_subscriptions = len(await SubscriptionManager.get_all_subscriptions())
        premium_users = active_subscriptions
        
        # Получаем метрики за последний час
        metrics = metrics_collector.get_stats()
        
        stats_text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"<b>Пользователи:</b>\n"
            f"• Всего зарегистрированных: {total_registered}\n"
            f"• Активных пользователей: {total_active}\n"
            f"• Премиум подписчиков: {premium_users}\n"
            f"• Разработчиков: {len(DEVELOPER_USER_IDS)}\n\n"
            f"<b>Запросы:</b>\n"
            f"• Всего запросов: {total_requests}\n"
            f"• Пользователей с запросами: {users_with_requests}\n\n"
            f"<b>Подписки:</b>\n"
            f"• Активных подписок: {active_subscriptions}\n\n"
            f"<b>Метрики за последний час:</b>\n"
            f"• Запросов: {metrics['total_requests']}\n"
            f"• Успешность: {metrics['success_rate']:.1f}%\n"
            f"• Среднее время ответа: {metrics['avg_response_time']:.2f}с\n"
            f"• Минимум: {metrics['min_response_time']:.2f}с\n"
            f"• Максимум: {metrics['max_response_time']:.2f}с\n"
            f"• Активных пользователей: {metrics['active_users']}\n"
        )
        
        if metrics['top_commands']:
            stats_text += "\n<b>Топ команд:</b>\n"
            for cmd, count in list(metrics['top_commands'].items())[:5]:
                stats_text += f"• /{cmd}: {count}\n"
        
        logger.info(f"[STATS] Формирование ответа для команды /stats")
        success = await safe_send_message(message, stats_text)
        logger.info(f"[STATS] Результат отправки сообщения для /stats: {success}")
        if not success:
            logger.error(f"[STATS] КРИТИЧНО: Не удалось отправить ответ на команду /stats пользователю {message.from_user.id}")
            try:
                logger.info(f"[STATS] Попытка отправить fallback сообщение для /stats")
                await message.answer("⚠️ Произошла ошибка при отправке статистики. Попробуйте позже.")
                logger.info(f"[STATS] Fallback сообщение для /stats отправлено успешно")
            except Exception as fallback_error:
                logger.error(f"[STATS] Не удалось отправить даже fallback сообщение для /stats: {fallback_error}", exc_info=True)
        else:
            logger.info(f"[STATS] Команда /stats выполнена успешно для пользователя {message.from_user.id}")
    except Exception as e:
        logger.error(f"[STATS] Ошибка при выполнении команды /stats: {e}", exc_info=True)
        try:
            await message.answer(f"❌ Ошибка при получении статистики: {str(e)[:200]}")
        except Exception as send_error:
            logger.error(f"[STATS] Не удалось отправить сообщение об ошибке для /stats: {send_error}", exc_info=True)


@dp.message(Command("db_info"), IsDeveloperFilter())
async def cmd_db_info(message: types.Message):
    """Информация о базе данных PostgreSQL"""
    try:
        from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_DSN
        
        info_text = (
            "🗄️ <b>Информация о базе данных PostgreSQL</b>\n\n"
            f"<b>Тип БД:</b> PostgreSQL\n"
        )
        
        if POSTGRES_DSN:
            info_text += f"<b>Подключение:</b> через DSN (скрыто)\n\n"
        else:
            info_text += (
                f"<b>Хост:</b> <code>{POSTGRES_HOST}</code>\n"
                f"<b>Порт:</b> <code>{POSTGRES_PORT}</code>\n"
                f"<b>База данных:</b> <code>{POSTGRES_DB}</code>\n"
                f"<b>Пользователь:</b> <code>{POSTGRES_USER}</code>\n\n"
            )
        
        try:
            total_users = await Database.get_registered_users_count()
            total_requests = await Database.get_total_requests_count()
            active_subs = len(await SubscriptionManager.get_all_subscriptions())
            
            info_text += (
                f"<b>Данные в БД:</b>\n"
                f"• Пользователей: {total_users}\n"
                f"• Запросов: {total_requests}\n"
                f"• Активных подписок: {active_subs}\n\n"
                f"✅ <b>Подключение активно</b>\n\n"
                f"💡 <i>Для просмотра содержимого БД используйте:</i>\n"
                f"• pgAdmin\n"
                f"• DBeaver\n"
                f"• psql (командная строка)\n"
                f"• Или любой другой PostgreSQL клиент"
            )
        except Exception as e:
            info_text += (
                f"❌ <b>Ошибка подключения к БД:</b>\n"
                f"<code>{str(e)}</code>"
            )
        
        success = await safe_send_message(message, info_text)
        if not success:
            logger.error(f"КРИТИЧНО: Не удалось отправить ответ на команду /db_info пользователю {message.from_user.id}")
            try:
                await message.answer("⚠️ Произошла ошибка при отправке информации о БД. Попробуйте позже.")
            except Exception as fallback_error:
                logger.error(f"Не удалось отправить даже fallback сообщение: {fallback_error}")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /db_info: {e}", exc_info=True)
        try:
            await message.answer(f"❌ Ошибка при получении информации о БД: {str(e)[:200]}")
        except Exception as send_error:
            logger.error(f"Не удалось отправить сообщение об ошибке: {send_error}")


@dp.message(Command("reset_user"), IsDeveloperFilter())
async def cmd_reset_user(message: types.Message):
    """Сброс лимитов пользователя: /reset_user <user_id>"""
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/reset_user &lt;user_id&gt;</code>\n\n"
            "Пример: <code>/reset_user 123456789</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        user_id = int(parts[1])
        had_requests = await LimitsManager.get_user_requests_count(user_id) > 0
        
        await LimitsManager.reset_user_requests(user_id)
        
        if had_requests:
            await message.answer(
                f"✅ <b>Лимиты сброшены</b>\n\n"
                f"Пользователь {user_id} теперь имеет полный лимит запросов.",
                parse_mode="HTML"
            )
        else:
            await message.answer(f"ℹ️ У пользователя {user_id} не было использованных запросов.")
            
    except ValueError:
        await message.answer("❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при сбросе лимитов: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("broadcast"), IsDeveloperFilter())
async def cmd_broadcast(message: types.Message, state: FSMContext):
    """Рассылка сообщения всем пользователям: /broadcast <сообщение>"""
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/broadcast &lt;сообщение&gt;</code>\n\n"
            "Пример: <code>/broadcast Всем привет! Обновление бота...</code>",
            parse_mode="HTML"
        )
        return
    
    broadcast_text = parts[1]
    total_users = await Database.get_registered_users_count()
    
    confirm_text = (
        f"⚠️ <b>Подтверждение рассылки</b>\n\n"
        f"<b>Сообщение:</b>\n{broadcast_text}\n\n"
        f"<b>Получателей:</b> {total_users} пользователей\n\n"
        f"Отправить? (да/нет)"
    )
    await message.answer(confirm_text, parse_mode="HTML")
    await state.update_data(broadcast_text=broadcast_text)
    await state.set_state(ScenarioStates.waiting_broadcast_confirm)


@dp.message(StateFilter(ScenarioStates.waiting_broadcast_confirm), IsDeveloperFilter())
async def process_broadcast_confirm(message: types.Message, state: FSMContext):
    """Обработка подтверждения рассылки"""
    if message.text.lower() not in ["да", "yes", "y", "ок", "ok"]:
        await message.answer("❌ Рассылка отменена.")
        await state.clear()
        return
    
    data = await state.get_data()
    broadcast_text = data.get("broadcast_text")
    
    if not broadcast_text:
        await message.answer("❌ Ошибка: текст рассылки не найден.")
        await state.clear()
        return
    
    user_ids = await Database.get_all_active_user_ids()
    total_users = len(user_ids)
    sent_count = 0
    failed_count = 0
    
    status_msg = await message.answer(f"📤 Начинаю рассылку... 0/{total_users}")
    
    semaphore = asyncio.Semaphore(20)
    
    async def send_to_user(user_id: int):
        nonlocal sent_count, failed_count
        async with semaphore:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"📢 <b>Рассылка от администрации:</b>\n\n{broadcast_text}",
                    parse_mode="HTML"
                )
                sent_count += 1
                if sent_count % 20 == 0:
                    try:
                        await status_msg.edit_text(f"📤 Рассылка... {sent_count}/{total_users}")
                    except:
                        pass
            except Exception as e:
                failed_count += 1
                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
    
    tasks = [send_to_user(user_id) for user_id in user_ids]
    await asyncio.gather(*tasks, return_exceptions=True)
    
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Отправлено: {sent_count}\n"
        f"• Ошибок: {failed_count}\n"
        f"• Всего: {total_users}",
        parse_mode="HTML"
    )
    
    await state.clear()


@dp.message(Command("reply"))
async def cmd_reply(message: types.Message):
    """Команда для ответа пользователю напрямую: /reply <user_id> <сообщение>"""
    if not LimitsManager.is_developer(message.from_user.id):
        await message.answer("❌ Эта команда доступна только разработчикам.")
        return
    
    parts = message.text.split(None, 2)
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "Использование: <code>/reply &lt;user_id&gt; &lt;сообщение&gt;</code>\n\n"
            "Пример: <code>/reply 123456789 Привет! Мы исправили проблему.</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        target_user_id = int(parts[1])
        reply_text = parts[2]
        
        await bot.send_message(
            chat_id=target_user_id,
            text=f"💬 <b>Ответ от поддержки:</b>\n\n{reply_text}",
            parse_mode="HTML"
        )
        await message.answer(f"✅ Ответ отправлен пользователю {target_user_id}!")
    except ValueError:
        await message.answer("❌ Неверный user_id. Должно быть число.")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа через /reply: {e}")
        await message.answer(
            f"❌ Не удалось отправить ответ.\n"
            f"Ошибка: {str(e)[:200]}"
        )


@dp.message(Command("admin", "give_sub", "remove_sub", "user_info", "ref_stats", "delete_user", "stats", "reset_user", "broadcast"))
async def admin_commands_not_developer(message: types.Message):
    """Обработчик для административных команд, когда пользователь не разработчик"""
    if not LimitsManager.is_developer(message.from_user.id):
        await message.answer("❌ Эта команда доступна только разработчикам.")


@dp.message()
async def echo_handler(message: types.Message):
    """Обработчик всех остальных сообщений"""
    active_users.add(message.from_user.id)
    
    await message.answer(
        "🤔 Я не понял тебя. Используй команду /help для справки или нажми кнопку «Создать сценарий»",
        reply_markup=get_main_keyboard()
    )


async def _batch_mark_active():
    """Пометить всех активных пользователей в БД батчем"""
    if not active_users:
        return
    
    # Копируем set, чтобы избежать проблем с изменением во время итерации
    users_to_mark = list(active_users)
    active_users.clear()  # Очищаем после копирования
    
    # Помечаем пользователей батчем
    for user_id in users_to_mark:
        try:
            await Database.mark_user_active(user_id)
        except Exception as e:
            logger.error(f"Ошибка при пометке пользователя {user_id} как активного: {e}")


async def _periodic_db_flush():
    while True:
        await asyncio.sleep(300)
        await _batch_mark_active()

async def periodic_cleanup():
    while True:
        await asyncio.sleep(1800)
        _cleanup_user_sets()
        _cleanup_storage()
        await _batch_mark_active()
        rate_limiter.cleanup()


async def send_reminders():
    """Отправка напоминаний пользователям о подписке на канал и реферальной программе"""
    while True:
        try:
            # Ждем 6 часов перед первой проверкой и между проверками
            await asyncio.sleep(21600)  # 6 часов
            
            # Напоминания о подписке на канал (каждые 2 дня)
            try:
                users_for_channel = await Database.get_users_for_channel_reminder(reminder_interval_hours=48)
                logger.info(f"Отправка напоминаний о подписке на канал для {len(users_for_channel)} пользователей")
                
                for user_id in users_for_channel[:50]:  # Ограничиваем до 50 за раз, чтобы не перегружать
                    try:
                        reminder_text = (
                            "📢 <b>Напоминание</b>\n\n"
                            "Не забудь подписаться на наш канал и получить <b>3 дополнительные попытки</b>! 🎁\n\n"
                            f"👉 {REQUIRED_CHANNEL_URL}\n\n"
                            "После подписки нажми кнопку ниже для проверки."
                        )
                        
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="📢 Подписаться на канал (+3 попытки)", url=REQUIRED_CHANNEL_URL)],
                                [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")]
                            ]
                        )
                        
                        await bot.send_message(
                            chat_id=user_id,
                            text=reminder_text,
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                        
                        await Database.update_channel_reminder_time(user_id)
                        await asyncio.sleep(1)  # Небольшая задержка между отправками
                    except Exception as e:
                        logger.warning(f"Не удалось отправить напоминание о канале пользователю {user_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при отправке напоминаний о канале: {e}")
            
            # Небольшая задержка между типами напоминаний
            await asyncio.sleep(60)
            
            # Напоминания о реферальной программе (каждые 3 дня)
            try:
                users_for_referral = await Database.get_users_for_referral_reminder(reminder_interval_hours=72)
                logger.info(f"Отправка напоминаний о реферальной программе для {len(users_for_referral)} пользователей")
                
                for user_id in users_for_referral[:50]:  # Ограничиваем до 50 за раз
                    try:
                        # Получаем статистику рефералов
                        referral_stats = await Database.get_referral_stats(user_id)
                        total_referrals = referral_stats["total_referrals"]
                        
                        # Получаем реферальную ссылку
                        bot_info = await bot.get_me()
                        bot_username = bot_info.username
                        referral_link = f"https://t.me/{bot_username}?start={user_id}"
                        
                        reminder_text = (
                            "👥 <b>Напоминание</b>\n\n"
                            "Не забудь пригласить друзей и получить дополнительные попытки! 🎁\n\n"
                            "💡 <b>За каждого друга, который активирует бота по твоей ссылке, ты получишь 1 попытку!</b>\n\n"
                            f"📊 <b>Твоя статистика:</b> {total_referrals} приглашенных друзей\n\n"
                            f"🔗 <b>Твоя ссылка:</b>\n"
                            f"<code>{referral_link}</code>"
                        )
                        
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="👥 Поделиться ссылкой", url=f"https://t.me/share/url?url={referral_link}&text=🎬%20Создавай%20сценарии%20для%20рилсов%20с%20ReelsScript%20Bot!")]
                            ]
                        )
                        
                        await bot.send_message(
                            chat_id=user_id,
                            text=reminder_text,
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                        
                        await Database.update_referral_reminder_time(user_id)
                        await asyncio.sleep(1)  # Небольшая задержка между отправками
                    except Exception as e:
                        logger.warning(f"Не удалось отправить напоминание о рефералах пользователю {user_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при отправке напоминаний о рефералах: {e}")
                
        except Exception as e:
            logger.error(f"Ошибка в задаче отправки напоминаний: {e}")
            await asyncio.sleep(3600)  # Ждем час перед повторной попыткой


async def handle_robokassa_payment_redirect(request: Request) -> Response:
    """
    Обработчик короткой ссылки на оплату - перенаправляет на Robokassa через POST форму
    Принимает inv_id и автоматически отправляет POST форму к Robokassa
    """
    if PAYMENT_SYSTEM != "robokassa" or not robokassa_service:
        return web.Response(text="Robokassa not configured", status=500)
    
    try:
        # Получаем inv_id из URL
        inv_id = int(request.match_info.get('inv_id', 0))
        logger.info(f"Получен запрос на оплату: inv_id={inv_id}")
        if not inv_id:
            logger.error(f"Неверный inv_id: {inv_id}")
            return web.Response(text="Invalid payment ID", status=400)
        
        # Получаем данные платежа из БД
        payment = await Database.get_payment_by_inv_id(inv_id)
        if not payment:
            logger.error(f"Платеж не найден в БД: inv_id={inv_id}")
            return web.Response(text="Payment not found", status=404)
        
        logger.info(f"Платеж найден: inv_id={inv_id}, тип={payment['payment_type']}, сумма={payment['amount']}₽")
        
        # Формируем Receipt для фискализации, если включено
        receipt = None
        if ROBOKASSA_FISCAL_ENABLED:
            if payment['payment_type'] == 'subscription':
                period_text = f"{payment['period_months']} {'месяц' if payment['period_months'] == 1 else 'месяца' if payment['period_months'] < 5 else 'месяцев'}"
                receipt = {
                    "items": [
                        {
                            "name": f"Премиум подписка {period_text}",
                            "quantity": "1",
                            "price": f"{payment['amount']:.2f}",
                            "tax": ROBOKASSA_TAX_RATE,
                            "payment_object": "service",
                            "payment_method": "full_payment"
                        }
                    ]
                }
            elif payment['payment_type'] == 'extra_requests':
                count = payment.get('count', 1)
                receipt = {
                    "items": [
                        {
                            "name": f"Дополнительные попытки ({count} шт.)",
                            "quantity": "1",
                            "price": f"{payment['amount']:.2f}",
                            "tax": ROBOKASSA_TAX_RATE,
                            "payment_object": "service",
                            "payment_method": "full_payment"
                        }
                    ]
                }
        
        # Генерируем данные для оплаты
        description = f"Платеж #{inv_id}"
        if payment['payment_type'] == 'subscription':
            period_text = f"{payment['period_months']} {'месяц' if payment['period_months'] == 1 else 'месяца' if payment['period_months'] < 5 else 'месяцев'}"
            description = f"Премиум подписка {period_text}"
        elif payment['payment_type'] == 'extra_requests':
            count = payment.get('count', 1)
            description = f"{count} дополнительных попыток для генерации сценариев"
        
        # Генерируем полную ссылку с параметрами
        payment_url = robokassa_service.generate_payment_url(
            out_sum=payment['amount'],
            inv_id=inv_id,
            description=description,
            user_id=payment['user_id'],
            receipt=receipt
        )
        
        # Парсим URL и извлекаем параметры
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(payment_url)
        params = parse_qs(parsed.query)
        
        # Преобразуем списки в строки (parse_qs возвращает списки)
        form_params = {k: v[0] if isinstance(v, list) and len(v) > 0 else v for k, v in params.items()}
        
        # Создаем HTML форму с автоматической отправкой POST запроса
        # Важно: экранируем значения для безопасности HTML
        from html import escape
        form_fields = ''.join([f'<input type="hidden" name="{escape(str(k))}" value="{escape(str(v))}">' for k, v in form_params.items()])
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Перенаправление на оплату...</title>
        </head>
        <body>
            <p>Перенаправление на страницу оплаты...</p>
            <form id="paymentForm" method="POST" action="{robokassa_service.base_url}">
                {form_fields}
            </form>
            <script>
                document.getElementById('paymentForm').submit();
            </script>
        </body>
        </html>
        """
        
        return web.Response(text=html, content_type='text/html; charset=utf-8')
    
    except ValueError as e:
        logger.error(f"Ошибка парсинга inv_id: {e}", exc_info=True)
        return web.Response(text=f"Invalid payment ID format: {str(e)}", status=400)
    except Exception as e:
        logger.error(f"Ошибка при обработке редиректа на оплату: {e}", exc_info=True)
        return web.Response(text=f"Error: {str(e)}", status=500)


async def handle_robokassa_result(request: Request) -> Response:
    """
    Обработчик уведомлений от Robokassa (Result URL)
    Robokassa отправляет POST запрос с данными о платеже
    """
    if PAYMENT_SYSTEM != "robokassa" or not robokassa_service:
        return web.Response(text="Robokassa not configured", status=500)
    
    try:
        # Получаем данные из POST запроса
        data = await request.post()
        
        # Извлекаем основные параметры
        out_sum_raw = data.get("OutSum", "0")
        # Сохраняем исходное значение суммы для проверки подписи (Robokassa может отправлять в разных форматах)
        inv_id = int(data.get("InvId", 0))
        signature = data.get("SignatureValue", "")
        
        # Извлекаем все Shp_параметры (как строки, как их отправил Robokassa)
        shp_params = {k: str(v) for k, v in data.items() if k.startswith("Shp_")}
        
        # Для логирования и проверки суммы преобразуем в float
        out_sum_float = float(out_sum_raw.replace(",", "."))
        
        logger.info(f"Получено уведомление от Robokassa: inv_id={inv_id}, сумма_raw={out_sum_raw}, сумма={out_sum_float}₽, подпись={signature}, shp_params={shp_params}")
        
        # Передаем исходное значение суммы (строку) для проверки подписи
        out_sum_for_signature = out_sum_raw
        
        # Проверяем подпись (передаем исходное значение суммы и все Shp_параметры)
        if not robokassa_service.verify_result_notification(
            out_sum=out_sum_for_signature,
            inv_id=inv_id,
            signature=signature,
            **shp_params
        ):
            logger.error(f"Неверная подпись уведомления Robokassa: inv_id={inv_id}")
            return web.Response(text=f"WRONG SIGNATURE", status=200)  # Robokassa требует 200 OK
        
        # Получаем информацию о платеже из БД
        payment = await Database.get_payment_by_inv_id(inv_id)
        if not payment:
            logger.error(f"Платеж не найден в БД: inv_id={inv_id}")
            return web.Response(text=f"OK{inv_id}", status=200)
        
        # Проверяем, не был ли платеж уже обработан
        if payment["status"] == "paid":
            logger.info(f"Платеж уже был обработан: inv_id={inv_id}")
            return web.Response(text=f"OK{inv_id}", status=200)
        
        # Проверяем сумму (используем преобразованное значение)
        if abs(payment["amount"] - out_sum_float) > 0.01:  # Допускаем небольшую погрешность
            logger.error(f"Несовпадение суммы платежа: ожидалось {payment['amount']}₽, получено {out_sum_float}₽")
            return web.Response(text=f"WRONG AMOUNT", status=200)
        
        user_id = payment["user_id"]
        payment_type = payment["payment_type"]
        
        # Дополнительная проверка: если есть Shp_userId, проверяем, что он совпадает
        if "Shp_userId" in shp_params:
            shp_user_id = int(shp_params["Shp_userId"])
            if shp_user_id != user_id:
                logger.error(f"Несовпадение user_id: ожидалось {user_id}, получено из Shp_userId={shp_user_id}")
                return web.Response(text=f"WRONG USER", status=200)
        
        # Обрабатываем платеж в зависимости от типа
        if payment_type == "subscription":
            period_months = payment["period_months"]
            duration_days = period_months * 30
            
            logger.info(f"Активация подписки через Robokassa: пользователь {user_id}, период {period_months} месяцев ({duration_days} дней)")
            await SubscriptionManager.activate_subscription(user_id, duration_days)
            await LimitsManager.reset_user_requests(user_id)
            
            subscription_info = await SubscriptionManager.get_subscription_info(user_id)
            expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y")
            logger.info(f"Подписка активирована через Robokassa: пользователь {user_id}, действует до {expires_at}")
            
            # Отправляем уведомление пользователю
            try:
                text = (
                    "🎉 <b>Спасибо за покупку!</b>\n\n"
                    "✅ <b>Премиум подписка активирована!</b>\n\n"
                    f"📅 <b>Действует до:</b> {expires_at}\n"
                    f"⏰ <b>Период:</b> {period_months} {'месяц' if period_months == 1 else 'месяца' if period_months < 5 else 'месяцев'}\n\n"
                    "Теперь у вас безлимитное количество запросов!\n"
                    "Можете создавать сколько угодно сценариев! 🚀"
                )
                
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")]
                    ]
                )
                
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
        
        elif payment_type == "extra_requests":
            count = payment["count"]
            
            logger.info(f"Добавление дополнительных попыток через Robokassa: пользователь {user_id}, количество {count}")
            await Database.add_extra_requests(user_id, count)
            total_extra = await Database.get_extra_requests_count(user_id)
            logger.info(f"Дополнительные попытки добавлены через Robokassa: пользователь {user_id}, всего попыток: {total_extra}")
            
            # Отправляем уведомление пользователю
            try:
                text = (
                    "🎉 <b>Спасибо за покупку!</b>\n\n"
                    f"✅ <b>Добавлено {count} дополнительных попыток!</b>\n\n"
                    f"📊 <b>Всего дополнительных попыток:</b> {total_extra}\n\n"
                    "💡 <b>Как это работает:</b>\n"
                    "• Дополнительные попытки используются после исчерпания бесплатных\n"
                    "• Они не сгорают и накапливаются\n"
                    "• Можно докупать в любое время\n\n"
                    "Можете продолжать создавать сценарии! 🚀"
                )
                
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")]
                    ]
                )
                
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
        
        # Отмечаем платеж как оплаченный
        await Database.mark_payment_paid(inv_id)
        
        # Возвращаем ответ Robokassa (обязательно в формате OK<номер заказа>)
        return web.Response(text=f"OK{inv_id}", status=200)
    
    except Exception as e:
        logger.error(f"Ошибка при обработке уведомления Robokassa: {e}", exc_info=True)
        # Все равно возвращаем 200 OK, чтобы Robokassa не повторяла запрос
        return web.Response(text="ERROR", status=200)


async def main():
    """Главная функция запуска бота"""
    logger.info("Запуск бота...")
    
    try:
        await init_database()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: не удалось подключиться к базе данных")
        logger.error(f"Ошибка: {e}")
        logger.error("Бот не может работать без подключения к PostgreSQL!")
        logger.error("Проверьте настройки подключения к базе данных в переменных окружения.")
        return
    
    try:
        await Database.cleanup_expired_subscriptions()
    except Exception as e:
        logger.warning(f"Не удалось очистить истекшие подписки: {e}")
    
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(_periodic_db_flush())
    asyncio.create_task(send_reminders())
    
    # Периодический сброс метрик (каждый час)
    async def reset_metrics_periodically():
        while True:
            await asyncio.sleep(3600)  # 1 час
            metrics_collector.reset_hourly_stats()
    
    asyncio.create_task(reset_metrics_periodically())
    
    # Запускаем приоритетную очередь
    await _priority_queue.start()
    logger.info("✅ Приоритетная очередь запущена")
    
    # Запускаем HTTP сервер для обработки уведомлений от Robokassa (если используется Robokassa)
    if PAYMENT_SYSTEM == "robokassa" and robokassa_service:
        app = web.Application()
        app.router.add_post("/robokassa/result", handle_robokassa_result)
        app.router.add_get("/robokassa/pay/{inv_id}", handle_robokassa_payment_redirect)  # Короткая ссылка на оплату
        
        # Получаем порт из переменной окружения или используем 80 (для Amvera)
        # Amvera использует containerPort: 80, поэтому слушаем порт 80
        web_port = int(os.getenv("WEBHOOK_PORT", "80"))
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", web_port)
        await site.start()
        logger.info(f"✅ HTTP сервер для Robokassa запущен на порту {web_port}")
        logger.info(f"📋 Зарегистрированные маршруты:")
        logger.info(f"   POST /robokassa/result - обработка уведомлений от Robokassa")
        logger.info(f"   GET  /robokassa/pay/{{inv_id}} - короткая ссылка на оплату")
        
        # Получаем публичный URL из переменной окружения (Amvera может предоставлять)
        # Можно указать в .env: PUBLIC_URL=https://ваш-проект.amvera.app
        public_url = os.getenv("AMVERA_PUBLIC_URL") or os.getenv("PUBLIC_URL") or "https://ваш-проект.amvera.app"
        result_url = f"{public_url}/robokassa/result"
        logger.info(f"✅ HTTP сервер для уведомлений Robokassa запущен на порту {web_port}")
        logger.info(f"📋 Result URL для Robokassa: {result_url}")
        logger.info(f"⚠️ ВАЖНО: Укажите этот URL в настройках Robokassa (Result URL, метод POST)")
        if not os.getenv("PUBLIC_URL") and not os.getenv("AMVERA_PUBLIC_URL"):
            logger.warning(f"⚠️ PUBLIC_URL не указан в .env! Укажите ваш домен Amvera для корректного Result URL")
    
    try:
        await dp.start_polling(
            bot, 
            skip_updates=True,
            allowed_updates=["message", "callback_query", "pre_checkout_query"]
        )
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    finally:
        # Останавливаем приоритетную очередь
        try:
            await _priority_queue.stop()
        except Exception as e:
            logger.error(f"Ошибка при остановке приоритетной очереди: {e}")
        
        await bot.session.close()
        _scenario_executor.shutdown(wait=False)
        from database import _db_pool
        if _db_pool:
            await _db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())

