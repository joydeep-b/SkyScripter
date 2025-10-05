import os
import asyncio
import logging
import discord
import sys
import subprocess
import time
import signal

# Read secrets from environment variables
TOKEN = None
TARGET_CHANNEL_ID = None
# Set up root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),                 # console
        logging.FileHandler(".discord_bot.log")  # file
    ]
)

script_dir = os.path.dirname(__file__)
POWER_ON_SCRIPT = os.path.join(script_dir, '..', 'power.sh')

# You MUST enable message content intent both here and in the Dev Portal
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

def ExecuteCommand(logging, command):
    command_parts = command.split(" ")
    result = subprocess.run(command_parts)
    logging.info(f"Command '{command}' returned {result.returncode}")
    return result

class WatchClient(discord.Client):
    async def on_ready(self):
        logging.info(f"Logged in as {self.user} (id={self.user.id})")
        if TARGET_CHANNEL_ID:
            ch = self.get_channel(TARGET_CHANNEL_ID)
            if ch is None:
                try:
                    ch = await self.fetch_channel(TARGET_CHANNEL_ID)
                except discord.NotFound:
                    logging.error(f"Channel {TARGET_CHANNEL_ID} not found (bot not in guild?)")
                except discord.Forbidden:
                    logging.error(f"No permission to access channel {TARGET_CHANNEL_ID}")
                except Exception as e:
                    logging.error(f"Unexpected error fetching channel: {e}")
            logging.info(f"Watching channel id={TARGET_CHANNEL_ID} -> {ch}")
        else:
            logging.warning("CHANNEL_ID not set; will log all channels this bot can see.")

    async def on_message(self, message: discord.Message):
        logging.info(f"Received message: {message}")
        # Ignore messages from ourselves
        if message.author.id == self.user.id:
            return

        # If a specific channel is set, filter on it
        if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
            return

        # Print a simple line per message
        # You can enrich this with attachments, embeds, etc.
        author = f"{message.author} (id={message.author.id})"
        where = f"#{getattr(message.channel, 'name', 'DM')} (id={message.channel.id})"
        logging.info(f"[{where}] {author}: {message.content!r}")
        if "opening" in message.content.lower():
            logging.info(f"Roof opening detected")
            result = ExecuteCommand(logging, POWER_ON_SCRIPT + " on")
            logging.info(f"Waiting 60 seconds to power on...")
            # time.sleep(60)
            await asyncio.sleep(60)
            logging.info(f"Starting scheduler")
            result = ExecuteCommand(logging,
                "qdbus org.kde.kstars /KStars/Ekos/Scheduler start")
            logging.info(f"Scheduler started with result: {result}")
        elif "closing" in message.content.lower():
            logging.info(f"Roof closing detected")
            logging.info(f"Stopping scheduler")
            result = ExecuteCommand(logging, 
                "qdbus org.kde.kstars /KStars/Ekos/Scheduler stop")
            logging.info("Parking the mount")
            result = ExecuteCommand(logging,
                "qdbus org.kde.kstars /KStars/Ekos/Mount park")
            logging.info(f"Waiting 120 seconds to park...")
            # time.sleep(120)
            await asyncio.sleep(120)
            logging.info(f"Disconnecting devices")
            result = ExecuteCommand(logging, 
                "qdbus org.kde.kstars /KStars/Ekos disconnectDevices")
            result = ExecuteCommand(logging, 
                "qdbus org.kde.kstars /KStars/Ekos stop")
            result = ExecuteCommand(logging, POWER_ON_SCRIPT + " off")
        # Example: handle attachments
        for a in message.attachments:
            logging.info(f"  attachment: {a.filename} -> {a.url}")

def exit_handler(signum, frame):
    logging.info(f"Exiting with signal {signum}")
    sys.exit(0)

# Auto-reconnect is built-in; wrap run in a task to allow clean shutdowns if needed
async def main():
    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)
    global TOKEN, TARGET_CHANNEL_ID
    try:
        with open('.discord_token', 'r') as f:
            TOKEN = f.read().strip()
    except FileNotFoundError:
        print("No .discord_token file found. Please create one with the bot token.")
        sys.exit(1)
    try:
        with open('.discord_channel_id', 'r') as f:
            TARGET_CHANNEL_ID = int(f.read().strip())
    except FileNotFoundError:
        print("No .discord_channel_id file found. Please create one with the channel ID.")
        sys.exit(1)
    print(f"TOKEN: {TOKEN}")
    print(f"TARGET_CHANNEL_ID: {TARGET_CHANNEL_ID}")
    client = WatchClient(intents=intents)
    try:
        await client.start(TOKEN)
    except KeyboardInterrupt:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
