"""Docker backend — each "VPS" is a Docker container with resource limits.

Uses the Docker SDK (``pip install docker``). The bot talks to the Docker
daemon through ``/var/run/docker.sock``: either run the bot natively, or mount
the socket if the bot itself runs inside Docker (see ``docker-compose.yml``).

All container-side commands go through the Docker SDK's ``exec_run`` — we do
NOT shell out to the ``docker`` CLI, so the bot works fine inside a container
that has no ``docker`` binary installed.
"""

import logging
import re
import time

import docker

from config import LIFETIME_DAYS, OWNER_PREFIX

log = logging.getLogger(__name__)

LABEL = "cloudy.vps"  # label that marks our managed VPS containers

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


# --- container operations ---
def _vps(all_=False):
    return client().containers.list(all=all_, filters={"label": f"{LABEL}=true"})


def list_containers() -> list[str]:
    try:
        return [c.name for c in _vps(all_=True)]
    except Exception as e:
        log.warning("list_containers failed: %s", e)
        return []


_NAME_OWNER_RE = re.compile(rf"^{re.escape(OWNER_PREFIX)}-(\d+)-vps-\d+$")


def docker_ok():
    """Check that the Docker daemon is reachable. Returns (ok, error)."""
    try:
        client().ping()
        return True, ""
    except Exception as e:
        return False, (
            f"{e}\n"
            "The bot talks to Docker through /var/run/docker.sock. Run it "
            "natively or mount the socket into the bot container "
            "(see docker-compose.yml). The `docker` CLI is NOT required."
        )


def owner_from_name(name: str) -> str:
    """Owner id encoded in the container name (fallback when labels are lost)."""
    m = _NAME_OWNER_RE.match(name or "")
    return m.group(1) if m else ""


def owner_of(name: str) -> str:
    """Return the owner id of a VPS: label first, container name as fallback."""
    try:
        labels = client().containers.get(name).labels or {}
        owner = str(labels.get("cloudy.owner") or "").strip()
        if owner:
            return owner
    except Exception:
        pass
    return owner_from_name(name)


def is_owner(name: str, user_id) -> bool:
    return owner_of(name) == str(user_id)


def list_containers_for_owner(owner_id) -> list[str]:
    """Containers owned by this user only.

    Matching uses the ``cloudy.owner`` label and, when that label is missing
    or stale, the owner id embedded in the container name. This keeps a VPS
    issued/transferred to someone else out of the admin's own list.
    """
    want = str(owner_id)
    out = []
    try:
        for c in _vps(all_=True):
            label_owner = str((c.labels or {}).get("cloudy.owner") or "").strip()
            owner = label_owner or owner_from_name(c.name)
            if owner == want:
                out.append(c.name)
    except Exception as e:
        log.warning("list_containers_for_owner failed: %s", e)
        return []
    return sorted(out)


def exists(name: str) -> bool:
    try:
        client().containers.get(name)
        return True
    except docker.errors.NotFound:
        return False
    except docker.errors.APIError:
        return False


def state(name: str) -> str:
    try:
        return client().containers.get(name).status.upper()
    except docker.errors.NotFound:
        return "NOT_FOUND"
    except docker.errors.APIError as e:
        log.warning("state(%s) failed: %s", name, e)
        return "UNKNOWN"


# Sentinel exit codes returned by _exec when the container cannot run commands.
EXEC_NOT_RUNNING = 125
EXEC_NOT_FOUND = 127


def last_logs(name: str, lines: int = 15) -> str:
    """Tail of the container log — explains why a VPS exited."""
    try:
        out = client().containers.get(name).logs(tail=lines)
        return out.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def ensure_running(name: str, wait: int = 20):
    """Make sure the container is up before running commands in it.

    A stopped VPS used to produce a burst of 409 "container is not running"
    errors from every `docker exec`. Now we simply start it and wait.
    Returns ``(ok, error)``.
    """
    st = state(name)
    if st == "RUNNING":
        return True, ""
    if st == "NOT_FOUND":
        return False, f"container `{name}` not found"

    try:
        client().containers.get(name).start()
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, f"could not start `{name}`: {e}"

    for _ in range(wait):
        if state(name) == "RUNNING":
            return True, ""
        time.sleep(1)

    tail = last_logs(name)
    detail = f"\nLast container output:\n{tail[-500:]}" if tail else ""
    return False, (
        f"container `{name}` keeps exiting right after start.{detail}"
    )


def _exec(name: str, cmd, workdir: str = "/", detach: bool = False, timeout_note: str = ""):
    """Execute a command inside the container.

    If ``cmd`` is a string, it is run through ``sh -c`` so shell operators like
    ``&&`` and ``|`` work. If ``cmd`` is a list, it is executed directly.
    Returns ``(exit_code, stdout_text)``. For detached execs, returns ``(0, "")``.
    """
    if isinstance(cmd, str):
        exec_cmd = ["sh", "-c", cmd]
    else:
        exec_cmd = list(cmd)
    try:
        c = client().containers.get(name)
        if detach:
            c.exec_run(exec_cmd, detach=True, workdir=workdir)
            return 0, ""
        res = c.exec_run(exec_cmd, workdir=workdir, demux=False)
        code = res.exit_code if hasattr(res, "exit_code") else res[0]
        out = res.output if hasattr(res, "output") else res[1]
        text = out.decode("utf-8", "ignore").strip() if out else ""
        return code, text
    except docker.errors.NotFound:
        return EXEC_NOT_FOUND, f"container `{name}` no longer exists"
    except docker.errors.APIError as e:
        msg = str(e)
        if "is not running" in msg or getattr(e, "status_code", None) == 409:
            # Expected whenever the VPS is stopped — callers handle it, so do
            # not spam the log with a stack of 409 warnings.
            log.debug("_exec(%s, %r): container not running", name, cmd)
            return EXEC_NOT_RUNNING, f"container `{name}` is not running"
        log.warning("_exec(%s, %r) API error%s: %s", name, cmd, f" [{timeout_note}]" if timeout_note else "", e)
        return 1, msg


def create(name: str, os_image: str, ram: str, cpu, disk: str, owner: str = ""):
    """Create and start a Docker container with the requested resource limits.

    ``cpu`` may be int or float (fractional cores allowed).
    Returns ``(ok, error_message)``.
    """
    try:
        cpu_val = float(cpu)
    except (TypeError, ValueError):
        return False, f"invalid cpu value: {cpu!r}"

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
        hostname=name,
        command=["sleep", "infinity"],
        detach=True,
        mem_limit=ram,
        nano_cpus=int(cpu_val * 1e9),
        network_mode="host",
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

    # Install basic tools + SSHX inside the VPS (best-effort; the console
    # button re-runs the installer later if this did not work at deploy time).
    try:
        ok, err = ensure_running(name)
        if not ok:
            log.warning("sshx preinstall skipped for %s: %s", name, err)
            return True, ""
        install_sshx(name)
    except Exception as e:  # never fail a deployment because of sshx
        log.warning("sshx preinstall failed for %s: %s", name, e)
    return True, ""


def start(name: str):
    try:
        client().containers.get(name).start()
        return True, ""
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, str(e)


def stop(name: str):
    try:
        client().containers.get(name).stop()
        return True, ""
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, str(e)


def restart(name: str):
    try:
        client().containers.get(name).restart()
        return True, ""
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, str(e)


def delete(name: str):
    try:
        client().containers.get(name).remove(force=True)
        return True, ""
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, str(e)


def transfer(name: str, new_owner):
    """Reassign a VPS to a new owner.

    Docker cannot edit a container's labels in place, so we snapshot the
    container to a temporary image, recreate it under the new owner's name
    with the same spec + a new ``cloudy.owner`` label, then remove the old
    container. Returns ``(ok, new_name_or_error)``.
    """
    try:
        c = client().containers.get(name)
    except docker.errors.NotFound:
        return False, f"container `{name}` not found"
    except docker.errors.APIError as e:
        return False, str(e)

    labels = c.labels or {}
    os_img = labels.get("cloudy.os", "ubuntu:24.04")
    ram = labels.get("cloudy.ram", "8g")
    cpu = labels.get("cloudy.cpu", "1")
    disk = labels.get("cloudy.disk", "10g")
    expires = labels.get("cloudy.expires", "")

    if owner_has_vps(new_owner):
        return False, f"user `{new_owner}` already owns a VPS"

    # Pick the next free suffix for the new owner instead of blindly reusing
    # the old one — the old suffix may already be taken by another VPS the
    # new owner had at some point.
    new_name = _next_free_name(new_owner)

    try:
        cpu_val = float(cpu)
    except (TypeError, ValueError):
        cpu_val = 1.0

    repo, tag = "cloudy-transfer-tmp", str(new_owner)
    try:
        c.commit(repository=repo, tag=tag)
    except docker.errors.APIError as e:
        return False, f"snapshot failed: {e}"

    new_labels = {
        LABEL: "true",
        "cloudy.os": os_img,
        "cloudy.ram": ram,
        "cloudy.cpu": str(cpu),
        "cloudy.disk": disk,
        "cloudy.owner": str(new_owner),
        "cloudy.expires": expires,
    }

    def _cleanup_img():
        try:
            client().images.remove(f"{repo}:{tag}", force=True)
        except Exception:
            pass

    try:
        client().containers.run(
            image=f"{repo}:{tag}",
            name=new_name,
            hostname=new_name,
            command=["sleep", "infinity"],
            detach=True,
            mem_limit=ram,
            nano_cpus=int(cpu_val * 1e9),
            network_mode="host",
            labels=new_labels,
        )
    except docker.errors.APIError as e:
        _cleanup_img()
        return False, f"recreate failed: {e}"

    try:
        c.remove(force=True)
    except docker.errors.APIError as e:
        _cleanup_img()
        return False, f"recreated as `{new_name}` but could not remove old container: {e}"

    _cleanup_img()
    return True, new_name


def _next_free_name(owner_id) -> str:
    """Pick the first unused ``<prefix>-<owner_id>-vps-<n>`` name."""
    existing = set(list_containers_for_owner(owner_id))
    n = 1
    while f"{OWNER_PREFIX}-{owner_id}-vps-{n}" in existing:
        n += 1
    return f"{OWNER_PREFIX}-{owner_id}-vps-{n}"


def get_config(name: str) -> dict:
    try:
        labels = client().containers.get(name).labels or {}
    except docker.errors.NotFound:
        return {}
    except docker.errors.APIError:
        return {}
    return {
        "os": labels.get("cloudy.os", ""),
        "ram": labels.get("cloudy.ram", ""),
        "cpu": labels.get("cloudy.cpu", ""),
        "disk": labels.get("cloudy.disk", ""),
        "expires": labels.get("cloudy.expires", ""),
        "owner": str(labels.get("cloudy.owner", "") or owner_from_name(name)),
    }


def get_uptime(name: str) -> str:
    code, out = _exec(name, "uptime")
    return out if code == 0 and out else "unavailable"


def get_load(name: str):
    code, out = _exec(name, "cat /proc/loadavg")
    if code == 0 and out:
        return out.split()[:3]
    return ["-", "-", "-"]


def get_stats(name: str):
    """Real container usage from Docker stats.

    Returns ``(cpu_percent, mem_used_mb, mem_limit_mb, mem_percent)``.
    """
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

    ms = s2.get("memory_stats", {}) or {}
    usage = ms.get("usage", 0) or 0
    limit = ms.get("limit", 0) or 0
    mem_used_mb = round(usage / 1048576)
    mem_limit_mb = round(limit / 1048576)
    mem_percent = round(usage / limit * 100, 1) if limit else 0.0

    cpu_percent = 0.0
    try:
        cs1 = s1.get("cpu_stats", {}) or {}
        cs2 = s2.get("cpu_stats", {}) or {}
        t1 = (cs1.get("cpu_usage", {}) or {}).get("total_usage", 0) or 0
        t2 = (cs2.get("cpu_usage", {}) or {}).get("total_usage", 0) or 0
        sy1 = cs1.get("system_cpu_usage", 0) or 0
        sy2 = cs2.get("system_cpu_usage", 0) or 0
        percpu = (cs2.get("cpu_usage", {}) or {}).get("percpu_usage") or [0]
        online = cs2.get("online_cpus") or len(percpu) or 1
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
    if code == 0 and out:
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


def get_ip(name: str) -> str:
    """Return the container's primary IPv4 (host networking = node's IP)."""
    code, out = _exec(name, "hostname -I")
    if code == 0 and out:
        return out.split()[0]
    return "—"


# --- SSHX console ---------------------------------------------------------
# Everything below runs INSIDE the VPS container through the Docker SDK
# (`exec_run`). We never shell out to the `docker` CLI, so the bot works even
# when the bot container has no docker binary (that was the old
# "/bin/sh: 1: docker: not found" error).

SSHX_LOG = "/tmp/sshx.log"
SSHX_URL_FILE = "/tmp/sshx.url"
SSHX_BIN = "/usr/local/bin/sshx"

_PKG_INSTALL = [
    ("apt-get", "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && "
                "apt-get install -y --no-install-recommends curl ca-certificates tar procps"),
    ("apk", "apk add --no-cache curl ca-certificates tar procps"),
    ("dnf", "dnf install -y curl ca-certificates tar procps-ng"),
    ("yum", "yum install -y curl ca-certificates tar procps-ng"),
    ("microdnf", "microdnf install -y curl ca-certificates tar procps-ng"),
    ("pacman", "pacman -Sy --noconfirm curl ca-certificates tar procps-ng"),
    ("zypper", "zypper --non-interactive install curl ca-certificates tar procps"),
]


def _has(name: str, binary: str) -> bool:
    code, _ = _exec(name, f"command -v {binary} >/dev/null 2>&1")
    return code == 0


def _down(name: str) -> bool:
    """True when the container cannot execute anything right now."""
    code, _ = _exec(name, "true")
    return code in (EXEC_NOT_RUNNING, EXEC_NOT_FOUND)


def _install_deps(name: str) -> str:
    """Install curl/tar/ca-certificates with whatever package manager exists."""
    if _has(name, "curl") or _has(name, "wget"):
        return ""
    for binary, cmd in _PKG_INSTALL:
        if _has(name, binary):
            code, out = _exec(name, cmd)
            if code == 0:
                return ""
            return f"{binary}: {out[-400:]}"
    return "no supported package manager (apt/apk/dnf/yum/pacman/zypper) in the image"


def _sshx_tarball_url(name: str) -> str:
    """Pick the right SSHX release archive for the container's architecture."""
    _, arch = _exec(name, "uname -m")
    arch = (arch or "").strip()
    if arch in ("aarch64", "arm64"):
        target = "aarch64-unknown-linux-musl"
    elif arch in ("armv7l", "armv7", "armhf"):
        target = "armv7-unknown-linux-musleabihf"
    else:
        target = "x86_64-unknown-linux-musl"
    return "https://s3.amazonaws.com/sshx/sshx-" + target + ".tar.gz"


def install_sshx(name: str) -> str:
    """Make sure the sshx binary exists in the container. Returns "" or an error."""
    ok, err = ensure_running(name)
    if not ok:
        return err
    if _has(name, "sshx"):
        return ""

    if _down(name):
        return f"container `{name}` stopped while installing sshx"

    errors = []
    dep_err = _install_deps(name)
    if dep_err:
        errors.append(dep_err)

    # 1) Official installer script.
    if _has(name, "curl"):
        _exec(name, "curl -sSfL https://sshx.io/get | sh -s run")
    elif _has(name, "wget"):
        _exec(name, "wget -qO- https://sshx.io/get | sh -s run")
    if _has(name, "sshx"):
        return ""

    # 2) Direct tarball download (no tty / no installer needed).
    url = _sshx_tarball_url(name)
    if _has(name, "curl"):
        dl = f"curl -sSfL {url} -o /tmp/sshx.tar.gz"
    elif _has(name, "wget"):
        dl = f"wget -qO /tmp/sshx.tar.gz {url}"
    else:
        dl = ""
    if dl:
        code, out = _exec(
            name,
            f"{dl} && tar -xzf /tmp/sshx.tar.gz -C /usr/local/bin sshx "
            f"&& chmod +x {SSHX_BIN} && rm -f /tmp/sshx.tar.gz",
        )
        if code != 0 and out:
            errors.append(out[-400:])

    if _has(name, "sshx"):
        return ""

    hint = (
        "could not install sshx inside the container "
        "(no internet in the container, or the image is too minimal)"
    )
    detail = "\n".join(e for e in errors if e)
    return f"{hint}\n{detail}".strip()[:800]


def _extract_sshx_link(text: str) -> str:
    m = re.search(r"https://sshx\.io/[^\s\x1b\"']+", text or "")
    return m.group(0).rstrip(".,)") if m else ""


def sshx_running(name: str) -> bool:
    code, _ = _exec(name, "pgrep -x sshx >/dev/null 2>&1")
    return code == 0


def current_sshx_link(name: str) -> str:
    """Return the link of an already-running session, if any."""
    if not sshx_running(name):
        return ""
    code, out = _exec(name, f"cat {SSHX_URL_FILE} 2>/dev/null")
    link = _extract_sshx_link(out) if code == 0 else ""
    if link:
        return link
    code, out = _exec(name, f"cat {SSHX_LOG} 2>/dev/null")
    return _extract_sshx_link(out) if code == 0 else ""


def stop_sshx(name: str):
    _exec(name, f"pkill -x sshx; rm -f {SSHX_LOG} {SSHX_URL_FILE}")
    return True, ""


def start_sshx(name: str, restart: bool = False):
    """Ensure SSHX is installed and running. Returns ``(link, error)``."""
    ok, err = ensure_running(name)
    if not ok:
        return "", err

    if restart:
        stop_sshx(name)
    else:
        link = current_sshx_link(name)
        if link:
            return link, ""
        if sshx_running(name):
            stop_sshx(name)  # running but no cached link -> restart

    err = install_sshx(name)
    if err:
        return "", err

    _exec(name, f"rm -f {SSHX_LOG} {SSHX_URL_FILE}")
    # `--quiet` prints just the URL; setsid keeps it alive after the exec ends.
    _exec(
        name,
        f"setsid sh -c 'sshx --quiet > {SSHX_LOG} 2>&1' >/dev/null 2>&1 &",
        detach=True,
    )

    for _ in range(30):  # up to ~30s
        time.sleep(1)
        code, out = _exec(name, f"cat {SSHX_LOG} 2>/dev/null")
        if code in (EXEC_NOT_RUNNING, EXEC_NOT_FOUND):
            tail = last_logs(name)
            return "", (
                f"container `{name}` stopped while starting sshx."
                + (f"\n{tail[-400:]}" if tail else "")
            )
        if code == 0:
            link = _extract_sshx_link(out)
            if link:
                _exec(name, f"printf '%s' '{link}' > {SSHX_URL_FILE}")
                return link, ""
        if not sshx_running(name) and out:
            break

    code, out = _exec(name, f"tail -20 {SSHX_LOG} 2>/dev/null")
    detail = out if code == 0 and out else "sshx produced no output"
    return "", f"sshx did not return a session link:\n{detail[-800:]}"
