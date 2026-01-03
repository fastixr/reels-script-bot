from openai import OpenAI, APIError, RateLimitError, APIConnectionError, APITimeoutError
from config import (
    AI_PROVIDER, 
    GROQ_API_KEY, GROQ_MODEL,
    OPENAI_API_KEY, OPENAI_MODEL,
    YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL
)
import logging
import json

logger = logging.getLogger(__name__)

class ScenarioGenerator:
    """Класс для генерации сценариев для рилсов с помощью различных AI провайдеров"""
    
    def __init__(self):
        self.provider = AI_PROVIDER
        
        if self.provider == "groq":
            # Groq использует OpenAI-совместимый API
            self.client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1"
            )
            self.model = GROQ_MODEL
        elif self.provider == "openai":
            self.client = OpenAI(api_key=OPENAI_API_KEY)
            self.model = OPENAI_MODEL
        elif self.provider == "yandexgpt":
            # YandexGPT использует другой API, обработаем отдельно
            self.client = None
            self.model = YANDEX_MODEL
        else:
            raise ValueError(f"Неизвестный провайдер: {self.provider}")
    
    def generate_scenario(
        self,
        niche: str = "общее",
        format_type: str = "60 секунд",
        style: str = "динамичный",
        topic: str = None,
        additional_info: str = None
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
        
        # Формируем промпт
        prompt = self._build_prompt(niche, format_type, style, topic, additional_info)
        
        # Генерируем в зависимости от провайдера
        if self.provider == "yandexgpt":
            return self._generate_yandexgpt(prompt)
        else:
            return self._generate_openai_compatible(prompt)
    
    def _generate_openai_compatible(self, prompt: str) -> str:
        """Генерация через OpenAI-совместимый API (OpenAI, Groq)"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты профессиональный сценарист для коротких видео (рилсы, TikTok, YouTube Shorts). Создавай увлекательные, цепляющие сценарии с четкой структурой: мощный хук, развитие сюжета, клиффхэнгер или призыв к действию. Пиши короткими, динамичными фразами, которые легко читать вслух."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.8,
                max_tokens=1500
            )
            
            scenario = response.choices[0].message.content.strip()
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
    
    def _generate_yandexgpt(self, prompt: str) -> str:
        """Генерация через YandexGPT API"""
        try:
            import requests
            
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            system_prompt = "Ты профессиональный сценарист для коротких видео (рилсы, TikTok, YouTube Shorts). Создавай увлекательные, цепляющие сценарии с четкой структурой: мощный хук, развитие сюжета, клиффхэнгер или призыв к действию. Пиши короткими, динамичными фразами, которые легко читать вслух."
            
            data = {
                "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{self.model}",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.8,
                    "maxTokens": 1500
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
            
            # Синхронный запрос
            response = requests.post(url, headers=headers, json=data, timeout=30)
            
            # Обработка ошибок до raise_for_status
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
    
    def _build_prompt(
        self,
        niche: str,
        format_type: str,
        style: str,
        topic: str,
        additional_info: str
    ) -> str:
        """Строит промпт на основе параметров"""
        
        prompt_parts = [
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
            "Структура сценария:",
            "1. ХУК (первые 3 секунды) - что-то цепляющее",
            "2. РАЗВИТИЕ - основной контент",
            "3. КЛИФФХЭНГЕР или CTA - призыв к действию или интрига",
            "",
            "Добавь также:",
            "- Что показывать в кадре (визуальные подсказки)",
            "- Рекомендуемые хэштеги (5-7 штук)",
            "- Оптимальное время публикации"
        ])
        
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
            f"Начни сразу с содержимого сценария (ХУК, РАЗВИТИЕ, КЛИФФХЭНГЕР и т.д.)."
        )
        
        # Генерируем в зависимости от провайдера
        if self.provider == "yandexgpt":
            return self._generate_yandexgpt(prompt)
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
                        "content": "Ты профессиональный сценарист для коротких видео. Ты помогаешь улучшать и дорабатывать сценарии согласно запросам пользователей, сохраняя при этом структуру и ключевые элементы. ВАЖНО: Возвращай ТОЛЬКО текст сценария, без заголовков типа 'Улучшенный сценарий готов', без комментариев, без повторений. Начинай сразу с содержимого сценария."
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
            
            # Очищаем от возможных заголовков и повторений
            # Удаляем ВСЕ строки, которые содержат заголовки (не только в начале)
            lines = improved_scenario.split('\n')
            cleaned_lines = []
            
            for line in lines:
                line_lower = line.lower().strip()
                line_stripped = line.strip()
                
                # Пропускаем ВСЕ строки с заголовками (в любом месте текста)
                if any(phrase in line_lower for phrase in [
                    'улучшенный сценарий готов', 
                    'твой запрос:', 
                    'готов!',
                    '✨ улучшенный',
                    '✨улучшенный'
                ]):
                    continue
                
                # Пропускаем строки, которые состоят только из символов "=" или "-"
                if line_stripped and (all(c == '=' for c in line_stripped) or all(c == '-' for c in line_stripped)):
                    continue
                
                # Пропускаем строки, которые начинаются с "=" и содержат заголовок
                if line_stripped.startswith('=') and ('улучшенный' in line_lower or 'твой запрос' in line_lower):
                    continue
                
                # Добавляем только содержательные строки
                cleaned_lines.append(line)
            
            cleaned_scenario = '\n'.join(cleaned_lines).strip()
            
            # Удаляем множественные пустые строки
            while '\n\n\n' in cleaned_scenario:
                cleaned_scenario = cleaned_scenario.replace('\n\n\n', '\n\n')
            
            # Если после очистки ничего не осталось или слишком мало, возвращаем оригинал (но тоже очищенный)
            if not cleaned_scenario or len(cleaned_scenario) < 50:
                # Более агрессивная очистка оригинала
                original_lines = improved_scenario.split('\n')
                final_lines = []
                for line in original_lines:
                    line_lower = line.lower().strip()
                    line_stripped = line.strip()
                    # Пропускаем заголовки
                    if any(phrase in line_lower for phrase in ['улучшенный сценарий готов', 'твой запрос:']):
                        continue
                    # Пропускаем строки из "="
                    if line_stripped and all(c == '=' for c in line_stripped):
                        continue
                    final_lines.append(line)
                result = '\n'.join(final_lines).strip()
                return result if result else improved_scenario
            
            return cleaned_scenario
            
        except Exception as e:
            # Используем общую обработку ошибок
            return self._generate_openai_compatible(prompt)
