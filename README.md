# Pomelu plugin for Hermes Agent

Lets your Hermes bots message you in [Pomelu](https://github.com/ateymoori/pomelu),
the mobile app for Hermes, the way they message you on Telegram or Slack:
the phone rings, also while the app is closed.

Without it, a reminder or a scheduled job that you make in Pomelu ("message
me every day at 7") goes to your Telegram home channel, or is only saved on
the server, and the phone learns about a new message only when Pomelu runs.

## Install

On the machine that runs Hermes (0.22 or later):

```bash
hermes plugins install ateymoori/pomelu-hermes-plugin --enable
hermes pomelu setup
```

To update: `hermes plugins update pomelu`, then `hermes pomelu setup`.

`setup` turns the plugin on for every bot (Hermes turns a plugin on per
profile). Then restart Hermes (`hermes serve`, the gateway service, or the
Hermes desktop app). Nothing else to set up: no account, no keys. Pomelu
finds the plugin on its next connection and registers the phone.

## What it does

- **Scheduled messages come back to the app.** When a bot makes a cron job
  from a Pomelu chat and no target is given (or the target is `origin`), the
  plugin sets `deliver: bot-chat`. A target that you or the bot chose
  (`telegram`, `local`, `all`, ...) stays.
- **The bot knows.** In Pomelu chats the bot is told that a scheduled job is
  the way to message you later, and that `send_message` cannot reach the app.
- **The phone rings.** When a bot answers in its Bot Chat and no phone has
  Pomelu open, each registered phone gets a push notification with the
  bot's name and the start of the answer. A tap opens that bot's chat.
- **New bots too.** Each time Pomelu registers, bots made since then get the
  plugin turned on (a bot where you turned it off stays off).

Jobs made before you installed the plugin keep their target. Ask the bot to
change one: "deliver the 7:00 job to bot-chat".

`hermes pomelu status` lists the phones and any bot without the plugin.

## How push works, and what leaves your machine

Apple and Google deliver pushes only for the app's owner, who holds the
app's push keys. So the plugin sends each push through the **Pomelu push
relay**, a small Cloudflare Worker run by Royan AB (source: [`relay/`](relay)).

- Pomelu makes a random key on the phone and gives it to your Hermes with
  the phone's push token. Phones are kept in `~/.hermes/pomelu/devices.json`
  (owner-only file).
- The plugin seals the message with the phone's key (AES-256-GCM) before it
  leaves. The relay, Apple and Google see the push token and sealed bytes,
  never the text. The phone opens it.
- The relay stores nothing and logs no message. It answers "gone" for a
  phone that removed the app; the plugin then forgets that phone.

Set `POMELU_PUSH_URL` to use another relay (one with the same keys).

## Remove

```bash
hermes plugins remove pomelu
```

## Develop

```bash
HERMES_HOME=$(mktemp -d) PYTHONPATH=<hermes checkout> <hermes venv>/bin/python -m pytest tests
cd relay && npm test          # relay unit tests (Node 22+)
npx wrangler deploy           # relay deploy (Royan AB account)
```

Apache-2.0. Pomelu is not made by, or affiliated with, Nous Research.
