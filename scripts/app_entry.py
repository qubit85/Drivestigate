"""Entry point for the self-contained desktop bundle and its read workers."""

import sys

if len(sys.argv) > 1 and sys.argv[1] == "--extract-worker":
    from drivestigate.extract import main
else:
    from drivestigate.server import main

main()
