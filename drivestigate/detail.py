"""All-file investigation: full-byte fingerprints, bounded parsing and explicit coverage."""

import hashlib
import json
import os
import time

from .source import source_file

TERMINAL = ("reviewed", "metadata", "error")


def fingerprint(engine, scan, file):
    digest = hashlib.sha256()
    consumed, updated = 0, time.monotonic()
    base = engine.store.get_scan(scan["id"])["read_bytes"]
    try:
        with source_file(scan["root"], file["rel"], scan["device"]) as (stream, info):
            if info.st_size != file["size"] or abs(info.st_mtime - file["mtime"]) > 0.001:
                raise ValueError("File changed since inventory; contents were not classified.")
            while True:
                engine.checkpoint(scan)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                consumed += len(block)
                if time.monotonic() - updated >= 0.5:
                    engine.progress_update(scan["id"], current_bytes=consumed, current_size=file["size"])
                    engine.store.update(scan["id"], read_bytes=base + consumed)
                    updated = time.monotonic()
            end = os.fstat(stream.fileno())
            if consumed != info.st_size or end.st_size != info.st_size or end.st_mtime_ns != info.st_mtime_ns:
                raise ValueError("File changed during fingerprinting; fingerprint discarded.")
    finally:
        engine.store.update(scan["id"], read_bytes=base + consumed)
    return digest.hexdigest()


def investigate_files(engine, scan):
    store, sid, settings = engine.store, scan["id"], scan["settings"]
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO file_details(file_id) SELECT id FROM files WHERE scan_id=?", (sid,))
        if not settings.get("detail_include_technical", False):
            db.execute(
                "UPDATE file_details SET state='metadata',content_status='grouped',summary=?,reason=?,priority=3 "
                "WHERE state NOT IN ('reviewed','metadata','error') AND file_id IN "
                "(SELECT id FROM files WHERE scan_id=? AND technical=1)",
                (
                    "Grouped technical material; content was not read individually.",
                    "Recognized from directory layout or file metadata. Enable individual technical-file inspection for content reads and full-byte fingerprints.",
                    sid,
                ),
            )
        total = db.execute("SELECT COUNT(*) FROM files WHERE scan_id=?", (sid,)).fetchone()[0]
        completed = db.execute(
            "SELECT COUNT(*) FROM files f JOIN file_details x ON x.file_id=f.id "
            "WHERE f.scan_id=? AND x.state IN ('reviewed','metadata','error')",
            (sid,),
        ).fetchone()[0]
    client = engine.client_factory(settings) if settings["detail_use_llm"] and completed < total else None
    if client:
        info = client.test()
        if settings["analyze_images"] and "vision" not in info.get("capabilities", []):
            raise ValueError("The selected model does not support image analysis.")
    engine.live.setdefault(sid, {}).update(phase_started=time.monotonic(), durations=[], baseline=completed)
    store.update(sid, phase="detail")
    engine.progress_update(
        sid,
        stage="detail",
        completed=completed,
        total=total,
        unit="files",
        current="",
        current_bytes=0,
        current_size=0,
    )
    failures = 0
    while True:
        engine.checkpoint(scan)
        with store.connect() as db:
            row = db.execute(
                "SELECT f.*,x.state,x.sha256 FROM files f JOIN file_details x ON x.file_id=f.id "
                "WHERE f.scan_id=? AND x.state NOT IN ('reviewed','metadata','error') "
                "ORDER BY f.technical,f.score DESC,f.id LIMIT 1",
                (sid,),
            ).fetchone()
        if not row:
            break
        file, started = dict(row), time.monotonic()
        store.update(sid, message=f"Detail inspection: {file['rel']}")
        engine.progress_update(sid, current=file["rel"], current_bytes=0, current_size=file["size"])
        if not file["sha256"]:
            try:
                hashed = fingerprint(engine, scan, file)
                with store.connect() as db:
                    db.execute(
                        "UPDATE file_details SET state='hashed',sha256=? WHERE file_id=?",
                        (hashed, file["id"]),
                    )
            except (ValueError, OSError) as exc:
                with store.connect() as db:
                    db.execute(
                        "UPDATE file_details SET state='error',content_status='error',error=?,summary=? "
                        "WHERE file_id=?",
                        (str(exc)[:500], "Unreadable or changed file.", file["id"]),
                    )
                store.event(sid, "detail", file["rel"], str(exc))
                completed += 1
                engine.progress_update(sid, completed=completed)
                continue
        engine.checkpoint(scan)
        sample = engine.sample(scan, file, 2 * 1024 * 1024)
        current = store.get_scan(sid)
        store.update(sid, read_bytes=current["read_bytes"] + sample.pop("bytes_read", 0))
        image = sample.pop("image", None)
        with store.connect() as db:
            db.execute(
                "UPDATE file_details SET content_status=?,excerpt=?,error=? WHERE file_id=?",
                (
                    sample["status"],
                    sample.get("text", "")[:6000],
                    sample.get("text", "") if sample["status"] == "error" else "",
                    file["id"],
                ),
            )
        summary = sample.get("text", "Metadata inspected.")
        reason = "Structural assessment; AI was disabled for this investigation."
        topics, priority, state = [], 2, "metadata"
        if client:
            engine.checkpoint(scan)
            store.update(sid, llm_calls=store.get_scan(sid)["llm_calls"] + 1)
            try:
                result = client.analyze_file(
                    {
                        "file": file["rel"],
                        "category": file["category"],
                        "coverage": "Every byte fingerprinted. Semantic evidence is bounded excerpts or metadata only.",
                        "samples": [{**sample, **({"image": image} if image else {})}],
                    },
                    timeout=settings["request_timeout_seconds"],
                )
                summary, reason, topics, priority = (
                    result["summary"],
                    result["reason"],
                    result["topics"],
                    result["priority"],
                )
                state, failures = "reviewed", 0
            except ValueError as exc:
                failures += 1
                store.event(sid, "llm", file["rel"], str(exc))
                if failures >= 3:
                    raise ValueError(
                        "Three AI requests failed for a file. Paused for retry; other jobs may continue."
                    ) from exc
                continue
        with store.connect() as db:
            db.execute(
                "UPDATE file_details SET state=?,summary=?,reason=?,topics=?,priority=?,duration=? WHERE file_id=?",
                (
                    state,
                    summary,
                    reason,
                    json.dumps(topics),
                    priority,
                    time.monotonic() - started,
                    file["id"],
                ),
            )
        completed += 1
        engine.unit_finished(sid, time.monotonic() - started)
        engine.progress_update(sid, completed=completed, current_bytes=file["size"])
    # Derive folder coverage from the complete per-file ledger, rather than implying extra folder AI calls.
    with store.connect() as db:
        rows = db.execute(
            "SELECT a.id,COUNT(f.id) count,COALESCE(SUM(x.state='error'),0) errors,"
            "COALESCE(SUM(x.content_status IN ('excerpt','image')),0) content "
            "FROM findings a JOIN files f ON f.dir_id=a.dir_id JOIN file_details x ON x.file_id=f.id "
            "WHERE a.scan_id=? GROUP BY a.id",
            (sid,),
        ).fetchall()
        for r in rows:
            db.execute(
                "UPDATE findings SET analyzed=1,method='detail',summary=?,reason=?,next_action=? WHERE id=?",
                (
                    f"All-file inspection: {r['count']} files processed; {r['errors']} read errors.",
                    f"{r['content']} files supplied readable content excerpts or images; other files have metadata assessments.",
                    "Explore the knowledge graph and per-file coverage.",
                    r["id"],
                ),
            )
    engine.checkpoint(scan)
    from .knowledge import build_vault

    store.update(sid, phase="vault", message="Building the Obsidian vault …")
    vault = build_vault(engine, scan)
    store.update(
        sid,
        vault_path=str(vault),
        phase="complete",
        status="completed",
        message="Every inventoried file is accounted for. Obsidian vault ready; review fingerprints, grouped technical files and content coverage.",
    )
