"""Hermes general-plugin adapters for commands, CLI, and human approvals."""

from __future__ import annotations

import shlex
from typing import Any

from .i18n import t
from .models import CommandError
from .service import DurableMemory


def _cli_setup(parser: Any) -> None:
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in (
        "doctor",
        "namespaces",
        "create-namespace",
        "create-inventory",
        "list-inventories",
        "grant",
        "search",
        "propose",
        "pending",
        "approve",
        "reject",
        "migrate",
        "migration-status",
        "bootstrap-profile",
    ):
        action_parser = subparsers.add_parser(action)
        for option in (
            "query",
            "namespace",
            "operation",
            "type",
            "identity",
            "text",
            "record-id",
            "expected-revision",
            "replace",
            "request-id",
            "slug",
            "kind",
            "profile",
            "capability",
            "runtime-role",
            "payload",
            "filter",
            "filters",
            "cursor",
            "sort",
            "descending",
            "fields",
        ):
            action_parser.add_argument(f"--{option}")


def _cli_handler(memory: DurableMemory, args: Any) -> None:
    try:
        options = []
        for key, value in vars(args).items():
            if key == "action" or value is None:
                continue
            options.extend([f"--{key.replace('_', '-')}", str(value)])
        print(memory.execute(shlex.join([args.action, *options])))
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


def register(ctx: Any) -> None:
    """Register adapters that require a regular Hermes PluginContext."""
    memory = DurableMemory()
    ctx.register_command(
        "durable-memory",
        command_handler(memory, ctx.human_decisions),
        description="Namespaced, approval-gated durable memory",
        args_hint=(
            "<doctor|namespaces|create-namespace|create-inventory|list-inventories|"
            "grant|search|propose|pending|approve|reject|migrate|migration-status|"
            "bootstrap-profile> [options]"
        ),
    )
    ctx.register_cli_command(
        "durable-memory",
        "Namespaced, approval-gated durable memory",
        _cli_setup,
        lambda args: _cli_handler(memory, args),
        description="Search, propose, and approve durable memory records.",
    )
