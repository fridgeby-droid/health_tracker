"""Stable module identity: bot and API share locks for JSON files."""
import asyncio
from bot import main

if __name__ == '__main__':
    asyncio.run(main())
