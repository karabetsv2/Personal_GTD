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
