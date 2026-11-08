"""Helpers shared by the tool modules."""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lira_sapr_mcp.errors import LiraError, LiraInputError
from lira_sapr_mcp.numbers import parse_numbers

READ_ONLY = ToolAnnotations(readOnlyHint=True)

# Words that stand for numbers wherever a tool takes node or element numbers.
PARTS = ("selected", "visible")
PARTS_HELP = (
    ' Instead of numbers: "selected" (what the user selected in LIRA\'s window) or '
    '"visible" (what LIRA shows, e.g. after a fragmentation); the model must be open.'
)

# LIRA API functions each tool needs, filled by ``needs``: lira_status lists the tools a
# connected LIRA release cannot run.
REQUIRES: dict[str, tuple[str, ...]] = {}


def lira_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Report LIRA errors to the agent as plain tool errors instead of tracebacks."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except LiraError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def extend_doc[F: Callable[..., Any]](extra: str) -> Callable[[F], F]:
    """Append ``extra`` to a tool's docstring. Put it right above ``def``: the MCP
    server reads the description when the tool is registered, so a later append is lost."""

    def wrap(fn: F) -> F:
        fn.__doc__ = (fn.__doc__ or "") + extra
        return fn

    return wrap


def plain(obj: Any) -> Any:
    """Dataclasses to dicts, recursively, for structured tool output."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list | tuple):
        return [plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    return obj


async def backup_before_change(session: Any, ref: str, fresh: bool = False) -> str | None:
    """Back up a saved model before the session first changes it, or before every
    change when ``fresh``; the backup's path."""
    info = await session.run(session.adapter.document_info, ref)
    if not info.saved:
        return None
    backup = session.backups.ensure(info.path, fresh=fresh)
    return str(backup) if backup else None


def part_of(value: object) -> str | None:
    """The word of ``PARTS`` a tool argument says instead of numbers, if any."""
    word = value.strip().lower() if isinstance(value, str) else None
    return word if word in PARTS else None


def require_api(session: Any, what: str, *capabilities: str) -> None:
    """Refuse clearly when this LIRA release lacks the API functions ``what`` needs."""
    missing = [c for c in capabilities if c not in session.adapter.capabilities]
    if missing:
        raise LiraInputError(
            f"{what} needs {', '.join(missing)} of LIRA's API, which LIRA "
            f"{session.install.year} does not have"
        )


async def numbers_in(
    session: Any, ref: str, value: list[int] | str | None, kind: str, edit: bool = False
) -> list[int] | None:
    """Node or element numbers (``kind`` "nodes" or "elements") of a tool argument: a
    list, LIRA-style ranges, or "selected" / "visible" in the open model ``ref``. For an
    ``edit``, "visible" of a model that is not fragmented (all of it) is refused: the
    user would not expect every element to change."""
    word = part_of(value)
    if word is None:
        return parse_numbers(value)
    if word == "selected":
        require_api(session, f'"{word}"', "GetSelection")
    found = await session.run(session.adapter.selection, ref, word, kind, edit)
    numbers = found[kind]
    if not numbers:
        hint = "select them" if word == "selected" else "show them"
        raise LiraInputError(
            f"No {kind} are {word} in {ref}: {hint} in LIRA's window first (or name the "
            "model with model=, if it is another one)"
        )
    if edit and word == "visible" and found.get("whole"):
        raise LiraInputError(
            f"All {len(numbers)} {kind} of {ref} are visible: nothing is fragmented. To change "
            "every one of them, give their numbers"
        )
    return numbers


# --8<-- [start:needs]
def needs(session: Any, *capabilities: str) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
    """Refuse a tool clearly when this LIRA release lacks an API function it needs."""

    def wrap[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        REQUIRES[fn.__name__] = capabilities

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            adapter = session.adapter
            if not adapter.connected:
                await session.run(adapter.connect)
            require_api(session, fn.__name__, *capabilities)
            return await fn(*args, **kwargs)

        return wrapper

    return wrap


# --8<-- [end:needs]


def unavailable_tools(capabilities: frozenset[str]) -> list[str]:
    return sorted(t for t, caps in REQUIRES.items() if not set(caps) <= capabilities)
