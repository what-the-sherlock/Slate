"""Vercel entry point: Vercel's Python runtime serves the ASGI ``app`` it finds here."""

from api.main import app

__all__ = ["app"]
