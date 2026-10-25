# Drivestigate

**Investigate your drives. Rediscover what matters.**

A local macOS application for exploring old hard drives and USB sticks with Ollama.
It builds a resumable metadata catalog, groups technical clutter, samples potentially
valuable folders, and produces an evidence-based report with Finder links.

![Drivestigate overview with synthetic demo data](docs/screenshots/overview.png)

## Start on macOS

Requirements: Python 3.11 or later, Ollama running locally, and an installed local
text/completion model. A vision-capable model is needed only for optional image sampling.

```sh
git clone https://github.com/qubit85/Drivestigate.git
cd Drivestigate
bash scripts/setup.sh
./Start\ Drivestigate.command
```

You can also double-click **Start Drivestigate.command** in Finder. The first launch
installs PDF/image support into a project-local virtual environment; this installation
requires an internet connection. Runtime processing does not require internet access.
Open [127.0.0.1:8741](http://127.0.0.1:8741) if the browser does not open automatically.
Keep the terminal running; Ctrl+C stops the application. Your progress is preserved.

The default model name is `qwen35-9b-alltag-64k:latest`; select your own installed model
in **Settings**. The model name is a configuration default, not a required download.
The Ollama MLX runner is supported even when native structured output is unavailable:
Drivestigate validates the model's JSON and all referenced file IDs itself.

### Self-contained macOS application

The build script creates `dist/Drivestigate.app` with Python, PDF/image readers, and the
web interface bundled. It only needs your local Ollama service at runtime:

```sh
.venv/bin/python -m pip install -c constraints.txt pyinstaller
bash scripts/build_macos.sh
```

The desktop bundle is built for the architecture of the build Mac. Local builds are
ad-hoc signed; public distribution still requires your Apple Developer identity and
notarization. Closing the browser does not stop the background desktop app; use Quit
from its Dock icon. The source launcher remains useful for development and terminal logs.

## Workflow

1. Open **Settings**. Select a local model and test the connection. Edit the system
   prompt, add search priorities, save reusable profiles, and choose sampling budgets.
2. Start **New investigation**. Choose a mounted drive or a folder, or generate a synthetic demo.
3. Inventory enumerates every accessible directory entry and collects metadata. Excluded
   paths, symlinks, other mounted filesystems, permission errors and unreadable filenames
   are logged. Files are not opened for their contents during inventory.
4. Relevant folders receive visible metadata findings before AI analysis starts. The
   investigator prioritizes filenames, types and your search focus, then samples different
   extensions, ages, sizes and a deterministic random slice. Qwen can request a second round.
5. Read the report. Folder names open Finder; sample filenames reveal the file in Finder.
   **Copy path** also works while a drive is disconnected. **Investigate further**
   requests additional samples from one folder. **Resume** investigates remaining folders.
6. Export HTML, CSV or JSON. HTML links return to that finding in the running local app,
   where Finder actions are available. Standalone exported HTML does not directly launch Finder.

Only one scan runs at a time. Pause/stop take effect at the next checkpoint; an in-flight
model request can take up to the configured request timeout. Restarted scans are paused
and can be resumed. Completed scans with unreviewed folders can also be continued.
Prompts, filters, model selection and budgets are snapshotted per scan. To use different
settings on existing material, create a new scan. Budgets renew on each explicit resume
or deepen action. Metadata inventory is not limited by the AI time budget.

## What is actually inspected?

| Type | Sampling behavior |
| --- | --- |
| Text, code, CSV, JSON, email | Up to 16 KB read; at most 6,000 characters retained per sample |
| PDF | Files up to 2 MiB, first three pages; requires pypdf; scanned/encrypted PDFs remain metadata |
| DOCX, XLSX, PPTX | Files up to 2 MiB; selected XML text only, no macros or external links executed |
| ZIP | Files up to 2 MiB; up to 80 entry names, never extract archive members to disk |
| Images | Files up to 2 MiB; dimensions by default, optional 512 px thumbnail sent to local vision model |
| Video, audio, legacy Office, RAR/7z, disk images, databases | Metadata only; reported as uninspected content |

Only excerpts are sent to the model, with additional per-request truncation to keep the
context manageable. Technical classification uses conservative path/extension rules;
potential user documents inside application folders remain visible. It is still a heuristic.
No OCR, recursive archive extraction, malware scan, forensic recovery, deduplication, or
proof that all important files have been found is implied. Empty folders have inventory
entries but no content finding. A grouped technical file is **not** a deletion recommendation.

## Local data and safety

- The application listens only on `127.0.0.1`. Sessions use an HttpOnly SameSite cookie,
  a per-run CSRF token, Host/Origin checks, CSP and no third-party browser assets.
- Only a loopback Ollama URL is accepted. Cloud model entries are filtered and remote
  model metadata is rejected. No source files are uploaded by Drivestigate. For defense
  in depth, Ollama supports `OLLAMA_NO_CLOUD=1` on the **Ollama service**, followed by a
  service restart; see the [official Ollama FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).
- Source files are opened read-only. Symlinks and special files are not followed; Finder
  accepts catalog IDs rather than arbitrary user-supplied paths. Extraction runs in a
  separate process with time and memory/CPU limits where supported. This is a resource
  boundary, not an OS sandbox for parser vulnerabilities. Keep parser dependencies updated.
- No source scripts, executables, macros or file-embedded commands are executed. Model
  output is validated and rendered as text. The model cannot run tools or mutate files.
- The OS can update access times or its own volume metadata when reading a mounted drive.
  For a forensic write guarantee, use a read-only mount or hardware write blocker.
- Catalogs contain filenames, model assessments, prompts and short sample excerpts. They
  are private local data, not encrypted at rest by this application. Protect them as you
  would protect the scanned files. Exports can contain the same sensitive information.
- The original volume is recognized using root-directory identity and, on macOS, its volume
  UUID if the device number changes. Keep its mount path stable. To scan a relocated or
  modified source afresh, create a new scan. Catalogs are snapshots, not live synchronization.

Default catalog location on macOS: `~/Library/Application Support/Drivestigate/`.
Use `--data-dir /path/outside/the/source` or `DRIVESTIGATE_DATA_DIR` to change it.
The source cannot contain the catalog directory. Back up the catalog only while the
application is stopped (SQLite uses WAL). Nothing under `.data/` is committed to Git.
Synthetic demo folders are created in the system temporary directory and can be removed
when no longer needed; saved reports remain in the catalog.

```sh
.venv/bin/python -m drivestigate.server --no-browser --port 8741 --data-dir .data
```

## Troubleshooting

- **Ollama unavailable:** start Ollama, verify its address, then refresh the model list.
- **Model missing:** select an installed local completion model; Drivestigate does not pull models.
- **Model errors:** check the report's events. After three consecutive failed calls, the scan
  pauses. Decrease sampling/prompt size or use a more capable model in a new scan.
- **Permission denied:** macOS may require Files and Folders access for the terminal/Python
  host. Grant only the access needed for your chosen drive; the app does not change permissions.
- **Disconnected:** reconnect the same volume at the same mount path, then resume.
- **Port already used:** stop the existing instance or choose another `--port`.
- **No content samples:** inspect each sample's status; large/unsupported/encrypted files
  may intentionally receive only a metadata assessment.

## Development

The core server, inventory and catalog use Python's standard library. The browser UI is
plain JavaScript/CSS; no frontend build or external CDN is needed. Optional readers are
installed by the normal setup script. macOS Finder integration is the supported desktop
target; core scanning also runs on Linux with manual paths and copied links.

```sh
.venv/bin/python -m pip install -c constraints.txt '.[documents,dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
node --check drivestigate/static/app.js
```

Tests use synthetic data and an injected model client. They cover persistence, pause/resume,
budgets, unchanged source content/mtime, unsafe paths and symlinks, disconnected drives,
model-output validation, exports, CSRF and session boundaries. GitHub Actions defines
Linux/macOS checks for Python 3.11, 3.12 and 3.14. Live-model and UI acceptance checks are
documented in [docs/VALIDATION.md](docs/VALIDATION.md).
