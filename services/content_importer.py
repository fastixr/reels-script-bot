"""
Сервис для импорта видео и текста и создания сценариев на их основе
"""
import re
import logging
import requests
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs
import json
import os
import tempfile
import asyncio

logger = logging.getLogger(__name__)

# Проверка доступности библиотек для работы с видео
YT_DLP_AVAILABLE = False
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    logger.warning("yt-dlp не установлена. Для распознавания речи установите: pip install yt-dlp")

# Проверка доступности Yandex SpeechKit
SPEECHKIT_AVAILABLE = False
try:
    import aiohttp
    SPEECHKIT_AVAILABLE = True
except ImportError:
    logger.warning("aiohttp не установлена. Для распознавания речи установите: pip install aiohttp")


class ContentImporter:
    """Класс для импорта контента из видео и текста"""
    
    def __init__(self, scenario_generator):
        """
        Args:
            scenario_generator: Экземпляр ScenarioGenerator для анализа и генерации
        """
        self.scenario_generator = scenario_generator
        
        # Настройки для Yandex SpeechKit (используем те же ключи, что и для YandexGPT)
        from config import YANDEX_API_KEY, YANDEX_FOLDER_ID
        self.speechkit_api_key = YANDEX_API_KEY
        self.speechkit_folder_id = YANDEX_FOLDER_ID
        self.speechkit_enabled = bool(self.speechkit_api_key and self.speechkit_folder_id)
    
    def is_url(self, text: str) -> bool:
        """Проверяет, является ли текст URL"""
        url_pattern = re.compile(
            r'^https?://'  # http:// or https://
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
            r'localhost|'  # localhost...
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
            r'(?::\d+)?'  # optional port
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
        return bool(url_pattern.match(text.strip()))
    
    def detect_platform(self, url: str) -> Optional[str]:
        """Определяет платформу по URL"""
        url_lower = url.lower()
        
        if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
            return 'youtube'
        elif 'instagram.com' in url_lower or 'instagr.am' in url_lower:
            return 'instagram'
        elif 'tiktok.com' in url_lower:
            return 'tiktok'
        elif 'vk.com' in url_lower or 'vk.ru' in url_lower:
            return 'vk'
        else:
            return None
    
    async def extract_video_info(self, url: str) -> Dict:
        """
        Извлекает информацию о видео из URL
        
        Returns:
            Dict с информацией: {
                'title': str,
                'description': str,
                'text': str,  # Текст из описания/комментариев/транскрипта
                'platform': str,
                'url': str
            }
        """
        platform = self.detect_platform(url)
        
        if not platform:
            return {
                'error': 'Неподдерживаемая платформа. Поддерживаются: YouTube, Instagram, TikTok',
                'platform': None,
                'url': url
            }
        
        try:
            if platform == 'youtube':
                return await self._extract_youtube_info(url)
            elif platform == 'instagram':
                return await self._extract_instagram_info(url)
            elif platform == 'tiktok':
                return await self._extract_tiktok_info(url)
            else:
                return {
                    'error': f'Платформа {platform} пока не поддерживается',
                    'platform': platform,
                    'url': url
                }
        except Exception as e:
            logger.error(f"Ошибка при извлечении информации из {url}: {e}", exc_info=True)
            return {
                'error': f'Ошибка при обработке ссылки: {str(e)}',
                'platform': platform,
                'url': url
            }
    
    async def _extract_youtube_info(self, url: str) -> Dict:
        """Извлекает информацию из YouTube видео, включая транскрипцию"""
        try:
            # Извлекаем video ID из разных форматов ссылок
            video_id = None
            url_lower = url.lower()
            
            if 'youtube.com/watch' in url_lower:
                parsed = urlparse(url)
                params = parse_qs(parsed.query)
                video_id = params.get('v', [None])[0]
            elif 'youtube.com/shorts/' in url_lower:
                # Обработка YouTube Shorts: https://youtube.com/shorts/VIDEO_ID
                parsed = urlparse(url)
                path_parts = parsed.path.strip('/').split('/')
                if 'shorts' in path_parts:
                    shorts_index = path_parts.index('shorts')
                    if shorts_index + 1 < len(path_parts):
                        video_id = path_parts[shorts_index + 1]
                        # Убираем query параметры если есть в ID
                        if '?' in video_id:
                            video_id = video_id.split('?')[0]
            elif 'youtu.be' in url_lower:
                parsed = urlparse(url)
                video_id = parsed.path.strip('/')
                # Убираем query параметры если есть
                if '?' in video_id:
                    video_id = video_id.split('?')[0]
            
            if not video_id:
                return {
                    'error': 'Не удалось извлечь ID видео из ссылки',
                    'platform': 'youtube',
                    'url': url
                }
            
            # Получаем базовую информацию через oEmbed
            title = ''
            author = ''
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            try:
                response = requests.get(oembed_url, timeout=10)
                if response.status_code == 200:
                    oembed_data = response.json()
                    title = oembed_data.get('title', '')
                    author = oembed_data.get('author_name', '')
            except Exception as e:
                logger.warning(f"Не удалось получить информацию через oEmbed: {e}")
            
            if not title:
                title = f'YouTube видео {video_id}'
            
            # Пытаемся получить транскрипцию через распознавание речи
            transcript_text = ''
            transcript_source = None
            
            # Пробуем распознать речь через Yandex SpeechKit (универсальное решение для всех платформ)
            if self.speechkit_enabled and YT_DLP_AVAILABLE:
                try:
                    logger.info(f"Пытаюсь распознать речь для видео {video_id} через Yandex SpeechKit...")
                    transcript_text = await self._recognize_speech_from_video(url, video_id)
                    if transcript_text:
                        transcript_source = 'speechkit_recognition'
                        logger.info(f"Речь успешно распознана для видео {video_id} ({len(transcript_text)} символов)")
                except Exception as e:
                    logger.warning(f"Не удалось распознать речь для видео {video_id}: {e}")
            elif not self.speechkit_enabled:
                logger.info(f"Yandex SpeechKit не настроен. Для распознавания речи добавьте YANDEX_API_KEY и YANDEX_FOLDER_ID в .env (используются те же ключи, что и для YandexGPT)")
            elif not YT_DLP_AVAILABLE:
                logger.info(f"yt-dlp не установлен. Установите: pip install yt-dlp")
            
            # Формируем полный текст для анализа
            text_content = f"{title}\n\n"
            if author:
                text_content += f"Автор: {author}\n\n"
            
            if transcript_text:
                text_content += f"ТРАНСКРИПЦИЯ ВИДЕО:\n{transcript_text}\n\n"
            else:
                # Если транскрипции нет, добавляем сообщение
                text_content += "Примечание: Транскрипция видео не найдена. Анализ будет выполнен на основе названия и метаданных.\n"
                text_content += "Для более точного анализа рекомендуется использовать видео с включенными субтитрами.\n\n"
            
            return {
                'title': title,
                'description': '',
                'text': text_content,
                'platform': 'youtube',
                'url': url,
                'video_id': video_id,
                'transcript': transcript_text,
                'transcript_source': transcript_source
            }
            
        except Exception as e:
            logger.error(f"Ошибка при извлечении информации YouTube: {e}", exc_info=True)
            return {
                'error': f'Ошибка при обработке YouTube ссылки: {str(e)}',
                'platform': 'youtube',
                'url': url
            }
    
    async def _recognize_speech_from_video(self, video_url: str, video_id: str = None) -> Optional[str]:
        """
        Распознает речь из видео через yt-dlp + Yandex SpeechKit
        
        Args:
            video_url: URL видео
            video_id: ID видео (опционально)
        
        Returns:
            str: Распознанный текст или None
        """
        if not YT_DLP_AVAILABLE or not self.speechkit_enabled:
            return None
        
        try:
            # Скачиваем аудио из видео
            audio_file = None
            try:
                audio_file = await self._download_audio_from_video(video_url)
                if not audio_file:
                    return None
                
                # Распознаем речь через Yandex SpeechKit
                transcript = await self._recognize_speech_with_speechkit(audio_file)
                return transcript
                
            finally:
                # Удаляем временный файл
                if audio_file and os.path.exists(audio_file):
                    try:
                        os.remove(audio_file)
                    except:
                        pass
                        
        except Exception as e:
            logger.error(f"Ошибка при распознавании речи: {e}", exc_info=True)
            return None
    
    def _find_ffmpeg_path(self) -> Optional[str]:
        """
        Находит путь к FFmpeg: сначала проверяет локальные бинарники, потом системные
        """
        # Проверяем локальные бинарники в папке ffmpeg/bin (для серверного развертывания)
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local_ffmpeg_paths = [
            os.path.join(script_dir, 'ffmpeg', 'bin', 'ffmpeg'),
            os.path.join(script_dir, 'ffmpeg', 'bin', 'ffmpeg.exe'),
            os.path.join('ffmpeg', 'bin', 'ffmpeg'),
            os.path.join('ffmpeg', 'bin', 'ffmpeg.exe'),
        ]
        
        for ffmpeg_path in local_ffmpeg_paths:
            if os.path.exists(ffmpeg_path) and os.access(ffmpeg_path, os.X_OK):
                logger.info(f"Найден локальный FFmpeg: {ffmpeg_path}")
                return ffmpeg_path
        
        # Проверяем системный FFmpeg
        import shutil
        system_ffmpeg = shutil.which('ffmpeg')
        if system_ffmpeg:
            logger.info(f"Найден системный FFmpeg: {system_ffmpeg}")
            return system_ffmpeg
        
        logger.warning("FFmpeg не найден ни локально, ни в системе")
        return None
    
    async def _download_audio_from_video(self, video_url: str) -> Optional[str]:
        """
        Скачивает аудио из видео через yt-dlp
        
        Returns:
            str: Путь к временному аудио файлу или None
        """
        if not YT_DLP_AVAILABLE:
            return None
        
        try:
            # Находим FFmpeg
            ffmpeg_path = self._find_ffmpeg_path()
            if not ffmpeg_path:
                logger.warning("FFmpeg не найден. Распознавание речи недоступно.")
                return None
            
            # Создаем временный файл для аудио
            temp_dir = tempfile.gettempdir()
            audio_file = os.path.join(temp_dir, f"audio_{os.urandom(8).hex()}.wav")
            
            # Настройки yt-dlp для извлечения только аудио
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': audio_file.replace('.wav', '.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'wav',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True,
                'ffmpeg_location': os.path.dirname(ffmpeg_path) if ffmpeg_path else None,  # Указываем путь к FFmpeg
            }
            
            # Скачиваем в отдельном потоке, чтобы не блокировать
            def download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])
                    # yt-dlp создает файл с расширением из формата, нужно найти его
                    base_name = audio_file.replace('.wav', '')
                    for ext in ['.wav', '.mp3', '.m4a', '.opus']:
                        if os.path.exists(base_name + ext):
                            return base_name + ext
                    return None
            
            # Выполняем в executor, чтобы не блокировать event loop
            loop = asyncio.get_event_loop()
            result_file = await loop.run_in_executor(None, download)
            
            if result_file and os.path.exists(result_file):
                return result_file
            else:
                logger.warning(f"Не удалось скачать аудио из {video_url}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при скачивании аудио: {e}", exc_info=True)
            return None
    
    async def _recognize_speech_with_speechkit(self, audio_file_path: str) -> Optional[str]:
        """
        Распознает речь из аудио файла через Yandex SpeechKit
        
        Args:
            audio_file_path: Путь к аудио файлу
        
        Returns:
            str: Распознанный текст или None
        """
        if not self.speechkit_enabled or not SPEECHKIT_AVAILABLE:
            return None
        
        try:
            # Читаем аудио файл
            with open(audio_file_path, 'rb') as f:
                audio_data = f.read()
            
            # Проверяем размер файла (SpeechKit имеет лимиты)
            max_size = 10 * 1024 * 1024  # 10 MB
            if len(audio_data) > max_size:
                logger.warning(f"Аудио файл слишком большой ({len(audio_data)} байт), максимальный размер {max_size}")
                return None
            
            # URL для синхронного распознавания
            url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
            
            headers = {
                'Authorization': f'Api-Key {self.speechkit_api_key}',
                'Content-Type': 'audio/wav'
            }
            
            params = {
                'folderId': self.speechkit_folder_id,
                'lang': 'ru-RU',  # Русский язык
                'format': 'lpcm',
                'sampleRateHertz': '16000'
            }
            
            # Отправляем запрос
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, params=params, data=audio_data) as response:
                    if response.status == 200:
                        result = await response.json()
                        if 'result' in result:
                            return result['result']
                        else:
                            logger.warning(f"Неожиданный формат ответа SpeechKit: {result}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка SpeechKit API (status {response.status}): {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Ошибка при распознавании речи через SpeechKit: {e}", exc_info=True)
            return None
    
    async def _extract_instagram_info(self, url: str) -> Dict:
        """Извлекает информацию из Instagram поста/рилса"""
        try:
            # Instagram требует авторизацию для API, поэтому используем базовый парсинг
            # В реальном проекте можно использовать Instagram Basic Display API или Graph API
            
            import time
            
            # Пытаемся получить базовую информацию через веб-скрапинг с retry
            max_retries = 3
            wait_time = 1
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            for attempt in range(max_retries):
                try:
                    response = requests.get(
                        url, 
                        headers=headers, 
                        timeout=15, 
                        allow_redirects=True,
                        verify=True
                    )
                    
                    if response.status_code == 200:
                        html = response.text
                        
                        # Пытаемся извлечь описание из мета-тегов
                        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                        description_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                        
                        # Также пробуем альтернативные форматы
                        if not title_match:
                            title_match = re.search(r'"og:title" content="([^"]+)"', html)
                        if not description_match:
                            description_match = re.search(r'"og:description" content="([^"]+)"', html)
                        
                        title = title_match.group(1) if title_match else 'Instagram пост'
                        description = description_match.group(1) if description_match else ''
                        
                        # Декодируем HTML entities
                        try:
                            import html
                            title = html.unescape(title)
                            description = html.unescape(description)
                        except:
                            pass
                        
                        text_content = f"{title}\n\n"
                        if description:
                            text_content += f"{description}\n\n"
                        else:
                            text_content += "Описание не найдено. "
                        
                        # Пробуем распознать речь из видео через SpeechKit
                        transcript_text = ''
                        if self.speechkit_enabled and YT_DLP_AVAILABLE:
                            try:
                                logger.info(f"Пытаюсь распознать речь для Instagram видео через Yandex SpeechKit...")
                                transcript_text = await self._recognize_speech_from_video(url)
                                if transcript_text:
                                    text_content += f"ТРАНСКРИПЦИЯ ВИДЕО:\n{transcript_text}\n\n"
                                    logger.info(f"Речь успешно распознана для Instagram видео ({len(transcript_text)} символов)")
                            except Exception as e:
                                logger.warning(f"Не удалось распознать речь для Instagram видео: {e}")
                        
                        if not description and not transcript_text:
                            text_content += "Пожалуйста, пришлите текст поста вручную для более точного анализа.\n\n"
                        
                        return {
                            'title': title,
                            'description': description,
                            'text': text_content,
                            'platform': 'instagram',
                            'url': url,
                            'transcript': transcript_text
                        }
                    elif response.status_code == 429:
                        # Rate limiting - ждем дольше
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 3
                            logger.warning(f"Rate limit для Instagram. Ждем {wait_time} сек...")
                            time.sleep(wait_time)
                            continue
                    
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, 
                        requests.exceptions.RequestException) as e:
                    error_str = str(e)
                    if attempt < max_retries - 1:
                        logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась для Instagram {url}: {e}. Повтор через {wait_time} сек...")
                        time.sleep(wait_time)
                        wait_time *= 2
                    else:
                        logger.warning(f"Не удалось получить информацию через парсинг после {max_retries} попыток: {e}")
                except Exception as e:
                    logger.warning(f"Неожиданная ошибка при парсинге Instagram: {e}")
                    break
            
            # Если парсинг не сработал, возвращаем базовую информацию
            return {
                'title': 'Instagram контент',
                'description': '',
                'text': f'Instagram контент: {url}\n\nНе удалось автоматически извлечь текст. Пожалуйста, пришлите текст поста вручную для более точного анализа.',
                'platform': 'instagram',
                'url': url
            }
            
        except Exception as e:
            logger.error(f"Ошибка при извлечении информации Instagram: {e}", exc_info=True)
            return {
                'error': f'Ошибка при обработке Instagram ссылки: {str(e)}',
                'platform': 'instagram',
                'url': url
            }
    
    async def _extract_tiktok_info(self, url: str) -> Dict:
        """Извлекает информацию из TikTok видео"""
        try:
            import time
            
            # TikTok также требует авторизацию для API
            # Пытаемся получить базовую информацию через веб-скрапинг с retry
            max_retries = 3
            wait_time = 1
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Referer': 'https://www.tiktok.com/'
            }
            
            for attempt in range(max_retries):
                try:
                    response = requests.get(
                        url, 
                        headers=headers, 
                        timeout=15, 
                        allow_redirects=True,
                        verify=True
                    )
                    
                    if response.status_code == 200:
                        html = response.text
                        
                        # Пытаемся извлечь описание из мета-тегов или JSON-LD
                        title_match = re.search(r'<title>([^<]+)</title>', html)
                        description_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                        
                        # Также пробуем альтернативные форматы
                        if not description_match:
                            description_match = re.search(r'"og:description" content="([^"]+)"', html)
                        if not description_match:
                            # Пробуем найти в JSON-LD
                            json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
                            if json_ld_match:
                                try:
                                    json_data = json.loads(json_ld_match.group(1))
                                    if isinstance(json_data, dict):
                                        description = json_data.get('description', '')
                                        if description:
                                            description_match = type('obj', (object,), {'group': lambda self, x: description})()
                                except:
                                    pass
                        
                        title = title_match.group(1).strip() if title_match else 'TikTok видео'
                        description = description_match.group(1) if description_match else ''
                        
                        # Декодируем HTML entities
                        try:
                            import html
                            title = html.unescape(title)
                            description = html.unescape(description)
                        except:
                            pass
                        
                        text_content = f"{title}\n\n"
                        if description:
                            text_content += f"{description}\n\n"
                        else:
                            text_content += "Описание не найдено. "
                        
                        # Пробуем распознать речь из видео через SpeechKit
                        transcript_text = ''
                        if self.speechkit_enabled and YT_DLP_AVAILABLE:
                            try:
                                logger.info(f"Пытаюсь распознать речь для TikTok видео через Yandex SpeechKit...")
                                transcript_text = await self._recognize_speech_from_video(url)
                                if transcript_text:
                                    text_content += f"ТРАНСКРИПЦИЯ ВИДЕО:\n{transcript_text}\n\n"
                                    logger.info(f"Речь успешно распознана для TikTok видео ({len(transcript_text)} символов)")
                            except Exception as e:
                                logger.warning(f"Не удалось распознать речь для TikTok видео: {e}")
                        
                        if not description and not transcript_text:
                            text_content += "Пожалуйста, пришлите текст описания вручную для более точного анализа.\n\n"
                        
                        return {
                            'title': title,
                            'description': description,
                            'text': text_content,
                            'platform': 'tiktok',
                            'url': url,
                            'transcript': transcript_text
                        }
                    elif response.status_code == 429:
                        # Rate limiting
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 3
                            logger.warning(f"Rate limit для TikTok. Ждем {wait_time} сек...")
                            time.sleep(wait_time)
                            continue
                    
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, 
                        requests.exceptions.RequestException) as e:
                    error_str = str(e)
                    if attempt < max_retries - 1:
                        logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась для TikTok {url}: {e}. Повтор через {wait_time} сек...")
                        time.sleep(wait_time)
                        wait_time *= 2
                    else:
                        logger.warning(f"Не удалось получить информацию через парсинг после {max_retries} попыток: {e}")
                except Exception as e:
                    logger.warning(f"Неожиданная ошибка при парсинге TikTok: {e}")
                    break
            
            # Если парсинг не сработал, возвращаем базовую информацию
            return {
                'title': 'TikTok видео',
                'description': '',
                'text': f'TikTok видео: {url}\n\nНе удалось автоматически извлечь текст. Пожалуйста, пришлите текст описания вручную для более точного анализа.',
                'platform': 'tiktok',
                'url': url
            }
            
        except Exception as e:
            logger.error(f"Ошибка при извлечении информации TikTok: {e}", exc_info=True)
            return {
                'error': f'Ошибка при обработке TikTok ссылки: {str(e)}',
                'platform': 'tiktok',
                'url': url
            }
    
    async def analyze_content_structure(self, content_text: str, platform: str = None) -> Dict:
        """
        Анализирует структуру и стиль контента с помощью AI
        
        Returns:
            Dict с анализом: {
                'structure': str,  # Описание структуры
                'style': str,  # Описание стиля
                'key_elements': List[str],  # Ключевые элементы
                'tone': str,  # Тон подачи
                'hook': str,  # Хук/начало
                'format': str  # Формат контента
            }
        """
        try:
            # Строим промпт для анализа
            analysis_prompt = (
                f"Проанализируй следующий контент и определи его структуру, стиль и ключевые элементы:\n\n"
                f"{content_text}\n\n"
                f"Определи:\n"
                f"1. СТРУКТУРА - как построен контент (хук, развитие, финал)\n"
                f"2. СТИЛЬ - особенности подачи (формальный/неформальный, длинные/короткие предложения, юмор и т.д.)\n"
                f"3. КЛЮЧЕВЫЕ ЭЛЕМЕНТЫ - что делает контент цепляющим (вопросы, неожиданные повороты, конкретные примеры и т.д.)\n"
                f"4. ТОН - эмоциональная окраска (мотивирующий, развлекательный, образовательный и т.д.)\n"
                f"5. ХУК - как начинается контент (первые 1-2 предложения)\n"
                f"6. ФОРМАТ - тип контента (POV, лайфхак, рассказ, челлендж и т.д.)\n\n"
                f"Ответь в структурированном виде, чтобы можно было использовать эти элементы для создания нового сценария в похожем стиле."
            )
            
            # Используем AI для анализа
            if self.scenario_generator.provider == "yandexgpt":
                analysis_result = self.scenario_generator._generate_yandexgpt(analysis_prompt, is_premium=False)
            elif self.scenario_generator.provider == "amvera":
                analysis_result = self.scenario_generator._generate_amvera_inference(analysis_prompt, "60 секунд", is_premium=False)
            else:
                analysis_result = self.scenario_generator._generate_openai_compatible(analysis_prompt, is_premium=False)
            
            # Парсим результат анализа (простой парсинг)
            analysis_dict = {
                'full_analysis': analysis_result,
                'structure': '',
                'style': '',
                'key_elements': [],
                'tone': '',
                'hook': '',
                'format': ''
            }
            
            # Пытаемся извлечь структурированные данные из ответа
            lines = analysis_result.split('\n')
            current_section = None
            
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                
                if 'СТРУКТУРА' in line_stripped.upper() or '1.' in line_stripped[:5]:
                    current_section = 'structure'
                    analysis_dict['structure'] = line_stripped.split(':', 1)[-1].strip() if ':' in line_stripped else ''
                elif 'СТИЛЬ' in line_stripped.upper() or '2.' in line_stripped[:5]:
                    current_section = 'style'
                    analysis_dict['style'] = line_stripped.split(':', 1)[-1].strip() if ':' in line_stripped else ''
                elif 'КЛЮЧЕВЫЕ' in line_stripped.upper() or '3.' in line_stripped[:5]:
                    current_section = 'key_elements'
                elif 'ТОН' in line_stripped.upper() or '4.' in line_stripped[:5]:
                    current_section = 'tone'
                    analysis_dict['tone'] = line_stripped.split(':', 1)[-1].strip() if ':' in line_stripped else ''
                elif 'ХУК' in line_stripped.upper() or '5.' in line_stripped[:5]:
                    current_section = 'hook'
                    analysis_dict['hook'] = line_stripped.split(':', 1)[-1].strip() if ':' in line_stripped else ''
                elif 'ФОРМАТ' in line_stripped.upper() or '6.' in line_stripped[:5]:
                    current_section = 'format'
                    analysis_dict['format'] = line_stripped.split(':', 1)[-1].strip() if ':' in line_stripped else ''
                else:
                    # Добавляем к текущей секции
                    if current_section == 'structure' and line_stripped and not line_stripped[0].isdigit():
                        analysis_dict['structure'] += ' ' + line_stripped if analysis_dict['structure'] else line_stripped
                    elif current_section == 'style' and line_stripped and not line_stripped[0].isdigit():
                        analysis_dict['style'] += ' ' + line_stripped if analysis_dict['style'] else line_stripped
                    elif current_section == 'key_elements' and line_stripped:
                        if line_stripped.startswith('-') or line_stripped.startswith('•'):
                            analysis_dict['key_elements'].append(line_stripped.lstrip('- •').strip())
                    elif current_section == 'tone' and line_stripped and not line_stripped[0].isdigit():
                        analysis_dict['tone'] += ' ' + line_stripped if analysis_dict['tone'] else line_stripped
                    elif current_section == 'hook' and line_stripped and not line_stripped[0].isdigit():
                        analysis_dict['hook'] += ' ' + line_stripped if analysis_dict['hook'] else line_stripped
                    elif current_section == 'format' and line_stripped and not line_stripped[0].isdigit():
                        analysis_dict['format'] += ' ' + line_stripped if analysis_dict['format'] else line_stripped
            
            return analysis_dict
            
        except Exception as e:
            logger.error(f"Ошибка при анализе контента: {e}", exc_info=True)
            return {
                'error': f'Ошибка при анализе контента: {str(e)}',
                'full_analysis': '',
                'structure': '',
                'style': '',
                'key_elements': [],
                'tone': '',
                'hook': '',
                'format': ''
            }
    
    async def create_scenario_from_content(
        self,
        content_text: str,
        analysis: Dict,
        niche: str = "общее",
        format_type: str = "60 секунд",
        style: str = "динамичный",
        platform: str = None,
        user_id: int = None,
        is_premium: bool = False
    ) -> str:
        """
        Создает новый сценарий на основе проанализированного контента
        
        Args:
            content_text: Исходный текст контента
            analysis: Результат анализа структуры и стиля
            niche: Ниша для нового сценария
            format_type: Формат видео
            style: Стиль сценария
            platform: Платформа
            user_id: ID пользователя
            is_premium: Является ли пользователь Premium
        
        Returns:
            str: Сгенерированный сценарий
        """
        try:
            # Строим промпт для генерации сценария на основе анализа
            scenario_prompt = (
                f"Создай новый сценарий для видео на основе следующего анализа успешного контента:\n\n"
                f"АНАЛИЗ ИСХОДНОГО КОНТЕНТА:\n"
                f"Структура: {analysis.get('structure', 'не определена')}\n"
                f"Стиль: {analysis.get('style', 'не определен')}\n"
                f"Ключевые элементы: {', '.join(analysis.get('key_elements', [])) if analysis.get('key_elements') else 'не определены'}\n"
                f"Тон: {analysis.get('tone', 'не определен')}\n"
                f"Хук: {analysis.get('hook', 'не определен')}\n"
                f"Формат: {analysis.get('format', 'не определен')}\n\n"
                f"ИСХОДНЫЙ ТЕКСТ (для справки):\n{content_text[:500]}...\n\n"
                f"ЗАДАЧА:\n"
                f"Создай НОВЫЙ сценарий в похожем стиле и структуре, но с ДРУГОЙ темой.\n"
                f"- Ниша: {niche}\n"
                f"- Формат: {format_type}\n"
                f"- Стиль: {style}\n"
                f"- Используй похожую структуру и стиль подачи\n"
                f"- Сохрани то же эмоциональное воздействие\n"
                f"- Адаптируй формат под новую тему\n\n"
                f"ВАЖНО: Это должен быть НОВЫЙ оригинальный сценарий, не копия. Используй только структуру и стиль как вдохновение."
            )
            
            # Генерируем сценарий - используем прямой промпт через дополнительную информацию
            # Поскольку метод generate_scenario строит промпт автоматически, используем additional_info
            # для передачи анализа и указания использовать его как основу
            additional_info_for_prompt = (
                f"ВАЖНО: Создай сценарий на основе следующего анализа успешного контента:\n\n"
                f"АНАЛИЗ ИСХОДНОГО КОНТЕНТА:\n"
                f"Структура: {analysis.get('structure', 'не определена')}\n"
                f"Стиль: {analysis.get('style', 'не определен')}\n"
                f"Ключевые элементы: {', '.join(analysis.get('key_elements', [])) if analysis.get('key_elements') else 'не определены'}\n"
                f"Тон: {analysis.get('tone', 'не определен')}\n"
                f"Хук: {analysis.get('hook', 'не определен')}\n"
                f"Формат: {analysis.get('format', 'не определен')}\n\n"
                f"ИСХОДНЫЙ ТЕКСТ (для справки):\n{content_text[:500]}...\n\n"
                f"ЗАДАЧА: Создай НОВЫЙ сценарий в похожем стиле и структуре, но с ДРУГОЙ темой. "
                f"Используй похожую структуру и стиль подачи. Сохрани то же эмоциональное воздействие. "
                f"Адаптируй формат под новую тему. Это должен быть НОВЫЙ оригинальный сценарий, не копия."
            )
            
            scenario = self.scenario_generator.generate_scenario(
                niche=niche,
                format_type=format_type,
                style=style,
                topic=None,
                additional_info=additional_info_for_prompt,
                is_premium=is_premium
            )
            
            return scenario
            
        except Exception as e:
            logger.error(f"Ошибка при создании сценария на основе контента: {e}", exc_info=True)
            return f"Ошибка при создании сценария: {str(e)}"

