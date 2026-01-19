"""
Модуль для сбора метрик и мониторинга бота
"""
import time
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

class MetricsCollector:
    """Класс для сбора метрик бота"""
    
    def __init__(self):
        self._request_times: list = []
        self._error_count = 0
        self._success_count = 0
        self._user_activity: Dict[int, datetime] = {}
        self._command_usage: Dict[str, int] = defaultdict(int)
        self._last_reset = datetime.now()
        
    def record_request(self, duration: float, success: bool = True):
        """Записать метрику запроса"""
        self._request_times.append((time.time(), duration))
        if success:
            self._success_count += 1
        else:
            self._error_count += 1
        
        # Ограничиваем размер списка (храним последние 1000 запросов)
        if len(self._request_times) > 1000:
            self._request_times = self._request_times[-1000:]
    
    def record_user_activity(self, user_id: int):
        """Записать активность пользователя"""
        self._user_activity[user_id] = datetime.now()
        
        # Очищаем старые записи (старше 24 часов)
        cutoff = datetime.now() - timedelta(hours=24)
        self._user_activity = {
            uid: ts for uid, ts in self._user_activity.items()
            if ts > cutoff
        }
    
    def record_command(self, command: str):
        """Записать использование команды"""
        self._command_usage[command] += 1
    
    def get_stats(self) -> Dict:
        """Получить статистику за последний час"""
        now = time.time()
        hour_ago = now - 3600
        
        # Фильтруем запросы за последний час
        recent_requests = [
            (ts, duration) for ts, duration in self._request_times
            if ts >= hour_ago
        ]
        
        if not recent_requests:
            return {
                "total_requests": 0,
                "success_rate": 0.0,
                "avg_response_time": 0.0,
                "min_response_time": 0.0,
                "max_response_time": 0.0,
                "error_count": 0,
                "active_users": len(self._user_activity),
                "top_commands": {}
            }
        
        durations = [duration for _, duration in recent_requests]
        recent_success = sum(1 for _ in recent_requests)
        recent_total = len(recent_requests)
        
        return {
            "total_requests": recent_total,
            "success_rate": (recent_success / recent_total * 100) if recent_total > 0 else 0.0,
            "avg_response_time": sum(durations) / len(durations) if durations else 0.0,
            "min_response_time": min(durations) if durations else 0.0,
            "max_response_time": max(durations) if durations else 0.0,
            "error_count": self._error_count,
            "active_users": len(self._user_activity),
            "top_commands": dict(sorted(self._command_usage.items(), key=lambda x: x[1], reverse=True)[:10])
        }
    
    def reset_hourly_stats(self):
        """Сбросить статистику (вызывается каждый час)"""
        self._error_count = 0
        self._success_count = 0
        self._request_times = []
        self._command_usage.clear()
        self._last_reset = datetime.now()
        logger.info("Метрики сброшены")

# Глобальный экземпляр коллектора метрик
metrics_collector = MetricsCollector()

