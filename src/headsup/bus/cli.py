"""`hu bus acl apply | show | remove`.

The ACL is the server-side half of the single-writer rule: `compiler` may
write `advisories`, `agent` may read and consume it but never append to it.
Applying it is a one-off against a running Redis, so it lives on the CLI
rather than in application startup. `apply` saves to the aclfile, so the users
survive a restart.

    uv run hu infra up
    uv run hu bus acl apply
    uv run hu bus acl show
"""

from __future__ import annotations

import asyncio
import os

import typer
from redis import asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from rich.console import Console
from rich.table import Table

from headsup.bus.acl import (
    DEFAULT_PASSWORD,
    ENV_PASSWORD,
    PROTECTED_STREAM,
    AclError,
    Applied,
    Role,
    apply,
    describe,
    load,
    password_for,
    remove,
    save,
)
from headsup.bus.client import DEFAULT_REDIS_URL

bus_app = typer.Typer(
    help="Redis Streams: ACL users for the single-writer rule.", no_args_is_help=True
)
acl_app = typer.Typer(help="The compiler and agent ACL users.", no_args_is_help=True)
bus_app.add_typer(acl_app, name="acl")

console = Console()
err = Console(stderr=True)

HINT = "redis unreachable - run `uv run hu infra up`"


def _url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)


async def _with_client(action, url: str):  # type: ignore[no-untyped-def]
    client = aioredis.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except RedisConnectionError as exc:
        await client.aclose()
        err.print(f"[red]{HINT}: {url} ({exc})[/red]")
        raise typer.Exit(1) from exc
    try:
        return await action(client)
    finally:
        await client.aclose()


@acl_app.command("apply")
def acl_apply(
    lock_default: bool = typer.Option(
        False,
        "--lock-default",
        help=(
            "Also take write access to advisories away from the `default` user. "
            "Makes `compiler` the only writer on the server; leaves `default` with "
            "stream access only."
        ),
    ),
    persist: bool = typer.Option(
        True,
        "--persist/--no-persist",
        help="Run ACL SAVE so the users survive a Redis restart.",
    ),
) -> None:
    """Create or update the `compiler` and `agent` users."""

    async def action(client: aioredis.Redis) -> Applied:
        try:
            return await apply(client, lock_default=lock_default, persist=persist)
        except AclError as exc:
            # The users are applied in memory either way; only persistence failed.
            err.print(f"[yellow]applied, but not persisted: {exc}[/yellow]")
            return await apply(client, lock_default=lock_default, persist=False)

    applied = asyncio.run(_with_client(action, _url()))
    table = Table(title=f"redis acl ({_url()})")
    table.add_column("user")
    table.add_column(f"{PROTECTED_STREAM} access")
    table.add_column("password from")
    for role in Role:
        access = "read + write" if role is Role.COMPILER else "read + consume, no append"
        source = (
            ENV_PASSWORD[role]
            if os.environ.get(ENV_PASSWORD[role], "").strip()
            else f"development default ({DEFAULT_PASSWORD[role]!r})"
        )
        table.add_row(role.value, access, source)
    if "default" in applied:
        table.add_row("default", "read + consume, no append", "nopass")
    console.print(table)
    console.print(f"[green]applied {len(applied)} ACL user(s)[/green]")
    if applied.persisted:
        console.print(f"[green]saved to {applied.aclfile}: survives a Redis restart[/green]")
    else:
        console.print(
            "[yellow]not saved: these users live in memory and are lost on restart[/yellow]"
        )

    unset = [role for role in Role if not os.environ.get(ENV_PASSWORD[role], "").strip()]
    if unset:
        console.print(
            "[yellow]put these in .env so each agent connects as its own user:[/yellow]\n"
            + "\n".join(f"  {ENV_PASSWORD[role]}={password_for(role)}" for role in unset)
        )


@acl_app.command("show")
def acl_show() -> None:
    """Print `ACL LIST` as the server has it."""

    async def action(client: aioredis.Redis) -> list[str]:
        return await describe(client)

    for line in asyncio.run(_with_client(action, _url())):
        user = line.split()[1] if len(line.split()) > 1 else "?"
        colour = "green" if user in {role.value for role in Role} else "dim"
        console.print(f"[{colour}]{line}[/{colour}]")


@acl_app.command("persist")
def acl_persist() -> None:
    """Write the current ACL users to the aclfile (`ACL SAVE`)."""

    async def action(client: aioredis.Redis) -> str:
        return await save(client)

    try:
        path = asyncio.run(_with_client(action, _url()))
    except AclError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]saved the ACL to {path}[/green]")


@acl_app.command("reload")
def acl_reload() -> None:
    """Discard the in-memory ACL and re-read the aclfile (`ACL LOAD`).

    The restart path without the restart: useful for checking that what is on
    disk is what you think it is.
    """

    async def action(client: aioredis.Redis) -> str:
        return await load(client)

    try:
        path = asyncio.run(_with_client(action, _url()))
    except AclError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]reloaded the ACL from {path}[/green]")


@acl_app.command("remove")
def acl_remove() -> None:
    """Delete the `compiler` and `agent` users."""

    async def action(client: aioredis.Redis) -> int:
        return await remove(client)

    removed = asyncio.run(_with_client(action, _url()))
    console.print(f"[green]removed {removed} ACL user(s)[/green]")
