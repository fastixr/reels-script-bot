"""
Система приоритетной очереди для генерации сценариев
Premium пользователи получают приоритет в обработке
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Any, Awaitable, Optional
from enum import IntEnum

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Приоритеты обработки"""
    PREMIUM = 1  # Premium пользователи - высший приоритет
    FREE = 2     # Бесплатные пользователи - обычный приоритет


@dataclass
class GenerationTask:
    """Задача на генерацию сценария"""
    user_id: int
    priority: Priority
    task_id: str
    generator_func: Callable[[], str]
    callback: Callable[[str], Awaitable[None]]
    created_at: datetime = field(default_factory=datetime.now)
    
    def __lt__(self, other):
        """Сравнение для сортировки по приоритету и времени создания"""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


class PriorityQueue:
    """Приоритетная очередь для генерации сценариев"""
    
    def __init__(self, max_workers: int = 3):
        """
        Инициализация очереди
        
        Args:
            max_workers: Максимальное количество параллельных генераций
        """
        self.max_workers = max_workers
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.running_tasks: set = set()
        self.task_counter = 0
        self._worker_tasks: list = []
        self._running = False
    
    async def start(self):
        """Запуск обработчиков очереди"""
        if self._running:
            return
        
        self._running = True
        # Запускаем воркеры
        for i in range(self.max_workers):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self._worker_tasks.append(task)
        logger.info(f"Приоритетная очередь запущена с {self.max_workers} воркерами")
    
    async def stop(self):
        """Остановка обработчиков очереди"""
        self._running = False
        # Ждем завершения всех воркеров
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        logger.info("Приоритетная очередь остановлена")
    
    async def add_task(
        self,
        user_id: int,
        is_premium: bool,
        generator_func: Callable[[], str],
        callback: Callable[[str], Awaitable[None]]
    ) -> str:
        """
        Добавить задачу в очередь
        
        Args:
            user_id: ID пользователя
            is_premium: Является ли пользователь Premium
            generator_func: Функция генерации сценария (синхронная)
            callback: Асинхронная функция для обработки результата
        
        Returns:
            str: ID задачи
        """
        self.task_counter += 1
        task_id = f"task-{self.task_counter}-{user_id}"
        
        priority = Priority.PREMIUM if is_premium else Priority.FREE
        
        task = GenerationTask(
            user_id=user_id,
            priority=priority,
            task_id=task_id,
            generator_func=generator_func,
            callback=callback,
            created_at=datetime.now()
        )
        
        await self.queue.put((priority.value, task.created_at.timestamp(), task))
        logger.debug(f"Задача {task_id} добавлена в очередь (приоритет: {priority.name}, пользователь: {user_id})")
        
        return task_id
    
    async def _worker(self, worker_name: str):
        """Воркер для обработки задач из очереди"""
        logger.debug(f"Воркер {worker_name} запущен")
        
        while self._running:
            try:
                # Получаем задачу из очереди с таймаутом
                try:
                    _, _, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                if task.task_id in self.running_tasks:
                    continue
                
                self.running_tasks.add(task.task_id)
                logger.debug(f"Воркер {worker_name} обрабатывает задачу {task.task_id} (приоритет: {task.priority.name})")
                
                try:
                    # Выполняем генерацию в отдельном потоке
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, task.generator_func)
                    
                    # Вызываем callback с результатом
                    await task.callback(result)
                    
                    logger.debug(f"Задача {task.task_id} успешно выполнена воркером {worker_name}")
                except Exception as e:
                    logger.error(f"Ошибка при выполнении задачи {task.task_id} воркером {worker_name}: {e}", exc_info=True)
                    # Вызываем callback с ошибкой
                    try:
                        await task.callback(f"❌ <b>Ошибка при генерации сценария</b>\n\nПроизошла неожиданная ошибка. Попробуйте позже.\n\nДетали: {str(e)[:200]}")
                    except:
                        pass
                finally:
                    self.running_tasks.discard(task.task_id)
                    self.queue.task_done()
                    
            except Exception as e:
                logger.error(f"Критическая ошибка в воркере {worker_name}: {e}", exc_info=True)
                await asyncio.sleep(1)  # Небольшая задержка перед следующей попыткой
        
        logger.debug(f"Воркер {worker_name} остановлен")
    
    def get_queue_size(self) -> int:
        """Получить размер очереди"""
        return self.queue.qsize()
    
    def get_running_tasks_count(self) -> int:
        """Получить количество выполняемых задач"""
        return len(self.running_tasks)


# Глобальный экземпляр очереди
priority_queue: Optional[PriorityQueue] = None

