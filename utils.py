"""Вспомогательные функции: работа с датами/временем, разбор текста, нечёткий поиск."""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process

import config

TZ = ZoneInfo(config.TIMEZONE)
UTC = ZoneInfo("UTC")

WEEKDAY_NAMES_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

STATUS_LABELS = {
    "inbox": "📥 Inbox",
    "next_action": "▶️ Следующее действие",
    "waiting_for": "⏳ Ожидание",
    "calendar": "📅 Календарь",
    "someday": "🌫 Когда-нибудь",
    "done": "✅ Выполнено",
}

PRIORITY_LABELS = {"A": "🔴A", "B": "🟡B", "C": "🟢C"}


# ---------- Время ----------

def now_local() -> datetime:
    """Текущее время в локальном часовом поясе бота."""
    return datetime.now(TZ)


def to_utc(dt: datetime) -> datetime:
    """Приводит datetime (с tz или наивный локальный) к UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(UTC)


def to_local(dt: datetime) -> datetime:
    """Приводит datetime (с tz или наивный UTC) к локальному часовому поясу."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TZ)


def dt_to_iso(dt: datetime) -> str:
    """Сериализация datetime в ISO8601 UTC для хранения в БД."""
    return to_utc(dt).isoformat()


def dt_from_iso(value: str) -> datetime:
    """Разбор ISO8601-строки из БД обратно в datetime с tz."""
    return datetime.fromisoformat(value)


def _day_label(d: date) -> str:
    today = now_local().date()
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return f"{d.day:02d}.{d.month:02d}.{d.year} ({WEEKDAY_NAMES_RU[d.weekday()]})"


def format_dt(value) -> str:
    """Человекочитаемое представление даты/времени на русском в локальном поясе."""
    dt = to_local(dt_from_iso(value)) if isinstance(value, str) else to_local(value)
    return f"{_day_label(dt.date())} {dt.hour:02d}:{dt.minute:02d}"


def format_date(d: date) -> str:
    """Человекочитаемое представление одной даты (без времени) на русском."""
    return _day_label(d)


# ---------- Разбор дат из свободного текста ----------

@dataclass
class ParsedDate:
    dt: datetime          # локальное время, tz-aware
    matched_text: str      # что именно распознали, для показа пользователю
    has_time: bool         # было ли явно указано время


_DATE_KEYWORDS = [
    ("послезавтра", 2),
    ("завтра", 1),
    ("сегодня", 0),
]

_WEEKDAY_WORDS = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2, "среда": 2,
    "четверг": 3,
    "пятницу": 4, "пятница": 4,
    "субботу": 5, "суббота": 5,
    "воскресенье": 6,
}

_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
_TIME_COLON_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME_HOUR_RE = re.compile(r"\bв\s+(\d{1,2})\s*(?:час\w*)?\b")


def parse_date_hints(text: str) -> Optional[ParsedDate]:
    """Ищет в тексте упоминание даты/времени (простые ключевые слова и форматы).

    Понимает: сегодня/завтра/послезавтра, дни недели ("в пятницу" — ближайшая
    будущая), даты ДД.ММ или ДД.ММ.ГГГГ, время "10:00" или "в 10".
    Если дата найдена, а время — нет, подставляется 09:00 по умолчанию
    (has_time=False, чтобы вызывающий код мог это показать пользователю).
    """
    lower = text.lower()
    today = now_local().date()
    found_date = None
    matched_parts = []

    for word, offset in _DATE_KEYWORDS:
        if re.search(rf"\b{word}\b", lower):
            found_date = today + timedelta(days=offset)
            matched_parts.append(word)
            break

    if found_date is None:
        for word, weekday in _WEEKDAY_WORDS.items():
            if re.search(rf"\b{word}\b", lower):
                days_ahead = (weekday - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                found_date = today + timedelta(days=days_ahead)
                matched_parts.append(word)
                break

    if found_date is None:
        m = _DATE_NUMERIC_RE.search(lower)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = today.year
            try:
                if m.group(3):
                    year = int(m.group(3))
                    if year < 100:
                        year += 2000
                    candidate = date(year, month, day)
                else:
                    candidate = date(year, month, day)
                    if candidate < today:
                        candidate = date(year + 1, month, day)
                found_date = candidate
                matched_parts.append(m.group(0))
            except ValueError:
                pass

    if found_date is None:
        return None

    found_time = None
    m = _TIME_COLON_RE.search(lower)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found_time = time(hour, minute)
            matched_parts.append(m.group(0))
    if found_time is None:
        m = _TIME_HOUR_RE.search(lower)
        if m:
            hour = int(m.group(1))
            if 0 <= hour <= 23:
                found_time = time(hour, 0)
                matched_parts.append(m.group(0))

    has_time = found_time is not None
    dt = datetime.combine(found_date, found_time or time(9, 0), tzinfo=TZ)
    return ParsedDate(dt=dt, matched_text=" ".join(matched_parts), has_time=has_time)


# ---------- Нечёткий поиск ----------

def fuzzy_search(query: str, tasks: list[dict]) -> list[dict]:
    """Нечёткий поиск задач по названию и описанию (опечатки, регистр, окончания)."""
    if not query or not tasks:
        return []
    choices = {}
    for t in tasks:
        haystack = t["title"]
        if t.get("description"):
            haystack += " " + t["description"]
        choices[t["id"]] = haystack
    results = process.extract(
        query,
        choices,
        scorer=fuzz.WRatio,
        limit=config.FUZZY_SEARCH_LIMIT,
        score_cutoff=config.FUZZY_SEARCH_THRESHOLD,
    )
    id_to_task = {t["id"]: t for t in tasks}
    return [id_to_task[task_id] for _, _score, task_id in results]


# ---------- Форматирование задач для показа ----------

def format_task_line(task: dict, contexts: Optional[list[dict]] = None) -> str:
    """Одна строка со сводкой задачи: приоритет, название, дедлайн, контексты."""
    parts = []
    if task.get("priority"):
        parts.append(PRIORITY_LABELS[task["priority"]])
    parts.append(task["title"])
    line = " ".join(parts)
    extras = []
    if task.get("deadline"):
        extras.append(format_dt(task["deadline"]))
    if contexts:
        extras.append(" ".join(f"@{c['name']}" for c in contexts))
    if extras:
        line += "\n    " + " · ".join(extras)
    return line
