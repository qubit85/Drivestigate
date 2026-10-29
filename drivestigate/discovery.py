"""A persistent, demand-driven directory cursor in a disposable read worker."""

import fnmatch
import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from .source import source_directory


class DirectoryCursor:
    def __init__(self, root, rel, device, patterns):
        command = (
            [sys.executable, "--discover-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "drivestigate.discovery"]
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.arguments = {"root": root, "rel": rel, "device": device, "patterns": patterns}
        self.started = False

    def next(self, alive=lambda: True, timeout=8):
        if not alive():
            raise InterruptedError("Directory request cancelled.")
        request = self.arguments if not self.started else {"next": True}
        self.started = True
        self.process.stdin.write(json.dumps(request).encode() + b"\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if not alive():
                self.close()
                raise InterruptedError("Directory request cancelled.")
            if time.monotonic() >= deadline:
                self.close()
                raise TimeoutError(
                    "Directory listing timed out; this branch can be retried in a later round."
                )
            if self.selector.select(0.1):
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise OSError("Directory worker stopped before returning its batch.")
                self.buffer += chunk
                if len(self.buffer) > 1024 * 1024:
                    self.close()
                    raise ValueError("Directory worker exceeded its output budget.")
        line, self.buffer = self.buffer.split(b"\n", 1)
        result = json.loads(line)
        if result.get("error"):
            raise OSError(result["error"])
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()


def batches(args):
    with source_directory(args["root"], args["rel"], args["device"]) as folder:
        with os.scandir(folder) as entries:
            while True:
                rows, events, complete = [], [], False
                begin = time.monotonic()
                for _ in range(96):
                    try:
                        entry = next(entries)
                    except StopIteration:
                        complete = True
                        break
                    child = (PurePosixPath(args["rel"]) / entry.name).as_posix()
                    try:
                        child.encode("utf-8")
                        if any(
                            fnmatch.fnmatchcase(entry.name.casefold(), p.casefold())
                            or fnmatch.fnmatchcase(child.casefold(), p.casefold())
                            for p in args["patterns"]
                        ):
                            events.append(["excluded", child, "Skipped by an explicit exclusion rule."])
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            events.append(["symlink", child, "Symbolic link not followed."])
                        elif info.st_dev != args["device"]:
                            events.append(["mount", child, "Different filesystem not traversed."])
                        elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                            rows.append(
                                {
                                    "rel": child,
                                    "name": entry.name,
                                    "directory": stat.S_ISDIR(info.st_mode),
                                    "size": info.st_size,
                                    "mtime": info.st_mtime,
                                }
                            )
                        else:
                            events.append(["special", child, "Special file not read."])
                    except (OSError, UnicodeError) as exc:
                        events.append(
                            [
                                "unreadable",
                                child.encode("utf-8", errors="backslashreplace").decode(),
                                str(exc)[:300],
                            ]
                        )
                    if time.monotonic() - begin > 0.15:
                        break
                yield {"entries": rows, "events": events, "complete": complete}
                if complete:
                    return


def main():
    try:
        args = json.loads(sys.stdin.readline(64000))
        for batch in batches(args):
            print(json.dumps(batch), flush=True)
            if batch["complete"] or not sys.stdin.readline(100):
                break
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:500]}), flush=True)


if __name__ == "__main__":
    main()
