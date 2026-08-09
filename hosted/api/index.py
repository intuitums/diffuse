"""Vercel ASGI entrypoint for the GitHub Integration Service."""

from github_integration.app import app

__all__ = ["app"]
