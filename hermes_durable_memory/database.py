"""PostgreSQL schema handling for internal durable-memory SQL."""

from __future__ import annotations

import re
from typing import Any

from .models import CommandError

DEFAULT_SCHEMA = "durable_memory"
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}$")


def validate_schema(value: str) -> str:
    """Return a safe, unquoted PostgreSQL schema identifier."""
    schema = value.strip()
    if not _SCHEMA_NAME.fullmatch(schema):
        raise CommandError(
            "DURABLE_MEMORY_SCHEMA must be a lowercase PostgreSQL identifier."
        )
    return schema


def rewrite_schema(statement: str, schema: str) -> str:
    """Replace the fixed internal schema name after identifier validation."""
    return statement.replace(DEFAULT_SCHEMA, schema)


class SchemaConnection:
    """Adapt a psycopg connection so all internal SQL uses one schema."""

    def __init__(self, connection: Any, schema: str) -> None:
        self._connection = connection
        self._schema = schema

    def __enter__(self) -> SchemaConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._connection.__exit__(*args)

    def execute(self, query: Any, *args: object, **kwargs: object) -> Any:
        return self._connection.execute(
            rewrite_schema(query, self._schema) if isinstance(query, str) else query,
            *args,
            **kwargs,
        )
