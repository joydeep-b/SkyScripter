"""
Discord bot for SFRO Ekos automation.

Listens for simple keywords in a Discord channel and issues Ekos/kstars
automation commands (power, scheduler, park/disconnect). Intended to be
reusable by SFRO (Starfront Remote Observatories) members.
"""

import asyncio
import logging
import argparse
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, TypeVar

import discord

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_POWER_SCRIPT = BASE_DIR.parent / "power.sh"
LOG_FILE = BASE_DIR / "sfro_discord_bot.log"

# Set up root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),  # console
        logging.FileHandler(LOG_FILE)  # file
    ],
)
logger = logging.getLogger("sfro.discord.bot")

# You MUST enable message content intent both in code and in the Dev Portal.
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True


@dataclass
class BotConfig:
    token: str
    channel_id: int
    power_script: Path = DEFAULT_POWER_SCRIPT
    open_keyword: str = "opening"
    close_keyword: str = "closing"
    power_on_wait: int = 60
    park_wait: int = 120


def _read_secret_file(name: str) -> Optional[str]:
    """Return the contents of a local secret file if it exists."""
    path = BASE_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


T = TypeVar("T")


def _resolve_value(
    name: str,
    cli_value: Optional[T],
    secret_value: Optional[T],
    default: Optional[T] = None,
    redact: bool = False,
) -> Tuple[T, str]:
    """
    Pick value with precedence: CLI > secrets > default.
    Returns the chosen value and a source string; logs the source.
    """
    if cli_value is not None:
        display = "(hidden)" if redact else cli_value
        logger.info("%s: using CLI argument -> %s", name, display)
        return cli_value, "cli"
    if secret_value is not None:
        display = "(hidden)" if redact else secret_value
        logger.info("%s: using secrets file -> %s", name, display)
        return secret_value, "secrets"
    display = "(hidden)" if redact else default
    logger.info("%s: using default -> %s", name, display)
    return default, "default"


def load_config(args: argparse.Namespace) -> BotConfig:
    """
    Build BotConfig from CLI flags or local secret files.
    Precedence: CLI > secrets file > defaults.
    """
    token_secret = _read_secret_file(".discord_token")
    channel_secret = _read_secret_file(".discord_channel_id")

    token, _ = _resolve_value("token", args.token, token_secret, redact=True)
    if not token:
        print("Provide --token or create .discord_token alongside this script.")
        sys.exit(1)

    channel_raw, _ = _resolve_value("channel_id", args.channel_id, channel_secret)
    if channel_raw is None:
        print("Provide --channel-id or create .discord_channel_id alongside this script.")
        sys.exit(1)
    try:
        channel_id = int(channel_raw)
    except ValueError:
        print("--channel-id / .discord_channel_id must be an integer.")
        sys.exit(1)

    power_script_path, _ = _resolve_value(
        "power_script",
        Path(args.power_script) if args.power_script else None,
        None,
        DEFAULT_POWER_SCRIPT,
    )

    open_keyword, _ = _resolve_value("open_keyword", args.open_keyword, None, "opening")
    close_keyword, _ = _resolve_value("close_keyword", args.close_keyword, None, "closing")
    power_on_wait, _ = _resolve_value("power_on_wait", args.power_on_wait, None, 60)
    park_wait, _ = _resolve_value("park_wait", args.park_wait, None, 120)

    if not power_script_path.exists():
        logger.warning("Power script not found at %s", power_script_path)

    return BotConfig(
        token=token,
        channel_id=channel_id,
        power_script=power_script_path,
        open_keyword=open_keyword.lower(),
        close_keyword=close_keyword.lower(),
        power_on_wait=int(power_on_wait),
        park_wait=int(park_wait),
    )


class EkosWatchClient(discord.Client):
    """Discord client that reacts to open/close keywords and runs Ekos commands."""

    def __init__(self, config: BotConfig):
        super().__init__(intents=intents)
        self.config = config

    async def on_ready(self):
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)
        channel = await self._resolve_channel(self.config.channel_id)
        logger.info("Watching channel id=%s -> %s", self.config.channel_id, channel)

    async def on_message(self, message: discord.Message):
        logger.info("Received message: %s", message)

        # Ignore messages from ourselves
        if message.author.id == self.user.id:
            return

        # Only respond inside the configured channel
        if message.channel.id != self.config.channel_id:
            return

        author = f"{message.author} (id={message.author.id})"
        where = f"#{getattr(message.channel, 'name', 'DM')} (id={message.channel.id})"
        content_lower = message.content.lower()
        logger.info("[%s] %s: %r", where, author, message.content)

        if self.config.open_keyword in content_lower:
            await self.handle_opening()
        elif self.config.close_keyword in content_lower:
            await self.handle_closing()

        for attachment in message.attachments:
            logger.info("attachment: %s -> %s", attachment.filename, attachment.url)

    async def handle_opening(self):
        logger.info("Roof opening detected")
        self._run_command(f"{self.config.power_script} on")
        logger.info("Waiting %s seconds to power on...", self.config.power_on_wait)
        await asyncio.sleep(self.config.power_on_wait)
        logger.info("Starting scheduler")
        self._run_command("qdbus org.kde.kstars /KStars/Ekos/Scheduler start")

    async def handle_closing(self):
        logger.info("Roof closing detected")
        logger.info("Stopping scheduler")
        self._run_command("qdbus org.kde.kstars /KStars/Ekos/Scheduler stop")
        logger.info("Parking the mount")
        self._run_command("qdbus org.kde.kstars /KStars/Ekos/Mount park")
        logger.info("Waiting %s seconds to park...", self.config.park_wait)
        await asyncio.sleep(self.config.park_wait)
        logger.info("Disconnecting devices")
        self._run_command("qdbus org.kde.kstars /KStars/Ekos disconnectDevices")
        self._run_command("qdbus org.kde.kstars /KStars/Ekos stop")
        self._run_command(f"{self.config.power_script} off")

    def _run_command(self, command: str) -> int:
        """
        Run a shell command (string) and log stdout/stderr.
        Uses shlex.split to avoid shell=True.
        """
        cmd_list = shlex.split(command)
        result = subprocess.run(cmd_list, capture_output=True, text=True)
        if result.stdout:
            logger.info("stdout: %s", result.stdout.strip())
        if result.stderr:
            logger.warning("stderr: %s", result.stderr.strip())
        if result.returncode != 0:
            logger.error("Command '%s' exited with %s", command, result.returncode)
        else:
            logger.info("Command '%s' completed successfully", command)
        return result.returncode

    async def _resolve_channel(self, channel_id: int) -> Optional[discord.abc.GuildChannel]:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.NotFound:
                logger.error("Channel %s not found (bot not in guild?)", channel_id)
            except discord.Forbidden:
                logger.error("No permission to access channel %s", channel_id)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Unexpected error fetching channel %s: %s", channel_id, exc)
        return channel


def exit_handler(signum, frame):
    logger.info("Exiting with signal %s", signum)
    sys.exit(0)


async def main():
    # Auto-reconnect is built-in; wrap run in a task to allow clean shutdowns if needed
    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)

    parser = argparse.ArgumentParser(description="SFRO Ekos Discord bot")
    parser.add_argument("--token", help="Discord bot token")
    parser.add_argument("--channel-id", type=int, help="Discord channel ID to listen to")
    parser.add_argument("--power-script", help="Path to power control script (on/off)")
    parser.add_argument("--open-keyword", help="Keyword to trigger opening flow (default: opening)")
    parser.add_argument("--close-keyword", help="Keyword to trigger closing flow (default: closing)")
    parser.add_argument("--power-on-wait", type=int, help="Seconds to wait after power on (default: 60)")
    parser.add_argument("--park-wait", type=int, help="Seconds to wait while parking (default: 120)")
    args = parser.parse_args()

    config = load_config(args)
    client = EkosWatchClient(config)

    try:
        await client.start(config.token)
    except KeyboardInterrupt:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
