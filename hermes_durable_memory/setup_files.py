"""Prepare profile updates without evaluating or exposing stored secrets."""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import t
from .models import CommandError


def merge_env(original: str, values: dict[str, str | None]) -> str:
    # Hermes already depends on python-dotenv. Preserve full original bindings,
    # including unrelated multiline secrets and comments, rather than splitlines.
    from dotenv.parser import parse_stream

    kept = []
    for binding in parse_stream(io.StringIO(original)):
        if binding.error:
            raise CommandError(t("setup_env_invalid"))
        if binding.key not in values:
            kept.append(binding.original.string)
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    for key, value in values.items():
        if value is not None:
            if any(c in value for c in "\r\n\x00"):
                raise CommandError(t("setup_input_invalid"))
            # Values written here are URL-encoded DSNs, booleans, numeric policy
            # fields, or validated profile IDs; no shell substitutions survive.
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            text += f"{key}='{escaped}'\n"
    return text


def _read(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CommandError(t("setup_file_unsafe"))
    return path.read_bytes() if path.exists() else None


def _atomic_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(repr=False)
class ProfileFiles:
    home: Path
    original_env: bytes | None = field(init=False)
    original_config: bytes | None = field(init=False)

    def __post_init__(self):
        if not self.home.is_dir():
            raise CommandError(t("setup_profile_missing"))
        self.original_env = _read(self.home / ".env")
        self.original_config = _read(self.home / "config.yaml")

    def render(
        self, values: dict[str, str | None], *, activate: bool
    ) -> tuple[bytes, bytes]:
        import yaml

        env = merge_env((self.original_env or b"").decode("utf-8-sig"), values)
        try:
            config = yaml.safe_load(self.original_config or b"")
        except yaml.YAMLError:
            raise CommandError(t("setup_config_invalid")) from None
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise CommandError(t("setup_config_invalid"))
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            raise CommandError(t("setup_config_invalid"))
        # Detach edited nodes from YAML aliases used by unrelated settings.
        plugins = config["plugins"] = dict(plugins)
        for key in ("enabled", "disabled"):
            items = plugins.setdefault(key, [])
            if not isinstance(items, list) or not all(
                isinstance(v, str) for v in items
            ):
                raise CommandError(t("setup_config_invalid"))
            plugins[key] = list(items)
        if "durable-memory" not in plugins["enabled"]:
            plugins["enabled"].append("durable-memory")
        plugins["disabled"] = [v for v in plugins["disabled"] if v != "durable-memory"]
        if activate:
            memory = config.setdefault("memory", {})
            if not isinstance(memory, dict):
                raise CommandError(t("setup_config_invalid"))
            memory = config["memory"] = dict(memory)
            memory["provider"] = "durable-memory"
        return env.encode("utf-8"), yaml.safe_dump(
            config, allow_unicode=True, sort_keys=False
        ).encode("utf-8")

    def commit(self, rendered: tuple[bytes, bytes]) -> None:
        env_path, config_path = self.home / ".env", self.home / "config.yaml"
        if (
            _read(env_path) != self.original_env
            or _read(config_path) != self.original_config
        ):
            raise CommandError(t("setup_files_changed"))
        _atomic_write(env_path, rendered[0])
        try:
            _atomic_write(config_path, rendered[1])
        except BaseException:
            if self.original_env is None:
                env_path.unlink()
            else:
                _atomic_write(env_path, self.original_env)
            raise
