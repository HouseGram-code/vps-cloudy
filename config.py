import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = "!"
BOT_VERSION = "1.0"

# Default VPS spec (Docker resource limits)
DEFAULT_RAM = os.getenv("DEFAULT_RAM", "1g")
DEFAULT_CPU = int(os.getenv("DEFAULT_CPU", "1"))
DEFAULT_DISK = os.getenv("DEFAULT_DISK", "10g")

NODE_NAME = os.getenv("NODE_NAME", "Local Node")
OWNER_PREFIX = os.getenv("OWNER_PREFIX", "tbmen12")

# How many days a new VPS lives before it expires
LIFETIME_DAYS = int(os.getenv("LIFETIME_DAYS", "30"))
