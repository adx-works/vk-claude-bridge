# vk-claude-bridge

A VKontakte bot that is a phone remote for local [Claude Code](https://claude.com/claude-code)
agents. Message it, pick a project with `/switch`, and a persistent headless Claude
conversation runs on your machine in that project's directory - the answer streams back into
the chat, edited live into the same bubble as it's written, tool calls shown as short code
boxes. When the agent wants you to pick between a few options, it ends its reply with a
`CHOICES:` block, which the bridge turns into tappable buttons instead of text you'd have to
type back.

Modeled on [runa-tic/tg-brain-bridge](https://github.com/runa-tic/tg-brain-bridge) (same
long-poll-only, no-webhook, single-file-stdlib shape), adapted from Telegram to VK's Bot Long
Poll API, with two additions that repo explicitly doesn't do: multiple named sessions in one
chat, and options rendered as buttons.

```
your phone (VK) ──► bot long-poll ──► vk_bridge.py ──► one `claude -p` turn
      ▲                                    │              (persistent session per project)
      └── live-edited preview message ◄────┘
          (text streams in, tool calls appear as [boxes], CHOICES: → buttons)
```

## What it does

- **Multiple projects, one chat.** `sessions` in the config maps a short name to a working
  directory. `/switch <name>` changes which one the current message goes to; each keeps its
  own persisted `claude` session id, so switching back resumes that project's history, not a
  fresh one. `/sessions` lists them with the current one marked.
- **One persistent conversation per session.** Session ids and the long-poll `ts` cursor are
  persisted, so a restart (or `/reload`) continues mid-thought. `/new` resets only the
  *current* session's history.
- **Live streaming**, edited into a single message via `messages.edit`, then finalized with
  markdown rendered down to plain text (VK bot messages have no rich formatting) - a
  non-streaming fallback runs if the stream path fails, so a reply is never lost.
- **Options as buttons.** The agent is told via `--append-system-prompt` that it can end a
  reply with:
  ```
  CHOICES:
  - Do X
  - Do Y instead
  ```
  The bridge strips that block from the displayed text and attaches a VK reply-keyboard built
  from it (up to 8 options). Tapping a button just sends its label back as a normal message -
  no VK callback/event plumbing needed, which also means it degrades gracefully to "type the
  option" on any VK client that doesn't render keyboards.
- **Photos, documents, and voice messages** are downloaded and handed to the agent to `Read`;
  voice is transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (optional dep).
- **Locked down.** One whitelisted VK numeric user id (discovery mode tells you yours on first
  message), and a restricted `--allowedTools` set for every turn - files, search, web, one
  whitelisted scripts dir per project. **No arbitrary Bash, by design**: this thing runs
  unattended, so it must never be handed a tool that needs a human to approve what it's about
  to do (SSH to a server, `rm`, deploys, ...). Don't widen `ALLOWED_TOOLS` without thinking
  through what that lets an unattended agent do at 3am.

## Setup

1. **Create the VK bot.** If you don't already have a community: create one at
   `vk.com/groups` → *Create community*. In it: *Manage → API usage → Bot Long Poll API* -
   turn it on, and under *Event types* enable at least **Incoming messages** (`message_new`).
   Then *Manage → API usage → Access tokens → Create token*, scope `messages`, copy it. Grab
   the community/group id from the community's URL or *Manage → API usage*.
2. Copy `.secrets/vk_bridge.json.example` to `.secrets/vk_bridge.json` and fill in
   `group_token`, `group_id`, and `sessions` (name → absolute path to each project's working
   directory - wherever its `CLAUDE.md` and files live). Leave `allowed_user_id` at `0`,
   message the bot once, and it replies with your id; paste it in and restart.
   (Env vars `VK_BRIDGE_GROUP_TOKEN` / `VK_BRIDGE_GROUP_ID` / `VK_BRIDGE_ALLOWED_USER_ID` also
   work, if you'd rather not put the token in a file.)
3. Optional voice: `pip install faster-whisper` (CPU int8; `small` model is the default).
4. Run it: `python vk_bridge.py`, or under Docker (below), or any supervisor (pm2, systemd).

## Commands

- `/sessions` - list configured projects, current one marked
- `/switch <name>` - change which project the chat is currently talking to
- `/new` - start a fresh conversation in the *current* project only
- `/reload` - restart the bridge process (e.g. to pick up a code change)
- `/start`, `/help` - overview + current session

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`.secrets/` is bind-mounted from the host (holds the token and the persisted session/ts
state), so redeploying the container never bakes it into the image. `~/.claude` and
`~/.claude.json` are mounted too - `claude` needs its own login and writes session history
there, so run `claude setup-token` (or just `claude` once) on the host first. Each `sessions`
path also needs to be mounted at the *same absolute path* inside the container - see
`docker-compose.yml`, which mounts the two paths from the example config; add or change lines
there to match your own `sessions`.

## Design notes, mostly copied from tg-brain-bridge because the failure modes are the same

- **ts-cursor persisted before processing**, so a crash mid-turn (or `/reload`) never replays
  a message into the agent twice.
- **Session-loss self-heal:** a failed `--resume` (session GC'd, machine moved) is detected
  from stderr and retried once with a fresh session instead of erroring forever.
- **`CREATE_NO_WINDOW`** on the claude child so a supervisor on Windows doesn't flash a console
  per turn (harmless no-op on Linux).
- **CHOICES: is opt-in per reply**, not a tool call - the agent's normal text turn either has
  the block or it doesn't, so nothing about the interactive `AskUserQuestion` tool needs to
  exist in a headless context (it can't - there's no terminal on the other end to render it).

## Limitations

- One operator, one VK chat, serialized turns per session - it's a remote for *your* agents,
  not a bot platform for a team.
- Long-poll only (no webhook server, nothing listens on any port).
- A session's working directory is trusted at face value - don't point one at something you
  wouldn't want an unattended, Bash-less agent reading and editing on your behalf. This
  workspace's own rule applies here as much as anywhere: production is touched only with
  explicit permission, and this bot has no way to ask for it - so don't give it the tools that
  would let it try.

## License

MIT (matching the project this is modeled on).
