"""
Сервис для экспорта сценариев в разные форматы
"""
import logging
import re
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def export_scenario_text(scenario_data: Dict) -> str:
    """
    Экспорт сценария в текстовый формат (.txt)
    
    Args:
        scenario_data: Словарь с данными сценария из БД
    
    Returns:
        str: Текст сценария в формате для экспорта
    """
    created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data.get('created_at') else "Дата неизвестна"
    
    text = "=" * 60 + "\n"
    text += "СЦЕНАРИЙ ДЛЯ ВИДЕО\n"
    text += "=" * 60 + "\n\n"
    
    text += f"ID сценария: #{scenario_data['id']}\n"
    text += f"Дата создания: {created_at}\n"
    text += f"Ниша: {scenario_data.get('niche') or 'Не указана'}\n"
    text += f"Формат: {scenario_data.get('format_type') or 'Не указан'}\n"
    text += f"Стиль: {scenario_data.get('style') or 'Не указан'}\n"
    
    if scenario_data.get('tone'):
        text += f"Тон: {scenario_data['tone']}\n"
    if scenario_data.get('duration'):
        text += f"Длительность: {scenario_data['duration']}\n"
    if scenario_data.get('platform'):
        platform_names = {
            "reels": "Instagram Reels",
            "tiktok": "TikTok",
            "shorts": "YouTube Shorts",
            "универсальный": "Универсальный"
        }
        platform_name = platform_names.get(scenario_data['platform'].lower(), scenario_data['platform'])
        text += f"Платформа: {platform_name}\n"
    if scenario_data.get('topic'):
        text += f"Тема: {scenario_data['topic']}\n"
    
    text += "\n" + "=" * 60 + "\n"
    text += "ТЕКСТ СЦЕНАРИЯ\n"
    text += "=" * 60 + "\n\n"
    text += scenario_data['scenario_text']
    
    return text


def export_scenario_shooting_list(scenario_data: Dict) -> str:
    """
    Экспорт сценария в формат съемочного листа
    
    Args:
        scenario_data: Словарь с данными сценария из БД
    
    Returns:
        str: Сценарий в формате съемочного листа
    """
    created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data.get('created_at') else "Дата неизвестна"
    
    text = "=" * 60 + "\n"
    text += "СЪЕМОЧНЫЙ ЛИСТ\n"
    text += "=" * 60 + "\n\n"
    
    text += f"Проект: {scenario_data.get('topic') or 'Без темы'}\n"
    text += f"Ниша: {scenario_data.get('niche') or 'Не указана'}\n"
    text += f"Дата создания: {created_at}\n"
    if scenario_data.get('platform'):
        platform_names = {
            "reels": "Instagram Reels",
            "tiktok": "TikTok",
            "shorts": "YouTube Shorts",
            "универсальный": "Универсальный"
        }
        platform_name = platform_names.get(scenario_data['platform'].lower(), scenario_data['platform'])
        text += f"Платформа: {platform_name}\n"
    text += "\n"
    
    # Парсим сценарий на кадры (упрощенный вариант)
    scenario_lines = scenario_data['scenario_text'].split('\n')
    
    text += "-" * 60 + "\n"
    text += "КАДРЫ\n"
    text += "-" * 60 + "\n\n"
    
    shot_num = 1
    current_section = ""
    
    for line in scenario_lines:
        line = line.strip()
        if not line:
            continue
        
        # Определяем секции
        if 'ХУК' in line.upper() or 'HOOK' in line.upper():
            current_section = "ХУК"
            text += f"\n[{current_section}]\n\n"
        elif 'РАЗВИТИЕ' in line.upper() or 'DEVELOPMENT' in line.upper():
            current_section = "РАЗВИТИЕ"
            text += f"\n[{current_section}]\n\n"
        elif 'КЛИФФХЭНГЕР' in line.upper() or 'CTA' in line.upper() or 'ПРИЗЫВ' in line.upper():
            current_section = "ФИНАЛ"
            text += f"\n[{current_section}]\n\n"
        else:
            # Обычная строка - добавляем как кадр
            text += f"Кадр {shot_num}:\n"
            text += f"  {line}\n\n"
            shot_num += 1
    
    text += "\n" + "=" * 60 + "\n"
    text += "ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ\n"
    text += "=" * 60 + "\n\n"
    text += scenario_data['scenario_text']
    
    return text


def export_scenario_table(scenario_data: Dict) -> str:
    """
    Экспорт сценария в табличный формат (для Excel или простой текст)
    
    Args:
        scenario_data: Словарь с данными сценария из БД
    
    Returns:
        str: Сценарий в табличном формате
    """
    created_at = scenario_data['created_at'].strftime("%d.%m.%Y %H:%M") if scenario_data.get('created_at') else "Дата неизвестна"
    
    text = "=" * 80 + "\n"
    text += "СЦЕНАРИЙ В ТАБЛИЧНОМ ФОРМАТЕ\n"
    text += "=" * 80 + "\n\n"
    
    text += f"Ниша: {scenario_data.get('niche') or 'Не указана'}\n"
    text += f"Тема: {scenario_data.get('topic') or 'Без темы'}\n"
    text += f"Дата: {created_at}\n\n"
    
    # Парсим сценарий на блоки (секции и кадры)
    scenario_text = scenario_data['scenario_text']
    scenario_lines = [line.strip() for line in scenario_text.split('\n') if line.strip()]
    
    text += f"{'№':<5} {'Время':<15} {'Описание':<58}\n"
    text += "-" * 80 + "\n"
    
    shot_num = 1
    current_time = ""
    current_description_parts = []
    
    i = 0
    while i < len(scenario_lines):
        line = scenario_lines[i]
        
        # Проверяем, является ли строка заголовком секции (все заглавные буквы)
        is_section_header = (line.isupper() or line == line.upper()) and len(line) > 3 and not re.search(r'\d+:\d+', line) and line in ["ХУК", "РАЗВИТИЕ", "КЛИФФХЭНГЕР", "CTA", "ДОПОЛНИТЕЛЬНО", "ПРИЗЫВ К ДЕЙСТВИЮ"]
        
        # Пытаемся извлечь время из строки
        time_match = re.search(r'(\d+:\d+(?:-\d+:\d+)?)', line)
        
        # Если нашли время - начинаем новый блок
        if time_match:
            # Сохраняем предыдущий блок, если он есть
            if current_description_parts:
                desc_text = ' | '.join(current_description_parts)
                if len(desc_text) > 58:
                    desc_text = desc_text[:55] + "..."
                text += f"{shot_num:<5} {current_time:<15} {desc_text:<58}\n"
                shot_num += 1
            
            # Устанавливаем новое время
            current_time = time_match.group(1)
            current_description_parts = []
            
            # Убираем время из строки
            line_clean = re.sub(r'\d+:\d+(?:-\d+:\d+)?\s*[-–—]?\s*', '', line).strip()
            if line_clean and not is_section_header:
                current_description_parts.append(line_clean)
            
            i += 1
            continue
        
        # Если это заголовок секции, добавляем его отдельной строкой
        if is_section_header:
            # Сохраняем предыдущий блок
            if current_description_parts:
                desc_text = ' | '.join(current_description_parts)
                if len(desc_text) > 58:
                    desc_text = desc_text[:55] + "..."
                text += f"{shot_num:<5} {current_time:<15} {desc_text:<58}\n"
                shot_num += 1
                current_description_parts = []
            
            # Добавляем заголовок секции
            desc_text = line
            if len(desc_text) > 58:
                desc_text = desc_text[:55] + "..."
            text += f"{shot_num:<5} {'':<15} {desc_text:<58}\n"
            shot_num += 1
            i += 1
            continue
        
        # Обычная строка - добавляем к текущему описанию
        # Если строка начинается с метки (Текст:, В кадре: и т.д.), объединяем её с содержимым
        if ':' in line:
            parts = line.split(':', 1)
            label = parts[0].strip()
            content = parts[1].strip() if len(parts) > 1 else ""
            
            # Метки, которые нужно объединить
            if label in ["Текст", "В кадре", "Реквизиты", "Локация", "Освещение", "Движения камеры", "Действия"]:
                if content:
                    current_description_parts.append(f"{label}: {content}")
                else:
                    current_description_parts.append(line)
            else:
                current_description_parts.append(line)
        else:
            # Обычная строка без метки - добавляем как есть
            current_description_parts.append(line)
        
        i += 1
    
    # Сохраняем последний блок
    if current_description_parts:
        desc_text = ' | '.join(current_description_parts)
        if len(desc_text) > 58:
            desc_text = desc_text[:55] + "..."
        text += f"{shot_num:<5} {current_time:<15} {desc_text:<58}\n"
    
    text += "\n" + "=" * 80 + "\n"
    text += "ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ\n"
    text += "=" * 80 + "\n\n"
    text += scenario_data['scenario_text']
    
    return text

