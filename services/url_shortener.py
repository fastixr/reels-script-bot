"""
Сервис для сокращения ссылок через clck.su API
Документация: https://clck.su/developers
"""
import aiohttp
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class URLShortener:
    """Сервис для сокращения ссылок через clck.su"""
    
    API_URL = "https://clck.su/api/url/add"
    
    def __init__(self, api_key: str):
        """
        Инициализация сервиса сокращения ссылок
        
        Args:
            api_key: API ключ от clck.su
        """
        self.api_key = api_key
        self.enabled = bool(api_key)
    
    async def shorten_url(self, long_url: str) -> Optional[str]:
        """
        Сократить длинную ссылку через clck.su API
        
        Args:
            long_url: Длинная ссылка для сокращения
        
        Returns:
            str: Короткая ссылка или None в случае ошибки
        """
        if not self.enabled:
            logger.debug("Сокращение ссылок отключено, возвращаем исходную ссылку")
            return long_url
        
        try:
            async with aiohttp.ClientSession() as session:
                # Согласно документации clck.su: POST к /api/url/add с JSON телом
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                payload = {"url": long_url}
                
                logger.debug(f"Запрос к clck.su API: {self.API_URL}, url={long_url[:50]}...")
                
                async with session.post(
                    self.API_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        try:
                            response_data = await response.json()
                            # Ответ содержит "shorturl", а не "short"
                            if response_data.get("error") == 0 and "shorturl" in response_data:
                                short_url = response_data["shorturl"]
                                logger.info(f"Ссылка сокращена: {long_url[:50]}... -> {short_url}")
                                return short_url
                            else:
                                logger.warning(f"Ошибка API clck.su: {response_data.get('message', 'Unknown error')}, response: {response_data}")
                                return long_url
                        except Exception as json_error:
                            response_text = await response.text()
                            logger.warning(f"Ошибка парсинга JSON от clck.su: {json_error}, response text: {response_text[:500]}")
                            return long_url
                    else:
                        # Читаем текст ответа только один раз
                        try:
                            error_data = await response.json()
                            logger.warning(f"Ошибка HTTP при сокращении ссылки: {response.status}, response: {error_data}")
                        except:
                            response_text = await response.text()
                            logger.warning(
                                f"Ошибка HTTP при сокращении ссылки: {response.status}, "
                                f"response: {response_text[:500] if response_text else 'empty'}"
                            )
                        return long_url
        
        except asyncio.TimeoutError:
            logger.warning("Таймаут при сокращении ссылки через clck.su")
            return long_url
        except Exception as e:
            logger.error(f"Ошибка при сокращении ссылки через clck.su: {e}")
            return long_url


# Глобальный экземпляр (будет инициализирован в bot.py)
url_shortener: Optional[URLShortener] = None

