"""Opt-in metadata scalability check using 10,000 disposable synthetic files."""

import json
import tempfile
import time
from pathlib import Path

from drivestigate.config import DEFAULTS
from drivestigate.engine import Engine
from drivestigate.store import Store


class MetadataOnly:
    def __init__(self, settings):
        pass

    def test(self):
        return {"ok": True}

    def analyze(self, evidence, timeout=None):
        return {
            "summary": "Synthetic sample",
            "reason": "Scale check",
            "priority": 2,
            "needs_more_samples": False,
            "next_action": "None",
            "file_ids": [s["id"] for s in evidence["samples"]],
        }


def main():
    with tempfile.TemporaryDirectory(prefix="Drivestigate-Scale-") as temp:
        root = Path(temp) / "source"
        for folder in range(100):
            path = root / f"folder-{folder:03}"
            path.mkdir(parents=True)
            for file in range(100):
                (path / f"note-{file:03}.txt").write_text("Synthetic scale-check content.")
        store = Store(Path(temp) / "catalog/db")
        engine = Engine(store, MetadataOnly)
        started = time.monotonic()
        sid = engine.create(root, "Scale check", {**DEFAULTS, "max_folders": 1, "samples_per_folder": 3})
        engine.thread.join(180)
        if engine.thread.is_alive():
            engine.interrupt(sid)
            raise RuntimeError("Scale check exceeded 180 seconds")
        stats = store.stats(sid)
        assert stats["files"] == 10000, stats
        assert stats["findings"] == 100, stats
        assert stats["analyzed_folders"] == 1, stats
        print(
            json.dumps(
                {
                    "files": stats["files"],
                    "folders": stats["findings"],
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "content_samples": stats["content_samples"],
                    "unreviewed_folders": stats["findings"] - stats["analyzed_folders"],
                }
            )
        )


if __name__ == "__main__":
    main()
