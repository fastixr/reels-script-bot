"""
Менеджер подписок для пользователей бота
"""
from typing import Dict, Optional
from datetime import datetime, timedelta
from config import MAX_REQUESTS_PER_USER
from database import Database
import logging

logger = logging.getLogger(__name__)


class SubscriptionManager:
    """Класс для управления подписками пользователей"""
    
    # Тарифы
    FREE_PLAN = "free"
    PREMIUM_PLAN = "premium"
    
    # Лимиты для тарифов
    @staticmethod
    def get_free_limit() -> int:
        """Возвращает лимит для бесплатного тарифа"""
        return MAX_REQUESTS_PER_USER
    
    PREMIUM_LIMIT = -1  # -1 означает безлимит
    
    # Длительность подписки
    PREMIUM_DURATION_DAYS = 30
    
    @staticmethod
    def get_user_plan(user_id: int) -> str:
        """Возвращает текущий тариф пользователя"""
        subscription = Database.get_subscription(user_id)
        if subscription:
            return subscription.get("plan", SubscriptionManager.FREE_PLAN)
        return SubscriptionManager.FREE_PLAN
    
    @staticmethod
    def has_active_subscription(user_id: int) -> bool:
        """Проверяет, есть ли у пользователя активная подписка"""
        return Database.has_active_subscription(user_id)
    
    @staticmethod
    def activate_subscription(user_id: int, duration_days: int = None) -> bool:
        """
        Активирует подписку для пользователя
        
        Args:
            user_id: ID пользователя
            duration_days: Длительность подписки в днях (по умолчанию PREMIUM_DURATION_DAYS)
        
        Returns:
            bool: True если успешно активирована
        """
        if duration_days is None:
            duration_days = SubscriptionManager.PREMIUM_DURATION_DAYS
        
        expires_at = datetime.now() + timedelta(days=duration_days)
        purchased_at = datetime.now()
        
        Database.create_subscription(
            user_id=user_id,
            plan=SubscriptionManager.PREMIUM_PLAN,
            expires_at=expires_at,
            purchased_at=purchased_at
        )
        logger.info(f"Подписка активирована для пользователя {user_id} до {expires_at}")
        return True
    
    @staticmethod
    def get_subscription_info(user_id: int) -> Optional[Dict]:
        """Возвращает информацию о подписке пользователя"""
        return Database.get_subscription(user_id)
    
    @staticmethod
    def cancel_subscription(user_id: int):
        """Отменяет подписку пользователя"""
        Database.cancel_subscription(user_id)
        logger.info(f"Подписка отменена для пользователя {user_id}")
    
    @staticmethod
    def get_all_subscriptions() -> Dict[int, Dict]:
        """Возвращает словарь всех активных подписок"""
        subscriptions_list = Database.get_all_active_subscriptions()
        return {sub["user_id"]: sub for sub in subscriptions_list}

