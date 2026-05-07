# VK Curator Bot — Project Documentation

## Описание

Бот для ВКонтакте, который мониторит беседы между кураторами и учениками,
ежедневно генерирует AI-саммари переписки и записывает отчёты в Google Sheets.
Дополнительно — оповещает кураторов в личку при «зависших» сообщениях ученика
и при невыполненных «отвечу позже». Раз в день тегает учеников с просьбой
прислать отчёт.

### Ключевые характеристики
- Поддержка **нескольких сообществ ВК одновременно** (мультитокен) — каждое
  сообщество даёт ~10 беседы, верифицированное — до 30
- Саммари генерируется в `SUMMARY_TIME` (по умолчанию 23:50) каждый день
- Сообщения хранятся `MESSAGE_RETENTION_DAYS` дней (по умолчанию 30), затем
  удаляются. После записи саммари сообщения за этот день удаляются из БД
- Алерт о неотвеченном сообщении ученика: ПН-СБ, в рабочем окне
  `WORK_HOURS_START`-`WORK_HOURS_END`, только если сообщение реально требует
  ответа (LLM-классификация)
- Напоминание про «отвечу позже»: если куратор написал «отвечу позже» и
  через `DELAYED_THRESHOLD_HOURS` (по умолчанию 1ч) ничего больше в эту
  беседу не написал — личный пинг ему и overseer'у
- Ежедневное напоминание ученикам в `REMINDER_TIME` (20:00 по умолчанию):
  тег всех не-кураторов в чате, если в беседе была активность ученика за
  последние `REMINDER_ACTIVITY_DAYS` дней (по умолчанию 2)

---

## Стек технологий

| Компонент | Технология |
|---|---|
| Язык | Python 3.12+ |
| VK интеграция | `vkbottle` 4.8.2 (async) |
| База данных | PostgreSQL 16 + `SQLAlchemy` 2.0 (async) |
| AI саммари + классификаторы | polza.ai (OpenAI-совместимый прокси), модель `deepseek-chat` |
| Google Sheets | `gspread` + Service Account |
| Планировщик | `APScheduler` |
| Сервер | Ubuntu 24.04 LTS |
| Процесс-менеджер | `systemd` |

---

## Структура проекта

```
vk-curator-bot/
├── CLAUDE.md
├── README.md
├── .env.example                # шаблон, .env в .gitignore
├── .gitignore
├── requirements.txt
├── main.py                     # точка входа
├── bot/
│   ├── __init__.py
│   ├── config.py               # хелперы для env (curator_ids, head_curator)
│   ├── vk_listener.py          # Long Poll + команды /start /stop /delete /sync /status
│   ├── classifier.py           # is_delayed_response + requires_response (regex+LLM)
│   ├── summarizer.py           # генерация саммари через polza.ai/DeepSeek
│   ├── sheets.py               # запись в Google Sheets с разделителями по дням
│   ├── notifier.py             # алерты + ежедневные напоминания ученикам
│   └── scheduler.py            # APScheduler (саммари + cleanup + alerts + reminders)
├── db/
│   ├── __init__.py
│   ├── models.py               # SQLAlchemy модели
│   ├── database.py             # async engine
│   └── crud.py                 # операции с БД
└── deploy/
    ├── vk-curator-bot.service  # systemd unit (sandbox-харденинг включён)
    └── harden_server.sh        # один раз на свежем сервере: UFW + fail2ban + SSH
```

---

## Переменные окружения (`.env`)

```env
# === VK ===
# Токены сообществ через запятую (1 или несколько)
VK_GROUP_TOKENS=tok1,tok2

# === PostgreSQL ===
# Под systemd с ProtectHome=tmpfs обязательно ?ssl=disable
DATABASE_URL=postgresql+asyncpg://botuser:PASS@localhost:5432/vk_curator_bot?ssl=disable

# === LLM (polza.ai, OpenAI-совместимый) ===
POLZA_API_KEY=
POLZA_BASE_URL=https://polza.ai/api/v1
POLZA_MODEL=deepseek-chat

# === Google Sheets ===
GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
GOOGLE_SPREADSHEET_ID=

# === Авторизация ===
# Главный куратор — единственный, кто может выполнять команды бота
HEAD_CURATOR_ID=
# Все кураторы — для разметки ролей в саммари (куратор/ученик)
CURATOR_IDS=

# === Расписание ===
SUMMARY_TIME=23:50
CLEANUP_TIME=03:00
REMINDER_TIME=20:00
MESSAGE_RETENTION_DAYS=30
TIMEZONE=Asia/Yekaterinburg

# === Алерты по неотвеченным сообщениям ученика ===
# Overseer (получает копию всех алертов в личку, может не быть в беседах).
# Он должен сам сначала написать одному из ботов.
ALERT_RECIPIENT_ID=
ALERT_THRESHOLD_HOURS=2
ALERT_CHECK_INTERVAL_MIN=15
WORK_HOURS_START=11
WORK_HOURS_END=19
# Воскресенье — выходной (захардкожено)

# === Напоминания про «отвечу позже» ===
DELAYED_THRESHOLD_HOURS=1
DELAYED_CHECK_INTERVAL_MIN=10

# === Ежедневные напоминания ученикам в чат ===
# Шлём только в беседы с активностью ученика за последние N дней
REMINDER_ACTIVITY_DAYS=2
```

---

## Схема БД

```sql
CREATE TABLE conversations (
    id SERIAL PRIMARY KEY,
    vk_peer_id BIGINT NOT NULL,
    vk_group_id BIGINT NOT NULL,                     -- ID сообщества
    title VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    -- Время последнего сообщения от ученика. Используется чтобы понять,
    -- слать ли вечернее напоминание про отчёт в эту беседу.
    last_student_message_at TIMESTAMP NULL,
    UNIQUE (vk_peer_id, vk_group_id)                 -- один и тот же chat_id
);                                                   -- может встретиться в разных
                                                     -- сообществах — это разные беседы

CREATE TABLE participants (
    id SERIAL PRIMARY KEY,
    vk_user_id BIGINT UNIQUE NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(20) CHECK (role IN ('curator','student','unknown')),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    vk_message_id BIGINT,
    sender_id BIGINT NOT NULL,
    sender_name VARCHAR(255),
    text TEXT,
    timestamp TIMESTAMP NOT NULL,                    -- UTC, naive
    created_at TIMESTAMP DEFAULT NOW(),
    alerted_at TIMESTAMP NULL,                       -- когда алертили по сообщению
    -- Сообщение куратора типа «отвечу позже» без конкретного времени
    is_delayed_response BOOLEAN DEFAULT FALSE NOT NULL,
    -- Когда напомнили куратору про этот «отвечу позже»
    delayed_alerted_at TIMESTAMP NULL,
    -- Для сообщений ученика: требуется ли ответ куратора?
    -- NULL = ещё не классифицировано (по умолчанию считаем что требует),
    -- TRUE = вопрос/просьба, FALSE = отчёт/констатация/благодарность.
    requires_response BOOLEAN NULL
);

CREATE INDEX idx_messages_conversation_timestamp
    ON messages(conversation_id, timestamp);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);
```

---

## Команды бота (только главному куратору)

| Команда | Что делает |
|---|---|
| `/start` | Активировать беседу для мониторинга |
| `/stop` | Деактивировать (история сохраняется) |
| `/delete` | Полностью удалить беседу + все сообщения из БД |
| `/sync` | Просканировать chat_id 1..50 у текущего сообщества и обновить БД |
| `/status` | Активна ли беседа + счётчик сообщений за сегодня |

Чужие сообщения с `/`-префиксами молча игнорируются (не реагируем, чтобы не
светить функционал).

---

## Логика работы

### 1. Приём сообщений (`bot/vk_listener.py`)
- N инстансов `vkbottle.Bot` (по числу токенов) делят один общий
  `loop_wrapper` от первого бота
- Каждый бот слушает свой Long Poll, входящие сообщения сохраняются в
  `messages` с привязкой к (peer_id, group_id)
- Если у беседы пустой `title` (бывает при `/start` сразу после добавления
  бота — VK ещё не синкнулся) — лениво подтягивается при первом сообщении
- Каждое сохранённое сообщение в фоне (`asyncio.create_task`) уходит в
  классификатор:
  - **сообщение куратора** → `is_delayed_response()` (это «отвечу позже»?)
  - **сообщение ученика** → `requires_response()` (нужен ли ответ куратора?)
  Результат пишется в `messages.is_delayed_response` /
  `messages.requires_response`. Не блокирует обработку нового сообщения.

### 2. Классификаторы (`bot/classifier.py`)
Гибридная схема: дешёвый regex-prefilter сначала, LLM — только для
подозрительных. Сэкономили на токенах, в LLM улетают единицы сообщений в день.

- `is_delayed_response(text)` — `True`, если это «отвечу позже / попозже /
  потом гляну» **без** конкретного времени. Если есть «через час», «к 18:00»,
  «вечером» — `False`.
- `requires_response(text)` — `True`, если сообщение ученика — вопрос/просьба
  и куратору надо ответить. `False` — для отчётов о работе, благодарностей,
  констатаций, коротких реакций. Если в тексте есть `?` — сразу `True` без
  LLM. При ошибках LLM — safe default `True`.

### 3. Саммари (`bot/scheduler.py` + `bot/summarizer.py`)
В `SUMMARY_TIME`:
1. Берём все активные беседы
2. Для каждой — выгружаем сообщения за сегодняшний день (TIMEZONE)
3. Шлём в polza.ai с промптом
4. Парсим JSON, добавляем строку в Google Sheets (с разделителем по дню,
   см. ниже)
5. После успешной записи — удаляем сообщения этого дня для этой беседы
6. Беседы без сообщений за день пропускаются (в Sheets ничего не пишем)
7. Если LLM упал — сообщения остаются на следующий заход

### 4. Cleanup (`bot/scheduler.py`)
В `CLEANUP_TIME`: `DELETE FROM messages WHERE timestamp < now() - 30d`.

### 5. Алерты по неотвеченным сообщениям (`bot/notifier.py::check_and_alert`)
Каждые `ALERT_CHECK_INTERVAL_MIN` минут:
- Если сейчас не в окне `WORK_HOURS_START`-`WORK_HOURS_END` ПН-СБ — пропускаем
- Для каждой беседы ищем самое старое сообщение ученика **после** последнего
  ответа куратора, старше `ALERT_THRESHOLD_HOURS`, ещё не алертенное, и у
  которого `requires_response IS NULL OR TRUE`. То есть отчёты, благодарности
  и констатации алерты не триггерят
- Получатели:
  - **Кураторы, состоящие в этой беседе** (через
    `messages.getConversationsMembers`) — текст со ссылкой на чат
    (`https://vk.com/im?sel=cN`)
  - **Overseer** (`ALERT_RECIPIENT_ID`, например dewal) — короткий текст
    **без** ссылки, потому что в чатах он не состоит и ссылка ему бесполезна
- После доставки хоть одному получателю — `messages.alerted_at = now()`,
  повтора по тому же сообщению не будет

### 6. Алерт по «отвечу позже» (`bot/notifier.py::check_delayed_responses`)
Каждые `DELAYED_CHECK_INTERVAL_MIN` минут в рабочем окне:
- Берём сообщения с `is_delayed_response=TRUE`,
  `delayed_alerted_at IS NULL`, старше `DELAYED_THRESHOLD_HOURS`
- Если ТОТ ЖЕ куратор написал что-то ещё в эту беседу после «отвечу позже» —
  считаем что он уже ответил, помечаем `delayed_alerted_at` без отправки
- Иначе пинг куратору-автору (со ссылкой на чат) и overseer'у (без ссылки)

### 7. Ежедневные напоминания ученикам (`bot/notifier.py::send_daily_reminders`)
В `REMINDER_TIME` (по умолчанию 20:00 ЕКБ):
- Берём активные беседы, в которых ученик писал за последние
  `REMINDER_ACTIVITY_DAYS` дней
- В каждой беседе тегаем всех не-кураторов:
  `Привет, [id123|Имя], [id456|Имя]! Пришли, пожалуйста, отчёт о работе за сегодня.`

---

## Промпт для саммари

```
Ты анализируешь переписку в учебной беседе ВКонтакте.
В беседе участвуют кураторы и ученик.

Переписка за {date}, беседа "{conversation_title}":
{messages_text}

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{
  "date": "DD.MM.YYYY",
  "conversation": "название беседы",
  "task": "какое задание было дано ученику (или 'не выдано')",
  "messages_count": 0,
  "active_participants": ["имя1", "имя2"],
  "key_points": "краткое описание главного за день (2-3 предложения)"
}
```

---

## Структура Google Sheets

Лист `"Отчёты"`:

| Дата | Беседа | Задание | Сообщений | Участники | Саммари |
|---|---|---|---|---|---|
| 27.04.2026 | Группа A | Тема X | 47 | Ученик1, Куратор1 | … |

Перед первой записью каждого нового дня в таблицу вставляется
**разделительная строка** — объединённая по всем 6 колонкам, синий фон,
белый жирный шрифт, формат: `📅 Четверг, 07.05.2026`. Это позволяет визуально
группировать отчёты по дням. Все записи одного прогона саммари (12-15 бесед)
ложатся под один такой разделитель.

При первом запуске на старой таблице (если в ней были устаревшие колонки
«Статус», «Вопросы без ответа», «Заметки») они удаляются автоматически
функцией `_migrate_legacy_columns()`.

---

## Получение токенов и ключей

### VK
1. Создать сообщество (или использовать существующее)
2. Управление → Работа с API → Создать ключ — права: `messages`,
   `photos`, `docs`, `manage`
3. Управление → Сообщения → включить
4. Управление → Сообщения → Возможности бота → разрешить добавление в беседы
5. Long Poll включается автоматически нашим кодом через
   `groups.setLongPollSettings` (или вручную в Управление → API → Long Poll API)

### polza.ai
[polza.ai/dashboard](https://polza.ai/dashboard) → создать API ключ.

### Google Sheets (Service Account)
1. [console.cloud.google.com](https://console.cloud.google.com) → новый проект
2. Включить **Google Sheets API** и **Google Drive API**
3. IAM & Admin → Service Accounts → Create → дать имя → Done
4. На созданном аккаунте: Keys → Add Key → Create new key → JSON
5. Скачанный JSON положить как `credentials.json` (chmod 600)
6. Открыть Google Таблицу → Поделиться → email сервисного аккаунта
   (`*-*@*.iam.gserviceaccount.com`) с правом «Редактор»
7. Скопировать ID таблицы из URL: `docs.google.com/spreadsheets/d/{ID}/`

⚠️ **Не путать**: OAuth Client ID (`client_secret_*.json`) — это другой тип
кредов, для интерактивных приложений; нашему боту нужен именно Service Account.

---

## Установка на сервер (Ubuntu 24.04 LTS)

### Минимальные требования
- 1 vCPU, 1 GB RAM (с swap), 10 GB SSD достаточно

### Системные пакеты
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip postgresql \
                    postgresql-contrib git ufw fail2ban unattended-upgrades
```

### PostgreSQL
```bash
sudo -u postgres psql -c "CREATE USER botuser WITH PASSWORD '<random>';"
sudo -u postgres psql -c "CREATE DATABASE vk_curator_bot OWNER botuser;"
# Переключить listen на localhost
sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = 'localhost'/" \
  /etc/postgresql/16/main/postgresql.conf
sudo systemctl restart postgresql
```

### Код проекта
```bash
sudo mkdir -p /opt/vk-curator-bot
sudo chown $USER:$USER /opt/vk-curator-bot
git clone <repo> /opt/vk-curator-bot
cd /opt/vk-curator-bot
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### `.env` и `credentials.json`
Скопировать `.env.example` → `.env`, заполнить, затем:
```bash
chmod 600 .env credentials.json
```

### systemd
```bash
sudo cp deploy/vk-curator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vk-curator-bot
sudo journalctl -u vk-curator-bot -f
```

### Хардненинг (один раз)
```bash
sudo bash deploy/harden_server.sh
# Затем (после проверки SSH-ключа): sudo systemctl restart ssh
```

Делает: UFW (только 22), fail2ban для SSH, отключение пароля и root-логина в SSH,
unattended-upgrades, chmod 600 на секреты.

---

## requirements.txt

```
vkbottle==4.8.2
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
openai>=1.55
gspread==6.1.2
google-auth==2.29.0
apscheduler==3.10.4
python-dotenv==1.0.1
aiohttp==3.9.5
pytz==2024.1
```

---

## Эксплуатация

```bash
# Логи
sudo journalctl -u vk-curator-bot -f

# Рестарт после изменения .env или кода
sudo systemctl restart vk-curator-bot

# Ручной прогон саммари
cd /opt/vk-curator-bot && set -a && . ./.env && set +a && \
  ./venv/bin/python -c \
  "import asyncio; from bot.scheduler import run_daily_summary; asyncio.run(run_daily_summary())"

# Проверка БД
PGPASSWORD=$(...) psql -h localhost -U botuser -d vk_curator_bot \
  -c "SELECT vk_group_id, count(*) FROM conversations GROUP BY vk_group_id;"

# Бэкап БД
PGPASSWORD=$(...) pg_dump -h localhost -U botuser vk_curator_bot > backup.sql
```

---

## Масштабирование

- 1 сообщество ВК = ~10 беседы; верифицированное — 30
- Несколько сообществ → добавить токены в `VK_GROUP_TOKENS=t1,t2,t3`
- ~45 MB данных в БД (потолок при 30-дневном хранении 7 500 сообщений/день)
- При 50+ беседах — рассмотреть разделение на отдельные процессы по
  токену с общей БД
