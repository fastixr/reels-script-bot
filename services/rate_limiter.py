"""
Модуль для rate limiting (ограничения частоты запросов)
"""
import time
import logging
from typing import Dict, Optional
from collections import defaultdict
from config import (
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_MESSAGES_PER_MINUTE,
    RATE_LIMIT_CALLBACKS_PER_MINUTE,
    DEVELOPER_USER_IDS
)

logger = logging.getLogger(__name__)

class RateLimiter:
    """Класс для ограничения частоты запросов"""
    
    def __init__(self):
        self._message_timestamps: Dict[int, list] = defaultdict(list)
        self._callback_timestamps: Dict[int, list] = defaultdict(list)
        self._blocked_users: Dict[int, float] = {}  # user_id -> unblock_time
    
    def _cleanup_old_timestamps(self, timestamps: list, window_seconds: int = 60):
        """Удалить старые временные метки"""
        now = time.time()
        cutoff = now - window_seconds
        return [ts for ts in timestamps if ts > cutoff]
    
    def check_message_rate(self, user_id: int) -> tuple[bool, Optional[str]]:
        """
        Проверить лимит сообщений для пользователя
        
        Returns:
            (allowed, error_message)
        """
        if not RATE_LIMIT_ENABLED:
            return True, None
        
        # Разработчики не ограничены
        if user_id in DEVELOPER_USER_IDS:
            return True, None
        
        # Проверяем, не заблокирован ли пользователь
        if user_id in self._blocked_users:
            unblock_time = self._blocked_users[user_id]
            if time.time() < unblock_time:
                remaining = int(unblock_time - time.time())
                return False, f"⏳ Вы отправляете сообщения слишком часто. Подождите {remaining} секунд."
            else:
                del self._blocked_users[user_id]
        
        now = time.time()
        timestamps = self._message_timestamps[user_id]
        
        # Очищаем старые метки (старше минуты)
        timestamps = self._cleanup_old_timestamps(timestamps, 60)
        self._message_timestamps[user_id] = timestamps
        
        # Проверяем лимит
        if len(timestamps) >= RATE_LIMIT_MESSAGES_PER_MINUTE:
            # Блокируем на 1 минуту
            self._blocked_users[user_id] = now + 60
            return False, "⏳ Вы отправляете сообщения слишком часто. Подождите 1 минуту."
        
        # Добавляем текущую метку
        timestamps.append(now)
        return True, None
    
    def check_callback_rate(self, user_id: int) -> tuple[bool, Optional[str]]:
        """
        Проверить лимит callback'ов для пользователя
        
        Returns:
            (allowed, error_message)
        """
        if not RATE_LIMIT_ENABLED:
            return True, None
        
        # Разработчики не ограничены
        if user_id in DEVELOPER_USER_IDS:
            return True, None
        
        now = time.time()
        timestamps = self._callback_timestamps[user_id]
        
        # Очищаем старые метки (старше минуты)
        timestamps = self._cleanup_old_timestamps(timestamps, 60)
        self._callback_timestamps[user_id] = timestamps
        
        # Проверяем лимит
        if len(timestamps) >= RATE_LIMIT_CALLBACKS_PER_MINUTE:
            return False, "⏳ Вы нажимаете кнопки слишком часто. Подождите немного."
        
        # Добавляем текущую метку
        timestamps.append(now)
        return True, None
    
    def cleanup(self):
        """Очистить старые данные (вызывается периодически)"""
        now = time.time()
        
        # Удаляем разблокированных пользователей
        self._blocked_users = {
            uid: unblock_time for uid, unblock_time in self._blocked_users.items()
            if unblock_time > now
        }
        
        # Очищаем старые временные метки
        for user_id in list(self._message_timestamps.keys()):
            self._message_timestamps[user_id] = self._cleanup_old_timestamps(
                self._message_timestamps[user_id], 120
            )
            if not self._message_timestamps[user_id]:
                del self._message_timestamps[user_id]
        
        for user_id in list(self._callback_timestamps.keys()):
            self._callback_timestamps[user_id] = self._cleanup_old_timestamps(
                self._callback_timestamps[user_id], 120
            )
            if not self._callback_timestamps[user_id]:
                del self._callback_timestamps[user_id]

# Глобальный экземпляр rate limiter
rate_limiter = RateLimiter()

