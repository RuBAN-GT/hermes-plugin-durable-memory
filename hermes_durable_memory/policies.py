from __future__ import annotations

from dataclasses import dataclass

from .i18n import t
from .models import OPERATIONS, POLICY_ACTIONS, CommandError


@dataclass(frozen=True)
class ApprovalPolicy:
    create: str = "require"
    update: str = "require"
    delete: str = "require"
    ttl_seconds: int = 86400

    def __post_init__(self) -> None:
        for name in OPERATIONS:
            value = getattr(self, name)
            if value not in POLICY_ACTIONS:
                raise CommandError(t("policy_invalid", name=name))
        if self.ttl_seconds < 1:
            raise CommandError(t("ttl_invalid"))

    def for_operation(self, operation: str) -> str:
        if operation not in OPERATIONS:
            raise CommandError(t("unknown_operation"))
        return getattr(self, operation)

    def as_dict(self) -> dict[str, str | int]:
        return {
            "create": self.create,
            "update": self.update,
            "delete": self.delete,
            "ttl_seconds": self.ttl_seconds,
        }
