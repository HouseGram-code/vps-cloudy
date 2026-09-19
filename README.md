# Cloudy VPS Bot

Discord bot for handing out and managing VPS machines. Each VPS is a **Docker container with resource limits** (RAM / CPU / disk). Written in Python (discord.py + Docker SDK).

## Features

- `!help` — command list
- `!about` — specs and limits
- `!deploy` — create a new VPS: pick the OS (Ubuntu 22.04 / 24.04) → progress animation → control panel
- `!manage [name]` — VPS control panel with buttons:
  - ▶️ Start / ⏹️ Stop / 🔁 Restart
  - 💻 Console (SSHX) — real browser console link (a stopped VPS is started automatically)
  - 🗑️ Delete — with confirmation
  - 🔁 Transfer (admins) — reassign the VPS to another user
- `!status` (alias `!ping`) — ping + node load: 🟢 normal / 🟡 high load / 🔴 failure

### Admin

- `!admin` — admin panel (issue VPS, list all VPS, ban/unban)
- `!give <id> <ram> <cpu> <disk> [os]` — issue a VPS; the bot validates the ID, labels the container with the owner and DMs them a ready panel
- `!transfer <name> <id>` — reassign a VPS to another user (the new owner gets a DM panel)
- `!ban <id>` / `!unban <id>`
- `!vpslist` — all VPS with their owners

A user only ever sees their own machines in `!manage`; admins can open someone else's panel by passing the exact container name.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | main bot code |
| `vps_manager.py` | Docker work (creating / managing containers, SSHX) |
| `config.py` | configuration |
| `.env` | token and settings (secret, not committed) |
| `.env.example` | config template |
| `Dockerfile` / `docker-compose.yml` | run the bot in Docker |

## Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) → Bot.
2. Enable **Privileged Gateway Intents → Message Content Intent** (otherwise `!` commands do not work).
3. Put the token into `.env` → `DISCORD_TOKEN`.
4. Invite the bot: OAuth2 → URL Generator → scopes `bot` → permissions `Send Messages`, `Read Message History`, `Embed Links`, `Use External Emojis`.

## Running

### Option 1 — directly

```bash
pip install -r requirements.txt
python3 bot.py
```

On Ubuntu 24.04 add `--break-system-packages` to pip.

### Option 2 — Docker

```bash
docker compose up -d --build
```

The bot manages containers through `/var/run/docker.sock`, already mounted in `docker-compose.yml`. The `docker` CLI is **not** required inside the bot container.

### Only one instance at a time

The bot takes an exclusive lock on `/tmp/cloudy-vps-bot.lock` at startup (override with `BOT_LOCK_FILE`). Two copies logged in with the same token make every reply appear twice, so a second copy refuses to start. If you see that message, stop the other process (`docker compose down`, or kill the stray `python3 bot.py`).

## Host requirements

- **Docker** installed and running (the daemon must answer).
- Access to the `ubuntu:22.04` / `ubuntu:24.04` images (Docker Hub).
- The VPS containers need outbound internet for the SSHX console.

## Default VPS limits

Configured in `.env`:

| Setting | Default |
| --- | --- |
| `DEFAULT_RAM` | `8g` |
| `DEFAULT_CPU` | `1` |
| `DEFAULT_DISK` | `10g` |
| `LIFETIME_DAYS` | `15` |

Note: Docker always enforces RAM and CPU. The disk limit works on btrfs/zfs; on plain ext4 the value is stored but not hard-enforced.

## ⚠️ Security

- Never publish the bot token or `.env`. If the token leaks, regenerate it.
- SSHX console links give full control over a VPS — never share them.
