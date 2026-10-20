"""Thin wrapper around `docker compose` for the Redis and PostGIS services."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

#: Services that `hu infra status` reports on, in display order.
SERVICES = ("redis", "postgis")

# Docker Desktop installs here but does not always land on the PATH of a
# Git Bash or MSYS shell, so fall back to the known locations.
_FALLBACK_DOCKER_PATHS = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    r"C:\ProgramData\DockerDesktop\version-bin\docker.exe",
    "/usr/bin/docker",
    "/usr/local/bin/docker",
)


class InfraError(RuntimeError):
    """Docker is missing, unreachable, or a compose command failed."""


@dataclass(frozen=True)
class ServiceState:
    """One service as `docker compose ps` sees it."""

    name: str
    state: str = "absent"
    health: str = "unknown"

    @property
    def healthy(self) -> bool:
        return self.state == "running" and self.health == "healthy"


def docker_bin() -> str:
    """Absolute path to the docker CLI, or raise InfraError."""
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _FALLBACK_DOCKER_PATHS:
        if Path(candidate).exists():
            return candidate
    raise InfraError(
        "docker CLI not found. Install Docker Desktop (WSL2 backend) and make sure "
        "`docker` is on PATH."
    )


def _child_env(docker: str) -> dict[str, str]:
    """Env for the compose child, with the docker bin dir on PATH.

    Credential helpers such as docker-credential-desktop sit next to docker.exe
    and are looked up on PATH, so calling docker by absolute path is not enough.
    """
    bin_dir = str(Path(docker).parent)
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        path = bin_dir + os.pathsep + path if path else bin_dir
    return {**os.environ, "PATH": path, "DOCKER_CLI_HINTS": "false"}


def compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run `docker compose -f docker-compose.yml <args>` from the repo root."""
    docker = docker_bin()
    cmd = [docker, "compose", "-f", str(COMPOSE_FILE), *args]
    try:
        return subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=not capture,
            text=True,
            capture_output=capture,
            env=_child_env(docker),
        )
    except FileNotFoundError as exc:  # pragma: no cover - docker vanished mid-run
        raise InfraError(f"could not run {cmd[0]}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise InfraError(
            f"`docker compose {' '.join(args)}` failed with exit {exc.returncode}"
        ) from exc


def status() -> list[ServiceState]:
    """Current state and healthcheck result for every service in SERVICES."""
    proc = compose("ps", "--all", "--format", "json", capture=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint = detail[-1] if detail else "is the Docker daemon running?"
        raise InfraError(f"docker compose ps failed: {hint}")

    # Compose emits one JSON object per line (older versions emit a JSON array).
    rows: list[dict[str, object]] = []
    payload = proc.stdout.strip()
    if payload.startswith("["):
        rows = json.loads(payload)
    else:
        rows = [json.loads(line) for line in payload.splitlines() if line.strip()]

    by_service = {str(row.get("Service", "")): row for row in rows}
    states = []
    for name in SERVICES:
        row = by_service.get(name)
        if row is None:
            states.append(ServiceState(name))
            continue
        states.append(
            ServiceState(
                name=name,
                state=str(row.get("State", "unknown")),
                health=str(row.get("Health") or "none"),
            )
        )
    return states
