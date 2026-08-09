"""Vercel ASGI entrypoint for the hosted Diffuse-Agent integration."""

from diffuse_setup.app import app

__all__ = ["app"]
