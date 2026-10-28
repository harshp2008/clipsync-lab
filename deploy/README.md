# Deployment files (written, not installed)

These unit files are provided for the owner to review and install. **No service was
installed or started by the agent** and `systemctl` was not run.

| File | Target | Notes |
|---|---|---|
| `clipsync-relay.service` | system unit (`/etc/systemd/system/`) | relay role; dedicated `clipsync` user |
| `clipsync-member.service` | user unit (`~/.config/systemd/user/`) | laptop daemon; runs as you, for the Wayland clipboard |

Both `ExecStart` lines use `python3 -m clipsync.daemon`, the entry point declared as
`clipsync = "clipsync.daemon:main"` in `pyproject.toml`. They assume this checkout is
at `/opt/clipsync` (relay) and `~/clipsync-lab` (member); edit the paths if yours
differ.

## Relay on a small always-on box

```bash
sudo useradd --system --home-dir /var/lib/clipsync --shell /usr/sbin/nologin clipsync
sudo install -d -o root -g clipsync -m 0750 /etc/clipsync
sudo install -o root -g clipsync -m 0640 examples/relay.toml /etc/clipsync/relay.toml
sudo cp deploy/clipsync-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clipsync-relay.service
```

Replace every `CHANGE-ME-...` in `relay.toml` first (`scripts/gen_secrets.py` prints
the tokens). The relay needs no `e2e_key`. Keep the token readable by the
`clipsync` group only; never commit it.

## Laptop member

```bash
mkdir -p ~/.config/clipsync ~/.config/systemd/user
cp examples/member-laptop.toml ~/.config/clipsync/member-laptop.toml
# edit the tokens/key/addresses, then:
cp deploy/clipsync-member.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now clipsync-member.service
```

The daemon refuses to start while any `CHANGE-ME` placeholder remains. Secrets live
in `~/.config/clipsync/` with mode 0600.

## What is not covered

The TV box is deliberately out of scope: its OS is unknown, so no unit, package or
OS-specific path is written for it. See the README's "TV box (later)" section for
the information needed before that work can start.
