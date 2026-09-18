"""Docker backend — each "VPS" is a Docker container with resource limits.

Uses the Docker SDK (pip install docker). The bot talks to the Docker daemon
through /var/run/docker.sock: run the bot natively, or mount the socket if the
bot itself runs inside Docker (see docker-compose.yml).
"""

import re
import subprocess
import time

import docker

from config import LIFETIME_DAYS

LABEL = "cloudy.vps"  # label that marks our managed VPS containers

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


# --- host-level shell helper (used by !status ping) ---
def run(cmd: str, timeout: int = 60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", "command timed out"


# --- container operations ---
def _vps(all_=False):
    return client().containers.list(all=all_, filters={"label": f"{LABEL}=true"})


def list_containers() -> list[str]:
    try:
        return [c.name for c in _vps(all_=True)]
    except Exception:
        return []


def exists(name: str) -> bool:
    try:
        client().containers.get(name)
        return True
    except docker.errors.NotFound:
        return False


def state(name: str) -> str:
    try:
        return client().containers.get(name).status.upper()
    except docker.errors.NotFound:
        return "NOT_FOUND"


def _exec(name: str, cmd: str):
    try:
        c = client().containers.get(name)
        code, out = c.exec_run(cmd)
        text = out.decode("utf-8", "ignore").strip() if out else ""
        return code, text
    except docker.errors.NotFound:
        return 1, ""
    except docker.errors.APIError as e:
        return 1, str(e)


def create(name: str, os_image: str, ram: str, cpu: int, disk: str, owner: str = ""):
    """Create and start a Docker container with the requested resource limits."""
    labels = {
        LABEL: "true",
        "cloudy.os": os_image,
        "cloudy.ram": ram,
        "cloudy.cpu": str(cpu),
        "cloudy.disk": disk,
        "cloudy.owner": str(owner),
        "cloudy.expires": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + LIFETIME_DAYS * 86400)
        ),
    }
    kwargs = dict(
        image=os_image,
        name=name,
        command=["sleep", "infinity"],
        detach=True,
        mem_limit=ram,
        nano_cpus=int(float(cpu) * 1e9),
        labels=labels,
    )
    # Disk quota only works on btrfs/zfs/overlay2+pquota — try it, then fall back.
    try:
        client().containers.run(**kwargs, storage_opt={"size": disk})
    except docker.errors.APIError:
        try:
            client().containers.run(**kwargs)
        except docker.errors.APIError as e:
            return False, str(e)

    # Install basic tools + SSHX inside the VPS (best-effort)
    _exec(name, "apt-get update -y && apt-get install -y procps curl ca-certificates")
    _exec(name, "curl -sSf https://sshx.io/get | sh")
    return True, ""


def start(name: str):
    try:
        client().containers.get(name).start()
        return True, ""
    except docker.errors.APIError as e:
        return False, str(e)


def stop(name: str):
    try:
        client().containers.get(name).stop()
        return True, ""
    except docker.errors.APIError as e:
        return False, str(e)


def restart(name: str):
    try:
        client().containers.get(name).restart()
        return True, ""
    except docker.errors.APIError as e:
        return False, str(e)


def delete(name: str):
    try:
        client().containers.get(name).remove(force=True)
        return True, ""
    except docker.errors.APIError as e:
        return False, str(e)


def get_config(name: str) -> dict:
    try:
        labels = client().containers.get(name).labels or {}
    except docker.errors.NotFound:
        return {}
    return {
        "os": labels.get("cloudy.os", ""),
        "ram": labels.get("cloudy.ram", ""),
        "cpu": labels.get("cloudy.cpu", ""),
        "disk": labels.get("cloudy.disk", ""),
        "expires": labels.get("cloudy.expires", ""),
    }


def get_uptime(name: str) -> str:
    code, out = _exec(name, "uptime")
    return out if code == 0 else "unavailable"


def get_load(name: str):
    code, out = _exec(name, "cat /proc/loadavg")
    if code == 0 and out:
        return out.split()[:3]
    return ["-", "-", "-"]


def get_stats(name: str):
    """Real container usage from Docker stats: (cpu_percent, mem_used_mb, mem_limit_mb, mem_percent)."""
    try:
        c = client().containers.get(name)
        s1 = c.stats(stream=False)
    except Exception:
        return 0.0, 0, 0, 0.0

    time.sleep(1)

    try:
        s2 = c.stats(stream=False)
    except Exception:
        s2 = s1

    ms = s2.get("memory_stats", {})
    usage = ms.get("usage", 0) or 0
    limit = ms.get("limit", 0) or 0
    mem_used_mb = round(usage / 1048576)
    mem_limit_mb = round(limit / 1048576)
    mem_percent = round(usage / limit * 100, 1) if limit else 0.0

    cpu_percent = 0.0
    try:
        cs1 = s1.get("cpu_stats", {})
        cs2 = s2.get("cpu_stats", {})
        t1 = cs1.get("cpu_usage", {}).get("total_usage", 0) or 0
        t2 = cs2.get("cpu_usage", {}).get("total_usage", 0) or 0
        sy1 = cs1.get("system_cpu_usage", 0) or 0
        sy2 = cs2.get("system_cpu_usage", 0) or 0
        online = cs2.get("online_cpus") or len(cs2.get("cpu_usage", {}).get("percpu_usage", [0])) or 1
        cd = t2 - t1
        sd = sy2 - sy1
        if cd > 0 and sd > 0:
            cpu_percent = round((cd / sd) * online * 100.0, 1)
    except Exception:
        cpu_percent = 0.0

    return cpu_percent, mem_used_mb, mem_limit_mb, mem_percent


def get_disk(name: str):
    """Return (used_str, size_str, percent_str)."""
    code, out = _exec(name, "df -h /")
    if code == 0:
        lines = out.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                return parts[2], parts[1], parts[4].rstrip("%")
    return "-", "-", "-"


def owner_has_vps(owner_id) -> bool:
    """True if the user already owns a VPS container (one VPS per user)."""
    try:
        for c in _vps(all_=True):
            if str((c.labels or {}).get("cloudy.owner")) == str(owner_id):
                return True
    except Exception:
        pass
    return False


def _extract_sshx_link(text: str) -> str:
    m = re.search(r"https://sshx\.io/[^\s\x1b]+", text)
    return m.group(0) if m else ""


def start_sshx(name: str):
    """Ensure SSHX is installed, start it detached, return the share link."""
    try:
        c = client().containers.get(name)
    except docker.errors.NotFound:
        return ""

    # Install SSHX if it's not there yet
    code, _ = _exec(name, "command -v sshx")
    if code != 0:
        _exec(name, "apt-get update -y && apt-get install -y curl ca-certificates procps tar bsdutils")
        _exec(name, "curl -sSf https://sshx.io/get | sh")
        code, _ = _exec(name, "command -v sshx")
        if code != 0:
            return ""

    # Start SSHX detached (with a PTY) so the session stays alive
    try:
        c.exec_run("sshx > /tmp/sshx.log 2>&1", detach=True, tty=True)
    except Exception:
        return ""

    # Poll the log for the share link
    for _ in range(10):
        time.sleep(1)
        code, out = _exec(name, "cat /tmp/sshx.log 2>/dev/null")
        if code == 0:
            link = _extract_sshx_link(out)
            if link:
                return link

    return ""
