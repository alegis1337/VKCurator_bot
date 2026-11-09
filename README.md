# VK Curator Bot

Бот для ВКонтакте, который:
- мониторит беседы между кураторами и учениками,
- ежедневно генерирует AI-саммари переписки и пишет его в Google Sheets,
- оповещает кураторов в личку, если на сообщение ученика долго нет ответа
  в рабочее время,
- напоминает куратору про невыполненное «отвечу позже»,
- раз в день просит учеников прислать отчёт о работе.

## Особенности

- Поддержка **нескольких сообществ ВК** одним процессом (мультитокен) — для
  обхода лимита ВК: ~10 бесед на сообщество, 30 для верифицированного.
- AI через **polza.ai** (OpenAI-совместимый прокси) и DeepSeek — дёшево и
  работает в РФ без VPN.
- Гибридная классификация сообщений: быстрый regex-фильтр, LLM — только для
  неоднозначных случаев.
- Сообщения хранятся в PostgreSQL ровно столько, сколько нужно для саммари
  (по умолчанию 30 дней), затем удаляются автоматически.
- Алерты приходят только в рабочее окно (ПН–СБ, настраиваемые часы).

## Стек

| Компонент | Технология |
|---|---|
| Язык | Python 3.12+ |
| VK | `vkbottle` 4.8.2 (async, Long Poll) |
| База данных | PostgreSQL 16 + `SQLAlchemy` 2.0 (async) |
| Саммари и классификация | polza.ai, модель `deepseek-chat` (`openai` SDK) |
| Google Sheets | `gspread` + Service Account |
| Планировщик | `APScheduler` |
| Сервер | Ubuntu 24.04 LTS + `systemd` |

## Команды бота

Команды выполняет только главный куратор (`HEAD_CURATOR_ID`); от остальных
сообщения с `/` молча игнорируются.

| Команда | Что делает |
|---|---|
| `/start` | Активировать беседу для мониторинга |
| `/stop` | Деактивировать (история сохраняется) |
| `/delete` | Полностью удалить беседу и её сообщения из БД |
| `/sync` | Просканировать беседы сообщества и обновить БД |
| `/status` | Активна ли беседа + счётчик сообщений за сегодня |
| `/broadcast <текст>` | Разослать объявление во все активные беседы всех сообществ; в ответ — отчёт «доставлено / ошибок» |

## Как это работает

**Приём сообщений.** Для каждого токена поднимается свой экземпляр бота с
Long Poll, все они работают в одном процессе. Сообщения сохраняются в БД с
привязкой к беседе и сообществу и в фоне отправляются на классификацию:
- сообщение куратора — это «отвечу позже» без конкретного времени?
- сообщение ученика — требует ли оно ответа (вопрос, просьба) или это отчёт,
  благодарность, короткая реакция?

**Ежедневное саммари** (`SUMMARY_TIME`). Для каждой активной беседы сообщения
за день отправляются в LLM, результат пишется строкой в Google Sheets, после
чего сообщения этого дня удаляются из БД. Беседы без сообщений пропускаются.
Если LLM недоступен, сообщения остаются до следующего запуска.

**Алерт о неотвеченном сообщении** (каждые `ALERT_CHECK_INTERVAL_MIN` минут,
ПН–СБ в рабочие часы). Если сообщение ученика, требующее ответа, висит дольше
`ALERT_THRESHOLD_HOURS` после последнего ответа куратора:
- кураторы, состоящие в беседе, получают уведомление со ссылкой на чат;
- наблюдатель (`ALERT_RECIPIENT_ID`) получает короткое уведомление без ссылки.

По одному сообщению алерт отправляется один раз.

**«Отвечу позже»** (каждые `DELAYED_CHECK_INTERVAL_MIN` минут в рабочее
время). Если куратор написал «отвечу позже» и за `DELAYED_THRESHOLD_HOURS`
больше ничего не написал в эту беседу — напоминание ему и наблюдателю.

**Напоминание ученикам** (`REMINDER_TIME`). В беседах, где ученик писал за
последние `REMINDER_ACTIVITY_DAYS` дней, бот отмечает всех не-кураторов и
просит прислать отчёт о работе за сегодня.

**Очистка** (`CLEANUP_TIME`). Удаляются сообщения старше
`MESSAGE_RETENTION_DAYS` дней.

## Google Sheets

Лист `Отчёты`:

| Дата | Беседа | Задание | Сообщений | Участники | Саммари |
|---|---|---|---|---|---|
| 27.04.2026 | Группа A | Тема X | 47 | Ученик1, Куратор1 | … |

Записи группируются по дням: перед первой записью нового дня вставляется
строка-разделитель вида `📅 Четверг, 07.05.2026`.

## Настройка

Все параметры задаются в `.env` (шаблон — `.env.example`).

| Переменная | По умолчанию | Описание |
|---|---|---|
| `VK_GROUP_TOKENS` | — | Токены сообществ через запятую |
| `DATABASE_URL` | — | `postgresql+asyncpg://...`; под systemd добавьте `?ssl=disable` |
| `POLZA_API_KEY` | — | Ключ polza.ai |
| `POLZA_BASE_URL` | `https://polza.ai/api/v1` | |
| `POLZA_MODEL` | `deepseek-chat` | |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | `credentials.json` | JSON-ключ Service Account |
| `GOOGLE_SPREADSHEET_ID` | — | ID таблицы из URL |
| `HEAD_CURATOR_ID` | — | VK ID главного куратора (управляет ботом) |
| `CURATOR_IDS` | — | VK ID всех кураторов через запятую |
| `ALERT_RECIPIENT_ID` | — | VK ID наблюдателя; он должен сначала сам написать одному из ботов |
| `TIMEZONE` | `Europe/Moscow` | |
| `SUMMARY_TIME` | `22:00` | Время ежедневного саммари |
| `REMINDER_TIME` | `20:00` | Время напоминания ученикам |
| `CLEANUP_TIME` | `03:00` | Время очистки старых сообщений |
| `MESSAGE_RETENTION_DAYS` | `30` | Сколько дней хранить сообщения |
| `ALERT_THRESHOLD_HOURS` | `2` | Через сколько часов без ответа слать алерт |
| `ALERT_CHECK_INTERVAL_MIN` | `15` | Период проверки (0 — выключить) |
| `WORK_HOURS_START` / `WORK_HOURS_END` | `11` / `19` | Рабочее окно для алертов |
| `DELAYED_THRESHOLD_HOURS` | `1` | Порог для «отвечу позже» |
| `DELAYED_CHECK_INTERVAL_MIN` | `10` | Период проверки «отвечу позже» (0 — выключить) |
| `REMINDER_ACTIVITY_DAYS` | `2` | Напоминать только в беседах с активностью ученика за N дней |

## Получение ключей

### VK
1. Создайте сообщество (или используйте существующее).
2. Управление → Работа с API → Создать ключ с правами `messages`, `photos`,
   `docs`, `manage`.
3. Управление → Сообщения → включить; в «Возможностях бота» разрешить
   добавление в беседы.
4. Long Poll включается автоматически при запуске бота.

### polza.ai
[polza.ai/dashboard](https://polza.ai/dashboard) → создать API-ключ.

### Google Sheets (Service Account)
1. [console.cloud.google.com](https://console.cloud.google.com) → новый проект.
2. Включите **Google Sheets API** и **Google Drive API**.
3. IAM & Admin → Service Accounts → Create.
4. На созданном аккаунте: Keys → Add Key → JSON.
5. Сохраните файл как `credentials.json` в корне проекта.
6. Откройте таблицу → «Поделиться» → email сервисного аккаунта с правом
   «Редактор».
7. ID таблицы — из URL: `docs.google.com/spreadsheets/d/{ID}/`.

Нужен именно Service Account, а не OAuth Client ID (`client_secret_*.json`).

## Установка (Ubuntu 24.04)

Минимум: 1 vCPU, 1 GB RAM (со swap), 10 GB SSD.

```bash
# 1. Пакеты
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip postgresql \
                    postgresql-contrib git ufw fail2ban unattended-upgrades

# 2. PostgreSQL
sudo -u postgres psql -c "CREATE USER botuser WITH PASSWORD '<random>';"
sudo -u postgres psql -c "CREATE DATABASE vk_curator_bot OWNER botuser;"

# 3. Код
sudo mkdir -p /opt/vk-curator-bot && sudo chown $USER:$USER /opt/vk-curator-bot
git clone https://github.com/alegis1337/VKCurator_bot.git /opt/vk-curator-bot
cd /opt/vk-curator-bot
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt

# 4. Секреты
cp .env.example .env          # заполните значения
# положите credentials.json в корень проекта
chmod 600 .env credentials.json

# 5. systemd
sudo cp deploy/vk-curator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vk-curator-bot

# 6. Харденинг (один раз на свежем сервере)
sudo bash deploy/harden_server.sh
```

`harden_server.sh` настраивает UFW (открыт только SSH), fail2ban, отключает
вход по паролю и root-логин в SSH, включает unattended-upgrades. После
проверки входа по ключу: `sudo systemctl restart ssh`.

## Эксплуатация

```bash
# Логи
sudo journalctl -u vk-curator-bot -f

# Перезапуск после изменения .env или кода
sudo systemctl restart vk-curator-bot

# Ручной запуск саммари
cd /opt/vk-curator-bot && set -a && . ./.env && set +a && \
  ./venv/bin/python -c \
  "import asyncio; from bot.scheduler import run_daily_summary; asyncio.run(run_daily_summary())"

# Бэкап БД
pg_dump -h localhost -U botuser vk_curator_bot > backup.sql
```

## Структура проекта

```
├── main.py                     # точка входа
├── bot/
│   ├── config.py               # чтение настроек из окружения
│   ├── vk_listener.py          # Long Poll и команды бота
│   ├── classifier.py           # классификация сообщений (regex + LLM)
│   ├── summarizer.py           # генерация саммари
│   ├── sheets.py               # запись в Google Sheets
│   ├── notifier.py             # алерты и напоминания
│   └── scheduler.py            # расписание задач
├── db/
│   ├── models.py               # модели SQLAlchemy
│   ├── database.py             # подключение к БД
│   └── crud.py                 # операции с БД
└── deploy/
    ├── vk-curator-bot.service  # systemd unit с sandbox-настройками
    └── harden_server.sh        # базовая защита сервера
```

## Лицензия

MIT — см. [LICENSE](LICENSE).
