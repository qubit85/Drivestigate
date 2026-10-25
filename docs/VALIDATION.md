# Drivestigate 1.0.0 validation

Validated on 2026-10-25 on an Apple Silicon Mac, using Python 3.12 and the locally
installed Ollama model `qwen35-9b-alltag-64k:latest` (MLX runner).

## Automated checks

- **36 tests passed**: inventory and source integrity; budgets and unreviewed-folder
  visibility; pause/resume and restart recovery; disconnected/replaced sources;
  symlink/path escape protection; invalid filenames; Office compression limits;
  model schema and file-reference validation; cloud-model rejection; MLX compatibility;
  snapshot exports, escaping and CSV formula protection; session/CSRF/Host/Origin
  boundaries; and final report-counter refresh.
- Ruff lint and formatting checks passed.
- JavaScript syntax check passed on Node 22.
- `pip check` passed.
- Wheel and source distribution built; wheel includes all four frontend assets.

## Live local-model acceptance

Only synthetic data was used. No personal source drive was scanned.

- Browser-created investigation inventoried 13 files in 15 folders, grouped three
  technical files, and recorded one excluded trash directory.
- Qwen returned English assessments linked to known file IDs. A budget of two folders
  produced two AI-reviewed folders and left the other five visibly unreviewed.
- The installed MLX runner returned HTTP 501 for native JSON-schema output. The
  compatibility path successfully generated JSON without grammar constraints, with
  identical application-side schema and reference validation.
- Model discovery displayed six installed local models. Connection test, prompt/focus
  editing and settings persistence were exercised through the browser.
- Clicking the synthetic Family finding opened its matching folder in Finder; the
  Finder accessibility state showed the two expected synthetic files.
- The English desktop layout and compact responsive layout were inspected. Screenshots
  in `screenshots/` contain only synthetic demo paths and public model names.

## Packaged application

- Built `dist/Drivestigate.app` for Apple Silicon, approximately 31 MiB.
- Executed its bundled reader directly: a synthetic Markdown file returned the expected
  excerpt without relying on the development Python installation.
- Started the app on a separate loopback port with an isolated catalog. A complete
  synthetic scan cataloged 13 files, assessed one folder within the one-call budget,
  and produced nonempty HTML, CSV and JSON exports (5,899 / 1,097 / 5,592 bytes in that run).
- The final app was also started on port 8741 using its normal Application Support catalog.

## Scale check

`scripts/scale_check.py` created 10,000 synthetic text files across 100 folders on the
internal SSD. Inventory plus one simulated assessment finished in **1.7 seconds**,
read three content samples, and retained 99 unreviewed folders in the report.
This is a metadata scalability check, not a USB-drive or live-inference benchmark.

## Release boundaries

- macOS desktop integration is the supported user-facing target. The CI workflow also
  defines core checks on Linux and Python 3.11/3.12/3.14; remote results must be checked
  in GitHub Actions after publication.
- A physical removable drive, failing hardware, and a real unplug/replug cycle were not
  available for acceptance testing. Simulated disconnect/replacement and persistence
  behavior are covered by tests. Treat this as a tested local release, not a forensic certification.
- PDF/Office/ZIP/image sampling has explicit size and format limits listed in the README.
  OCR, legacy/binary-format content inspection and exhaustive recovery are outside this release.
- The desktop bundle is locally ad-hoc signed. Public distribution requires Apple
  Developer signing and notarization; neither is performed automatically.
- LLM judgments remain probabilistic. Sampling and displayed evidence do not guarantee
  that every important file is found or that a file can be deleted.
