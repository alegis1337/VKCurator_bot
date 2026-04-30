# VK Curator Bot

Бот для ВКонтакте, который:
- мониторит беседы между кураторами и учениками,
- ежедневно генерирует AI-саммари переписки и пишет в Google Sheets,
- оповещает главного куратора в личку, если на сообщение ученика нет ответа
  дольше N часов в рабочее время.

## Особенности

- Поддержка **нескольких сообществ ВК** одним процессом (мультитокен) — для
  обхода лимита ВК на ~10 беседы на сообщество.
- AI через **polza.ai** (OpenAI-совместимый прокси) и DeepSeek — дёшево и
  работает в РФ без VPN.
- Хранение сообщений в PostgreSQL ровно столько, сколько нужно для саммари
  (по умолчанию 30 дней), затем чистится автоматически.
- Алерты приходят только в рабочее окно (ПН-СБ, настраиваемые часы).

## Стек

Python 3.12, vkbottle 4.8.2, SQLAlchemy 2.0 (async), PostgreSQL 16,
APScheduler, gspread, openai SDK.

## Команды бота (для главного куратора)

| Команда | Что делает |
|---|---|
| `/start` | Активировать беседу для мониторинга |
| `/stop` | Деактивировать (история сохраняется) |
| `/delete` | Полное удаление беседы + сообщений из БД |
| `/sync` | Просканировать чаты сообщества и обновить БД |
| `/status` | Активна ли беседа + счётчик сообщений за сегодня |

## Быстрый старт

Подробная инструкция и описание архитектуры — в [CLAUDE.md](CLAUDE.md).

```bash
# 1. Подготовь сервер (Ubuntu 24.04)
git clone <repo> /opt/vk-curator-bot
cd /opt/vk-curator-bot
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Настрой .env (см. .env.example)
cp .env.example .env
# заполни VK_GROUP_TOKENS, POLZA_API_KEY, GOOGLE_SPREADSHEET_ID,
#       HEAD_CURATOR_ID, ALERT_RECIPIENT_ID и т.д.
chmod 600 .env

# 3. Service Account для Google Sheets
# Положи credentials.json в корень проекта, дай таблице доступ
chmod 600 credentials.json

# 4. PostgreSQL
sudo -u postgres psql -c "CREATE USER botuser WITH PASSWORD '...';"
sudo -u postgres psql -c "CREATE DATABASE vk_curator_bot OWNER botuser;"

# 5. systemd
sudo cp deploy/vk-curator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vk-curator-bot
sudo journalctl -u vk-curator-bot -f

# 6. Хардненинг (один раз на свежем сервере)
sudo bash deploy/harden_server.sh
```


## Лицензия

MIT — см. [LICENSE](LICENSE).
