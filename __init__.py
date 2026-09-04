"""Directory-plugin shim for Hermes GitHub installs."""

if __package__:
    from .hermes_durable_memory.general_plugin import register
else:
    from hermes_durable_memory.general_plugin import register

__all__ = ["register"]
