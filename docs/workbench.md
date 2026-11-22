# Workbench: build, host and operate from a chat

The workbench turns agent mode into a remote development and operations seat:
create or clone a project, have the agent write and commit the code, keep the
result running as a service with an address your phone can open, run commands
on your other computers, and hand work to other coding agents installed on the
server. All of it is driven from an Aegis chat, at home or away.

It is four administrator-only tools, a few chat commands, and one rule: **a
project opened in a chat stays open in that chat**.

| Tool | Chat command | What it does |
|---|---|---|
| `manage_projects` | `/project` | Create, clone, open, list code projects. Opening one binds it to the chat. |
| `manage_apps` | `/apps` | Deploy a project's app as a service with a URL; status, logs, restart, remove. |
| `manage_hosts` | `/hosts`, `/remote <host> <command>` | Run commands and copy files on other machines over SSH. |
| `delegate_agent` | — | Hand a task to Claude Code, Kimi Code, Codex, OpenCode or Aider. |

You rarely name the tools. In agent mode: *"build me a workout tracker and host
it"*, *"restart the tracker and show me its log"*, *"how much disk is free on my
Windows PC"*, *"have Claude Code add login to the tracker"*.

## Projects

A project is a git repository under the projects folder (`projects_dir`
setting, default `~/apps`). `create` makes the folder with a README, a
`.gitignore` and a first commit; `clone` takes an https/ssh git URL or
`owner/repo` for GitHub.

**Opening a project binds it to the chat.** From then on, in that chat:

- the file tools are confined to the project and relative paths resolve in it;
- the shell starts there, with your git identity, GitHub login (`gh`) and the
  tools from your version manager on `PATH`;
- this holds on every later message, after an Aegis restart, and in the turn
  that runs when a background job finishes.

The binding is per chat and lives on the server (`data/workbench.json`), not in
the browser — so it follows you between phone and desktop, and two chats can
work in two projects at once. It outranks the browser-wide workspace pill.
`/project` shows what is open; `/project close` unties the chat.

`aegis` and `engine` in the projects folder are Aegis itself and its engines:
they can be opened to work on, but never created over, deployed or removed by
these tools.

## Hosted apps

`manage_apps deploy` takes a project and a **foreground start command that
listens on `$PORT`** and makes it a systemd *user* unit,
`aegis-app-<name>.service`:

- it starts at boot (user lingering: `sudo loginctl enable-linger <user>`, once),
- restarts after a crash (a crash loop backs off),
- is independent of Aegis — restarting Aegis does not touch your apps,
- needs no root, which matters because a language model is the caller.

Web apps get the next free port from 8300–8899 and are published with
`tailscale serve --https=<port>`, so the address is
`https://<host>.<tailnet>.ts.net:<port>/`. The app itself listens on loopback.
`"web": false` is for workers and bots with no port. Secrets go in the
project's `.env` (git-ignored by the template; the unit loads it).

```
manage_apps {"action": "deploy", "name": "tracker",
             "command": "uv run uvicorn app:app --host 127.0.0.1 --port $PORT"}
→ 'tracker' is running. Open it at https://box.tailnet.ts.net:8300/ …
```

`remove` stops and unregisters an app; it never deletes the project's code.

### Who can reach a hosted app

| From | How | Notes |
|---|---|---|
| The server itself | `http://127.0.0.1:<port>/` | always |
| Your phone and PCs, anywhere | the `https://…ts.net:<port>/` address | every device signed in to your tailnet; HTTPS; same URL at home and away |
| Devices on your LAN that are **not** on the tailnet | not by default | apps bind loopback and the firewall only admits SSH from the LAN — opening a port to the LAN is a deliberate step (bind `0.0.0.0`, allow the port in the firewall) |
| The public internet | not at all | would need Tailscale Funnel or a tunnel and a decision per app |

Hosting something "inside Aegis" does not change this: Aegis itself is reached
over the tailnet, so a page inside it is exactly as reachable as Aegis is.
Separate apps on their own ports are the simpler and safer arrangement — an
app's bugs stay out of the origin that holds your Aegis session.

## Other machines

A host is a name for `user@address`. The server logs in with its own SSH key —
no passwords, no prompts — so the other machine must run an SSH server and
list the key (`~/.ssh/id_ed25519.pub` on the server) in its `authorized_keys`
(Windows administrator accounts: `C:\ProgramData\ssh\administrators_authorized_keys`).

```
manage_hosts {"action": "add", "host": "desktop", "address": "desktop.tailnet.ts.net", "user": "me"}
manage_hosts {"action": "run", "host": "desktop", "command": "Get-Service spooler | Restart-Service"}
manage_hosts {"action": "copy", "host": "desktop", "source": "/srv/build.zip",
              "destination": "C:/Users/me/Downloads/", "direction": "put"}
```

Windows hosts run PowerShell, Linux and macOS hosts run bash; `peers` lists the
machines on the tailnet. How commands travel, and why:

- **One connection per host, reused** (`ControlMaster`). Windows' OpenSSH
  server stops accepting commands for a while after a burst of fresh logins.
- **Scripts go over stdin, never the command line.** A multi-line PowerShell
  script quoted through `cmd.exe` is a coin toss, and `-EncodedCommand` is the
  pattern security software looks for. The remote side reads the whole script
  and evaluates it as one unit, so multi-line blocks work.
- **Every command is written to `data/workbench/audit.log`** before it runs,
  together with deploys and hand-offs.

What this gives you on Windows is the command line: services, processes,
files, winget, scheduled tasks, WSL, the registry. It does not drive the
desktop (clicking through windows); launch GUI work through a scheduled task
that runs in your session if you need that.

## Other agents

`delegate_agent` runs another coding agent non-interactively in the project
folder, as a background job of the chat. When it finishes, Aegis's agent is
called again with the output — you get the result even if you closed the
browser in between (see `/api/chat/resume`).

| Agent | Runs | Autonomy switch |
|---|---|---|
| `claude` | `claude -p … --permission-mode acceptEdits` | `full` → `--dangerously-skip-permissions` |
| `kimi` | `kimi -p …` | none in prompt mode (its own policy) |
| `codex` | `codex exec --sandbox workspace-write …` | `full` → `--dangerously-bypass-approvals-and-sandbox` |
| `opencode` | `opencode run …` | `full` → `--auto` |
| `aider` | the bundled Aider on the local model | — |

`edits` (the default, `delegate_autonomy` setting) lets the agent change files
in the project; `full` lets it also run any command without asking. Those
agents use their own logins and cloud models, so they need the internet;
Aider and Aegis's own agent do not. `"continue": true` resumes the agent's
previous session in that folder.

## Persistence

| What | Survives | Where |
|---|---|---|
| Project code | everything | the folder, in git |
| Chat ↔ project binding | Aegis restarts, reboots | `data/workbench.json` |
| Hosted apps | Aegis restarts, crashes, reboots | systemd user units + lingering |
| Tailnet addresses | reboots | Tailscale's serve configuration |
| Registered hosts | everything | `data/workbench.json` |
| A running agent turn | the browser closing (re-attach with the chat open) | in memory — **not** an Aegis restart |
| Background jobs and hand-offs | Aegis restarts | `data/bg_jobs/` |

## Settings

`workbench_enabled` (true), `projects_dir` (empty = `~/apps`),
`delegate_autonomy` (`edits`). Environment: `AEGIS_PROJECTS_DIR`;
`AEGIS_WORKBENCH_DISABLED=1` switches everything off (the test suite sets it).

## Security notes

- All four tools and every `/api/workbench/*` endpoint are administrator-only.
- These tools let the model start services, run commands as you on this
  server and as the registered account on other machines (on Windows that is
  usually an elevated session), and steer other agents. The agent is told to
  confirm before destructive actions; the audit log records what it did.
- Apps are published to the tailnet only. Nothing here uses Funnel or opens a
  firewall port.
- Hosts are trusted on first use (`StrictHostKeyChecking=accept-new`); a
  changed host key is refused.
- `git clone` accepts only https/ssh URLs — git's `ext::`/`fd::` transports,
  which run commands, are rejected.

## Troubleshooting

- **"nothing is listening on 127.0.0.1:PORT"** — the command exited, is
  listening on another port, or daemonised itself. `manage_apps logs`.
- **Windows host answers `exec request failed on channel 0`** — its SSH server
  is refusing new sessions after too many logins in a row; it recovers within
  a few minutes. The workbench reuses one connection to avoid this.
- **`gh`/`git push` asks for credentials** — `gh` keeps its token in the
  desktop keyring, which is locked until someone has logged in to the desktop
  after a reboot.
- **A user unit does not start at boot** — `loginctl show-user $USER -p Linger`
  must say `yes`.
