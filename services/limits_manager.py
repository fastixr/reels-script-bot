"""
Менеджер лимитов для пользователей бота
"""
from config import DEVELOPER_USER_IDS, MAX_REQUESTS_PER_USER
from services.subscription_manager import SubscriptionManager
from database import Database
from typing import Dict, Tuple
import time

_limit_cache = {}
_limit_cache_ttl = 180

def _get_cached_limit(user_id: int) -> Tuple[bool, str]:
    if user_id in _limit_cache:
        result, timestamp = _limit_cache[user_id]
        if time.time() - timestamp < _limit_cache_ttl:
            return result
        del _limit_cache[user_id]
    return None

def _set_cached_limit(user_id: int, result: Tuple[bool, str]):
    _limit_cache[user_id] = (result, time.time())
    if len(_limit_cache) > 2000:
        _limit_cache.clear()

class LimitsManager:
    """Класс для управления лимитами пользователей"""
    
    @staticmethod
    def is_developer(user_id: int) -> bool:
        """Проверяет, является ли пользователь разработчиком (в whitelist)"""
        return user_id in DEVELOPER_USER_IDS
    
    @staticmethod
    async def has_premium(user_id: int) -> bool:
        """Проверяет, есть ли у пользователя активная премиум подписка"""
        return await SubscriptionManager.has_active_subscription(user_id)
    
    @staticmethod
    async def can_make_request(user_id: int) -> Tuple[bool, str]:
        """
        Проверяет, может ли пользователь сделать запрос (с кэшированием)
        
        Returns:
            tuple[bool, str]: (можно_ли_делать_запрос, сообщение_об_ошибке)
        """
        cached = _get_cached_limit(user_id)
        if cached is not None:
            return cached
        
        if LimitsManager.is_developer(user_id):
            result = (True, "")
            _set_cached_limit(user_id, result)
            return result
        
        if await LimitsManager.has_premium(user_id):
            result = (True, "")
            _set_cached_limit(user_id, result)
            return result
        
        current_requests = await Database.get_user_requests_count(user_id)
        extra_requests = await Database.get_extra_requests_count(user_id)
        
        if extra_requests > 0:
            result = (True, "")
            _set_cached_limit(user_id, result)
            return result
        
        if current_requests >= MAX_REQUESTS_PER_USER:
            result = (False, (
                f"⚠️ <b>Достигнут лимит запросов</b>\n\n"
                f"Вы использовали {MAX_REQUESTS_PER_USER} запросов.\n\n"
                f"💎 <b>Варианты:</b>\n"
                f"• Оформите премиум подписку (безлимит) - /subscribe\n"
                f"• Купите дополнительные попытки - /subscribe\n\n"
                f"Используйте команду /subscribe для выбора варианта"
            ))
            _set_cached_limit(user_id, result)
            return result
        
        result = (True, "")
        _set_cached_limit(user_id, result)
        return result
    
    @staticmethod
    async def increment_request(user_id: int, active_users_set: set = None):
        if active_users_set is not None:
            active_users_set.add(user_id)
        
        if user_id in _limit_cache:
            del _limit_cache[user_id]
        
        if not LimitsManager.is_developer(user_id) and not await LimitsManager.has_premium(user_id):
            try:
                if await Database.use_extra_request(user_id):
                    return
                await Database.increment_user_requests(user_id)
            except Exception:
                pass
    
    @staticmethod
    async def get_remaining_requests(user_id: int) -> int:
        """Возвращает количество оставшихся запросов"""
        if LimitsManager.is_developer(user_id) or await LimitsManager.has_premium(user_id):
            return -1
        
        current = await Database.get_user_requests_count(user_id)
        free_remaining = max(0, MAX_REQUESTS_PER_USER - current)
        extra_requests = await Database.get_extra_requests_count(user_id)
        
        return free_remaining + extra_requests
    
    @staticmethod
    async def get_user_requests_count(user_id: int) -> int:
        """Возвращает количество использованных запросов пользователя"""
        return await Database.get_user_requests_count(user_id)
    
    @staticmethod
    async def reset_user_requests(user_id: int):
        """Сбрасывает счетчик запросов пользователя (для тестирования)"""
        await Database.reset_user_requests(user_id)

