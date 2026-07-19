"""Конфигурация бота: переменные окружения и общие константы."""
import os

# Пытаемся подхватить .env для локальной разработки.
# На Railway переменные окружения задаются в панели проекта, python-dotenv там не нужен.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "gtd.db")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Укажите его в переменных окружения или в файле .env"
    )

# Порог совпадения для нечёткого поиска (0-100, rapidfuzz)
FUZZY_SEARCH_THRESHOLD = 60
FUZZY_SEARCH_LIMIT = 10

# Через сколько секунд планировщик проверяет напоминания
REMINDER_CHECK_INTERVAL_SECONDS = 60

# За сколько времени до дедлайна напоминать
REMINDER_LEAD_TIME_MINUTES = 60

# Сколько задач показывать на одной странице списка
PAGE_SIZE = 6

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---------- OpenRouter / AI-функции ----------
# Не обязателен для старта бота (в отличие от BOT_TOKEN) — если не задан,
# AI-функции просто вернут пользователю понятное сообщение вместо падения.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Самая дешёвая на данный момент лёгкая модель Gemini на OpenRouter
# (дешевле даже новой линейки Gemini 3.x Flash Lite).
AI_MODEL_DEFAULT = os.getenv("AI_MODEL_DEFAULT", "google/gemini-2.5-flash-lite")
AI_MODEL_BREAKDOWN = os.getenv("AI_MODEL_BREAKDOWN", AI_MODEL_DEFAULT)
AI_MODEL_BALANCE = os.getenv("AI_MODEL_BALANCE", AI_MODEL_DEFAULT)
AI_MODEL_COACH = os.getenv("AI_MODEL_COACH", AI_MODEL_DEFAULT)

# Разбивка проекта — максимально предсказуемо, без фантазии
AI_TEMPERATURE_BREAKDOWN = float(os.getenv("AI_TEMPERATURE_BREAKDOWN", "0"))
# Анализ баланса — более свободный синтез наблюдений
AI_TEMPERATURE_BALANCE = float(os.getenv("AI_TEMPERATURE_BALANCE", "1"))
# Коуч — живой диалог
AI_TEMPERATURE_COACH = float(os.getenv("AI_TEMPERATURE_COACH", "0.8"))

AI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "30"))

# Сколько последних сообщений держать в истории чата с коучем
COACH_HISTORY_LIMIT = int(os.getenv("COACH_HISTORY_LIMIT", "10"))
