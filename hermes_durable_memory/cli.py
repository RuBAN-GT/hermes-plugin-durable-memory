"""CLI discovery entry point for the durable-memory provider."""

from __future__ import annotations

import shlex
from typing import Any

from .models import CommandError
from .service import DurableMemory


def _handle(args: Any) -> None:
    try:
        print(DurableMemory().execute(shlex.join(args.arguments)))
    except (CommandError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None


def register_cli(parser: Any) -> None:
    """Register ``hermes durable-memory <action> [options]``."""
    parser.add_argument("arguments", nargs="*")
    parser.set_defaults(func=_handle)
