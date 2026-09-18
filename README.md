# Cloudy VPS Discord Bot — v1.0

Discord-бот для управления VPS. Каждый VPS — это **Docker-контейнер с лимитами ресурсов** (RAM / CPU / диск). Написан на Python (discord.py + Docker SDK).

## Возможности

- `!help` — список команд
- `!deploy` — создание новой VPS: выбор ОС (Ubuntu 22.04 / 24.04) → анимация → панель управления
- `!manage [имя]` — панель управления VPS с кнопками:
  - ▶️ Start / ⏹️ Stop / 🔄 Restart
  - 💻 Console (SSHX) — реальная ссылка на консоль
  - 🗑️ Delete — с подтверждением
- `!status` (алиас `!ping`) — пинг + нагрузка узла: 🟢 норма / 🟡 высокая нагрузка / 🔴 сбой

## Файлы

| Файл | Назначение |
|------|-----------|
| `bot.py` | основной код бота |
| `vps_manager.py` | работа с Docker (создание/управление контейнерами) |
| `config.py` | конфигурация |
| `.env` | токен и настройки (секрет, в git не попадает) |
| `.env.example` | шаблон конфига |
| `Dockerfile` / `docker-compose.yml` | запуск бота в Docker |

## Настройка

1. Создай приложение в [Discord Developer Portal](https://discord.com/developers/applications) → Bot.
2. Включи **Privileged Gateway Intents → Message Content Intent** (иначе `!` команды не работают).
3. Вставь токен в `.env` → `DISCORD_TOKEN`.
4. Пригласи бота: OAuth2 → URL Generator → scopes `bot` → permissions `Send Messages`, `Read Message History`, `Embed Links`, `Use External Emojis`.

## Запуск

### Вариант 1 — напрямую (проще всего)

```bash
pip install -r requirements.txt
python3 bot.py
```

На Ubuntu 24.04 добавь `--break-system-packages` к pip.

### Вариант 2 — Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Бот управляет контейнерами через `/var/run/docker.sock` — он уже проброшен в `docker-compose.yml`.

## Требования к хосту

- Установлен и запущен **Docker** (демон должен отвечать).
- Для создания VPS нужен доступ к образам `ubuntu:22.04` / `ubuntu:24.04` (Docker Hub).

## Лимиты VPS по умолчанию

Меняются в `.env`:

```env
DEFAULT_RAM=1g
DEFAULT_CPU=1
DEFAULT_DISK=10g
LIFETIME_DAYS=30
NODE_NAME=Local Node
OWNER_PREFIX=tbmen12
```

Примечание: RAM и CPU Docker ограничивает жёстко всегда. Лимит диска работает на btrfs/zfs; на обычном ext4 значение сохраняется, но жёстко не режется.

## ⚠️ Безопасность

- Никогда не публикуй токен бота и `.env`. Если токен засветился — сгенерируй новый.
- Ссылки на SSHX-консоль дают полный контроль над VPS — никому их не передавай.
