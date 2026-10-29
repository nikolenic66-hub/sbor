from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
from html import escape
from pathlib import PurePath
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import (
    get_admin_ids, get_backup_directory, get_backup_interval_hours,
    get_backup_retention, get_bot_token, get_database_path, get_lead_retention_days, load_defaults,
)
from database import Database
from safety import is_private_admin, safe_csv_cell
from keyboards import (
    BTN_ADMIN, BTN_CONTACTS, BTN_NEW_LEAD, BTN_SERVICES, admin_keyboard,
    admin_settings_keyboard, backup_confirm_keyboard, backup_list_keyboard, retention_confirm_keyboard,
    lead_actions_keyboard, lead_list_keyboard, main_menu, service_keyboard, skip_keyboard,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("sbor")
router = Router()


class LeadForm(StatesGroup):
    name = State()
    contact = State()
    description = State()
    budget = State()
    deadline = State()
    custom_answer = State()


class AdminSettingsFlow(StatesGroup):
    waiting_value = State()


class AdminSearchFlow(StatesGroup):
    waiting_query = State()


class BackupUploadFlow(StatesGroup):
    waiting_file = State()


DB: Database
ADMIN_IDS: set[int]
BACKUP_INTERVAL_HOURS: int
LEAD_RETENTION_DAYS: int
MAX_BACKUP_UPLOAD_BYTES = 19 * 1024 * 1024
MAX_BACKUP_TELEGRAM_BYTES = 45 * 1024 * 1024

STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "completed": "Выполнена",
    "cancelled": "Отменена",
}
NOTIFICATION_LABELS = {
    "pending": "ещё не проверено",
    "sent": "доставлено всем администраторам",
    "partial": "доставлено не всем администраторам",
    "failed": "не доставлено ни одному администратору",
    "unknown": "статус неизвестен (старая заявка)",
}
SETTING_LABELS = {
    "company_name": "название компании",
    "welcome_text": "текст приветствия",
    "contact_text": "контактный текст",
    "services": "список услуг",
    "custom_questions": "дополнительные вопросы",
    "privacy_notice": "уведомление о данных",
}


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def setting(key: str, fallback: Any = None) -> Any:
    return await DB.get_setting(key, fallback)


def escape_text(value: Any, max_length: int | None = None) -> str:
    raw = str(value if value is not None else "—")
    if max_length is not None and len(raw) > max_length:
        raw = raw[:max_length] + "…"
    return escape(raw)


def private_admin_message(message: Message) -> bool:
    return is_private_admin(
        message.from_user.id if message.from_user else None,
        message.chat.type,
        ADMIN_IDS,
    )


async def require_admin_callback(callback: CallbackQuery) -> bool:
    chat = getattr(callback.message, "chat", None) if callback.message is not None else None
    chat_type = getattr(chat, "type", None)
    if is_private_admin(callback.from_user.id, chat_type, ADMIN_IDS):
        return True
    await callback.answer(
        "Админ-панель доступна только администратору в личном чате с ботом.",
        show_alert=True,
    )
    return False


async def admin_menu_keyboard():
    return admin_keyboard(await DB.count_notification_issues())


def format_lead(lead: dict[str, Any], *, show_notification_status: bool = True) -> str:
    # Telegram limits messages to 4096 characters. Keep the admin view bounded;
    # full original text remains available in the CSV export.
    username = f"@{lead['username']}" if lead.get("username") else "не указан"
    answers = lead.get("custom_answers") or {}
    answer_text = ""
    if answers:
        answer_text = "\n\n<b>Дополнительные ответы:</b>"
        for index, (question, answer) in enumerate(answers.items()):
            if index >= 2:
                answer_text += "\n<i>Остальные ответы доступны в CSV.</i>"
                break
            answer_text += f"\n• {escape_text(question, 25)}: {escape_text(answer, 30)}"
    return (
        f"<b>Заявка #{lead['id']}</b>\n"
        f"<b>Статус:</b> {STATUS_LABELS.get(lead['status'], lead['status'])}\n"
        f"<b>Создана:</b> {escape_text(lead['created_at'], 25)}\n\n"
        f"<b>Имя:</b> {escape_text(lead['full_name'], 35)}\n"
        f"<b>Telegram:</b> {escape_text(username, 30)}\n"
        f"<b>Услуга:</b> {escape_text(lead['service'], 30)}\n"
        f"<b>Контакт:</b> {escape_text(lead['contact'], 40)}\n"
        f"<b>Описание:</b> {escape_text(lead['description'], 250)}\n"
        f"<b>Бюджет:</b> {escape_text(lead['budget'] or 'не указан', 25)}\n"
        f"<b>Срок:</b> {escape_text(lead['deadline'] or 'не указан', 25)}"
        + (f"\n<b>Уведомления:</b> {NOTIFICATION_LABELS.get(lead.get('notification_status', 'unknown'), 'неизвестно')}" if show_notification_status else "")
        + f"{answer_text}"
    )


async def send_admin_notification(
    bot: Bot, lead: dict[str, Any], *, retry_failed: bool = False
) -> str:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    delivered = set(int(x) for x in lead.get("notification_delivered_admins", []))
    previous_failed = set(int(x) for x in lead.get("notification_failed_admins", []))
    recipients = (
        previous_failed
        if retry_failed and previous_failed
        else (set(ADMIN_IDS) if not retry_failed or lead.get("notification_status") == "pending" else set())
    )
    if not recipients:
        return str(lead.get("notification_status", "unknown"))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Открыть заявку #{lead['id']}", callback_data=f"admin:view:{lead['id']}:new")]
    ])
    failed = set(previous_failed if retry_failed else ())
    errors: list[str] = []
    for admin_id in sorted(recipients):
        try:
            await bot.send_message(
                admin_id,
                f"🆕 <b>Новая заявка</b>\n\n{format_lead(lead, show_notification_status=False)}",
                reply_markup=keyboard,
            )
            delivered.add(admin_id)
            failed.discard(admin_id)
        except TelegramAPIError as exc:
            failed.add(admin_id)
            errors.append(f"{admin_id}: {type(exc).__name__}")
            log.warning(
                "Не удалось отправить уведомление о заявке #%s администратору %s (%s)",
                lead["id"], admin_id, type(exc).__name__,
            )

    if not failed:
        status = "sent"
    elif delivered:
        status = "partial"
    else:
        status = "failed"

    await DB.update_notification_state(
        int(lead["id"]), sorted(delivered), sorted(failed), status, "; ".join(errors)
    )
    if failed:
        log.error(
            "Для заявки #%s остались недоставленные уведомления: %s",
            lead["id"], sorted(failed),
        )
    return status


async def show_service_picker(message: Message, state: FSMContext) -> None:
    services = await setting("services", [])
    services = services if isinstance(services, list) else []
    services = [str(item).strip() for item in services if str(item).strip()][:40]
    if not services:
        await message.answer("Список услуг пока не настроен. Обратитесь к владельцу бота.")
        return
    await state.clear()
    await message.answer("Выберите, по какому вопросу хотите обратиться:", reply_markup=service_keyboard(services))


async def ask_budget(message: Message, state: FSMContext) -> None:
    await state.set_state(LeadForm.budget)
    await message.answer("Какой у вас ориентировочный бюджет? Можно пропустить.", reply_markup=skip_keyboard("budget"))


async def ask_deadline(message: Message, state: FSMContext) -> None:
    await state.set_state(LeadForm.deadline)
    await message.answer("Когда нужна услуга или результат? Можно пропустить.", reply_markup=skip_keyboard("deadline"))


async def ask_custom_question_or_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    questions = await setting("custom_questions", [])
    questions = [str(q).strip() for q in questions if str(q).strip()][:8] if isinstance(questions, list) else []
    await state.update_data(custom_questions=questions, custom_answers={}, custom_index=0)
    if questions:
        await state.set_state(LeadForm.custom_answer)
        await message.answer(f"{escape_text(questions[0], 300)}\n\n<i>Вопрос 1 из {len(questions)}. Для отмены отправьте /cancel.</i>")
        return
    await finalize_lead(message, state, bot)


async def finalize_lead(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user = message.from_user
    if user is None:
        await state.clear()
        return
    # The form may finish from an inline-button callback; in that case the
    # Message author is the bot, so preserve the client ID saved at form start.
    data["user_id"] = int(data.get("user_id") or user.id)
    data["username"] = str(data.get("username") or user.username or "")
    lead_id = await DB.create_lead(data)
    lead = await DB.get_lead(lead_id)
    await state.clear()
    if lead is None:
        await message.answer("Заявка отправлена, но не удалось получить её номер. Сохраните это сообщение и свяжитесь с владельцем.")
        return
    company = escape_text(await setting("company_name", "Компания"))
    await message.answer(
        f"✅ <b>Заявка #{lead_id} принята!</b>\n\n"
        f"Спасибо, {escape_text(lead['full_name'])}. Заявка передана ответственному менеджеру.\n"
        f"Компания: <b>{company}</b>",
        reply_markup=main_menu(is_admin(int(data["user_id"]))),
    )
    await send_admin_notification(bot, lead)


async def render_lead(callback: CallbackQuery, lead_id: int, back_to: str) -> None:
    lead = await DB.get_lead(lead_id)
    if not lead:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await callback.message.edit_text(format_lead(lead), reply_markup=lead_actions_keyboard(lead_id, back_to))
    await callback.answer()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    company = escape_text(await setting("company_name", "Заявки компании"))
    welcome = escape_text(await setting("welcome_text", "Оставьте заявку через меню."))
    privacy = escape_text(await setting("privacy_notice", ""))
    text = f"<b>{company}</b>\n\n{welcome}"
    if privacy:
        text += f"\n\n<i>{privacy}</i>"
    await message.answer(text, reply_markup=main_menu(is_admin(message.from_user.id if message.from_user else None)))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu(is_admin(message.from_user.id if message.from_user else None)))


@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN)
async def admin_home(message: Message, state: FSMContext) -> None:
    if not private_admin_message(message):
        await message.answer("Админ-панель доступна только администратору в личном чате с ботом.")
        return
    await state.clear()
    company = escape_text(await setting("company_name", "Компания"))
    await message.answer(
        f"<b>Админ-панель · {company}</b>\nВыберите действие:",
        reply_markup=await admin_menu_keyboard(),
    )


@router.message(F.text == BTN_NEW_LEAD)
async def new_lead(message: Message, state: FSMContext) -> None:
    await show_service_picker(message, state)


@router.message(F.text == BTN_SERVICES)
async def services(message: Message) -> None:
    items = await setting("services", [])
    items = items if isinstance(items, list) else []
    if not items:
        await message.answer("Список услуг пока не настроен.")
        return
    content = "<b>Доступные услуги</b>\n\n" + "\n".join(f"• {escape_text(x)}" for x in items[:40])
    await message.answer(content)


@router.message(F.text == BTN_CONTACTS)
async def contacts(message: Message) -> None:
    content = escape_text(await setting("contact_text", "Контакты пока не настроены."))
    await message.answer(content)


@router.callback_query(F.data == "lead:cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Заявка отменена")
    await callback.message.answer("Заявка отменена. При необходимости можно начать заново.", reply_markup=main_menu(is_admin(callback.from_user.id)))


@router.callback_query(F.data.startswith("lead:service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    services_list = await setting("services", [])
    services_list = [str(item).strip() for item in services_list if str(item).strip()] if isinstance(services_list, list) else []
    services_list = services_list[:40]
    if index < 0 or index >= len(services_list):
        await callback.answer("Услуга больше недоступна. Начните заявку заново.", show_alert=True)
        return
    await state.clear()
    await state.update_data(service=str(services_list[index]), user_id=callback.from_user.id,
                            username=callback.from_user.username or "")
    await state.set_state(LeadForm.name)
    await callback.answer()
    await callback.message.answer("Как к вам обращаться? Напишите имя или название компании.")


@router.message(LeadForm.name)
async def receive_name(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 2 or len(value) > 100:
        await message.answer("Укажите имя длиной от 2 до 100 символов.")
        return
    await state.update_data(full_name=value)
    await state.set_state(LeadForm.contact)
    await message.answer("Как с вами связаться? Можно указать телефон, @username или другой удобный способ.")


@router.message(LeadForm.contact)
async def receive_contact(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 3 or len(value) > 200:
        await message.answer("Укажите способ связи длиной от 3 до 200 символов.")
        return
    await state.update_data(contact=value)
    await state.set_state(LeadForm.description)
    await message.answer("Кратко опишите, что вам нужно. Не отправляйте пароли или лишние личные данные.")


@router.message(LeadForm.description)
async def receive_description(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 5 or len(value) > 4000:
        await message.answer("Описание должно содержать от 5 до 4000 символов.")
        return
    await state.update_data(description=value)
    await ask_budget(message, state)


@router.message(LeadForm.budget)
async def receive_budget(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value.lower() in {"/skip", "пропустить", "—", "-"}:
        value = ""
    if len(value) > 300:
        await message.answer("Ответ слишком длинный. Укажите бюджет кратко или нажмите «Пропустить».")
        return
    await state.update_data(budget=value)
    await ask_deadline(message, state)


@router.message(LeadForm.deadline)
async def receive_deadline(message: Message, state: FSMContext, bot: Bot) -> None:
    value = (message.text or "").strip()
    if value.lower() in {"/skip", "пропустить", "—", "-"}:
        value = ""
    if len(value) > 300:
        await message.answer("Ответ слишком длинный. Укажите срок кратко или нажмите «Пропустить».")
        return
    await state.update_data(deadline=value)
    await ask_custom_question_or_finish(message, state, bot)


@router.callback_query(F.data.startswith("lead:skip:"))
async def skip_field(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    field_name = callback.data.rsplit(":", 1)[1]
    current = await state.get_state()
    if field_name == "budget" and current == LeadForm.budget.state:
        await state.update_data(budget="")
        await callback.answer()
        await ask_deadline(callback.message, state)
    elif field_name == "deadline" and current == LeadForm.deadline.state:
        await state.update_data(deadline="")
        await callback.answer()
        await ask_custom_question_or_finish(callback.message, state, bot)
    else:
        await callback.answer("Этап уже изменился. Продолжите с последнего сообщения.", show_alert=True)


@router.message(LeadForm.custom_answer)
async def receive_custom_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    answer = (message.text or "").strip()
    if not answer:
        await message.answer("Напишите ответ текстом.")
        return
    if len(answer) > 2000:
        await message.answer("Ответ слишком длинный — максимум 2000 символов.")
        return
    data = await state.get_data()
    questions = data.get("custom_questions", [])
    answers = data.get("custom_answers", {})
    index = int(data.get("custom_index", 0))
    if index >= len(questions):
        await finalize_lead(message, state, bot)
        return
    answers[str(questions[index])] = answer
    index += 1
    await state.update_data(custom_answers=answers, custom_index=index)
    if index < len(questions):
        await message.answer(f"{escape_text(questions[index], 300)}\n\n<i>Вопрос {index + 1} из {len(questions)}. Для отмены отправьте /cancel.</i>")
        return
    await finalize_lead(message, state, bot)


@router.callback_query(F.data == "admin:home")
async def admin_home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.clear()
    company = escape_text(await setting("company_name", "Компания"))
    await callback.message.edit_text(
        f"<b>Админ-панель · {company}</b>\nВыберите действие:",
        reply_markup=await admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:leads:"))
async def admin_list_leads(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    parts = callback.data.split(":")
    filter_value = parts[2] if len(parts) > 2 else ""
    try:
        offset = max(0, int(parts[3])) if len(parts) > 3 else 0
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    titles = {
        "all": "Все заявки",
        "new": "Новые заявки",
        "in_progress": "Заявки в работе",
        "completed": "Выполненные заявки",
        "cancelled": "Отменённые заявки",
    }
    if filter_value not in titles:
        await callback.answer("Неизвестный фильтр", show_alert=True)
        return
    status = None if filter_value == "all" else filter_value
    total = await DB.count_leads(status)
    if offset >= total and offset > 0:
        offset = max(0, ((total - 1) // 10) * 10) if total else 0
    items = await DB.list_leads(status=status, limit=10, offset=offset)
    caption = f"<b>{escape_text(titles[filter_value])}</b>"
    if not items:
        await callback.message.edit_text(
            caption + "\n\nЗаявок пока нет.",
            reply_markup=await admin_menu_keyboard(),
        )
    else:
        caption += f"\nПоказаны {offset + 1}–{offset + len(items)} из {total}. Нажмите на заявку:"
        await callback.message.edit_text(
            caption,
            reply_markup=lead_list_keyboard(
                items, filter_value, offset,
                has_prev=offset > 0,
                has_next=(offset + len(items)) < total,
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:view:"))
async def admin_view_lead(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    parts = callback.data.split(":")
    try:
        lead_id = int(parts[2])
        back_to = parts[3] if len(parts) > 3 and parts[3] in {"new", "all", "in_progress", "completed", "cancelled"} else "all"
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер заявки", show_alert=True)
        return
    await render_lead(callback, lead_id, back_to)


@router.callback_query(F.data.startswith("admin:status:"))
async def admin_update_status(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    parts = callback.data.split(":")
    try:
        lead_id, status = int(parts[2]), parts[3]
    except (ValueError, IndexError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if status not in STATUS_LABELS:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    changed = await DB.update_status(lead_id, status)
    if not changed:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await callback.answer(f"Статус: {STATUS_LABELS[status]}")
    lead = await DB.get_lead(lead_id)
    if lead:
        await callback.message.edit_text(format_lead(lead), reply_markup=lead_actions_keyboard(lead_id, "all"))


@router.callback_query(F.data == "admin:search")
async def admin_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.set_state(AdminSearchFlow.waiting_query)
    await callback.message.answer(
        "🔎 <b>Поиск заявок</b>\nОтправьте номер заявки (например, <code>#24</code>) "
        "или часть имени/контакта клиента.\nДля отмены отправьте /cancel."
    )
    await callback.answer()


@router.message(AdminSearchFlow.waiting_query)
async def admin_search_run(message: Message, state: FSMContext) -> None:
    if not private_admin_message(message):
        await state.clear()
        await message.answer("Поиск доступен только администратору в личном чате с ботом.")
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите номер заявки, имя или контакт.")
        return
    if len(query) > 100:
        await message.answer("Запрос слишком длинный. Максимум 100 символов.")
        return
    items = await DB.search_leads(query, limit=10)
    await state.clear()
    if not items:
        await message.answer(
            f"По запросу «{escape_text(query, 100)}» ничего не найдено.",
            reply_markup=await admin_menu_keyboard(),
        )
        return
    await message.answer(
        f"🔎 <b>Результаты поиска</b>\nЗапрос: «{escape_text(query, 100)}». Показаны первые {len(items)} результатов.",
        reply_markup=lead_list_keyboard(items, "all", 0, has_prev=False, has_next=False),
    )


@router.callback_query(F.data == "admin:notifications")
async def admin_notifications(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    count = await DB.count_notification_issues()
    issues = await DB.list_notification_issues(limit=10)
    if not issues:
        await callback.message.edit_text(
            "✅ <b>Проблем с уведомлениями нет.</b>\n\n"
            "Перед запуском проверьте, что каждый администратор открыл личный чат с ботом и отправил /start.",
            reply_markup=await admin_menu_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"⚠️ <b>Проблемы доставки уведомлений: {count}</b>\n\n"
            "Откройте заявку и повторите отправку. При повторе бот отправляет сообщение только администраторам, "
            "которым не удалось доставить его ранее. Показаны последние 10 заявок.",
            reply_markup=notification_list_keyboard(issues),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:notify-retry:"))
async def admin_notify_retry(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_admin_callback(callback):
        return
    try:
        lead_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер заявки", show_alert=True)
        return
    lead = await DB.get_lead(lead_id)
    if not lead:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await callback.answer("Проверяю доставку…")
    await send_admin_notification(bot, lead, retry_failed=True)
    updated = await DB.get_lead(lead_id)
    if updated:
        await callback.message.edit_text(
            format_lead(updated),
            reply_markup=lead_actions_keyboard(lead_id, "all"),
        )


@router.callback_query(F.data == "admin:backup:create")
async def admin_backup_create(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_admin_callback(callback):
        return
    await callback.answer("Создаю резервную копию…")
    try:
        backup = await DB.create_backup()
    except Exception:
        log.exception("Не удалось создать резервную копию базы")
        await callback.message.answer(
            "❌ Не удалось создать резервную копию. Проверьте доступность постоянного диска хостинга."
        )
        return

    size = int(backup["size_bytes"])
    size_text = f"{size / (1024 * 1024):.2f} МБ" if size >= 1024 * 1024 else f"{max(1, size // 1024)} КБ"
    if size > MAX_BACKUP_TELEGRAM_BYTES:
        await callback.message.answer(
            f"✅ Копия создана на сервере: <code>{escape_text(backup['filename'])}</code> ({size_text}).\n"
            "Файл слишком большой для безопасной отправки через этот бот. Для защиты от потери всего хранилища "
            "нужна отдельная выгрузка файла из хранилища хостинга."
        )
        return

    try:
        path = DB.backup_dir / backup["filename"]
        document = BufferedInputFile(path.read_bytes(), filename=backup["filename"])
        await bot.send_document(
            callback.from_user.id,
            document,
            caption=(
                "✅ Резервная копия базы создана.\n"
                f"Файл: {escape_text(backup['filename'])}\n"
                f"Размер: {size_text}\n\n"
                "Скачайте этот файл на устройство или в другое надёжное хранилище. "
                "Локальная копия на том же хостинге не защищает от потери всего диска."
            ),
        )
    except (OSError, TelegramAPIError):
        log.exception("Резервная копия создана, но не удалось отправить её администратору")
        await callback.message.answer(
            f"✅ Копия сохранена на сервере ({size_text}), но отправить файл в Telegram не удалось. "
            "Откройте список резервных копий и проверьте доступность файла."
        )


@router.callback_query(F.data == "admin:backup:list")
async def admin_backup_list(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    backups = await DB.list_backups()
    if not backups:
        text = (
            "<b>Резервные копии</b>\n\n"
            "Пока нет ни одной корректной копии. Создайте её кнопкой в админ-панели "
            "или загрузите файл резервной копии с устройства."
        )
    else:
        text = (
            f"<b>Резервные копии: {len(backups)}</b>\n\n"
            "Выберите копию для восстановления. Перед восстановлением текущая база автоматически сохраняется "
            "в отдельную резервную копию. Для восстановления после потери хостинга загрузите копию с устройства."
        )
    await callback.message.edit_text(text, reply_markup=backup_list_keyboard(backups))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:backup:restore:"))
async def admin_backup_restore_preview(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    filename = callback.data.split(":", 3)[3]
    backups = await DB.list_backups()
    selected = next((item for item in backups if item["filename"] == filename), None)
    if selected is None:
        await callback.answer("Эта копия не найдена или не прошла проверку целостности.", show_alert=True)
        return
    size = int(selected["size_bytes"])
    size_text = f"{size / (1024 * 1024):.2f} МБ" if size >= 1024 * 1024 else f"{max(1, size // 1024)} КБ"
    await callback.message.edit_text(
        "⚠️ <b>Подтвердите восстановление базы</b>\n\n"
        f"Копия: <code>{escape_text(filename)}</code>\n"
        f"Создана: {escape_text(selected['created_at'])}\n"
        f"Размер: {size_text}\n\n"
        "Текущая база будет сохранена перед восстановлением. "
        "Заявки и настройки, появившиеся после выбранной копии, исчезнут из активной базы. "
        "Продолжайте только если выбрана правильная дата.",
        reply_markup=backup_confirm_keyboard(filename),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:backup:confirm:"))
async def admin_backup_restore_confirm(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    filename = callback.data.split(":", 3)[3]
    await callback.answer("Проверяю копию и восстанавливаю базу…")
    try:
        result = await DB.restore_backup(filename)
    except (ValueError, OSError) as exc:
        await callback.message.answer(f"❌ Восстановление не выполнено: {escape_text(exc, 350)}")
        return
    except Exception:
        log.exception("Ошибка восстановления резервной копии %s", filename)
        await callback.message.answer(
            "❌ Восстановление не завершилось. Текущая база предварительно сохранена; "
            "проверьте журнал и используйте созданную перед восстановлением копию."
        )
        return

    await callback.message.edit_text(
        "✅ <b>База восстановлена</b>\n\n"
        f"Источник: <code>{escape_text(result['restored_from'])}</code>\n"
        f"Заявок в восстановленной копии: <b>{result['lead_count']}</b>\n"
        f"Настроек: <b>{result['setting_count']}</b>\n\n"
        "Текущее состояние перед восстановлением сохранено в копию:\n"
        f"<code>{escape_text(result['pre_restore_backup'])}</code>",
        reply_markup=await admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:backup:upload")
async def admin_backup_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    await state.set_state(BackupUploadFlow.waiting_file)
    await callback.message.answer(
        "📥 <b>Загрузка резервной копии</b>\n\n"
        "Прикрепите SQLite-файл с расширением <code>.sqlite3</code>, <code>.sqlite</code> или <code>.db</code>. "
        "Максимальный размер — 19 МБ.\n"
        "Файл будет проверен на целостность и структуру; активная база не изменится до подтверждения восстановления.\n"
        "Для отмены отправьте /cancel."
    )
    await callback.answer()


@router.message(BackupUploadFlow.waiting_file, F.document)
async def admin_backup_upload_file(message: Message, state: FSMContext, bot: Bot) -> None:
    if not private_admin_message(message):
        await state.clear()
        await message.answer("Загрузка копий доступна только администратору в личном чате с ботом.")
        return
    document = message.document
    suffix = PurePath(document.file_name or "").suffix.lower()
    if suffix not in {".sqlite3", ".sqlite", ".db"}:
        await message.answer("Неизвестный формат. Пришлите файл .sqlite3, .sqlite или .db.")
        return
    if document.file_size is None or document.file_size < 1 or document.file_size > MAX_BACKUP_UPLOAD_BYTES:
        await message.answer("Размер файла должен быть от 1 байта до 19 МБ.")
        return

    DB.backup_dir.mkdir(parents=True, exist_ok=True)
    temporary = DB.backup_dir / f".upload_{secrets.token_hex(8)}.tmp"
    try:
        await bot.download(document.file_id, destination=temporary)
        backup = await DB.import_backup(temporary)
    except (ValueError, OSError, TelegramAPIError) as exc:
        log.warning("Отклонён загруженный файл резервной копии", exc_info=True)
        await message.answer(
            f"❌ Файл не принят: {escape_text(exc, 300)}\n"
            "Проверьте, что это целый файл базы данных Sbor, а не CSV или архив."
        )
        return
    except Exception:
        log.exception("Не удалось загрузить или проверить файл резервной копии")
        await message.answer(
            "❌ Не удалось проверить файл. Активная база не изменена. "
            "Попробуйте отправить другую копию."
        )
        return
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    await state.clear()
    await message.answer(
        "✅ <b>Резервная копия проверена и сохранена.</b>\n\n"
        f"Файл: <code>{escape_text(backup['filename'])}</code>\n"
        "Активная база пока не изменена. Для восстановления нажмите кнопку ниже и подтвердите действие.",
        reply_markup=backup_confirm_keyboard(backup["filename"]),
    )


@router.message(BackupUploadFlow.waiting_file)
async def admin_backup_upload_invalid(message: Message) -> None:
    if not private_admin_message(message):
        return
    await message.answer("Пришлите файл резервной копии как документ или отправьте /cancel для отмены.")


@router.callback_query(F.data == "admin:retention:preview")
async def admin_retention_preview(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    count = await DB.count_old_closed_leads(LEAD_RETENTION_DAYS)
    await callback.message.edit_text(
        "🧹 <b>Очистка старых заявок</b>\n\n"
        f"Срок хранения закрытых заявок: <b>{LEAD_RETENTION_DAYS} дней</b>.\n"
        f"Сейчас подходят под удаление: <b>{count}</b>.\n\n"
        "Удаляются только заявки со статусом «Выполнена» или «Отменена», созданные раньше срока хранения. "
        "Новые заявки и заявки «В работе» не затрагиваются. Удаление из активной базы необратимо; "
        "копии в уже созданных резервных файлах останутся до удаления этих файлов.",
        reply_markup=retention_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:retention:confirm")
async def admin_retention_confirm(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    await callback.answer("Удаляю старые закрытые заявки…")
    try:
        result = await DB.delete_old_closed_leads(LEAD_RETENTION_DAYS)
    except Exception:
        log.exception("Не удалось удалить старые закрытые заявки")
        await callback.message.answer("❌ Очистка не выполнена. Проверьте журнал приложения.")
        return
    log.info("Privacy retention cleanup deleted %d closed leads older than %s", result["deleted"], result["cutoff"])
    await callback.message.edit_text(
        "✅ <b>Очистка завершена</b>\n\n"
        f"Удалено заявок: <b>{result['deleted']}</b>\n"
        f"Срок хранения: {LEAD_RETENTION_DAYS} дней.\n"
        f"Дата отсечения: <code>{escape_text(result['cutoff'])} UTC</code>\n\n"
        "Новые заявки и заявки «В работе» сохранены. Удалённые данные могут оставаться в старых резервных копиях.",
        reply_markup=await admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    stats = await DB.get_stats()
    await callback.message.edit_text(
        "<b>Статистика заявок</b>\n\n"
        f"Всего: <b>{stats['total']}</b>\n"
        f"🆕 Новые: {stats['new']}\n"
        f"⏳ В работе: {stats['in_progress']}\n"
        f"✅ Выполнены: {stats['completed']}\n"
        f"🚫 Отменены: {stats['cancelled']}\n"
        f"⚠️ Проблемы доставки уведомлений: <b>{await DB.count_notification_issues()}</b>",
        reply_markup=await admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:export")
async def admin_export(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_admin_callback(callback):
        return
    leads = await DB.export_leads()
    if not leads:
        await callback.answer("Пока нечего экспортировать", show_alert=True)
        return
    output = io.StringIO(newline="")
    columns = ["id", "created_at", "status", "full_name", "username", "service", "contact", "description", "budget", "deadline", "custom_answers"]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = dict(lead)
        row["custom_answers"] = " | ".join(f"{k}: {v}" for k, v in (row.get("custom_answers") or {}).items())
        # Keep untrusted form values as text when opened in spreadsheet software.
        row = {key: safe_csv_cell(value) for key, value in row.items()}
        writer.writerow(row)
    file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="sbor_leads.csv")
    await bot.send_document(callback.from_user.id, file, caption=f"Экспорт заявок: {len(leads)}")
    await callback.answer("CSV отправлен в личные сообщения")


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text(
        "<b>Настройки бота</b>\nВыберите параметр. Услуги и вопросы задаются по одному на строку.",
        reply_markup=admin_settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:setting:"))
async def admin_setting_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin_callback(callback):
        return
    key = callback.data.split(":", 2)[2]
    if key not in SETTING_LABELS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await state.set_state(AdminSettingsFlow.waiting_value)
    await state.update_data(setting_key=key)
    current = await setting(key, None)
    if isinstance(current, list):
        current_text = "\n".join(str(x) for x in current) or "(пусто)"
    else:
        current_text = str(current or "(пусто)")
    instructions = "Введите новый текст одним сообщением."
    if key in {"services", "custom_questions"}:
        instructions = "Отправьте по одному пункту на строку. Чтобы очистить список, отправьте тире (-)."
    await callback.message.answer(
        f"<b>Изменение: {escape_text(SETTING_LABELS[key])}</b>\n\n"
        f"<b>Сейчас:</b>\n{escape_text(current_text[:2500])}\n\n{instructions}\n"
        "Для отмены отправьте /cancel."
    )
    await callback.answer()


@router.message(AdminSettingsFlow.waiting_value)
async def admin_setting_save(message: Message, state: FSMContext) -> None:
    if not private_admin_message(message):
        await state.clear()
        await message.answer("Настройки доступны только администратору в личном чате с ботом.")
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Пустое значение не сохранено. Отправьте текст или /cancel.")
        return
    data = await state.get_data()
    key = data.get("setting_key")
    if key not in SETTING_LABELS:
        await state.clear()
        await message.answer("Настройка потеряна. Откройте админ-панель заново.")
        return
    if key in {"services", "custom_questions"}:
        value_list = [] if value == "-" else [line.strip() for line in value.splitlines() if line.strip()]
        maximum = 40 if key == "services" else 8
        if len(value_list) > maximum:
            await message.answer(f"Слишком много пунктов. Максимум: {maximum}. Разделите пункты переносами строк.")
            return
        max_item_length = 100 if key == "services" else 300
        if any(len(item) > max_item_length for item in value_list):
            await message.answer(f"Один из пунктов слишком длинный (максимум {max_item_length} символов).")
            return
        value_to_save: Any = value_list
    else:
        max_length = 80 if key == "company_name" else 3500
        if len(value) > max_length:
            await message.answer(f"Текст слишком длинный. Максимум: {max_length} символов.")
            return
        value_to_save = value
    await DB.set_setting(key, value_to_save)
    await state.clear()
    await message.answer(
        f"✅ Сохранено: <b>{escape_text(SETTING_LABELS[key])}</b>.",
        reply_markup=main_menu(is_admin=True),
    )
    await message.answer("Параметры применены.", reply_markup=await admin_menu_keyboard())


async def periodic_backup_worker() -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 60 * 60)
        try:
            backup = await DB.create_backup()
            log.info("Создана плановая резервная копия %s", backup['filename'])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось создать плановую резервную копию")


async def periodic_retention_worker() -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            result = await DB.delete_old_closed_leads(LEAD_RETENTION_DAYS)
            if result["deleted"]:
                log.info("Automatic retention cleanup deleted %d closed leads older than %s", result["deleted"], result["cutoff"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось автоматически удалить старые закрытые заявки")


async def main() -> None:
    global DB, ADMIN_IDS, BACKUP_INTERVAL_HOURS, LEAD_RETENTION_DAYS
    token = get_bot_token()
    ADMIN_IDS = get_admin_ids()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен Telegram-бота в переменные окружения.")
    if not ADMIN_IDS:
        raise RuntimeError("Не заданы ADMIN_IDS. Укажите Telegram ID хотя бы одного администратора.")

    database_path = get_database_path()
    BACKUP_INTERVAL_HOURS = get_backup_interval_hours()
    LEAD_RETENTION_DAYS = get_lead_retention_days()
    DB = Database(database_path, get_backup_directory(), get_backup_retention())
    await DB.initialize(load_defaults())
    try:
        cleanup = await DB.delete_old_closed_leads(LEAD_RETENTION_DAYS)
        if cleanup["deleted"]:
            log.info("Startup retention cleanup deleted %d closed leads older than %s", cleanup["deleted"], cleanup["cutoff"])
    except Exception:
        log.exception("Не удалось выполнить стартовую очистку старых закрытых заявок; бот продолжит работу")
    try:
        initial_backup = await DB.create_backup()
        log.info("Создана стартовая резервная копия %s", initial_backup['filename'])
    except Exception:
        log.exception("Не удалось создать стартовую резервную копию; бот продолжит работу")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    log.info(
        "Бот запускается; администраторов: %d; SQLite: %s; backups: %s; backup retention: %d; backup interval: %dh; lead retention: %dd",
        len(ADMIN_IDS), database_path, DB.backup_dir, DB.backup_retention, BACKUP_INTERVAL_HOURS, LEAD_RETENTION_DAYS,
    )
    backup_task = asyncio.create_task(periodic_backup_worker(), name="sbor-periodic-backup")
    retention_task = asyncio.create_task(periodic_retention_worker(), name="sbor-periodic-retention")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        backup_task.cancel()
        retention_task.cancel()
        for task in (backup_task, retention_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
