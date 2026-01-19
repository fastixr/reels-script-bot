"""
Сервис для работы с Robokassa API
Документация: https://docs.robokassa.ru/
"""
import hashlib
import json
import logging
from urllib.parse import urlencode, quote
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class RobokassaService:
    """Сервис для работы с Robokassa API"""
    
    # URL для тестового режима
    TEST_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
    # URL для боевого режима
    PROD_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
    
    def __init__(
        self,
        merchant_login: str,
        password1: str,
        password2: str,
        is_test: bool = True
    ):
        """
        Инициализация сервиса Robokassa
        
        Args:
            merchant_login: Идентификатор магазина (Merchant Login)
            password1: Пароль #1 (для создания ссылок на оплату)
            password2: Пароль #2 (для проверки уведомлений)
            is_test: Режим работы (True - тестовый, False - боевой)
        """
        self.merchant_login = merchant_login
        self.password1 = password1
        self.password2 = password2
        self.is_test = is_test
        self.base_url = self.TEST_URL if is_test else self.PROD_URL
    
    def generate_payment_url(
        self,
        out_sum: float,
        inv_id: int,
        description: str,
        user_id: Optional[int] = None,
        email: Optional[str] = None,
        receipt: Optional[Dict] = None,
        culture: str = "ru",
        encoding: str = "utf-8",
        **kwargs
    ) -> str:
        """
        Генерация ссылки на оплату
        
        Args:
            out_sum: Сумма платежа
            inv_id: Номер счета (уникальный ID заказа)
            description: Описание платежа
            user_id: ID пользователя Telegram (для Shp_userId)
            email: Email покупателя (опционально)
            receipt: Данные для фискализации (Receipt в формате JSON)
            culture: Язык интерфейса (ru/en)
            encoding: Кодировка (utf-8)
            **kwargs: Дополнительные параметры
        
        Returns:
            str: URL для оплаты
        """
        # Формируем параметры
        # ВАЖНО: OutSum должен быть в формате с двумя знаками после запятой (например, "30.00")
        # Это формат, который ожидает Robokassa, и он должен совпадать с форматом в подписи
        out_sum_formatted = f"{float(out_sum):.2f}"
        
        params = {
            "MerchantLogin": self.merchant_login,
            "OutSum": out_sum_formatted,  # Используем отформатированную сумму
            "InvId": inv_id,
            "Description": description,
            "Culture": culture,
            "Encoding": encoding,
        }
        
        # Добавляем email если указан
        if email:
            params["Email"] = email
        
        # Добавляем Receipt для фискализации (Робочеки), если указан
        receipt_str = None
        receipt_encoded = None
        if receipt:
            # Receipt должен быть в формате JSON строки БЕЗ пробелов
            # Важно: использовать separators=(',', ':') для компактного формата
            receipt_str = json.dumps(receipt, ensure_ascii=False, separators=(',', ':'))
            # Убираем все пробелы для точного соответствия требованиям
            receipt_str = receipt_str.replace(' ', '')
            # ВАЖНО: Receipt должен быть URL-кодирован перед включением в подпись и отправкой
            # Согласно документации: https://docs.robokassa.ru/fiscalization/
            receipt_encoded = quote(receipt_str, safe='')
            params["Receipt"] = receipt_encoded
        
        # Добавляем Shp_userId если указан user_id
        shp_params = {}
        if user_id is not None:
            shp_params["Shp_userId"] = user_id
        
        # Добавляем дополнительные параметры
        params.update(kwargs)
        params.update(shp_params)
        
        # Формируем подпись (SignatureValue)
        # ВАЖНО: Порядок параметров согласно документации: MerchantLogin:OutSum:InvId:Receipt:Пароль#1
        # Receipt должен быть ПЕРЕД Password#1, а не после!
        # Согласно документации: https://docs.robokassa.ru/fiscalization/
        # "Параметр включается в контрольную подпись запроса после номера счета магазина.
        #  Например: MerchantLogin:OutSum:InvId:Receipt:Пароль#1"
        # ВАЖНО: Receipt должен быть URL-кодирован перед включением в подпись!
        # Shp_параметры должны быть отсортированы по алфавиту (по ключу)
        # ВАЖНО: сумма в подписи должна быть в ТОЧНО ТАКОМ ЖЕ формате, что и в параметре OutSum
        signature_parts = [self.merchant_login, out_sum_formatted, str(inv_id)]
        
        # Добавляем Receipt в подпись ПЕРЕД Password#1 (если он указан)
        if receipt_encoded:
            signature_parts.append(receipt_encoded)
        
        # Добавляем Password#1 ПОСЛЕ Receipt
        signature_parts.append(self.password1)
        
        # Добавляем Shp_параметры в отсортированном порядке (если есть)
        if shp_params:
            sorted_shp = sorted(shp_params.items())  # Сортировка по ключу
            for key, value in sorted_shp:
                signature_parts.append(f"{key}={value}")
        
        signature_string = ":".join(signature_parts)
        signature = hashlib.md5(signature_string.encode(encoding)).hexdigest().upper()
        params["SignatureValue"] = signature
        
        # Подробное логирование для отладки
        logger.info(f"🔐 Формирование подписи для ссылки оплаты:")
        logger.info(f"   MerchantLogin: {self.merchant_login}")
        logger.info(f"   OutSum: {out_sum_formatted} (исходное значение: {out_sum})")
        logger.info(f"   InvId: {inv_id}")
        logger.info(f"   Password#1: {self.password1[:5]}... (первые 5 символов, длина: {len(self.password1)})")
        logger.info(f"   Тестовый режим: {self.is_test}")
        if receipt_str:
            logger.info(f"   Receipt (JSON): {receipt_str}")
            logger.info(f"   Receipt длина: {len(receipt_str)} символов")
        logger.info(f"   Shp_params: {shp_params}")
        logger.info(f"   Порядок параметров в подписи:")
        logger.info(f"     1. MerchantLogin: {self.merchant_login}")
        logger.info(f"     2. OutSum: {out_sum_formatted}")
        logger.info(f"     3. InvId: {inv_id}")
        logger.info(f"     4. Password#1: {self.password1[:5]}...")
        if receipt_str:
            logger.info(f"     5. Receipt: {receipt_str[:100]}... (первые 100 символов)")
        if shp_params:
            logger.info(f"     6. Shp_params: {shp_params}")
        logger.info(f"   Полная строка для подписи: {signature_string}")
        logger.info(f"   Подпись (MD5, upper): {signature}")
        
        # Если тестовый режим, добавляем IsTest=1
        if self.is_test:
            params["IsTest"] = 1
        
        # Формируем URL
        url = f"{self.base_url}?{urlencode(params)}"
        
        logger.info(f"Сгенерирована ссылка на оплату Robokassa: inv_id={inv_id}, сумма={out_sum}₽, user_id={user_id}")
        logger.debug(f"Подпись для ссылки: {signature_string} -> {signature}")
        
        return url
    
    def verify_result_notification(
        self,
        out_sum: float,
        inv_id: int,
        signature: str,
        **kwargs
    ) -> bool:
        """
        Проверка подписи уведомления от Robokassa (Result URL)
        
        Args:
            out_sum: Сумма платежа (может быть строкой с запятой или точкой)
            inv_id: Номер счета
            signature: Подпись от Robokassa
            **kwargs: Дополнительные параметры (включая Shp_*)
        
        Returns:
            bool: True если подпись верна, False иначе
        """
        # Нормализуем сумму (Robokassa может отправлять с запятой)
        if isinstance(out_sum, str):
            out_sum_clean = out_sum.replace(",", ".")
        else:
            out_sum_clean = str(out_sum)
        
        # Преобразуем в float для проверки
        out_sum_float = float(out_sum_clean)
        
        # Robokassa для Result URL использует формат суммы БЕЗ лишних нулей для целых чисел
        # Например, 30.0 становится "30", а 30.50 остается "30.5" или "30.50"
        # Но лучше использовать исходный формат, который отправил Robokassa
        # Попробуем несколько вариантов:
        # 1. Исходный формат (как пришел от Robokassa)
        # 2. Без .0 для целых чисел
        # 3. С двумя знаками после запятой
        
        if out_sum_float.is_integer():
            # Для целых чисел пробуем оба варианта
            possible_sums = [
                str(int(out_sum_float)),  # "30" - без десятичной части
                out_sum_clean,  # Исходный формат
                f"{out_sum_float:.2f}",  # "30.00"
            ]
        else:
            # Для дробных чисел пробуем исходный формат и с двумя знаками
            possible_sums = [
                out_sum_clean,  # Исходный формат
                f"{out_sum_float:.2f}",  # С двумя знаками
                f"{out_sum_float:g}",  # Без лишних нулей
            ]
        
        # Извлекаем и сортируем Shp_параметры (как строки)
        shp_params = {k: str(v) for k, v in kwargs.items() if k.startswith("Shp_")}
        
        # Пробуем разные варианты формата суммы
        for out_sum_str in possible_sums:
            # Формируем строку для проверки подписи
            # Формат: OutSum:InvId:Password#2:Shp_key=value
            # Shp_параметры должны быть отсортированы по алфавиту (по ключу)
            signature_parts = [out_sum_str, str(inv_id), self.password2]
            
            if shp_params:
                sorted_shp = sorted(shp_params.items())  # Сортировка по ключу
                for key, value in sorted_shp:
                    signature_parts.append(f"{key}={value}")
            
            signature_string = ":".join(signature_parts)
            calculated_signature = hashlib.md5(signature_string.encode("utf-8")).hexdigest().upper()
            
            logger.debug(f"Проверка подписи: сумма={out_sum_str}, строка={signature_string}, подпись={calculated_signature}")
            
            if calculated_signature == signature.upper():
                logger.info(f"✅ Подпись совпала! Формат суммы: {out_sum_str}")
                return True
        
        # Если ни один вариант не подошел, логируем детали
        logger.error(
            f"❌ Неверная подпись уведомления Robokassa: "
            f"inv_id={inv_id}, ожидалось (последний вариант)={calculated_signature}, получено={signature}"
        )
        logger.error(f"Пробовали форматы суммы: {possible_sums}")
        logger.error(f"Последняя строка для подписи: {signature_string}")
        logger.error(f"Параметры: out_sum_raw={out_sum}, inv_id={inv_id}, shp_params={shp_params}")
        logger.error(f"Password2 используется: {self.password2[:5]}... (первые 5 символов)")
        
        return False
    
    def verify_success_redirect(
        self,
        out_sum: float,
        inv_id: int,
        signature: str,
        **kwargs
    ) -> bool:
        """
        Проверка подписи при редиректе на Success URL
        
        Args:
            out_sum: Сумма платежа
            inv_id: Номер счета
            signature: Подпись от Robokassa
            **kwargs: Дополнительные параметры
        
        Returns:
            bool: True если подпись верна, False иначе
        """
        # Формируем строку для проверки подписи
        # Формат: OutSum:InvId:Password#1
        signature_string = f"{out_sum}:{inv_id}:{self.password1}"
        calculated_signature = hashlib.md5(signature_string.encode("utf-8")).hexdigest().upper()
        
        is_valid = calculated_signature == signature.upper()
        
        if not is_valid:
            logger.warning(
                f"Неверная подпись редиректа Robokassa: "
                f"inv_id={inv_id}, ожидалось={calculated_signature}, получено={signature}"
            )
        
        return is_valid

