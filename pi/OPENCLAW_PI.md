# OpenClaw task: install Virta on this Raspberry Pi

You are on a Raspberry Pi 3 (user `lilja`, home `/home/lilja`). Install **Virta** so it
runs on its own after boot: a live loop that reads Home Assistant, and a fullscreen console
on the HDMI screen. The code is finished and tested on a Windows PC. Your job is to get
it running here. **Do not redesign or rewrite it.**

## What Virta is (enough to do this job)

- Python package `virta/`. Every path in it is relative to the project root, so always
  run it with the working directory set to `/home/lilja/virta`.
- `python -m virta.live` polls Home Assistant (LAN, `http://192.168.86.33:8123`) every
  ~15 s, runs a load detector, writes `var/state.json`, and makes rate-limited cloud calls
  to Nebius Token Factory (NVIDIA Nemotron). At 03:30 it runs a nightly analysis. It also
  acts through HA: speaks on a smart speaker, sets a status lamp, and can switch one
  configured light.
- `python -m virta.console --fullscreen` draws `var/state.json` with pygame. It never
  calls HA or the cloud. It is only a screen.
- The two processes only talk through files in `var/`. Each can restart without the other.
- Config comes from `/home/lilja/virta/.env` (secrets: Nebius key and HA token).

## Hard rules

1. **Never print, cat, log, or copy the contents of `.env`**, and never echo `HA_TOKEN` or
   `NEBIUS_API_KEY`. If you need to confirm a variable is set, check that it isn't empty.
   Do `chmod 600 .env`.
2. **Don't touch Home Assistant config.** Don't create or edit automations, scripts or
   entities. Virta only reads HA and calls services, and that is its own business.
3. **Don't edit Virta's Python code** except for a real Pi incompatibility, such as a
   syntax feature this Python lacks. Make any such change minimal and list it in your
   report as a diff.
4. **Leave `~/project` alone** (the matrix screen, `matrix_camera.py`). Don't delete or
   edit it. You may stop it and add a way to switch the screen between matrix and Virta
   (step 7).
5. **Cloud calls cost money.** The only intentional call in this task is the single
   `hello_nebius` check in step 4. Use `--dry` for other live-loop tests. Never delete or
   reset `var/cloud_usage.json` (the spend ledger). Never remove or loosen the caps in
   `.env`.
6. System changes (apt packages, groups, systemd units, disabling console blanking) are
   allowed. List every one in the report so it can be undone.

## Step 0 - look before you change anything

Report the following:
- OS and version, 32- or 64-bit (`uname -m`, `/etc/os-release`), Python version, free RAM
  and disk.
- Whether a desktop is running (X11/Wayland) or the Pi boots to a text console, and which
  video driver is in use (`/boot/firmware/config.txt` or `/boot/config.txt`: is
  `dtoverlay=vc4-kms-v3d` present?), plus whether `/dev/dri/card*` exists.
- The HDMI screen's resolution (`kmsprint`, `fbset`, or `tvservice -s`).
- The timezone (`timedatectl`). **It must be `Europe/Helsinki` with NTP synced.** Prices
  and the 03:30 nightly depend on it. Fix it if it's wrong.
- How the matrix screen is started, if anything autostarts it (systemd, crontab, rc.local,
  `.bashrc`, `.profile`, autologin). Report it, don't change it yet.
- That HA is reachable: `curl -s -o /dev/null -w "%{http_code}" http://192.168.86.33:8123/`.

## Step 1 - unpack

The user copies two files into `/home/lilja`: `virta_pi.tgz` (code, profiles, labels,
sample day, the local power archive, last night's insights) and `.env`.

```bash
mkdir -p ~/virta && tar -xzf ~/virta_pi.tgz -C ~/virta
mv ~/.env ~/virta/.env && chmod 600 ~/virta/.env
mkdir -p ~/virta/var
```

If `~/virta` already exists, back it up first (`mv ~/virta ~/virta.bak-$(date +%s)`), then
copy `.env`, `var/cloud_usage.json` and `data/` back from the backup.

## Step 2 - Python environment

```bash
cd ~/virta
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` needs only `openai` and `pygame-ce`. Problems to expect on a Pi 3:
- **`openai` pulls in `pydantic-core` and `jiter`, which are built in Rust.** On 64-bit
  (aarch64) PyPI has wheels. On 32-bit (armv7l), make sure piwheels is in use
  (`/etc/pip.conf` has `extra-index-url=https://www.piwheels.org/simple`). If a Rust build
  still starts, stop and try an older `openai` whose deps have armv7 wheels on piwheels.
  Don't install a Rust toolchain unless nothing else works; a Pi 3 builds for hours.
- **pygame-ce** must be able to open the screen (step 5). If its wheel's SDL has no
  `kmsdrm` support, use the distro build instead: `sudo apt install python3-pygame`,
  recreate the venv with `python3 -m venv --system-site-packages .venv`, reinstall
  `openai` only, and uninstall `pygame-ce` from the venv. The console only uses basic
  pygame APIs (`draw.line/rect/circle/ellipse/lines`, `font.SysFont`, `display.set_mode`),
  so classic pygame 2.x is fine.
- Fonts: the console asks for `dejavusansmono` among others. Make sure
  `fonts-dejavu-core` is installed.

Check: `.venv/bin/python -c "import openai, pygame; print(openai.__version__, pygame.version.ver)"`

## Step 3 - offline checks (free)

```bash
cd ~/virta
.venv/bin/python -m compileall -q virta && echo COMPILED
.venv/bin/python -m virta.check_safeguards        # must end "Guardrails held: True"
.venv/bin/python -m virta.check_ha                # reads the HA entities live
```

`check_detector` may need `csv/` exports that were deliberately not copied. If it fails
only because of missing files, note it and move on.

If `compileall` reports syntax errors, this Python is older than the code expects (it was
developed on 3.14, and 3.10+ is assumed). Report the Python version and the errors. Fix
them minimally only if they are few and mechanical; otherwise stop and report.

## Step 4 - one real cloud call, then the loop dry

```bash
.venv/bin/python -m virta.hello_nebius            # ONE call; must print an answer, exit 0
.venv/bin/python -m virta.live --once --dry       # one tick, no cloud call
.venv/bin/python -m virta.live --minutes 3 --dry --quiet
```

Confirm that `var/state.json` is rewritten every ~15 s, and report how long a tick takes
and the CPU use (`top`/`ps`) of the live loop on the Pi 3.

**Only one live loop may run in the house.** The PC also runs one during development, and
two loops would both speak, both switch lights and each keep its own call budget. The user
stops the PC loop before step 6. Don't start a non-dry live loop before that.

## Step 5 - the console on the HDMI screen

First render a frame without the screen, at the real HDMI resolution from step 0:

```bash
.venv/bin/python -m virta.console --snapshot var/pi_snapshot.png --size <W>x<H>
```

Then draw it fullscreen:
- **Text console (no desktop):** run from a local tty or via systemd with
  `SDL_VIDEODRIVER=kmsdrm`. The user needs access to `/dev/dri` and input:
  `sudo usermod -aG video,render,input lilja` (then log out and back in).
- **Desktop running:** `DISPLAY=:0 .venv/bin/python -m virta.console --fullscreen`
  (or `WAYLAND_DISPLAY` under Wayland).

```bash
SDL_VIDEODRIVER=kmsdrm .venv/bin/python -m virta.console --fullscreen --fps 15
```

Measure CPU at `--fps 15`, then at `--fps 10`. Pick the highest fps that keeps the console
under ~50% of one core while the live loop keeps its 15 s tick. If fullscreen at the native
resolution is too slow (for example 1920x1080), report it. Don't change the HDMI mode
without saying so; `hdmi_group`/`hdmi_mode` for 1280x720 is the likely fix and the user
decides.

Disable console blanking so the screen stays on: add `consoleblank=0` to `cmdline.txt`,
or the equivalent for this OS. Hide the blinking text cursor behind the console if it
shows (`vt.global_cursor_default=0`).

## Step 6 - run on boot (systemd)

Create two system units that run as `lilja`, with `WorkingDirectory=/home/lilja/virta`:

`virta-live.service`
- `ExecStart=/home/lilja/virta/.venv/bin/python -u -m virta.live --quiet $VIRTA_LIVE_ARGS`
- `EnvironmentFile=-/home/lilja/virta/pi/live.env`, which holds `VIRTA_LIVE_ARGS=`. The
  user switches demo mode by setting it to `--demo`.
- `After=network-online.target time-sync.target`, `Wants=network-online.target`.
- `Restart=always`, `RestartSec=10`.

`virta-console.service`
- `ExecStart=/home/lilja/virta/.venv/bin/python -m virta.console --fullscreen --fps <chosen>`
- `Environment=SDL_VIDEODRIVER=kmsdrm` (text console case) or the `DISPLAY` equivalent.
- `Restart=always`, `RestartSec=5`, `After=virta-live.service`. Independent: the console
  can start before any state exists (it shows NO SIGNAL until state arrives).
- If it has to take over `tty1`, use `TTYPath=/dev/tty1` with
  `Conflicts=getty@tty1.service`. The Pi still has SSH for maintenance.

Enable both, but only **start** `virta-live` after the user confirms the PC loop is
stopped. Logs go to journald (`journalctl -u virta-live -f`).

## Step 7 - small helper scripts in `~/virta/pi/`

The Pi has no keyboard, so these are what the user types over SSH:

- `screen.sh virta|matrix`: stop `virta-console` and start the matrix exactly the way
  step 0 found it (or run `python3 matrix_camera.py --input video --video-file matrix.mp4
  --output tty` from `~/project` on tty1), and back. Never touch `virta-live` here: Virta
  keeps listening while the matrix is on screen.
- `demo_price.sh <c/kWh>|off`: write the number to `~/virta/var/demo_price`, or delete
  it. The live loop treats it as the current slot's price and the console shows DEMO
  PRICE. This is how the "electricity turns expensive" scene is filmed.
- `demo_mode.sh on|off`: set `VIRTA_LIVE_ARGS=--demo` or empty in `pi/live.env`, then
  `sudo systemctl restart virta-live`.
- `answer.sh yes|no`: read the pending proposal id from `var/state.json`
  (`.proposal.id`) and write `var/answer.json` as
  `{"id": "<id>", "answer": "yes"|"no", "at": "<ISO time with offset>"}`, which is what the
  console writes when Y/N is pressed. Print "no proposal pending" if there is none.
- `status.sh`: both services' state, the age of `var/state.json`, today's cloud call count
  from `var/cloud_usage.json`, the last 10 lines of `journalctl -u virta-live`, CPU temp
  (`vcgencmd measure_temp`), and the `vcgencmd get_throttled` flags.

Make them executable. Keep them short and plain bash.

## Step 8 - verify and report

With the user's go-ahead (PC loop stopped), start `virta-live`. Within ~1 minute:
- `var/state.json` should be fresh, and the console shows live readings and the trace.
- Within a few minutes the top panel shows a Nemotron line, and the lamp may change colour.
- `sudo reboot`, then check that both services come back by themselves and that the
  screen shows Virta without anyone logging in.
- Run `status.sh`.

Report back with:
1. What step 0 found.
2. Every system change (packages, groups, units, boot cmdline and config.txt edits), each
   with how to undo it.
3. Any change to Virta code, as a diff.
4. Measured performance: tick time, console fps chosen, CPU for both, temperature and
   throttling after 10 minutes.
5. Anything that didn't work, with the exact error.
