# Telegram auto-reply userbot

A personal Telegram assistant that answers your private messages on your behalf while you are away. It runs on **your own human account** (a userbot — not a bot account), watches whether you are really online, and replies with a short "I'm not here right now" message only when you actually are away. Everything that happens is reported to a **control bot** you create with @BotFather: that private chat is your control panel and your notification feed, with inline buttons to ignore a person, answer them yourself, or mark their message as read.

**🇮🇷 فارسی: [readme_fa.md](readme_fa.md)**

**Features**

- Auto-replies from your own account, so people see a normal reply from you, not from a bot.
- Presence-aware: a second account (the *watcher*) reads your online status, so nothing is answered while you are at your keyboard.
- Optional "was online recently" modes — the default goes by presence, and it falls back to a PC-idle heuristic if presence is unavailable.
- Flood protection: someone who sends more than N messages inside a time window gets a spam warning instead of a polite reply — and that check runs before every other rule, so a flood is answered even while you are online.
- Per-person cooldown, hourly reply cap, ignore list, whitelist, and an allowed-groups list — all live-editable, no restart.
- Control bot as the control panel: status, on/off, ignore list, allowed groups, reply texts, cooldown/spam settings, report mode, daily report, and a list of recent errors.
- Reports only ever go to your private chat with the control bot; no message content is written to any log file.
- Data (database, logs, pause flag) lives in `DATA_DIR`, so a container can keep its state on a mounted volume.

**What it never does:** open a port, accept inbound connections, run a web server, or prompt you for anything at runtime. The session comes from an environment variable, so `python auto_reply.py` starts and stays running without a single keystroke.

## How it works

One process, two Telegram clients:

| Client | Account | Role |
| --- | --- | --- |
| **Userbot** | your human account (`SESSION_STRING`) | receives your private messages, sends the auto-reply as you, reads the allowed groups |
| **Control bot** | a bot you create with @BotFather (`BOT_TOKEN`) | sends every notification to you, and hosts the control panel — it answers `OWNER_ID` only |
| *Watcher* (optional) | a second human account (`WATCHER_SESSION_STRING`) | only reads your online status; an account cannot see its own |

Everything is written under `DATA_DIR`:

| File | Contents |
| --- | --- |
| `state.db` | SQLite: settings, per-person counters, ignore list, allowed groups, error memory |
| `auto_reply.log` | timestamped log lines (counts, error codes, masked ids — never message text) |
| `offline.jsonl` | journal of the away/online transitions |
| `pause.flag` | while this file exists, nothing is answered |

`config.json` next to the code holds the **default** settings and reply texts; on first run they are imported into `state.db`, which then becomes the source of truth. Changing a setting in the panel affects behaviour immediately, with no restart.

## Requirements

- **Python 3.10+** (the Docker image uses 3.12).
- A Telegram account (the one that will auto-reply) and its `api_id` / `api_hash`.
- A bot created with **@BotFather** (free, takes a minute).
- Dependencies: `pip install -r requirements.txt` (telethon, python-dotenv, python-socks, cryptg).

The tools and games need a few more packages, all listed in the same file: `qrcode` + `Pillow` (`/qr`), `opencv-python-headless` (`/qrscan`), `matplotlib` + `numpy` (`/plot`), `sympy` (`/solve`) and `tzdata` (`/time`). They are **optional**: install the file and you get every command, but if a package is missing the bot still starts and auto-replies — only the one command that needs it answers with a short "not installed" message.

## Local setup

**1. Get `API_ID` and `API_HASH`** — open <https://my.telegram.org>, log in with the phone number of the account that will auto-reply, go to *API development tools*, create an application if you have none, and copy the two values.

**2. Get your `SESSION_STRING`** — this is the login of the human account, in one line. Run the helper if your checkout includes it:

```bash
python generate_session.py
```

Otherwise paste this into a temporary file (`gen_session.py`) and run it once:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("api_id: "))
api_hash = input("api_hash: ")
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nSESSION_STRING:\n" + client.session.save() + "\n")
```

It asks for your phone number and the login code Telegram sends you, then prints the string **once**. Treat it exactly like your password — anybody who has it is logged in as you. Do not commit the file you ran; `.gitignore` already excludes `*.session` and `.env`.

**3. Get `BOT_TOKEN`** — talk to **@BotFather** in Telegram: `/newbot`, pick a name and a username, and copy the token it prints. This bot is your control panel; it does not need to be added to any group.

**4. Get your `OWNER_ID`** — your numeric user id. Send any message to **@userinfobot** and copy the number it replies with. This is the *only* account the control bot answers.

**5. Open your bot** — press **/start** in the private chat with your new bot, otherwise Telegram will not let it send you anything yet.

**6. Configure and run**

```bash
cp .env.example .env      # then fill in the five required values
pip install -r requirements.txt
python auto_reply.py
```

At startup the process prints a health line, for example:

```
2026-01-01 12:00:00  === auto-reply bot v2 starting ===
2026-01-01 12:00:01  owner: id=123456789 @yourname name='You'
2026-01-01 12:00:02  notify: control bot ready (owner 1234***789)
2026-01-01 12:00:02  config: enabled=True mode=presence delay=5s cooldown=10m spam>3/30m report=offline_only daily=23:00 control_bot=ok
```

If the control bot cannot reach you, the log says so in one line and the userbot keeps running: open the bot in Telegram and press /start once.

Useful checks that need no Telegram connection: `python auto_reply.py --env-check`, `--db-check`, `--commands-check`, `--tools-check` (`--tools-check --net` also hits the live weather/Wikipedia endpoints), `--deploy-check` (the Dockerfile and `.dockerignore` rules Railway validates before building), `--signal-check` (a real SIGTERM still ends in a clean shutdown, as `docker stop` sends it), `--behavior-check` (the reviewed bugs, tested against a throwaway database), `--test-decisions`, `--daily-preview`. With credentials in place, `--presence-check` shows which accounts connect and whether you currently count as away, and `--self-test` exercises the whole reply pipeline.

## Environment variables

All configuration is read from the environment at runtime. `python-dotenv` also loads a local `.env` if one exists (a real environment variable always wins), which is why a local run needs the file and a server does not.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `API_ID` | yes | — | Numeric API id of the human account, from <https://my.telegram.org> → *API development tools*. `TELEGRAM_API_ID` is accepted as a legacy alias. |
| `API_HASH` | yes | — | API hash from the same page. `TELEGRAM_API_HASH` is accepted as a legacy alias. |
| `SESSION_STRING` | yes | — | Telethon `StringSession` of the account that sends the replies (step 2 above). It *is* a login — keep it secret. |
| `BOT_TOKEN` | yes | — | Token of the control bot, from @BotFather. Panel and notifications only. |
| `OWNER_ID` | yes | — | Your numeric Telegram user id. The only account the control bot answers and the only chat it reports to. `OWNER_IDS`, `ADMIN_ID` and `ADMIN_IDS` are accepted as legacy aliases. |
| `DATA_DIR` | no | `./data` (the Docker image sets `/data`) | Directory for `state.db`, `auto_reply.log`, `offline.jsonl` and `pause.flag`. Point it at a mounted volume. |
| `WATCHER_SESSION_STRING` | no | empty | `StringSession` of a second human account used **only** to read your online status. Empty: the watcher falls back to a local session file if one exists, and presence degrades to the PC-idle heuristic. |
| `REPORT_MODE` | no | `offline_only` | `all` reports every incoming message, `offline_only` reports only while you are away. Also in the panel: *Reports*. |
| `COOLDOWN_MINUTES` | no | `10` | Minutes before the same person may be auto-replied to again. Also `/cooldown <minutes>` and the panel: *Settings*. |
| `TIMEZONE` | no | `Asia/Tehran` | Timezone used for timestamps and the daily-report schedule. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` for the log file. |
| `PROXY` | no | empty | Proxy for a blocked or filtered network: `socks5://user:pass@host:port`, `http://host:port`, or `host:port:secret` for an MTProxy. |

The behaviour values (`report_mode`, `group_report_mode`, `cooldown_minutes`, the spam threshold and window, the reply texts, the daily-report time) live in `state.db`, seeded from `config.json`, and are edited live from the control-bot panel. Setting their environment counterparts is a way to declare the intended defaults for a fresh deployment, not a hard override of what the database already holds.

## Commands

Type them in **Saved Messages** (the chat with yourself) or in the **control-bot chat** — both work, and the bot answers in the same chat. Only `OWNER_ID` is ever answered. `/help` prints the same list in Persian.

The first block is the core bot; the other three come from the optional feature packs (`features.py`, `webtools.py`, `games.py`), each of which is a plain set of `cmd_<name>` methods.

### Bot control
| Command | What it does |
| --- | --- |
| `/on` `/off` | turn the whole bot (replies + reports) on or off |
| `/status` `/ping` | full state, and a health/online check |
| `/set_delay <s>` | seconds offline before an auto-reply goes out (default 5) |
| `/set_cooldown <min>` | minutes before the same person is answered again |
| `/set_spam <count> <min>` | spam rule |
| `/set_report_time <HH:MM>` / `/daily_on` `/daily_off` | daily report |
| `/set_text`, `/set_spam_text`, `/set_group_text <text>` | the three reply texts, `{name}` = the other person |
| `/ignore_add`, `/ignore_remove`, `/ignore_list` | people who are never answered |
| `/group_add`, `/group_remove`, `/group_list` | groups where mentions get the group text |
| `/report_mode <all\|offline_only>`, `/group_mode <away\|mentions\|all>`, `/report_now`, `/errors_clear`, `/reload_config` | reports and maintenance |
| `/help` | the whole command list |

### Profile and messages
| Command | What it does |
| --- | --- |
| `/bioclock <template>` | put a live clock in your bio (`{time}` is replaced), `/biofont`, `/biotemplate` |
| `/autoread on\|off` | mark incoming messages as read |
| `/replymode <all\|offline\|never>` | when to answer |
| `/alerts on\|off` | notify you when someone comes online or goes away |
| `/alwaysonline on\|off` | keep your status online |
| `/font <b\|i\|u\|s\|mono\|caps\|small> <text>` | restyle text, `/tr` converts the keyboard |
| `/prefix <text>` | a prefix for the replies |
| `/quick add\|del\|list` , `/quick <key>` | saved quick replies |
| `/fwd save\|go\|list\|del` | store a message and send it later |
| `/mention <@user>` | mention without typing the name |
| `/watchprof on\|off` | tell me when a contact changes their profile |
| `/copyprof <@user>` | copy someone's name/photo/bio, `/whois`, `/findnum` |

### Tools
| Command | What it does |
| --- | --- |
| `/qr <text>` / `/qrscan` (reply to a photo) | make or read a QR code |
| `/calc <expression>` | calculator (`/calc 2^10 + sqrt(81)`) |
| `/plot <function> [from] [to]` | graph a function, e.g. `/plot sin(x)/x -20 20` |
| `/solve <equation>` | solve it symbolically, e.g. `/solve x^2 - 5x + 6 = 0` |
| `/time <city>` | the time there (`/time شیراز`) |
| `/weather <city>` | current weather, no API key |
| `/wiki <topic>` | a Persian Wikipedia summary |
| `/tools` | the list of everything in here |

### Fun and games
| Command | What it does |
| --- | --- |
| `/loc <city\|lat,lon> [minutes]` | a fake live location for that long |
| `/fun` | ten small time-killers (`/fun 1` … `/fun 10`) |
| `/rps سنگ\|کاغذ\|قیچی` | rock-paper-scissors with a score |
| `/dooz new` · `/dooz <1-9>` | tic-tac-toe against the bot |
| `/word new` · `/word <letter>` | hangman |
| `/hokm start` · `/hokm <خشت\|دل\|گشنیز\|پیك>` | a simple hokm |
| `/play <number>` | play a card in hokm |

These are all plain text — the human account cannot send inline buttons, so every game and menu answers to a typed number or word.

## Deploy on Railway

The repository deploys as-is: `railway.json` selects the `Dockerfile` builder, and there is no build-time secret to supply.

1. **New Project** → **Deploy from GitHub repo**.
2. **Paste the repository URL** (or pick it from your GitHub list) and let Railway import it.
3. Open the service → **Variables** and add the five required variables: `API_ID`, `API_HASH`, `SESSION_STRING`, `BOT_TOKEN`, `OWNER_ID`. Add any optional one you want (`DATA_DIR`, `REPORT_MODE`, `COOLDOWN_MINUTES`, `TIMEZONE`, `LOG_LEVEL`, `PROXY`, `WATCHER_SESSION_STRING`) — never put them in the repository.
4. **Add a Volume** to the service and mount it at the path in `DATA_DIR` — `/data`, which is what the image sets. Without a volume the database is recreated on every redeploy and all counters and settings are lost. The volume is created here in the dashboard, *not* with a `VOLUME` line in the `Dockerfile` — Railway validates the Dockerfile before the build, rejects that instruction with `dockerfile invalid: docker VOLUME at Line N is not supported, use Railway Volumes`, and fails the deployment immediately.
5. **`.dockerignore` is part of the build.** Everything it does not exclude is uploaded to the builder, and the
   `Dockerfile` copies `*.py` from that context, so the local helper scripts, `*.session` files, `state.db` and
   `.env` are kept out of the image by name there. `--deploy-check` fails if a secret becomes reachable that way or
   if a module the bot needs is excluded by mistake.
6. **Deploy** and watch the logs. A healthy start ends with the `config: …` health line. The container is PID 1 and
   handles `SIGTERM` (what `docker stop`, a redeploy and `docker compose down` send), so a restart closes the sqlite
   database and disconnects both clients instead of dropping them mid-write — `--signal-check` proves it offline.

Two notes specific to this app:

- **No inbound port, no domain.** Both clients dial *out* to Telegram; nothing ever listens. If you add a port or a public domain, you have added attack surface for no benefit. The `HEALTHCHECK` in the image therefore checks process liveness plus write access to `DATA_DIR` instead of probing a port.
- **Restarts are automatic.** `restartPolicyType: ON_FAILURE` with 10 retries, and the process is designed never to exit on a Telegram error — a disconnect is logged and retried.
- Railway mounts volumes as `root` while the image runs unprivileged, so set the service variable **`RAILWAY_RUN_UID=0`** if the database cannot be created (the log will show a write error against `/data`).

## The control-bot panel

Open your bot in Telegram and send **/menu**. Every screen is inline buttons, every change applies immediately, and ids are shown masked (`1234***789`). The bot ignores every sender who is not in `OWNER_ID` — without replying, so a stranger cannot even tell it exists.

| Screen | What you can do |
| --- | --- |
| **Main menu** | toggle auto-reply on/off, jump to any screen, close the panel |
| **Status** | live overview: enabled, detection mode, cooldown, spam rule, report mode, how loud a group is, daily-report time, number of ignored people and allowed groups, database counters, today's messages |
| **Reply texts** | edit the normal reply, the spam warning and the group text; `{name}` is replaced with the other person's name |
| **Ignore list** | add or remove people (numeric id or `@username`) so they are never answered |
| **Allowed groups** | add or remove groups (numeric id like `-100…`, or `@username` when the bot is a member) where the group text may be used |
| **Settings** | cooldown per person, spam threshold, spam window, detection mode (presence / idle / offline) |
| **Reports** | report mode (`all` / `offline_only`), how loud an allowed group is (`away` = every message there but only while you are away, `mentions` = only a mention of you or a reply to one of your messages, `all` = always), daily-report time, daily report on/off, error reports on/off, startup message on/off |
| **Errors** | the last error lines and a button to clear the list |

Notification buttons, available on every reported message: **🚫 ignore this user**, **✍️ answer as Hermes** (type your text — it is sent as you), **✅ mark as read**.

Text commands, if you prefer typing: `/menu`, `/status`, `/on`, `/off`, `/ignore <id or @username>`, `/unignore <id or @username>`, `/mode all|offline_only`, `/cooldown <minutes>`, `/help`, `/cancel`.

## If something looks wrong

| Symptom | What it means |
| --- | --- |
| `ERROR [missing_credentials] API_ID, API_HASH is required …` and the process exits | Startup found no credentials and says so in one line instead of a traceback. Set `API_ID` and `API_HASH` (and `SESSION_STRING`). A malformed `SESSION_STRING` is reported the same way. |
| `ERROR [invalid_session] …` | The `SESSION_STRING` is corrupt or was built with another `API_ID`. Run `python generate_session.py` again. |
| The log says `entity cache warmed: 0 dialogs` | The session is signed in but Telegram returned no dialogs. Replying to a person who has not written since the restart can then fail with `Could not find the input entity` — send that person a message once, or check that the session is not a brand-new one. |
| The status screen shows a "raw presence" line | The detection mode is forced (`online`/`offline`), your own messages are recent, or the idle fallback decided — the first line is the decision, the raw line is the measurement behind it. |
| Presence says `UserStatusRecently - coarse status …` | The watcher account is not allowed to see your exact last-seen time, so an actually-online owner also reads as *recently* and the bot would answer. Telegram → **Settings → Privacy and Security → Last Seen & Online** → add the watcher account (or *Everybody*). |
| The control bot reports every message in a busy group | `/group_mode mentions` (only a mention of you or a reply to one of your messages) or `away` (only while you are away, which is the default). |
| No last-seen times and no replies at all from bots | Bots are ignored by design (`ignore_bots`): a message from a bot is not counted, not journalled and never notified — the control bot talks to you through this same account. |

## Security

- **Never commit `.env`.** `.gitignore` excludes `.env`, every `*.session` file and `data/`; `.env.example` contains names and comments only. If you fork this repo, check `git status` before your first push.
- **The session string is as powerful as your login.** Anyone holding `SESSION_STRING` (or `WATCHER_SESSION_STRING`) can read and send your messages without a password or code. Keep it out of the repository, out of screenshots, and out of issue reports.
- **If it leaks, revoke it:** Telegram → *Settings* → *Devices* → terminate the session. Then generate a new `SESSION_STRING` and update the variable. The old one stops working everywhere.
- **Keep `BOT_TOKEN` private too** — it is your control panel; a stranger with the token sees the panel interface (though not your chat, which is bound to `OWNER_ID`).
- The code logs counts, error codes and masked ids only. Notification texts carry message content but go to your private chat with the control bot, never to a log file.
- Run the process as a non-root user (the Docker image does) and give the volume only to that user.

## Files

| Path | What it is |
| --- | --- |
| `auto_reply.py` | entry point: `python auto_reply.py` (plus the `--…-check` diagnostics) |
| `bot.py` | wiring: handlers, decision rules, supervision |
| `store.py` | `state.db`: settings, counters, ignore list, groups, error memory |
| `tg.py` | credentials, clients, safe sending, error wording |
| `presence.py` | watcher account, auto-offline timer, PC-idle fallback |
| `notify.py` | notifications to the owner through the control bot |
| `panel.py` | the inline-button control panel |
| `reports.py` | message report, daily report, error report |
| `commands.py` | text commands (the core set + the router that finds pack commands) |
| `features.py` | feature pack: bio clock, fonts, quick replies, profile copy, alerts, shortcuts |
| `webtools.py` | feature pack: `/qr`, `/qrscan`, `/calc`, `/plot`, `/solve`, `/time`, `/weather`, `/wiki` |
| `games.py` | feature pack: fake live location, mini games, tic-tac-toe, hangman, hokm |
| `paths.py` | the single place that resolves `DATA_DIR` and every file inside it |
| `config.json` | default settings and reply texts, imported into the database |
| `Dockerfile`, `Procfile`, `railway.json` | deployment (see `deploy/README-deploy.md`) |
| `deploy/hermes.service` | systemd unit for a plain Linux VPS |
