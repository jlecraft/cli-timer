# timer

A small CLI countdown timer for Linux. Set it and get your shell prompt back
immediately — it rings from the background with a desktop notification and a
single alarm sound, and never blocks or steals focus.

## Usage

```
timer <duration> [label...]
timer               # no args: list all active timers and time remaining
```

### Duration formats

| Format      | Meaning              |
|-------------|----------------------|
| `90`        | 90 seconds           |
| `45s`       | 45 seconds           |
| `10m`       | 10 minutes           |
| `1h30m`     | 1 hour 30 minutes    |
| `25:00`     | MM:SS (25 minutes)   |
| `1:30:00`   | HH:MM:SS             |

### Examples

```
timer 10m
timer 1h30m "Roast chicken"
timer 90 "Tea"
timer
```

## How it works

Running `timer 10m` re-launches the script as a second, fully-detached
background process (the "worker") and returns control to your shell right
away. The worker sleeps until the deadline, plays the alarm sound once, and
shows a desktop notification (`notify-send`) that disappears on its own after
a few seconds without stealing keyboard/mouse focus - then it exits.

Running bare `timer` with no arguments lists every active timer by scanning
`/proc` for worker processes and reading each one's deadline/label straight
out of its own argv. There's no separate state file or registry to keep in
sync - the process table is the source of truth, so a timer disappears from
the list the moment it's actually gone (rung, or killed).

## Requirements

- Linux with `/proc` (this won't work on macOS/BSD)
- Python 3
- [`paplay`](https://www.freedesktop.org/wiki/Software/PulseAudio/) (part of `pulseaudio-utils`) - plays the alarm sound
- [`notify-send`](https://www.freedesktop.org/wiki/Software/notification-spec/) (part of `libnotify-bin`) - shows the desktop notification

## Setup

Make the script executable and put it on your `PATH`, e.g.:
```
chmod +x timer.py
ln -s "$(pwd)/timer.py" ~/.local/bin/timer
```
(make sure `~/.local/bin` is on your `PATH`)

The alarm sound (`alarm.ogg`) ships in this repo alongside `timer.py` and is
found relative to the script's real location, so this works whether you run
it directly or through the symlink above. Swap in your own sound by
replacing `alarm.ogg`, or point `ALARM_SOUND` in `timer.py` at a different file.
