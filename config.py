import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения!")

# AI Provider Configuration
# Доступные провайдеры: "groq", "openai", "yandexgpt", "amvera"
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

# Amvera LLM Configuration
# API endpoint: https://kong-proxy.yc.amvera.ru/api/v1
# Авторизация: X-Auth-Token: Bearer <access_token>
# Документация: https://docs.amvera.ru
AMVERA_API_BASE_URL = os.getenv("AMVERA_API_BASE_URL", "https://kong-proxy.yc.amvera.ru/api/v1")
AMVERA_ACCESS_TOKEN = os.getenv("AMVERA_ACCESS_TOKEN", None)  # Токен доступа из ЛК Amvera
AMVERA_MODEL = os.getenv("AMVERA_MODEL", "deepseek-V3")  # Модель: deepseek-V3, deepseek-R1, gpt-5, gpt-4.1, llama8b, llama70b, qwen3_30b, qwen3_235b

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
elif AI_PROVIDER == "amvera":
    if not AMVERA_ACCESS_TOKEN:
        raise ValueError("AMVERA_ACCESS_TOKEN не найден! Получите токен в ЛК Amvera в разделе LLM")
else:
    raise ValueError(f"Неизвестный провайдер: {AI_PROVIDER}. Доступны: groq, openai, yandexgpt, amvera")

# Настройки бота
MAX_SCENARIO_LENGTH = 5000  # Максимальная длина сценария

# Система лимитов
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "5"))  # Лимит запросов на пользователя (бесплатные попытки)
# Whitelist для разработчиков (безлимит) - через запятую, например: "123456789,987654321"
DEVELOPER_USER_IDS = [
    int(uid.strip()) for uid in os.getenv("DEVELOPER_USER_IDS", "").split(",") 
    if uid.strip().isdigit()
]

# Настройки подписок
# Выбор платежной системы: "telegram_payments" (PayMaster через Telegram) или "robokassa" (Robokassa API)
PAYMENT_SYSTEM = os.getenv("PAYMENT_SYSTEM", "robokassa").lower()

# Telegram Payments Provider Token (для PayMaster и других через Telegram Payments)
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

# Robokassa Configuration (для прямой интеграции через API)
ROBOKASSA_MERCHANT_LOGIN = os.getenv("ROBOKASSA_MERCHANT_LOGIN", "reelsgenaibot")
ROBOKASSA_IS_TEST = os.getenv("ROBOKASSA_IS_TEST", "true").lower() == "true"  # Тестовый режим

# Пароли для боевого режима
ROBOKASSA_PASSWORD1 = os.getenv("ROBOKASSA_PASSWORD1", "")  # Пароль #1 для создания ссылок (боевой режим)
ROBOKASSA_PASSWORD2 = os.getenv("ROBOKASSA_PASSWORD2", "")  # Пароль #2 для проверки уведомлений (боевой режим)

# Пароли для тестового режима (если ROBOKASSA_IS_TEST=true, используются эти пароли)
ROBOKASSA_TEST_PASSWORD1 = os.getenv("ROBOKASSA_TEST_PASSWORD1", "")  # Пароль #1 для тестового режима
ROBOKASSA_TEST_PASSWORD2 = os.getenv("ROBOKASSA_TEST_PASSWORD2", "")  # Пароль #2 для тестового режима
ROBOKASSA_SUCCESS_URL = os.getenv("ROBOKASSA_SUCCESS_URL", "https://t.me/reelsAIgenbot")
ROBOKASSA_FAIL_URL = os.getenv("ROBOKASSA_FAIL_URL", "https://t.me/reelsAIgenbot")
# Result URL будет формироваться автоматически на основе PUBLIC_URL или AMVERA_PUBLIC_URL
# Если не указан, нужно будет указать вручную в настройках Robokassa
ROBOKASSA_RESULT_URL = os.getenv("ROBOKASSA_RESULT_URL", "")

# Порт для HTTP сервера (для Robokassa Result URL)
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "80"))

# Настройки сокращения ссылок (clck.su)
# API ключ для clck.su (опционально, если не указан - используются длинные ссылки)
CLCK_API_KEY = os.getenv("CLCK_API_KEY", "")
CLCK_ENABLED = os.getenv("CLCK_ENABLED", "false").lower() == "true"  # Включить сокращение ссылок

# Настройки фискализации (Робочеки)
# Включить автоматическую фискализацию через Робочеки Robokassa
ROBOKASSA_FISCAL_ENABLED = os.getenv("ROBOKASSA_FISCAL_ENABLED", "true").lower() == "true"
# Налоговая ставка для услуг: "none" (без НДС), "vat0" (НДС 0%), "vat10" (НДС 10%), "vat20" (НДС 20%)
# Для ИП на УСН обычно используется "vat0" или "none"
ROBOKASSA_TAX_RATE = os.getenv("ROBOKASSA_TAX_RATE", "vat0")

# Цены подписок в копейках (можно переопределить через .env)
# Расчет для старта: хостинг ~500₽/мес, нужно минимум 3 подписчика для окупаемости
# Учитываем комиссии платежей (~3-5%) и налоги
# Для старта: более доступные цены для привлечения пользователей
SUBSCRIPTION_PRICE_1_MONTH = int(os.getenv("SUBSCRIPTION_PRICE_1_MONTH", "19900"))  # 199₽ за 1 месяц (стартовая цена)
SUBSCRIPTION_PRICE_3_MONTHS = int(os.getenv("SUBSCRIPTION_PRICE_3_MONTHS", "49900"))  # 499₽ за 3 месяца (скидка ~17%)
SUBSCRIPTION_PRICE_6_MONTHS = int(os.getenv("SUBSCRIPTION_PRICE_6_MONTHS", "89900"))  # 899₽ за 6 месяцев (скидка ~25%)
SUBSCRIPTION_PRICE_1_YEAR = int(os.getenv("SUBSCRIPTION_PRICE_1_YEAR", "159900"))  # 1599₽ за год (скидка ~33%)

# Дополнительные попытки (цена за 1 попытку в копейках)
# Более доступные цены для привлечения пользователей на старте
EXTRA_REQUEST_PRICE = int(os.getenv("EXTRA_REQUEST_PRICE", "3000"))  # 30₽ за 1 попытку
EXTRA_REQUESTS_PACK_10 = int(os.getenv("EXTRA_REQUESTS_PACK_10", "24900"))  # 249₽ за 10 попыток (скидка 17%)
EXTRA_REQUESTS_PACK_25 = int(os.getenv("EXTRA_REQUESTS_PACK_25", "54900"))  # 549₽ за 25 попыток (скидка 27%)
EXTRA_REQUESTS_PACK_50 = int(os.getenv("EXTRA_REQUESTS_PACK_50", "99900"))  # 999₽ за 50 попыток (скидка 33%)

# Для обратной совместимости (старые переменные)
SUBSCRIPTION_PRICE = SUBSCRIPTION_PRICE_1_MONTH
SUBSCRIPTION_DURATION_DAYS = 30

# PostgreSQL Configuration
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "telegram_bot")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", None)

# Channel subscription check
REQUIRED_CHANNEL_USERNAME = os.getenv("REQUIRED_CHANNEL_USERNAME", "reelsAIcontent")  # Без @
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/reelsAIcontent")

# Оферта (публичная оферта)
OFFER_DOCUMENT_URL = os.getenv("OFFER_DOCUMENT_URL", "https://drive.google.com/uc?export=download&id=1CPu0M44BkkbgqBVqkfPhGIaUH7QugjcP")

# Rate Limiting Configuration
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_MESSAGES_PER_MINUTE = int(os.getenv("RATE_LIMIT_MESSAGES_PER_MINUTE", "20"))  # Сообщений в минуту
RATE_LIMIT_CALLBACKS_PER_MINUTE = int(os.getenv("RATE_LIMIT_CALLBACKS_PER_MINUTE", "30"))  # Callback'ов в минуту

# YouTube API Configuration (опционально)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")  # YouTube Data API v3 ключ (опционально)

# Yandex SpeechKit использует те же ключи, что и YandexGPT (YANDEX_API_KEY и YANDEX_FOLDER_ID)

