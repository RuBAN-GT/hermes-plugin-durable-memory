"""Hermes transport adapters."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .i18n import t
from .models import CommandError
from .provider import DurableMemoryProvider
from .service import DurableMemory


def _cli_setup(parser: Any) -> None:
    parser.add_argument(
        "arguments", nargs="*", help="durable-memory action and options"
    )


def _cli_handler(memory: DurableMemory, args: Any) -> None:
    try:
        print(memory.execute(shlex.join(args.arguments)))
    except (CommandError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None


async def _request_human_decision(
    memory: DurableMemory,
    human_decisions: Any,
    payload: dict[str, Any],
    session_key: str,
) -> dict[str, Any]:
    if payload.get("status") != "pending" or not session_key:
        return payload
    body = t(
        "human_decision_body",
        operation=t(f"operation_{payload['operation']}"),
        type=payload["type"],
        identity=payload["identity"],
        text=payload["text"],
        id=payload["id"],
    )[:3500]
    result = await human_decisions.request(
        title=t("human_decision_title"),
        body=body,
        choices=("approve", "reject"),
        session_key=session_key,
        timeout_s=min(300, memory.settings.policy.ttl_seconds),
    )
    if result.get("ok"):
        decided = memory.decide(payload["id"], str(result.get("decision")))
        decided["human_decision"] = result
        return decided
    payload["human_decision"] = result
    error = result.get("error", "gateway_unavailable")
    payload["message"] = (
        f"{payload['message']}\n{t('human_decision_unavailable', error=error)}"
    )
    return payload


def command_handler(memory: DurableMemory, human_decisions: Any | None = None):
    async def handle(raw_args: str, *, session_key: str = "") -> str:
        try:
            payload = memory.execute_payload(raw_args)
            if human_decisions is not None:
                payload = await _request_human_decision(
                    memory, human_decisions, payload, session_key
                )
            return str(payload["message"])
        except (CommandError, OSError, ValueError) as error:
            return str(error)

    return handle


def _tool_handler(memory: DurableMemory):
    async def handle(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "")
        options = []
        for key in (
            "query",
            "namespace",
            "operation",
            "type",
            "identity",
            "text",
            "record-id",
            "expected-revision",
            "request-id",
            "slug",
            "kind",
            "profile",
            "capability",
            "runtime-role",
            "payload",
            "filter",
            "filters",
            "fields",
        ):
            value = args.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            options.extend([f"--{key}", str(value)])
        try:
            if action in {
                "search",
                "propose",
                "create-inventory",
                "list-inventories",
            }:
                command = shlex.join([action, *options])
            else:
                command = action
            payload = memory.execute_payload(command)
            return json.dumps({"ok": True, **payload}, ensure_ascii=False)
        except CommandError as error:
            return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)

    return handle


def register(ctx: Any) -> None:
    """Register slash, CLI, tool, and optional memory-provider adapters."""
    memory = DurableMemory()
    # Memory-provider discovery captures this first; later registrations are
    # delegated to the regular PluginContext by Hermes.
    ctx.register_memory_provider(DurableMemoryProvider(memory))
    ctx.register_command(
        "durable-memory",
        command_handler(memory, ctx.human_decisions),
        description="Namespaced, approval-gated durable memory",
        args_hint=(
            "<doctor|namespaces|create-namespace|create-inventory|list-inventories|grant|search|propose|"
            "pending|approve|reject|migrate|migration-status|bootstrap-profile> "
            "[options]"
        ),
    )
    ctx.register_cli_command(
        "durable-memory",
        "Namespaced, approval-gated durable memory",
        _cli_setup,
        lambda args: _cli_handler(memory, args),
        description="Search, propose, and approve durable memory records.",
    )
    ctx.register_tool(
        name="durable_memory",
        toolset="durable-memory",
        schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "doctor",
                        "namespaces",
                        "create-inventory",
                        "list-inventories",
                        "search",
                        "propose",
                        "pending",
                    ],
                },
                "query": {"type": "string"},
                "namespace": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["create", "update", "delete"],
                },
                "type": {"type": "string"},
                "identity": {"type": "string"},
                "text": {"type": "string"},
                "record-id": {"type": "string"},
                "expected-revision": {"type": "integer"},
                "request-id": {"type": "string"},
                "slug": {"type": "string"},
                "kind": {"type": "string", "enum": ["private", "shared"]},
                "profile": {"type": "string"},
                "capability": {
                    "type": "string",
                    "enum": ["read", "propose", "approve", "admin"],
                },
                "runtime-role": {"type": "string"},
                "payload": {"type": "object"},
                "filter": {"type": "object"},
                "filters": {"type": "object"},
                "fields": {"type": "object"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=_tool_handler(memory),
        is_async=True,
        description=(
            "Search durable memory or propose create/update/delete. "
            "Writes follow the profile approval policy."
        ),
    )
