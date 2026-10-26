# Drivestigate 1.3.0 validation

Validated on 2026-10-26 on an Apple Silicon Mac, using Python 3.12 and the locally
installed model `qwen35-9b-alltag-64k:latest` through the local Ollama MLX runner.
Only synthetic sources were used for acceptance testing.

## Automated checks

- **74 tests passed**, including the existing inventory, source integrity, budget,
  disconnect, extraction, model validation and local-session security checks.
- New checks cover persistent queue order, stale/repeated ID rejection, enqueueing
  while busy, automatic continuation, disconnected-job bypass and restart recovery.
- Detail checks cover every inventoried file including technical binaries, full SHA-256,
  duplicate groups, per-file AI and structural-only mode, parser errors, AI failure/retry,
  pause/resume without duplicate completed work, and unchanged source bytes/mtime.
- Graph provenance, search escaping, file caps, folder scoping, tree pagination,
  v1 catalog migration, rough phase ETA behavior, scoped API access and ZIP exports pass.
- Obsidian wikilinks resolve to generated notes, unsafe labels remain literal, original
  files are absent, excerpts are omitted by default, and rebuilding preserves older vaults.
- Two local ports retain independent sessions in one browser cookie jar.
- HTML/CSV/JSON detail reports include the file coverage ledger and known file links.
- Ruff lint/format, Node 22 syntax checks for all browser scripts and `pip check` passed.
- CI defines Linux/macOS checks on Python 3.11, 3.12 and 3.14, plus wheel/source builds.

## Folder summary checks

- Plain-text descriptions cover every cataloged directory, including roots, empty folders,
  unreviewed parents, sampled folders and all-file per-file assessment results.
- Tests verify literal wildcard/case-sensitive subtree boundaries, per-investigation ID
  isolation, metadata-only labeling, sampled-content scope, skipped/unreadable paths,
  authenticated API access, and unchanged source-read/model-call counters while offline.
- Browser checks verify keyboard-focus tooltips, viewport containment, Escape dismissal,
  full-summary dialogs, metadata-only descriptions, copy controls and selected-folder text.
  Automatic scrolling when keyboard focus moves does not cancel the tooltip.
- Source and model strings are inserted as plain text; no source/model HTML is executed.
- The packaged 1.3.0 app serves folder summaries from the existing catalog. All four
  saved investigations survived the update; the active all-file investigation was
  paused at a checkpoint, backed up and resumed after the rebuild.
- The tooltip screenshot in `docs/screenshots/folder-summary.png` uses synthetic data.

## Consolidated-report and map regression checks

- Completed runs save a consolidated report automatically. Tests cover local AI synthesis,
  fallback when inference fails, strict next-step/reference validation, unsupported numeric-claim rejection, bounded reading
  lists, low-priority clutter filtering, complete byte/file totals across grouped areas,
  portable HTML, escaped untrusted text and authenticated report routes.
- Regenerating a report while its source is offline preserves the scan's original phase,
  status, investigation-call count and source-read count. No inventory or content reader
  is invoked. Existing queued/running jobs cannot be replaced by a report request.
- Browser acceptance uses a synthetic 27-file, 26-folder tree with strongly skewed sizes
  and many tiny entries. All 24 map entries remain accessible; rendered tiles stay within
  the map bounds, with no horizontal page overflow.
- The direct **Start all-file investigation** action prepares a new form with the same
  source and the All files radio selected. Both scan choices and the scope explanation
  are visible in the form. Individual finding cards are collapsed by default.
- The local Qwen model completed the sampled fixture and automatically produced a saved
  narrative report and visual breakdowns. Numeric charts use catalog values; semantic
  conclusions remain limited to supplied saved assessments.

## Local-model and browser acceptance

- The real local Qwen model completed a three-file all-file investigation in roughly
  40 seconds: three fingerprints, three assessments, three content excerpts and one
  identical-byte group. No cloud inference or personal source-drive scan was used.
- The compatibility path handles the MLX runner's HTTP 501 for structured output and
  validates the resulting JSON, topic bounds and referenced IDs in the application.
- Browser checks exercised queue hold, adding jobs while held, arrow reordering and
  drag/drop reordering. The queue visibly reflected the resulting order.
- Folder map drilling, per-file evidence, full hashes, English assessments, topic and
  duplicate links, graph inspection, vault controls and accessible graph navigation
  were checked in the running application. Screenshots contain synthetic sources only.
- The self-contained app completed a separate three-file structural detail pass: all
  three files were hashed and parsed, one duplicate group was detected, and a 4,973-byte
  vault ZIP contained human-readable file notes and the correct instance-port links.
- The versioned, ad-hoc signed Mac bundle passed `codesign --verify --deep --strict`.
  The build guard rejected rebuilding while the packaged app was running.
- The app runs on port 8741 with the existing Application Support catalog. The earlier
  1.2.0 migration preserved saved investigations and returned an interrupted disconnected
  investigation to the waiting queue with its progress preserved.

## Scale and release boundaries

The existing metadata benchmark created 10,000 synthetic text files across 100 folders
on the internal SSD and finished inventory plus one simulated assessment in 1.7 seconds.
This is not a detail-mode or USB benchmark. Detail mode reads every accessible regular
file completely for hashing and can take substantially longer, especially with one AI
call per file. ETA ranges use recent unit durations, exclude later phases and are not
confidence intervals. Inventory discovery has no reliable total or ETA initially.

The app graph displays at most 400 matching files; the exported vault includes all
inventoried files. AI topic links are suggestions. Full-byte hashes do not establish
full semantic understanding. Bounded readers, unsupported/encrypted formats, exclusions,
read errors and skipped filesystem entries remain explicit coverage limits.

A physical drive unplug/replug cycle, failing hardware and notarized distribution have
not been acceptance tested. Simulated disconnect/replacement and catalog persistence
are covered. The macOS bundle is ad-hoc signed; public distribution requires Apple
Developer signing and notarization. This is a tested local release, not forensic certification.
