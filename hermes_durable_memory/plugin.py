"""Backward-compatible import path for the general Hermes plugin."""

from .general_plugin import command_handler, register

__all__ = ["command_handler", "register"]
