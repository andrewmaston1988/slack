#!/usr/bin/env python3
"""
Slack <-> Claude Code bridge (session-resume mode).

Each Slack channel maps to one Claude Code session. First message starts a
fresh session; subsequent messages in the same channel resume it via --resume.

DMs: all messages trigger Claude.
Channels: all messages trigger Claude (bot must be a member; no @mention needed).
  On new channel sessions, recent history (+ thread replies) is fetched and
  injected as context. Requires channels:history scope.

Required env vars:
  SLACK_BOT_TOKEN   xoxb-... (Bot User OAuth Token)
  SLACK_APP_TOKEN   xapp-... (App-Level Token, Socket Mode)
  CLAUDE_SLACK_CWD  Working directory for claude subprocess

Optional env vars:
  CLAUDE_SLACK_CHANNEL  If set, only respond in this channel ID (e.g. C01234567)
  CLAUDE_SLACK_HISTORY  Number of recent messages to inject as context (default: 40)
  CLAUDE_SLACK_ADD_DIR  Additional directory to pass via --add-dir to claude CLI
  SLACK_USER_TOKEN      xoxp-... (User OAuth Token) — sets Claude status in Slack
  SLACK_ADMIN_USER      Slack user ID allowed to run /ps-admin commands
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


# Signal to Claude that it is running inside a Slack bridge session.
# Inherited by all subprocesses (claude CLI + hook commands) via os.environ.copy().
os.environ["CLAUDE_VIA_SLACK"] = "1"

# Set at startup so the first message's hook can inject a restart note into context.
# Cleared in handle()'s finally block after the first message completes.
import datetime as _dt
os.environ["CLAUDE_BRIDGE_RESTARTED"] = _dt.datetime.now().strftime("%H:%M:%S")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# slack_sessions.json is stored alongside this script — maps channel IDs to
# Claude session IDs so sessions survive bridge restarts.
SESSIONS_FILE = Path(__file__).resolve().parent / "slack_sessions.json"
ONLY_CHANNEL = os.getenv("CLAUDE_SLACK_CHANNEL", "")
USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")

# Active Claude subprocesses keyed by channel — used by /stop to terminate them.
_active_procs: dict[str, subprocess.Popen] = {}
_stop_requested: set[str] = set()


class _StopRequested(Exception):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session store  (channel -> claude session_id)
# ---------------------------------------------------------------------------

def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_sessions(sessions: dict) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Title derivation
# ---------------------------------------------------------------------------

def _derive_title(text: str, max_len: int = 60) -> str:
    """Derive a short session title from the first user message."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "…"

# ---------------------------------------------------------------------------
# Heartbeat — tool-aware haiku placeholder while Claude works
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL = 5        # seconds between Slack placeholder updates
_HEARTBEAT_COLORS = [
    "#E8A000",  # amber
    "#0078D4",  # blue
    "#7B5EA7",  # purple
    "#00A86B",  # jade
    "#D44000",  # burnt orange
    "#E63946",  # crimson
    "#F72585",  # hot pink
    "#B5179E",  # magenta
    "#3A0CA3",  # indigo
    "#4361EE",  # royal blue
    "#4CC9F0",  # sky
    "#06D6A0",  # mint
    "#8AC926",  # lime
    "#FFD60A",  # yellow
    "#FF6B35",  # tangerine
    "#FF006E",  # rose
    "#6A4C93",  # grape
    "#1982C4",  # cerulean
    "#00B4D8",  # cyan
    "#90BE6D",  # sage
]

# Fallback verbs shown while the initial Haiku fetch is in-flight
_FALLBACK_VERBS = [
    "Cerebrating", "Ratiocinating", "Excogitating", "Deliberating", "Cogitating",
    "Rationalizing", "Theorizing", "Synthesizing", "Evaluating", "Ruminating",
    "Contemplating", "Pondering", "Introspecting", "Lucubrating", "Meditating",
    "Mulling", "Brooding", "Woolgathering", "Musing", "Ideating",
    "Daydreaming", "Envisioning", "Imagineering", "Mooning", "Reverieing",
    "Wondering", "Bethinking", "Perpending", "Apprehending", "Cudgeling",
    "Sprouting", "Forming",
]

from md_to_slack import md_to_slack as _md_to_slack, md_to_blocks as _md_to_blocks


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _fetch_verb_for_tool(tool_name: Optional[str], tool_input: dict, verb_pool: dict) -> None:
    """
    Background thread: ask Haiku for one action-relevant verb for the current tool + input.
    Updates verb_pool["current"] in-place when the response arrives.
    """
    action = tool_name or "thinking"
    # Extract the most meaningful single value from the input args
    context = ""
    if tool_input:
        for key in ("file_path", "command", "pattern", "path", "prompt", "description", "query"):
            val = tool_input.get(key)
            if val and isinstance(val, str):
                # Trim long values to keep the prompt short
                context = f" on {val[:80]}" if len(val) <= 80 else f" on ...{val[-60:]}"
                break
    prompt = (
        f"Give one single-word present-participle verb (ending in -ing) describing an AI assistant "
        f"currently using the '{action}' tool{context}. "
        "Prefer surreal, whimsical, or quirky words — evocative of transformation, discovery, or momentum. "
        "Be playful and unexpected. Avoid generic words like Processing, Computing, Analyzing, Running. "
        "Return only the single word, nothing else."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt,
             "--model", "claude-haiku-4-5",
             "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", timeout=45,
        )
        if result.returncode == 0:
            # Skip any non-word lines (e.g. hook JSON output) — find first purely alpha token
            word = next(
                (ln.strip().strip(".,;:\"'").capitalize()
                 for ln in result.stdout.splitlines()
                 if ln.strip().isalpha()),
                None,
            )
            if word:
                verb_pool["current"] = word
                log.info("verb for %s: %s", tool_name, word)
            else:
                log.info("verb fetch returned no alpha word for %s (stdout=%r)", tool_name, result.stdout[:200])
        else:
            log.info("verb fetch non-zero exit %s for %s: %r", result.returncode, tool_name, result.stderr[:100])
    except Exception as exc:
        log.info("verb fetch failed for %s: %s", tool_name, exc)



def _heartbeat_loop(
    poster,
    channel: str,
    ts: str,
    stop_event: threading.Event,
    start_time: float,
    tool_state: dict,
    verb_pool: dict,
    user_text: str = "",
) -> None:
    """
    Update the Slack placeholder while Claude works.
    On each tool change, fires a fresh Haiku fetch for a new verb.
    The verb is held steady until the tool changes.
    """
    last_tool = object()  # sentinel — guaranteed != any real value on first tick
    color_idx = 0

    while not stop_event.wait(_HEARTBEAT_INTERVAL):
        elapsed = _fmt_elapsed(int(time.time() - start_time))

        # Detect tool change and kick off a new fetch
        current_tool = tool_state.get("name")
        if current_tool != last_tool:
            last_tool = current_tool
            threading.Thread(
                target=_fetch_verb_for_tool,
                args=(current_tool, tool_state.get("input", {}), verb_pool),
                daemon=True,
            ).start()

        verb = verb_pool["current"]
        footer = f"_{verb}_ _({elapsed})_"
        color_idx = (color_idx + 1) % len(_HEARTBEAT_COLORS)
        color = _HEARTBEAT_COLORS[color_idx]

        parts = [p for p in [user_text, footer] if p]
        attachment_text = "\n\n".join(parts)

        # If Claude has already written text, show it in the message body
        # with the verb/elapsed as a small footer strip.
        streamed_text = tool_state.get("text", "")
        if streamed_text:
            streamed_text = _md_to_slack(streamed_text)

        try:
            if streamed_text:
                poster.chat_update(
                    channel=channel,
                    ts=ts,
                    text=streamed_text,
                    attachments=[{
                        "color": color,
                        "text": attachment_text,
                        "mrkdwn_in": ["text"],
                    }],
                )
            else:
                poster.chat_update(
                    channel=channel,
                    ts=ts,
                    text="",
                    attachments=[{
                        "color": color,
                        "text": attachment_text,
                        "mrkdwn_in": ["text"],
                    }],
                )
        except Exception as exc:
            log.debug("heartbeat update failed: %s", exc)

# ---------------------------------------------------------------------------
# Claude subprocess
# ---------------------------------------------------------------------------

def run_claude(
    message: str,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    tool_state: Optional[dict] = None,
    channel: str = "",
) -> tuple:
    """
    Run `claude -p <message> --output-format stream-json [--resume <session_id>]`.
    Streams output line-by-line; returns (response_text: str, session_id: str).
    Updates tool_state["name"] on each tool_use event for the heartbeat.
    No hard timeout — the heartbeat loop signals liveness to the user.
    """
    slack_instruction = (
        "SLACK BRIDGE ACTIVE: (1) Every runnable command must be in a standalone fenced code block "
        "in its own separate message — no explanation text in the same message. (2) Run commands yourself "
        "using tools rather than presenting them to the user; only send a command to the user when it "
        "requires elevation (admin/SYSTEM privileges) or you genuinely cannot execute it.\n\n"
    )
    enhanced_message = slack_instruction + message

    add_dir = os.getenv("CLAUDE_SLACK_ADD_DIR", "")
    add_dir_args = ["--add-dir", add_dir] if add_dir else []

    base_args = ["claude"] + add_dir_args + ["--permission-mode", "bypassPermissions", "-p", enhanced_message, "--output-format", "stream-json"]
    if session_id:
        cmd = ["claude", "--resume", session_id] + add_dir_args + ["--permission-mode", "bypassPermissions", "-p", enhanced_message, "--output-format", "stream-json"]
    else:
        cmd = base_args

    working_dir = cwd
    log.info("claude cmd: %s  cwd=%s", " ".join(cmd[:4]) + "...", working_dir)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=working_dir,
        env=os.environ.copy(),
    )

    if channel:
        _active_procs[channel] = proc

    result_text = ""
    accumulated_text = ""
    new_sid = session_id or ""

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                etype = event.get("type")
                if etype == "result":
                    result_text = event.get("result") or ""
                    new_sid = event.get("session_id") or new_sid
                elif etype == "assistant":
                    for item in event.get("message", {}).get("content", []):
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_use" and tool_state is not None:
                            tool_state["name"] = item.get("name")
                            tool_state["input"] = item.get("input") or {}
                        elif item.get("type") == "text":
                            chunk = item.get("text", "").strip()
                            if chunk:
                                accumulated_text = (accumulated_text + "\n\n" + chunk).strip()
                                if tool_state is not None:
                                    tool_state["text"] = accumulated_text
            except json.JSONDecodeError:
                log.debug("stream-json non-JSON line: %r", line[:120])
    except OSError:
        pass  # pipe broken by /stop
    finally:
        if channel:
            _active_procs.pop(channel, None)
        proc.stdout.close()

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("claude process did not exit after stream ended")

    if proc.returncode != 0:
        if channel in _stop_requested:
            _stop_requested.discard(channel)
            raise _StopRequested()
        stderr = proc.stderr.read(500).strip()
        raise RuntimeError(f"claude exited {proc.returncode}: {stderr}")

    # result_text from the `result` event may only contain the final text chunk
    # (text after the last tool call). Use accumulated_text when it's longer.
    if len(accumulated_text) > len(result_text):
        result_text = accumulated_text

    return result_text, new_sid

# ---------------------------------------------------------------------------
# Channel history — injected as context on new channel sessions
# ---------------------------------------------------------------------------

HISTORY_LIMIT = int(os.getenv("CLAUDE_SLACK_HISTORY", "40"))


def _fetch_channel_history(client, channel: str) -> str:
    """
    Fetch recent channel history (top-level messages + thread replies) and
    return a formatted transcript string to prepend to the first prompt.
    Returns "" on any error or if the channel has no history.
    """
    if not HISTORY_LIMIT:
        return ""
    try:
        resp = client.conversations_history(channel=channel, limit=HISTORY_LIMIT)
        messages = list(reversed(resp.get("messages", [])))  # oldest first
    except Exception as exc:
        log.warning("conversations_history failed: %s", exc)
        return ""

    if not messages:
        return ""

    lines: list[str] = []
    for msg in messages:
        subtype = msg.get("subtype", "")
        if subtype in ("channel_join", "channel_leave", "channel_purpose", "channel_topic"):
            continue
        ts = msg.get("ts", "")
        user = msg.get("username") or msg.get("user") or msg.get("bot_id", "unknown")
        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f"[{user}]: {text}")

        # Hydrate thread replies
        if msg.get("reply_count", 0) > 0:
            try:
                tresp = client.conversations_replies(channel=channel, ts=ts)
                replies = tresp.get("messages", [])[1:]  # skip parent (already added)
                for reply in replies:
                    ru = reply.get("username") or reply.get("user") or reply.get("bot_id", "unknown")
                    rt = (reply.get("text") or "").strip()
                    if rt:
                        lines.append(f"  [thread · {ru}]: {rt}")
            except Exception as exc:
                log.debug("conversations_replies failed for ts=%s: %s", ts, exc)

    if not lines:
        return ""

    transcript = "\n".join(lines)
    return (
        f"<channel_history>\n"
        f"The following is the recent history of this Slack channel "
        f"(oldest first, up to {HISTORY_LIMIT} messages). "
        f"Use it as background context for the conversation.\n\n"
        f"{transcript}\n"
        f"</channel_history>\n\n"
    )


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

def handle(event: dict, client) -> None:
    poster = client
    channel = event.get("channel", "")

    # Channel filter
    if ONLY_CHANNEL and channel != ONLY_CHANNEL:
        return

    # Skip bot messages and edited/deleted subtypes
    if event.get("bot_id") or event.get("subtype"):
        return

    raw_text = event.get("text", "").strip()
    if not raw_text:
        return

    # Strip @-mention tokens (e.g. <@U0123ABC>) — present in app_mention events
    text = re.sub(r"<@\w+>", "", raw_text).strip()
    if not text:
        return

    is_dm = event.get("channel_type") == "im"

    # Session key: always the bare channel ID — whole channel = one session,
    # no threading. Same behaviour for DMs and channel messages.
    session_key = channel
    reply_thread_ts = None

    sessions = load_sessions()
    session_id = sessions.get(session_key)
    is_new_session = session_id is None

    log.info("session_key=%s  existing_session=%s", session_key, session_id or "(new)")

    # /new command — clear saved session so next message starts fresh
    if text.lower() in ("/new", "/reset"):
        sessions.pop(session_key, None)
        save_sessions(sessions)
        poster.chat_postMessage(channel=channel, text="", attachments=[{"color": random.choice(_HEARTBEAT_COLORS), "text": "_Session cleared. Start a new message to begin fresh._", "mrkdwn_in": ["text"]}])
        return

    # /restart command — exit cleanly so the tray launcher restarts the bridge.
    # Post message first, then exit on a daemon thread so the caller (slash
    # command handler) can ack() before the process terminates.
    if text.lower() in ("/restart", "restart bridge"):
        poster.chat_postMessage(channel=channel, text="", attachments=[{"color": random.choice(_HEARTBEAT_COLORS), "text": "_Restarting bridge..._", "mrkdwn_in": ["text"]}])
        log.info("Restart requested via Slack — exiting")
        def _delayed_exit():
            time.sleep(0.5)
            import os as _os; _os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()
        return

    # Post initial placeholder with first fallback verb
    start_time = time.time()
    initial_verb = random.choice(_FALLBACK_VERBS)
    verb_pool: dict = {"current": initial_verb}
    tool_state: dict = {"name": None, "input": {}}

    cmd_echo = f"_`{_derive_title(text, max_len=120)}`_" if text.startswith("/") else ""
    placeholder = poster.chat_postMessage(
        channel=channel,
        **({"thread_ts": reply_thread_ts} if reply_thread_ts else {}),
        text="",
        attachments=[{
            "color": _HEARTBEAT_COLORS[0],
            "text": f"{cmd_echo}\n\n_{initial_verb}_ _(0s)_",
            "mrkdwn_in": ["text"],
        }],
    )
    ph_ts = placeholder["ts"]

    # Start heartbeat — updates placeholder every 5 s while Claude works
    stop_hb = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(poster, channel, ph_ts, stop_hb, start_time, tool_state, verb_pool, cmd_echo),
        daemon=True,
    )
    hb_thread.start()

    try:
        cwd = os.getenv("CLAUDE_SLACK_CWD")
        if not cwd:
            raise RuntimeError("CLAUDE_SLACK_CWD env var is required but not set")

        # On new channel sessions, prepend recent history as context
        prompt = text
        if is_new_session and not is_dm:
            history = _fetch_channel_history(client, channel)
            if history:
                prompt = history + text
                log.info("Injected channel history (%d chars) into first prompt", len(history))

        response_text, new_sid = run_claude(prompt, session_id, cwd, tool_state=tool_state, channel=channel)

        # Persist the session ID for future turns
        if new_sid:
            sessions[session_key] = new_sid
            save_sessions(sessions)
            log.info("Saved session %s for key %s", new_sid, session_key)

        if not response_text:
            response_text = "_(no response)_"

        # Prepend a bold title to the first response so sessions are labelled
        if is_new_session:
            title = _derive_title(text)
            response_text = f"**{title}**\n\n{response_text}"  # md bold — converted by md_to_slack/md_to_blocks

        # Stop heartbeat before writing final response — prevents race where
        # a final heartbeat tick overwrites the response with the verb display
        stop_hb.set()
        hb_thread.join(timeout=3)

        # Use Block Kit blocks when the response contains tables; plain mrkdwn otherwise
        blocks = _md_to_blocks(response_text)
        if blocks:
            # Slack allows max 50 blocks; trim with a warning appended if exceeded
            if len(blocks) > 50:
                blocks = blocks[:49]
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_…response truncated (50 block limit)_"}})
            poster.chat_update(
                channel=channel,
                ts=ph_ts,
                text="(see above)",
                blocks=blocks,
            )
        else:
            # Convert markdown to Slack mrkdwn
            response_text = _md_to_slack(response_text)

            # Slack block text limit is 3 000 chars; split if needed
            if len(response_text) <= 3000:
                poster.chat_update(
                    channel=channel,
                    ts=ph_ts,
                    text=response_text,
                )
            else:
                poster.chat_delete(channel=channel, ts=ph_ts)
                chunks = [response_text[i : i + 3000] for i in range(0, len(response_text), 3000)]
                for i, chunk in enumerate(chunks):
                    poster.chat_postMessage(
                        channel=channel,
                        **({"thread_ts": reply_thread_ts} if reply_thread_ts else {}),
                        text=chunk,
                    )

    except _StopRequested:
        log.info("Claude stopped by /stop for channel %s", channel)
        stop_hb.set()
        hb_thread.join(timeout=3)
        poster.chat_update(
            channel=channel,
            ts=ph_ts,
            text="_Stopped._",
            attachments=[],
        )
    except Exception as exc:
        log.error("handle error: %s", exc, exc_info=True)
        stop_hb.set()
        hb_thread.join(timeout=3)
        poster.chat_update(
            channel=channel,
            ts=ph_ts,
            text=f"_Error: {exc}_",
            attachments=[],
        )
    finally:
        stop_hb.set()  # idempotent safety net
        os.environ.pop("CLAUDE_BRIDGE_RESTARTED", None)  # one-shot restart signal

# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# ---------------------------------------------------------------------------
# Per-channel message queue — ensures messages for the same channel are
# processed sequentially even if the user sends several in quick succession.
# ---------------------------------------------------------------------------

import queue as _queue

_channel_queues: dict[str, _queue.Queue] = {}
_channel_workers: dict[str, threading.Thread] = {}
_channel_lock = threading.Lock()


def _ensure_worker(channel: str) -> _queue.Queue:
    """Return the queue for channel, starting a worker thread if needed."""
    with _channel_lock:
        q = _channel_queues.setdefault(channel, _queue.Queue())
        worker = _channel_workers.get(channel)
        if worker is None or not worker.is_alive():
            def _worker(ch=channel, wq=q):
                while True:
                    try:
                        event, client = wq.get(timeout=60)
                    except _queue.Empty:
                        # Idle for 60 s with nothing queued — retire the thread.
                        with _channel_lock:
                            if wq.empty():
                                _channel_workers.pop(ch, None)
                                break
                        continue
                    try:
                        # Drain any messages that arrived while we were busy —
                        # batch them into a single Claude invocation so the agent
                        # sees all pending messages at once.
                        texts = [event.get("text", "")]
                        while not wq.empty():
                            try:
                                extra_event, _ = wq.get_nowait()
                                wq.task_done()
                                if extra_event.get("text"):
                                    texts.append(extra_event["text"])
                            except _queue.Empty:
                                break
                        if len(texts) > 1:
                            event = dict(event)
                            event["text"] = "\n\n".join(t for t in texts if t)
                        handle(event, client)
                    finally:
                        wq.task_done()
            t = threading.Thread(target=_worker, daemon=True, name=f"worker-{channel}")
            t.start()
            _channel_workers[channel] = t
        return q


def enqueue(event: dict, client) -> None:
    """Queue a message event for sequential processing on its channel."""
    channel = event.get("channel", "")
    _ensure_worker(channel).put((event, client))


@app.event("message")
def on_message(message, client):
    """Respond to all messages in DMs and channels the bot is a member of."""
    enqueue(message, client)


@app.command(re.compile(r".*"))
def on_slash_command(ack, command, client):
    """
    Route all registered slash commands through the bridge.

    Bridge-level commands (/new, /reset, /restart) are handled inline.
    Everything else is passed through to Claude as the message text, so
    /merge foo, /pipeline review, etc. work exactly as in Claude Code.

    Each command must be registered in the Slack app dashboard (Slash Commands
    section). Socket Mode: no request URL needed.
    """
    ack()  # must respond within 3 s; placeholder message handles user feedback

    cmd = command.get("command", "")        # e.g. "/merge"
    args = command.get("text", "").strip()  # e.g. "my-branch"
    text = f"{cmd} {args}".strip() if args else cmd

    # /stop bypasses the per-channel queue — it must run immediately on the
    # socket thread so it can terminate an in-flight Claude subprocess.
    if cmd == "/stop":
        chan = command["channel_id"]
        proc = _active_procs.get(chan)
        if proc:
            _stop_requested.add(chan)
            proc.terminate()
            client.chat_postMessage(channel=chan, text="", attachments=[{
                "color": random.choice(_HEARTBEAT_COLORS),
                "text": "_Stop requested_",
                "mrkdwn_in": ["text"],
            }])
        else:
            client.chat_postMessage(channel=chan, text="", attachments=[{
                "color": _HEARTBEAT_COLORS[0],
                "text": "_Nothing is running._",
                "mrkdwn_in": ["text"],
            }])
        return

    fake_event = {
        "channel": command["channel_id"],
        "channel_type": "im" if command["channel_id"].startswith("D") else "channel",
        "user": command["user_id"],
        "text": text,
        "ts": str(time.time()),
    }
    enqueue(fake_event, client)


# ---------------------------------------------------------------------------
# Presence via user token (optional)
# ---------------------------------------------------------------------------

def _set_status(emoji: str, text: str) -> None:
    user_token = os.getenv("SLACK_USER_TOKEN", "")
    if not user_token:
        return
    try:
        requests.post(
            "https://slack.com/api/users.profile.set",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"profile": {"status_emoji": emoji, "status_text": text, "status_expiration": 0}},
            timeout=10,
        )
    except Exception as exc:
        log.warning("Failed to set Slack status: %s", exc)


def _clear_status() -> None:
    _set_status("", "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _post_startup_notification(client) -> None:
    """Post a restart notification to the most recent DM session, if any."""
    sessions = load_sessions()
    # DM channel IDs start with "D"; channel/group IDs start with "C"/"G"
    dm_channels = [k for k in sessions if k.startswith("D")]
    if not dm_channels:
        return
    channel = dm_channels[-1]
    try:
        client.chat_postMessage(channel=channel, text="🔄 *Bridge restarted*")
        log.info("Startup notification posted to %s", channel)
    except Exception as exc:
        log.warning("Could not post startup notification: %s", exc)


if __name__ == "__main__":
    log.info("Slack bridge starting (Socket Mode) — cwd: %s", os.getenv("CLAUDE_SLACK_CWD", "(not set — will error on first message)"))

    # Windows: register a console control handler so Ctrl+C clears status
    # before the process is terminated (os._exit bypasses finally/atexit)
    _HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)

    def _ctrl_handler(ctrl_type):
        _clear_status()
        return False  # let the default handler terminate the process

    _ctrl_fn = _HandlerRoutine(_ctrl_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_fn, True)

    _set_status(":large_green_circle:", "Claude online")

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    try:
        _post_startup_notification(app.client)
        handler.start()
    except KeyboardInterrupt:
        pass
    finally:
        _clear_status()
        log.info("Slack bridge stopped — status cleared")
