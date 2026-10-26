"""Eject checks simulate diskutil; no test ejects physical hardware."""

import plistlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivestigate import drives


@pytest.fixture
def external(monkeypatch):
    calls = []
    info = {
        "Internal": False,
        "Ejectable": True,
        "MountPoint": "/Volumes/QA",
        "DeviceIdentifier": "disk9s1",
        "VolumeName": "QA",
    }
    engine = SimpleNamespace(
        lock=threading.RLock(),
        active_id=None,
        store=SimpleNamespace(get_scan=lambda sid: {"root": "/Volumes/QA/Archive"}),
        connected=lambda scan: True,
        kick=lambda: calls.append("kick"),
    )
    monkeypatch.setattr(drives.sys, "platform", "darwin")
    monkeypatch.setattr(drives, "source_mount", lambda root: Path("/Volumes/QA"))

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["capture_output"] and kwargs["timeout"] <= 30
        return SimpleNamespace(returncode=0, stdout=plistlib.dumps(info))

    monkeypatch.setattr(drives.subprocess, "run", run)
    return engine, info, calls


def test_eject_uses_only_verified_device_and_continues_queue(external):
    engine, _, calls = external
    assert drives.eject_drive(engine, "scan") == {"ejected": True, "volume": "QA"}
    assert calls == [
        ["/usr/sbin/diskutil", "info", "-plist", "/Volumes/QA"],
        ["/usr/sbin/diskutil", "eject", "disk9s1"],
        "kick",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("Internal", True),
        ("Ejectable", False),
        ("MountPoint", "/"),
        ("DeviceIdentifier", "/dev/disk9; anything"),
        ("DeviceIdentifier", None),
    ],
)
def test_eject_rejects_internal_non_ejectable_or_unverified_devices(external, field, value):
    engine, info, calls = external
    if value is None:
        del info[field]
    else:
        info[field] = value
    with pytest.raises(ValueError, match="Only an ejectable external"):
        drives.eject_drive(engine, "scan")
    assert len(calls) == 1 and "eject" not in calls[0]


def test_eject_requires_idle_worker_and_original_drive(external):
    engine, _, calls = external
    engine.active_id = "other-scan"
    with pytest.raises(ValueError, match="Pause the running"):
        drives.eject_drive(engine, "scan")
    engine.active_id = None
    engine.connected = lambda scan: False
    with pytest.raises(ValueError, match="disconnected"):
        drives.eject_drive(engine, "scan")
    assert not calls


def test_eject_rechecks_identity_before_ejecting(external):
    engine, _, calls = external
    answers = iter([True, False])
    engine.connected = lambda scan: next(answers)
    with pytest.raises(ValueError, match="drive changed"):
        drives.eject_drive(engine, "scan")
    assert len(calls) == 1


def test_eject_errors_do_not_force_unmount(external, monkeypatch):
    engine, _, calls = external
    original = drives.subprocess.run

    def run(args, **kwargs):
        if args[1] == "eject":
            calls.append(args)
            return SimpleNamespace(returncode=1)
        return original(args, **kwargs)

    monkeypatch.setattr(drives.subprocess, "run", run)
    with pytest.raises(ValueError, match="Close files and other apps"):
        drives.eject_drive(engine, "scan")
    assert calls[-1] == ["/usr/sbin/diskutil", "eject", "disk9s1"]


def test_eject_rejects_invalid_metadata_and_unsupported_platform(external, monkeypatch):
    engine, _, _ = external
    monkeypatch.setattr(
        drives.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"invalid")
    )
    with pytest.raises(ValueError, match="invalid drive information"):
        drives.eject_drive(engine, "scan")
    monkeypatch.setattr(drives.sys, "platform", "linux")
    with pytest.raises(ValueError, match="available on macOS"):
        drives.eject_drive(engine, "scan")


def test_source_folder_resolves_to_mount_and_system_disk_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "volume/archive/project"
    source.mkdir(parents=True)
    volume = tmp_path / "volume"
    monkeypatch.setattr(drives.os.path, "ismount", lambda path: path == volume)
    assert drives.source_mount(source) == volume
    monkeypatch.setattr(drives.os.path, "ismount", lambda path: False)
    with pytest.raises(ValueError, match="system disk"):
        drives.source_mount(source)
