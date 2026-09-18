"""Thin wrapper around the LXD/LXC CLI used by the Discord bot.

All operations go through `lxc` (LXD) or `incus` (drop-in LXD fork).
Set LXC_BIN=incus in .env if you use Incus instead of LXD.
"""

import os
import subprocess
import time

from config import LXC_BIN, LXD_SOCKET, LIFETIME_DAYS

BIN = LXC_BIN  # "lxc" or "incus"


def _env() -> dict:
    env = os.environ.copy()
    if LXD_SOCKET:
        env["LXD_SOCKET"] = LXD_SOCKET
        env["INCUS_SOCKET"] = LXD_SOCKET
    return env


def run(cmd: str, timeout: int = 120):
    """Run a shell command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", "command timed out"


def list_containers() -> list[str]:
    code, out, _ = run(f"{BIN} list -c n --format csv")
    if code != 0 or not out:
        return []
    return [x for x in out.splitlines() if x.strip()]


def exists(name: str) -> bool:
    code, _, _ = run(f"{BIN} info {name}")
    return code == 0


def state(name: str) -> str:
    code, out, _ = run(f"{BIN} list {name} -c s --format csv")
    if code != 0:
        return "NOT_FOUND"
    return (out or "STOPPED").splitlines()[0].strip()


def create(name: str, os_image: str, ram: str, cpu: int, disk: str):
    """Create and start an LXC container with the requested spec."""
    steps = [
        f"{BIN} init {os_image} {name}",
        f"{BIN} config set {name} limits.memory {ram}",
        f"{BIN} config set {name} limits.cpu {cpu}",
        f"{BIN} config device override {name} root size={disk}",
        f"{BIN} start {name}",
    ]
    for step in steps:
        code, out, err = run(step, timeout=600)
        if code != 0:
            return False, err or out

    # Store expiry metadata on the container itself
    expires = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + LIFETIME_DAYS * 86400)
    )
    run(f"{BIN} config set {name} user.expires '{expires}'")
    run(f"{BIN} config set {name} user.os '{os_image}'")
    run(f"{BIN} config set {name} user.ram '{ram}'")
    run(f"{BIN} config set {name} user.cpu '{cpu}'")
    run(f"{BIN} config set {name} user.disk '{disk}'")

    # Install SSHX inside the container (best-effort)
    run(
        f"{BIN} exec {name} -- bash -c 'apt-get update -y && apt-get install -y curl ca-certificates'",
        timeout=600,
    )
    run(f"{BIN} exec {name} -- bash -c 'curl -sSf https://sshx.io/get | sh'", timeout=600)

    return True, ""


def start(name: str):
    code, out, err = run(f"{BIN} start {name}")
    return code == 0, err or out


def stop(name: str):
    code, out, err = run(f"{BIN} stop {name}")
    return code == 0, err or out


def restart(name: str):
    code, out, err = run(f"{BIN} restart {name}")
    return code == 0, err or out


def delete(name: str):
    code, out, err = run(f"{BIN} delete --force {name}")
    return code == 0, err or out


def get_config(name: str) -> dict:
    """Read the spec + expiry metadata we stored on the container."""
    out = {}
    for key in ("os", "ram", "cpu", "disk", "expires"):
        code, val, _ = run(f"{BIN} config get {name} user.{key}")
        out[key] = val if code == 0 and val else ""
    return out


def get_uptime(name: str) -> str:
    code, out, _ = run(f"{BIN} exec {name} -- uptime")
    return out if code == 0 else "unavailable"


def get_load(name: str) -> str:
    code, out, _ = run(f"{BIN} exec {name} -- cat /proc/loadavg")
    if code == 0 and out:
        return out.split()[:3]
    return ["-", "-", "-"]


def get_memory(name: str):
    """Return (used_mb, total_mb, percent)."""
    code, out, _ = run(f"{BIN} exec {name} -- free -m")
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    pct = round(used / total * 100, 1) if total else 0.0
                    return used, total, pct
                except (ValueError, IndexError):
                    break
    return 0, 0, 0.0


def get_disk(name: str):
    """Return (used_str, size_str, percent_str)."""
    code, out, _ = run(f"{BIN} exec {name} -- df -h /")
    if code == 0:
        lines = out.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                return parts[2], parts[1], parts[4].rstrip("%")
    return "-", "-", "-"


def get_cpu_usage(name: str):
    """Return CPU usage percent (host CPU as seen from the container)."""
    code, out, _ = run(f"{BIN} exec {name} -- bash -c 'top -bn1 | grep -m1 Cpu'")
    if code == 0 and out:
        # e.g. "%Cpu(s):  66.1 us,  ..."
        try:
            after = out.split(":", 1)[1].strip()
            return float(after.split(",")[0].split()[0])
        except (IndexError, ValueError):
            pass
    return 0.0


def start_sshx(name: str):
    """Start an SSHX session in the container and return the share link."""
    code, out, err = run(f"{BIN} exec {name} -- bash -c 'sshx 2>&1 | head -20'", timeout=60)
    text = (out or "") + (err or "")
    for token in text.split():
        if token.startswith("https://sshx.io/"):
            return token.rstrip(".,;")
    return ""
