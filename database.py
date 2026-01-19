"""
Модуль для работы с базой данных PostgreSQL
"""
import asyncpg
import logging
from datetime import datetime
from typing import Dict, Optional, List
import time
import os
import asyncio
import json

from config import (
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, 
    POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DSN
)

logger = logging.getLogger(__name__)

class SimpleCache:
    """Простой кэш с TTL"""
    def __init__(self, ttl: int = 300):
        self._cache: Dict[str, tuple] = {}
        self.ttl = ttl
    
    def get(self, key: str) -> Optional[any]:
        if key not in self._cache:
            return None
        value, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl:
            del self._cache[key]
            return None
        return value
    
    def set(self, key: str, value: any):
        self._cache[key] = (value, time.time())
    
    def invalidate(self, key: str):
        if key in self._cache:
            del self._cache[key]
    
    def clear(self):
        self._cache.clear()

_db_cache = SimpleCache(ttl=600)

_db_pool = None

async def get_db_pool():
    """Получить connection pool для PostgreSQL"""
    global _db_pool
    if _db_pool is None:
        try:
            if POSTGRES_DSN:
                logger.info(f"Подключение к PostgreSQL через DSN...")
                _db_pool = await asyncpg.create_pool(
                    POSTGRES_DSN,
                    min_size=2,
                    max_size=10,
                    command_timeout=30
                )
            else:
                logger.info(f"Подключение к PostgreSQL: {POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}")
                _db_pool = await asyncpg.create_pool(
                    host=POSTGRES_HOST,
                    port=POSTGRES_PORT,
                    database=POSTGRES_DB,
                    user=POSTGRES_USER,
                    password=POSTGRES_PASSWORD,
                    min_size=2,
                    max_size=10,
                    command_timeout=30
                )
            logger.info("✅ Подключение к PostgreSQL установлено")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")
            logger.error(f"Проверьте настройки подключения:")
            if POSTGRES_DSN:
                logger.error(f"POSTGRES_DSN: установлен (скрыт)")
            else:
                logger.error(f"POSTGRES_HOST: {POSTGRES_HOST}")
                logger.error(f"POSTGRES_PORT: {POSTGRES_PORT}")
                logger.error(f"POSTGRES_DB: {POSTGRES_DB}")
                logger.error(f"POSTGRES_USER: {POSTGRES_USER}")
            raise
    return _db_pool

async def init_database():
    """Инициализация базы данных - создание таблиц"""
    try:
        pool = await get_db_pool()
    except Exception as e:
        logger.error(f"Не удалось подключиться к базе данных: {e}")
        logger.error("Бот не может работать без подключения к PostgreSQL!")
        logger.error("Проверьте:")
        logger.error("1. Запущен ли PostgreSQL сервер")
        logger.error("2. Правильность настроек подключения (POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD)")
        logger.error("3. Доступность базы данных из сети (firewall, security groups)")
        raise
    
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
                user_id BIGINT PRIMARY KEY,
                requests_count INTEGER DEFAULT 0,
                last_request TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id BIGINT PRIMARY KEY,
                plan TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS extra_requests (
                user_id BIGINT PRIMARY KEY,
                requests_count INTEGER DEFAULT 0,
                last_purchase TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id BIGINT PRIMARY KEY,
                editing_patterns JSONB,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scenario_edits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                original_scenario TEXT NOT NULL,
                improved_scenario TEXT NOT NULL,
                improvement_request TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referred_id BIGINT NOT NULL,
                referral_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bonus_given BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(user_id) ON DELETE CASCADE,
                UNIQUE(referred_id)
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_codes (
                user_id BIGINT PRIMARY KEY,
                referral_code TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                inv_id INTEGER NOT NULL UNIQUE,
                payment_type TEXT NOT NULL,
                amount DECIMAL(10, 2) NOT NULL,
                period_months INTEGER,
                count INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payments_user_id 
            ON payments(user_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payments_inv_id 
            ON payments(inv_id)
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_scenarios (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                niche TEXT,
                format_type TEXT,
                style TEXT,
                tone TEXT,
                duration TEXT,
                platform TEXT,
                topic TEXT,
                scenario_text TEXT NOT NULL,
                is_premium BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_scenarios_user_id 
            ON user_scenarios(user_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_scenarios_created_at 
            ON user_scenarios(created_at DESC)
        """)
        
        logger.info("✅ Таблица user_scenarios создана/проверена")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_templates (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                prompt_modifier TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_templates_user_id 
            ON user_templates(user_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_templates_created_at 
            ON user_templates(created_at DESC)
        """)
        
        logger.info("✅ Таблица user_templates создана/проверена")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scenario_statistics (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                niche TEXT,
                format_type TEXT,
                style TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_statistics_user_id 
            ON scenario_statistics(user_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_statistics_created_at 
            ON scenario_statistics(created_at DESC)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_statistics_niche 
            ON scenario_statistics(niche)
        """)
        
        logger.info("✅ Таблица scenario_statistics создана/проверена")
        
        # Таблица для шаринга сценариев (Premium)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scenario_shares (
                id SERIAL PRIMARY KEY,
                scenario_id INTEGER NOT NULL,
                owner_id BIGINT NOT NULL,
                share_token TEXT NOT NULL UNIQUE,
                access_type TEXT DEFAULT 'view_only',
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (scenario_id) REFERENCES user_scenarios(id) ON DELETE CASCADE,
                FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_shares_scenario_id 
            ON scenario_shares(scenario_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_shares_owner_id 
            ON scenario_shares(owner_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_shares_token 
            ON scenario_shares(share_token)
        """)
        
        logger.info("✅ Таблица scenario_shares создана/проверена")
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payments_status 
            ON payments(status)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario_edits_user_id 
            ON scenario_edits(user_id, created_at DESC)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id 
            ON referrals(referrer_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referred_id 
            ON referrals(referred_id)
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_is_active 
            ON users(is_active)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_expires_at 
            ON subscriptions(expires_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_last_active 
            ON users(last_active)
        """)
        
        # Миграция: добавляем колонку editing_patterns, если её нет
        try:
            # Проверяем, существует ли колонка editing_patterns
            result = await conn.fetchval("""
                SELECT COUNT(*) 
                FROM information_schema.columns 
                WHERE table_name = 'user_preferences' 
                AND column_name = 'editing_patterns'
            """)
            if result == 0:
                # Колонки нет, добавляем её
                await conn.execute("""
                    ALTER TABLE user_preferences 
                    ADD COLUMN IF NOT EXISTS editing_patterns JSONB
                """)
                logger.info("Добавлена колонка editing_patterns в user_preferences")
        except Exception as e:
            logger.warning(f"Ошибка при добавлении колонки editing_patterns: {e}")
        
        # Миграция: удаляем старые поля preferred_style и preferred_niche, если они существуют
        try:
            await conn.execute("""
                ALTER TABLE user_preferences 
                DROP COLUMN IF EXISTS preferred_style,
                DROP COLUMN IF EXISTS preferred_niche
            """)
        except Exception as e:
            # Игнорируем ошибки, если колонки уже удалены или не существуют
            logger.debug(f"Миграция полей user_preferences: {e}")
        
        # Миграция: добавляем колонку channel_subscribed, если её нет
        try:
            await conn.execute("""
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS channel_subscribed BOOLEAN DEFAULT FALSE
            """)
            logger.info("Добавлена колонка channel_subscribed в users")
        except Exception as e:
            logger.warning(f"Ошибка при добавлении колонки channel_subscribed: {e}")
        
        # Миграция: добавляем колонку first_scenario_shown, если её нет
        try:
            await conn.execute("""
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS first_scenario_shown BOOLEAN DEFAULT FALSE
            """)
            logger.info("Добавлена колонка first_scenario_shown в users")
        except Exception as e:
            logger.warning(f"Ошибка при добавлении колонки first_scenario_shown: {e}")
        
        # Миграция: добавляем колонки для отслеживания напоминаний
        try:
            await conn.execute("""
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS last_channel_reminder TIMESTAMP,
                ADD COLUMN IF NOT EXISTS last_referral_reminder TIMESTAMP
            """)
            logger.info("Добавлены колонки для отслеживания напоминаний в users")
        except Exception as e:
            logger.warning(f"Ошибка при добавлении колонок напоминаний: {e}")
        
        logger.info("База данных PostgreSQL инициализирована с индексами")


class Database:
    """Класс для работы с базой данных"""
    
    @staticmethod
    async def register_user(user_id: int):
        """Регистрация пользователя (при /start)"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            now = datetime.now()
            await conn.execute("""
                INSERT INTO users (user_id, registered_at, last_active, is_active)
                VALUES ($1, $2, $2, TRUE)
                ON CONFLICT (user_id) DO UPDATE 
                SET last_active = $2, is_active = TRUE
            """, user_id, now)
    
    @staticmethod
    async def is_user_new(user_id: int) -> bool:
        """Проверить, является ли пользователь новым (первый запуск бота)"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем, существует ли пользователь в БД
            result = await conn.fetchval("""
                SELECT registered_at FROM users WHERE user_id = $1
            """, user_id)
            
            # Если пользователя нет в БД - он новый
            if result is None:
                return True
            
            # Если пользователь уже существует в БД - он не новый
            # (даже если он перезапускает бота, он уже был зарегистрирован ранее)
            return False
    
    @staticmethod
    async def mark_user_active(user_id: int):
        """Отметить пользователя как активного"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            now = datetime.now()
            await conn.execute("""
                INSERT INTO users (user_id, last_active, is_active)
                VALUES ($1, $2, TRUE)
                ON CONFLICT (user_id) DO UPDATE 
                SET last_active = $2, is_active = TRUE
            """, user_id, now)
    
    @staticmethod
    async def is_channel_subscribed(user_id: int) -> bool:
        """Проверить, прошел ли пользователь проверку подписки на канал"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT channel_subscribed FROM users WHERE user_id = $1
            """, user_id)
            return result if result is not None else False
    
    @staticmethod
    async def set_channel_subscribed(user_id: int, subscribed: bool = True):
        """Установить статус подписки на канал для пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, channel_subscribed)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE 
                SET channel_subscribed = $2
            """, user_id, subscribed)
    
    @staticmethod
    async def is_first_scenario_shown(user_id: int) -> bool:
        """Проверить, показывалось ли уже сообщение с предложениями при первом создании сценария"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT first_scenario_shown FROM users WHERE user_id = $1
            """, user_id)
            return result if result is not None else False
    
    @staticmethod
    async def set_first_scenario_shown(user_id: int, shown: bool = True):
        """Установить флаг показа сообщения с предложениями"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, first_scenario_shown)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE 
                SET first_scenario_shown = $2
            """, user_id, shown)
    
    @staticmethod
    async def get_users_for_channel_reminder(reminder_interval_hours: int = 48) -> List[int]:
        """Получить список пользователей, которым нужно напомнить о подписке на канал"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id FROM users 
                WHERE is_active = TRUE 
                AND channel_subscribed = FALSE
                AND (last_channel_reminder IS NULL 
                     OR last_channel_reminder < NOW() - ($1 || ' hours')::INTERVAL)
                AND last_active > NOW() - INTERVAL '7 days'
            """, reminder_interval_hours)
            return [row['user_id'] for row in rows]
    
    @staticmethod
    async def get_users_for_referral_reminder(reminder_interval_hours: int = 72) -> List[int]:
        """Получить список пользователей, которым нужно напомнить о реферальной программе"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id FROM users 
                WHERE is_active = TRUE 
                AND (last_referral_reminder IS NULL 
                     OR last_referral_reminder < NOW() - ($1 || ' hours')::INTERVAL)
                AND last_active > NOW() - INTERVAL '7 days'
            """, reminder_interval_hours)
            return [row['user_id'] for row in rows]
    
    @staticmethod
    async def update_channel_reminder_time(user_id: int):
        """Обновить время последнего напоминания о подписке на канал"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users 
                SET last_channel_reminder = NOW()
                WHERE user_id = $1
            """, user_id)
    
    @staticmethod
    async def update_referral_reminder_time(user_id: int):
        """Обновить время последнего напоминания о реферальной программе"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users 
                SET last_referral_reminder = NOW()
                WHERE user_id = $1
            """, user_id)
    
    @staticmethod
    async def get_registered_users_count() -> int:
        """Получить количество зарегистрированных пользователей"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
    
    @staticmethod
    async def get_active_users_count() -> int:
        """Получить количество активных пользователей"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT COUNT(*) FROM users 
                WHERE is_active = TRUE 
                AND last_active > NOW() - INTERVAL '30 days'
            """)
    
    @staticmethod
    async def get_all_active_user_ids() -> List[int]:
        """Получить список всех активных user_id для рассылки"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_active = TRUE")
            return [row['user_id'] for row in rows]
    
    @staticmethod
    async def get_user_requests_count(user_id: int) -> int:
        """Получить количество запросов пользователя (с кэшированием)"""
        cache_key = f"requests_{user_id}"
        cached = _db_cache.get(cache_key)
        if cached is not None:
            return cached
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT requests_count FROM user_requests WHERE user_id = $1
            """, user_id)
            result = result if result is not None else 0
            _db_cache.set(cache_key, result)
            return result
    
    @staticmethod
    async def increment_user_requests(user_id: int):
        """Увеличить счетчик запросов пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            now = datetime.now()
            await conn.execute("""
                INSERT INTO user_requests (user_id, requests_count, last_request)
                VALUES ($1, 0, $2)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, now)
            await conn.execute("""
                UPDATE user_requests 
                SET requests_count = requests_count + 1, last_request = $1
                WHERE user_id = $2
            """, now, user_id)
        
        _db_cache.invalidate(f"requests_{user_id}")
    
    @staticmethod
    async def reset_user_requests(user_id: int):
        """Сбросить счетчик запросов пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            now = datetime.now()
            await conn.execute("""
                UPDATE user_requests SET requests_count = 0, last_request = $1
                WHERE user_id = $2
            """, now, user_id)
    
    @staticmethod
    async def get_total_requests_count() -> int:
        """Получить общее количество запросов"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT SUM(requests_count) FROM user_requests")
            return result if result is not None else 0
    
    @staticmethod
    async def get_users_with_requests_count() -> int:
        """Получить количество пользователей с запросами"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM user_requests WHERE requests_count > 0")
    
    @staticmethod
    async def create_subscription(user_id: int, plan: str, expires_at: datetime, purchased_at: datetime = None):
        """Создать подписку"""
        if purchased_at is None:
            purchased_at = datetime.now()
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO subscriptions (user_id, plan, expires_at, purchased_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE 
                SET plan = $2, expires_at = $3, purchased_at = $4
            """, user_id, plan, expires_at, purchased_at)
        
        _db_cache.invalidate(f"subscription_{user_id}")
    
    @staticmethod
    async def get_subscription(user_id: int) -> Optional[Dict]:
        """Получить информацию о подписке пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT plan, expires_at, purchased_at 
                FROM subscriptions 
                WHERE user_id = $1 AND expires_at > NOW()
            """, user_id)
            if row:
                expires_at = row['expires_at']
                purchased_at = row['purchased_at']
                days_left = (expires_at - datetime.now()).days
                return {
                    "plan": row['plan'],
                    "expires_at": expires_at,
                    "purchased_at": purchased_at,
                    "days_left": days_left
                }
            return None
    
    @staticmethod
    async def has_active_subscription(user_id: int) -> bool:
        """Проверить, есть ли у пользователя активная подписка (с кэшированием)"""
        cache_key = f"subscription_{user_id}"
        cached = _db_cache.get(cache_key)
        if cached is not None:
            return cached
        
        subscription = await Database.get_subscription(user_id)
        result = subscription is not None
        _db_cache.set(cache_key, result)
        return result
    
    @staticmethod
    async def cancel_subscription(user_id: int):
        """Отменить подписку пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM subscriptions WHERE user_id = $1", user_id)
        
        _db_cache.invalidate(f"subscription_{user_id}")
    
    @staticmethod
    async def get_all_active_subscriptions() -> List[Dict]:
        """Получить все активные подписки"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, plan, expires_at, purchased_at 
                FROM subscriptions 
                WHERE expires_at > NOW()
            """)
            if not rows:
                return []
            subscriptions = []
            for row in rows:
                subscriptions.append({
                    "user_id": row['user_id'],
                    "plan": row['plan'],
                    "expires_at": row['expires_at'],
                    "purchased_at": row['purchased_at']
                })
            return subscriptions
    
    @staticmethod
    async def cleanup_expired_subscriptions():
        """Очистить истекшие подписки (можно вызывать периодически)"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            deleted = await conn.execute("DELETE FROM subscriptions WHERE expires_at <= NOW()")
            if deleted:
                logger.info(f"Удалено истекших подписок")
    
    @staticmethod
    async def get_extra_requests_count(user_id: int) -> int:
        """Получить количество дополнительных попыток пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT requests_count FROM extra_requests WHERE user_id = $1
            """, user_id)
            return result if result is not None else 0
    
    @staticmethod
    async def add_extra_requests(user_id: int, count: int):
        """Добавить дополнительные попытки пользователю"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            now = datetime.now()
            await conn.execute("""
                INSERT INTO extra_requests (user_id, requests_count, last_purchase)
                VALUES ($1, 0, $2)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, now)
            await conn.execute("""
                UPDATE extra_requests 
                SET requests_count = requests_count + $1, last_purchase = $2
                WHERE user_id = $3
            """, count, now, user_id)
    
    @staticmethod
    async def use_extra_request(user_id: int) -> bool:
        """Использовать одну дополнительную попытку. Возвращает True если попытка была использована"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT requests_count FROM extra_requests WHERE user_id = $1
            """, user_id)
            if count is None or count <= 0:
                return False
            
            result = await conn.execute("""
                UPDATE extra_requests 
                SET requests_count = requests_count - 1
                WHERE user_id = $1 AND requests_count > 0
            """, user_id)
            return result == "UPDATE 1"
    
    @staticmethod
    async def save_scenario_edit(user_id: int, original_scenario: str, improved_scenario: str, improvement_request: str = None):
        """Сохранить историю редактирования сценария"""
        await Database.register_user(user_id)
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scenario_edits (user_id, original_scenario, improved_scenario, improvement_request)
                VALUES ($1, $2, $3, $4)
            """, user_id, original_scenario, improved_scenario, improvement_request)
    
    @staticmethod
    async def get_user_editing_patterns(user_id: int) -> Dict:
        """Получить паттерны редактирования пользователя (с кэшированием)"""
        # Убеждаемся, что пользователь зарегистрирован
        await Database.register_user(user_id)
        
        cache_key = f"editing_patterns_{user_id}"
        cached = _db_cache.get(cache_key)
        if cached is not None:
            return cached
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT editing_patterns 
                FROM user_preferences 
                WHERE user_id = $1
            """, user_id)
            
            if row and row['editing_patterns']:
                result = row['editing_patterns'] if isinstance(row['editing_patterns'], dict) else json.loads(row['editing_patterns'])
            else:
                result = {}
            
            _db_cache.set(cache_key, result)
            return result
    
    @staticmethod
    async def save_editing_patterns(user_id: int, patterns: Dict):
        """Сохранить паттерны редактирования пользователя"""
        await Database.register_user(user_id)
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_preferences (user_id, editing_patterns)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (user_id) DO UPDATE 
                SET editing_patterns = $2::jsonb
            """, user_id, json.dumps(patterns))
        
        _db_cache.invalidate(f"editing_patterns_{user_id}")
    
    @staticmethod
    async def get_recent_edits(user_id: int, limit: int = 10) -> List[Dict]:
        """Получить последние редактирования пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT original_scenario, improved_scenario, improvement_request, created_at
                FROM scenario_edits
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_id, limit)
            
            return [
                {
                    "original": row['original_scenario'],
                    "improved": row['improved_scenario'],
                    "request": row['improvement_request'],
                    "created_at": row['created_at']
                }
                for row in rows
            ]
    
    @staticmethod
    async def get_referral_code(user_id: int) -> str:
        """Получить реферальный код пользователя (просто user_id)"""
        return str(user_id)
    
    @staticmethod
    async def register_referral(referrer_id: int, referred_id: int) -> bool:
        """
        Зарегистрировать реферальную связь и начислить бонусы
        
        Returns:
            bool: True если регистрация успешна, False если пользователь уже был приглашен
        """
        if referrer_id == referred_id:
            return False  # Нельзя пригласить самого себя
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем, не был ли уже приглашен этот пользователь
            existing = await conn.fetchval("""
                SELECT id FROM referrals WHERE referred_id = $1
            """, referred_id)
            
            if existing:
                return False  # Пользователь уже был приглашен
            
            # Регистрируем реферальную связь
            await conn.execute("""
                INSERT INTO referrals (referrer_id, referred_id, bonus_given)
                VALUES ($1, $2, TRUE)
            """, referrer_id, referred_id)
            
            # Начисляем 1 попытку пригласившему
            await Database.add_extra_requests(referrer_id, 1)
            
            logger.info(f"Реферальная связь зарегистрирована: {referrer_id} -> {referred_id}")
            return True
    
    @staticmethod
    async def get_referral_stats(user_id: int) -> Dict:
        """Получить статистику рефералов пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Количество приглашенных друзей
            referrals_count = await conn.fetchval("""
                SELECT COUNT(*) FROM referrals WHERE referrer_id = $1
            """, user_id)
            
            # Количество попыток, заработанных через рефералов (1 попытка за каждого)
            earned_attempts = referrals_count if referrals_count else 0
            
            return {
                "total_referrals": referrals_count if referrals_count else 0,
                "earned_attempts": earned_attempts
            }
    
    @staticmethod
    async def get_referrer_id(user_id: int) -> Optional[int]:
        """Получить ID пользователя, который пригласил данного пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT referrer_id FROM referrals WHERE referred_id = $1
            """, user_id)
            return result
    
    @staticmethod
    async def save_referral_code(user_id: int, referral_code: str):
        """Сохранить реферальный код пользователя из партнерской программы"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO referral_codes (user_id, referral_code)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE 
                SET referral_code = $2
            """, user_id, referral_code)
    
    @staticmethod
    async def get_referrer_by_code(referral_code: str) -> Optional[int]:
        """Получить user_id реферера по коду партнерской программы"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT user_id FROM referral_codes WHERE referral_code = $1
            """, referral_code)
            return result
    
    @staticmethod
    async def register_referral_by_code(referral_code: str, referred_id: int) -> bool:
        """
        Зарегистрировать реферальную связь по коду партнерской программы
        
        Returns:
            bool: True если регистрация успешна, False если пользователь уже был приглашен
        """
        referrer_id = await Database.get_referrer_by_code(referral_code)
        if not referrer_id:
            return False  # Код не найден
        
        if referrer_id == referred_id:
            return False  # Нельзя пригласить самого себя
        
        # Используем существующий метод register_referral
        return await Database.register_referral(referrer_id, referred_id)
    
    @staticmethod
    async def create_payment(
        user_id: int,
        inv_id: int,
        payment_type: str,
        amount: float,
        period_months: Optional[int] = None,
        count: Optional[int] = None
    ) -> bool:
        """
        Создать запись о платеже
        
        Args:
            user_id: ID пользователя
            inv_id: Номер счета (уникальный ID заказа)
            payment_type: Тип платежа ('subscription' или 'extra_requests')
            amount: Сумма платежа
            period_months: Период подписки в месяцах (для subscription)
            count: Количество попыток (для extra_requests)
        
        Returns:
            bool: True если создано успешно, False если inv_id уже существует
        """
        pool = await get_db_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO payments (user_id, inv_id, payment_type, amount, period_months, count, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                """, user_id, inv_id, payment_type, amount, period_months, count)
                return True
        except asyncpg.UniqueViolationError:
            logger.warning(f"Платеж с inv_id={inv_id} уже существует")
            return False
    
    @staticmethod
    async def get_payment_by_inv_id(inv_id: int) -> Optional[Dict]:
        """Получить информацию о платеже по inv_id"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, user_id, inv_id, payment_type, amount, period_months, count, status, created_at, paid_at
                FROM payments
                WHERE inv_id = $1
            """, inv_id)
            if row:
                return {
                    "id": row['id'],
                    "user_id": row['user_id'],
                    "inv_id": row['inv_id'],
                    "payment_type": row['payment_type'],
                    "amount": float(row['amount']),
                    "period_months": row['period_months'],
                    "count": row['count'],
                    "status": row['status'],
                    "created_at": row['created_at'],
                    "paid_at": row['paid_at']
                }
            return None
    
    @staticmethod
    async def mark_payment_paid(inv_id: int) -> bool:
        """Отметить платеж как оплаченный"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE payments
                SET status = 'paid', paid_at = CURRENT_TIMESTAMP
                WHERE inv_id = $1 AND status = 'pending'
            """, inv_id)
            return result == "UPDATE 1"
    
    @staticmethod
    async def get_next_inv_id() -> int:
        """Получить следующий уникальный номер счета"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Получаем максимальный inv_id и добавляем 1
            max_id = await conn.fetchval("SELECT COALESCE(MAX(inv_id), 0) FROM payments")
            return max_id + 1
    
    @staticmethod
    async def save_user_scenario(
        user_id: int,
        scenario_text: str,
        niche: str = None,
        format_type: str = None,
        style: str = None,
        tone: str = None,
        duration: str = None,
        platform: str = None,
        topic: str = None,
        is_premium: bool = False
    ) -> int:
        """Сохранить сценарий пользователя в историю"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            scenario_id = await conn.fetchval("""
                INSERT INTO user_scenarios 
                (user_id, niche, format_type, style, tone, duration, platform, topic, scenario_text, is_premium)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
            """, user_id, niche, format_type, style, tone, duration, platform, topic, scenario_text, is_premium)
            return scenario_id
    
    @staticmethod
    async def get_user_scenarios(user_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
        """Получить список сценариев пользователя (последние сначала)"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, niche, format_type, style, tone, duration, platform, topic, 
                       scenario_text, is_premium, created_at
                FROM user_scenarios
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, user_id, limit, offset)
            
            scenarios = []
            for row in rows:
                scenarios.append({
                    "id": row['id'],
                    "niche": row['niche'],
                    "format_type": row['format_type'],
                    "style": row['style'],
                    "tone": row['tone'],
                    "duration": row['duration'],
                    "platform": row['platform'],
                    "topic": row['topic'],
                    "scenario_text": row['scenario_text'],
                    "is_premium": row['is_premium'],
                    "created_at": row['created_at']
                })
            return scenarios
    
    @staticmethod
    async def get_user_scenarios_count(user_id: int) -> int:
        """Получить количество сохраненных сценариев пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM user_scenarios WHERE user_id = $1
            """, user_id)
            return count if count else 0
    
    @staticmethod
    async def save_scenario_statistics(
        user_id: int,
        niche: str = None,
        format_type: str = None,
        style: str = None
    ):
        """Сохранить статистику генерации сценария"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scenario_statistics (user_id, niche, format_type, style)
                VALUES ($1, $2, $3, $4)
            """, user_id, niche, format_type, style)
    
    @staticmethod
    async def get_user_statistics(user_id: int) -> Dict:
        """Получить статистику пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Общее количество сценариев
            total_count = await conn.fetchval("""
                SELECT COUNT(*) FROM scenario_statistics WHERE user_id = $1
            """, user_id) or 0
            
            # Популярные ниши
            niche_stats = await conn.fetch("""
                SELECT niche, COUNT(*) as count
                FROM scenario_statistics
                WHERE user_id = $1 AND niche IS NOT NULL
                GROUP BY niche
                ORDER BY count DESC
                LIMIT 5
            """, user_id)
            
            # Популярные форматы
            format_stats = await conn.fetch("""
                SELECT format_type, COUNT(*) as count
                FROM scenario_statistics
                WHERE user_id = $1 AND format_type IS NOT NULL
                GROUP BY format_type
                ORDER BY count DESC
                LIMIT 5
            """, user_id)
            
            # Популярные стили
            style_stats = await conn.fetch("""
                SELECT style, COUNT(*) as count
                FROM scenario_statistics
                WHERE user_id = $1 AND style IS NOT NULL
                GROUP BY style
                ORDER BY count DESC
                LIMIT 5
            """, user_id)
            
            # Активность по дням (последние 30 дней)
            activity_stats = await conn.fetch("""
                SELECT DATE(created_at) as date, COUNT(*) as count
                FROM scenario_statistics
                WHERE user_id = $1 
                AND created_at >= NOW() - INTERVAL '30 days'
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """, user_id)
            
            return {
                'total_count': total_count,
                'niche_stats': [{'niche': row['niche'], 'count': row['count']} for row in niche_stats],
                'format_stats': [{'format_type': row['format_type'], 'count': row['count']} for row in format_stats],
                'style_stats': [{'style': row['style'], 'count': row['count']} for row in style_stats],
                'activity_stats': [{'date': row['date'], 'count': row['count']} for row in activity_stats]
            }
    
    @staticmethod
    async def save_user_template(
        user_id: int,
        name: str,
        description: str,
        prompt_modifier: str
    ) -> int:
        """Сохранить пользовательский шаблон"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            template_id = await conn.fetchval("""
                INSERT INTO user_templates 
                (user_id, name, description, prompt_modifier)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, user_id, name, description, prompt_modifier)
            return template_id
    
    @staticmethod
    async def get_user_templates(user_id: int) -> List[Dict]:
        """Получить все шаблоны пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, name, description, prompt_modifier, created_at
                FROM user_templates
                WHERE user_id = $1
                ORDER BY created_at DESC
            """, user_id)
            if not rows:
                return []
            templates = []
            for row in rows:
                templates.append({
                    'id': row['id'],
                    'name': row['name'],
                    'description': row['description'],
                    'prompt_modifier': row['prompt_modifier'],
                    'created_at': row['created_at']
                })
            return templates
    
    @staticmethod
    async def get_user_template(template_id: int, user_id: int) -> Optional[Dict]:
        """Получить конкретный шаблон пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, name, description, prompt_modifier, created_at
                FROM user_templates
                WHERE id = $1 AND user_id = $2
            """, template_id, user_id)
            if not row:
                return None
            return {
                'id': row['id'],
                'name': row['name'],
                'description': row['description'],
                'prompt_modifier': row['prompt_modifier'],
                'created_at': row['created_at']
            }
    
    @staticmethod
    async def delete_user_template(template_id: int, user_id: int) -> bool:
        """Удалить шаблон пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            deleted = await conn.execute("""
                DELETE FROM user_templates
                WHERE id = $1 AND user_id = $2
            """, template_id, user_id)
            return deleted == "DELETE 1"
    
    @staticmethod
    async def get_scenario_by_id(scenario_id: int, user_id: int) -> Optional[Dict]:
        """Получить конкретный сценарий по ID (только если он принадлежит пользователю)"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, niche, format_type, style, tone, duration, platform, topic, 
                       scenario_text, is_premium, created_at
                FROM user_scenarios
                WHERE id = $1 AND user_id = $2
            """, scenario_id, user_id)
            
            if row:
                return {
                    "id": row['id'],
                    "niche": row['niche'],
                    "format_type": row['format_type'],
                    "style": row['style'],
                    "tone": row['tone'],
                    "duration": row['duration'],
                    "platform": row['platform'],
                    "topic": row['topic'],
                    "scenario_text": row['scenario_text'],
                    "is_premium": row['is_premium'],
                    "created_at": row['created_at']
                }
            return None
    
    @staticmethod
    async def create_scenario_share(scenario_id: int, owner_id: int, share_token: str, 
                                     expires_at: Optional[datetime] = None) -> bool:
        """Создать шаринг-ссылку на сценарий"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем, что сценарий принадлежит пользователю
            scenario = await conn.fetchrow("""
                SELECT id FROM user_scenarios
                WHERE id = $1 AND user_id = $2
            """, scenario_id, owner_id)
            
            if not scenario:
                return False
            
            # Создаем шаринг
            await conn.execute("""
                INSERT INTO scenario_shares (scenario_id, owner_id, share_token, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (share_token) DO UPDATE 
                SET scenario_id = $1, expires_at = $4
            """, scenario_id, owner_id, share_token, expires_at)
            return True
    
    @staticmethod
    async def get_scenario_by_share_token(share_token: str) -> Optional[Dict]:
        """Получить сценарий по шаринг-токену"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT ss.scenario_id, ss.owner_id, ss.expires_at,
                       us.niche, us.format_type, us.style, us.tone, us.duration,
                       us.platform, us.topic, us.scenario_text, us.is_premium, us.created_at
                FROM scenario_shares ss
                JOIN user_scenarios us ON ss.scenario_id = us.id
                WHERE ss.share_token = $1
                AND (ss.expires_at IS NULL OR ss.expires_at > NOW())
            """, share_token)
            
            if row:
                return {
                    "id": row['scenario_id'],
                    "niche": row['niche'],
                    "format_type": row['format_type'],
                    "style": row['style'],
                    "tone": row['tone'],
                    "duration": row['duration'],
                    "platform": row['platform'],
                    "topic": row['topic'],
                    "scenario_text": row['scenario_text'],
                    "is_premium": row['is_premium'],
                    "created_at": row['created_at'],
                    "owner_id": row['owner_id']
                }
            return None
    
    @staticmethod
    async def get_user_scenario_shares(user_id: int) -> List[Dict]:
        """Получить все шаринг-ссылки пользователя"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ss.id, ss.scenario_id, ss.share_token, ss.expires_at, ss.created_at,
                       us.niche, us.topic
                FROM scenario_shares ss
                JOIN user_scenarios us ON ss.scenario_id = us.id
                WHERE ss.owner_id = $1
                ORDER BY ss.created_at DESC
            """, user_id)
            
            return [
                {
                    "id": row['id'],
                    "scenario_id": row['scenario_id'],
                    "share_token": row['share_token'],
                    "expires_at": row['expires_at'],
                    "created_at": row['created_at'],
                    "niche": row['niche'],
                    "topic": row['topic']
                }
                for row in rows
            ]
    
    @staticmethod
    async def delete_scenario_share(share_id: int, user_id: int) -> bool:
        """Удалить шаринг-ссылку"""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            deleted = await conn.execute("""
                DELETE FROM scenario_shares
                WHERE id = $1 AND owner_id = $2
            """, share_id, user_id)
            return deleted == "DELETE 1"
