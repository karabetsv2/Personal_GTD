"""Единая точка обращения к OpenRouter (OpenAI-совместимый chat completions API).

Все три AI-функции бота (разбивка проекта, анализ баланса, коуч) идут
через _chat(). Наружу — только AIError: сетевые сбои, отсутствие ключа,
HTTP-ошибки и невалидный ответ модели всегда заворачиваются в неё, чтобы
вызывающий код (handlers.py) мог показать пользователю понятное сообщение
и предложить действовать без ИИ, не роняя бота.
"""
import json
import logging
import re
import time

import aiohttp

import config

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class AIError(Exception):
    """Показывается пользователю как понятное сообщение о недоступности ИИ."""


async def _chat(
    messages: list[dict],
    model: str,
    temperature: float,
    *,
    json_mode: bool = False,
    max_tokens: int = 800,
) -> str:
    """Низкоуровневый вызов OpenRouter. Логирует только факт вызова, без содержимого."""
    if not config.OPENROUTER_API_KEY:
        raise AIError("Не задан OPENROUTER_API_KEY")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "Personal GTD Bot",
    }

    started = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=config.AI_REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENROUTER_URL, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    err = (body or {}).get("error", {}).get("message", f"HTTP {resp.status}")
                    raise AIError(err)
        content = body["choices"][0]["message"]["content"]
    except AIError:
        logger.warning(
            "OpenRouter FAIL model=%s took=%.2fs", model, time.monotonic() - started
        )
        raise
    except Exception as e:
        logger.warning(
            "OpenRouter FAIL model=%s took=%.2fs err=%s",
            model, time.monotonic() - started, type(e).__name__,
        )
        raise AIError("Не удалось получить ответ от ИИ") from e

    logger.info(
        "OpenRouter OK model=%s temperature=%s took=%.2fs",
        model, temperature, time.monotonic() - started,
    )
    return content


def _parse_actions_json(raw: str) -> list[str]:
    """Достаёт список действий из ответа модели, даже если JSON обёрнут текстом/разметкой."""
    data = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return []
    actions = data.get("actions")
    if not isinstance(actions, list):
        return []
    return [str(a).strip() for a in actions if str(a).strip()]


async def suggest_next_actions(title: str, description: str | None) -> list[str]:
    """Функция 1: черновик 3-5 следующих действий для задачи-проекта."""
    system = (
        "Ты помогаешь разбить задачу-проект на конкретные следующие действия "
        "по методологии GTD (Getting Things Done). Каждое действие — один "
        "конкретный физический шаг, который можно выполнить, без общих фраз "
        "и без промежуточных 'подготовить'/'продумать', если это не единственно "
        "возможный первый шаг. Дай от 3 до 5 действий.\n"
        'Ответь строго в формате JSON без пояснений вокруг: {"actions": ["действие 1", "действие 2", ...]}'
    )
    user_content = f"Задача-проект: {title}"
    if description:
        user_content += f"\nОписание: {description}"

    raw = await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        model=config.AI_MODEL_BREAKDOWN,
        temperature=config.AI_TEMPERATURE_BREAKDOWN,
        json_mode=True,
        max_tokens=500,
    )
    actions = _parse_actions_json(raw)
    if not actions:
        raise AIError("ИИ вернул пустой или нераспознаваемый ответ")
    return actions[:5]


async def analyze_balance(stats_text: str) -> str:
    """Функция 2: короткий отчёт по перекосам на основе статистики за период."""
    system = (
        "Ты — аналитик личной продуктивности пользователя, который ведёт задачи "
        "по системе GTD в Telegram-боте. Тебе дана статистика за последние 30 дней. "
        "Составь короткий отчёт (не больше 150 слов): где заметен перекос "
        "(например, задачи копятся в одном списке, много просрочек, что-то долго "
        "висит в ожидании, слишком много уходит в Someday/Maybe), и 1-2 конкретных "
        "предложения, что стоит пересмотреть. Пиши по цифрам, без давления и "
        "морализаторства, на 'ты', по-русски."
    )
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": stats_text},
        ],
        model=config.AI_MODEL_BALANCE,
        temperature=config.AI_TEMPERATURE_BALANCE,
        max_tokens=600,
    )


async def coach_reply(
    system_prompt: str,
    context_data: str,
    history: list[dict],
    user_message: str,
) -> str:
    """Функция 3: ответ коуча с учётом системного промпта, данных пользователя и истории."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Актуальные данные пользователя:\n{context_data}"},
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return await _chat(
        messages,
        model=config.AI_MODEL_COACH,
        temperature=config.AI_TEMPERATURE_COACH,
        max_tokens=800,
    )
