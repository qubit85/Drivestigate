"""macOS drive ejection, scoped to an investigation's verified external volume."""

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path


def source_mount(root):
    mount = Path(root).resolve(strict=True)
    while not os.path.ismount(mount) and mount.parent != mount:
        mount = mount.parent
    if mount == Path("/"):
        raise ValueError("This source is on the system disk, which cannot be ejected.")
    return mount


def eject_drive(engine, scan_id):
    # Hold the scheduler lock through unmount: no new reader can start between
    # checking for an active worker and asking macOS to eject the physical drive.
    with engine.lock:
        if sys.platform != "darwin":
            raise ValueError("Drive ejection is available on macOS.")
        scan = engine.store.get_scan(scan_id)
        if engine.active_id:
            raise ValueError(
                "Pause the running investigation and wait for it to finish pausing before ejecting a drive."
            )
        if not engine.connected(scan):
            raise ValueError("The original drive is disconnected or its identity has changed.")
        mount = source_mount(scan["root"])
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(mount)], capture_output=True, timeout=10
        )
        if result.returncode:
            raise ValueError("macOS could not identify this drive. Nothing was ejected.")
        try:
            info = plistlib.loads(result.stdout)
        except (plistlib.InvalidFileException, ValueError) as exc:
            raise ValueError("macOS returned invalid drive information. Nothing was ejected.") from exc
        if not isinstance(info, dict):
            raise ValueError("macOS returned invalid drive information. Nothing was ejected.")
        device = info.get("DeviceIdentifier", "")
        if (
            info.get("Internal") is not False
            or info.get("Ejectable") is not True
            or info.get("MountPoint") != str(mount)
            or not isinstance(device, str)
            or not re.fullmatch(r"disk\d+(?:s\d+)*", device)
        ):
            raise ValueError("Only an ejectable external source drive can be ejected here.")
        if not engine.connected(scan):
            raise ValueError("The source drive changed. Nothing was ejected.")
        result = subprocess.run(["/usr/sbin/diskutil", "eject", device], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError(
                "macOS could not eject the drive. Close files and other apps using it, then try again."
            )
        engine.kick()
        return {"ejected": True, "volume": str(info.get("VolumeName") or mount.name)}
