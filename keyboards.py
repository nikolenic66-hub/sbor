from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_NEW_LEAD = "📝 Оставить заявку"
BTN_SERVICES = "🧾 Услуги"
BTN_CONTACTS = "☎️ Контакты"
BTN_ADMIN = "🔐 Админ-панель"
PAGE_SIZE = 10

STATUS_TITLES = {
    "all": "Все заявки",
    "new": "Новые заявки",
    "in_progress": "Заявки в работе",
    "completed": "Выполненные заявки",
    "cancelled": "Отменённые заявки",
}


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


def admin_keyboard(notification_issues: int = 0) -> InlineKeyboardMarkup:
    notice = f"⚠️ Уведомления ({notification_issues})" if notification_issues else "⚠️ Проверка уведомлений"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новые заявки", callback_data="admin:leads:new:0"),
         InlineKeyboardButton(text="📋 Все заявки", callback_data="admin:leads:all:0")],
        [InlineKeyboardButton(text="⏳ В работе", callback_data="admin:leads:in_progress:0"),
         InlineKeyboardButton(text="✅ Выполнены", callback_data="admin:leads:completed:0")],
        [InlineKeyboardButton(text="🚫 Отменённые", callback_data="admin:leads:cancelled:0"),
         InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:search")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="admin:export")],
        [InlineKeyboardButton(text=notice, callback_data="admin:notifications")],
        [InlineKeyboardButton(text="🗄️ Создать копию", callback_data="admin:backup:create"),
         InlineKeyboardButton(text="📚 Резервные копии", callback_data="admin:backup:list")],
        [InlineKeyboardButton(text="🧹 Очистка старых заявок", callback_data="admin:retention:preview")],
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


def lead_list_keyboard(
    leads: list[dict],
    status: str,
    offset: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lead in leads:
        label = f"#{lead['id']} · {lead['full_name']} · {lead['service']}"
        builder.button(text=label[:60], callback_data=f"admin:view:{lead['id']}:{status}")
    builder.adjust(1)

    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:leads:{status}:{max(0, offset - PAGE_SIZE)}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin:leads:{status}:{offset + PAGE_SIZE}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return builder.as_markup()


def notification_list_keyboard(leads: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    labels = {"failed": "не доставлено", "partial": "доставлено частично", "pending": "не проверено"}
    for lead in leads:
        state = labels.get(str(lead.get("notification_status")), "нужна проверка")
        title = f"#{lead['id']} · {lead['full_name']} · {state}"
        builder.button(text=title[:60], callback_data=f"admin:notify-retry:{lead['id']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return builder.as_markup()


def lead_actions_keyboard(lead_id: int, back_to: str) -> InlineKeyboardMarkup:
    allowed = {"all", "new", "in_progress", "completed", "cancelled"}
    status = back_to if back_to in allowed else "all"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новая", callback_data=f"admin:status:{lead_id}:new"),
         InlineKeyboardButton(text="⏳ В работе", callback_data=f"admin:status:{lead_id}:in_progress")],
        [InlineKeyboardButton(text="✅ Выполнена", callback_data=f"admin:status:{lead_id}:completed"),
         InlineKeyboardButton(text="🚫 Отменена", callback_data=f"admin:status:{lead_id}:cancelled")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"admin:leads:{status}:0")],
    ])



def backup_list_keyboard(backups: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for backup in backups[:10]:
        size = int(backup.get("size_bytes", 0))
        if size >= 1024 * 1024:
            size_label = f"{size / (1024 * 1024):.1f} МБ"
        else:
            size_label = f"{max(1, size // 1024)} КБ"
        created = str(backup.get("created_at", backup["filename"]))[:20]
        builder.button(
            text=f"{created} · {size_label}"[:60],
            callback_data=f"admin:backup:restore:{backup['filename']}",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(
        text="📥 Загрузить копию с устройства",
        callback_data="admin:backup:upload",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return builder.as_markup()


def retention_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Удалить старые закрытые заявки", callback_data="admin:retention:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:home")],
    ])


def backup_confirm_keyboard(filename: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚠️ Да, восстановить эту копию",
            callback_data=f"admin:backup:confirm:{filename}",
        )],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:backup:list")],
    ])
