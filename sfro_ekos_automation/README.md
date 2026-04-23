SFRO Ekos Discord Bot
=====================

Discord listener that reacts to roof **opening/closing** messages in a specific channel and triggers Ekos automation (power on/off, scheduler start/stop, park/disconnect). 

Setup
-----
- Python: `pip install discord.py`
- Place the bot in a machine that can run `qdbus` against KStars/Ekos and can reach your power-control script.
- Ensure Discord message content intent is enabled in the Discord Developer Portal **and** in the code (already set).

Configuration
-------------
Secrets file (optional, used when flags are not provided):
- `.discord_token`: Discord bot token.
- `.discord_channel_id`: Channel ID the bot should listen to.

CLI flags (take precedence over secrets):
- `--token`: Discord bot token.
- `--channel-id`: Channel ID to listen to.
- `--power-script`: Path to power control script (default: `../power.sh`).
- `--open-keyword`: Word to trigger opening flow (default: `opening`).
- `--close-keyword`: Word to trigger closing flow (default: `closing`).
- `--power-on-wait`: Seconds to wait after power on (default: 60).
- `--park-wait`: Seconds to wait while parking (default: 120).

Discord Integration
--------------

To use this on your own Discord server, first make sure the roof notifications are arriving in a dedicated text channel in **your** server.

If the upstream roof-status feed is posted in a Discord **Announcement** channel, you can use Discord's **Follow** feature to mirror those posts into a channel in your own server. Create a destination channel in your server, open the source Announcement channel, choose **Follow**, and select your destination channel. Only **published** announcement posts are forwarded.

Then create and add your own bot:

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new **Application**.
2. In the **Bot** tab, add a bot user.
3. Copy the bot token and save it in `.discord_token`, or pass it with `--token`.
4. In the bot settings, enable **Message Content Intent**. This script reads message text and will not work correctly without it.
5. In **OAuth2 -> URL Generator**, select the `bot` scope and invite the bot to your server.
6. Make sure the bot can access the destination channel that receives the roof notifications.

Next, get the channel ID the bot should listen to:

1. In Discord, enable **Developer Mode**.
2. Right-click the destination channel and choose **Copy Channel ID**.
3. Save that value in `.discord_channel_id`, or pass it with `--channel-id`.

Recommended setup:

- Use a **dedicated channel** for roof-status notifications.
- Add **only your own bot** to your server; it does not need to join the upstream source server if messages are being mirrored into your server.
- If your notification messages use different wording than `opening` / `closing`, override them with `--open-keyword` and `--close-keyword`.


Run
---
From this folder:
- `python discord_ekos_bot.py --token ... --channel-id ...`
- If flags are omitted, the bot will look for `.discord_token` and `.discord_channel_id`.
- Log file: `sfro_discord_bot.log` in the same directory.

Notes for SFRO reuse
--------------------
- site-specific commands are inside the `_run_command` calls in `discord_ekos_bot.py`. If your setup needs extra steps (e.g., heater startup), add them to `handle_opening`/`handle_closing`.
- If your power script lives elsewhere, set `POWER_SCRIPT` to its absolute path.
