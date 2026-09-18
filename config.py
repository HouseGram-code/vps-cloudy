import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = "!"

BOT_VERSION = "1.0"

# Default VPS spec
DEFAULT_RAM = os.getenv("DEFAULT_RAM", "4GB")
DEFAULT_CPU = int(os.getenv("DEFAULT_CPU", "8"))
DEFAULT_DISK = os.getenv("DEFAULT_DISK", "85GB")

NODE_NAME = os.getenv("NODE_NAME", "Local Node")
OWNER_PREFIX = os.getenv("OWNER_PREFIX", "tbmen12")

# LXC socket path (empty = let lxc client auto-detect)
LXD_SOCKET = os.getenv("LXD_SOCKET", "")

# How many days a new VPS lives before it expires
LIFETIME_DAYS = int(os.getenv("LIFETIME_DAYS", "30"))
