from openai import OpenAI, APIError, RateLimitError, APIConnectionError, APITimeoutError
from config import (
    AI_PROVIDER, 
    GROQ_API_KEY, GROQ_MODEL,
    OPENAI_API_KEY, OPENAI_MODEL,
    YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL,
    AMVERA_API_BASE_URL, AMVERA_ACCESS_TOKEN, AMVERA_MODEL
)
import logging
import json
import re
import time
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

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


def _get_season_context() -> str:
    """Возвращает контекст текущего времени года для промпта"""
    now = datetime.now()
    month = now.month
    
    if month in [12, 1, 2]:
        return (
            "Сейчас зима (декабрь-февраль). Учитывай актуальные зимние темы: "
            "новогодние праздники, зимний отдых, теплые напитки, домашний уют, "
            "зимние виды спорта, зимняя мода и аксессуары, праздничное настроение."
        )
    elif month in [3, 4, 5]:
        return (
            "Сейчас весна (март-май). Учитывай актуальные весенние темы: "
            "обновление гардероба, весенние праздники (8 марта, Пасха), "
            "активный образ жизни, красота и уход за собой, весеннее настроение, "
            "приготовление к лету, домашние дела и ремонт."
        )
    elif month in [6, 7, 8]:
        return (
            "Сейчас лето (июнь-август). Учитывай актуальные летние темы: "
            "отпуск и путешествия, пляжный отдых, летние активности, "
            "летняя мода, свежие фрукты и овощи, активный образ жизни на свежем воздухе, "
            "летние фестивали и мероприятия, дача и садоводство."
        )
    else:  # 9, 10, 11
        return (
            "Сейчас осень (сентябрь-ноябрь). Учитывай актуальные осенние темы: "
            "возвращение к учебе и работе, осенняя мода и стиль, "
            "уютные домашние вечера, горячие напитки, осенние праздники, "
            "подготовка к зиме, хобби и творчество, осенняя кулинария."
        )


def _get_trending_topics() -> str:
    """Возвращает актуальные тренды для соцсетей с конкретными форматами"""
    now = datetime.now()
    trends = []
    
    # Конкретные популярные форматы контента (работают постоянно)
    trends.append(
        "ПОПУЛЯРНЫЕ ФОРМАТЫ (всегда актуальны): "
        "'POV: ты...' (point of view), 'Расскажи о...', 'Хаки для...', "
        "'Проблема-Решение за 30 сек', 'Тренды vs Реальность', "
        "'Что я узнал за...', 'Мифы развеиваю', 'Сравнение до/после', "
        "'Day in my life', 'Неожиданный поворот', 'Реакция на...', "
        "'Челлендж', 'Секреты раскрываю', 'Как я...', 'Это изменило мою жизнь', "
        "'Ошибки, которые все делают', 'Лайфхаки, которые реально работают'."
    )
    
    # Стиль подачи для молодежной аудитории
    trends.append(
        "СТИЛЬ: Пиши для Gen Z (16-25 лет). Используй: "
        "короткие предложения, энергичный тон, "
        "современный сленг (но не перебор), "
        "эмодзи в тексте (но умеренно), "
        "обращения 'ты' вместо 'вы', "
        "прямой, честный разговор без воды, "
        "цепляющие вопросы в начале, "
        "призывы к действию через вызов/любопытство."
    )
    
    # Сезонные тренды
    month = now.month
    if month == 12:
        trends.append("Декабрь: новогодняя тематика, подведение итогов года, планы на новый год, 'Что изменилось за год', 'Новогодние привычки'.")
    elif month == 1:
        trends.append("Январь: новые цели, изменения в жизни, мотивация на год, здоровый образ жизни, 'Челленджи на январь', 'Как я изменился за месяц'.")
    elif month in [2, 3]:
        trends.append("Февраль-март: весеннее настроение, обновления, активность, красота, 'Весенний челлендж', 'Обновляю гардероб', 'Новые привычки к весне'.")
    elif month in [6, 7]:
        trends.append("Июнь-июль: лето, отпуска, путешествия, активный отдых, 'Летний чеклист', 'Как провести лето круто', 'Летние тренды'.")
    elif month in [9, 10]:
        trends.append("Сентябрь-октябрь: возвращение к рутине, осенний уют, продуктивность, 'Осенние привычки', 'Как вернуться к режиму', 'Осенние тренды'.")
    
    return " | ".join(trends)

class ScenarioGenerator:
    """Класс для генерации сценариев для рилсов с помощью различных AI провайдеров"""
    
    def __init__(self):
        self.provider = AI_PROVIDER
        
        if self.provider == "groq":
            self.client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1"
            )
            self.model = GROQ_MODEL
        elif self.provider == "openai":
            self.client = OpenAI(api_key=OPENAI_API_KEY)
            self.model = OPENAI_MODEL
        elif self.provider == "yandexgpt":
            self.client = None
            self.model = YANDEX_MODEL
        elif self.provider == "amvera":
            # Amvera LLM Inference API
            self.client = None
            self.model = AMVERA_MODEL
            self.api_base_url = AMVERA_API_BASE_URL
            self.access_token = AMVERA_ACCESS_TOKEN
        else:
            raise ValueError(f"Неизвестный провайдер: {self.provider}")
    
    def generate_scenario(
        self,
        niche: str = "общее",
        format_type: str = "60 секунд",
        style: str = "динамичный",
        topic: str = None,
        additional_info: str = None,
        user_patterns: Dict = None,
        is_premium: bool = False,
        tone: str = None,
        duration: str = None,
        platform: str = None,
        template_id: str = None,
        template_prompt_modifier: str = None
    ) -> str:
        """
        Генерирует сценарий для рилса
        
        Args:
            niche: Ниша контента (юмор, лайфхаки, мотивация, обзоры, общее)
            format_type: Формат видео (15 секунд, 30 секунд, 60 секунд, longform)
            style: Стиль сценария (динамичный, спокойный, драматичный, образовательный)
            topic: Конкретная тема (опционально)
            additional_info: Дополнительная информация (опционально)
        
        Returns:
            str: Сгенерированный сценарий
        """
        
        prompt = self._build_prompt(niche, format_type, style, topic, additional_info, user_patterns, is_premium, tone, duration, platform, template_id, template_prompt_modifier)
        
        if self.provider == "yandexgpt":
            return self._generate_yandexgpt(prompt, is_premium=is_premium)
        elif self.provider == "amvera":
            return self._generate_amvera_inference(prompt, format_type, is_premium)
        else:
            return self._generate_openai_compatible(prompt, is_premium=is_premium)
    
    def _generate_openai_compatible(self, prompt: str, is_premium: bool = False) -> str:
        """Генерация через OpenAI-совместимый API (OpenAI, Groq)"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты профессиональный сценарист для коротких видео (рилсы, TikTok, YouTube Shorts). КРИТИЧЕСКИ ВАЖНО: твоя целевая аудитория - молодежь Gen Z (16-25 лет), НЕ взрослые 30+. Создавай увлекательные, цепляющие сценарии с четкой структурой: мощный хук, развитие сюжета, клиффхэнгер или призыв к действию. Пиши короткими, энергичными, динамичными фразами современным языком (как молодые контент-мейкеры), используй обращение 'ты' вместо 'вы'. Избегай формального тона и длинных объяснений." + (
                            " Для Premium пользователей: создавай более детальные и развернутые сценарии с учетом актуальных трендов, сезонности и продвинутых техник сторителлинга. Интегрируй тренды естественно и органично." if is_premium else " Используй популярные форматы (POV, 'Расскажи о...', 'Хаки для...', 'Челлендж' и т.д.) для создания актуального контента."
                        ) + " КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.8,
                max_tokens=3000 if is_premium else 1500  # Больше токенов для Premium
            )
            
            scenario = response.choices[0].message.content.strip()
            scenario = self._remove_markdown(scenario)
            return scenario
            
        except RateLimitError as e:
            error_msg = str(e)
            provider_name = "Groq" if self.provider == "groq" else "OpenAI"
            
            if "quota" in error_msg.lower() or "insufficient_quota" in error_msg.lower():
                if self.provider == "groq":
                    return (
                        "⚠️ <b>Превышен лимит Groq API</b>\n\n"
                        "Бесплатный тариф Groq имеет лимиты на количество запросов.\n\n"
                        "🔧 <b>Что делать:</b>\n"
                        "1. Подождите несколько минут и попробуйте снова\n"
                        "2. Проверьте лимиты на https://console.groq.com/\n"
                        "3. Попробуйте переключиться на другой провайдер в .env файле"
                    )
                else:
                    return (
                        "⚠️ <b>Превышен лимит OpenAI API</b>\n\n"
                        "У вас закончился баланс или исчерпан лимит запросов на вашем аккаунте OpenAI.\n\n"
                        "🔧 <b>Что делать:</b>\n"
                        "1. Проверьте баланс на https://platform.openai.com/account/billing\n"
                        "2. Пополните баланс, если он пустой\n"
                        "3. Проверьте лимиты вашего тарифа\n"
                        "4. Подождите, если превышен rate limit (обычно сбрасывается через минуту)\n\n"
                        "После пополнения баланса попробуйте создать сценарий снова."
                    )
            else:
                return (
                    "⏳ <b>Слишком много запросов</b>\n\n"
                    f"Превышен лимит скорости запросов к {provider_name} API.\n"
                    "Подождите несколько секунд и попробуйте снова."
                )
                
        except APIError as e:
            error_code = getattr(e, 'code', None) or getattr(e, 'status_code', None)
            error_msg = str(e)
            provider_name = "Groq" if self.provider == "groq" else "OpenAI"
            
            if error_code == 401:
                return (
                    f"🔑 <b>Ошибка авторизации {provider_name} API</b>\n\n"
                    "Неверный API ключ. Проверьте правильность ключа в файле .env"
                )
            elif error_code == 429:
                return (
                    f"⚠️ <b>Превышен лимит {provider_name} API</b>\n\n"
                    "Подождите несколько минут и попробуйте снова."
                )
            else:
                logger.error(f"{provider_name} API Error: {error_msg}")
                return (
                    f"❌ <b>Ошибка {provider_name} API</b>\n\n"
                    f"Код ошибки: {error_code or 'неизвестен'}\n"
                    f"Сообщение: {error_msg[:200]}"
                )
                
        except (APIConnectionError, APITimeoutError) as e:
            provider_name = "Groq" if self.provider == "groq" else "OpenAI"
            return (
                "🌐 <b>Проблема с подключением</b>\n\n"
                f"Не удалось подключиться к {provider_name} API.\n"
                "Проверьте интернет-соединение и попробуйте позже."
            )
            
        except Exception as e:
            logger.error(f"Unexpected error in scenario generation: {str(e)}", exc_info=True)
            provider_name = "Groq" if self.provider == "groq" else "OpenAI"
            return (
                "❌ <b>Неожиданная ошибка</b>\n\n"
                f"Произошла ошибка при генерации сценария через {provider_name}.\n"
                f"Попробуйте позже или свяжитесь с поддержкой.\n\n"
                f"Детали: {str(e)[:200]}"
            )
    
    def _generate_yandexgpt(self, prompt: str, is_premium: bool = False) -> str:
        """Генерация через YandexGPT API"""
        try:
            import requests
            
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            system_prompt = "Ты профессиональный сценарист для коротких видео (рилсы, TikTok, YouTube Shorts). КРИТИЧЕСКИ ВАЖНО: твоя целевая аудитория - молодежь Gen Z (16-25 лет), НЕ взрослые 30+. Создавай увлекательные, цепляющие сценарии с четкой структурой: мощный хук, развитие сюжета, клиффхэнгер или призыв к действию. Пиши короткими, энергичными, динамичными фразами современным языком (как молодые контент-мейкеры), используй обращение 'ты' вместо 'вы'. Избегай формального тона и длинных объяснений." + (
                " Для Premium пользователей: создавай более детальные и развернутые сценарии с учетом актуальных трендов, сезонности и продвинутых техник сторителлинга. Интегрируй тренды естественно и органично." if is_premium else " Используй популярные форматы (POV, 'Расскажи о...', 'Хаки для...', 'Челлендж' и т.д.) для создания актуального контента."
            ) + " КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек."
            
            data = {
                "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{self.model}",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.8,
                    "maxTokens": 3000 if is_premium else 1500  # Больше токенов для Premium
                },
                "messages": [
                    {
                        "role": "system",
                        "text": system_prompt
                    },
                    {
                        "role": "user",
                        "text": prompt
                    }
                ]
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 403:
                error_detail = response.text
                logger.error(f"YandexGPT API 403 Error: {error_detail}")
                return (
                    "🔑 <b>Ошибка доступа YandexGPT API (403 Forbidden)</b>\n\n"
                    "Проблема с доступом к API. Возможные причины:\n\n"
                    "1. <b>Неверный API ключ</b> - проверьте YANDEX_API_KEY в .env\n"
                    "2. <b>Неверный Folder ID</b> - проверьте YANDEX_FOLDER_ID в .env\n"
                    "3. <b>Нет прав у сервисного аккаунта</b> - убедитесь, что у сервисного аккаунта есть роль 'ai.languageModels.user'\n"
                    "4. <b>Модель недоступна в каталоге</b> - проверьте доступность модели\n\n"
                    "<b>💡 Рекомендация:</b> YandexGPT сложнее в настройке. Попробуйте переключиться на Groq (проще и бесплатно):\n"
                    "Измените в .env: AI_PROVIDER=groq и добавьте GROQ_API_KEY\n"
                    "Получить ключ: https://console.groq.com/keys"
                )
            elif response.status_code == 401:
                return (
                    "🔑 <b>Ошибка авторизации YandexGPT API (401)</b>\n\n"
                    "Неверный API ключ. Проверьте правильность YANDEX_API_KEY в файле .env"
                )
            elif response.status_code == 400:
                error_detail = response.text
                logger.error(f"YandexGPT API 400 Error: {error_detail}")
                return (
                    "❌ <b>Ошибка запроса YandexGPT API (400)</b>\n\n"
                    "Некорректный запрос. Проверьте формат данных.\n\n"
                    f"Детали: {error_detail[:300]}"
                )
            
            response.raise_for_status()
            
            result = response.json()
            scenario = result["result"]["alternatives"][0]["message"]["text"].strip()
            scenario = self._remove_markdown(scenario)
            return scenario
            
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') and e.response else None
            error_detail = e.response.text if hasattr(e, 'response') and e.response else str(e)
            
            if status_code == 403:
                return (
                    "🔑 <b>Ошибка доступа YandexGPT API (403)</b>\n\n"
                    "Проблема с доступом к API. Проверьте:\n\n"
                    "1. Правильность API ключа (YANDEX_API_KEY)\n"
                    "2. Правильность Folder ID (YANDEX_FOLDER_ID)\n"
                    "3. Права сервисного аккаунта (должна быть роль 'ai.languageModels.user')\n"
                    "4. Доступность модели в вашем каталоге\n\n"
                    "Инструкция: https://yandex.cloud/ru/docs/foundation-models/operations/create-api-key"
                )
            elif status_code == 401:
                return (
                    "🔑 <b>Неверный API ключ YandexGPT (401)</b>\n\n"
                    "Проверьте правильность YANDEX_API_KEY в файле .env"
                )
            else:
                logger.error(f"YandexGPT API HTTP Error {status_code}: {error_detail}")
                return (
                    f"❌ <b>Ошибка YandexGPT API ({status_code})</b>\n\n"
                    f"Произошла ошибка при обращении к API.\n"
                    f"Попробуйте позже или проверьте настройки.\n\n"
                    f"Детали: {error_detail[:200]}"
                )
            
        except Exception as e:
            logger.error(f"YandexGPT API Error: {str(e)}", exc_info=True)
            return (
                "❌ <b>Ошибка YandexGPT API</b>\n\n"
                f"Произошла ошибка при генерации сценария.\n"
                f"Попробуйте позже или проверьте настройки API.\n\n"
                f"Детали: {str(e)[:200]}"
            )
    
    def _generate_amvera_inference(self, prompt: str, format_type: str = "60 секунд") -> str:
        """Генерация через API Amvera (DeepSeek-V3, GPT-5, LLaMA и т.д.)"""
        try:
            import requests
            
            # Определяем endpoint в зависимости от модели
            model_to_endpoint = {
                "deepseek-V3": "deepseek",
                "deepseek-R1": "deepseek",
                "gpt-5": "gpt",
                "gpt-4.1": "gpt",
                "llama8b": "llama",
                "llama70b": "llama",
                "qwen3_30b": "qwen",
                "qwen3_235b": "qwen"
            }
            
            endpoint = model_to_endpoint.get(self.model, "deepseek")  # По умолчанию deepseek
            url = f"{self.api_base_url}/models/{endpoint}"
            
            headers = {
                "Content-Type": "application/json",
                "X-Auth-Token": f"Bearer {self.access_token}"
            }
            
            # Определяем параметры в зависимости от формата
            is_longform = "longform" in format_type.lower() or "long" in format_type.lower()
            
            if is_longform:
                max_tokens = 4000  # Для longform нужен больший лимит токенов
                timeout_seconds = 180  # Увеличиваем таймаут до 3 минут для longform
                system_prompt = "Ты профессиональный сценарист для длинных видео (longform контент, полные видео). КРИТИЧЕСКИ ВАЖНО: твоя целевая аудитория - молодежь Gen Z (16-25 лет). Создавай подробные, структурированные сценарии с развитым сюжетом, несколькими актами, детальными описаниями сцен и диалогов. Пиши развернуто, но сохраняй динамику и современный стиль. КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами без звездочек."
            else:
                max_tokens = 3000 if is_premium else 1500  # Больше токенов для Premium
                timeout_seconds = 120  # 2 минуты для обычных форматов
                system_prompt = "Ты профессиональный сценарист для коротких видео (рилсы, TikTok, YouTube Shorts). КРИТИЧЕСКИ ВАЖНО: твоя целевая аудитория - молодежь Gen Z (16-25 лет), НЕ взрослые 30+. Создавай увлекательные, цепляющие сценарии с четкой структурой: мощный хук, развитие сюжета, клиффхэнгер или призыв к действию. Пиши короткими, энергичными, динамичными фразами современным языком (как молодые контент-мейкеры), используй обращение 'ты' вместо 'вы'. Избегай формального тона. Используй популярные форматы (POV, 'Расскажи о...', 'Хаки для...', 'Челлендж'). КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек."
            
            # Формат запроса согласно документации Amvera
            data = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "text": f"{system_prompt}\n\n{prompt}"
                    }
                ],
                "max_tokens": max_tokens  # Добавляем параметр max_tokens
            }
            
            # Увеличиваем таймаут для больших моделей и добавляем retry
            max_retries = 2
            response = None
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    # Используем stream=True для контроля чтения ответа
                    response = requests.post(
                        url, 
                        headers=headers, 
                        json=data, 
                        timeout=timeout_seconds,  # Используем динамический таймаут
                        stream=True,  # Включаем streaming для контроля чтения
                        verify=True  # Проверка SSL
                    )
                    
                    # Проверяем статус ответа ДО raise_for_status
                    if response.status_code == 502 or response.status_code == 503 or response.status_code == 504:
                        # Ошибки сервера - временные проблемы, обрабатываем сразу
                        # Пытаемся прочитать тело ответа для получения деталей ошибки
                        error_detail = ""
                        try:
                            # Читаем тело ответа
                            response_content = response.content
                            if response_content:
                                response_text = response_content.decode('utf-8', errors='ignore')
                                # Пытаемся распарсить JSON, если есть
                                try:
                                    error_json = json.loads(response_text)
                                    if "message" in error_json:
                                        error_detail = error_json["message"]
                                    elif "error" in error_json:
                                        error_detail = str(error_json["error"])
                                    else:
                                        error_detail = response_text[:200]
                                except:
                                    error_detail = response_text[:200] if len(response_text) < 200 else response_text[:200] + "..."
                        except:
                            pass
                        
                        error_msg = (
                            f"🌐 <b>Сервер Amvera временно недоступен ({response.status_code})</b>\n\n"
                        )
                        
                        if error_detail:
                            error_msg += f"<b>Детали ошибки:</b> {error_detail}\n\n"
                        
                        error_msg += (
                            "Сервер Amvera перегружен или находится на техническом обслуживании.\n\n"
                            "💡 <b>Что делать:</b>\n"
                            "1. Подождите 1-2 минуты и попробуйте создать сценарий еще раз\n"
                            "2. Если проблема сохраняется, попробуйте использовать другой формат (не longform)\n"
                            "3. Проверьте статус сервиса Amvera в их документации\n\n"
                            "Это временная проблема, обычно решается автоматически."
                        )
                        return error_msg
                    elif response.status_code >= 400:
                        # Если ошибка HTTP, обработаем её ниже
                        response.raise_for_status()
                    
                    # Читаем ответ по частям с обработкой ошибок
                    try:
                        # Читаем ответ по частям для больших ответов
                        chunks = []
                        for chunk in response.iter_content(chunk_size=8192, decode_unicode=False):
                            if chunk:
                                chunks.append(chunk)
                        
                        # Объединяем все части
                        response_content = b''.join(chunks)
                        
                        # Декодируем в текст
                        response_text = response_content.decode('utf-8', errors='ignore')
                        
                        # Создаем объект ответа с содержимым для дальнейшей обработки
                        response._content = response_content
                        response._text = response_text
                        
                    except (requests.exceptions.ChunkedEncodingError, Exception) as read_error:
                        # Если ошибка при чтении, пробуем еще раз
                        if attempt < max_retries - 1:
                            logger.warning(f"Ошибка чтения ответа на попытке {attempt + 1}/{max_retries}, повторяю: {read_error}")
                            time.sleep(3)  # Увеличиваем задержку до 3 секунд
                            continue
                        else:
                            raise
                    
                    break  # Успешный запрос и чтение
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась, повторяю запрос к Amvera API: {e}")
                        time.sleep(2)  # Ждем 2 секунды перед повтором
                        continue
                    else:
                        # Если все попытки исчерпаны, обработаем ошибку ниже
                        raise
                except requests.exceptions.ChunkedEncodingError as e:
                    # Ошибка при чтении ответа
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Ошибка чтения ответа на попытке {attempt + 1}/{max_retries}, повторяю запрос: {e}")
                        time.sleep(3)  # Увеличиваем задержку до 3 секунд
                        continue
                    else:
                        raise
            
            if response.status_code == 401:
                return (
                    "🔑 <b>Ошибка авторизации Amvera API (401)</b>\n\n"
                    "Неверный токен доступа. Проверьте AMVERA_ACCESS_TOKEN в переменных окружения.\n"
                    "Получите токен в ЛК Amvera в разделе LLM."
                )
            elif response.status_code == 403:
                return (
                    "🔑 <b>Ошибка доступа Amvera API (403 Forbidden)</b>\n\n"
                    "Проблема с доступом к API. Возможные причины:\n\n"
                    "1. <b>Неверный токен доступа</b> - проверьте AMVERA_ACCESS_TOKEN\n"
                    "2. <b>Недостаточно токенов</b> - пополните баланс токенов в панели Amvera\n"
                    "3. <b>Модель недоступна</b> - проверьте доступность модели в ЛК Amvera"
                )
            elif response.status_code == 400:
                # Получаем текст ошибки безопасно
                try:
                    error_detail = response.text if hasattr(response, '_text') else response.content.decode('utf-8', errors='ignore')
                except:
                    error_detail = str(response.content)[:500] if hasattr(response, 'content') else "Неизвестная ошибка"
                logger.error(f"Amvera API 400 Error: {error_detail}")
                return (
                    "❌ <b>Ошибка запроса Amvera API (400)</b>\n\n"
                    "Некорректный запрос. Проверьте формат данных.\n\n"
                    f"Детали: {error_detail[:300]}"
                )
            
            # Пытаемся прочитать ответ с обработкой ошибок
            try:
                # Парсим JSON ответ
                result = response.json()
            except (ValueError, json.JSONDecodeError, requests.exceptions.ChunkedEncodingError) as json_error:
                # Если ошибка при чтении/парсинге, пробуем прочитать текст напрямую
                try:
                    response_text = response.text
                    if response_text:
                        # Пытаемся найти JSON в тексте
                        import re
                        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                        if json_match:
                            result = json.loads(json_match.group())
                        else:
                            # Если JSON не найден, возвращаем ошибку
                            logger.error(f"Не удалось распарсить ответ от Amvera API. Текст: {response_text[:500]}")
                            raise json_error
                    else:
                        raise json_error
                except Exception as e:
                    logger.error(f"Ошибка обработки ответа от Amvera API: {e}")
                    raise
            
            # Amvera API возвращает ответ в формате {"response": "..."} или {"text": "..."}
            if "response" in result:
                scenario = result["response"].strip()
            elif "text" in result:
                scenario = result["text"].strip()
            elif "choices" in result and len(result["choices"]) > 0:
                # Если формат OpenAI-совместимый
                if "message" in result["choices"][0]:
                    scenario = result["choices"][0]["message"].get("content", "").strip()
                else:
                    scenario = result["choices"][0].get("text", "").strip()
            else:
                # Пытаемся найти текст в ответе
                logger.warning(f"Неожиданный формат ответа Amvera API: {result}")
                scenario = str(result).strip()
            
            if not scenario:
                return "❌ <b>Ошибка</b>\n\nПолучен пустой ответ от Amvera API. Попробуйте еще раз."
            
            scenario = self._remove_markdown(scenario)
            return scenario
            
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') and e.response else None
            try:
                error_detail = e.response.text if hasattr(e, 'response') and e.response else str(e)
            except:
                error_detail = str(e)
            
            if status_code == 403:
                return (
                    "🔑 <b>Ошибка доступа Amvera API (403)</b>\n\n"
                    "Проблема с доступом к API. Проверьте:\n\n"
                    "1. Правильность токена доступа (AMVERA_ACCESS_TOKEN)\n"
                    "2. Баланс токенов в панели Amvera\n"
                    "3. Доступность модели в ЛК Amvera"
                )
            elif status_code == 401:
                return (
                    "🔑 <b>Неверный токен доступа Amvera (401)</b>\n\n"
                    "Проверьте правильность AMVERA_ACCESS_TOKEN в переменных окружения.\n"
                    "Получите токен в ЛК Amvera в разделе LLM."
                )
            elif status_code in (502, 503, 504):
                # Ошибки сервера - временные проблемы
                # Пытаемся прочитать детали ошибки из тела ответа
                error_detail = ""
                try:
                    if hasattr(e, 'response') and e.response:
                        response_text = e.response.text if hasattr(e.response, 'text') else str(e.response.content.decode('utf-8', errors='ignore'))
                        try:
                            error_json = json.loads(response_text)
                            if "message" in error_json:
                                error_detail = error_json["message"]
                            elif "error" in error_json:
                                error_detail = str(error_json["error"])
                            else:
                                error_detail = response_text[:200]
                        except:
                            error_detail = response_text[:200] if len(response_text) < 200 else response_text[:200] + "..."
                except:
                    pass
                
                error_msg = (
                    f"🌐 <b>Сервер Amvera временно недоступен ({status_code})</b>\n\n"
                )
                
                if error_detail:
                    error_msg += f"<b>Детали ошибки:</b> {error_detail}\n\n"
                
                error_msg += (
                    "Сервер Amvera перегружен или находится на техническом обслуживании.\n\n"
                    "💡 <b>Что делать:</b>\n"
                    "1. Подождите 1-2 минуты и попробуйте создать сценарий еще раз\n"
                    "2. Если проблема сохраняется, попробуйте использовать другой формат (не longform)\n"
                    "3. Проверьте статус сервиса Amvera\n\n"
                    "Это временная проблема, обычно решается автоматически."
                )
                return error_msg
            else:
                logger.error(f"Amvera API HTTP Error {status_code}: {error_detail}")
                return (
                    f"❌ <b>Ошибка Amvera API ({status_code})</b>\n\n"
                    f"Произошла ошибка при обращении к API.\n"
                    f"Попробуйте позже или проверьте настройки.\n\n"
                    f"Детали: {error_detail[:200]}"
                )
            
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            logger.error(f"Amvera API Connection/Timeout Error: {str(e)}", exc_info=True)
            error_msg = str(e)
            if "Response ended prematurely" in error_msg or "ChunkedEncodingError" in str(type(e).__name__):
                return (
                    "🌐 <b>Проблема с подключением к Amvera API</b>\n\n"
                    "Соединение было прервано во время генерации. Возможные причины:\n\n"
                    "1. <b>Таймаут запроса</b> - генерация заняла слишком много времени (более 120 секунд)\n"
                    "2. <b>Перегрузка сервера</b> - сервер Amvera перегружен, попробуйте через несколько секунд\n"
                    "3. <b>Проблемы с сетью</b> - нестабильное соединение\n\n"
                    "💡 <b>Рекомендация:</b> Попробуйте создать сценарий еще раз. Если проблема повторяется, "
                    "возможно, модель перегружена - попробуйте позже или используйте более быструю модель (например, LLaMA 3.1 8B)."
                )
            else:
                return (
                    "🌐 <b>Проблема с подключением к Amvera API</b>\n\n"
                    "Не удалось подключиться к серверу. Возможные причины:\n\n"
                    "1. <b>Проблемы с сетью</b> - проверьте интернет-соединение\n"
                    "2. <b>Сервер недоступен</b> - попробуйте через несколько секунд\n"
                    "3. <b>Таймаут</b> - запрос занял слишком много времени\n\n"
                    "💡 <b>Рекомендация:</b> Попробуйте создать сценарий еще раз."
                )
        except Exception as e:
            logger.error(f"Amvera API Error: {str(e)}", exc_info=True)
            error_msg = str(e)
            if "Response ended prematurely" in error_msg:
                return (
                    "🌐 <b>Проблема с подключением к Amvera API</b>\n\n"
                    "Соединение было прервано во время генерации. Возможные причины:\n\n"
                    "1. <b>Таймаут запроса</b> - генерация заняла слишком много времени\n"
                    "2. <b>Перегрузка сервера</b> - сервер Amvera перегружен\n"
                    "3. <b>Проблемы с сетью</b> - нестабильное соединение\n\n"
                    "💡 <b>Рекомендация:</b> Попробуйте создать сценарий еще раз через несколько секунд."
                )
            return (
                "❌ <b>Ошибка Amvera API</b>\n\n"
                f"Произошла ошибка при генерации сценария.\n"
                f"Попробуйте позже или проверьте настройки API.\n\n"
                f"Детали: {error_msg[:200]}"
            )
    
    def _build_prompt(
        self,
        niche: str,
        format_type: str,
        style: str,
        topic: str,
        additional_info: str,
        user_patterns: Dict = None,
        is_premium: bool = False,
        tone: str = None,
        duration: str = None,
        platform: str = None,
        template_id: str = None,
        template_prompt_modifier: str = None
    ) -> str:
        """Строит промпт на основе параметров"""
        
        if is_premium:
            # Детальный промпт для Premium пользователей с учетом трендов и сезонности
            # Добавляем контекст времени года и трендов
            season_context = _get_season_context()
            trending_topics = _get_trending_topics()
            
            prompt_parts = [
                "🔥 ИСПОЛЬЗУЙ АКТУАЛЬНЫЕ ТРЕНДЫ И СЕЗОННОСТЬ:",
                trending_topics,
                "",
                season_context,
                "",
                "=" * 50,
                "",
                f"Создай ДЕТАЛЬНЫЙ и РАЗВЕРНУТЫЙ сценарий для видео с следующими параметрами:",
                f"- Ниша: {niche}",
                f"- Длительность: {format_type}",
                f"- Стиль: {style}",
            ]
            
            if tone:
                prompt_parts.append(f"- Тон подачи: {tone}")
            if duration:
                prompt_parts.append(f"- Длительность: {duration}")
            if platform:
                platform_names = {
                    "reels": "Instagram Reels",
                    "tiktok": "TikTok",
                    "shorts": "YouTube Shorts",
                    "универсальный": "универсальный формат"
                }
                platform_name = platform_names.get(platform.lower(), platform)
                prompt_parts.append(f"- Платформа: {platform_name}")
            
            if topic:
                prompt_parts.append(f"- Тема: {topic}")
            
            if additional_info:
                prompt_parts.append(f"- Дополнительная информация: {additional_info}")
            
            # Добавляем модификатор промпта для шаблона, если он выбран
            if template_id:
                # Проверяем, это пользовательский шаблон (user_XXX) или стандартный
                if template_id.startswith("user_") and template_prompt_modifier:
                    # Пользовательский шаблон - используем переданный модификатор
                    prompt_parts.append("")
                    prompt_parts.append("ФОРМАТ СЦЕНАРИЯ:")
                    prompt_parts.append(template_prompt_modifier)
                elif not template_id.startswith("user_"):
                    # Стандартный шаблон
                    from services.scenario_templates import get_template_prompt_modifier
                    template_modifier = get_template_prompt_modifier(template_id)
                    if template_modifier:
                        prompt_parts.append("")
                        prompt_parts.append("ФОРМАТ СЦЕНАРИЯ:")
                        prompt_parts.append(template_modifier)
            
            prompt_parts.extend([
                "",
                "Создай ПОЛНЫЙ и ДЕТАЛЬНЫЙ сценарий со следующей структурой:",
                "",
                "1. ХУК (первые 3-5 секунд):",
                "   - Цепляющее начало с визуальной подсказкой",
                "   - Что именно говорить (дословный текст)",
                "   - Что показывать в кадре (детальное описание)",
                "   - Реквизиты и локация (если нужны)",
                "",
                "2. РАЗВИТИЕ (основная часть):",
                "   - Пошаговое развитие сюжета",
                "   - Каждый кадр с таймингом (например: 0:05-0:15, 0:15-0:30 и т.д.)",
                "   - Дословный текст для каждого кадра",
                "   - Детальные визуальные подсказки",
                "   - Реквизиты, локации, освещение",
                "",
                "3. КЛИФФХЭНГЕР или CTA (финал):",
                "   - Призыв к действию или интрига",
                "   - Дословный текст",
                "   - Визуальная подсказка",
                "",
                "4. ДОПОЛНИТЕЛЬНО:",
                "   - Детальные советы по съемке (ракурсы, движения камеры)",
                "   - Советы по монтажу (переходы, эффекты)",
                "   - Рекомендуемые хэштеги (7-10 штук)",
                "   - Оптимальное время публикации",
                "   - Рекомендации по тексту для описания поста",
                "",
                "ВАЖНО: Сценарий должен быть МАКСИМАЛЬНО детальным и развернутым. Каждый элемент должен быть описан подробно:",
                "- Конкретные тайминги для каждого кадра (например: 0:00-0:03, 0:03-0:08 и т.д.)",
                "- Дословный текст для каждого кадра (что именно говорить)",
                "- Детальное описание визуального ряда (что показывать, крупность плана)",
                "- Реквизиты и их расположение в кадре",
                "- Описание локации, фона, освещения",
                "- Движения камеры (статичная, панорама, приближение, отдаление)",
                "- Действия в кадре (что делает человек, какие жесты)",
                "- Музыка и звуки (если применимо)",
                "",
                "🎯 ИНТЕГРАЦИЯ ТРЕНДОВ (КРИТИЧЕСКИ ВАЖНО):",
                "- ОБЯЗАТЕЛЬНО используй популярные форматы из списка выше (POV, 'Расскажи о...', 'Хаки для...' и т.д.)",
                "- Пиши для молодежной аудитории Gen Z (16-25 лет), НЕ для взрослых 30+",
                "- Используй современный, энергичный язык: короткие предложения, обращение 'ты', прямой разговор",
                "- Естественно вплети актуальные тренды в сценарий (НЕ принудительно, только если уместно)",
                "- Добавь элементы интерактивности (вопрос к зрителям, призыв к действию, вызов)",
                "- Учитывай сезонность: используй актуальные темы текущего времени года",
                "- Создавай контент, который звучит как у современных контент-мейкеров TikTok/Reels в 2024-2025",
                "- Избегай устаревших формулировок, формального тона и длинных 'взрослых' объяснений",
                "",
                "💡 ПРОДВИНУТЫЕ ТЕХНИКИ:",
                "- Используй психологию вовлечения: создавай эмоциональную связь с аудиторией",
                "- Применяй принципы storytelling: конфликт, развитие, разрешение",
                "- Добавь элементы неожиданности и интриги для удержания внимания",
                "- Используй конкретные примеры и цифры для большей убедительности",
                "- Создавай контент с образовательной ценностью (edutainment подход)",
                "",
                "Сценарий должен быть настолько подробным и актуальным, чтобы по нему можно было сразу начать съемку без дополнительных вопросов. Контент должен быть трендовым, но при этом органичным и естественным.",
                "",
                "КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек. Визуальные подсказки и текст пиши без звездочек и подчеркиваний."
            ])
        else:
            # Обычный промпт для бесплатных пользователей (тоже с современными трендами)
            trending_topics = _get_trending_topics()
            
            prompt_parts = [
                "🎯 ВАЖНО: Сценарий должен быть актуальным и трендовым для молодежной аудитории (16-25 лет, Gen Z).",
                "Используй популярные форматы и современный стиль подачи.",
                "",
                "📱 АКТУАЛЬНЫЕ ФОРМАТЫ И СТИЛЬ:",
                trending_topics,
                "",
                "=" * 50,
                "",
                f"Создай сценарий для рилса с следующими параметрами:",
                f"- Ниша: {niche}",
                f"- Длительность: {format_type}",
                f"- Стиль: {style}",
            ]
            
            if topic:
                prompt_parts.append(f"- Тема: {topic}")
            
            if additional_info:
                prompt_parts.append(f"- Дополнительная информация: {additional_info}")
            
            prompt_parts.extend([
                "",
                "СТРУКТУРА СЦЕНАРИЯ:",
                "1. ХУК (первые 3 секунды) - что-то цепляющее, используй популярный формат (POV, вопрос, неожиданное заявление)",
                "2. РАЗВИТИЕ - основной контент, короткими энергичными фразами",
                "3. КЛИФФХЭНГЕР или CTA - призыв к действию, вопрос или интрига",
                "",
                "СТИЛЬ ПОДАЧИ:",
                "- Короткие, энергичные предложения",
                "- Современный язык (для Gen Z), но не перебор со сленгом",
                "- Обращение 'ты' вместо 'вы'",
                "- Прямой, честный разговор без лишней воды",
                "- Цепляющие вопросы и интригующие заявления",
                "",
                "Добавь также:",
                "- Что показывать в кадре (визуальные подсказки)",
                "- Рекомендуемые хэштеги (5-7 штук, актуальные)",
                "- Оптимальное время публикации",
                "",
                "ВАЖНО: Сценарий должен звучать современно и актуально, как будто его создает молодой контент-мейкер для TikTok/Reels в 2024-2025 году. Избегай устаревших формулировок и формального тона.",
                "",
                "КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек. Визуальные подсказки и текст пиши без звездочек и подчеркиваний."
            ])
        
        # Добавляем паттерны пользователя, если они есть
        if user_patterns:
            patterns_prompt = self.build_patterns_prompt(user_patterns)
            if patterns_prompt:
                prompt_parts.append(patterns_prompt)
        
        return "\n".join(prompt_parts)
    
    def improve_scenario(self, current_scenario: str, improvement_request: str) -> str:
        """
        Улучшает/развивает существующий сценарий на основе запроса пользователя
        
        Args:
            current_scenario: Текущий сценарий, который нужно улучшить
            improvement_request: Запрос на улучшение (например, "сделай более динамичным", "добавь больше юмора")
        
        Returns:
            str: Улучшенный сценарий
        """
        prompt = (
            f"Вот текущий сценарий для рилса:\n\n"
            f"{current_scenario}\n\n"
            f"Пользователь просит: {improvement_request}\n\n"
            f"Улучши/доработай сценарий согласно запросу пользователя. "
            f"Сохрани структуру и все важные элементы, но внеси необходимые изменения. "
            f"ВАЖНО: Верни ТОЛЬКО улучшенный сценарий, без заголовков, без дополнительных комментариев, без фраз типа 'Улучшенный сценарий готов'. "
            f"Начни сразу с содержимого сценария (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР и т.д.). "
            f"КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами без звездочек."
        )
        
        if self.provider == "yandexgpt":
            return self._generate_yandexgpt(prompt)
        elif self.provider == "amvera":
            # Для улучшения используем стандартный формат (не longform)
            return self._generate_amvera_inference(prompt, format_type="60 секунд")
        else:
            return self._generate_openai_compatible_improvement(prompt)
    
    def _generate_openai_compatible_improvement(self, prompt: str) -> str:
        """Генерация улучшенного сценария через OpenAI-совместимый API"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты профессиональный сценарист для коротких видео. Ты помогаешь улучшать и дорабатывать сценарии согласно запросам пользователей, сохраняя при этом структуру и ключевые элементы. ВАЖНО: Возвращай ТОЛЬКО текст сценария, без заголовков типа 'Улучшенный сценарий готов', без комментариев, без повторений. Начинай сразу с содержимого сценария. КРИТИЧЕСКИ ВАЖНО: НИКОГДА не используй markdown форматирование. Запрещено использовать: **, __, *, _ для форматирования. Пиши ТОЛЬКО обычным текстом. Заголовки пиши заглавными буквами (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР) без звездочек."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.8,
                max_tokens=1500
            )
            
            improved_scenario = response.choices[0].message.content.strip()
            improved_scenario = self._remove_markdown(improved_scenario)
            
            lines = improved_scenario.split('\n')
            cleaned_lines = []
            
            for line in lines:
                line_lower = line.lower().strip()
                line_stripped = line.strip()
                
                if any(phrase in line_lower for phrase in [
                    'улучшенный сценарий готов', 
                    'твой запрос:', 
                    'готов!',
                    '✨ улучшенный',
                    '✨улучшенный'
                ]):
                    continue
                
                if line_stripped and (all(c == '=' for c in line_stripped) or all(c == '-' for c in line_stripped)):
                    continue
                
                if line_stripped.startswith('=') and ('улучшенный' in line_lower or 'твой запрос' in line_lower):
                    continue
                
                cleaned_lines.append(line)
            
            cleaned_scenario = '\n'.join(cleaned_lines).strip()
            
            while '\n\n\n' in cleaned_scenario:
                cleaned_scenario = cleaned_scenario.replace('\n\n\n', '\n\n')
            
            if not cleaned_scenario or len(cleaned_scenario) < 50:
                original_lines = improved_scenario.split('\n')
                final_lines = []
                for line in original_lines:
                    line_lower = line.lower().strip()
                    line_stripped = line.strip()
                    if any(phrase in line_lower for phrase in ['улучшенный сценарий готов', 'твой запрос:']):
                        continue
                    if line_stripped and all(c == '=' for c in line_stripped):
                        continue
                    final_lines.append(line)
                result = '\n'.join(final_lines).strip()
                return result if result else improved_scenario
            
            return cleaned_scenario
            
        except Exception as e:
            return self._generate_openai_compatible(prompt)
    
    def remove_markdown(self, text: str) -> str:
        """Удаляет все markdown форматирование из текста - агрессивная очистка (публичный метод)"""
        return self._remove_markdown(text)
    
    def _remove_markdown(self, text: str) -> str:
        """Удаляет все markdown форматирование из текста - агрессивная очистка"""
        if not text:
            return text
        
        max_iterations = 20
        for _ in range(max_iterations):
            if '**' not in text:
                break
            text = _RE_BOLD_DOUBLE.sub(r'\1', text)
            text = _RE_BOLD_DOUBLE_SPACES.sub(r'\1', text)
        
        for _ in range(max_iterations):
            if '__' not in text:
                break
            text = _RE_UNDERSCORE_DOUBLE.sub(r'\1', text)
            text = _RE_UNDERSCORE_DOUBLE_SPACES.sub(r'\1', text)
        
        for _ in range(max_iterations):
            if '*' not in text:
                break
            text = _RE_ITALIC_STAR.sub(r'\1', text)
            text = _RE_ITALIC_STAR_SPACES.sub(r'\1', text)
        
        for _ in range(max_iterations):
            if '_' not in text:
                break
            text = _RE_ITALIC_UNDERSCORE.sub(r'\1', text)
            text = _RE_ITALIC_UNDERSCORE_SPACES.sub(r'\1', text)
        
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = _RE_LIST_MARKER.sub('', line)
            cleaned_lines.append(line)
        
        text = '\n'.join(cleaned_lines)
        
        for _ in range(5):
            text = text.replace("**", "")
            text = text.replace("*", "")
            text = text.replace("__", "")
        
        text = _RE_UNDERSCORE_STANDALONE.sub('', text)
        text = _RE_MULTIPLE_SPACES.sub(' ', text)
        
        lines = text.split('\n')
        cleaned_lines = [line.strip() for line in lines]
        text = '\n'.join(cleaned_lines)
        
        while '\n\n\n' in text:
            text = text.replace('\n\n\n', '\n\n')
        
        return text
    
    @staticmethod
    def analyze_editing_patterns(original: str, improved: str, request: str = None) -> Dict:
        """
        Анализирует паттерны редактирования, сравнивая оригинальный и улучшенный сценарий
        
        Returns:
            Dict с паттернами: изменения длины, стиля, структуры и т.д.
        """
        patterns = {
            "length_change": 0,  # процент изменения длины
            "style_changes": [],  # список изменений стиля
            "structure_changes": [],  # изменения структуры
            "common_requests": [],  # частые запросы
            "preferred_elements": []  # предпочитаемые элементы
        }
        
        if not original or not improved:
            return patterns
        
        # Анализ изменения длины
        orig_len = len(original)
        impr_len = len(improved)
        if orig_len > 0:
            patterns["length_change"] = round(((impr_len - orig_len) / orig_len) * 100, 1)
        
        # Анализ структуры (количество секций)
        orig_sections = len([line for line in original.split('\n') if line.strip().isupper() and len(line.strip()) > 2])
        impr_sections = len([line for line in improved.split('\n') if line.strip().isupper() and len(line.strip()) > 2])
        if orig_sections != impr_sections:
            patterns["structure_changes"].append(f"sections_{'added' if impr_sections > orig_sections else 'removed'}")
        
        # Анализ стиля (короткие/длинные предложения)
        orig_sentences = [s.strip() for s in original.split('.') if s.strip()]
        impr_sentences = [s.strip() for s in improved.split('.') if s.strip()]
        if orig_sentences and impr_sentences:
            orig_avg_sentence = sum(len(s.split()) for s in orig_sentences) / len(orig_sentences)
            impr_avg_sentence = sum(len(s.split()) for s in impr_sentences) / len(impr_sentences)
            if impr_avg_sentence < orig_avg_sentence * 0.8:
                patterns["style_changes"].append("shorter_sentences")
            elif impr_avg_sentence > orig_avg_sentence * 1.2:
                patterns["style_changes"].append("longer_sentences")
        
        # Анализ запроса пользователя
        if request:
            request_lower = request.lower()
            if any(word in request_lower for word in ['короче', 'сократи', 'меньше']):
                patterns["common_requests"].append("shorter")
            elif any(word in request_lower for word in ['длиннее', 'подробнее', 'больше', 'детальнее']):
                patterns["common_requests"].append("longer")
            if any(word in request_lower for word in ['динамичн', 'энергичн', 'быстр']):
                patterns["common_requests"].append("dynamic")
            elif any(word in request_lower for word in ['спокойн', 'медленн', 'плавн']):
                patterns["common_requests"].append("calm")
            if any(word in request_lower for word in ['юмор', 'смешн', 'весел']):
                patterns["common_requests"].append("humor")
            if any(word in request_lower for word in ['проще', 'понятн', 'легче']):
                patterns["common_requests"].append("simpler")
        
        return patterns
    
    @staticmethod
    def merge_patterns(existing_patterns: Dict, new_patterns: Dict) -> Dict:
        """
        Объединяет существующие паттерны с новыми, усредняя значения
        """
        if not existing_patterns:
            return new_patterns
        
        merged = existing_patterns.copy()
        
        # Усредняем изменение длины
        if "length_change" in new_patterns:
            if "length_change" in merged:
                # Взвешенное среднее (больше веса новым данным)
                merged["length_change"] = round((merged["length_change"] * 0.7 + new_patterns["length_change"] * 0.3), 1)
            else:
                merged["length_change"] = new_patterns["length_change"]
        
        # Объединяем списки изменений стиля
        if "style_changes" in new_patterns:
            if "style_changes" not in merged:
                merged["style_changes"] = []
            for change in new_patterns["style_changes"]:
                if change not in merged["style_changes"]:
                    merged["style_changes"].append(change)
        
        # Объединяем изменения структуры
        if "structure_changes" in new_patterns:
            if "structure_changes" not in merged:
                merged["structure_changes"] = []
            for change in new_patterns["structure_changes"]:
                if change not in merged["structure_changes"]:
                    merged["structure_changes"].append(change)
        
        # Объединяем частые запросы (подсчитываем частоту)
        if "common_requests" in new_patterns:
            if "common_requests" not in merged:
                merged["common_requests"] = {}
            elif isinstance(merged["common_requests"], list):
                # Конвертируем старый формат в новый
                merged["common_requests"] = {req: 1 for req in merged["common_requests"]}
            
            for req in new_patterns["common_requests"]:
                merged["common_requests"][req] = merged["common_requests"].get(req, 0) + 1
        
        return merged
    
    def build_patterns_prompt(self, patterns: Dict) -> str:
        """
        Строит промпт на основе паттернов редактирования пользователя
        """
        if not patterns:
            return ""
        
        prompt_parts = ["\n\nВАЖНО: Учитывай предпочтения пользователя в стиле написания:"]
        
        # Длина текста
        if "length_change" in patterns and patterns["length_change"]:
            if patterns["length_change"] < -10:
                prompt_parts.append("- Пользователь предпочитает более короткие сценарии (на ~" + str(abs(int(patterns["length_change"]))) + "% короче)")
            elif patterns["length_change"] > 10:
                prompt_parts.append("- Пользователь предпочитает более подробные сценарии (на ~" + str(int(patterns["length_change"])) + "% длиннее)")
        
        # Стиль предложений
        if "style_changes" in patterns:
            if "shorter_sentences" in patterns["style_changes"]:
                prompt_parts.append("- Пользователь предпочитает короткие, лаконичные предложения")
            elif "longer_sentences" in patterns["style_changes"]:
                prompt_parts.append("- Пользователь предпочитает более развернутые предложения")
        
        # Частые запросы
        if "common_requests" in patterns:
            if isinstance(patterns["common_requests"], dict):
                sorted_requests = sorted(patterns["common_requests"].items(), key=lambda x: x[1], reverse=True)
                top_requests = [req for req, count in sorted_requests[:3] if count >= 2]
            else:
                top_requests = list(set(patterns["common_requests"]))
            
            if top_requests:
                if "shorter" in top_requests:
                    prompt_parts.append("- Пользователь часто просит делать сценарии короче")
                if "longer" in top_requests:
                    prompt_parts.append("- Пользователь часто просит добавлять больше деталей")
                if "dynamic" in top_requests:
                    prompt_parts.append("- Пользователь предпочитает динамичный, энергичный стиль")
                if "calm" in top_requests:
                    prompt_parts.append("- Пользователь предпочитает спокойный, плавный стиль")
                if "humor" in top_requests:
                    prompt_parts.append("- Пользователь часто просит добавлять юмор")
                if "simpler" in top_requests:
                    prompt_parts.append("- Пользователь предпочитает простой, понятный язык")
        
        return "\n".join(prompt_parts) if len(prompt_parts) > 1 else ""
