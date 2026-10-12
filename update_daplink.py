#!/usr/bin/env python3
"""Batch-update DAPLink interface firmware on BBC micro:bit boards.

Plug each board in while holding the reset button, and release it once the
MAINTENANCE drive shows up. For every board that appears the script will:

  1. read DETAILS.TXT (HIC / board ID, bootloader and interface version)
  2. copy the matching interface firmware from ./fw onto MAINTENANCE
     (skipped if the board already runs that version, unless --force)
  3. wait for the board to reboot into the MICROBIT drive and check the
     interface version in its DETAILS.TXT
  4. notify you; the board can then be unplugged

No AUTO_ON / START_BL / AUTO_OFF round trip, because on the micro:bit:
  - flashing the interface resets the interface settings to the defaults
    (automation off), and DAPLink ignores MSD commands unless automation is
    on or the reset button is held, so START_BL.ACT can't bring a freshly
    flashed board back to MAINTENANCE;
  - in MAINTENANCE any command (e.g. AUTO_OFF.CFG) resets the board straight
    into interface mode, and the bootloader's "Automation allowed" setting
    does not stick anyway.

Boards are found through /dev/disk/by-id (usb-MBED_VFS_<Unique ID>) and are
tracked by the chip-specific part of their Unique ID, which is the same in
bootloader and interface mode. If the desktop does not mount a board within
a couple of seconds, the script mounts it with udisksctl. Several boards can
be plugged in at the same time.

The newest hex per interface chip in ./fw is used, so dropping a newer
release in there is enough to change the target version.
"""

import argparse
import csv
import datetime
import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BY_ID = Path("/dev/disk/by-id")

# DAPLink HIC ID -> interface chip (used first)
HIC_CHIPS = {
    "97969901": "kl26z",
    "9796990b": "kl27z",
    "6e052820": "nrf52820",
}

# Board ID (first 4 hex digits of the Unique ID) -> interface chip (fallback)
BOARD_CHIPS = {
    "9900": "kl26z",     # V1.0 - V1.3
    "9901": "kl26z",     # V1.5
    "9903": "kl27z",     # V2.00
    "9904": "kl27z",     # V2.00
    "9905": "nrf52820",  # V2.20
    "9906": "nrf52820",  # V2.21
}

POLL = 0.25
MOUNT_GRACE = 2.0     # give the desktop automounter this long first
FLASH_TIMEOUT = 120   # interface flashing + reboot + mount

print_lock = threading.Lock()
verbose = False


def log(tag, msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with print_lock:
        print(f"{stamp} [{tag or '--'}] {msg}", flush=True)


def debug(tag, msg):
    if verbose:
        log(tag, msg)


# --------------------------------------------------------------------------
# Board discovery
# --------------------------------------------------------------------------

def board_key(uid):
    """Part of the Unique ID that is the same in bootloader and interface mode.

    Bootloader: 9905 0000 00052820d41ac56c...
    Interface:  9906 3602 00052820d41ac56c...
    """
    return uid[8:] if len(uid) > 16 else uid


def parse_details(text):
    info = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def _unescape(path):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def mount_table():
    """{device path: (mount_id, mountpoint)} for vfat mounts."""
    mounts = {}
    try:
        with open("/proc/self/mountinfo") as f:
            lines = f.readlines()
    except OSError:
        return mounts
    for line in lines:
        pre, _, post = line.partition(" - ")
        fields, post = pre.split(), post.split()
        if len(post) >= 2 and post[0] == "vfat":
            mounts[post[1]] = (int(fields[0]), Path(_unescape(fields[4])))
    return mounts


def usb_devices():
    """{device path: (USB serial, link id)} for every DAPLink drive.

    udev recreates the by-id link whenever the board re-enumerates, so the
    link's inode/ctime identifies one connection of the board.
    """
    devs = {}
    try:
        links = list(BY_ID.glob("usb-MBED_VFS_*-0:0"))
    except OSError:
        return devs
    for link in links:
        serial = link.name[len("usb-MBED_VFS_"):-len("-0:0")]
        try:
            st = link.lstat()
            devs[os.path.realpath(link)] = (serial, (st.st_ino, st.st_ctime_ns))
        except OSError:
            pass
    return devs


def scan():
    """All DAPLink boards currently connected.

    Returns a list of dicts with uid/key/dev and, once mounted and readable,
    mount_id/path/mode/info (otherwise those are None).
    """
    boards = []
    mounts = mount_table()
    for dev, (serial, link_id) in usb_devices().items():
        b = {"dev": dev, "uid": serial, "key": board_key(serial),
             "link_id": link_id,
             "mount_id": None, "path": None, "mode": None, "info": None}
        if dev in mounts:
            mount_id, path = mounts[dev]
            try:
                info = parse_details((path / "DETAILS.TXT")
                                     .read_text(errors="replace"))
            except OSError:
                info = None
            # a stale mount from before a reset can still serve cached data
            if info and info.get("Unique ID", "").lower() == serial.lower():
                b.update(mount_id=mount_id, path=path, info=info,
                         mode=info.get("Daplink Mode", ""))
        boards.append(b)
    return boards


class Mounter:
    """Mount boards the desktop has not mounted after MOUNT_GRACE seconds."""

    def __init__(self):
        self.first_seen = {}
        self.attempted = set()

    def run_once(self, boards):
        now = time.monotonic()
        mounted = set(mount_table())
        devs = {b["dev"] for b in boards}
        for dev in list(self.first_seen):
            if dev not in devs or dev in mounted:
                del self.first_seen[dev]
                self.attempted.discard(dev)
        for dev in devs - mounted:
            t = self.first_seen.setdefault(dev, now)
            if now - t > MOUNT_GRACE and dev not in self.attempted:
                self.attempted.add(dev)
                r = subprocess.run(["udisksctl", "mount", "--no-user-interaction",
                                    "-b", dev], capture_output=True, text=True)
                debug("", f"udisksctl mount {dev}: "
                          f"{(r.stdout or r.stderr).strip()}")


def wait_reconnect(tag, old, timeout):
    """Wait until the board has reset (dropped off USB) and is mounted again.

    Mount IDs get reused by the kernel, so a reset is detected by the board
    disappearing, or by its by-id link having been recreated.
    """
    deadline = time.monotonic() + timeout
    reset = False
    while time.monotonic() < deadline:
        b = next((b for b in scan() if b["key"] == old["key"]), None)
        if not reset and (b is None or (b["uid"], b["link_id"]) !=
                          (old["uid"], old["link_id"])):
            debug(tag, "board reset")
            reset = True
        if reset and b is not None and b["mount_id"] is not None:
            debug(tag, f"back as {b['mode']} on {b['path']}")
            return b
        time.sleep(POLL)
    return None


# --------------------------------------------------------------------------
# Board operations
# --------------------------------------------------------------------------

class UpdateError(Exception):
    pass


def copy_to_drive(tag, src, drive):
    dst = drive["path"] / src.name
    try:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 64 * 1024)
            fo.flush()
            os.fsync(fo.fileno())
    except OSError as e:
        # The board resets as soon as it has the image; the kernel may still
        # be busy with the file at that point.
        debug(tag, f"copy: {e.strerror} (usually harmless, checking result)")


def board_chip(info):
    hic = info.get("HIC ID", "").lower()
    board_id = info.get("Unique ID", "")[:4]
    chip = HIC_CHIPS.get(hic) or BOARD_CHIPS.get(board_id)
    if chip is None:
        raise UpdateError(f"unknown board (HIC ID {hic}, board ID {board_id})")
    return chip


def select_firmware(fw_dir, chip):
    candidates = []
    for p in fw_dir.glob(f"*_{chip}_*.hex"):
        m = re.match(r"(\d{4})", p.name)
        if m:
            candidates.append((m.group(1), p))
    if not candidates:
        raise UpdateError(f"no firmware for {chip} in {fw_dir}")
    return max(candidates)


def update_board(tag, drive, args, result):
    info = drive["info"]
    chip = board_chip(info)
    target, fw = select_firmware(args.fw_dir, chip)
    result.update(board_id=info["Unique ID"][:4], chip=chip, target=target,
                  bl_before=info.get("Bootloader Version", ""),
                  if_before=info.get("Interface Version", ""))
    log(tag, f"{chip}, bootloader {result['bl_before']}, interface "
             f"{result['if_before']} -> target {target}")

    if info.get("Interface Version") == target and not args.force:
        log(tag, f"interface already {target}, nothing to flash")
        final = info
    else:
        log(tag, f"flashing {fw.name}")
        copy_to_drive(tag, fw, drive)
        new = wait_reconnect(tag, drive, FLASH_TIMEOUT)
        if new is None:
            raise UpdateError(f"board not back within {FLASH_TIMEOUT}s after "
                              "flashing")
        if new["mode"] != "Interface":
            try:
                reason = (new["path"] / "FAIL.TXT").read_text(
                    errors="replace").strip()
            except OSError:
                reason = (f"came back in {new['mode']} mode - was the button "
                          "still held?")
            raise UpdateError(f"flash failed: {reason}")
        final = new["info"]

    result.update(mode_after=final.get("Daplink Mode", ""),
                  bl_after=final.get("Bootloader Version", ""),
                  if_after=final.get("Interface Version", ""),
                  if_crc=final.get("Interface CRC", ""))
    if result["if_after"] != target:
        raise UpdateError(f"verification failed: interface "
                          f"{result['if_after']} != {target}")


# --------------------------------------------------------------------------
# Notification / logging
# --------------------------------------------------------------------------

def notify(ok, title, body):
    sys.stdout.write("\a")
    sys.stdout.flush()
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "normal" if ok else "critical",
                        "-a", "micro:bit updater", title, body],
                       stderr=subprocess.DEVNULL)
    sound = Path("/usr/share/sounds/freedesktop/stereo/" +
                 ("complete.oga" if ok else "dialog-error.oga"))
    if shutil.which("paplay") and sound.exists():
        subprocess.Popen(["paplay", str(sound)], stderr=subprocess.DEVNULL)


LOG_FIELDS = ["time", "board", "uid", "board_id", "chip", "bl_before",
              "if_before", "target", "mode_after", "bl_after", "if_after",
              "if_crc", "status", "message"]
csv_lock = threading.Lock()


def append_csv(path, row):
    with csv_lock:
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

class Supervisor:
    def __init__(self, args):
        self.args = args
        self.numbers = itertools.count(1)
        self.active = set()     # keys currently being processed
        self.finished = set()   # keys done, until the board is unplugged
        self.counts = {"ok": 0, "failed": 0}
        self.lock = threading.Lock()

    def worker(self, tag, drive):
        result = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "board": tag, "uid": drive["uid"]}
        t0 = time.monotonic()
        try:
            update_board(tag, drive, self.args, result)
            ok, msg = True, f"done in {time.monotonic() - t0:.0f}s"
        except UpdateError as e:
            ok, msg = False, str(e)
        except Exception as e:  # keep the supervisor alive no matter what
            ok, msg = False, f"unexpected error: {e!r}"
        result.update(status="OK" if ok else "FAILED", message=msg)
        append_csv(self.args.log, result)
        with self.lock:
            self.active.discard(drive["key"])
            self.finished.add(drive["key"])
            self.counts["ok" if ok else "failed"] += 1
            total = f"[ok {self.counts['ok']} / failed {self.counts['failed']}]"
        summary = (f"board {tag}: interface {result.get('if_before', '?')} -> "
                   f"{result.get('if_after', '?')}")
        if ok:
            log(tag, f"SUCCESS: {summary} ({msg}) - unplug it   {total}")
            notify(True, f"micro:bit {tag} updated - unplug", summary)
        else:
            log(tag, f"FAILED: {msg} - unplug and retry   {total}")
            notify(False, f"micro:bit {tag} FAILED", msg)

    def run(self):
        log("", f"firmware dir: {self.args.fw_dir}")
        for chip in sorted(set(HIC_CHIPS.values())):
            try:
                v, p = select_firmware(self.args.fw_dir, chip)
                log("", f"  {chip:9s} -> {v}  ({p.name})")
            except UpdateError as e:
                log("", f"  {chip:9s} -> {e}")
        log("", "waiting for boards in MAINTENANCE mode (Ctrl+C to quit)...")
        mounter = Mounter()
        seen = {}               # key -> last state, for -v and unplug detection
        reported_if = set()     # interface-mode boards we already warned about
        while True:
            boards = scan()
            mounter.run_once(boards)
            present = {b["key"]: b for b in boards}
            for key, b in present.items():
                state = (b["dev"], b["mode"], str(b["path"]))
                if seen.get(key) != state:
                    debug("", f"{key[8:20]}: {b['dev']} {b['mode'] or 'not mounted'}"
                              f" {b['path'] or ''}")
                    seen[key] = state
            for key in set(seen) - set(present):
                debug("", f"{key[8:20]}: gone")
                del seen[key]
            with self.lock:
                self.finished &= set(present)   # unplugged -> forget
                reported_if &= set(present)
                for key, b in present.items():
                    if key in self.active or key in self.finished or not b["mode"]:
                        continue
                    if b["mode"] != "Bootloader":
                        if key not in reported_if:
                            log("", f"{b['path']} is in interface mode - "
                                    "replug holding the button to update")
                            reported_if.add(key)
                        continue
                    self.active.add(key)
                    tag = f"#{next(self.numbers)}"
                    log(tag, f"new board on {b['path']} (Unique ID {b['uid']})")
                    threading.Thread(target=self.worker, args=(tag, b),
                                     daemon=True).start()
            time.sleep(POLL)


def main():
    global verbose
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fw-dir", type=Path, default=SCRIPT_DIR / "fw",
                    help="directory with interface .hex files (default: ./fw)")
    ap.add_argument("--log", type=Path, default=SCRIPT_DIR / "update_log.csv",
                    help="CSV log of every board processed")
    ap.add_argument("--force", action="store_true",
                    help="flash even if the board already runs the target version")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every board appearing / disappearing / mounting")
    args = ap.parse_args()
    verbose = args.verbose
    if not sys.platform.startswith("linux"):
        sys.exit("this script uses /dev/disk/by-id and /proc and needs Linux")
    try:
        Supervisor(args).run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
