"""Слой доступа к данным: SQLite через aiosqlite, схема, сиды, CRUD."""
import logging
import os
from datetime import timedelta
from typing import Optional

import aiosqlite

import config
import utils

logger = logging.getLogger(__name__)

_conn: Optional[aiosqlite.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('place', 'time', 'custom')),
    minutes INTEGER,
    location_group TEXT CHECK(location_group IN ('home', 'work', 'out')),
    is_builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    is_archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'inbox'
        CHECK(status IN ('inbox', 'next_action', 'waiting_for', 'calendar', 'someday', 'done')),
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    priority TEXT CHECK(priority IN ('A', 'B', 'C')),
    deadline TEXT,
    waiting_for_whom TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    status_changed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_contexts (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    context_id INTEGER NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, context_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    remind_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at);
"""

BUILTIN_CONTEXTS = [
    # name, type, minutes, location_group
    ("Работа", "place", None, "work"),
    ("Дом", "place", None, "home"),
    ("Звонки", "place", None, None),
    ("Компьютер", "place", None, None),
    ("Магазин", "place", None, "out"),
    ("5мин", "time", 5, None),
    ("15мин", "time", 15, None),
    ("30мин", "time", 30, None),
    ("60мин", "time", 60, None),
    ("120мин", "time", 120, None),
]


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


async def init_db() -> None:
    """Открывает соединение, создаёт схему и сеет базовые контексты (если их ещё нет)."""
    global _conn
    db_dir = os.path.dirname(config.DATABASE_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError:
            logger.exception(
                "Не удалось создать директорию для БД %s — проверь права доступа "
                "(например, к смонтированному Railway Volume)",
                db_dir,
            )
            raise
    if os.path.exists(config.DATABASE_PATH):
        size = os.path.getsize(config.DATABASE_PATH)
        logger.info("Найден существующий файл БД %s (%d байт)", config.DATABASE_PATH, size)
    else:
        logger.info("Файл БД %s не найден, будет создан с нуля", config.DATABASE_PATH)

    _conn = await aiosqlite.connect(config.DATABASE_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA foreign_keys = ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()

    # Лёгкие additive-миграции для БД, развёрнутых до появления этих колонок.
    # ALTER TABLE ADD COLUMN не трогает существующие строки — безопасно для прод-данных.
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN status_changed_at TEXT",
        "ALTER TABLE projects ADD COLUMN description TEXT",
    ):
        try:
            await _conn.execute(ddl)
        except aiosqlite.OperationalError:
            pass  # колонка уже есть
    await _conn.execute("UPDATE tasks SET status_changed_at = created_at WHERE status_changed_at IS NULL")
    await _conn.commit()

    cursor = await _conn.execute("SELECT COUNT(*) AS c FROM contexts WHERE is_builtin = 1")
    row = await cursor.fetchone()
    if row["c"] == 0:
        for name, ctx_type, minutes, location_group in BUILTIN_CONTEXTS:
            await _conn.execute(
                "INSERT OR IGNORE INTO contexts (name, type, minutes, location_group, is_builtin) "
                "VALUES (?, ?, ?, ?, 1)",
                (name, ctx_type, minutes, location_group),
            )
        await _conn.commit()
    logger.info("База данных инициализирована: %s", config.DATABASE_PATH)


async def close_db() -> None:
    if _conn is not None:
        await _conn.close()


# ---------- settings ----------

async def get_setting(key: str) -> Optional[str]:
    cursor = await _conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    await _conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await _conn.commit()


async def get_owner_chat_id() -> Optional[int]:
    value = await get_setting("owner_chat_id")
    return int(value) if value else None


async def set_owner_chat_id(chat_id: int) -> None:
    await set_setting("owner_chat_id", str(chat_id))


# ---------- tasks ----------

async def add_task(
    title: str, description: str = None, status: str = "inbox", project_id: int = None
) -> int:
    now = utils.dt_to_iso(utils.now_local())
    cursor = await _conn.execute(
        "INSERT INTO tasks (title, description, status, project_id, created_at, updated_at, status_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, description, status, project_id, now, now, now),
    )
    await _conn.commit()
    return cursor.lastrowid


async def get_task(task_id: int) -> Optional[dict]:
    cursor = await _conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


_UPDATABLE_FIELDS = {
    "title", "description", "status", "priority", "deadline",
    "waiting_for_whom", "project_id",
}


async def update_task(task_id: int, **fields) -> None:
    """Обновляет произвольные поля задачи. Неизвестные ключи игнорируются."""
    changes = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not changes:
        return
    changes["updated_at"] = utils.dt_to_iso(utils.now_local())
    if "status" in changes:
        changes["status_changed_at"] = changes["updated_at"]
        if changes["status"] == "done":
            changes["completed_at"] = changes["updated_at"]
    set_clause = ", ".join(f"{k} = ?" for k in changes)
    values = list(changes.values()) + [task_id]
    await _conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    await _conn.commit()


async def delete_task(task_id: int) -> None:
    await _conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await _conn.commit()


async def list_tasks_by_status(status: str, order_by: str = "created_at ASC") -> list[dict]:
    cursor = await _conn.execute(
        f"SELECT * FROM tasks WHERE status = ? ORDER BY {order_by}", (status,)
    )
    return _rows_to_dicts(await cursor.fetchall())


async def list_inbox_tasks() -> list[dict]:
    return await list_tasks_by_status("inbox", "created_at ASC")


async def list_open_tasks() -> list[dict]:
    """Незавершённые задачи (next_action + calendar) для /do, отсортированные по приоритету."""
    cursor = await _conn.execute(
        "SELECT * FROM tasks WHERE status IN ('next_action', 'calendar') "
        "ORDER BY CASE priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END, "
        "COALESCE(deadline, '9999') ASC, created_at ASC"
    )
    return _rows_to_dicts(await cursor.fetchall())


async def list_all_tasks_excluding_done() -> list[dict]:
    cursor = await _conn.execute("SELECT * FROM tasks WHERE status != 'done' ORDER BY created_at DESC")
    return _rows_to_dicts(await cursor.fetchall())


async def list_done_tasks_between(start_iso: str, end_iso: str) -> list[dict]:
    cursor = await _conn.execute(
        "SELECT * FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ? "
        "ORDER BY completed_at DESC",
        (start_iso, end_iso),
    )
    return _rows_to_dicts(await cursor.fetchall())


async def list_done_tasks_all() -> list[dict]:
    cursor = await _conn.execute("SELECT * FROM tasks WHERE status = 'done' ORDER BY completed_at DESC")
    return _rows_to_dicts(await cursor.fetchall())


async def list_next_actions_for_now(max_minutes: int, location_groups: Optional[list[str]]) -> list[dict]:
    """Next Actions, подходящие по времени (контекст с minutes <= max_minutes) и месту.

    location_groups=None означает «где угодно» — фильтр по месту не применяется.
    Иначе задача подходит, если её контекст-место входит в location_groups
    ИЛИ у задачи вовсе нет контекста-места с заданной группой (universal-контексты).
    """
    query = """
        SELECT DISTINCT t.* FROM tasks t
        JOIN task_contexts tc_time ON tc_time.task_id = t.id
        JOIN contexts c_time ON c_time.id = tc_time.context_id AND c_time.type = 'time'
        WHERE t.status = 'next_action' AND c_time.minutes <= ?
    """
    params: list = [max_minutes]

    if location_groups is not None:
        placeholders = ",".join("?" for _ in location_groups)
        query += f"""
            AND (
                NOT EXISTS (
                    SELECT 1 FROM task_contexts tc_p
                    JOIN contexts c_p ON c_p.id = tc_p.context_id AND c_p.type = 'place'
                    WHERE tc_p.task_id = t.id AND c_p.location_group IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM task_contexts tc_p
                    JOIN contexts c_p ON c_p.id = tc_p.context_id AND c_p.type = 'place'
                    WHERE tc_p.task_id = t.id AND c_p.location_group IN ({placeholders})
                )
            )
        """
        params.extend(location_groups)

    query += """
        ORDER BY CASE t.priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END,
                 COALESCE(t.deadline, '9999') ASC, t.created_at ASC
    """
    cursor = await _conn.execute(query, params)
    return _rows_to_dicts(await cursor.fetchall())


async def list_tasks_with_deadline_between(start_iso: str, end_iso: str) -> list[dict]:
    cursor = await _conn.execute(
        "SELECT * FROM tasks WHERE deadline IS NOT NULL AND deadline >= ? AND deadline < ? "
        "AND status != 'done' ORDER BY deadline ASC",
        (start_iso, end_iso),
    )
    return _rows_to_dicts(await cursor.fetchall())


# ---------- contexts ----------

async def list_contexts(ctx_type: Optional[str] = None) -> list[dict]:
    if ctx_type:
        cursor = await _conn.execute(
            "SELECT * FROM contexts WHERE type = ? ORDER BY is_builtin DESC, name ASC", (ctx_type,)
        )
    else:
        cursor = await _conn.execute("SELECT * FROM contexts ORDER BY type ASC, is_builtin DESC, name ASC")
    return _rows_to_dicts(await cursor.fetchall())


async def get_context(context_id: int) -> Optional[dict]:
    cursor = await _conn.execute("SELECT * FROM contexts WHERE id = ?", (context_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def get_context_by_name(name: str) -> Optional[dict]:
    cursor = await _conn.execute("SELECT * FROM contexts WHERE name = ?", (name,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def get_or_create_context(name: str, ctx_type: str = "custom") -> dict:
    existing = await get_context_by_name(name)
    if existing:
        return existing
    cursor = await _conn.execute(
        "INSERT INTO contexts (name, type, is_builtin) VALUES (?, ?, 0)", (name, ctx_type)
    )
    await _conn.commit()
    return await get_context(cursor.lastrowid)


async def get_task_contexts(task_id: int) -> list[dict]:
    cursor = await _conn.execute(
        "SELECT c.* FROM contexts c "
        "JOIN task_contexts tc ON tc.context_id = c.id "
        "WHERE tc.task_id = ? ORDER BY c.type ASC, c.name ASC",
        (task_id,),
    )
    return _rows_to_dicts(await cursor.fetchall())


async def set_task_contexts(task_id: int, context_ids: list[int]) -> None:
    """Полностью заменяет набор контекстов задачи."""
    await _conn.execute("DELETE FROM task_contexts WHERE task_id = ?", (task_id,))
    for context_id in context_ids:
        await _conn.execute(
            "INSERT OR IGNORE INTO task_contexts (task_id, context_id) VALUES (?, ?)",
            (task_id, context_id),
        )
    await _conn.commit()


# ---------- reminders ----------

async def get_reminder_for_task(task_id: int) -> Optional[dict]:
    cursor = await _conn.execute(
        "SELECT * FROM reminders WHERE task_id = ? AND sent = 0 ORDER BY remind_at ASC LIMIT 1",
        (task_id,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def delete_reminders_for_task(task_id: int) -> None:
    await _conn.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    await _conn.commit()


async def sync_reminder_for_task(task_id: int, deadline_iso: Optional[str]) -> None:
    """Пересоздаёт напоминание (дедлайн минус REMINDER_LEAD_TIME_MINUTES) при изменении дедлайна.

    Если дедлайн снят или уже прошло время напоминания — активных напоминаний не остаётся.
    """
    await _conn.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    if deadline_iso:
        deadline_dt = utils.dt_from_iso(deadline_iso)
        remind_dt = deadline_dt - timedelta(minutes=config.REMINDER_LEAD_TIME_MINUTES)
        if remind_dt > utils.now_local():
            now = utils.dt_to_iso(utils.now_local())
            await _conn.execute(
                "INSERT INTO reminders (task_id, remind_at, sent, created_at) VALUES (?, ?, 0, ?)",
                (task_id, utils.dt_to_iso(remind_dt), now),
            )
    await _conn.commit()


async def get_due_reminders(now_iso: str) -> list[dict]:
    cursor = await _conn.execute(
        "SELECT r.*, t.title AS task_title FROM reminders r "
        "JOIN tasks t ON t.id = r.task_id "
        "WHERE r.sent = 0 AND r.remind_at <= ?",
        (now_iso,),
    )
    return _rows_to_dicts(await cursor.fetchall())


async def mark_reminder_sent(reminder_id: int) -> None:
    await _conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    await _conn.commit()


# ---------- проекты ----------

async def create_project(name: str, description: Optional[str] = None) -> int:
    now = utils.dt_to_iso(utils.now_local())
    cursor = await _conn.execute(
        "INSERT INTO projects (name, description, created_at) VALUES (?, ?, ?)",
        (name, description, now),
    )
    await _conn.commit()
    return cursor.lastrowid


async def get_project(project_id: int) -> Optional[dict]:
    cursor = await _conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


# ---------- статистика для /balance и коуча ----------

async def get_context_stats_since(start_iso: str) -> list[dict]:
    """Сколько задач с каждым контекстом создано и сколько выполнено за период."""
    cursor = await _conn.execute(
        """
        SELECT c.name AS name,
               COUNT(DISTINCT CASE WHEN t.created_at >= ? THEN t.id END) AS created,
               COUNT(DISTINCT CASE WHEN t.status = 'done' AND t.completed_at >= ? THEN t.id END) AS done
        FROM contexts c
        JOIN task_contexts tc ON tc.context_id = c.id
        JOIN tasks t ON t.id = tc.task_id
        GROUP BY c.id
        HAVING created > 0 OR done > 0
        ORDER BY created DESC
        """,
        (start_iso, start_iso),
    )
    return _rows_to_dicts(await cursor.fetchall())


async def list_stale_waiting_for(threshold_iso: str) -> list[dict]:
    """Задачи в Waiting For, не менявшие статус с threshold_iso или раньше."""
    cursor = await _conn.execute(
        "SELECT * FROM tasks WHERE status = 'waiting_for' AND status_changed_at <= ? "
        "ORDER BY status_changed_at ASC",
        (threshold_iso,),
    )
    return _rows_to_dicts(await cursor.fetchall())


async def list_overdue_tasks() -> list[dict]:
    now_iso = utils.dt_to_iso(utils.now_local())
    cursor = await _conn.execute(
        "SELECT * FROM tasks WHERE deadline IS NOT NULL AND deadline < ? AND status != 'done' "
        "ORDER BY deadline ASC",
        (now_iso,),
    )
    return _rows_to_dicts(await cursor.fetchall())


# ---------- удаление с проверкой связанных данных ----------

async def delete_task_safe(task_id: int) -> None:
    """Удаляет задачу вместе со связанными контекстами/напоминаниями (ON DELETE CASCADE).

    Проверку на наличие активного напоминания и подтверждение у пользователя
    делает вызывающий код (handlers.py) до вызова этой функции.
    """
    await delete_task(task_id)
