# deploy/

Host deployment artifacts for the production machine (`stl`).

## `com.n8n-shorts.api.plist`

The `launchd` LaunchAgent that supervises the `shorts-api` FastAPI service on
`stl`. `RunAtLoad` + `KeepAlive` → starts on login/boot and **auto-restarts on
crash** (10 s throttle). Added 2026-06-20 after a sustained mflux run crashed the
unsupervised uvicorn process and silently took out 4 consecutive scheduled runs.

It bakes `/opt/homebrew/bin` into `PATH` because `uv` and `ffmpeg` live there and
are not on a non-interactive shell's PATH. Runtime config/secrets are **not** in
the plist — they load from `api/.env` (gitignored) via the working directory.

This file is the source of truth; the deployed copy lives at
`~/Library/LaunchAgents/com.n8n-shorts.api.plist` on `stl`.

### Install / update on stl

```bash
# copy this file to the LaunchAgents dir, then (re)load it
cat deploy/com.n8n-shorts.api.plist | ssh stl 'cat > ~/Library/LaunchAgents/com.n8n-shorts.api.plist && plutil -lint ~/Library/LaunchAgents/com.n8n-shorts.api.plist'
ssh stl 'launchctl bootout gui/501/com.n8n-shorts.api 2>/dev/null; launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.n8n-shorts.api.plist'
```

### Manage

```bash
ssh stl 'launchctl print gui/501/com.n8n-shorts.api | grep -E "state|pid|last exit"'  # status
ssh stl 'launchctl kickstart -k gui/501/com.n8n-shorts.api'                            # restart
```

Do **not** start uvicorn manually on `stl` (`pkill`/`nohup`) — it fights launchd
for port 7860. See AGENTS.md §1.
