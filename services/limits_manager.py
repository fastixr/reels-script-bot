"""
Менеджер лимитов для пользователей бота
"""
from config import DEVELOPER_USER_IDS, MAX_REQUESTS_PER_USER
from services.subscription_manager import SubscriptionManager
from database import Database
from typing import Dict, Tuple


class LimitsManager:
    """Класс для управления лимитами пользователей"""
    
    @staticmethod
    def is_developer(user_id: int) -> bool:
        """Проверяет, является ли пользователь разработчиком (в whitelist)"""
        return user_id in DEVELOPER_USER_IDS
    
    @staticmethod
    def has_premium(user_id: int) -> bool:
        """Проверяет, есть ли у пользователя активная премиум подписка"""
        return SubscriptionManager.has_active_subscription(user_id)
    
    @staticmethod
    def can_make_request(user_id: int) -> Tuple[bool, str]:
        """
        Проверяет, может ли пользователь сделать запрос
        
        Returns:
            tuple[bool, str]: (можно_ли_делать_запрос, сообщение_об_ошибке)
        """
        # Разработчики имеют безлимит
        if LimitsManager.is_developer(user_id):
            return True, ""
        
        # Премиум подписчики имеют безлимит
        if LimitsManager.has_premium(user_id):
            return True, ""
        
        # Проверяем лимит для обычных пользователей
        current_requests = Database.get_user_requests_count(user_id)
        if current_requests >= MAX_REQUESTS_PER_USER:
            return False, (
                f"⚠️ <b>Достигнут лимит запросов</b>\n\n"
                f"Вы использовали {MAX_REQUESTS_PER_USER} запросов.\n"
                f"Лимит сбросится при перезапуске бота.\n\n"
                f"💎 <b>Хотите безлимит?</b> Оформите премиум подписку!\n"
                f"Используйте команду /subscribe"
            )
        
        return True, ""
    
    @staticmethod
    def increment_request(user_id: int, active_users_set: set = None):
        """Увеличивает счетчик запросов пользователя"""
        # Добавляем пользователя в активные (для статистики и рассылки)
        if active_users_set is not None:
            active_users_set.add(user_id)
        
        # Отмечаем пользователя как активного в БД
        Database.mark_user_active(user_id)
        
        if not LimitsManager.is_developer(user_id) and not LimitsManager.has_premium(user_id):
            Database.increment_user_requests(user_id)
    
    @staticmethod
    def get_remaining_requests(user_id: int) -> int:
        """Возвращает количество оставшихся запросов"""
        if LimitsManager.is_developer(user_id) or LimitsManager.has_premium(user_id):
            return -1  # -1 означает безлимит
        
        current = Database.get_user_requests_count(user_id)
        return max(0, MAX_REQUESTS_PER_USER - current)
    
    @staticmethod
    def get_user_requests_count(user_id: int) -> int:
        """Возвращает количество использованных запросов пользователя"""
        return Database.get_user_requests_count(user_id)
    
    @staticmethod
    def reset_user_requests(user_id: int):
        """Сбрасывает счетчик запросов пользователя (для тестирования)"""
        Database.reset_user_requests(user_id)

