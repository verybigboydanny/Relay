from __future__ import annotations

import asyncio
import signal
import sys

from relay.config import load_settings
from relay.logging_setup import setup_logging
from relay.utils.errors import ConfigError


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    setup_logging(settings.cfg.log_level, settings.base_dir / "logs")
    from relay.bot import RelayBot
    bot = RelayBot(settings)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, main_task.cancel)
            except (NotImplementedError, RuntimeError):
                pass
        async with bot:
            await bot.start(settings.secrets.discord_token.get_secret_value())

    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
