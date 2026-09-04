"""Hermes memory-provider discovery entry point."""

from .provider import DurableMemoryProvider
from .service import DurableMemory


def create_provider(ctx=None) -> DurableMemoryProvider:
    """Create directly, or register with older collector-based Hermes loaders."""
    provider = DurableMemoryProvider(DurableMemory())
    if ctx is not None:
        ctx.register_memory_provider(provider)
    return provider
