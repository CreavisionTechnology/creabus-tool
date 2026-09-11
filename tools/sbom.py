# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Generate a CycloneDX software bill of materials for a release.

    python tools/sbom.py                 # writes sbom.cdx.json
    python tools/sbom.py --check         # verify it is current, write nothing

A bill of materials is one of the few Cyber Resilience Act obligations that is
actually a file rather than a process, so it is generated from the real
environment rather than typed out: versions come from the interpreter and the
installed packages, which is the only way it stays true after an upgrade.

What it lists is what ends up *inside a release binary* - the interpreter, Tk,
and the two optional dependencies - not the whole development environment.
PyInstaller is included because its bootloader is embedded in the executable;
the test and lint tooling is not, because none of it ships.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool import branding                                   # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(HERE, "sbom.cdx.json")

# Licences are SPDX identifiers. Anything added here has to be both GPL-3.0
# compatible and usable in a proprietary build - see THIRD-PARTY-NOTICES.md.
COMPONENTS = [
    {"name": "python", "purl": "pkg:generic/python@{version}", "licence": "PSF-2.0",
     "publisher": "Python Software Foundation",
     "description": "The interpreter, embedded in every release binary"},
    {"name": "tcl-tk", "purl": "pkg:generic/tcl-tk@{version}", "licence": "TCL",
     "publisher": "Tcl Core Team",
     "description": "The widget toolkit behind the window; unused by --headless"},
    {"name": "pyyaml", "purl": "pkg:pypi/pyyaml@{version}", "licence": "MIT",
     "publisher": "Ingy dot Net, Kirill Simonov",
     "description": "Reads .yaml device profiles; optional, .json needs nothing"},
    {"name": "pyserial", "purl": "pkg:pypi/pyserial@{version}", "licence": "BSD-3-Clause",
     "publisher": "Chris Liechti",
     "description": "Modbus RTU only; optional, TCP works without it"},
    {"name": "pyinstaller", "purl": "pkg:pypi/pyinstaller@{version}",
     "licence": "GPL-2.0-or-later WITH Bootloader-exception",
     "publisher": "PyInstaller Development Team",
     "description": "Build tool; its bootloader is embedded in the executable"},
]


def installed_version(name: str) -> str:
    """The version actually present, or 'not installed'."""
    if name == "python":
        return platform.python_version()
    if name == "tcl-tk":
        try:
            import tkinter
            return str(tkinter.TkVersion)
        except Exception:
            return "not installed"
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        return "not installed"


def build() -> dict:
    components = []
    for entry in COMPONENTS:
        version = installed_version(entry["name"])
        component = {
            "type": "library",
            "name": entry["name"],
            "version": version,
            "description": entry["description"],
            "publisher": entry["publisher"],
            "licenses": [{"license": {"id": entry["licence"]}}
                         if " WITH " not in entry["licence"] else
                         {"expression": entry["licence"]}],
            "scope": "required" if entry["name"] in ("python", "tcl-tk") else "optional",
        }
        if version != "not installed":
            component["purl"] = entry["purl"].format(version=version)
        components.append(component)

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [{"name": "sbom.py", "vendor": branding.VENDOR}],
            "component": {
                "type": "application",
                "name": branding.APP_NAME,
                "version": branding.VERSION,
                "description": branding.TAGLINE,
                "publisher": branding.VENDOR,
                "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
                "externalReferences": [
                    {"type": "website", "url": branding.VENDOR_URL},
                    {"type": "vcs", "url": branding.PROJECT_URL},
                ],
            },
        },
        "components": components,
    }


def stable(document: dict) -> dict:
    """The document without the fields that change on every run."""
    copy = json.loads(json.dumps(document))
    copy.pop("serialNumber", None)
    copy["metadata"].pop("timestamp", None)
    return copy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if sbom.cdx.json is missing or out of date")
    parser.add_argument("-o", "--output", default=OUTPUT)
    args = parser.parse_args()

    document = build()
    missing = [c["name"] for c in document["components"]
               if c["version"] == "not installed"]

    if args.check:
        if not os.path.exists(args.output):
            print(f"error: {args.output} does not exist - run tools/sbom.py",
                  file=sys.stderr)
            return 1
        with open(args.output, encoding="utf-8") as handle:
            existing = json.load(handle)
        if stable(existing) != stable(document):
            print("error: sbom.cdx.json does not match this environment",
                  file=sys.stderr)
            return 1
        print(f"sbom.cdx.json is current ({len(document['components'])} components)")
        return 0

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    print(f"wrote {args.output}")
    for component in document["components"]:
        entry = component["licenses"][0]
        licence = entry["license"]["id"] if "license" in entry else entry["expression"]
        print(f"  {component['name']:<14} {component['version']:<12} {licence}")
    if missing:
        print(f"\nnot installed here, so recorded without a version: "
              f"{', '.join(missing)}", file=sys.stderr)
        print("run this on a machine with the full build environment for a "
              "release SBOM", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
