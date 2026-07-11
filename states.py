"""FSM-состояния бота.

Состояния заводятся только там, где нужно перехватить следующее текстовое
сообщение (свободный ввод) и понять, к какому сценарию оно относится.
Чисто кнопочные шаги (выбор даты/времени по кнопкам, тумблеры контекстов,
приоритет, пагинация списков) обходятся без State — вся нужная информация
(task_id, к какому сценарию относится шаг — 'process' или 'edit', текущий
набор выбранных контекстов и т.п.) хранится в FSMContext.data, которое
доступно независимо от того, установлен ли State.
"""
from aiogram.fsm.state import State, StatesGroup


class ProcessInbox(StatesGroup):
    entering_waiting_whom = State()    # "кого/что ждём" — общее для /process и /edit (data["flow"])


class ScheduleTask(StatesGroup):
    """Общее для /process («Запланировать») и /edit («Дедлайн»), см. data["flow"]."""
    entering_custom_date = State()
    entering_custom_time = State()


class ContextPick(StatesGroup):
    """Общее для /process («Следующее действие») и /edit («Контексты»), см. data["flow"]."""
    entering_custom_context = State()


class EditTask(StatesGroup):
    entering_title = State()
    entering_description = State()


class SearchTask(StatesGroup):
    entering_query = State()
