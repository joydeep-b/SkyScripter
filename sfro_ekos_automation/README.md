SFRO Ekos Discord Bot
=====================

Discord listener that reacts to roof **opening/closing** messages in a specific channel and triggers Ekos automation (power on/off, scheduler start/stop, park/disconnect). Intended for Starfront Remote Observatories (SFRO) members so the same script can be reused on multiple sites.

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

Run
---
From this folder:
- `python discord_ekos_bot.py --token ... --channel-id ...`
- If flags are omitted, the bot will look for `.discord_token` and `.discord_channel_id`.
- Log file: `sfro_discord_bot.log` in the same directory.

Notes for SFRO reuse
--------------------
- Keep site-specific commands inside the `_run_command` calls in `discord_ekos_bot.py`. If your observatory needs extra steps (e.g., dome sync), add them to `handle_opening`/`handle_closing`.
- If your power script lives elsewhere, set `POWER_SCRIPT` to its absolute path.
- Use a dedicated Discord channel per site to avoid cross-triggering. Give each deployment its own bot token.
