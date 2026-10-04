"""Thin entrypoint: all startup logic lives in ``app.app``."""

import asyncio

from app.app import run

if __name__ == "__main__":
    asyncio.run(run())
