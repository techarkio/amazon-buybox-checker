"""Recommend how many local browser sessions this machine can run, from its CPU cores and memory.

Input:  nothing; reads the operating system and, inside a container, the container's CPU and memory limits.
Output: a Resources record with the cores, the memory, the recommended number of sessions and what limited it.
Constraints: no extra dependency (no psutil). Sizes come from the measurements in the README: each session is one
        headless Chromium of about 0.5 GB and up to 0.5 CPU core, and the app itself needs about 0.5 GB.
        Amazon rate-limits by IP, and all local sessions share one, so the recommendation stays at or below
        IP_SAFE_SESSIONS however big the machine is. Free memory is known on Linux; elsewhere total memory is used.
"""

import math
import os
from dataclasses import dataclass
from pathlib import Path

CORES_PER_SESSION = 0.5
MB_PER_SESSION = 512
APP_MB = 512
IP_SAFE_SESSIONS = 4  # sessions per IP that long runs keep up without Amazon pushing back (see README)
CGROUP_DIR = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")


@dataclass(frozen=True)
class Resources:
    """What this machine offers and how many local sessions it can run."""

    cores: float
    memory_mb: int | None  # None when the operating system doesn't tell
    memory_is_free: bool  # True: memory still available; False: total memory
    recommended_sessions: int
    limited_by: str  # "cpu", "memory" or "ip"


def detect_resources(max_sessions: int = IP_SAFE_SESSIONS) -> Resources:
    """Read this machine's cores and memory and recommend a number of sessions (1 to max_sessions)."""
    cores = cpu_cores()
    memory_mb, memory_is_free = free_memory_mb(), True
    if memory_mb is None:
        memory_mb, memory_is_free = total_memory_mb(), False
    return recommend(cores, memory_mb, memory_is_free, min(max_sessions, IP_SAFE_SESSIONS))


def sessions_or_auto(local_sessions: int | None, max_sessions: int = IP_SAFE_SESSIONS) -> int:
    """Return the number of sessions asked for, or this machine's recommendation when it is None ("auto")."""
    return detect_resources(max_sessions).recommended_sessions if local_sessions is None else local_sessions


def parse_sessions(value: str) -> int | None:
    """Read a --local-sessions value: a number, or "auto" (None) to size it from this machine."""
    return None if value.strip().lower() == "auto" else int(value)


def recommend(cores: float, memory_mb: int | None, memory_is_free: bool, cap: int) -> Resources:
    """Pick the smallest of the CPU, memory and IP limits, and never fewer than one session."""
    limits = {"ip": cap, "cpu": math.floor(cores / CORES_PER_SESSION)}
    if memory_mb is not None:
        limits["memory"] = (memory_mb - APP_MB) // MB_PER_SESSION
    limited_by = min(limits, key=lambda name: (limits[name], name != "ip"))  # on a tie, credit the IP cap
    return Resources(round(cores, 1), memory_mb, memory_is_free, max(1, limits[limited_by]), limited_by)


def cpu_cores() -> float:
    """Return the cores this process may use: the CPUs it can run on, lowered by a container CPU quota."""
    cores = float(len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1)
    quota = cgroup_cpu_quota()
    return min(cores, quota) if quota else cores


def cgroup_cpu_quota() -> float | None:
    """Return the container's CPU limit in cores (cgroup v2 or v1), or None when there is none."""
    v2 = read_text(CGROUP_DIR / "cpu.max").split()
    if len(v2) == 2 and v2[0] != "max":
        return int(v2[0]) / int(v2[1])
    quota, period = read_text(CGROUP_DIR / "cpu/cpu.cfs_quota_us"), read_text(CGROUP_DIR / "cpu/cpu.cfs_period_us")
    if quota.lstrip("-").isdigit() and int(quota) > 0 and period.isdigit():
        return int(quota) / int(period)
    return None


def free_memory_mb() -> int | None:
    """Return the memory still available on Linux, lowered by a container memory limit, or None elsewhere."""
    available = next((int(line.split()[1]) // 1024 for line in read_text(MEMINFO).splitlines()
                      if line.startswith("MemAvailable:")), None)
    if available is None:
        return None
    limit, used = read_text(CGROUP_DIR / "memory.max"), read_text(CGROUP_DIR / "memory.current")
    if limit.isdigit() and used.isdigit():
        available = min(available, (int(limit) - int(used)) // 2**20)
    return available


def total_memory_mb() -> int | None:
    """Return the machine's total memory where the OS reports it (macOS, BSD), or None (Windows)."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2**20
    except (AttributeError, ValueError, OSError):
        return None


def read_text(path: Path) -> str:
    """Return a small system file's text, or "" when it doesn't exist or can't be read."""
    try:
        return path.read_text().strip()
    except OSError:
        return ""
