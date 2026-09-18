"""Cloudy VPS Discord Bot — v1.0

Commands:
  !help     — show help
  !deploy   — create a new LXC VPS (interactive)
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


async def build_manage_embed(name: str, number: int) -> discord.Embed:
    info = await asyncio.to_thread(vm.get_config, name)
    st = await asyncio.to_thread(vm.state, name)
    uptime = await asyncio.to_thread(vm.get_uptime, name)
    load = await asyncio.to_thread(vm.get_load, name)
    mem_used, mem_total, mem_pct = await asyncio.to_thread(vm.get_memory, name)
    disk_used, disk_size, disk_pct = await asyncio.to_thread(vm.get_disk, name)
    cpu = await asyncio.to_thread(vm.get_cpu_usage, name)

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
            f"**Disk:** {disk_used}/{disk_size} ({disk_pct}%)"
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
        link = await asyncio.to_thread(vm.start_sshx, self.name)
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
                "⚠️ Could not start SSHX. Make sure SSHX is installed in the container, or use:\n"
                f"`lxc exec {self.name} -- bash`",
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
        super().__init__(timeout=60)
        self.name = name

    @discord.ui.button(label="Yes, delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, err = await asyncio.to_thread(vm.delete, self.name)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        if ok:
            await interaction.followup.send(f"🗑️ VPS `{self.name}` deleted.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Delete failed: {err}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


async def run_deploy(msg: discord.Message, name: str, os_image: str, user: discord.User):
    stages = [
        ("Allocating resources", 15),
        ("Creating LXC container", 40),
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
        description="Manage your LXC VPS right from Discord.",
        color=BLURPLE,
    )
    embed.add_field(
        name="Commands",
        value=(
            f"`{config.PREFIX}help` — show this message\n"
            f"`{config.PREFIX}deploy` — create a new LXC VPS\n"
            f"`{config.PREFIX}manage [name]` — open the VPS control panel\n"
            f"`{config.PREFIX}status` — check ping and server load"
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
    containers = vm.list_containers()

    if not containers:
        await ctx.send(f"No VPS found. Deploy one first with `{config.PREFIX}deploy`.")
        return

    if name is None:
        if len(containers) == 1:
            name = containers[0]
        else:
            lines = "\n".join(f"• `{c}`" for c in containers)
            await ctx.send(
                f"You have {len(containers)} VPS:\n{lines}\n\n"
                f"Specify one: `{config.PREFIX}manage <name>`"
            )
            return

    if name not in containers:
        await ctx.send(f"VPS `{name}` not found.")
        return

    number = containers.index(name) + 1
    embed = await build_manage_embed(name, number)
    await ctx.send(embed=embed, view=ManageView(name, number))


@bot.command(name="status", aliases=["ping"])
async def status(ctx):
    # Discord gateway latency
    gateway_ms = round(bot.latency * 1000, 1)

    # Network ping to 1.1.1.1 (fallback 8.8.8.8)
    ping_ms = await asyncio.to_thread(measure_ping)

    # Host load average
    load1, load5, load15 = await asyncio.to_thread(get_host_load)
    cores = os.cpu_count() or 1

    # Decide color
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
        value=f"`{load1} {load5} {load15}`  (CPU cores: {cores})",
        inline=False,
    )
    embed.add_field(
        name="Legend",
        value="🟢 normal • 🟡 high load • 🔴 failure",
        inline=False,
    )
    embed.set_footer(text=f"Cloudy VPS Bot v{config.BOT_VERSION}")
    await ctx.send(embed=embed)


def measure_ping() -> int | None:
    import subprocess

    for host in ("1.1.1.1", "8.8.8.8"):
        code, out, _ = vm.run(f"ping -c 1 -W 2 {host}")
        if code == 0:
            # "time=12.3 ms"
            for token in out.split():
                if token.startswith("time="):
                    try:
                        return round(float(token[5:].rstrip("ms")), 1)
                    except ValueError:
                        pass
    return None


def get_host_load():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[:3]
    except OSError:
        return ["-", "-", "-"]


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set it in .env")
    bot.run(config.TOKEN)
