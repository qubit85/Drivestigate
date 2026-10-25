"""Synthetic data only; never copy personal files into a demo."""

import json
import os
import zipfile
from pathlib import Path


def create_demo(root):
    root = Path(root)
    files = {
        "Documents/Family/Travel plans 2012.txt": "Family summer trip 2012. Photo album and travel notes.",
        "Documents/Family/Recipes.md": "# Grandma's recipes\nApple cake: apples, flour, butter, cinnamon.",
        "Documents/Finance/Tax documents 2014.csv": "year,description\n2014,Demo tax record (synthetic)",
        "Documents/Finance/contract-notes.txt": "Sample rental contract notes. This is synthetic demo data.",
        "Projects/WeatherStation/README.md": "# Weather station\nAn unfinished personal Raspberry Pi project.",
        "Projects/WeatherStation/sensor.py": "# Personal weather station prototype\ndef read_temperature():\n    return 21.5\n",
        "Projects/WeatherStation/node_modules/example/index.js": "module.exports = {};",
        "Windows/System32/kernel.dll": "Synthetic DLL placeholder, not executable.",
        "Program Files/OldApp/app.exe": "Synthetic program placeholder, not executable.",
        "Program Files/OldApp/User Data/personal-backup.txt": "Personal notes saved inside an application folder.",
        "Mystery/untitled.txt": "Notes from an old unpublished book project. Chapter outline and research.",
        ".Trashes/deleted.txt": "Synthetic trash placeholder.",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.utime(path, (1400000000, 1400000000))
    folder = root / "Backups"
    folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(folder / "project-backup.zip", "w") as archive:
        archive.writestr("journal.txt", "Synthetic project journal.")
        archive.writestr("photos/README.md", "Placeholder for family photographs.")
    (root / "demo-info.json").write_text(json.dumps({"synthetic": True, "app": "Drivestigate"}))
    return root
