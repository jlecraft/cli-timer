#!/usr/bin/env python3
"""
timer - a CLI countdown timer that runs in the background and alerts you
with a non-blocking notification + a single alarm sound when it finishes.

Usage:
    timer <duration> [label...]
    timer               (no args) - list all active timers and time remaining

Duration formats:
    90          -> 90 seconds (bare integer = seconds)
    45s         -> 45 seconds
    10m         -> 10 minutes
    1h30m       -> 1 hour 30 minutes
    25:00       -> MM:SS  (25 minutes)
    1:30:00     -> HH:MM:SS (1 hour 30 minutes)

Examples:
    timer 10m
    timer 1h30m "Roast chicken"
    timer 90 "Tea"

How it works, in a nutshell:
    Running `timer 10m` immediately re-launches THIS SAME SCRIPT as a
    second, fully-detached background process (the "worker"), and then
    the original command exits right away so your shell prompt comes
    straight back. The worker sleeps until the deadline, then plays the
    alarm sound once and shows a desktop notification that disappears on
    its own after a few seconds without stealing focus or blocking input,
    then exits. `timer` with no args lists active timers by reading each
    worker's deadline/label straight out of its own argv in /proc - there's
    no separate registry to keep in sync, the process table is the truth.
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

# The alarm sound lives alongside this script in the repo. We resolve via
# realpath (not just __file__'s own directory) because `timer` is normally
# invoked through a symlink (e.g. ~/.local/bin/timer) - __file__ would then
# be the symlink's path, not the repo checkout, and dirname() of that would
# point at the wrong directory.
ALARM_SOUND = os.path.join(os.path.dirname(os.path.realpath(__file__)), "alarm.ogg")

# A sentinel argument we pass to ourselves to say "don't act like the CLI,
# act like the background worker." Leading underscore just signals
# "internal, not part of the public command-line interface."
WORKER_FLAG = "_worker"

# How many seconds the "timer complete" notification stays on screen before
# the notification daemon auto-dismisses it.
NOTIFICATION_SECONDS = 5


def parse_duration(text: str) -> int:
    """
    Turn a duration string into a whole number of seconds.

    We support three input styles because different ones feel natural
    depending on the length of the timer:
      1. Bare number         "90"      -> seconds
      2. Unit-suffixed combo "1h30m"   -> hours/minutes/seconds
      3. Clock/colon form    "25:00"   -> MM:SS or HH:MM:SS
    """
    text = text.strip()

    # --- Style 1: bare integer, e.g. "90" -------------------------------
    # isdigit() is enough here since we only support whole seconds - no
    # need to drag in float parsing for a CLI timer.
    if text.isdigit():
        return int(text)

    # --- Style 3: colon form, e.g. "25:00" or "1:30:00" -----------------
    if ":" in text:
        parts = text.split(":")
        if not all(p.isdigit() for p in parts):
            raise ValueError(f"Invalid duration: {text!r}")
        parts = [int(p) for p in parts]
        # Pad on the left so we always end up with [hours, minutes, seconds],
        # e.g. "25:00" (2 parts) becomes [0, 25, 0].
        while len(parts) < 3:
            parts.insert(0, 0)
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds

    # --- Style 2: unit-suffixed combo, e.g. "1h30m", "45s", "10m" -------
    # This regex looks for an optional hour group, optional minute group,
    # optional second group, in that order, each of the form <digits><unit>.
    # e.g. against "1h30m" it captures groups ("1", "30", None).
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if not match or not any(match.groups()):
        raise ValueError(f"Invalid duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def format_duration(total_seconds: int) -> str:
    """Turn seconds back into a friendly string like '1h 30m' for the
    confirmation message printed to the user."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    # Always show seconds if nothing else applies (e.g. "45s"), or if
    # there's a leftover remainder (e.g. "1h 30m 5s").
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def list_timers() -> None:
    """
    Scan /proc for running worker processes and print each one's remaining
    time and label, soonest-first. This is what bare `timer` (no arguments)
    does. Rather than maintaining a separate registry of active timers, the
    process table is the source of truth: a worker's deadline and label are
    already sitting in its own argv (see start_timer's Popen call), so
    reading them directly can never drift out of sync with what's actually
    still running.
    """
    entries = []  # (remaining_seconds, label)
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")[:-1]
            argv = [part.decode() for part in argv]
        except (OSError, UnicodeDecodeError):
            # Process exited between listdir and open, or we don't have
            # permission to read it - either way, skip it.
            continue

        # A worker's argv is always exactly:
        #   [<python>, <path/to/timer script>, WORKER_FLAG, <deadline>, <label>]
        # We match on WORKER_FLAG alone (not argv[1]'s path) because the
        # timer command can be invoked through different paths that all
        # point at this same file - e.g. a direct dev checkout vs. an
        # installed symlink - and those would otherwise produce different
        # argv[1] values for what's really the same worker "kind".
        if len(argv) != 5 or argv[2] != WORKER_FLAG:
            continue

        try:
            deadline = float(argv[3])
        except ValueError:
            continue
        label = argv[4]
        remaining = max(0, round(deadline - time.time()))
        entries.append((remaining, label))

    if not entries:
        print("No active timers.")
        return

    entries.sort()  # soonest-first
    plural = "" if len(entries) == 1 else "s"
    print(f"{len(entries)} active timer{plural}:")
    for remaining, label in entries:
        print(f"  {format_duration(remaining):>10} remaining - {label}")


def start_timer(duration_text: str, label_words: list[str]) -> None:
    """
    The "front door" of the program: what runs when you type
    `timer 10m "Tea"` at the shell. Its whole job is to hand the actual
    waiting off to a background worker and return control to you
    immediately, so your terminal isn't blocked for the next 10 minutes.
    """
    duration_seconds = parse_duration(duration_text)
    label = " ".join(label_words) if label_words else "Timer"

    # The deadline is a fixed point in time (seconds since the epoch),
    # not "sleep for N seconds". We pass this absolute timestamp to the
    # worker instead of the duration so that if there's any delay
    # between launching the worker and it actually starting to sleep,
    # the timer still rings at the right wall-clock moment rather than
    # running slightly long.
    deadline = time.time() + duration_seconds

    # This is the key trick that makes the timer "run in the background":
    # we re-invoke this exact same script (sys.executable = the python3
    # interpreter, __file__ = this file) but with a special WORKER_FLAG
    # argument, so when that new process starts up it takes the worker
    # path instead of parsing CLI args again.
    #
    #   start_new_session=True   Puts the child in a brand new OS session
    #                            (and process group), detaching it from
    #                            this terminal. Without this, closing the
    #                            terminal (or it dying) would send SIGHUP
    #                            to the whole process group, including
    #                            our worker, killing the timer early.
    #
    #   stdin/stdout/stderr      Redirected to DEVNULL so the worker has
    #     = DEVNULL              no open file handles pointing at this
    #                            terminal. This is what lets the shell
    #                            consider the terminal "free" again -
    #                            otherwise some shells/terminals wait for
    #                            all processes holding the tty open to
    #                            finish before you get your prompt back.
    #
    #   close_fds=True           Don't let the child inherit any other
    #                            open file descriptors from us either.
    subprocess.Popen(
        [sys.executable, __file__, WORKER_FLAG, str(deadline), label],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    ring_time = datetime.fromtimestamp(deadline).strftime("%-I:%M:%S %p")
    print(f"Timer set for {format_duration(duration_seconds)} "
          f"({label!r}) - will ring at {ring_time}")


def run_worker(deadline: float, label: str) -> None:
    """
    The background half of the program. This function runs inside the
    detached child process that start_timer() launched above. Nobody is
    watching its stdout (it was redirected to DEVNULL), so from here on
    the only way it communicates with the user is via the sound + notification.
    """
    # Sleep until the deadline. We compute the remaining time rather than
    # sleeping for a stored "duration" because some time may have already
    # passed between the parent process computing the deadline and this
    # worker process actually getting scheduled and reaching this line.
    # max(0, ...) guards against a negative sleep if we're somehow already
    # past the deadline.
    remaining = deadline - time.time()
    if remaining > 0:
        time.sleep(remaining)

    # --- Play the alarm sound once, in the background -----------------
    # Popen (rather than run) so this doesn't block the notification below
    # from appearing at essentially the same time. There's nothing to
    # track or kill afterward - paplay plays the file once and exits on
    # its own.
    subprocess.Popen(["paplay", ALARM_SOUND])

    # --- Show a desktop notification -----------------------------------
    # notify-send fires off a notification bubble and returns almost
    # immediately - unlike a dialog window, it never grabs keyboard/mouse
    # focus and can't block input. `-t` tells the notification daemon how
    # long to display it (milliseconds) before auto-dismissing it; we
    # don't have to wait around for that ourselves. The suppress-sound
    # hint tells the daemon not to play its own default notification
    # sound, since we're already playing our own alarm sound above.
    subprocess.run([
        "notify-send",
        "-t", str(NOTIFICATION_SECONDS * 1000),
        "-h", "boolean:suppress-sound:true",
        f"{label}",
        "Timer Complete",
    ])


def main() -> None:
    args = sys.argv[1:]

    # Dispatch: are we being invoked as the hidden background worker, or
    # as the normal user-facing CLI? We check this first, before any
    # normal argument parsing, since the worker's argv shape is different
    # (WORKER_FLAG, a deadline timestamp, a label) from the CLI's.
    if args and args[0] == WORKER_FLAG:
        _, deadline_text, label = args
        run_worker(float(deadline_text), label)
        return

    if not args:
        list_timers()
        return

    duration_text, *label_words = args
    try:
        start_timer(duration_text, label_words)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
