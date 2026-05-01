import asyncio
import logging
import os

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_NAME = "Отчёты"
HEADERS = [
    "Дата",
    "Беседа",
    "Задание",
    "Сообщений",
    "Участники",
    "Саммари",
]


def _open_sheet():
    creds_file = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID is not set")

    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        worksheet.append_row(HEADERS)
        return worksheet

    # Привести таблицу к новой схеме (могут быть остатки старых колонок:
    # "Статус", "Вопросы без ответа", "Заметки")
    _migrate_legacy_columns(worksheet)

    first_row = worksheet.row_values(1)
    if first_row != HEADERS:
        worksheet.update(values=[HEADERS], range_name=f"A1:{chr(ord('A')+len(HEADERS)-1)}1")

    return worksheet


# Старая схема — для миграции
_LEGACY_HEADERS = [
    "Дата", "Беседа", "Задание", "Статус", "Сообщений",
    "Участники", "Саммари", "Вопросы без ответа", "Заметки",
]


def _migrate_legacy_columns(worksheet) -> None:
    """Если в таблице остался старый header, удаляем колонки которых нет в
    новом HEADERS. Один раз при инициализации, безопасно."""
    first_row = worksheet.row_values(1)
    if first_row != _LEGACY_HEADERS:
        return  # либо уже мигрировано, либо вообще другой формат
    # Колонки 4 (Статус), 8 (Вопросы), 9 (Заметки) → удалить.
    # Удаляем с конца, чтобы индексы не сбивались.
    for col_idx in (9, 8, 4):
        worksheet.delete_columns(col_idx)
    logger.info("Sheets: migrated legacy schema, removed Status/Questions/Notes columns")


def _row_from_summary(summary: dict) -> list[str]:
    participants = summary.get("active_participants") or []
    if isinstance(participants, list):
        participants_str = ", ".join(str(p) for p in participants)
    else:
        participants_str = str(participants)

    return [
        summary.get("date", ""),
        summary.get("conversation", ""),
        summary.get("task", ""),
        str(summary.get("messages_count", 0)),
        participants_str,
        summary.get("key_points", ""),
    ]


async def append_summary(summary: dict) -> None:
    row = _row_from_summary(summary)

    def _append():
        worksheet = _open_sheet()
        worksheet.append_row(row, value_input_option="USER_ENTERED")

    await asyncio.to_thread(_append)
    logger.info("Summary appended to Google Sheets: %s", summary.get("conversation"))


async def init_sheet() -> None:
    await asyncio.to_thread(_open_sheet)
