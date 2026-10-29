# Drivestigate

**Investigate your drives. Rediscover what matters.**

A local macOS application for exploring arbitrary drives and folders with Ollama.
Discovery and AI analysis run concurrently. The investigator builds an early overview,
requests selective content samples, revises its understanding and updates a single live
report. Folder names, device layouts and operating systems are clues, not assumptions
about what a drive contains.

![Drivestigate overview with synthetic demo data](docs/screenshots/overview.png)

## Start on macOS

Requirements: Python 3.11 or later, a running local Ollama service and an installed
completion model capable of following structured instructions. A vision model is needed
only for optional image sampling.

```sh
git clone https://github.com/qubit85/Drivestigate.git
cd Drivestigate
bash scripts/setup.sh
./Start\ Drivestigate.command
```

You can double-click **Start Drivestigate.command** in Finder. Initial dependency
installation requires internet access; runtime processing is local. Open
[127.0.0.1:8741](http://127.0.0.1:8741) if the browser does not open automatically.
The default model is `qwen35-9b-alltag-64k:latest`; select your installed model in Settings.
The MLX runner's missing native structured-output support is handled with a validated
JSON fallback. No model is downloaded automatically.

For the self-contained Apple Silicon/macOS application:

```sh
.venv/bin/python -m pip install -c constraints.txt pyinstaller
bash scripts/build_macos.sh
```

The bundle is `dist/Drivestigate.app`. Quit it before rebuilding; the build script refuses
to replace a running bundle. Builds are architecture-specific and ad-hoc signed. Public
Mac distribution still requires Developer ID signing and notarization. Closing a browser
tab does not stop the app; use Quit from its Dock icon.

## Progressive investigations

1. Select a model in **Settings** and test its connection. Set your system prompt,
   search focus and time/read/AI-call budgets. The new default round lasts up to five
   minutes, including discovery and analysis. Existing saved custom budgets are retained.
2. Choose **New investigation**, select a mounted drive or folder, and edit its search
   focus directly. Add it to the persistent investigation queue.
3. Discovery lists folders in bounded chunks. A separate AI worker starts as soon as
   evidence is available. It can explore, sample, deepen, defer or summarize a branch,
   and search names and retained excerpts in the already discovered catalog.
4. Read the live report while work continues. It includes an evolving content narrative,
   evidence links, open questions, recent understanding updates, size/type/timeline
   charts and coverage gaps. Numbers come from the catalog, not the model.
5. **Pause**, **Stop** or let the round reach its budget. **Continue deepening** grants
   another bounded round, reusing cached evidence and unfinished work. Completed means
   the round finished, not that every file on the source was discovered or understood.
6. While paused or between rounds, **Change search focus** edits the investigation's
   focus and system prompt. Prior judgments are reassessed against retained evidence;
   completed content reads need not be repeated.

The source has one read lane. AI inference overlaps source access; parser subprocesses
isolate bounded extraction. Work is persisted in SQLite and resumes after restart. The
reader has a bounded lead over inference, and directory chunks yield so a giant folder
cannot monopolize discovery. Scheduling reserves turns for unfamiliar and deferred
branches. Directory decisions and summaries remain hypotheses backed by specific evidence.
No full-byte hashing or knowledge-graph generation runs on the progressive path.

Model replies may request only known, scoped IDs and a fixed set of read-only actions.
Invalid plans receive a bounded correction attempt. If the model remains unavailable,
work stops with the catalog/report preserved; repair the local connection and continue.
Directory listing runs in demand-driven helper processes with an eight-second batch timeout and cancellation; a failed branch remains visible and can be retried on continuation. A source read can still take up to its worker timeout to stop. In-flight AI
requests are capped by the remaining round budget and at most 60 seconds. A timeout
bounds the client wait; an external model server may continue processing a cancelled request.

The app cannot know the total size or duration of unexplored branches. It displays time
left in the **current round**, discovered/listed/assessed counts, active reader and AI work,
and queued/deferred tasks. These are not estimates of exhaustive drive coverage.

## Folder and file views

- **Folder map** shows discovered logical sizes and review states. Unmeasured branches
  use placeholder areas; `≥` means a partial lower bound. Small entries remain accessible
  in the list below the map.
- **Size tree** is an expandable, paged tree of folders and files, sortable by size or name.
  Measured folder totals include descendants. Partial totals use `≥`; unmeasured totals
  show **Unknown**. Exact totals require enumerating the corresponding subtree.
- Hover/focus a folder or select **ⓘ** for its plain-text description, evidence basis,
  uncertainty and coverage. Descriptions use saved evidence and also work offline.
- Evidence links open folders or reveal files in Finder. Copy paths while disconnected.
  Individual finding cards remain available below the comprehensive report.

**Open / save full report** provides a portable HTML document suitable for saving or
printing. Its links return to the running local app. HTML/CSV/JSON evidence exports remain
available. Regenerating a progressive report uses saved knowledge without source reads or
an extra model call. Legacy reports retain their original synthesis behavior.

## Queue, retention and drives

The persistent queue runs one investigation at a time. Drag queued jobs or use arrows
to reorder them. **Hold queue** prevents the next job from starting. Disconnected jobs
wait without blocking connected sources. Interrupted active jobs become paused on restart.
The sidebar shows status and direct Pause/Resume/Stop controls.

**Delete** removes only the selected investigation's local catalog, tasks, findings and
saved report. Active deletion waits for its workers to stop. Source files, downloaded
reports and previously exported Obsidian vaults are kept. Confirmed deletion intent
survives restart.

On macOS, **Eject drive** verifies source identity and disk flags and safely ejects the
external disk containing the source, including other volumes. Pause active work first.
Internal or non-ejectable disks are rejected; ejection is never forced. Other applications
can still prevent macOS from ejecting a busy disk.

## Compatibility with older investigations

Saved sampled and detail investigations remain readable and resumable. They keep their
original processing model; start a new progressive investigation to use the new scheduler.
Legacy detail runs and exported Obsidian vaults are preserved. Their graph is visible only
for detail investigations. New investigations use the progressive workflow.

Legacy detail can group recognized technical files without hashing or reading them when
`detail_include_technical` is false. Existing strict-detail settings retain their original
behavior. Full hashes never imply complete semantic understanding.

## What is actually inspected?

| Type | Bounded sampling behavior |
| --- | --- |
| Text, code, CSV, JSON, email | Up to 16 KB read; at most 6,000 characters retained |
| PDF | Files up to 2 MiB, first three pages; scanned/encrypted documents remain metadata |
| DOCX, XLSX, PPTX | Files up to 2 MiB; selected XML text; no macros or external links executed |
| ZIP | Files up to 2 MiB; up to 80 entry names; no extraction onto the source |
| Images | Files up to 2 MiB; dimensions or optional bounded thumbnails for a vision model |
| Video/audio, legacy Office, RAR/7z, disk images, databases | Metadata only; content remains uninspected |

Model context uses bounded representative metadata and excerpts. Familiar path/extension
rules provide priority hints only in progressive investigations; personal files in unusual
or technical-looking locations remain eligible. Source changes, skipped entries and read
issues are explicit. There is no OCR, forensic recovery, exhaustive discovery guarantee,
automatic duplicate removal or deletion recommendation.

Common password assignments, private-key blocks and token patterns are redacted before
progressive excerpts are cached or sent to the model. This is best-effort pattern filtering,
not a guarantee that arbitrary sensitive text is detected. Reports should describe secret
locations and types, never reproduce their values.

## Local data and safety

- The server binds to `127.0.0.1`. It uses HttpOnly SameSite session cookies, per-run CSRF
  protection, Host/Origin checks, CSP and no third-party browser assets.
- Only loopback model endpoints and locally stored models are accepted. Redirects and
  inherited HTTP proxies are disabled. No external service receives source content.
- The application opens sources read-only, rejects symlinks/special files and validates
  source identity. Model text cannot execute shell commands or modify source files.
- Extraction uses subprocess time and CPU/memory limits where supported. This is resource
  isolation, not an OS security sandbox. Keep parser dependencies updated.
- The OS can update access times or volume metadata when reading a mounted drive. Use a
  read-only mount or hardware write blocker if forensic write protection is required.
- Catalogs contain private paths, prompts, excerpts, optional image thumbnails and model
  assessments. They are not encrypted at rest by the app. Protect catalogs and exports.
- A catalog is a snapshot, not live synchronization. Keep the original mount path stable;
  create a new investigation for a relocated or changed source.

Default data location: `~/Library/Application Support/Drivestigate/`. Override it with
`--data-dir` or `DRIVESTIGATE_DATA_DIR`. The catalog must be outside the source. Back up a
stopped catalog, or use SQLite's backup API to capture a live WAL database consistently.
No investigation data should be committed to Git.

```sh
.venv/bin/python -m drivestigate.server --no-browser --port 8741 --data-dir .data
```

## Development and validation

The server/catalog use Python's standard library, with optional PDF/image readers. The UI
is plain JavaScript/CSS. Finder integration targets macOS; the core also runs on Linux.

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
node --check drivestigate/static/app.js
node --check drivestigate/static/visuals.js
node --check drivestigate/static/sizetree.js
```

Synthetic tests cover concurrent discovery/inference, early reports, persistence, read-only
sampling, model/tool boundaries, pause/resume, refocusing, size coverage, deletion, queue
ordering, session/CSRF protection and legacy compatibility. Live-model and UI checks are
recorded in [docs/VALIDATION.md](docs/VALIDATION.md). GitHub Actions defines Linux/macOS jobs
for Python 3.11, 3.12 and 3.14.
