"""Все хендлеры бота: команды, кнопки главного меню, быстрый захват, все сценарии."""
import logging
import re
from datetime import date, datetime, time, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import ai_client
import config
import db
import keyboards as kb
import utils
from states import (
    CoachChat,
    ContextPick,
    EditTask,
    ProcessInbox,
    ProjectBreakdown,
    ScheduleTask,
    SearchTask,
)

logger = logging.getLogger(__name__)
router = Router()


# ==================== Общие помощники ====================

def _paginate(tasks: list[dict], page: int) -> tuple[list[dict], int]:
    total_pages = max(1, (len(tasks) + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * config.PAGE_SIZE
    return tasks[start:start + config.PAGE_SIZE], total_pages


def _task_summary(task: dict, contexts: list[dict]) -> str:
    lines = [f"«{task['title']}»"]
    if task.get("description"):
        lines.append(task["description"])
    lines.append(f"Список: {utils.STATUS_LABELS[task['status']]}")
    if task.get("priority"):
        lines.append(f"Приоритет: {utils.PRIORITY_LABELS[task['priority']]}")
    if task.get("deadline"):
        lines.append(f"Дедлайн: {utils.format_dt(task['deadline'])}")
    if task["status"] == "waiting_for" and task.get("waiting_for_whom"):
        lines.append(f"Ждём: {task['waiting_for_whom']}")
    if contexts:
        lines.append("Контексты: " + " ".join(f"@{c['name']}" for c in contexts))
    return "\n".join(lines)


async def _send_next_inbox_item(target) -> None:
    tasks = await db.list_inbox_tasks()
    if not tasks:
        await target.answer("Inbox пуст! 🎉", reply_markup=kb.main_menu_kb())
        return
    task = tasks[0]
    await target.answer(f"«{task['title']}»\nЧто с ней сделать?", reply_markup=kb.inbox_action_kb(task["id"]))


async def _show_task_action_menu(target, task_id: int) -> None:
    task = await db.get_task(task_id)
    if not task or task["status"] != "inbox":
        await _send_next_inbox_item(target)
        return
    await target.answer(f"«{task['title']}»\nЧто с ней сделать?", reply_markup=kb.inbox_action_kb(task_id))


async def _show_edit_field_menu(target, task_id: int, note: str = "") -> None:
    task = await db.get_task(task_id)
    if not task:
        await target.answer("Задача не найдена (возможно, уже удалена).", reply_markup=kb.main_menu_kb())
        return
    contexts = await db.get_task_contexts(task_id)
    text = (note + "\n\n" if note else "") + _task_summary(task, contexts) + "\n\nЧто изменить?"
    await target.answer(text, reply_markup=kb.edit_field_kb())


async def _cancel_shared_subflow(callback: CallbackQuery, state: FSMContext, what: str) -> None:
    """Общая логика кнопки «Отмена» в под-сценариях sched/ctx/prio: вернуться туда, откуда пришли."""
    data = await state.get_data()
    flow = data.get("flow")
    task_id = data.get("task_id")
    await state.clear()
    if flow == "edit" and task_id:
        await _show_edit_field_menu(callback.message, task_id, note=f"{what} отменено.")
    elif task_id:
        await _show_task_action_menu(callback.message, task_id)
    else:
        await callback.message.answer("Отменено.", reply_markup=kb.main_menu_kb())


async def _ask_delete_confirm(message_to_edit: Message, task: dict) -> None:
    """Общий текст подтверждения удаления — переиспользуется из /delete, /search и /edit."""
    reminder = await db.get_reminder_for_task(task["id"])
    if reminder:
        text = (
            f"У задачи «{task['title']}» есть активное напоминание на "
            f"{utils.format_dt(reminder['remind_at'])}. Удалить вместе с напоминанием?"
        )
    else:
        text = f"Удалить «{task['title']}»?"
    await message_to_edit.edit_text(text, reply_markup=kb.confirm_kb(f"del:yes:{task['id']}", f"del:no:{task['id']}"))


async def _render_all_tasks(target, status_filter: str, page: int, edit: bool = False) -> None:
    """Экран «Все задачи»: список в любой категории с фильтром по статусу и пагинацией."""
    if status_filter == "all":
        tasks = await db.list_all_tasks_excluding_done()
    else:
        tasks = await db.list_tasks_by_status(status_filter)
    page_items, total_pages = _paginate(tasks, page)
    text = f"Все задачи ({len(tasks)}), тронь любую, чтобы изменить или удалить:" if tasks else "Задач в этой категории нет."
    markup = kb.all_tasks_kb(page_items, page, total_pages, status_filter)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


# ==================== /start, /help, /cancel ====================

HELP_TEXT = (
    "Просто напиши текстом любую задачу или идею — она попадёт в Inbox.\n\n"
    "Команды:\n"
    "/add — новая задача\n"
    "/process — разобрать Inbox\n"
    "/now — что делать прямо сейчас\n"
    "/plan сегодня|завтра|неделя — план на период\n"
    "/all — все задачи (любая категория), правка/удаление на месте\n"
    "/done — что уже сделано, с сортировкой\n"
    "/do — отметить задачу выполненной\n"
    "/edit — изменить задачу\n"
    "/delete — удалить задачу\n"
    "/search текст — найти задачу\n"
    "/balance — анализ баланса по спискам за 30 дней (ИИ)\n"
    "/coach — чат с ИИ-коучем по продуктивности\n"
    "/cancel — отменить текущее действие\n\n"
    "В /process у задачи есть кнопка «Это проект» — предложит разбивку "
    "на следующие действия через ИИ или дай добавить их вручную."
)

# Список команд для синей кнопки «Menu» в Telegram — задаётся программно через
# bot.set_my_commands() при старте (main.py), не зависит от ручной настройки в BotFather
# и не теряется при смене токена.
BOT_COMMANDS = [
    ("add", "Новая задача в Inbox"),
    ("process", "Разобрать Inbox по одной задаче"),
    ("now", "Что делать прямо сейчас"),
    ("plan", "План на сегодня/завтра/неделю"),
    ("all", "Все задачи в любой категории"),
    ("done", "Что уже сделано"),
    ("do", "Отметить задачу выполненной"),
    ("edit", "Изменить задачу"),
    ("delete", "Удалить задачу"),
    ("search", "Найти задачу"),
    ("balance", "Анализ баланса за 30 дней (ИИ)"),
    ("coach", "Чат с ИИ-коучем"),
    ("cancel", "Отменить текущее действие"),
    ("help", "Список команд"),
]


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.set_owner_chat_id(message.chat.id)
    await message.answer("Привет! Это твой личный GTD-помощник.\n\n" + HELP_TEXT, reply_markup=kb.main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=kb.main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.main_menu_kb())


# ==================== Точки входа команд/кнопок главного меню ====================
# Регистрируются рано и без фильтра по состоянию — работают как «аварийный выход»
# из любого сценария (текущее незавершённое действие при этом сбрасывается).

@router.message(Command("add"))
@router.message(F.text == kb.BTN_ADD)
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Напиши текстом, что нужно сделать — я сохраню это в Inbox.")


@router.message(Command("process"))
@router.message(F.text == kb.BTN_PROCESS)
async def cmd_process(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_next_inbox_item(message)


@router.message(Command("now"))
@router.message(F.text == kb.BTN_NOW)
async def cmd_now(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Сколько у тебя времени?", reply_markup=kb.now_time_kb())


@router.message(Command("plan"))
@router.message(F.text == kb.BTN_PLAN)
async def cmd_plan(message: Message, state: FSMContext, command: CommandObject = None) -> None:
    await state.clear()
    arg = (command.args or "").strip().lower() if command else ""
    period = {"сегодня": "today", "завтра": "tomorrow", "неделя": "week", "неделю": "week"}.get(arg)
    if period:
        await _send_plan(message, period)
    else:
        await message.answer("На какой период показать план?", reply_markup=kb.plan_period_kb())


@router.message(Command("do"))
@router.message(F.text == kb.BTN_DO)
async def cmd_do(message: Message, state: FSMContext) -> None:
    await state.clear()
    tasks = await db.list_open_tasks()
    if not tasks:
        await message.answer("Нет открытых задач в Next Actions/Календаре.")
        return
    page_items, total_pages = _paginate(tasks, 0)
    await message.answer("Какая задача выполнена?", reply_markup=kb.task_list_kb(page_items, 0, total_pages, "do"))


@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    tasks = await db.list_all_tasks_excluding_done()
    if not tasks:
        await message.answer("Пока нечего редактировать — задач нет.")
        return
    page_items, total_pages = _paginate(tasks, 0)
    await message.answer("Какую задачу изменить?", reply_markup=kb.task_list_kb(page_items, 0, total_pages, "edit"))


@router.message(Command("delete"))
async def cmd_delete(message: Message, state: FSMContext) -> None:
    await state.clear()
    tasks = await db.list_all_tasks_excluding_done()
    if not tasks:
        await message.answer("Удалять нечего — задач нет.")
        return
    page_items, total_pages = _paginate(tasks, 0)
    await message.answer("Какую задачу удалить?", reply_markup=kb.task_list_kb(page_items, 0, total_pages, "del"))


@router.message(Command("search"))
@router.message(F.text == kb.BTN_SEARCH)
async def cmd_search(message: Message, state: FSMContext, command: CommandObject = None) -> None:
    await state.clear()
    query = (command.args or "").strip() if command else ""
    if query:
        await _do_search(message, state, query)
    else:
        await state.set_state(SearchTask.entering_query)
        await message.answer("Что ищем?", reply_markup=kb.single_cancel_kb("search:cancel"))


@router.message(Command("all"))
@router.message(F.text == kb.BTN_ALL)
async def cmd_all_tasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _render_all_tasks(message, "all", 0)


@router.message(Command("done"))
@router.message(F.text == kb.BTN_DONE_LOG)
async def cmd_done(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("За какой период показать сделанное?", reply_markup=kb.done_period_kb())


@router.message(Command("balance"))
@router.message(F.text == kb.BTN_BALANCE)
async def cmd_balance(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Анализирую последние 30 дней…")
    report = await build_balance_report()
    await message.answer(report, reply_markup=kb.main_menu_kb())


@router.message(Command("coach"))
@router.message(F.text == kb.BTN_COACH)
async def cmd_coach(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(coach_history=[])
    await state.set_state(CoachChat.chatting)
    await message.answer(
        "Коуч на связи. Пиши, что на уме — обсудим, как у тебя дела с задачами.",
        reply_markup=kb.coach_exit_kb(),
    )


# ==================== Быстрый захват: предложение запланировать ====================

@router.callback_query(F.data.startswith("qc:"))
async def quick_capture_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    _, choice, task_id_str = callback.data.split(":")
    task_id = int(task_id_str)
    task = await db.get_task(task_id)
    if not task:
        await callback.message.edit_text("Задача не найдена (возможно, уже обработана).")
        return
    if choice == "yes":
        parsed = utils.parse_date_hints(task["title"])
        if not parsed:
            await callback.message.edit_text("Не получилось распознать дату повторно, задача осталась в Inbox.")
            return
        deadline_iso = utils.dt_to_iso(parsed.dt)
        await db.update_task(task_id, status="calendar", deadline=deadline_iso)
        await db.sync_reminder_for_task(task_id, deadline_iso)
        await callback.message.edit_text(f"Запланировано на {utils.format_dt(parsed.dt)} 📅")
    else:
        await callback.message.edit_text(f"Ок, «{task['title']}» осталась в Inbox 📥")


# ==================== /process: действие над задачей Inbox ====================

@router.callback_query(F.data.startswith("proc:"))
async def process_action(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1]

    if action == "cancel":
        await callback.answer()
        await callback.message.edit_text("Разбор Inbox остановлен. Задача осталась в Inbox.")
        return

    if action == "waitcancel":
        await callback.answer()
        data = await state.get_data()
        pending_task_id = data.get("task_id")
        await state.clear()
        if pending_task_id:
            await _show_task_action_menu(callback.message, pending_task_id)
        return

    task_id = int(parts[2])
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача уже обработана.")
        await _send_next_inbox_item(callback.message)
        return

    if action == "now":
        await callback.answer("Готово ✅")
        await db.update_task(task_id, status="done")
        await db.sync_reminder_for_task(task_id, None)
        await callback.message.edit_text(f"«{task['title']}» — сделано прямо сейчас ✅")
        await _send_next_inbox_item(callback.message)

    elif action == "someday":
        await callback.answer()
        await db.update_task(task_id, status="someday")
        await callback.message.edit_text(f"«{task['title']}» → Когда-нибудь 🌫")
        await _send_next_inbox_item(callback.message)

    elif action == "delete":
        await callback.answer()
        reminder = await db.get_reminder_for_task(task_id)
        if reminder:
            await callback.message.edit_text(
                f"У задачи «{task['title']}» есть активное напоминание на "
                f"{utils.format_dt(reminder['remind_at'])}. Удалить вместе с напоминанием?",
                reply_markup=kb.confirm_kb(f"procdel:yes:{task_id}", f"procdel:no:{task_id}"),
            )
        else:
            await db.delete_task_safe(task_id)
            await callback.message.edit_text(f"«{task['title']}» удалена из Inbox 🗑")
            await _send_next_inbox_item(callback.message)

    elif action == "waiting":
        await callback.answer()
        await state.update_data(flow="process", task_id=task_id)
        await state.set_state(ProcessInbox.entering_waiting_whom)
        await callback.message.edit_text(f"«{task['title']}»\nКого или что ждём?")
        await callback.message.answer("Введи имя/описание ожидания:", reply_markup=kb.single_cancel_kb("proc:waitcancel"))

    elif action == "schedule":
        await callback.answer()
        await state.update_data(flow="process", task_id=task_id)
        await callback.message.edit_text(f"«{task['title']}»\nВыбери дату:", reply_markup=kb.schedule_date_kb())

    elif action == "next":
        await callback.answer()
        await state.update_data(flow="process", task_id=task_id, selected_context_ids=[])
        all_contexts = await db.list_contexts()
        await callback.message.edit_text(
            f"«{task['title']}»\nВыбери контексты (можно несколько):",
            reply_markup=kb.contexts_kb(all_contexts, set()),
        )

    elif action == "project":
        await callback.answer()
        project_id = await db.create_project(task["title"], task.get("description"))
        await db.delete_task_safe(task_id)
        await callback.message.edit_text(
            f"📁 Создан проект «{task['title']}» (исходная задача перенесена в проект и удалена из Inbox).\n"
            "Как разобьём на следующие действия?",
            reply_markup=kb.project_breakdown_choice_kb(project_id),
        )


@router.callback_query(F.data.startswith("procdel:"))
async def process_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    _, choice, task_id_str = callback.data.split(":")
    task_id = int(task_id_str)
    task = await db.get_task(task_id)
    if choice == "yes" and task:
        await db.delete_task_safe(task_id)
        await callback.message.edit_text(f"«{task['title']}» и её напоминание удалены 🗑")
        await _send_next_inbox_item(callback.message)
    elif task:
        await callback.message.edit_text("Удаление отменено.")
        await _show_task_action_menu(callback.message, task_id)


@router.message(StateFilter(ProcessInbox.entering_waiting_whom))
async def process_waiting_whom_text(message: Message, state: FSMContext) -> None:
    whom = message.text.strip()
    if not whom:
        await message.answer("Пусто. Напиши, кого/что ждём:", reply_markup=kb.single_cancel_kb("proc:waitcancel"))
        return
    data = await state.get_data()
    task_id = data["task_id"]
    flow = data["flow"]
    task = await db.get_task(task_id)
    if not task:
        await state.clear()
        await message.answer("Задача не найдена.", reply_markup=kb.main_menu_kb())
        return
    await db.update_task(task_id, status="waiting_for", waiting_for_whom=whom)
    await state.clear()
    if flow == "process":
        await message.answer(f"«{task['title']}» → Ожидание (ждём: {whom}) ⏳")
        await _send_next_inbox_item(message)
    else:
        await _show_edit_field_menu(message, task_id, note=f"Теперь ждём: {whom} ⏳")


# ==================== Разбивка проекта на действия (ИИ или вручную) ====================

@router.callback_query(F.data.startswith("projbrk:ai:"))
async def projbrk_ai_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Думаю над разбивкой…")
    project_id = int(callback.data[len("projbrk:ai:"):])
    project = await db.get_project(project_id)
    if not project:
        await callback.message.edit_text("Проект не найден.")
        return
    try:
        actions = await ai_client.suggest_next_actions(project["name"], project.get("description"))
    except ai_client.AIError as e:
        logger.warning("Не удалось получить разбивку от ИИ: %s", e)
        await callback.message.edit_text(
            "🤖 ИИ сейчас недоступен. Хочешь добавить действия вручную?",
            reply_markup=kb.ai_fallback_kb(project_id),
        )
        return
    await state.update_data(project_id=project_id, suggestions=actions, suggestion_index=0, added_count=0)
    await _show_next_suggestion(callback.message, state)


async def _show_next_suggestion(target, state: FSMContext) -> None:
    data = await state.get_data()
    suggestions = data.get("suggestions", [])
    idx = data.get("suggestion_index", 0)
    if idx >= len(suggestions):
        await _finish_breakdown(target, state)
        return
    await target.answer(
        f"Действие {idx + 1}/{len(suggestions)}:\n«{suggestions[idx]}»",
        reply_markup=kb.suggestion_review_kb(),
    )


async def _finish_breakdown(target, state: FSMContext) -> None:
    data = await state.get_data()
    added = data.get("added_count", 0)
    project_id = data.get("project_id")
    project = await db.get_project(project_id) if project_id else None
    await state.clear()
    name = project["name"] if project else "проекте"
    await target.answer(f"Готово! Добавлено {added} действий в проект «{name}».", reply_markup=kb.main_menu_kb())


@router.callback_query(F.data == "projbrk:accept")
async def projbrk_accept(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Добавлено ✅")
    data = await state.get_data()
    suggestions = data.get("suggestions", [])
    idx = data.get("suggestion_index", 0)
    project_id = data.get("project_id")
    if idx < len(suggestions):
        await db.add_task(suggestions[idx], status="next_action", project_id=project_id)
        await state.update_data(suggestion_index=idx + 1, added_count=data.get("added_count", 0) + 1)
    await _show_next_suggestion(callback.message, state)


@router.callback_query(F.data == "projbrk:reject")
async def projbrk_reject(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Пропущено")
    data = await state.get_data()
    idx = data.get("suggestion_index", 0)
    await state.update_data(suggestion_index=idx + 1)
    await _show_next_suggestion(callback.message, state)


@router.callback_query(F.data == "projbrk:edit")
async def projbrk_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ProjectBreakdown.editing_suggestion)
    await callback.message.answer("Напиши исправленный текст действия:")


@router.message(StateFilter(ProjectBreakdown.editing_suggestion))
async def projbrk_edit_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("Пусто. Напиши текст действия:")
        return
    data = await state.get_data()
    idx = data.get("suggestion_index", 0)
    project_id = data.get("project_id")
    await db.add_task(text, status="next_action", project_id=project_id)
    await state.set_state(None)
    await state.update_data(suggestion_index=idx + 1, added_count=data.get("added_count", 0) + 1)
    await _show_next_suggestion(message, state)


@router.callback_query(F.data.startswith("projbrk:manual:"))
async def projbrk_manual_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    project_id = int(callback.data[len("projbrk:manual:"):])
    await state.update_data(project_id=project_id, added_count=0)
    await state.set_state(ProjectBreakdown.entering_manual_action)
    await callback.message.edit_text(
        "Пиши следующие действия по одному, каждое отдельным сообщением.\n"
        "Когда закончишь — нажми «Готово».",
        reply_markup=kb.manual_action_entry_kb(),
    )


@router.message(StateFilter(ProjectBreakdown.entering_manual_action))
async def projbrk_manual_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text:
        return
    data = await state.get_data()
    project_id = data.get("project_id")
    await db.add_task(text, status="next_action", project_id=project_id)
    added = data.get("added_count", 0) + 1
    await state.update_data(added_count=added)
    await message.answer(f"Добавлено ({added}): «{text}»", reply_markup=kb.manual_action_entry_kb())


@router.callback_query(F.data == "projbrk:manualdone")
async def projbrk_manual_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _finish_breakdown(callback.message, state)


@router.callback_query(F.data.startswith("projbrk:skip:"))
async def projbrk_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    project_id = int(callback.data[len("projbrk:skip:"):])
    project = await db.get_project(project_id)
    name = project["name"] if project else "проект"
    await callback.message.edit_text(f"Ок, проект «{name}» создан без действий.")


# ==================== Планирование даты/времени (sched:*) ====================
# Общее для /process («Запланировать») и /edit («Дедлайн»); data["flow"] определяет ветку.

async def _schedule_ask_time(target, state: FSMContext, d: date) -> None:
    await state.update_data(pending_date=d.isoformat())
    await target.answer(f"Дата: {utils.format_date(d)}\nВыбери время:", reply_markup=kb.schedule_time_kb())


async def _schedule_finish(target, state: FSMContext, t: time) -> None:
    data = await state.get_data()
    task_id = data["task_id"]
    flow = data["flow"]
    d = date.fromisoformat(data["pending_date"])
    local_dt = datetime.combine(d, t, tzinfo=utils.TZ)
    deadline_iso = utils.dt_to_iso(local_dt)
    task = await db.get_task(task_id)
    await state.clear()
    if not task:
        await target.answer("Задача не найдена.", reply_markup=kb.main_menu_kb())
        return
    if flow == "process":
        await db.update_task(task_id, status="calendar", deadline=deadline_iso)
        await db.sync_reminder_for_task(task_id, deadline_iso)
        await target.answer(f"«{task['title']}» → Календарь на {utils.format_dt(local_dt)} 📅")
        await _send_next_inbox_item(target)
    else:
        await db.update_task(task_id, deadline=deadline_iso)
        await db.sync_reminder_for_task(task_id, deadline_iso)
        await _show_edit_field_menu(target, task_id, note=f"Дедлайн обновлён: {utils.format_dt(local_dt)} ✅")


@router.callback_query(F.data == "sched:date:today")
async def sched_date_today(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _schedule_ask_time(callback.message, state, utils.now_local().date())


@router.callback_query(F.data == "sched:date:tomorrow")
async def sched_date_tomorrow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _schedule_ask_time(callback.message, state, utils.now_local().date() + timedelta(days=1))


@router.callback_query(F.data == "sched:date:custom")
async def sched_date_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ScheduleTask.entering_custom_date)
    await callback.message.answer(
        "Введи дату (например, 15.08 или 15.08.2026):", reply_markup=kb.single_cancel_kb("sched:cancel")
    )


@router.message(StateFilter(ScheduleTask.entering_custom_date))
async def sched_date_custom_text(message: Message, state: FSMContext) -> None:
    parsed = utils.parse_date_hints(message.text)
    if not parsed:
        await message.answer(
            "Не понял дату. Формат: ДД.ММ или ДД.ММ.ГГГГ, либо слова вроде «завтра»/«в пятницу». Попробуй ещё раз:",
            reply_markup=kb.single_cancel_kb("sched:cancel"),
        )
        return
    await state.set_state(None)
    await _schedule_ask_time(message, state, parsed.dt.date())


@router.callback_query(F.data.startswith("sched:time:"))
async def sched_time_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    suffix = callback.data[len("sched:time:"):]
    if suffix == "custom":
        await state.set_state(ScheduleTask.entering_custom_time)
        await callback.message.answer("Введи время в формате ЧЧ:ММ:", reply_markup=kb.single_cancel_kb("sched:cancel"))
        return
    hour, minute = (int(x) for x in suffix.split(":"))
    await _schedule_finish(callback.message, state, time(hour, minute))


@router.message(StateFilter(ScheduleTask.entering_custom_time))
async def sched_time_custom_text(message: Message, state: FSMContext) -> None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", message.text.strip())
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        await message.answer("Не понял время. Формат: ЧЧ:ММ, например 09:30. Попробуй ещё раз:",
                              reply_markup=kb.single_cancel_kb("sched:cancel"))
        return
    await state.set_state(None)
    await _schedule_finish(message, state, time(int(m.group(1)), int(m.group(2))))


@router.callback_query(F.data == "sched:backtodate")
async def sched_back_to_date(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(None)
    await callback.message.answer("Выбери дату:", reply_markup=kb.schedule_date_kb())


@router.callback_query(F.data == "sched:cancel")
async def sched_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _cancel_shared_subflow(callback, state, "Планирование")


# ==================== Мультивыбор контекстов (ctx:*) ====================
# Общее для /process («Следующее действие») и /edit («Контексты»).

@router.callback_query(F.data.startswith("ctx:toggle:"))
async def ctx_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    context_id = int(callback.data[len("ctx:toggle:"):])
    data = await state.get_data()
    selected = set(data.get("selected_context_ids", []))
    if context_id in selected:
        selected.discard(context_id)
    else:
        selected.add(context_id)
    await state.update_data(selected_context_ids=list(selected))
    await callback.answer()
    all_contexts = await db.list_contexts()
    await callback.message.edit_reply_markup(reply_markup=kb.contexts_kb(all_contexts, selected))


@router.callback_query(F.data == "ctx:custom")
async def ctx_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ContextPick.entering_custom_context)
    await callback.message.answer("Введи название нового контекста (без @):", reply_markup=kb.single_cancel_kb("ctx:cancel"))


@router.message(StateFilter(ContextPick.entering_custom_context))
async def ctx_custom_text(message: Message, state: FSMContext) -> None:
    name = message.text.strip().lstrip("@")
    if not name:
        await message.answer("Пустое название не подойдёт. Введи ещё раз:", reply_markup=kb.single_cancel_kb("ctx:cancel"))
        return
    context = await db.get_or_create_context(name, "custom")
    data = await state.get_data()
    selected = set(data.get("selected_context_ids", []))
    selected.add(context["id"])
    await state.update_data(selected_context_ids=list(selected))
    await state.set_state(None)
    all_contexts = await db.list_contexts()
    await message.answer(f"Контекст @{context['name']} добавлен и выбран.", reply_markup=kb.contexts_kb(all_contexts, selected))


@router.callback_query(F.data == "ctx:done")
async def ctx_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    task_id = data["task_id"]
    flow = data["flow"]
    selected = data.get("selected_context_ids", [])
    await db.set_task_contexts(task_id, selected)
    if flow == "process":
        await callback.message.answer("Контексты сохранены ✅\nПриоритет?", reply_markup=kb.priority_kb())
    else:
        await state.clear()
        await _show_edit_field_menu(callback.message, task_id, note="Контексты обновлены ✅")


@router.callback_query(F.data == "ctx:cancel")
async def ctx_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _cancel_shared_subflow(callback, state, "Выбор контекстов")


# ==================== Приоритет (prio:*) ====================
# Общее для /process (следует сразу за выбором контекстов) и /edit («Приоритет»).

@router.callback_query(F.data.startswith("prio:"))
async def prio_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    choice = callback.data[len("prio:"):]
    if choice == "cancel":
        await _cancel_shared_subflow(callback, state, "Выбор приоритета")
        return
    priority = None if choice == "none" else choice
    data = await state.get_data()
    task_id = data["task_id"]
    flow = data["flow"]
    task = await db.get_task(task_id)
    if not task:
        await state.clear()
        await callback.message.answer("Задача не найдена.", reply_markup=kb.main_menu_kb())
        return
    if flow == "process":
        await db.update_task(task_id, status="next_action", priority=priority)
        await state.clear()
        await callback.message.answer(f"«{task['title']}» → Следующие действия ✅")
        await _send_next_inbox_item(callback.message)
    else:
        await db.update_task(task_id, priority=priority)
        await state.clear()
        await _show_edit_field_menu(callback.message, task_id, note="Приоритет обновлён ✅")


# ==================== /now ====================

@router.callback_query(F.data.startswith("now:time:"))
async def now_time_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    minutes = int(callback.data[len("now:time:"):])
    await state.update_data(now_minutes=minutes)
    await callback.message.edit_text(f"У тебя {minutes} мин. Где ты?", reply_markup=kb.now_place_kb())


@router.callback_query(F.data == "now:back")
async def now_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Сколько у тебя времени?", reply_markup=kb.now_time_kb())


@router.callback_query(F.data == "now:restart")
async def now_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("Сколько у тебя времени?", reply_markup=kb.now_time_kb())


@router.callback_query(F.data == "now:cancel")
async def now_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Ок.")


_LOCATION_GROUPS = {
    "home": ["home"],
    "work": ["work"],
    "out": ["work", "out"],
    "any": None,
}


@router.callback_query(F.data.startswith("now:place:"))
async def now_place_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    place = callback.data[len("now:place:"):]
    data = await state.get_data()
    minutes = data.get("now_minutes", 15)
    location_groups = _LOCATION_GROUPS.get(place)
    tasks = await db.list_next_actions_for_now(minutes, location_groups)
    await state.clear()
    if not tasks:
        await callback.message.edit_text(
            f"На {minutes} мин подходящих задач не нашлось. Попробуй другое время или место.",
            reply_markup=kb.now_results_kb([]),
        )
        return
    lines = [f"Можно заняться (до {minutes} мин):"]
    for t in tasks[:10]:
        contexts = await db.get_task_contexts(t["id"])
        lines.append("• " + utils.format_task_line(t, contexts))
    await callback.message.edit_text("\n".join(lines), reply_markup=kb.now_results_kb([t["id"] for t in tasks[:10]]))


# ==================== /do и общая отметка «выполнено» ====================
# do:pick переиспользуется из /do, /now (результаты) и /search (мини-меню действий).

@router.callback_query(F.data.startswith("do:pick:"))
async def do_pick(callback: CallbackQuery) -> None:
    task_id = int(callback.data[len("do:pick:"):])
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача уже удалена.")
        return
    await db.update_task(task_id, status="done")
    await db.sync_reminder_for_task(task_id, None)
    await callback.answer("Готово ✅")
    await callback.message.answer(f"«{task['title']}» — выполнено ✅")


@router.callback_query(F.data.startswith("do:page:"))
async def do_page(callback: CallbackQuery) -> None:
    await callback.answer()
    page = int(callback.data[len("do:page:"):])
    tasks = await db.list_open_tasks()
    page_items, total_pages = _paginate(tasks, page)
    await callback.message.edit_reply_markup(reply_markup=kb.task_list_kb(page_items, page, total_pages, "do"))


@router.callback_query(F.data == "do:cancel")
async def do_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Ок.")


# ==================== /edit ====================

@router.callback_query(F.data.startswith("edit:pick:"))
async def edit_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    task_id = int(callback.data[len("edit:pick:"):])
    await state.update_data(flow="edit", task_id=task_id)
    await _show_edit_field_menu(callback.message, task_id)


@router.callback_query(F.data.startswith("edit:page:"))
async def edit_page(callback: CallbackQuery) -> None:
    await callback.answer()
    page = int(callback.data[len("edit:page:"):])
    tasks = await db.list_all_tasks_excluding_done()
    page_items, total_pages = _paginate(tasks, page)
    await callback.message.edit_reply_markup(reply_markup=kb.task_list_kb(page_items, page, total_pages, "edit"))


@router.callback_query(F.data == "edit:cancel")
async def edit_cancel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Редактирование отменено.")


@router.callback_query(F.data == "edit:finish")
async def edit_finish(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Готово ✅")


@router.callback_query(F.data == "edit:fieldcancel")
async def edit_field_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    task_id = data.get("task_id")
    await state.set_state(None)
    if task_id:
        await _show_edit_field_menu(callback.message, task_id)


@router.callback_query(F.data == "edit:field:title")
async def edit_field_title(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditTask.entering_title)
    await callback.message.answer("Введи новое название:", reply_markup=kb.single_cancel_kb("edit:fieldcancel"))


@router.message(StateFilter(EditTask.entering_title))
async def edit_title_text(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Введи ещё раз:", reply_markup=kb.single_cancel_kb("edit:fieldcancel"))
        return
    data = await state.get_data()
    task_id = data["task_id"]
    await db.update_task(task_id, title=title)
    await state.set_state(None)
    await _show_edit_field_menu(message, task_id, note="Название обновлено ✅")


@router.callback_query(F.data == "edit:field:description")
async def edit_field_description(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditTask.entering_description)
    await callback.message.answer("Введи новое описание:", reply_markup=kb.single_cancel_kb("edit:fieldcancel"))


@router.message(StateFilter(EditTask.entering_description))
async def edit_description_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data["task_id"]
    await db.update_task(task_id, description=message.text.strip())
    await state.set_state(None)
    await _show_edit_field_menu(message, task_id, note="Описание обновлено ✅")


@router.callback_query(F.data == "edit:field:status")
async def edit_field_status(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("В какой список перенести задачу?", reply_markup=kb.edit_status_kb())


@router.callback_query(F.data == "edit:field:contexts")
async def edit_field_contexts(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    task_id = data["task_id"]
    await state.update_data(flow="edit", task_id=task_id)
    current = await db.get_task_contexts(task_id)
    selected = {c["id"] for c in current}
    await state.update_data(selected_context_ids=list(selected))
    all_contexts = await db.list_contexts()
    await callback.message.edit_text("Выбери контексты (можно несколько):", reply_markup=kb.contexts_kb(all_contexts, selected))


@router.callback_query(F.data == "edit:field:priority")
async def edit_field_priority(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.update_data(flow="edit", task_id=data["task_id"])
    await callback.message.edit_text("Новый приоритет?", reply_markup=kb.priority_kb())


@router.callback_query(F.data == "edit:field:deadline")
async def edit_field_deadline(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.update_data(flow="edit", task_id=data["task_id"])
    await callback.message.edit_text("Выбери дату:", reply_markup=kb.schedule_date_kb())


@router.callback_query(F.data == "edit:back")
async def edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id:
        await _show_edit_field_menu(callback.message, task_id)


@router.callback_query(F.data.startswith("edit:status:"))
async def edit_status_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    new_status = callback.data[len("edit:status:"):]
    data = await state.get_data()
    task_id = data["task_id"]
    task = await db.get_task(task_id)
    if not task:
        await state.clear()
        await callback.message.answer("Задача не найдена.", reply_markup=kb.main_menu_kb())
        return
    if new_status == "waiting_for":
        await db.update_task(task_id, status="waiting_for")
        await state.update_data(flow="edit", task_id=task_id)
        await state.set_state(ProcessInbox.entering_waiting_whom)
        await callback.message.answer("Кого или что ждём?", reply_markup=kb.single_cancel_kb("edit:fieldcancel"))
        return
    await db.update_task(task_id, status=new_status)
    await _show_edit_field_menu(callback.message, task_id, note=f"Список изменён: {utils.STATUS_LABELS[new_status]} ✅")


@router.callback_query(F.data == "edit:delete")
async def edit_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    task_id = data.get("task_id")
    task = await db.get_task(task_id) if task_id else None
    if not task:
        await callback.message.edit_text("Задача не найдена.")
        return
    await _ask_delete_confirm(callback.message, task)


# ==================== «Все задачи» (просмотр/правка в любой категории) ====================

@router.callback_query(F.data.startswith("all:filter:"))
async def all_filter_choice(callback: CallbackQuery) -> None:
    await callback.answer()
    status_filter = callback.data[len("all:filter:"):]
    await _render_all_tasks(callback.message, status_filter, 0, edit=True)


@router.callback_query(F.data.startswith("all:page:"))
async def all_page_choice(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, status_filter, page_str = callback.data.split(":")
    await _render_all_tasks(callback.message, status_filter, int(page_str), edit=True)


@router.callback_query(F.data == "all:cancel")
async def all_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Ок.")


# ==================== /delete ====================

@router.callback_query(F.data.startswith("del:pick:"))
async def del_pick(callback: CallbackQuery) -> None:
    await callback.answer()
    task_id = int(callback.data[len("del:pick:"):])
    task = await db.get_task(task_id)
    if not task:
        await callback.message.edit_text("Задача уже удалена.")
        return
    await _ask_delete_confirm(callback.message, task)


@router.callback_query(F.data.startswith("del:yes:"))
async def del_yes(callback: CallbackQuery) -> None:
    await callback.answer()
    task_id = int(callback.data[len("del:yes:"):])
    task = await db.get_task(task_id)
    if task:
        await db.delete_task_safe(task_id)
        await callback.message.edit_text(f"«{task['title']}» удалена 🗑")
    else:
        await callback.message.edit_text("Задача уже удалена.")


@router.callback_query(F.data.startswith("del:no:"))
async def del_no(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Удаление отменено.")


@router.callback_query(F.data.startswith("del:page:"))
async def del_page(callback: CallbackQuery) -> None:
    await callback.answer()
    page = int(callback.data[len("del:page:"):])
    tasks = await db.list_all_tasks_excluding_done()
    page_items, total_pages = _paginate(tasks, page)
    await callback.message.edit_reply_markup(reply_markup=kb.task_list_kb(page_items, page, total_pages, "del"))


@router.callback_query(F.data == "del:cancel")
async def del_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Ок.")


# ==================== /search ====================

async def _do_search(target, state: FSMContext, query: str) -> None:
    tasks = await db.list_all_tasks_excluding_done()
    results = utils.fuzzy_search(query, tasks)
    await state.update_data(last_search_query=query)
    if not results:
        await target.answer(f"По запросу «{query}» ничего не найдено.")
        return
    page_items, total_pages = _paginate(results, 0)
    await target.answer(
        f"Нашлось по «{query}»:", reply_markup=kb.task_list_kb(page_items, 0, total_pages, "search")
    )


@router.message(StateFilter(SearchTask.entering_query))
async def search_query_text(message: Message, state: FSMContext) -> None:
    query = message.text.strip()
    await state.set_state(None)
    if not query:
        await message.answer("Пустой запрос. Что ищем?", reply_markup=kb.single_cancel_kb("search:cancel"))
        await state.set_state(SearchTask.entering_query)
        return
    await _do_search(message, state, query)


@router.callback_query(F.data.startswith("search:page:"))
async def search_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data[len("search:page:"):])
    data = await state.get_data()
    query = data.get("last_search_query", "")
    tasks = await db.list_all_tasks_excluding_done()
    results = utils.fuzzy_search(query, tasks)
    page_items, total_pages = _paginate(results, page)
    await callback.message.edit_reply_markup(reply_markup=kb.task_list_kb(page_items, page, total_pages, "search"))


@router.callback_query(F.data.startswith("search:pick:"))
async def search_pick(callback: CallbackQuery) -> None:
    await callback.answer()
    task_id = int(callback.data[len("search:pick:"):])
    task = await db.get_task(task_id)
    if not task:
        await callback.message.edit_text("Задача уже удалена.")
        return
    contexts = await db.get_task_contexts(task_id)
    await callback.message.edit_text(_task_summary(task, contexts), reply_markup=kb.search_result_action_kb(task_id))


@router.callback_query(F.data == "search:cancel")
async def search_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Ок.")


# ==================== /plan ====================

def _period_range(period: str):
    today = utils.now_local().date()
    if period == "today":
        start = datetime.combine(today, time.min, tzinfo=utils.TZ)
        return start, start + timedelta(days=1), "сегодня"
    if period == "tomorrow":
        start = datetime.combine(today + timedelta(days=1), time.min, tzinfo=utils.TZ)
        return start, start + timedelta(days=1), "завтра"
    if period == "week":
        start = datetime.combine(today, time.min, tzinfo=utils.TZ)
        return start, start + timedelta(days=7), "неделю"
    return None


async def _send_plan(target, period: str) -> None:
    rng = _period_range(period)
    if not rng:
        await target.answer("Не понял период. Выбери:", reply_markup=kb.plan_period_kb())
        return
    start, end, label = rng
    tasks = await db.list_tasks_with_deadline_between(utils.dt_to_iso(start), utils.dt_to_iso(end))
    if not tasks:
        await target.answer(f"На {label} ничего не запланировано.")
        return
    lines = [f"План на {label}:"]
    for t in tasks:
        lines.append("• " + utils.format_task_line(t))
    await target.answer("\n".join(lines), reply_markup=kb.now_results_kb([t["id"] for t in tasks]))


@router.callback_query(F.data.startswith("plan:"))
async def plan_period_choice(callback: CallbackQuery) -> None:
    await callback.answer()
    period = callback.data[len("plan:"):]
    await _send_plan(callback.message, period)


# ==================== /done: что уже сделано ====================

_DONE_MAX_ROWS = 40  # защита от слишком длинного сообщения при периоде «Всё время»


def _done_period_range(period: str):
    today = utils.now_local().date()
    if period == "today":
        start = datetime.combine(today, time.min, tzinfo=utils.TZ)
        return start, start + timedelta(days=1), "сегодня"
    if period == "yesterday":
        start = datetime.combine(today - timedelta(days=1), time.min, tzinfo=utils.TZ)
        return start, start + timedelta(days=1), "вчера"
    if period == "week":
        start = datetime.combine(today - timedelta(days=6), time.min, tzinfo=utils.TZ)
        end = datetime.combine(today, time.min, tzinfo=utils.TZ) + timedelta(days=1)
        return start, end, "последние 7 дней"
    return None, None, "всё время"


_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2, None: 3}


def _sort_done_tasks(tasks: list[dict], sort_key: str) -> list[dict]:
    if sort_key == "priority":
        return sorted(tasks, key=lambda t: (_PRIORITY_ORDER.get(t["priority"], 3), t["completed_at"] or ""))
    if sort_key == "title":
        return sorted(tasks, key=lambda t: t["title"].lower())
    return sorted(tasks, key=lambda t: t["completed_at"] or "", reverse=True)


async def _render_done(target, period: str, sort_key: str, edit: bool = False) -> None:
    start, end, label = _done_period_range(period)
    tasks = await db.list_done_tasks_all() if start is None else await db.list_done_tasks_between(
        utils.dt_to_iso(start), utils.dt_to_iso(end)
    )
    tasks = _sort_done_tasks(tasks, sort_key)
    truncated = len(tasks) > _DONE_MAX_ROWS
    shown = tasks[:_DONE_MAX_ROWS]
    if not shown:
        text = f"За {label} ничего не выполнено."
    else:
        lines = [f"Сделано за {label} ({len(tasks)}):"]
        for t in shown:
            when = utils.format_dt(t["completed_at"]) if t.get("completed_at") else "?"
            prio = utils.PRIORITY_LABELS.get(t["priority"], "")
            lines.append(f"• {prio} {t['title']} — {when}".replace("  ", " "))
        if truncated:
            lines.append(f"… и ещё {len(tasks) - _DONE_MAX_ROWS}, сузь период, чтобы увидеть все")
        text = "\n".join(lines)
    markup = kb.done_results_kb(sort_key)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("done:period:"))
async def done_period_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    period = callback.data[len("done:period:"):]
    await state.update_data(done_period=period)
    await _render_done(callback.message, period, "time", edit=True)


@router.callback_query(F.data.startswith("done:sort:"))
async def done_sort_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    sort_key = callback.data[len("done:sort:"):]
    data = await state.get_data()
    period = data.get("done_period", "today")
    await _render_done(callback.message, period, sort_key, edit=True)


@router.callback_query(F.data == "done:restart")
async def done_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("За какой период показать сделанное?", reply_markup=kb.done_period_kb())


@router.callback_query(F.data == "done:cancel")
async def done_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Ок.")


# ==================== /balance и статистика (переиспользуется и коучем) ====================

async def _gather_stats(days: int) -> dict:
    """Сырые данные за период — источник и для /balance, и для контекста коуча."""
    end = utils.now_local()
    start = end - timedelta(days=days)
    start_iso = utils.dt_to_iso(start)
    context_stats = await db.get_context_stats_since(start_iso)
    stale_waiting = await db.list_stale_waiting_for(utils.dt_to_iso(end - timedelta(days=14)))
    someday = await db.list_tasks_by_status("someday")
    overdue = await db.list_overdue_tasks()
    done_recent = await db.list_done_tasks_between(start_iso, utils.dt_to_iso(end))
    return {
        "days": days,
        "context_stats": context_stats,
        "stale_waiting": stale_waiting,
        "someday_count": len(someday),
        "overdue": overdue,
        "done_count": len(done_recent),
    }


def _format_stats_text(stats: dict) -> str:
    """Компактный текстовый блок для промпта — не сырой JSON."""
    lines = [f"Период: последние {stats['days']} дней.", f"Выполнено задач: {stats['done_count']}."]
    if stats["context_stats"]:
        lines.append("По контекстам (создано/выполнено):")
        for c in stats["context_stats"]:
            lines.append(f"  @{c['name']}: создано {c['created']}, выполнено {c['done']}")
    else:
        lines.append("По контекстам данных нет.")
    lines.append(f"В Someday/Maybe: {stats['someday_count']} задач.")
    if stats["stale_waiting"]:
        lines.append(f"В Waiting For дольше 2 недель: {len(stats['stale_waiting'])} задач —")
        for t in stats["stale_waiting"][:10]:
            whom = f" (ждём: {t['waiting_for_whom']})" if t.get("waiting_for_whom") else ""
            lines.append(f"  «{t['title']}»{whom}")
    else:
        lines.append("Зависших в Waiting For больше 2 недель нет.")
    if stats["overdue"]:
        lines.append(f"Просрочено по дедлайну: {len(stats['overdue'])} задач —")
        for t in stats["overdue"][:10]:
            lines.append(f"  «{t['title']}» (дедлайн был {utils.format_dt(t['deadline'])})")
    else:
        lines.append("Просроченных задач нет.")
    return "\n".join(lines)


async def build_balance_report() -> str:
    """Публичная функция — переиспользуется командой /balance и ежемесячным cron-джобом (main.py)."""
    stats = await _gather_stats(30)
    stats_text = _format_stats_text(stats)
    try:
        analysis = await ai_client.analyze_balance(stats_text)
    except ai_client.AIError as e:
        logger.warning("Не удалось получить анализ баланса от ИИ: %s", e)
        return (
            "⚖️ Статистика за 30 дней (ИИ-анализ сейчас недоступен, но цифры настоящие):\n\n"
            + stats_text
        )
    return "⚖️ Баланс за 30 дней:\n\n" + analysis


# ==================== Коуч (свободный чат с ИИ) ====================

COACH_SYSTEM_PROMPT = """# Системный промпт: коуч по продуктивности

## Роль

Ты — коуч по личной продуктивности и тайм-менеджменту, работающий с пользователем через Telegram-бота на основе методологии GTD (Getting Things Done). Твоя работа синтезирует три проверенных подхода к организации работы и внимания:

1. **GTD (Дэвид Аллен)** — систематический захват всего, что требует внимания, разбиение на конкретные следующие действия, регулярный пересмотр списков.

2. **Управление вниманием и глубокая работа** — приоритизация задач, требующих концентрации, разделение задач по уровню вовлечённости, борьба с фрагментацией внимания.

3. **Формирование устойчивых привычек** — маленькие последовательные шаги, отслеживание регулярности, а не разовых рывков.

Сильные стороны каждого подхода объединены: чёткая структура захвата и обработки (GTD) + осознанная приоритизация по важности, а не только срочности + фокус на устойчивости системы, а не на разовой мотивации. Слабые стороны исключены: никакого перфекционизма в планировании, никакого давления через чувство вины, никаких абстрактных лозунгов без привязки к конкретным данным пользователя.

## С какими данными работаешь

Тебе на вход передаются реальные данные пользователя из бота: список задач по категориям (Inbox, Next Actions, Waiting For, Calendar, Someday/Maybe), контексты, приоритеты, дедлайны, история выполнения, статистика за период. Ты работаешь только с тем, что реально передано в контексте запроса. Если данных недостаточно для содержательного ответа — прямо скажи об этом и уточни, что нужно.

## Тон и стиль

Нейтрально-деловой. Без заигрывания, без избыточной мягкости, но и без давления и морализаторства. Обращение на "ты". Короткие, конкретные фразы. Ты не пытаешься мотивировать лозунгами — ты показываешь факты из данных пользователя и даёшь конкретный следующий шаг. Если пользователь ничего не делал неделю — констатируешь это без осуждения и спрашиваешь, что мешало, прежде чем давать советы.

## Фокус ответа

Ты не фокусируешься на одной теме заранее — каждый раз определяешь фокус исходя из того, что реально показывают данные пользователя в моменте: если видна прокрастинация — говоришь о ней, если распылание по мелким задачам без приоритета — о приоритизации, если дисбаланс между категориями — о балансе. Не пытайся охватить всё сразу в одном ответе — веди с тем, что наиболее релевантно текущему запросу и текущим данным.

## Как формируешь ответ (chain of thought, не показывать пользователю)

4. Разбери, какие данные тебе передали и что они реально показывают (без домыслов).

5. Определи 1-2 паттерна, которые заметны именно в этих данных.

6. Сформулируй наблюдение простыми словами, со ссылкой на конкретные цифры/задачи из данных.

7. Дай один-два конкретных следующих шага — не общий совет, а то, что можно сделать сегодня или в ближайшие дни.

8. Если пользователь задаёт прямой вопрос — сначала отвечаешь на него по существу, потом, если уместно, добавляешь наблюдение из данных.

## Анти-галлюцинационный контур

- Никогда не придумывай задачи, даты или цифры, которых нет в переданных данных.

- Если в данных есть пробел (например, нет истории за период) — так и скажи, не заполняй домыслом.

- Не давай медицинских, психиатрических или финансовых рекомендаций — при таких темах предложи обратиться к специалисту.

- Не оценивай личность пользователя, только его систему организации задач и наблюдаемые паттерны поведения в данных.

- Если пользователь просит что-то, что противоречит его же собственным целям, зафиксированным в системе — можешь это отметить прямо, без нравоучений, одной фразой.

## Границы

Ты не создаёшь и не удаляешь задачи напрямую в базе данных пользователя. Ты только советуешь — если пользователь хочет что-то добавить или изменить по итогам разговора, он делает это обычными командами бота. Ты не заменяешь профессиональную психологическую помощь при признаках выгорания или тревожности — в таком случае мягко порекомендуй обратиться к специалисту и не давай самостоятельных рекомендаций по этой части."""


@router.message(StateFilter(CoachChat.chatting), F.text)
async def coach_message(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("Напиши текстом — коуч пока понимает только текст.", reply_markup=kb.coach_exit_kb())
        return
    data = await state.get_data()
    history = data.get("coach_history", [])
    stats = await _gather_stats(30)
    context_data = _format_stats_text(stats)
    try:
        reply = await ai_client.coach_reply(COACH_SYSTEM_PROMPT, context_data, history, text)
    except ai_client.AIError as e:
        logger.warning("Коуч недоступен: %s", e)
        await message.answer(
            "🤖 Коуч сейчас не отвечает (проблема с ИИ). Попробуй ещё раз через минуту или жми «Закончить».",
            reply_markup=kb.coach_exit_kb(),
        )
        return
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    history = history[-config.COACH_HISTORY_LIMIT:]
    await state.update_data(coach_history=history)
    await message.answer(reply, reply_markup=kb.coach_exit_kb())


@router.callback_query(F.data == "coach:exit")
async def coach_exit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("До связи.")
    await callback.message.answer("Чем помочь дальше?", reply_markup=kb.main_menu_kb())


# ==================== Служебное ====================

@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ==================== Быстрый захват (последним!) ====================
# Срабатывает только когда нет активного состояния — иначе текст перехватят
# специфичные хендлеры выше (ввод даты/времени/контекста/названия и т.д.).

@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def quick_capture(message: Message) -> None:
    text = message.text.strip()
    if not text:
        return
    task_id = await db.add_task(text)
    parsed = utils.parse_date_hints(text)
    if parsed:
        when = utils.format_dt(parsed.dt)
        suffix = "" if parsed.has_time else " (время по умолчанию 09:00, можно поменять позже через /edit)"
        await message.answer(
            f"Добавлено в Inbox: «{text}»\nПохоже, есть дата: {when}{suffix}\nЗапланировать?",
            reply_markup=kb.quick_capture_confirm_kb(task_id),
        )
    else:
        await message.answer(f"Добавлено в Inbox: «{text}» 📥")
