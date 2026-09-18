# Cloudy VPS Discord Bot — v1.0

Discord-бот для управления LXC/LXD VPS. Написан на Python (discord.py), запускается в Docker.

## Возможности

- `!help` — список команд
- `!deploy` — создание новой VPS (LXC): выбор ОС (Ubuntu 22.04 / 24.04) → анимация → панель управления
- `!manage [имя]` — панель управления VPS с кнопками:
  - ▶️ Start / ⏹️ Stop / 🔄 Restart
  - 💻 Console (SSHX) — реальная ссылка на консоль
  - 🗑️ Delete — с подтверждением
- `!status` (алиас `!ping`) — пинг + нагрузка узла: 🟢 норма / 🟡 высокая нагрузка / 🔴 сбой

## Файлы

| Файл | Назначение |
|------|-----------|
| `bot.py` | основной код бота |
| `vps_manager.py` | обёртка над `lxc` (LXD) |
| `config.py` | конфигурация |
| `.env` | токен и настройки (секрет) |
| `Dockerfile` / `docker-compose.yml` | сборка и запуск в Docker |

## Настройка

1. Создай приложение в [Discord Developer Portal](https://discord.com/developers/applications) → Bot.
2. Включи **Privileged Gateway Intents → Message Content Intent** (обязательно, иначе `!` команды не будут работать).
3. Вставь токен в `.env` → `DISCORD_TOKEN`.
4. Пригласи бота на сервер (OAuth2 → URL Generator → scopes `bot` → permissions `Send Messages`, `Read Message History`, `Embed Links`, `Use External Emojis`, `Use Slash Commands`).

## Запуск

### Вариант 1 — Docker (рекомендуется)

```bash
docker compose up -d --build
docker compose logs -f
```

Бот должен иметь доступ к LXD-демону на хосте. `docker-compose.yml` уже монтирует
сокеты LXD (`/var/lib/lxd`, `/run/lxd`, snap-путь `/var/snap/lxd/common/lxd`) и работает
в `privileged` + `network_mode: host`.

Проверь путь к сокету LXD на твоём узле и при необходимости укажи его в `.env`:

```env
# Snap LXD:
LXD_SOCKET=/var/snap/lxd/common/lxd/unix.socket
# Deb LXD:
LXD_SOCKET=/var/lib/lxd/unix.socket
```

### Вариант 2 — напрямую на узле (без Docker)

```bash
pip install -r requirements.txt
python bot.py
```

## Требования к хосту

- Установлен и запущен **LXD** (`lxc` CLI должен работать): `lxc list` должен выводить контейнеры.
- Для создания VPS нужен доступ к образам `ubuntu:22.04` / `ubuntu:24.04` (remote `ubuntu:`).

## Настройки VPS по умолчанию

Меняются в `.env`:

```env
DEFAULT_RAM=4GB
DEFAULT_CPU=8
DEFAULT_DISK=85GB
LIFETIME_DAYS=30
NODE_NAME=Local Node
OWNER_PREFIX=tbmen12
```

## ⚠️ Безопасность

- Никогда не публикуй токен бота и `.env`. Если токен засветился — **сгенерируй новый** в Developer Portal.
- Ссылки на SSHX-консоль дают полный контроль над VPS — никому их не передавай.
