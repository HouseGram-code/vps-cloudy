"""Cloudy VPS Discord Bot.

Commands:
  !help     — show help
  !deploy   — create a new VPS (interactive)
  !manage   — manage a VPS (buttons)
  !status   — ping + host load check (green/yellow/red)
"""

import asyncio
import atexit
import logging
import os
import re
import socket
import sys
import time
from collections import OrderedDict

import discord
from discord.ext import commands

import config
import store
import vps_manager as vm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cloudy")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)

GREEN = discord.Color.brand_green()
YELLOW = discord.Color.gold()
RED = discord.Color.red()
BLURPLE = discord.Color.blurple()

# --- Custom server emojis -------------------------------------------------
E_VPS = "<:6017vps:1550844331898699906>"                     # !deploy / VPS
E_GEAR = "<a:957955purplegear:1550844906761887864>"          # !manage
E_HELP = "<a:550231purplequestionmark:1550845013741805648>"  # !help
E_CHAIN = "<a:427726purplechain:1550845151977676911>"        # SSHX link
E_WORLD = "<a:474377purpleworld:1550845303756685362>"        # !status

# Matches the trailing "-vps-<N>" suffix inside a container name.
_VPS_SUFFIX_RE = re.compile(r"-vps-(\d+)$")


# --------------------------------------------------------------------------- #
# Single instance guard
# --------------------------------------------------------------------------- #
# Every reply was showing up twice because two copies of the bot were logged in
# with the same token (e.g. an old `python bot.py` next to the docker-compose
# container). Discord happily delivers each event to both sessions. We now take
# an exclusive lock at startup so the second copy refuses to run, and we also
# ignore a message id we have already handled inside this process.

LOCK_PATH = os.getenv("BOT_LOCK_FILE", "/tmp/cloudy-vps-bot.lock")
_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """Return True if this process is the only bot instance."""
    global _lock_handle
    try:
        import fcntl

        _lock_handle = open(LOCK_PATH, "w")
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(f"{os.getpid()}@{socket.gethostname()}\n")
        _lock_handle.flush()
        atexit.register(_release_lock)
        return True
    except ImportError:  # non-POSIX — skip the lock
        return True
    except OSError:
        return False


def _release_lock():
    global _lock_handle
    try:
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
            os.unlink(LOCK_PATH)
    except OSError:
        pass


_handled_messages: "OrderedDict[int, float]" = OrderedDict()


def _seen_message(message_id: int) -> bool:
    """True if this message id was already dispatched (duplicate delivery)."""
    now = time.time()
    for mid, ts in list(_handled_messages.items()):
        if now - ts > 300:
            _handled_messages.pop(mid, None)
        else:
            break
    if message_id in _handled_messages:
        return True
    _handled_messages[message_id] = now
    while len(_handled_messages) > 1000:
        _handled_messages.popitem(last=False)
    return False


@bot.check
async def _no_duplicate_commands(ctx) -> bool:
    """Run each command exactly once per message."""
    return not _seen_message(ctx.message.id)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def bar(percent: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def generate_name(user_id) -> str:
    """Return the next free ``<prefix>-<user_id>-vps-<n>`` name for this user."""
    return vm._next_free_name(user_id)


def vps_number(name: str) -> int:
    """Extract the trailing ``-vps-N`` number from a container name (default 1)."""
    m = _VPS_SUFFIX_RE.search(name or "")
    return int(m.group(1)) if m else 1


def days_left(expires: str) -> int:
    try:
        exp = time.mktime(time.strptime(expires, "%Y-%m-%d %H:%M:%S"))
        return max(0, int((exp - time.time()) // 86400))
    except (ValueError, TypeError):
        return 0


def is_admin(user_id) -> bool:
    try:
        return int(user_id) in config.ADMIN_IDS
    except (TypeError, ValueError):
        return False


async def build_manage_embed(name: str, number: int) -> discord.Embed:
    info = await asyncio.to_thread(vm.get_config, name)
    st = await asyncio.to_thread(vm.state, name)
    uptime = await asyncio.to_thread(vm.get_uptime, name)
    load = await asyncio.to_thread(vm.get_load, name)
    cpu, mem_used, mem_total, mem_pct = await asyncio.to_thread(vm.get_stats, name)
    ip = await asyncio.to_thread(vm.get_ip, name)

    ram = info.get("ram") or config.DEFAULT_RAM
    cpu_n = info.get("cpu") or str(config.DEFAULT_CPU)
    disk = info.get("disk") or config.DEFAULT_DISK
    os_img = info.get("os") or "ubuntu:24.04"
    expires = info.get("expires") or "—"

    running = st.upper() == "RUNNING"
    color = GREEN if running else (YELLOW if st == "STOPPED" else RED)

    owner = info.get("owner") or vm.owner_of(name)
    embed = discord.Embed(
        title=f"{E_GEAR} VPS Management — VPS {number}",
        description=(
            f"{E_VPS} Container: `{name}`\n"
            f"Node: `{config.NODE_NAME}`\n"
            f"Owner: {f'<@{owner}>' if owner else '—'}"
        ),
        color=color,
    )

    embed.add_field(
        name="➤ Allocated Resources",
        value=(
            f"**Configuration:** {ram} RAM / {cpu_n} CPU / {disk} Disk\n"
            f"**Status:** {st}\n"
            f"**RAM:** {ram}\n"
            f"**CPU:** {cpu_n} Cores\n"
            f"**Storage:** {disk}\n"
            f"**OS:** {os_img}\n"
            f"**IPv4:** {ip}\n"
            f"**Uptime:** {uptime}"
        ),
        inline=False,
    )

    embed.add_field(
        name="➤ Expiration",
        value=(
            f"**Status:** ACTIVE\n"
            f"**Expires:** {expires}\n"
            f"**Days Left:** {days_left(expires)} days"
        ),
        inline=False,
    )

    embed.add_field(
        name="➤ Live Usage",
        value=(
            f"**CPU Usage:** {cpu:.1f}%\n"
            f"**Memory:** {mem_used}/{mem_total} MB ({mem_pct}%)\n"
            f"**Disk:** unknown / {disk}"
        ),
        inline=False,
    )

    embed.add_field(
        name="➤ Controls",
        value="Use the buttons below to manage your VPS",
        inline=False,
    )

    embed.set_footer(text=f"Cloudy VPS Bot v{config.BOT_VERSION} • load avg: {' '.join(load)}")
    return embed


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class OSSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.chosen = None

    async def _handle(self, interaction: discord.Interaction, os_image: str):
        self.chosen = os_image
        for child in self.children:
            child.disabled = True
        msg = interaction.message
        await interaction.response.edit_message(view=self)

        name = generate_name(interaction.user.id)
        await run_deploy(msg, name, os_image, interaction.user)

    @discord.ui.button(label="Ubuntu 22.04", emoji="🐧", style=discord.ButtonStyle.primary)
    async def ubuntu22(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "ubuntu:22.04")

    @discord.ui.button(label="Ubuntu 24.04", emoji="🐧", style=discord.ButtonStyle.success)
    async def ubuntu24(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "ubuntu:24.04")


class ManageView(discord.ui.View):
    def __init__(self, name: str, number: int, admin: bool = False, owner_id=None):
        super().__init__(timeout=None)
        self.name = name
        self.number = number
        self.admin = admin
        self.owner_id = str(owner_id) if owner_id is not None else vm.owner_of(name)
        if admin:
            btn = discord.ui.Button(
                label="Transfer",
                emoji="🔁",
                style=discord.ButtonStyle.gray,
            )
            btn.callback = self._transfer_callback
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the VPS owner (or an admin) may press these buttons."""
        uid = str(interaction.user.id)
        if uid == self.owner_id or is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "⛔ Это не ваш VPS.", ephemeral=True
        )
        return False

    async def _transfer_callback(self, interaction: discord.Interaction):
        view = TransferConfirmView(self.name, interaction.message)
        await interaction.response.send_message(
            f"⚠️ Передать VPS `{self.name}` другому пользователю?\n"
            "Контейнер будет пересоздан под новым владельцем — данные сохранятся, "
            "имя и владелец поменяются.",
            view=view,
            ephemeral=True,
        )

    async def _do(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer()
        if action == "start":
            ok, err = await asyncio.to_thread(vm.start, self.name)
        elif action == "stop":
            ok, err = await asyncio.to_thread(vm.stop, self.name)
        elif action == "restart":
            ok, err = await asyncio.to_thread(vm.restart, self.name)
        else:
            ok, err = False, "unknown action"

        if ok:
            embed = await build_manage_embed(self.name, self.number)
            await interaction.message.edit(embed=embed)
            await interaction.followup.send(f"✅ VPS `{self.name}` {action}ed.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed to {action}: {err}", ephemeral=True)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.green)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do(interaction, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do(interaction, "stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.blurple)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do(interaction, "restart")

    @discord.ui.button(label="Console (SSHX)", emoji="💻", style=discord.ButtonStyle.gray)
    async def console(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        link, err = await asyncio.to_thread(vm.start_sshx, self.name)
        if link:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open Console", url=link))
            await interaction.followup.send(
                f"{E_CHAIN} Консоль SSHX готова: {link}\n"
                "**Никому не передавайте эту ссылку** — "
                "у любого, кто её откроет, будет полный доступ к VPS.",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Не удалось запустить SSHX.\n```{err[:900]}```\n"
                "Нажмите «Перезапустить консоль» ниже или "
                "проверьте, что VPS запущен и у него есть интернет.",
                view=ConsoleRetryView(self.name),
                ephemeral=True,
            )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ConfirmDeleteView(self.name)
        await interaction.response.send_message(
            f"⚠️ Delete VPS `{self.name}`? This cannot be undone.", view=view, ephemeral=True
        )


class ConsoleRetryView(discord.ui.View):
    """Kills a stale sshx process and starts a fresh session."""

    def __init__(self, name: str):
        super().__init__(timeout=300)
        self.name = name

    @discord.ui.button(
        label="Перезапустить консоль",
        emoji="🔁",
        style=discord.ButtonStyle.blurple,
    )
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        link, err = await asyncio.to_thread(vm.start_sshx, self.name, True)
        if link:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open Console", url=link))
            await interaction.followup.send(
                f"{E_CHAIN} Новая сессия SSHX: {link}\n**Не делитесь ссылкой.**",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Снова не вышло.\n```{err[:900]}```",
                ephemeral=True,
            )


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, name: str):
        super().__init__(timeout=None)
        self.name = name

    @discord.ui.button(label="Yes, delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except Exception:
            return
        ok, err = await asyncio.to_thread(vm.delete, self.name)
        for child in self.children:
            child.disabled = True
        if ok:
            await interaction.edit_original_response(content=f"🗑️ VPS `{self.name}` deleted.", view=None)
        else:
            await interaction.edit_original_response(content=f"❌ Delete failed: {err}", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="Cancelled.", view=None)
        except Exception:
            pass
        self.stop()


class TransferConfirmView(discord.ui.View):
    def __init__(self, name: str, manage_message: discord.Message):
        super().__init__(timeout=None)
        self.name = name
        self.manage_message = manage_message

    @discord.ui.button(label="Да, передать", emoji="🔁", style=discord.ButtonStyle.blurple)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TransferModal(self.name, self.manage_message))

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="Отменено.", view=None)
        except Exception:
            pass
        self.stop()


class TransferModal(discord.ui.Modal, title="Передать VPS"):
    user_id = discord.ui.TextInput(
        label="ID нового владельца",
        placeholder="Discord user ID",
        required=True,
    )

    def __init__(self, name: str, manage_message: discord.Message):
        super().__init__()
        self.name = name
        self.manage_message = manage_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Неверный ID пользователя.", ephemeral=True)
            return

        ok, result = await asyncio.to_thread(vm.transfer, self.name, uid)
        if ok:
            try:
                await self.manage_message.edit(
                    content=f"🔁 VPS `{self.name}` передан → `{result}` (владелец: `{uid}`).",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass
            await interaction.followup.send(
                f"✅ VPS передан. Новое имя: `{result}` (владелец: `{uid}`).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ Ошибка передачи: {result[:1000]}", ephemeral=True)


async def run_deploy(msg: discord.Message, name: str, os_image: str, user: discord.User):
    stages = [
        ("Allocating resources", 15),
        ("Creating container", 40),
        ("Installing operating system", 65),
        ("Configuring SSHX", 80),
        ("Finalizing deployment", 100),
    ]

    for text, pct in stages:
        embed = discord.Embed(
            title=f"{E_VPS} VPS Deployment",
            description=(
                f"**Container:** `{name}`\n"
                f"**OS:** {os_image}\n"
                f"**Spec:** {config.DEFAULT_RAM} RAM / {config.DEFAULT_CPU} CPU / {config.DEFAULT_DISK} Disk\n\n"
                f"{bar(pct)}\n`{text}...`"
            ),
            color=BLURPLE,
        )
        await msg.edit(embed=embed)
        await asyncio.sleep(1.0)

    ok, err = await asyncio.to_thread(
        vm.create,
        name,
        os_image,
        config.DEFAULT_RAM,
        config.DEFAULT_CPU,
        config.DEFAULT_DISK,
        str(user.id),
    )

    if not ok:
        embed = discord.Embed(
            title="❌ Deployment failed",
            description=f"`{name}`\n```{err[:1500]}```",
            color=RED,
        )
        await msg.edit(embed=embed)
        return

    n = vps_number(name)
    embed = await build_manage_embed(name, n)
    embed.title = f"{E_VPS} VPS Deployed — VPS {n}"
    await msg.edit(embed=embed, view=ManageView(name, n, owner_id=user.id))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Cloudy VPS Bot v{config.BOT_VERSION} ready. Prefix: {config.PREFIX}")
    try:
        await bot.change_presence(
            activity=discord.Game(name=f"{config.PREFIX}help • {config.NODE_NAME}")
        )
    except Exception:
        pass
    ok, err = await asyncio.to_thread(vm.docker_ok)
    if ok:
        log.info("Docker daemon reachable — VPS management enabled.")
    else:
        log.error("Docker daemon NOT reachable: %s", err)


@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title=f"{E_HELP} Cloudy VPS Bot — Help",
        description=f"{E_VPS} Manage your VPS right from Discord.",
        color=BLURPLE,
    )
    embed.add_field(
        name="Commands",
        value=(
            f"{E_HELP} `{config.PREFIX}help` — show this message\n"
            f"{E_VPS} `{config.PREFIX}deploy` — create a new VPS\n"
            f"{E_GEAR} `{config.PREFIX}manage [name]` — open the VPS control panel\n"
            f"{E_WORLD} `{config.PREFIX}status` — check ping and server load\n"
            f"{E_VPS} `{config.PREFIX}about` — specs and limits"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Admin",
        value=(
            f"`{config.PREFIX}admin` — admin panel\n"
            f"`{config.PREFIX}give <id> <ram> <cpu> <disk>` — issue a VPS\n"
            f"`{config.PREFIX}transfer <name> <id>` — reassign a VPS to another user\n"
            f"`{config.PREFIX}ban <id>` / `{config.PREFIX}unban <id>` — ban/unban"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ Security",
        value="Never share your VPS console links or credentials — anyone with them can take over your VPS.",
        inline=False,
    )
    embed.set_footer(text=f"Version {config.BOT_VERSION} • Node: {config.NODE_NAME}")
    await ctx.send(embed=embed)


@bot.command(name="about", aliases=["specs"])
async def about_cmd(ctx):
    embed = discord.Embed(
        title=f"{E_VPS} About Cloudy VPS",
        description=(
            "Free Ubuntu VPS, right from Discord — no card, no cost.\n"
            f"Get one with `{config.PREFIX}deploy`."
        ),
        color=BLURPLE,
    )
    embed.add_field(
        name="Specs",
        value=(
            f"**RAM:** {config.DEFAULT_RAM}\n"
            f"**CPU:** {config.DEFAULT_CPU} core(s)\n"
            f"**Disk:** {config.DEFAULT_DISK}\n"
            "**OS:** Ubuntu 22.04 / 24.04"
        ),
        inline=False,
    )
    embed.add_field(
        name="Limits",
        value=(
            f"**Lifetime:** {config.LIFETIME_DAYS} days\n"
            "**Per user:** 1 VPS\n"
            f"**Console:** {E_CHAIN} SSHX (browser terminal)"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Cloudy VPS Bot v{config.BOT_VERSION} • Node: {config.NODE_NAME}")
    await ctx.send(embed=embed)


@bot.command(name="deploy")
async def deploy(ctx):
    if store.is_banned(ctx.author.id):
        await ctx.send("🚫 You are banned from using this bot.")
        return
    if vm.owner_has_vps(ctx.author.id):
        await ctx.send(f"❌ You already have a VPS. Use `{config.PREFIX}manage` to control it.")
        return

    embed = discord.Embed(
        title=f"{E_VPS} VPS Deployment",
        description=(
            "Choose the operating system for your new VPS:\n\n"
            f"**Spec:** {config.DEFAULT_RAM} RAM / {config.DEFAULT_CPU} CPU / {config.DEFAULT_DISK} Disk"
        ),
        color=BLURPLE,
    )
    view = OSSelectView()
    await ctx.send(embed=embed, view=view)


@bot.command(name="manage")
async def manage(ctx, name: str = None):
    if store.is_banned(ctx.author.id):
        await ctx.send("🚫 You are banned from using this bot.")
        return

    admin = is_admin(ctx.author.id)
    # A panel always belongs to ONE owner: your own VPS list never contains
    # machines you issued or transferred to somebody else.
    own = vm.list_containers_for_owner(ctx.author.id)

    if name is None:
        if not own:
            hint = (
                f"\n{E_GEAR} Админ: `{config.PREFIX}manage <имя>` или `{config.PREFIX}vpslist` для чужих VPS."
                if admin
                else ""
            )
            await ctx.send(
                f"{E_VPS} У вас нет VPS. Создайте его: `{config.PREFIX}deploy`." + hint
            )
            return
        if len(own) == 1:
            name = own[0]
        else:
            lines = "\n".join(f"• `{c}`" for c in own)
            await ctx.send(
                f"{E_GEAR} Ваши VPS ({len(own)}):\n{lines}\n\n"
                f"Выберите один: `{config.PREFIX}manage <имя>`"
            )
            return

    if name not in own:
        # Admins may open somebody else's panel explicitly, by exact name.
        if not (admin and vm.exists(name)):
            await ctx.send(f"❌ VPS `{name}` не найден или принадлежит другому пользователю.")
            return

    owner_id = vm.owner_of(name)
    number = vps_number(name)
    embed = await build_manage_embed(name, number)
    if admin and owner_id != str(ctx.author.id):
        embed.set_footer(
            text=f"Админ-режим • владелец: {owner_id or 'unknown'}"
        )
    await ctx.send(
        embed=embed,
        view=ManageView(name, number, admin=admin, owner_id=owner_id),
    )


@bot.command(name="status", aliases=["ping"])
async def status(ctx):
    try:
        # Discord gateway latency
        gateway_ms = round(bot.latency * 1000, 1)

        # Network ping (TCP to Cloudflare/Google DNS)
        ping_ms = await asyncio.to_thread(measure_ping)

        # Host load average
        load1, load5, load15 = await asyncio.to_thread(get_host_load)
        cores = os.cpu_count() or 1

        if ping_ms is None or load1 >= cores * 1.2:
            color = RED
            health = "🔴 Degraded"
        elif load1 >= cores * 0.7 or (ping_ms and ping_ms > 150):
            color = YELLOW
            health = "🟡 High load"
        else:
            color = GREEN
            health = "🟢 Healthy"

        embed = discord.Embed(
            title=f"{E_WORLD} Server Status",
            description=f"Node: `{config.NODE_NAME}`",
            color=color,
        )
        embed.add_field(name="Health", value=health, inline=False)
        embed.add_field(
            name="Ping",
            value=(
                f"**Network:** {f'{ping_ms} ms' if ping_ms is not None else 'timeout'}\n"
                f"**Discord latency:** {gateway_ms} ms"
            ),
            inline=False,
        )
        embed.add_field(
            name="Load Average",
            value=f"`{load1:.2f} {load5:.2f} {load15:.2f}`  (CPU cores: {cores})",
            inline=False,
        )
        embed.add_field(
            name="Legend",
            value="🟢 normal • 🟡 high load • 🔴 failure",
            inline=False,
        )
        embed.set_footer(text=f"Cloudy VPS Bot v{config.BOT_VERSION}")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Status check failed: {e}")


def measure_ping() -> float | None:
    import socket

    for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        start = time.monotonic()
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return round((time.monotonic() - start) * 1000, 1)
        except OSError:
            continue
    return None


def get_host_load():
    try:
        with open("/proc/loadavg") as f:
            return [float(x) for x in f.read().split()[:3]]
    except (OSError, ValueError):
        return [0.0, 0.0, 0.0]


# --------------------------------------------------------------------------- #
# Admin panel
# --------------------------------------------------------------------------- #
class IssueVPSModal(discord.ui.Modal, title="Issue VPS"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="Discord user ID", required=True)
    ram = discord.ui.TextInput(label="RAM", default="8g", required=True)
    cpu = discord.ui.TextInput(label="CPU", default="1", required=True)
    disk = discord.ui.TextInput(label="Disk", default="10g", required=True)
    os_img = discord.ui.TextInput(
        label="OS", placeholder="ubuntu:24.04 or ubuntu:22.04", default="ubuntu:24.04", required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
            cpu_n = int(self.cpu.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid User ID or CPU value.", ephemeral=True)
            return

        os_image = self.os_img.value.strip() or "ubuntu:24.04"
        if os_image not in ("ubuntu:22.04", "ubuntu:24.04"):
            os_image = "ubuntu:24.04"

        name = generate_name(uid)
        ok, err = await asyncio.to_thread(
            vm.create,
            name,
            os_image,
            self.ram.value.strip() or "8g",
            cpu_n,
            self.disk.value.strip() or "10g",
            str(uid),
        )
        if ok:
            await interaction.followup.send(
                f"✅ VPS `{name}` issued to user `{uid}` "
                f"({self.ram.value} RAM / {cpu_n} CPU / {self.disk.value} Disk).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ Failed to create: {err[:1000]}", ephemeral=True)


class BanModal(discord.ui.Modal, title="Ban user"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="Discord user ID", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID.", ephemeral=True)
            return
        store.ban(uid)
        await interaction.followup.send(f"🚫 User `{uid}` banned.", ephemeral=True)


class UnbanModal(discord.ui.Modal, title="Unban user"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="Discord user ID", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID.", ephemeral=True)
            return
        store.unban(uid)
        await interaction.followup.send(f"✅ User `{uid}` unbanned.", ephemeral=True)


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Issue VPS", emoji="🖥️", style=discord.ButtonStyle.green)
    async def issue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IssueVPSModal())

    @discord.ui.button(label="Ban", emoji="🚫", style=discord.ButtonStyle.red)
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BanModal())

    @discord.ui.button(label="Unban", emoji="✅", style=discord.ButtonStyle.gray)
    async def unban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UnbanModal())

    @discord.ui.button(label="List VPS", emoji="📋", style=discord.ButtonStyle.blurple)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        containers = vm.list_containers()
        if not containers:
            await interaction.followup.send("No VPS found.", ephemeral=True)
            return
        lines = "\n".join(
            f"• `{c}` — <@{vm.owner_of(c)}>" if vm.owner_of(c) else f"• `{c}`"
            for c in containers
        )
        await interaction.followup.send(
            f"{E_VPS} **All VPS ({len(containers)}):**\n{lines}", ephemeral=True
        )


@bot.command(name="admin")
async def admin_cmd(ctx):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ This command is for admins only.")
        return

    total = len(vm.list_containers())
    banned_count = len(store.banned())
    embed = discord.Embed(
        title="🛠️ Admin Panel",
        description=(
            f"**Node:** `{config.NODE_NAME}`\n"
            f"**Active VPS:** {total}\n"
            f"**Banned users:** {banned_count}\n\n"
            "Use the buttons below."
        ),
        color=YELLOW,
    )
    await ctx.send(embed=embed, view=AdminView())


@bot.command(name="give")
async def give_cmd(ctx, user_id: int = None, ram: str = "8g", cpu: int = 1, disk: str = "10g", os_img: str = "ubuntu:24.04"):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ Admins only.")
        return
    if user_id is None:
        await ctx.send(
            f"Usage: `{config.PREFIX}give <user_id> <ram> <cpu> <disk> [os]`\n"
            f"Example: `{config.PREFIX}give 123456789 8g 2 20g ubuntu:24.04`"
        )
        return
    if os_img not in ("ubuntu:22.04", "ubuntu:24.04"):
        os_img = "ubuntu:24.04"
    name = generate_name(user_id)
    await ctx.send(f"⏳ Creating `{name}` for user `{user_id}`...")
    ok, err = await asyncio.to_thread(vm.create, name, os_img, ram, cpu, disk, str(user_id))
    if ok:
        await ctx.send(f"✅ VPS `{name}` issued ({ram} RAM / {cpu} CPU / {disk} Disk).")
    else:
        await ctx.send(f"❌ Failed: {err[:1000]}")


@bot.command(name="transfer")
async def transfer_cmd(ctx, name: str = None, new_user_id: int = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ Admins only.")
        return
    if name is None or new_user_id is None:
        await ctx.send(
            f"Usage: `{config.PREFIX}transfer <vps_name> <new_user_id>`\n"
            f"Example: `{config.PREFIX}transfer tbmen12-1480292372620251169-vps-2 987654321098765432`"
        )
        return
    await ctx.send(f"⏳ Transferring `{name}` to user `{new_user_id}`...")
    ok, result = await asyncio.to_thread(vm.transfer, name, new_user_id)
    if ok:
        await ctx.send(f"✅ VPS transferred. New name: `{result}` (owner: `{new_user_id}`).")
    else:
        await ctx.send(f"❌ Transfer failed: {result[:1000]}")


@bot.command(name="ban")
async def ban_cmd(ctx, user_id: int):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ Admins only.")
        return
    store.ban(user_id)
    await ctx.send(f"🚫 User `{user_id}` banned.")


@bot.command(name="unban")
async def unban_cmd(ctx, user_id: int):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ Admins only.")
        return
    store.unban(user_id)
    await ctx.send(f"✅ User `{user_id}` unbanned.")


@bot.command(name="vpslist", aliases=["list"])
async def vpslist_cmd(ctx):
    if not is_admin(ctx.author.id):
        await ctx.send("⛔ Admins only.")
        return
    containers = vm.list_containers()
    if not containers:
        await ctx.send("No VPS found.")
        return
    lines = "\n".join(
        f"• `{c}` — <@{vm.owner_of(c)}>" if vm.owner_of(c) else f"• `{c}`"
        for c in containers
    )
    await ctx.send(f"{E_VPS} **All VPS ({len(containers)}):**\n{lines}")


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in .env")
    if not acquire_single_instance_lock():
        print(
            f"Another Cloudy VPS Bot instance already holds {LOCK_PATH}.\n"
            "Running two copies with the same token makes every reply appear "
            "twice — stop the other one (e.g. `docker compose down` or kill the "
            "stray `python bot.py`) and start again.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    bot.run(config.TOKEN)
