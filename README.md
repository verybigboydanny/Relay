# Relay

Relay is a Discord bot that manages a server from plain-English requests. You say what you want ("make a private channel for moderators", "ban Sam in 10 minutes"). Groq plans the steps, and ordinary Python code decides what is actually allowed, asks you to confirm anything risky, runs the steps in order, and writes each one to an audit log.

The model never talks to Discord directly. It can only choose from a fixed list of tools, and every argument is validated, matched against the real server, and authorized in code before anything runs.

You can run it two ways: as a desktop app (a small control panel with a chat, logs, and settings), or headless with `python main.py`.

## Contents

- [Setup](#setup)
- [Create the Discord bot](#create-the-discord-bot)
- [Get a Groq key](#get-a-groq-key)
- [The desktop app](#the-desktop-app)
- [Using Relay in Discord](#using-relay-in-discord)
- [Permissions and confirmation](#permissions-and-confirmation)
- [Memory, scheduling and audit](#memory-scheduling-and-audit)
- [Storage](#storage)
- [Tests](#tests)
- [Running on a server](#running-on-a-server)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Setup

You need Python 3.11 or newer, a Discord bot token, and a Groq API key. Do the two short sections below first ([Create the Discord bot](#create-the-discord-bot) and [Get a Groq key](#get-a-groq-key)), then follow the steps for your system.

The launcher files (`Relay.command` on macOS, `Relay.bat` on Windows) do the install for you on the first run: they create a `.venv` folder, install the requirements, and copy `.env.example` to `.env`. After that they just open the app.

### macOS

1. Install Python 3.11 or newer. The version that ships with macOS is too old. Either download the installer from [python.org](https://www.python.org/downloads/) or use Homebrew:

   ```bash
   brew install python
   ```

   Check it with `python3 --version`.

2. Unzip Relay somewhere permanent, for example `~/Documents/Relay`.

3. Allow the launcher to run. Anything downloaded from the internet is marked with a quarantine flag, and macOS refuses to open a quarantined `.command` file ("Apple could not verify..." or "cannot be opened because it is from an unidentified developer"). Open Terminal and remove the flag from the whole folder:

   ```bash
   xattr -dr com.apple.quarantine "/path/to/Relay"
   chmod +x "/path/to/Relay/Relay.command"
   ```

   Replace `/path/to/Relay` with the real folder. The easy way is to type `xattr -dr com.apple.quarantine ` (with a trailing space) and then drag the Relay folder from Finder into the Terminal window, which pastes its path. The `-r` makes it cover every file inside.

   If you would rather not use Terminal: double-click `Relay.command`, dismiss the warning, then open System Settings, go to Privacy & Security, scroll down, and click "Open Anyway" next to Relay.command.

4. Double-click `Relay.command`. The first run takes a minute while it installs. A Terminal window stays open while Relay is running, and closing it quits the app.

5. In the app, open **Environment**, paste your Discord token and Groq key, and save. Then go to **Home** and press Start.

Optional: give the launcher the Relay icon. Open `assets/icon-mac.png` in Preview, press Cmd+A then Cmd+C, select `Relay.command` in Finder, press Cmd+I, click the small icon at the top left of the info window, and press Cmd+V. The Dock shows the Relay icon and name while the app runs either way.

To run it by hand instead of using the launcher:

```bash
cd /path/to/Relay
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python dashboard.py
```

### Windows

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/). On the first screen of the installer, tick **Add python.exe to PATH**. Check it in PowerShell with `py --version`.

2. Unzip Relay somewhere permanent, for example `C:\Users\you\Relay`. If you downloaded a zip, right-click the zip first, choose Properties, tick **Unblock**, and press OK, then extract it. That avoids a security prompt on every file. If you already extracted it, run this in PowerShell instead:

   ```powershell
   Get-ChildItem -Recurse "C:\path\to\Relay" | Unblock-File
   ```

3. Double-click `Relay.bat`. If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**. A console window shows the install on the first run and closes when the app opens.

4. In the app, open **Environment**, paste your Discord token and Groq key, and save. Then go to **Home** and press Start.

The app window uses Microsoft's WebView2 runtime. It is already present on Windows 11 and on up-to-date Windows 10. If the window doesn't open, install the "Evergreen" runtime from Microsoft.

Optional: a desktop shortcut with the Relay icon. Run this in PowerShell, changing the path:

```powershell
$dir = "C:\path\to\Relay"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\Relay.lnk")
$s.TargetPath = "$dir\Relay.bat"; $s.WorkingDirectory = $dir; $s.IconLocation = "$dir\assets\icon.ico"; $s.WindowStyle = 7
$s.Save()
```

To run it by hand (this avoids the PowerShell script policy that blocks `Activate.ps1`):

```powershell
cd C:\path\to\Relay
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.venv\Scripts\python dashboard.py
```

### Linux

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python dashboard.py --browser
```

`--browser` opens the panel in a normal browser tab. The native window needs GTK or Qt libraries that pywebview documents for each distribution.

### Without the app

If you only want the bot, fill in `.env` and run:

```bash
python main.py
```

Startup stops with exit code 2 and a clear message if the config or credentials are missing or invalid.

### The .env file

```
DISCORD_TOKEN=
GROQ_API_KEY=
TURSO_DATABASE_URL=      # only for Turso storage
TURSO_AUTH_TOKEN=        # only for Turso storage
AUDIT_HMAC_KEY=          # optional, see Memory, scheduling and audit
```

Never commit `.env` or share it. `config.json` holds everything that isn't secret and is checked at startup, so unknown keys and bad values are rejected with a clear message.

## Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and add a bot.
2. Copy the bot token. This is `DISCORD_TOKEN`.
3. Under Bot, then Privileged Gateway Intents, turn on:
   - Server Members Intent, so Relay can find members by name and see role changes.
   - Message Content Intent, so Relay can read mentions and commands and search history.

### Invite it

The app builds an invite link for you on the Home tab. If you make one yourself, use the scopes `bot` and `applications.commands`, and do not give it Administrator. These are the permissions Relay uses:

| Feature | Permission |
|---|---|
| Channels and categories | Manage Channels |
| Roles and permission overwrites | Manage Roles |
| Timeouts | Moderate Members |
| Kick and ban | Kick Members, Ban Members |
| Nicknames | Manage Nicknames |
| Posting, pins, deleting messages | View Channels, Send Messages, Manage Messages, Read Message History |
| Saying who made a change | View Audit Log (optional) |

Grant only what you plan to use. A missing permission is reported for that action and never skipped silently. Relay can only manage roles below its own highest role, so move its role up if needed.

## Get a Groq key

Create a key at [console.groq.com](https://console.groq.com). The default model is `openai/gpt-oss-120b`. Model availability changes over time, so if you see "model isn't available", set `groq.model` in `config.json` to a current one. `groq.reasoning_effort` is passed to reasoning models and dropped automatically if the model rejects it. The `/groq` command (admins) tests the connection and shows whether the model is available.

## The desktop app

Start it with the launcher, or `python dashboard.py`. Use `python dashboard.py --browser` to open it in a browser tab instead.

| Tab | What it does |
|---|---|
| Home | Start, stop and restart Relay. Shows the live connection to Discord, uptime, latency, the servers it's in, a checklist of anything missing, and the invite link. |
| Chat | Talk to Relay. Ask about its status or logs, change its settings, or tell it what to do in Discord. |
| Logs | Live log with level filters and search. Secrets are masked. |
| Config | Every `config.json` setting as a form, or as raw JSON. Validated before saving, with a `.bak` copy of the previous file. |
| Environment | Edit the keys in `.env`. Saved secrets are never sent back to the page in full. |
| Assistant | Name, tone, formality, reply length, language, and standing instructions, with a preview of what gets added to each request. These change wording only, never permissions. |
| Profile | Discord status and activity (with optional rotating lines), and while Relay is online, its profile picture, username, and nickname per server. |
| Appearance | Surface (Charcoal by default, or Graphite, Steel, Slate, Stone) and accent color. |

Config changes apply the next time Relay starts. Profile changes apply right away while it is online.

### Chat

Type a request such as "make a channel called test" or "delete everything except general". Relay runs it through the same planner, permission checks, confirmations and audit log as a Discord message, acting as the server owner. Risky actions show Confirm and Cancel buttons in the chat, and "which one?" questions show option buttons. If Relay is in more than one server, choose the server at the top of the chat. Relay has to be online for this.

Requests like "show the logs" print the real lines from the log file. Changes to Relay itself (start, stop, restart, config edits) appear as buttons you press, so nothing happens without a click.

### Where things are saved

Everything stays on your computer.

| What | Where |
|---|---|
| Settings, including Discord presence | `config.json` |
| Keys | `.env` |
| Surface, accent color and selected server | `data/ui_prefs.json` |
| Chat history | `data/dashboard_chat.json` |
| Profile picture copy | `data/avatar.*` |
| App to bot messages | `data/bridge/` |

### Security

The app only listens on `127.0.0.1`. Every request needs a random key generated at launch, which the app adds for you. It rejects unexpected Host and Origin headers, and it can only edit the known environment variables. Closing the app window stops the bot it started. A bot started separately with `python main.py` is not tracked by the app.

The app and the running bot exchange small files in `data/bridge/` rather than opening a network port.

## Using Relay in Discord

Mention Relay, or reply to one of its messages, and say what you want:

- "Set this server up as a gaming server." Also: community, study, creative. It builds roles, categories, topics, read-only info channels, voice rooms and a private staff area, skips anything that exists, and shows the full layout for you to confirm.
- "Create a text channel called lol under category baby. If baby doesn't exist, create it first."
- "Make it private so only moderators can see it."
- "Create an Events Team role, give it permission to manage events, and assign it to Alex."
- "Delete all channels except general."
- "Warn rambutan for spam, then mute him for 30 minutes."
- "Ban rambutan in 2 minutes."
- "Every Friday at 7 PM post a moderation summary in #admin."
- "Why can't Alex see #announcements?"
- "What happened yesterday?" or "Who changed the permissions on #general?"
- "Undo the last permission change."
- "Remember that announcements go in #news."

Follow-ups such as "make it private" or "mute him too" work for a while using per-user, per-channel context (`context.ttl_minutes`).

Bulk requests ("all", "everything", "everything except X") are handled by dedicated tools: `delete_channels`, `delete_roles` and `clear_messages`. They still show a preview and ask for confirmation first.

Commands (default prefix `r!`, also available as slash commands): `help`, `ask`, `status`, `groq`, `storage`, `permissions`, `config` (`show`, `role`, `channel`, `timezone`), `tasks` (`list`, `cancel`), `memory` (`list`, `remember`, `forget`), `audit` (`recent`, `user`, `target`, `action`, `failures`, `task`, `export`, `verify`), `warn`, `timeout`, `untimeout`, `kick`, `ban`, `unban`, `history`. Moderation commands accept an optional `delay` such as `10m`. All of them go through the same authorization, confirmation and audit path as natural language.

## Permissions and confirmation

Authorization is ordinary Python and never depends on the model.

- **admin**: the server owner, anyone with Discord's Administrator permission (`policy.administrator_is_admin`), or a role mapped to the `admin` group. Role hierarchy still applies, and granting Administrator needs the owner or a real administrator.
- **moderator**, **trusted**, **restricted**, or any group you define in `policy.groups`: each lists the tools it allows (`"*"`, a tool name, `@read`, or a tag like `@moderation`). A grant also requires the matching real Discord permission, such as `ban_members`.
- **restricted** is refused entirely.
- Everyone else can only use open tools: their own memories, reminders, posting where they can already post, searching history they can already read, and listing their own tasks.

Map roles to groups by ID, not name, with `/config role group:moderator role:@Mods` (this needs confirmation) or under `guilds.<guild_id>.roles` in `config.json`. Having Manage Channels in Discord is not enough on its own; the person must also be in a group.

Under `confirmation`, `direct`, `scheduled` and `recurring` can each be `never`, `risky` (medium and high risk tools), or `always`. Some things always ask: granting dangerous permissions, assigning roles that carry them, changing permission groups, copying channel permissions, and bulk kicks and bans. Only the requester can confirm. The plan is hash-checked, and confirmations expire (`ttl_seconds`) and survive restarts.

## Memory, scheduling and audit

### Memory

- Conversation context is kept per server, channel and user, and expires.
- Event memory records channel, role, permission, moderation and config changes made by Relay or seen on Discord, with their source. Visibility is filtered: moderation and permission events are staff-only, and channel events are visible only to people who can see that channel.
- Server notes and user preferences are managed with `remember`, `recall` and `forget`. Deleting removes the record, and the local backup is rewritten. Tokens and credentials are refused.
- Warnings are stored records.

Questions about the past are answered only from stored records. If there's nothing, Relay says so.

### Scheduling

Tasks can run "in 2 minutes", at a local time, or on a repeat (interval, daily, weekly, monthly). Times respect the server timezone, which a user can override with `remember ... key=timezone`. Tasks are saved, claimed atomically, retried with backoff only for temporary failures, and recovered after a restart: safe tasks are queued again, and destructive tasks with an unknown outcome are marked `uncertain` and never re-run. A one-shot task later than `scheduler.late_grace_seconds` is marked `missed`, and a repeating task skips missed slots. At run time the creator's permissions and every target are checked again. Use `/tasks list` and `/tasks cancel`.

### Audit

The audit log is separate from memory and can't be edited by users. It records every request that changes something, each plan, authorization decision, confirmation, step start and finish, scheduler event, memory change, config change, and external change Relay observed. Each entry has fields such as `event_id`, `ts`, `guild_id`, `actor_id`, `action`, `target_id`, `request_id`, `plan_id`, `status`, `reason`, `details` and `duration_ms`. Secrets are redacted, and message contents are not copied wholesale.

For medium and high risk actions the "started" entry is written before the action runs. If it can't be written, the action doesn't run (`audit.fail_closed`, default `true`). Low risk actions are logged too, but a failed write doesn't stop them and is recorded in the application log.

Entries are append-only and hash-chained per server. In Turso mode, database triggers also reject updates and deletes. If `AUDIT_HMAC_KEY` is set, the chain is keyed (HMAC-SHA256), so someone who edits a file without the key can't forge a valid chain. Without it, the chain only catches accidental damage and naive edits. Anyone with control of the machine or the database credentials can still rewrite the records, so this is tamper evidence, not immutability. `/audit verify` checks the chain. `audit.retention_days` prunes old entries and records the pruning. Set a security channel with `/config channel security #channel` to get alerts for failures and high-risk actions.

## Storage

### Local JSON (default)

`"storage": {"mode": "local", "local": {"directory": "data"}}`. State is stored as `data/<collection>/<guild_id>.json` with atomic writes, a mirrored `.bak`, and automatic restore from the backup if corruption is detected. The audit log is `data/audit/<guild_id>/<YYYY-MM>.jsonl`. Run only one process per directory.

### Turso

1. Create a database and token: `turso db create relay`, `turso db show relay --url`, `turso db tokens create relay`.
2. Put the URL and token in `.env`.
3. Set `"storage": {"mode": "turso", "turso": {"enabled": true}}`.

Schema migrations run at startup. Relay never switches backends on its own.

### Backups

- Local: stop Relay and copy the `data/` folder. The `.bak` files are automatic mirrors, not a substitute for backups. Corrupt files are copied aside as `*.corrupt-<timestamp>`; they may contain data you deleted, so remove them once you've looked.
- Turso: `turso db shell relay .dump > backup.sql`, plus Turso's point-in-time restore.

### Moving between JSON and Turso

This is always explicit. Fill in the Turso variables and run:

```bash
python -m relay.storage.migrate --from local --to turso --dry-run
python -m relay.storage.migrate --from local --to turso
```

It keeps IDs and audit hashes, never changes the source, never overwrites differing records (they're reported as conflicts, exit code 1), refuses to import an audit chain that doesn't verify, and refuses to merge into a server that already has audit entries. Then change `storage.mode`. `--from turso --to local` works the same way, and `--guild ID` limits it to one server.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests mock Discord and Groq and never touch a real server. Storage tests run against both backends. The Turso backend is exercised through an in-memory SQLite stand-in with the same client interface, which covers the SQL, triggers and transactions but not network behavior.

For a live check, invite the bot to a throwaway server, point `config.json` at a scratch data directory, and try the examples above, `/groq`, `/storage` and `/audit verify`.

## Running on a server

```bash
git clone <your repo> relay && cd relay
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # then fill it in
```

A systemd unit, `/etc/systemd/system/relay.service`:

```
[Unit]
Description=Relay
After=network-online.target

[Service]
WorkingDirectory=/opt/relay
ExecStart=/opt/relay/.venv/bin/python main.py
Restart=on-failure
User=relay
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now relay`. Logs go to `logs/relay.log` (rotating, secrets scrubbed) and `journalctl -u relay`. On SIGTERM, Relay stops the scheduler and waits for running tasks.

## Troubleshooting

| Problem | Fix |
|---|---|
| macOS says Relay.command can't be opened | Remove the quarantine flag: `xattr -dr com.apple.quarantine "/path/to/Relay"` (see Setup). |
| macOS says "permission denied" | `chmod +x "/path/to/Relay/Relay.command"` |
| Windows says "Windows protected your PC" | Click More info, then Run anyway. Unblock the zip before extracting to avoid it. |
| The launcher says Python is too old or missing | Install Python 3.11 or newer and open the launcher again. |
| The app window doesn't open on Windows | Install the Microsoft WebView2 runtime, or run `python dashboard.py --browser`. |
| Exit code 2 at start | A config or env problem. The message says which. |
| Bot ignores mentions | Message Content Intent isn't enabled in the Developer Portal. |
| Can't find a member by name | Server Members Intent isn't enabled, or the name is ambiguous (Relay will ask). |
| "I don't have the X permission" | Give the bot's role that permission, or move the role higher. |
| "Your roles aren't allowed to use ..." | Map your role to a group with `/config role`. |
| Slash commands are missing | Invite with `applications.commands`. The first sync can take a few minutes. |
| "The configured model isn't available" | Update `groq.model` and check `/groq`. |
| Message search returns nothing | Message Content Intent is missing. |
| Turso errors at start | Check the URL and token with `turso db show`. |
| An error naming a `.corrupt-` file | Relay refused to start from damaged data and kept a copy. Restore from a backup. |

## Known limitations

- Discord and Groq are mocked in the tests and Turso is tested through an SQLite stand-in, so try a first deployment on a scratch server.
- Local JSON mode supports one process. Turso mode also serializes audit writes through one process.
- Bots can only read recent history through the API, so searches cover the latest `limits.max_history_fetch` messages in one channel. Changes made before Relay joined, or while it was offline, are unknown, and attributing a change to a person needs View Audit Log.
- A server setup can't be undone with one command. The audit log lists everything it created.
- Undo covers permission overwrites, channel edits, role edits, role assignments and nicknames. It can't restore deleted channels or roles, or bans (unban is a separate action).
- Scheduled actions are confirmed when created, not again when they run, but they are re-authorized and re-validated at run time.
- Discord caps timeouts at 28 days, and moderation DMs fail silently when a user has DMs closed.
- Relay can't act above its own role, on the server owner, or in channels it can't see. Those fail with an explanation.
- History questions use structured queries by time, kind, member and channel. There is no semantic search.
- How well requests are understood depends on the Groq model. Everything it proposes is validated, but it can misread an ambiguous request, which is why consequential plans are previewed first.
