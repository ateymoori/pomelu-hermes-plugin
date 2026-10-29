# Pomelu plugin for Hermes Agent

Lets your Hermes bots message you in [Pomelu](https://github.com/ateymoori/pomelu),
the mobile app for Hermes, the way they message you on Telegram or Slack.

Without it, a reminder or a scheduled job that you make in Pomelu ("message
me every day at 7") goes to your Telegram home channel, or is only saved on
the server. With it, the message lands in the bot's chat in Pomelu, and
Pomelu shows a notification.

## Install

On the machine that runs Hermes (0.22 or later):

```bash
hermes plugins install ateymoori/pomelu-hermes-plugin --enable
```

Then restart Hermes (`hermes serve`, the gateway service, or the Hermes
desktop app). Nothing else to set up: no account, no keys.

## What it does

- **Scheduled messages come back to the app.** When a bot makes a cron job
  from a Pomelu chat and no target is given (or the target is `origin`), the
  plugin sets `deliver: bot-chat`. A target that you or the bot chose
  (`telegram`, `local`, `all`, ...) stays.
- **The bot knows.** In Pomelu chats the bot is told that a scheduled job is
  the way to message you later, and that `send_message` cannot reach the app.

Jobs made before you installed the plugin keep their target. Ask the bot to
change one: "deliver the 7:00 job to bot-chat".

## Notifications on the phone

- **Android:** keep "Stay connected in the background" on in Pomelu settings
  (the default). The message then rings at once; without it, within about
  15 minutes.
- **iOS:** iOS lets an app check only when it chooses, so the notification
  can come late, or when you open the app.

## Remove

```bash
hermes plugins remove pomelu
```

## Develop

```bash
HERMES_HOME=$(mktemp -d) PYTHONPATH=<hermes checkout> <hermes venv>/bin/python -m pytest tests
```

Apache-2.0. Pomelu is not made by, or affiliated with, Nous Research.
