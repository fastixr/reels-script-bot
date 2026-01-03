import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения!")

# AI Provider Configuration
# Доступные провайдеры: "groq", "openai", "yandexgpt"
AI_PROVIDER = os.getenv("AI_PROVIDER", "yandexgpt").lower()

# Groq Configuration (бесплатный, быстрый)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")  # или "mixtral-8x7b-32768"

# OpenAI Configuration (платный)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# YandexGPT Configuration (есть бесплатный тариф)
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_MODEL = os.getenv("YANDEX_MODEL", "yandexgpt/latest")

# Проверка наличия ключа для выбранного провайдера
if AI_PROVIDER == "groq":
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не найден! Получите бесплатный ключ на https://console.groq.com/keys")
elif AI_PROVIDER == "openai":
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY не найден в переменных окружения!")
elif AI_PROVIDER == "yandexgpt":
    if not YANDEX_API_KEY:
        raise ValueError("YANDEX_API_KEY не найден! Получите ключ на https://yandex.cloud/ru/docs/foundation-models/operations/create-api-key")
    if not YANDEX_FOLDER_ID:
        raise ValueError("YANDEX_FOLDER_ID не найден! Это ID папки в Yandex Cloud")
else:
    raise ValueError(f"Неизвестный провайдер: {AI_PROVIDER}. Доступны: groq, openai, yandexgpt")

# Настройки бота
MAX_SCENARIO_LENGTH = 5000  # Максимальная длина сценария

# Система лимитов
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "10"))  # Лимит запросов на пользователя
# Whitelist для разработчиков (безлимит) - через запятую, например: "123456789,987654321"
DEVELOPER_USER_IDS = [
    int(uid.strip()) for uid in os.getenv("DEVELOPER_USER_IDS", "").split(",") 
    if uid.strip().isdigit()
]

# Настройки подписок
# Telegram Payments Provider Token (получите у @BotFather после настройки платежей)
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
# Цена подписки в копейках (например, 29900 = 299 рублей)
SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", "29900"))  # 299 рублей по умолчанию
# Длительность подписки в днях
SUBSCRIPTION_DURATION_DAYS = int(os.getenv("SUBSCRIPTION_DURATION_DAYS", "30"))

