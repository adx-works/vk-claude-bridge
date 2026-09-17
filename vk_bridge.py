#!/usr/bin/env python3
"""
vk_bridge.py - a VKontakte bot that is a phone remote for local Claude Code agents.

Modeled on runa-tic's tg-brain-bridge (https://github.com/runa-tic/tg-brain-bridge), adapted
for VK's Bot Long Poll API and extended with two things that repo deliberately doesn't do:

  - Multiple named sessions. Each session is a (working directory, persisted claude
    session id) pair. One VK chat, one command to switch which project you're talking to:
    /sessions lists them, /switch <name> changes the current one. Each keeps its own
    conversation history.
  - Options as tappable buttons. The agent is told (via --append-system-prompt) that it can
    end a reply with a "CHOICES:" block listing short options; the bridge strips that block
    from the displayed text and attaches a VK reply-keyboard built from it. Tapping a button
    just sends its label back as an ordinary text message - no callback/event plumbing needed.

Each incoming VK message is fed to a persistent headless Claude Code conversation running in
the current session's working directory. The reply streams back live: the bot posts a preview
message and edits it in place as text arrives, tool calls shown as small code boxes.

Posture: read + write files, web research, one whitelisted scripts dir. The agent may NOT run
arbitrary Bash - deliberately, since this thing has no human in the loop to approve a prompt.
Point a session's working directory at something you're comfortable an unattended headless
agent editing; do not point one at prod config/secrets.

Setup:
  1. Create a VK community (if you don't have one) and a bot in it: Community -> Manage ->
     API usage -> Bot Long Poll API, turn it on with "Message events" + "Incoming messages".
     Copy the community token from API usage -> Access tokens (Manage -> API usage -> Create
     token, with messages scope).
  2. Put the token, your VK numeric user id, group id, and your session map in
     .secrets/vk_bridge.json (see .secrets/vk_bridge.json.example).
     (Leave allowed_user_id 0 to discover it: message the bot once and it replies with your id.)
  3. Run: python vk_bridge.py   (or under Docker/pm2, see README)

No third-party deps required (faster-whisper only if you want voice notes). Stdlib otherwise.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRETS_DIR = BASE_DIR / ".secrets"
CONFIG_PATH = SECRETS_DIR / "vk_bridge.json"
STATE_PATH = SECRETS_DIR / "vk_bridge.state.json"
MEDIA_DIR = BASE_DIR / "_media"  # gitignored: downloaded photos + voice notes stay local

VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.199"

# What the remote agent is allowed to do without a human to approve prompts.
# "Read + write files": recall, capture, research, run your scripts dir. No free Bash.
# The Bash(python _tools/*) entries are an EXAMPLE of whitelisting one scripts directory
# (relative to the session's cwd) - point at your own automation dir per-project, or drop it.
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write", "TodoWrite",
    "WebSearch", "WebFetch",
    "Bash(python _tools/*)", "Bash(py _tools/*)",
]
PERMISSION_MODE = "acceptEdits"
CLAUDE_TIMEOUT_SEC = 900          # a single turn may take a while
VK_LONGPOLL_WAIT_SEC = 25
VK_MAX_CHARS = 4000               # VK hard cap is 4096; leave headroom
WHISPER_MODEL_SIZE = "small"      # faster-whisper model for voice transcription
MAX_CHOICE_BUTTONS = 8

CHOICES_SYSTEM_PROMPT = (
    "You are being driven through a VK (VKontakte) chat bridge, not the interactive CLI - "
    "there is no terminal UI, so never call an AskUserQuestion-style tool. Instead: when you "
    "want to offer the user a short set of discrete options to pick between (a decision, an "
    "A/B choice, next steps to take), end your final reply with a line reading exactly "
    "'CHOICES:' followed by each option on its own line formatted as '- <short option text>' "
    "(at most 8 options, each a few words). The bridge renders these as tappable buttons. Only "
    "use this for an actual choice you want tapped instead of typed - never for ordinary lists, "
    "steps, or file listings."
)


# --------------------------------------------------------------------------- io
def log(msg: str) -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


_whisper_model = None  # lazily loaded; reused across turns (load is expensive)


def transcribe_audio(path: Path):
    """Transcribe an audio file locally with faster-whisper. Returns (text, error):
    exactly one is non-None, so the caller can tell the user *why* it failed."""
    global _whisper_model
    try:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            log(f"[vk_bridge] loading whisper model '{WHISPER_MODEL_SIZE}' (first use)...")
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        segments, _info = _whisper_model.transcribe(str(path), beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()
        return (text, None) if text else (None, "empty transcript (silent clip?)")
    except ImportError:
        log("[warn] faster-whisper not installed: pip install faster-whisper")
        return (None, "faster-whisper not installed (pip install faster-whisper)")
    except Exception as e:
        log(f"[warn] transcription failed: {e}")
        return (None, f"{type(e).__name__}: {e}")


def find_claude() -> str:
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    fallback = Path.home() / ".local" / "bin" / "claude.EXE"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("Could not find the 'claude' executable on PATH.")


# ------------------------------------------------------------------- choices
def split_choices(text: str) -> tuple[str, list[str]]:
    """Strip a trailing 'CHOICES:\n- a\n- b' block off text; return (body, options)."""
    m = re.search(r"\n?CHOICES:\s*\n((?:\s*-\s*.+\n?)+)\s*$", text, re.IGNORECASE)
    if not m:
        return text, []
    body = text[:m.start()].rstrip()
    options = [ln.strip().lstrip("-").strip() for ln in m.group(1).splitlines() if ln.strip()]
    return body, options[:MAX_CHOICE_BUTTONS]


def build_keyboard(options: list[str]) -> str | None:
    if not options:
        return None
    return json.dumps({
        "one_time": True,
        "inline": False,
        "buttons": [[{"action": {"type": "text", "label": opt[:40]}, "color": "primary"}]
                    for opt in options],
    })


# ------------------------------------------------------------------------- md
def md_to_plain(text: str) -> str:
    """VK bot messages have no rich text formatting - render the agent's markdown down to
    readable plain text instead of leaving raw ** and ``` in the chat."""
    text = re.sub(r"```(?:\w+\n)?([\s\S]*?)```", lambda m: m.group(1).strip("\n"), text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
    return text


# --------------------------------------------------------------------- vk api
class VK:
    def __init__(self, token: str, group_id: int):
        self.token = token
        self.group_id = group_id

    def call(self, method: str, params: dict, timeout: int = 60) -> dict:
        params = {**params, "access_token": self.token, "v": VK_API_VERSION}
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(f"{VK_API}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_longpoll_server(self) -> dict:
        res = self.call("groups.getLongPollServer", {"group_id": self.group_id})
        return res["response"]

    def poll(self, server: str, key: str, ts: str) -> dict:
        params = {"act": "a_check", "key": key, "ts": ts, "wait": VK_LONGPOLL_WAIT_SEC}
        url = f"{server}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=VK_LONGPOLL_WAIT_SEC + 15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log(f"[warn] longpoll failed: {e}")
            time.sleep(3)
            return {}

    def send(self, peer_id: int, text: str, keyboard: str | None = None) -> int | None:
        text = text or "(empty response)"
        last_id = None
        for i in range(0, len(text), VK_MAX_CHARS):
            chunk = text[i:i + VK_MAX_CHARS]
            is_last = i + VK_MAX_CHARS >= len(text)
            params = {"peer_id": peer_id, "message": chunk, "random_id": uuid.uuid4().int & 0x7FFFFFFF,
                      "dont_parse_links": 1}
            if is_last and keyboard:
                params["keyboard"] = keyboard
            elif is_last:
                # Explicitly clear any previous keyboard once the choice has been made.
                params["keyboard"] = json.dumps({"buttons": [], "one_time": True})
            try:
                res = self.call("messages.send", params)
                if "error" in res:
                    raise ValueError(res["error"].get("error_msg", "unknown error"))
                last_id = res.get("response")
            except Exception as e:
                log(f"[warn] messages.send failed: {e}")
        return last_id

    def edit(self, peer_id: int, message_id: int, text: str, keyboard: str | None = None) -> bool:
        try:
            params = {"peer_id": peer_id, "message_id": message_id,
                      "message": text[:VK_MAX_CHARS] or "(empty response)", "dont_parse_links": 1}
            if keyboard:
                params["keyboard"] = keyboard
            res = self.call("messages.edit", params)
            return "error" not in res
        except Exception:
            return False

    def delete(self, peer_id: int, message_id: int) -> None:
        try:
            self.call("messages.delete", {"peer_id": peer_id, "message_ids": message_id,
                                          "delete_for_all": 1})
        except Exception:
            pass

    def download_attachment_url(self, url: str, dest_dir: Path, stem: str, ext: str) -> Path | None:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{stem}{ext}"
            with urllib.request.urlopen(url, timeout=120) as r:
                dest.write_bytes(r.read())
            return dest
        except Exception as e:
            log(f"[warn] attachment download failed: {e}")
            return None


# --------------------------------------------------------------- claude bridge
def run_claude(claude: str, cwd: Path, message: str, session_id: str | None) -> tuple[str, str | None]:
    """Run one headless turn. Returns (reply_text, new_session_id)."""
    cmd = [claude, "-p", "--output-format", "json",
           "--permission-mode", PERMISSION_MODE, "--allowedTools", *ALLOWED_TOOLS,
           "--append-system-prompt", CHOICES_SYSTEM_PROMPT]
    if session_id:
        cmd += ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]

    env = dict(os.environ, PYTHONUTF8="1", CLAUDE_BRAIN_BRIDGE="1")
    try:
        proc = subprocess.run(
            cmd, input=message, cwd=str(cwd),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT_SEC, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ("[bridge] Claude timed out on that turn. Try a smaller ask.", session_id)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        if session_id and ("No conversation" in err or "not found" in err.lower()):
            return ("__RESET__", None)
        return (f"[bridge] claude exited {proc.returncode}: {err}", session_id)

    try:
        out = json.loads(proc.stdout)
        return (out.get("result", "").strip(), out.get("session_id", session_id))
    except json.JSONDecodeError:
        return (proc.stdout.strip()[:VK_MAX_CHARS] or "(no output)", session_id)


def _deliver(vk, peer_id, bubble_id, text: str) -> None:
    """Turn the live preview bubble into the final, plain-text-rendered answer, with any
    trailing CHOICES: block stripped off and rendered as a keyboard instead."""
    body, options = split_choices(md_to_plain(text))
    keyboard = build_keyboard(options)
    if bubble_id is not None and len(body) <= VK_MAX_CHARS and vk.edit(peer_id, bubble_id, body, keyboard):
        return
    if bubble_id is not None:
        vk.delete(peer_id, bubble_id)
    vk.send(peer_id, body, keyboard)


def tool_summary(name, inp):
    """A live, human-readable line for a tool call in the stream view."""
    inp = inp or {}

    def clip(s, limit=500):
        s = s or ""
        return s if len(s) <= limit else s[:limit] + "\n..."

    if name == "Edit":
        return f"Edit {inp.get('file_path', '')}\n{clip(inp.get('new_string', ''))}"
    if name == "Write":
        return f"Write {inp.get('file_path', '')}\n{clip(inp.get('content', ''))}"
    if name == "Read":
        return f"Read {inp.get('file_path', '')}"
    if name == "Bash":
        return f"$ {clip(inp.get('command', ''), 300)}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}"
    if name == "WebSearch":
        return f"WebSearch {inp.get('query', '')}"
    if name == "WebFetch":
        return f"WebFetch {inp.get('url', '')}"
    return f"{name or 'tool'}"


def run_claude_streaming(vk, peer_id, claude, cwd, message, session_id):
    """Stream one turn live to VK by editing a message as text arrives, then finalize.
    Returns (session_id, reset_needed). Falls back to run_claude if the stream fails."""
    cmd = [claude, "-p", "--output-format", "stream-json", "--include-partial-messages",
           "--verbose", "--permission-mode", PERMISSION_MODE, "--allowedTools", *ALLOWED_TOOLS,
           "--append-system-prompt", CHOICES_SYSTEM_PROMPT]
    if session_id:
        cmd += ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]
    env = dict(os.environ, PYTHONUTF8="1", CLAUDE_BRAIN_BRIDGE="1")

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=str(cwd), env=env, bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        vk.send(peer_id, f"[bridge] failed to start claude: {e}")
        return (session_id, False)

    killer = threading.Timer(CLAUDE_TIMEOUT_SEC, proc.kill)
    killer.start()
    try:
        proc.stdin.write(message)
        proc.stdin.close()
    except Exception:
        pass

    segments = []            # ordered [("text", str) | ("tool", str)] shown live
    bubble_id = None
    last_edit = 0.0
    final_result = None
    new_sid = session_id
    seen_tools = set()

    def render():
        parts = []
        for kind, txt in segments:
            parts.append(f"\n[{txt}]\n" if kind == "tool" else txt)
        while len("".join(parts)) > VK_MAX_CHARS and len(parts) > 1:
            parts.pop(0)
        return "".join(parts)[:VK_MAX_CHARS]

    def preview():
        nonlocal bubble_id
        body = render()
        if not body.strip():
            return
        if bubble_id is None:
            bubble_id = vk.send(peer_id, body)
        else:
            vk.edit(peer_id, bubble_id, body)

    def add_text(t):
        if segments and segments[-1][0] == "text":
            segments[-1] = ("text", segments[-1][1] + t)
        else:
            segments.append(("text", t))

    def mark_tool(tool_id, box):
        nonlocal last_edit
        if not tool_id or tool_id in seen_tools:
            return
        seen_tools.add(tool_id)
        segments.append(("tool", box))
        preview()
        last_edit = time.monotonic()

    for raw in iter(proc.stdout.readline, ""):
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system":
            new_sid = ev.get("session_id", new_sid)
        elif kind == "stream_event":
            e = ev.get("event", {})
            et = e.get("type")
            if et == "content_block_delta":
                d = e.get("delta", {})
                if d.get("type") == "text_delta":
                    add_text(d.get("text", ""))
                    now = time.monotonic()
                    if now - last_edit >= 1.2:   # VK edit rate limit is looser than TG's
                        preview()
                        last_edit = now
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content", []):
                if block.get("type") == "tool_use":
                    mark_tool(block.get("id"), tool_summary(block.get("name"), block.get("input")))
        elif kind == "result":
            new_sid = ev.get("session_id", new_sid)
            final_result = ev.get("result")

    killer.cancel()
    proc.wait()
    err = ((proc.stderr.read() if proc.stderr else "") or "").strip()
    streamed_text = "".join(t for k, t in segments if k == "text")
    final = (final_result if final_result is not None else streamed_text).strip()

    if proc.returncode not in (0, None) and not final:
        if "No conversation" in err or "not found" in err.lower():
            if bubble_id is not None:
                vk.delete(peer_id, bubble_id)
            return (None, True)
        log(f"[warn] stream failed (rc={proc.returncode}); falling back. {err[:200]}")
        if bubble_id is not None:
            vk.delete(peer_id, bubble_id)
        reply, sid = run_claude(claude, cwd, message, session_id)
        if reply == "__RESET__":
            return (None, True)
        body, options = split_choices(md_to_plain(reply))
        vk.send(peer_id, body, build_keyboard(options))
        return (sid, False)

    _deliver(vk, peer_id, bubble_id, final or "(no output)")
    return (new_sid, False)


# ------------------------------------------------------------------ sessions
def session_state(state: dict, name: str) -> dict:
    return state.setdefault("sessions", {}).setdefault(name, {"claude_session_id": None})


def current_session_name(state: dict, sessions_cfg: dict) -> str:
    cur = state.get("current_session")
    if cur in sessions_cfg:
        return cur
    default = next(iter(sessions_cfg))
    state["current_session"] = default
    return default


# --------------------------------------------------------------------- driver
def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    token = cfg.get("group_token") or os.environ.get("VK_BRIDGE_GROUP_TOKEN")
    group_id = cfg.get("group_id") or int(os.environ.get("VK_BRIDGE_GROUP_ID", "0") or 0)
    allowed = cfg.get("allowed_user_id") or int(os.environ.get("VK_BRIDGE_ALLOWED_USER_ID", "0") or 0)
    sessions_cfg = cfg.get("sessions") or {}
    if not token or not group_id:
        log(f"[fatal] group_token/group_id missing. Fill in {CONFIG_PATH} "
            f"(see vk_bridge.json.example).")
        return 1
    if not sessions_cfg:
        log(f"[fatal] no 'sessions' configured in {CONFIG_PATH} - need at least one "
            f'name -> working-directory mapping, e.g. {{"napopravku": "/home/alex/code/napopravku"}}.')
        return 1

    claude = find_claude()
    vk = VK(token, group_id)
    state = load_json(STATE_PATH, {"ts": None, "current_session": None, "sessions": {}})

    lp = vk.get_longpoll_server()
    server, key, ts = lp["server"], lp["key"], state.get("ts") or lp["ts"]

    log(f"[vk_bridge] up. group_id={group_id} claude={claude}")
    log(f"[vk_bridge] sessions={list(sessions_cfg)}")
    log(f"[vk_bridge] allowed_user_id={allowed or '(discovery mode)'}")

    while True:
        res = vk.poll(server, key, ts)
        if res.get("failed"):
            # ts too old / key expired: refresh the long poll server entirely.
            lp = vk.get_longpoll_server()
            server, key, ts = lp["server"], lp["key"], lp["ts"]
            continue
        ts = res.get("ts", ts)
        state["ts"] = ts
        save_state(state)

        for upd in res.get("updates", []):
            if upd.get("type") != "message_new":
                continue
            msg = (upd.get("object") or {}).get("message") or {}
            text = (msg.get("text") or "").strip()
            peer_id = msg.get("peer_id")
            from_id = msg.get("from_id")
            attachments = msg.get("attachments") or []
            if peer_id is None or not (text or attachments):
                continue

            if not allowed:
                log(f"[setup] message from user id {from_id}. Put this in vk_bridge.json.")
                vk.send(peer_id, f"Your VK id is {from_id}. Add it as "
                                 f"\"allowed_user_id\" in vk_bridge.json and restart.")
                continue
            if from_id != allowed:
                log(f"[warn] ignored message from non-whitelisted user {from_id}")
                continue

            cur_name = current_session_name(state, sessions_cfg)

            if text in ("/start", "/help"):
                names = ", ".join(sessions_cfg)
                vk.send(peer_id, "Brain online.\n"
                                 f"Current session: {cur_name}\n"
                                 f"Available: {names}\n\n"
                                 "/sessions - list projects\n"
                                 "/switch <name> - change project\n"
                                 "/new - fresh conversation in the current project\n"
                                 "/reload - restart the bridge")
                continue
            if text == "/sessions":
                lines = [f"{'* ' if n == cur_name else '  '}{n} -> {p}"
                         for n, p in sessions_cfg.items()]
                vk.send(peer_id, "\n".join(lines))
                continue
            if text.startswith("/switch"):
                target = text[len("/switch"):].strip()
                if target not in sessions_cfg:
                    vk.send(peer_id, f"Unknown session '{target}'. /sessions to list.")
                    continue
                state["current_session"] = target
                save_state(state)
                vk.send(peer_id, f"Switched to {target} ({sessions_cfg[target]}).")
                continue
            if text == "/new":
                session_state(state, cur_name)["claude_session_id"] = None
                save_state(state)
                vk.send(peer_id, f"Started a fresh conversation in {cur_name}.")
                continue
            if text == "/reload":
                save_state(state)
                vk.send(peer_id, "Reloading the bridge...")
                log("[vk_bridge] /reload: re-executing in place.")
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, *sys.argv])

            user_text = text
            note_lines = []
            stem = f"{upd.get('object', {}).get('message', {}).get('id', uuid.uuid4().hex)}"

            for att in attachments:
                atype = att.get("type")
                if atype == "photo":
                    sizes = (att.get("photo") or {}).get("sizes") or []
                    if not sizes:
                        continue
                    best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                    p = vk.download_attachment_url(best["url"], MEDIA_DIR, stem, ".jpg")
                    if p:
                        note_lines.append(f"[The user sent an image via VK, saved at:\n{p}\n"
                                          f"Use the Read tool to view it.]")
                elif atype == "doc":
                    d = att.get("doc") or {}
                    url = d.get("url")
                    title = d.get("title") or "document"
                    ext = "." + (d.get("ext") or "bin")
                    base = re.sub(r"[^\w .()\-]+", "_", os.path.splitext(title)[0]).strip()
                    if url:
                        p = vk.download_attachment_url(url, MEDIA_DIR / stem, base or stem, ext)
                        if p:
                            note_lines.append(f"[The user sent a file ({title}) via VK, saved at:\n"
                                              f"{p}\nUse the Read tool to view it.]")
                elif atype == "audio_message":
                    am = att.get("audio_message") or {}
                    url = am.get("link_mp3") or am.get("link_ogg")
                    if not url:
                        continue
                    a = vk.download_attachment_url(url, MEDIA_DIR, stem, ".mp3")
                    transcript, terr = transcribe_audio(a) if a else (None, "download failed")
                    if transcript:
                        note_lines.append("[The text below was transcribed from a voice message.]")
                        user_text = f"{user_text}\n\n{transcript}".strip() if user_text else transcript
                    else:
                        vk.send(peer_id, f"[bridge] Couldn't transcribe that voice message - {terr}")

            message = ("\n".join(note_lines) + "\n\n" + user_text).strip() if note_lines else user_text
            if not message:
                continue

            log(f"[{cur_name}] {message[:120]}")
            cwd = Path(sessions_cfg[cur_name])
            sess = session_state(state, cur_name)
            new_sid, reset = run_claude_streaming(vk, peer_id, claude, cwd, message,
                                                   sess.get("claude_session_id"))
            if reset:
                new_sid, _ = run_claude_streaming(vk, peer_id, claude, cwd, message, None)
            sess["claude_session_id"] = new_sid
            save_state(state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[vk_bridge] stopped.")
