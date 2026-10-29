from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_NEW_LEAD = "📝 Оставить заявку"
BTN_SERVICES = "🧾 Услуги"
BTN_CONTACTS = "☎️ Контакты"
BTN_ADMIN = "🔐 Админ-панель"


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_NEW_LEAD)],
        [KeyboardButton(text=BTN_SERVICES), KeyboardButton(text=BTN_CONTACTS)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите действие")


def service_keyboard(services: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, service in enumerate(services[:40]):
        builder.button(text=service[:60], callback_data=f"lead:service:{index}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="lead:cancel"))
    return builder.as_markup()


def skip_keyboard(field_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data=f"lead:skip:{field_name}")],
        [InlineKeyboardButton(text="Отмена", callback_data="lead:cancel")],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новые заявки", callback_data="admin:leads:new")],
        [InlineKeyboardButton(text="📋 Все заявки", callback_data="admin:leads:all")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="admin:export")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
    ])


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название компании", callback_data="admin:setting:company_name")],
        [InlineKeyboardButton(text="Приветствие", callback_data="admin:setting:welcome_text")],
        [InlineKeyboardButton(text="Контакты", callback_data="admin:setting:contact_text")],
        [InlineKeyboardButton(text="Список услуг", callback_data="admin:setting:services")],
        [InlineKeyboardButton(text="Дополнительные вопросы", callback_data="admin:setting:custom_questions")],
        [InlineKeyboardButton(text="Уведомление о данных", callback_data="admin:setting:privacy_notice")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home")],
    ])


def lead_list_keyboard(leads: list[dict], status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lead in leads:
        label = f"#{lead['id']} · {lead['full_name']} · {lead['service']}"
        builder.button(text=label[:60], callback_data=f"admin:view:{lead['id']}:{status}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return builder.as_markup()


def lead_actions_keyboard(lead_id: int, back_to: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новая", callback_data=f"admin:status:{lead_id}:new"),
         InlineKeyboardButton(text="⏳ В работе", callback_data=f"admin:status:{lead_id}:in_progress")],
        [InlineKeyboardButton(text="✅ Выполнена", callback_data=f"admin:status:{lead_id}:completed"),
         InlineKeyboardButton(text="🚫 Отменена", callback_data=f"admin:status:{lead_id}:cancelled")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"admin:leads:{back_to}")],
    ])
