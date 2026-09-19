"""Cloudy VPS Discord Bot — v1.0

Commands:
  !help     — show help
  !deploy   — create a new VPS (interactive)
  !manage   — manage a VPS (buttons)
  !status   — ping + host load check (green/yellow/red)
"""

import asyncio
import os
import time

import discord
from discord.ext import commands

import config
import vps_manager as vm
import store

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)

GREEN = discord.Color.brand_green()
YELLOW = discord.Color.gold()
RED = discord.Color.red()
BLURPLE = discord.Color.blurple()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def bar(percent: float, width: int = 12) -> str:
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def generate_name(user_id: int) -> str:
    n = len(vm.list_containers()) + 1
    return f"{config.OWNER_PREFIX}-{user_id}-vps-{n}"


def days_left(expires: str) -> int:
    try:
        exp = time.mktime(time.strptime(expires, "%Y-%m-%d %H:%M:%S"))
        return max(0, int((exp - time.time()) // 86400))
    except ValueError:
        return 0


def is_admin(user_id) -> bool:
    return int(user_id) in config.ADMIN_IDS


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

    embed = discord.Embed(
        title=f"VPS Management - VPS {number}",
        description=f"Managing container: `{name}` on node `{config.NODE_NAME}`",
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
    def __init__(self, name: str, number: int):
        super().__init__(timeout=None)
        self.name = name
        self.number = number

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
                "🔗 Your SSHX console is ready. **Never share this link** — anyone with it can control your VPS.",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Could not start SSHX.\n```{err[:1000]}```\n"
                f"Manual access: `docker exec -it {self.name} bash`",
                ephemeral=True,
            )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ConfirmDeleteView(self.name)
        await interaction.response.send_message(
            f"⚠️ Delete VPS `{self.name}`? This cannot be undone.", view=view, ephemeral=True
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
            title="🚀 VPS Deployment",
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

    n = len(vm.list_containers())
    embed = await build_manage_embed(name, n)
    embed.title = f"✅ VPS Deployed - VPS {n}"
    await msg.edit(embed=embed, view=ManageView(name, n))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Cloudy VPS Bot v{config.BOT_VERSION} ready. Prefix: {config.PREFIX}")


@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="Cloudy VPS Bot — Help",
        description="Manage your VPS right from Discord.",
        color=BLURPLE,
    )
    embed.add_field(
        name="Commands",
        value=(
            f"`{config.PREFIX}help` — show this message\n"
            f"`{config.PREFIX}deploy` — create a new VPS\n"
            f"`{config.PREFIX}manage [name]` — open the VPS control panel\n"
            f"`{config.PREFIX}status` — check ping and server load"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Admin",
        value=(
            f"`{config.PREFIX}admin` — admin panel\n"
            f"`{config.PREFIX}give <id> <ram> <cpu> <disk>` — issue a VPS\n"
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


@bot.command(name="deploy")
async def deploy(ctx):
    if store.is_banned(ctx.author.id):
        await ctx.send("🚫 You are banned from using this bot.")
        return
    if vm.owner_has_vps(ctx.author.id):
        await ctx.send(f"❌ You already have a VPS. Use `{config.PREFIX}manage` to control it.")
        return

    embed = discord.Embed(
        title="🚀 VPS Deployment",
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

    if is_admin(ctx.author.id):
        containers = vm.list_containers()
    else:
        containers = vm.list_containers_for_owner(ctx.author.id)

    if not containers:
        await ctx.send(f"No VPS found. Deploy one first with `{config.PREFIX}deploy`.")
        return

    if name is None:
        if len(containers) == 1:
            name = containers[0]
        else:
            lines = "\n".join(f"• `{c}`" for c in containers)
            await ctx.send(
                f"Your VPS:\n{lines}\n\n"
                f"Specify one: `{config.PREFIX}manage <name>`"
            )
            return

    if name not in containers:
        await ctx.send(f"VPS `{name}` not found (or it's not yours).")
        return

    number = containers.index(name) + 1
    embed = await build_manage_embed(name, number)
    await ctx.send(embed=embed, view=ManageView(name, number))


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
            title="📡 Server Status",
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
        lines = "\n".join(f"• `{c}`" for c in containers)
        await interaction.followup.send(f"**All VPS ({len(containers)}):**\n{lines}", ephemeral=True)


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
    lines = "\n".join(f"• `{c}`" for c in containers)
    await ctx.send(f"**All VPS ({len(containers)}):**\n{lines}")


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in .env")
    bot.run(config.TOKEN)
