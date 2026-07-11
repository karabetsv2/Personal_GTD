"""Билдеры клавиатур: главное меню и все инлайн-клавиатуры сценариев."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import utils

# ---------- Главное меню ----------

BTN_ADD = "📥 Новая задача"
BTN_PROCESS = "📋 Обработать Inbox"
BTN_NOW = "❓ Что делать сейчас"
BTN_PLAN = "📅 План"
BTN_DO = "✅ Отметить сделанным"
BTN_SEARCH = "🔍 Поиск"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_PROCESS)],
            [KeyboardButton(text=BTN_NOW), KeyboardButton(text=BTN_PLAN)],
            [KeyboardButton(text=BTN_DO), KeyboardButton(text=BTN_SEARCH)],
        ],
        resize_keyboard=True,
    )


def _back_cancel_row(builder: InlineKeyboardBuilder, back_cb: str = None, cancel_cb: str = "cancel") -> None:
    row = []
    if back_cb:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    row.append(InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_cb))
    builder.row(*row)


def single_cancel_kb(cancel_cb: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_cb))
    return builder.as_markup()


# ---------- Быстрый захват: предложение запланировать ----------

def quick_capture_confirm_kb(task_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📅 Запланировать", callback_data=f"qc:yes:{task_id}"))
    builder.row(InlineKeyboardButton(text="📥 Оставить в Inbox", callback_data=f"qc:no:{task_id}"))
    return builder.as_markup()


# ---------- /process: действие над задачей Inbox ----------

def inbox_action_kb(task_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚡ Сделать сейчас", callback_data=f"proc:now:{task_id}"))
    builder.row(InlineKeyboardButton(text="📅 Запланировать", callback_data=f"proc:schedule:{task_id}"))
    builder.row(InlineKeyboardButton(text="▶️ Следующее действие", callback_data=f"proc:next:{task_id}"))
    builder.row(InlineKeyboardButton(text="⏳ Ждём от кого-то", callback_data=f"proc:waiting:{task_id}"))
    builder.row(InlineKeyboardButton(text="🌫 Когда-нибудь", callback_data=f"proc:someday:{task_id}"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"proc:delete:{task_id}"))
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="proc:cancel"))
    return builder.as_markup()


# ---------- Планирование даты/времени ----------
# Используются и из /process («Запланировать»), и из /edit («Дедлайн»);
# к какому сценарию относится текущий шаг, хендлеры узнают из FSM-data (data["flow"]).

def schedule_date_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Сегодня", callback_data="sched:date:today"),
        InlineKeyboardButton(text="Завтра", callback_data="sched:date:tomorrow"),
    )
    builder.row(InlineKeyboardButton(text="Другая дата (ДД.ММ)", callback_data="sched:date:custom"))
    _back_cancel_row(builder, None, "sched:cancel")
    return builder.as_markup()


def schedule_time_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="09:00", callback_data="sched:time:09:00"),
        InlineKeyboardButton(text="12:00", callback_data="sched:time:12:00"),
        InlineKeyboardButton(text="15:00", callback_data="sched:time:15:00"),
    )
    builder.row(
        InlineKeyboardButton(text="18:00", callback_data="sched:time:18:00"),
        InlineKeyboardButton(text="21:00", callback_data="sched:time:21:00"),
    )
    builder.row(InlineKeyboardButton(text="Своё время (ЧЧ:ММ)", callback_data="sched:time:custom"))
    _back_cancel_row(builder, "sched:backtodate", "sched:cancel")
    return builder.as_markup()


# ---------- Мультивыбор контекстов ----------
# Общий для /process («Следующее действие») и /edit («Контексты»), см. data["flow"].

def contexts_kb(all_contexts: list[dict], selected_ids: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ctx in all_contexts:
        mark = "✅ " if ctx["id"] in selected_ids else ""
        builder.row(InlineKeyboardButton(text=f"{mark}@{ctx['name']}", callback_data=f"ctx:toggle:{ctx['id']}"))
    builder.row(InlineKeyboardButton(text="➕ Свой контекст", callback_data="ctx:custom"))
    builder.row(InlineKeyboardButton(text="Готово ✅", callback_data="ctx:done"))
    _back_cancel_row(builder, None, "ctx:cancel")
    return builder.as_markup()


# ---------- Приоритет ----------
# Общий для /process и /edit, см. data["flow"].

def priority_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔴 A", callback_data="prio:A"),
        InlineKeyboardButton(text="🟡 B", callback_data="prio:B"),
        InlineKeyboardButton(text="🟢 C", callback_data="prio:C"),
    )
    builder.row(InlineKeyboardButton(text="Без приоритета", callback_data="prio:none"))
    _back_cancel_row(builder, None, "prio:cancel")
    return builder.as_markup()


# ---------- Да / Нет ----------

def confirm_kb(yes_cb: str, no_cb: str, yes_label: str = "Да", no_label: str = "Нет") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=yes_label, callback_data=yes_cb),
        InlineKeyboardButton(text=no_label, callback_data=no_cb),
    )
    return builder.as_markup()


# ---------- /plan ----------

def plan_period_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Сегодня", callback_data="plan:today"),
        InlineKeyboardButton(text="Завтра", callback_data="plan:tomorrow"),
        InlineKeyboardButton(text="Неделя", callback_data="plan:week"),
    )
    return builder.as_markup()


# ---------- /now ----------

def now_time_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="5 мин", callback_data="now:time:5"),
        InlineKeyboardButton(text="15 мин", callback_data="now:time:15"),
        InlineKeyboardButton(text="30 мин", callback_data="now:time:30"),
    )
    builder.row(
        InlineKeyboardButton(text="60 мин", callback_data="now:time:60"),
        InlineKeyboardButton(text="120 мин", callback_data="now:time:120"),
    )
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="now:cancel"))
    return builder.as_markup()


def now_place_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🏠 Дома", callback_data="now:place:home"),
        InlineKeyboardButton(text="🏢 Работа", callback_data="now:place:work"),
    )
    builder.row(
        InlineKeyboardButton(text="🚶 Вне дома", callback_data="now:place:out"),
        InlineKeyboardButton(text="🌍 Где угодно", callback_data="now:place:any"),
    )
    _back_cancel_row(builder, "now:back", "now:cancel")
    return builder.as_markup()


def now_results_kb(task_ids: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for task_id in task_ids:
        builder.row(InlineKeyboardButton(text=f"✅ Готово #{task_id}", callback_data=f"do:pick:{task_id}"))
    builder.row(InlineKeyboardButton(text="🔁 Другие время/место", callback_data="now:restart"))
    return builder.as_markup()


# ---------- Списки задач с пагинацией (для /do, /edit, /delete, /search) ----------

def task_list_kb(tasks_page: list[dict], page: int, total_pages: int, prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for task in tasks_page:
        title = task["title"] if len(task["title"]) <= 40 else task["title"][:37] + "…"
        label = f"{utils.PRIORITY_LABELS.get(task['priority'], '')} {title}".strip()
        builder.row(InlineKeyboardButton(text=label, callback_data=f"{prefix}:pick:{task['id']}"))
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"{prefix}:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Далее »", callback_data=f"{prefix}:page:{page + 1}"))
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data=f"{prefix}:cancel"))
    return builder.as_markup()


# ---------- /edit: выбор поля ----------

def edit_field_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Название", callback_data="edit:field:title"))
    builder.row(InlineKeyboardButton(text="Описание", callback_data="edit:field:description"))
    builder.row(InlineKeyboardButton(text="Список", callback_data="edit:field:status"))
    builder.row(InlineKeyboardButton(text="Контексты", callback_data="edit:field:contexts"))
    builder.row(InlineKeyboardButton(text="Приоритет", callback_data="edit:field:priority"))
    builder.row(InlineKeyboardButton(text="Дедлайн", callback_data="edit:field:deadline"))
    builder.row(InlineKeyboardButton(text="✅ Завершить", callback_data="edit:finish"))
    return builder.as_markup()


def search_result_action_kb(task_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Готово", callback_data=f"do:pick:{task_id}"))
    builder.row(InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:pick:{task_id}"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:pick:{task_id}"))
    builder.row(InlineKeyboardButton(text="✖️ Закрыть", callback_data="search:cancel"))
    return builder.as_markup()


def edit_status_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for status, label in utils.STATUS_LABELS.items():
        if status == "done":
            continue
        builder.row(InlineKeyboardButton(text=label, callback_data=f"edit:status:{status}"))
    _back_cancel_row(builder, "edit:back", "edit:cancel")
    return builder.as_markup()
