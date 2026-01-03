import asyncio
import logging
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from config import TELEGRAM_BOT_TOKEN, PAYMENT_PROVIDER_TOKEN, SUBSCRIPTION_PRICE, SUBSCRIPTION_DURATION_DAYS, MAX_REQUESTS_PER_USER, DEVELOPER_USER_IDS
from services.scenario_generator import ScenarioGenerator
from services.limits_manager import LimitsManager
from services.subscription_manager import SubscriptionManager
from database import Database, init_database

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scenario_generator = ScenarioGenerator()

# Трекинг активных пользователей (для статистики и рассылки)
# В production лучше использовать БД
active_users: set[int] = set()  # Множество всех пользователей, которые когда-либо использовали бота
registered_users: set[int] = set()  # Множество пользователей, которые нажали /start (зарегистрировались)


# Состояния FSM
class ScenarioStates(StatesGroup):
    waiting_for_niche = State()
    waiting_for_format = State()
    waiting_for_style = State()
    waiting_for_topic = State()
    waiting_for_additional_info = State()
    waiting_for_improvement = State()  # Состояние для развития/доработки сценария
    waiting_for_support_message = State()  # Состояние для ожидания сообщения в поддержку
    waiting_broadcast_confirm = State()  # Состояние для подтверждения рассылки


# Клавиатуры
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


def get_niche_keyboard():
    """Выбор ниши"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="😄 Юмор", callback_data="niche_юмор")],
            [InlineKeyboardButton(text="💡 Лайфхаки", callback_data="niche_лайфхаки")],
            [InlineKeyboardButton(text="🚀 Мотивация", callback_data="niche_мотивация")],
            [InlineKeyboardButton(text="📱 Обзоры", callback_data="niche_обзоры")],
            [InlineKeyboardButton(text="🎓 Образование", callback_data="niche_образование")],
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
            [InlineKeyboardButton(text="📚 Образовательный", callback_data="style_образовательный")]
        ]
    )
    return keyboard


# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    # Трекинг активных пользователей (для обратной совместимости)
    active_users.add(user_id)
    registered_users.add(user_id)
    # Регистрация в БД
    Database.register_user(user_id)
    logger.info(f"Пользователь {user_id} зарегистрирован через /start")
    
    welcome_text = (
        "🎬 <b>Добро пожаловать в ReelsScript Bot!</b>\n\n"
        "Я помогаю создавать сценарии для рилсов, TikTok и YouTube Shorts.\n\n"
        "Просто нажми <b>«Создать сценарий»</b> и я сгенерирую для тебя уникальный сценарий!\n\n"
        "Возможности:\n"
        "✨ Разные ниши (юмор, лайфхаки, мотивация и др.)\n"
        "⏱️ Разные форматы (15 сек, 30 сек, 60 сек, longform)\n"
        "🎨 Разные стили (динамичный, спокойный, драматичный)\n"
        "📝 Визуальные подсказки и хэштеги\n\n"
        "Начнем? 🚀"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Обработчик команды /help"""
    active_users.add(message.from_user.id)
    help_text = (
        "📖 <b>Справка по использованию бота</b>\n\n"
        "<b>Команды:</b>\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать эту справку\n"
        "/new - Создать новый сценарий\n"
        "/subscribe - Информация о подписке\n"
        "/my_subscription - Моя подписка\n"
        "/support - Связаться с поддержкой\n\n"
        "<b>Как использовать:</b>\n"
        "1. Нажми «Создать сценарий»\n"
        "2. Выбери нишу контента\n"
        "3. Выбери формат видео\n"
        "4. Выбери стиль сценария\n"
        "5. (Опционально) Укажи тему\n"
        "6. Получи готовый сценарий!\n\n"
        "💡 <b>Совет:</b> Чем больше деталей ты укажешь, тем лучше будет сценарий!"
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command("new"))
async def cmd_new(message: types.Message, state: FSMContext):
    """Обработчик команды /new - начать создание нового сценария"""
    # Проверяем лимиты
    can_request, error_msg = LimitsManager.can_make_request(message.from_user.id)
    if not can_request:
        await message.answer(error_msg, parse_mode="HTML")
        return
    
    await state.clear()
    await ask_for_niche(message)


@dp.message(F.text == "🎬 Создать сценарий")
async def create_scenario(message: types.Message, state: FSMContext):
    """Обработчик кнопки создания сценария"""
    # Проверяем лимиты
    can_request, error_msg = LimitsManager.can_make_request(message.from_user.id)
    if not can_request:
        await message.answer(error_msg, parse_mode="HTML")
        return
    
    await ask_for_niche(message)


@dp.message(F.text == "ℹ️ Помощь")
async def show_help(message: types.Message):
    """Обработчик кнопки помощи"""
    await cmd_help(message)


@dp.message(F.text == "💎 Подписка")
async def show_subscription(message: types.Message):
    """Обработчик кнопки подписки"""
    await cmd_subscribe(message)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    """Обработчик команды /subscribe - показать информацию о подписке"""
    user_id = message.from_user.id
    
    # Проверяем текущую подписку
    subscription_info = SubscriptionManager.get_subscription_info(user_id)
    has_premium = SubscriptionManager.has_active_subscription(user_id)
    
    if has_premium and subscription_info:
        # У пользователя есть активная подписка
        days_left = subscription_info["days_left"]
        expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y")
        
        text = (
            "💎 <b>У вас активная премиум подписка!</b>\n\n"
            f"📅 <b>Действует до:</b> {expires_at}\n"
            f"⏰ <b>Осталось дней:</b> {days_left}\n\n"
            "✅ <b>Преимущества:</b>\n"
            "• Безлимитное количество запросов\n"
            "• Приоритетная поддержка\n"
            "• Доступ ко всем функциям\n\n"
            "Спасибо за поддержку! 🙏"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        # Показываем информацию о подписке и кнопку оплаты
        remaining = LimitsManager.get_remaining_requests(user_id)
        remaining_text = f"Осталось запросов: {remaining}" if remaining != -1 else "Безлимит"
        
        text = (
            "💎 <b>Премиум подписка</b>\n\n"
            f"📊 <b>Текущий статус:</b> Бесплатный тариф\n"
            f"📈 {remaining_text}\n\n"
            "✨ <b>Что дает премиум подписка:</b>\n"
            "• 🚀 Безлимитное количество запросов\n"
            "• ⚡ Приоритетная обработка\n"
            "• 🎯 Доступ ко всем функциям\n"
            "• 💬 Приоритетная поддержка\n\n"
            f"💰 <b>Цена:</b> {SUBSCRIPTION_PRICE / 100:.0f} ₽ на {SUBSCRIPTION_DURATION_DAYS} дней\n\n"
            "Оформить подписку?"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_subscription")],
                [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="subscription_info")]
            ]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.message(Command("my_subscription"))
async def cmd_my_subscription(message: types.Message):
    active_users.add(message.from_user.id)
    """Обработчик команды /my_subscription - показать информацию о текущей подписке"""
    user_id = message.from_user.id
    subscription_info = SubscriptionManager.get_subscription_info(user_id)
    
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


@dp.callback_query(F.data == "buy_subscription")
async def buy_subscription_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Обработка кнопки покупки подписки"""
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer(
            "❌ <b>Платежи не настроены</b>\n\n"
            "Для работы платежей необходимо настроить PAYMENT_PROVIDER_TOKEN в .env файле.\n"
            "Получите токен у @BotFather после настройки платежей.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Проверяем, нет ли уже активной подписки
    if SubscriptionManager.has_active_subscription(callback.from_user.id):
        await callback.message.answer(
            "✅ У вас уже есть активная премиум подписка!\n"
            "Используйте /my_subscription чтобы посмотреть детали.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Создаем invoice для оплаты
    try:
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title="💎 Премиум подписка",
            description=f"Безлимитное количество запросов на {SUBSCRIPTION_DURATION_DAYS} дней",
            payload=f"subscription_{callback.from_user.id}",
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=[types.LabeledPrice(label="Премиум подписка", amount=SUBSCRIPTION_PRICE)],
            start_parameter="subscription",
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
    except Exception as e:
        logger.error(f"Ошибка при создании invoice: {e}")
        await callback.message.answer(
            "❌ Произошла ошибка при создании платежа. Попробуйте позже.",
            parse_mode="HTML"
        )
        await callback.answer()


@dp.callback_query(F.data == "subscription_info")
async def subscription_info_callback(callback: types.CallbackQuery):
    active_users.add(callback.from_user.id)
    """Обработка кнопки информации о подписке"""
    text = (
        "ℹ️ <b>О подписке</b>\n\n"
        "<b>Бесплатный тариф:</b>\n"
        f"• {MAX_REQUESTS_PER_USER} запросов\n"
        "• Лимит сбрасывается при перезапуске бота\n\n"
        "<b>Премиум подписка:</b>\n"
        f"• Безлимитное количество запросов\n"
        f"• Действует {SUBSCRIPTION_DURATION_DAYS} дней\n"
        f"• Цена: {SUBSCRIPTION_PRICE / 100:.0f} ₽\n\n"
        "<b>Что включено:</b>\n"
        "• Генерация неограниченного количества сценариев\n"
        "• Развитие и доработка сценариев без ограничений\n"
        "• Приоритетная поддержка\n"
        "• Доступ ко всем функциям бота"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    """Обработка pre-checkout запроса (перед оплатой)"""
    # Проверяем payload
    if pre_checkout_query.invoice_payload.startswith("subscription_"):
        # Подтверждаем платеж
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    else:
        # Отклоняем неизвестный платеж
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неизвестный платеж")


@dp.message(F.content_type == "successful_payment")
async def successful_payment_handler(message: types.Message):
    """Обработка успешной оплаты"""
    user_id = message.from_user.id
    payment = message.successful_payment
    
    # Активируем подписку
    SubscriptionManager.activate_subscription(user_id, SUBSCRIPTION_DURATION_DAYS)
    
    # Сбрасываем счетчик запросов (так как теперь безлимит)
    LimitsManager.reset_user_requests(user_id)
    
    subscription_info = SubscriptionManager.get_subscription_info(user_id)
    expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y")
    
    text = (
        "🎉 <b>Спасибо за покупку!</b>\n\n"
        "✅ <b>Премиум подписка активирована!</b>\n\n"
        f"📅 <b>Действует до:</b> {expires_at}\n\n"
        "Теперь у вас безлимитное количество запросов!\n"
        "Можете создавать сколько угодно сценариев! 🚀"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Создать сценарий", callback_data="new_scenario")]
        ]
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def ask_for_niche(message: types.Message):
    """Спросить пользователя о нише"""
    text = (
        "🎯 <b>Шаг 1/4: Выбери нишу контента</b>\n\n"
        "Какой тип контента ты хочешь создать?"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_niche_keyboard())


# Обработчики callback-запросов
@dp.callback_query(F.data.startswith("niche_"))
async def process_niche(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка выбора ниши"""
    niche = callback.data.replace("niche_", "")
    await state.update_data(niche=niche)
    
    text = (
        f"✅ Ниша: <b>{niche}</b>\n\n"
        "⏱️ <b>Шаг 2/4: Выбери формат видео</b>\n\n"
        "Какой длительности будет твое видео?"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_format_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("format_"))
async def process_format(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка выбора формата"""
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
    await callback.answer()


@dp.callback_query(F.data.startswith("style_"))
async def process_style(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка выбора стиля"""
    style = callback.data.replace("style_", "")
    await state.update_data(style=style)
    
    data = await state.get_data()
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
    await callback.answer()


@dp.message(StateFilter(ScenarioStates.waiting_for_topic), Command("skip"))
async def skip_topic(message: types.Message, state: FSMContext):
    """Пропуск темы"""
    await generate_and_send_scenario(message, state)


@dp.message(StateFilter(ScenarioStates.waiting_for_topic))
async def process_topic(message: types.Message, state: FSMContext):
    active_users.add(message.from_user.id)
    """Обработка темы"""
    topic = message.text
    await state.update_data(topic=topic)
    await generate_and_send_scenario(message, state)


async def generate_and_send_scenario(message: types.Message, state: FSMContext):
    """Генерация и отправка сценария"""
    data = await state.get_data()
    
    # Отправляем сообщение о генерации
    status_msg = await message.answer("⏳ Генерирую сценарий... Это может занять несколько секунд.")
    
    # Генерируем сценарий в отдельном потоке, чтобы не блокировать event loop
    loop = asyncio.get_event_loop()
    scenario = await loop.run_in_executor(
        None,
        lambda: scenario_generator.generate_scenario(
            niche=data.get('niche', 'общее'),
            format_type=data.get('format_type', '60 секунд'),
            style=data.get('style', 'динамичный'),
            topic=data.get('topic'),
            additional_info=None
        )
    )
    
    # Удаляем сообщение о статусе
    await status_msg.delete()
    
    # Проверяем, является ли ответ ошибкой (начинается с эмодзи ошибки)
    is_error = scenario.startswith(("⚠️", "❌", "🔑", "🌐", "⏳"))
    
    if is_error:
        # Если это ошибка, отправляем её как есть
        await message.answer(scenario, parse_mode="HTML")
        
        # Предлагаем попробовать снова
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="new_scenario")]
            ]
        )
        await message.answer("Попробуй создать сценарий снова после решения проблемы.", reply_markup=keyboard)
    else:
        # Форматируем успешный ответ
        response_text = (
            "🎬 <b>Твой сценарий готов!</b>\n\n"
            f"<b>Ниша:</b> {data.get('niche')}\n"
            f"<b>Формат:</b> {data.get('format_type')}\n"
            f"<b>Стиль:</b> {data.get('style')}\n"
        )
        if data.get('topic'):
            response_text += f"<b>Тема:</b> {data.get('topic')}\n"
        response_text += "\n" + "="*30 + "\n\n"
        response_text += scenario
        
        # Если сообщение слишком длинное, разбиваем на части
        if len(response_text) > 4096:
            # Отправляем первую часть
            await message.answer(response_text[:4096], parse_mode="HTML")
            # Отправляем остаток
            await message.answer(response_text[4096:], parse_mode="HTML")
        else:
            await message.answer(response_text, parse_mode="HTML")
        
        # Сохраняем сценарий для возможного развития
        await state.update_data(last_scenario=scenario)
        
        # Показываем информацию о лимитах
        remaining = LimitsManager.get_remaining_requests(message.from_user.id)
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
        
        # Предлагаем создать новый сценарий или развить идею
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✨ Развить идею", callback_data="improve_scenario"),
                    InlineKeyboardButton(text="🔄 Новый сценарий", callback_data="new_scenario")
                ]
            ]
        )
        await message.answer(f"Что дальше?{limits_text}", reply_markup=keyboard)
        
        # Увеличиваем счетчик запросов
        LimitsManager.increment_request(message.from_user.id, active_users)
    
    # НЕ очищаем состояние - сохраняем для развития идеи


@dp.callback_query(F.data == "new_scenario")
async def new_scenario_callback(callback: types.CallbackQuery, state: FSMContext):
    active_users.add(callback.from_user.id)
    """Обработка кнопки создания нового сценария"""
    # Проверяем лимиты
    can_request, error_msg = LimitsManager.can_make_request(callback.from_user.id)
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
    # Проверяем лимиты
    can_request, error_msg = LimitsManager.can_make_request(callback.from_user.id)
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
    
    # Отправляем сообщение о генерации
    status_msg = await message.answer("⏳ Улучшаю сценарий... Это может занять несколько секунд.")
    
    # Генерируем улучшенный сценарий в отдельном потоке, чтобы не блокировать event loop
    loop = asyncio.get_event_loop()
    improved_scenario = await loop.run_in_executor(
        None,
        lambda: scenario_generator.improve_scenario(last_scenario, improvement_request)
    )
    
    # Удаляем сообщение о статусе
    await status_msg.delete()
    
    # Проверяем, является ли ответ ошибкой
    is_error = improved_scenario.startswith(("⚠️", "❌", "🔑", "🌐", "⏳"))
    
    if is_error:
        await message.answer(improved_scenario, parse_mode="HTML")
        await state.set_state(None)
    else:
        # Очищаем состояние ПЕРЕД отправкой ответа, чтобы избежать повторной обработки
        await state.set_state(None)
        
        # Отправляем заголовок отдельным сообщением
        header_text = (
            "✨ <b>Улучшенный сценарий готов!</b>\n\n"
            f"<b>Твой запрос:</b> {improvement_request}"
        )
        await message.answer(header_text, parse_mode="HTML")
        
        # Отправляем сам сценарий отдельным сообщением
        # Если сценарий слишком длинный, разбиваем на части
        if len(improved_scenario) > 4096:
            await message.answer(improved_scenario[:4096], parse_mode="HTML")
            await message.answer(improved_scenario[4096:], parse_mode="HTML")
        else:
            await message.answer(improved_scenario, parse_mode="HTML")
        
        # Обновляем сохраненный сценарий (сохраняем для возможного дальнейшего развития)
        await state.update_data(last_scenario=improved_scenario)
        
        # Показываем информацию о лимитах
        remaining = LimitsManager.get_remaining_requests(message.from_user.id)
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
        
        # Предлагаем действия
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✨ Развить еще", callback_data="improve_scenario"),
                    InlineKeyboardButton(text="🔄 Новый сценарий", callback_data="new_scenario")
                ]
            ]
        )
        await message.answer(f"Что дальше?{limits_text}", reply_markup=keyboard)
        
        # Увеличиваем счетчик запросов
        LimitsManager.increment_request(message.from_user.id, active_users)


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
    
    # Формируем сообщение для разработчиков
    dev_message = (
        f"💬 <b>Новое обращение в поддержку</b>\n\n"
        f"<b>Пользователь:</b> {user_name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Сообщение:</b>\n{support_message}"
    )
    
    # Отправляем всем разработчикам
    sent_count = 0
    for dev_id in DEVELOPER_USER_IDS:
        try:
            # Пересылаем оригинальное сообщение для удобства ответа
            await bot.forward_message(
                chat_id=dev_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            # Отправляем информацию о пользователе
            await bot.send_message(
                chat_id=dev_id,
                text=dev_message,
                parse_mode="HTML"
            )
            sent_count += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения разработчику {dev_id}: {e}")
    
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
    # Проверяем, является ли отправитель разработчиком
    if not LimitsManager.is_developer(message.from_user.id):
        return  # Игнорируем, если не разработчик
    
    # Проверяем, что это ответ на пересланное сообщение
    replied_message = message.reply_to_message
    
    if not replied_message:
        return
    
    # Проверяем, что это пересланное сообщение от пользователя
    if not replied_message.forward_from:
        # Если forward_from отсутствует (пользователь скрыл пересылку), 
        # пытаемся найти user_id в тексте сообщения
        if replied_message.text and "ID:" in replied_message.text:
            # Пытаемся извлечь ID из текста
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
    
    # Отправляем ответ пользователю
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


# ==================== АДМИНИСТРАТИВНЫЕ КОМАНДЫ ДЛЯ РАЗРАБОТЧИКОВ ====================

class IsDeveloperFilter(BaseFilter):
    """Фильтр для проверки, является ли пользователь разработчиком"""
    async def __call__(self, message: types.Message) -> bool:
        user_id = message.from_user.id
        is_dev = LimitsManager.is_developer(user_id)
        logger.info(f"Проверка разработчика: user_id={user_id} (тип: {type(user_id)}), is_dev={is_dev}, DEVELOPER_USER_IDS={DEVELOPER_USER_IDS}")
        return is_dev


@dp.message(Command("admin"), IsDeveloperFilter())
async def cmd_admin(message: types.Message):
    """Список административных команд"""
    logger.info(f"Команда /admin выполнена пользователем {message.from_user.id}")
    admin_text = (
        "🔧 <b>Административные команды</b>\n\n"
        "<b>Управление подписками:</b>\n"
        "• <code>/give_sub &lt;user_id&gt; [days]</code> - Выдать подписку\n"
        "• <code>/remove_sub &lt;user_id&gt;</code> - Удалить подписку\n\n"
        "<b>Информация:</b>\n"
        "• <code>/user_info &lt;user_id&gt;</code> - Информация о пользователе\n"
        "• <code>/stats</code> - Статистика бота\n\n"
        "<b>Управление лимитами:</b>\n"
        "• <code>/reset_user &lt;user_id&gt;</code> - Сбросить лимиты пользователя\n\n"
        "<b>Рассылка:</b>\n"
        "• <code>/broadcast &lt;сообщение&gt;</code> - Рассылка всем пользователям\n\n"
        "<b>Примеры:</b>\n"
        "<code>/give_sub 123456789 30</code> - Выдать подписку на 30 дней\n"
        "<code>/user_info 123456789</code> - Информация о пользователе\n"
        "<code>/stats</code> - Статистика"
    )
    await message.answer(admin_text, parse_mode="HTML")


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
        
        SubscriptionManager.activate_subscription(user_id, days)
        subscription_info = SubscriptionManager.get_subscription_info(user_id)
        
        expires_at_str = subscription_info['expires_at'].strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"✅ <b>Подписка выдана!</b>\n\n"
            f"<b>Пользователь:</b> {user_id}\n"
            f"<b>Действует до:</b> {expires_at_str}\n"
            f"<b>Осталось дней:</b> {subscription_info['days_left']}",
            parse_mode="HTML"
        )
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🎉 <b>Тебе выдана Премиум подписка!</b>\n\n"
                     f"Теперь у тебя безлимитный доступ к генерации сценариев!",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")
            
    except ValueError:
        await message.answer("❌ Неверный формат. user_id и days должны быть числами.")
    except Exception as e:
        logger.error(f"Ошибка при выдаче подписки: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


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
        had_subscription = SubscriptionManager.has_active_subscription(user_id)
        
        SubscriptionManager.cancel_subscription(user_id)
        
        if had_subscription:
            await message.answer(
                f"✅ <b>Подписка удалена</b>\n\n"
                f"Пользователь {user_id} больше не имеет премиум подписки.",
                parse_mode="HTML"
            )
            
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text="ℹ️ <b>Твоя Премиум подписка была отменена.</b>\n\n"
                         "Ты можешь оформить новую подписку через /subscribe",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")
        else:
            await message.answer(f"ℹ️ У пользователя {user_id} не было активной подписки.")
            
    except ValueError:
        await message.answer("❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при удалении подписки: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


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
        
        # Получаем информацию о пользователе
        is_dev = LimitsManager.is_developer(user_id)
        has_premium = SubscriptionManager.has_active_subscription(user_id)
        requests_count = Database.get_user_requests_count(user_id)
        remaining = LimitsManager.get_remaining_requests(user_id)
        subscription_info = SubscriptionManager.get_subscription_info(user_id)
        
        # Пытаемся получить информацию о пользователе из Telegram
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
        
        await message.answer(info_text, parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Неверный формат. user_id должно быть числом.")
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пользователе: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


@dp.message(Command("stats"), IsDeveloperFilter())
async def cmd_stats(message: types.Message):
    """Статистика бота"""
    total_registered = Database.get_registered_users_count()
    total_active = Database.get_active_users_count()
    total_requests = Database.get_total_requests_count()
    users_with_requests = Database.get_users_with_requests_count()
    active_subscriptions = len(SubscriptionManager.get_all_subscriptions())
    premium_users = active_subscriptions  # Количество активных подписок = премиум пользователей
    
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
        f"• Активных подписок: {active_subscriptions}\n"
    )
    
    await message.answer(stats_text, parse_mode="HTML")


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
        had_requests = LimitsManager.get_user_requests_count(user_id) > 0
        
        LimitsManager.reset_user_requests(user_id)
        
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
    total_users = Database.get_registered_users_count()
    
    # Подтверждение
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
    
    # Получаем список пользователей из БД
    user_ids = Database.get_all_active_user_ids()
    total_users = len(user_ids)
    sent_count = 0
    failed_count = 0
    
    status_msg = await message.answer(f"📤 Начинаю рассылку... 0/{total_users}")
    
    # Используем семафор для ограничения параллельных запросов (не более 30 одновременно)
    # Это предотвращает перегрузку Telegram API
    semaphore = asyncio.Semaphore(30)
    
    async def send_to_user(user_id: int):
        """Отправка сообщения одному пользователю"""
        nonlocal sent_count, failed_count
        async with semaphore:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"📢 <b>Рассылка от администрации:</b>\n\n{broadcast_text}",
                    parse_mode="HTML"
                )
                sent_count += 1
                # Обновляем статус каждые 10 сообщений
                if sent_count % 10 == 0:
                    try:
                        await status_msg.edit_text(f"📤 Рассылка... {sent_count}/{total_users}")
                    except:
                        pass  # Игнорируем ошибки редактирования статуса
            except Exception as e:
                failed_count += 1
                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
    
    # Создаем задачи для всех пользователей и выполняем параллельно
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
    
    # Парсим команду: /reply <user_id> <сообщение>
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


# Обработчик для административных команд, когда пользователь не разработчик
# Должен быть ПОСЛЕ всех обработчиков с фильтром
@dp.message(Command("admin", "give_sub", "remove_sub", "user_info", "stats", "reset_user", "broadcast"))
async def admin_commands_not_developer(message: types.Message):
    """Обработчик для административных команд, когда пользователь не разработчик"""
    if not LimitsManager.is_developer(message.from_user.id):
        await message.answer("❌ Эта команда доступна только разработчикам.")


@dp.message()
async def echo_handler(message: types.Message):
    """Обработчик всех остальных сообщений"""
    # Трекинг активных пользователей для всех сообщений
    active_users.add(message.from_user.id)
    
    await message.answer(
        "🤔 Я не понял тебя. Используй команду /help для справки или нажми кнопку «Создать сценарий»",
        reply_markup=get_main_keyboard()
    )


async def main():
    """Главная функция запуска бота"""
    logger.info("Запуск бота...")
    # Инициализация базы данных
    init_database()
    logger.info("База данных инициализирована")
    
    logger.info(f"DEVELOPER_USER_IDS загружены: {DEVELOPER_USER_IDS} (тип: {type(DEVELOPER_USER_IDS)})")
    for dev_id in DEVELOPER_USER_IDS:
        logger.info(f"  - Developer ID: {dev_id} (тип: {type(dev_id)})")
    
    # Очистка истекших подписок при запуске
    Database.cleanup_expired_subscriptions()
    
    try:
        # Настраиваем polling с оптимизацией для параллельной обработки
        await dp.start_polling(
            bot, 
            skip_updates=True,
            # Увеличиваем количество одновременных обновлений
            # Это позволяет обрабатывать несколько запросов параллельно
            allowed_updates=["message", "callback_query", "pre_checkout_query"]
        )
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

