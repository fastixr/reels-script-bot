"""
Модуль для работы с базой данных SQLite
"""
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Optional, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Используем постоянное хранилище /data для Amvera
import os
DB_NAME = os.path.join("/data", "bot_database.db")

# Создаем директорию /data если её нет (для локальной разработки)
if not os.path.exists("/data"):
    os.makedirs("/data", exist_ok=True)


@contextmanager
def get_db_connection():
    """Контекстный менеджер для работы с БД"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # Для доступа к колонкам по имени
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        conn.close()


def init_database():
    """Инициализация базы данных - создание таблиц"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        
        # Таблица запросов пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
                user_id INTEGER PRIMARY KEY,
                requests_count INTEGER DEFAULT 0,
                last_request TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        
        # Таблица подписок
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                plan TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        
        logger.info("База данных инициализирована")


class Database:
    """Класс для работы с базой данных"""
    
    @staticmethod
    def register_user(user_id: int):
        """Регистрация пользователя (при /start)"""
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id, registered_at, last_active, is_active)
                VALUES (?, ?, ?, ?)
            """, (user_id, now, now, True))
            # Обновляем last_active если пользователь уже существует
            cursor.execute("""
                UPDATE users SET last_active = ?, is_active = 1
                WHERE user_id = ?
            """, (now, user_id))
    
    @staticmethod
    def mark_user_active(user_id: int):
        """Отметить пользователя как активного"""
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id, last_active, is_active)
                VALUES (?, ?, ?)
            """, (user_id, now, True))
            cursor.execute("""
                UPDATE users SET last_active = ?, is_active = 1
                WHERE user_id = ?
            """, (now, user_id))
    
    @staticmethod
    def get_registered_users_count() -> int:
        """Получить количество зарегистрированных пользователей"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            return cursor.fetchone()[0]
    
    @staticmethod
    def get_active_users_count() -> int:
        """Получить количество активных пользователей"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Активные = те, кто был активен за последние 30 дней
            cursor.execute("""
                SELECT COUNT(*) FROM users 
                WHERE is_active = 1 
                AND last_active > datetime('now', '-30 days')
            """)
            return cursor.fetchone()[0]
    
    @staticmethod
    def get_all_active_user_ids() -> List[int]:
        """Получить список всех активных user_id для рассылки"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_active = 1")
            return [row[0] for row in cursor.fetchall()]
    
    @staticmethod
    def get_user_requests_count(user_id: int) -> int:
        """Получить количество запросов пользователя"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT requests_count FROM user_requests WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
    
    @staticmethod
    def increment_user_requests(user_id: int):
        """Увеличить счетчик запросов пользователя"""
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Создаем запись если её нет
            cursor.execute("""
                INSERT OR IGNORE INTO user_requests (user_id, requests_count, last_request)
                VALUES (?, 0, ?)
            """, (user_id, now))
            # Увеличиваем счетчик
            cursor.execute("""
                UPDATE user_requests 
                SET requests_count = requests_count + 1, last_request = ?
                WHERE user_id = ?
            """, (now, user_id))
    
    @staticmethod
    def reset_user_requests(user_id: int):
        """Сбросить счетчик запросов пользователя"""
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_requests SET requests_count = 0, last_request = ?
                WHERE user_id = ?
            """, (now, user_id))
    
    @staticmethod
    def get_total_requests_count() -> int:
        """Получить общее количество запросов"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(requests_count) FROM user_requests")
            result = cursor.fetchone()[0]
            return result if result else 0
    
    @staticmethod
    def get_users_with_requests_count() -> int:
        """Получить количество пользователей с запросами"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM user_requests WHERE requests_count > 0")
            return cursor.fetchone()[0]
    
    @staticmethod
    def create_subscription(user_id: int, plan: str, expires_at: datetime, purchased_at: datetime = None):
        """Создать подписку"""
        if purchased_at is None:
            purchased_at = datetime.now()
        expires_at_str = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
        purchased_at_str = purchased_at.isoformat() if isinstance(purchased_at, datetime) else purchased_at
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at, purchased_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, plan, expires_at_str, purchased_at_str))
    
    @staticmethod
    def get_subscription(user_id: int) -> Optional[Dict]:
        """Получить информацию о подписке пользователя"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT plan, expires_at, purchased_at 
                FROM subscriptions 
                WHERE user_id = ? AND expires_at > datetime('now')
            """, (user_id,))
            row = cursor.fetchone()
            if row:
                expires_at = datetime.fromisoformat(row[1]) if isinstance(row[1], str) else row[1]
                purchased_at = datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2]
                days_left = (expires_at - datetime.now()).days
                return {
                    "plan": row[0],
                    "expires_at": expires_at,
                    "purchased_at": purchased_at,
                    "days_left": days_left
                }
            return None
    
    @staticmethod
    def has_active_subscription(user_id: int) -> bool:
        """Проверить, есть ли у пользователя активная подписка"""
        subscription = Database.get_subscription(user_id)
        return subscription is not None
    
    @staticmethod
    def cancel_subscription(user_id: int):
        """Отменить подписку пользователя"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    
    @staticmethod
    def get_all_active_subscriptions() -> List[Dict]:
        """Получить все активные подписки"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, plan, expires_at, purchased_at 
                FROM subscriptions 
                WHERE expires_at > datetime('now')
            """)
            subscriptions = []
            for row in cursor.fetchall():
                expires_at = datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2]
                purchased_at = datetime.fromisoformat(row[3]) if isinstance(row[3], str) else row[3]
                subscriptions.append({
                    "user_id": row[0],
                    "plan": row[1],
                    "expires_at": expires_at,
                    "purchased_at": purchased_at
                })
            return subscriptions
    
    @staticmethod
    def cleanup_expired_subscriptions():
        """Очистить истекшие подписки (можно вызывать периодически)"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE expires_at <= datetime('now')")
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"Удалено {deleted} истекших подписок")

