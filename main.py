from __future__ import annotations

import asyncio
import csv
import io
import logging
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import get_admin_ids, get_bot_token, get_database_path, load_defaults
from database import Database
from keyboards import (
    BTN_ADMIN, BTN_CONTACTS, BTN_NEW_LEAD, BTN_SERVICES, admin_keyboard,
    admin_settings_keyboard, lead_actions_keyboard, lead_list_keyboard,
    main_menu, service_keyboard, skip_keyboard,
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


DB: Database
ADMIN_IDS: set[int]

STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "completed": "Выполнена",
    "cancelled": "Отменена",
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


def escape_text(value: Any) -> str:
    return escape(str(value if value is not None else "—"))


def format_lead(lead: dict[str, Any]) -> str:
    username = f"@{lead['username']}" if lead.get("username") else "не указан"
    answers = lead.get("custom_answers") or {}
    answer_text = ""
    if answers:
        answer_text = "\n\n<b>Дополнительные ответы:</b>"
        for question, answer in answers.items():
            answer_text += f"\n• {escape_text(question)}: {escape_text(answer)}"
    return (
        f"<b>Заявка #{lead['id']}</b>\n"
        f"<b>Статус:</b> {STATUS_LABELS.get(lead['status'], lead['status'])}\n"
        f"<b>Создана:</b> {escape_text(lead['created_at'])}\n\n"
        f"<b>Имя:</b> {escape_text(lead['full_name'])}\n"
        f"<b>Telegram:</b> {escape_text(username)}\n"
        f"<b>Услуга:</b> {escape_text(lead['service'])}\n"
        f"<b>Контакт:</b> {escape_text(lead['contact'])}\n"
        f"<b>Описание:</b> {escape_text(lead['description'])}\n"
        f"<b>Бюджет:</b> {escape_text(lead['budget'] or 'не указан')}\n"
        f"<b>Срок:</b> {escape_text(lead['deadline'] or 'не указан')}"
        f"{answer_text}"
    )


async def send_admin_notification(bot: Bot, lead: dict[str, Any]) -> None:
    text = format_lead(lead)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Открыть заявку #{lead['id']}", callback_data=f"admin:view:{lead['id']}:new")]
    ])
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🆕 <b>Новая заявка</b>\n\n{text}", reply_markup=keyboard)
            sent += 1
        except TelegramAPIError:
            log.warning("Не удалось отправить уведомление администратору %s", admin_id)
    if not sent:
        log.error("Заявка #%s сохранена, но уведомления не доставлены ни одному администратору", lead["id"])


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
        await message.answer(f"{questions[0]}\n\n<i>Вопрос 1 из {len(questions)}. Для отмены отправьте /cancel.</i>")
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
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("У вас нет доступа к админ-панели.")
        return
    await state.clear()
    company = escape_text(await setting("company_name", "Компания"))
    await message.answer(f"<b>Админ-панель · {company}</b>\nВыберите действие:", reply_markup=admin_keyboard())


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
        await message.answer(f"{questions[index]}\n\n<i>Вопрос {index + 1} из {len(questions)}. Для отмены отправьте /cancel.</i>")
        return
    await finalize_lead(message, state, bot)


@router.callback_query(F.data == "admin:home")
async def admin_home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    company = escape_text(await setting("company_name", "Компания"))
    await callback.message.edit_text(f"<b>Админ-панель · {company}</b>\nВыберите действие:", reply_markup=admin_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:leads:"))
async def admin_list_leads(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    filter_value = callback.data.rsplit(":", 1)[1]
    if filter_value not in {"new", "all"}:
        await callback.answer("Неизвестный фильтр", show_alert=True)
        return
    status = "new" if filter_value == "new" else None
    items = await DB.list_leads(status=status, limit=10)
    caption = "<b>Новые заявки</b>" if filter_value == "new" else "<b>Последние заявки</b>"
    if not items:
        await callback.message.edit_text(caption + "\n\nЗаявок пока нет.", reply_markup=admin_keyboard())
    else:
        caption += f"\nПоказаны последние {len(items)} заявок. Нажмите на заявку для подробностей:"
        await callback.message.edit_text(caption, reply_markup=lead_list_keyboard(items, filter_value))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:view:"))
async def admin_view_lead(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    try:
        lead_id = int(parts[2])
        back_to = parts[3] if len(parts) > 3 and parts[3] in {"new", "all"} else "all"
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер заявки", show_alert=True)
        return
    await render_lead(callback, lead_id, back_to)


@router.callback_query(F.data.startswith("admin:status:"))
async def admin_update_status(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
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


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    stats = await DB.get_stats()
    await callback.message.edit_text(
        "<b>Статистика заявок</b>\n\n"
        f"Всего: <b>{stats['total']}</b>\n"
        f"🆕 Новые: {stats['new']}\n"
        f"⏳ В работе: {stats['in_progress']}\n"
        f"✅ Выполнены: {stats['completed']}\n"
        f"🚫 Отменены: {stats['cancelled']}",
        reply_markup=admin_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:export")
async def admin_export(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
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
        writer.writerow(row)
    file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="sbor_leads.csv")
    await bot.send_document(callback.from_user.id, file, caption=f"Экспорт заявок: {len(leads)}")
    await callback.answer("CSV отправлен в личные сообщения")


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Настройки бота</b>\nВыберите параметр. Услуги и вопросы задаются по одному на строку.",
        reply_markup=admin_settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:setting:"))
async def admin_setting_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
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
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        await message.answer("У вас нет доступа к этой настройке.")
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
    await message.answer("Параметры применены.", reply_markup=admin_keyboard())


async def main() -> None:
    global DB, ADMIN_IDS
    token = get_bot_token()
    ADMIN_IDS = get_admin_ids()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен Telegram-бота в переменные окружения.")
    if not ADMIN_IDS:
        raise RuntimeError("Не заданы ADMIN_IDS. Укажите Telegram ID хотя бы одного администратора.")

    DB = Database(get_database_path())
    await DB.initialize(load_defaults())
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    log.info("Бот запускается; администраторов: %d; SQLite: %s", len(ADMIN_IDS), get_database_path())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
