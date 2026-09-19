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

from config import LIFETIME_DAYS, MAX_VPS_PER_USER, OWNER_PREFIX

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


_NAME_OWNER_RE = re.compile(r"^.+?-(\d{5,})-vps-\d+$")


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


EXEC_NOT_RUNNING = 125  # container exists but is stopped
EXEC_NOT_FOUND = 127    # container is gone


def last_logs(name: str, lines: int = 15) -> str:
    try:
        c = client().containers.get(name)
        return c.logs(tail=lines).decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def ensure_running(name: str, wait: int = 20):
    """Start the container if it is stopped. Returns (ok, error)."""
    st = state(name)
    if st == "RUNNING":
        return True, ""
    if st == "NOT_FOUND":
        return False, f"container `{name}` no longer exists"
    try:
        client().containers.get(name).start()
    except docker.errors.APIError as e:
        return False, f"could not start `{name}`: {e}"
    for _ in range(wait):
        time.sleep(1)
        if state(name) == "RUNNING":
            return True, ""
    tail = last_logs(name)
    return False, (
        f"container `{name}` stops right after start"
        + (f":\n{tail[-600:]}" if tail else "")
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
        # A stopped container is a normal state, not an error worth spamming
        # the log with (this was the old 409 Conflict warning flood).
        if "is not running" in msg or getattr(e, "status_code", None) == 409:
            log.debug("_exec(%s): container is not running", name)
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
        privileged=True,  # required so users can run Docker inside their VPS
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
        ok_run, run_err = ensure_running(name)
        if not ok_run:
            log.warning("skipping sshx preinstall for %s: %s", name, run_err)
        else:
            install_sshx(name)
            try:
                install_docker_inside(name)
                _write_docker_helper(name)
            except Exception as e:
                log.warning("docker preinstall failed for %s: %s", name, e)
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


def transfer(name: str, new_owner, new_owner_label: str = ""):
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
        return False, (
            f"user `{new_owner}` already owns "
            f"{count_for_owner(new_owner)} VPS (limit {MAX_VPS_PER_USER})"
        )

    # Pick the next free suffix for the new owner instead of blindly reusing
    # the old one — the old suffix may already be taken by another VPS the
    # new owner had at some point.
    new_name = _next_free_name(new_owner, new_owner_label)

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
        privileged=True,  # required so users can run Docker inside their VPS
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


# --- Docker inside the VPS -------------------------------------------------
# Containers are created privileged, so a full Docker daemon can run inside
# them. The daemon is started on demand with the vfs storage driver (works on
# every host filesystem) and without iptables management (the VPS shares the
# host network namespace).

DOCKERD_LOG = "/var/log/dockerd.log"
DOCKER_HELPER = "/usr/local/bin/dockerd-start"


def docker_inside_installed(name: str) -> bool:
    return _has(name, "dockerd")


def dockerd_running(name: str) -> bool:
    code, _ = _exec(name, "docker info >/dev/null 2>&1")
    return code == 0


def install_docker_inside(name: str) -> str:
    """Install the Docker engine inside the VPS. Returns "" or an error."""
    ok, err = ensure_running(name)
    if not ok:
        return err
    if docker_inside_installed(name):
        return ""

    dep_err = _install_deps(name)
    if dep_err and not (_has(name, "curl") or _has(name, "wget")):
        return dep_err

    if _has(name, "curl"):
        get = "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh"
    elif _has(name, "wget"):
        get = "wget -qO /tmp/get-docker.sh https://get.docker.com"
    else:
        return "no curl/wget inside the VPS to download the Docker installer"

    code, out = _exec(
        name,
        f"{get} && sh /tmp/get-docker.sh >/tmp/docker-install.log 2>&1; "
        "rm -f /tmp/get-docker.sh",
        timeout_note="docker install",
    )
    if docker_inside_installed(name):
        return ""

    for binary, cmd in (
        ("apt-get", "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && "
                    "apt-get install -y docker.io"),
        ("apk", "apk add --no-cache docker"),
        ("dnf", "dnf install -y docker"),
        ("yum", "yum install -y docker"),
    ):
        if _has(name, binary):
            _exec(name, cmd, timeout_note="docker install fallback")
            break
    if docker_inside_installed(name):
        return ""

    code, log_out = _exec(name, "tail -20 /tmp/docker-install.log 2>/dev/null")
    detail = log_out if code == 0 and log_out else (out or "")
    return f"could not install Docker inside the VPS:\n{detail[-700:]}".strip()


def _write_docker_helper(name: str):
    script = (
        "#!/bin/sh\n"
        "# Start the Docker daemon inside this VPS.\n"
        "if docker info >/dev/null 2>&1; then echo 'Docker is already running'; exit 0; fi\n"
        f"setsid sh -c 'dockerd --storage-driver=vfs --iptables=false "
        f"> {DOCKERD_LOG} 2>&1' >/dev/null 2>&1 &\n"
        "i=0\n"
        "while [ $i -lt 30 ]; do\n"
        "  if docker info >/dev/null 2>&1; then echo 'Docker is ready'; exit 0; fi\n"
        "  i=$((i+1)); sleep 1\n"
        "done\n"
        f"echo 'Docker failed to start, see {DOCKERD_LOG}'; exit 1\n"
    )
    _exec(name, f"cat > {DOCKER_HELPER} <<'EOF'\n{script}EOF\nchmod +x {DOCKER_HELPER}")


def start_docker_inside(name: str):
    """Install (if needed) and start dockerd inside the VPS. Returns (ok, msg)."""
    ok, err = ensure_running(name)
    if not ok:
        return False, err
    if dockerd_running(name):
        _, ver = _exec(name, "docker --version")
        return True, ver or "Docker is already running"

    err = install_docker_inside(name)
    if err:
        return False, err
    _write_docker_helper(name)

    _exec(name, "rm -f " + DOCKERD_LOG)
    _exec(
        name,
        f"setsid sh -c 'dockerd --storage-driver=vfs --iptables=false "
        f"> {DOCKERD_LOG} 2>&1' >/dev/null 2>&1 &",
        detach=True,
    )
    for _ in range(30):
        time.sleep(1)
        if dockerd_running(name):
            _, ver = _exec(name, "docker --version")
            return True, ver or "Docker is ready"
    code, tail = _exec(name, f"tail -20 {DOCKERD_LOG} 2>/dev/null")
    return False, (
        "the Docker daemon did not come up"
        + (f":\n{tail[-700:]}" if code == 0 and tail else "")
    )


def docker_inside_status(name: str) -> str:
    """Short status string for the manage panel."""
    if state(name) != "RUNNING":
        return "stopped VPS"
    if dockerd_running(name):
        return "running"
    if docker_inside_installed(name):
        return "installed, not started"
    return "not installed"


def name_slug(label: str) -> str:
    """Turn a Discord username into a safe container-name prefix."""
    slug = re.sub(r"[^a-z0-9]+", "", (label or "").lower())[:16]
    return slug or OWNER_PREFIX


def _next_free_name(owner_id, label: str = "") -> str:
    """Pick the first unused ``<owner-name>-<owner_id>-vps-<n>`` name.

    The prefix is the OWNER's Discord name, so a VPS issued or transferred to
    somebody else no longer carries the bot owner's name in its hostname.
    ``OWNER_PREFIX`` is only used as a fallback when no name is available.
    """
    prefix = name_slug(label)
    try:
        existing = {c.name for c in _vps(all_=True)}
    except Exception:
        existing = set(list_containers_for_owner(owner_id))
    n = 1
    while f"{prefix}-{owner_id}-vps-{n}" in existing:
        n += 1
    return f"{prefix}-{owner_id}-vps-{n}"


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


def count_for_owner(owner_id) -> int:
    """How many VPS this user owns.

    Counts by ``cloudy.owner`` label AND by the owner id embedded in the
    container name, so a container whose label was lost (older builds,
    manual recreation) still counts against the quota. The old version only
    looked at the label, which is how users could create unlimited VPS.
    """
    return len(list_containers_for_owner(owner_id))


def owner_has_vps(owner_id) -> bool:
    """True if the user already reached the per-user VPS limit."""
    if MAX_VPS_PER_USER <= 0:
        return False
    return count_for_owner(owner_id) >= MAX_VPS_PER_USER


def delete_many(names):
    """Delete several containers. Returns (deleted, [(name, error), ...])."""
    deleted, failed = [], []
    for n in names:
        ok, err = delete(n)
        if ok:
            deleted.append(n)
        else:
            failed.append((n, err))
    return deleted, failed


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


def _down(name: str) -> bool:
    return state(name) != "RUNNING"


def install_sshx(name: str) -> str:
    """Make sure the sshx binary exists in the container. Returns "" or an error."""
    ok, err = ensure_running(name)
    if not ok:
        return err
    if _has(name, "sshx"):
        return ""

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
    ok, err = ensure_running(name)  # a stopped VPS is started automatically
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
                f"the VPS stopped while starting the console"
                + (f":\n{tail[-600:]}" if tail else "")
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
