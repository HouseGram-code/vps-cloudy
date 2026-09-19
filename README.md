# Cloudy VPS Bot

Discord bot for handing out and managing VPS machines. Each VPS is a **Docker container with resource limits** (RAM / CPU / disk). Written in Python (discord.py + Docker SDK).

## Features

- `!help` — command list (the footer shows the build hash of the answering process)
- `!about` — specs and limits
- `!deploy` — create a new VPS: pick the OS (Ubuntu 22.04 / 24.04) → progress animation → control panel
- `!manage [name]` — VPS control panel with buttons:
  - ▶️ Start / ⏹️ Stop / 🔁 Restart
  - 💻 Console (SSHX) — browser console link; a stopped VPS is started automatically
  - 🗑️ Delete — with confirmation
  - 🔁 Transfer (admins) — reassign the VPS to another user
- `!status` (alias `!ping`) — ping + node load

### Admin

- `!admin` — admin panel (issue VPS, list all VPS, ban/unban)
- `!give <id> <ram> <cpu> <disk> [os]` — issue a VPS; the id is validated and the new owner gets a ready control panel in DM
- `!transfer <name> <id>` — reassign a VPS (the new owner gets a DM panel)
- `!ban <id>` / `!unban <id>`
- `!vpslist` — all VPS with their owners
- `!instances` — show this process (build, pid, host, uptime) and any other bot processes on the host

A user only sees their own machines in `!manage`; admins can open someone else's panel by passing the exact container name.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | main bot code |
| `vps_manager.py` | Docker work (containers, SSHX) |
| `config.py` | configuration |
| `.env` | token and settings (secret) |
| `.env.example` | config template |
| `Dockerfile` / `docker-compose.yml` | run the bot in Docker |

## Setup

1. Create an application in the Discord Developer Portal → Bot.
2. Enable **Message Content Intent**.
3. Put the token into `.env` → `DISCORD_TOKEN`.
4. Invite with scope `bot` and permissions: Send Messages, Read Message History, Embed Links, Use External Emojis.

## Running

```bash
pip install -r requirements.txt
python3 bot.py
```

or

```bash
docker compose up -d --build
```

The bot talks to Docker through `/var/run/docker.sock` (mounted in `docker-compose.yml`). The `docker` CLI is **not** required inside the bot container.

## Only one instance at a time (double replies)

Every answer appearing twice means two processes are logged in with the same token. Three layers now prevent that:

1. **Auto-kill** — on startup the bot SIGTERMs any other `python … bot.py` process on the same host. Disable with `AUTO_KILL_DUPLICATES=0`.
2. **Lock file** — an exclusive lock on `/tmp/cloudy-vps-bot.lock` (override with `BOT_LOCK_FILE`); a second copy refuses to start, and each message id is handled only once.
3. **Remote detection** — if a message is posted by the bot account but not by this process (a copy running on another server), it is logged and the channel gets a one-time warning every 10 minutes with the fix.

Use `!instances` to see which build/pid answered and whether another process is running locally.

Clean restart:

```bash
docker compose down
pkill -f "python.*bot.py"
docker compose up -d --build
```

## Host requirements

- Docker installed and running.
- Access to the `ubuntu:22.04` / `ubuntu:24.04` images.
- Outbound internet from the VPS containers for the SSHX console.

## Default VPS limits

| Setting | Default |
| --- | --- |
| `DEFAULT_RAM` | `8g` |
| `DEFAULT_CPU` | `1` |
| `DEFAULT_DISK` | `10g` |
| `LIFETIME_DAYS` | `15` |

RAM and CPU are always enforced by Docker; the disk limit needs btrfs/zfs (or overlay2 + pquota).

## ⚠️ Security

- Never publish the token or `.env`. If it leaks, regenerate it.
- SSHX links give full control over a VPS — never share them.
