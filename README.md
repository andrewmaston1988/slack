# Claude Slack Bridge

A two-way chat bridge that connects Slack to [Claude Code](https://claude.ai/code). Send messages to a Slack channel or DM and Claude responds — with session persistence, a live typing indicator, and rich markdown rendering.

## What it does

- **Chat with Claude from Slack** — DM the bot or mention it in a channel
- **Sessions persist** — follow-up messages resume the same Claude session; sessions survive bridge restarts
- **Live indicator** — a colour-cycling status attachment shows what Claude is doing while it thinks, with a whimsical generated verb per tool call
- **Rich formatting** — markdown tables render as structured Block Kit blocks; code blocks pass through verbatim
- **Outbound notifications** — `notify.ps1` lets any script post an alert to Slack

## Requirements

- Windows (the tray launcher uses Windows APIs; the bridge itself is cross-platform)
- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed and authenticated (`claude` on your PATH)
- A Slack workspace where you can create apps

## Installation

```
pip install -r requirements.txt
```

## Slack app setup

1. Go to https://api.slack.com/apps → **Create New App** → From scratch
2. **Socket Mode** (Features → Socket Mode) → Enable → copy the App Token (`xapp-...`)
3. **OAuth & Permissions** → Bot Token Scopes — add:
   `app_mentions:read`, `chat:write`, `chat:write.public`, `chat:delete`,
   `im:history`, `im:read`, `im:write`, `channels:history`, `channels:read`, `commands`
4. **Event Subscriptions** → Subscribe to bot events: `message.im`, `message.channels`, `app_mention`
5. **Slash Commands** — register `/new`, `/stop`, `/reset`, `/restart` (no Request URL needed with Socket Mode)
6. **Install app to workspace** → copy the Bot Token (`xoxb-...`)

## Configuration

Set these environment variables before starting the bridge:

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-Level Token for Socket Mode (`xapp-...`) |
| `CLAUDE_SLACK_CWD` | Yes | Working directory for the Claude CLI (your project root) |
| `CLAUDE_SLACK_ADD_DIR` | No | Extra directory passed as `--add-dir` to Claude |
| `CLAUDE_SLACK_CHANNEL` | No | Restrict bot to one channel ID |
| `CLAUDE_SLACK_HISTORY` | No | Messages of channel history to inject on new sessions (default: 40) |
| `SLACK_USER_TOKEN` | No | User token (`xoxp-...`) — sets your Slack status to show Claude is online |
| `SLACK_NOTIFY_CHANNEL` | No | Default channel for `notify.ps1` |

## Starting the bridge

**Recommended — Windows system tray:**

```
pythonw slack_bridge_tray.pyw
```

A green/grey dot appears in the notification area. The tray auto-restarts the bridge if it crashes. Right-click for Restart · View Log · Quit.

**Direct (for debugging):**

```
python slack_bridge.py
```

Logs are written to `logs/slack_bridge.log`.

## Usage

- **DM the bot** — every message is sent to Claude
- **In a channel** — invite the bot (`/invite @yourbot`), then every message triggers Claude
- `/new` or `/reset` — start a fresh session in this channel/DM
- `/stop` — interrupt Claude mid-response
- `/restart` — restart the bridge process

## Outbound notifications

Post a message to Slack from any script:

```powershell
# Requires SLACK_BOT_TOKEN and SLACK_NOTIFY_CHANNEL to be set
.\notify.ps1 -Title "Build done" -Message "All tests passed."

# From a file (markdown is converted automatically)
.\notify.ps1 -Title "Report" -MessageFile "C:\tmp\report.md" -Channel "#dev"
```

## Files

| File | Purpose |
|------|---------|
| `slack_bridge.py` | Core bridge — Socket Mode, session management, heartbeat |
| `slack_bridge_tray.pyw` | Windows tray launcher with auto-restart |
| `md_to_slack.py` | Markdown → Slack mrkdwn converter (also a standalone CLI) |
| `notify.ps1` | Post outbound alerts to Slack |
| `requirements.txt` | Python dependencies |
