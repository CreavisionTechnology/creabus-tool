# Third party notices

CreaBus Tool itself is under the [GNU General Public License v3 or later](LICENSE).
Every line of code in this repository was written for this project; nothing is
copied in from another code base, and no third party source is vendored here.

This file records the software that CreaBus Tool *uses* or *ships alongside*,
because those components carry their own licences.

**Two things are true of every component below, and both are deliberate.**

1. **Each one is GPL-3.0 compatible**, so the free version may be distributed
   under the GPL.
2. **Each one is also usable in a product that is not under the GPL**, because
   every one of them is permissive (or carries an explicit exception). That is
   what makes CreaBus Tool Pro possible. A single copyleft dependency without
   an exception would make the paid version unlawful to ship, so this is
   checked rather than assumed, and anything new has to pass both tests.

| Component | Licence | GPL-3.0 compatible | Usable in a proprietary build |
| --- | --- | --- | --- |
| Python | PSF License Agreement | yes | yes |
| Tcl/Tk | Tcl/Tk License (BSD-style) | yes | yes |
| PyYAML | MIT | yes | yes |
| libyaml | MIT | yes | yes |
| pyserial | BSD-3-Clause | yes | yes |
| PyInstaller | GPL-2.0-or-later **with Bootloader Exception** | yes (via "or later") | yes (via the exception) |

Two situations to keep apart:

* **Running from source** — you install the dependencies yourself. This
  repository distributes none of them.
* **Downloading a release binary** — the executable has these components
  compiled into it, so the release is a distribution of them and must carry
  this notice. The CI workflow copies this file, and `LICENSE`, into every
  release archive for exactly that reason — if you package a build by hand,
  include them too.

---

## Direct dependencies

| Component | Licence | Needed for | Copyright |
| --- | --- | --- | --- |
| [Python](https://www.python.org/) | PSF License Agreement | everything | Copyright © 2001-2026 Python Software Foundation |
| [Tcl/Tk](https://www.tcl-lang.org/) (via `tkinter`) | Tcl/Tk License (BSD-style) | the window; not needed for `--headless` | Copyright © Regents of the University of California, Sun Microsystems Inc., Scriptics Corporation and other parties |
| [PyYAML](https://pyyaml.org/) | MIT | reading `.yaml` device profiles; a `.json` profile needs nothing | Copyright © 2017-2021 Ingy döt Net, © 2006-2016 Kirill Simonov |
| [libyaml](https://pyyaml.org/wiki/LibYAML) | MIT | PyYAML's C accelerator | Copyright © 2017-2020 Ingy döt Net, © 2006-2016 Kirill Simonov |
| [pyserial](https://github.com/pyserial/pyserial) | BSD-3-Clause | Modbus RTU only; TCP works without it | Copyright © 2001-2020 Chris Liechti |

## Build-time only

| Component | Licence | Notes |
| --- | --- | --- |
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later **with the Bootloader Exception** | See below. Only needed to build the standalone executables. |

### The PyInstaller Bootloader Exception

PyInstaller is GPL licensed, which normally raises the question of whether a
program bundled with it must also be GPL. It must not: PyInstaller grants an
explicit exception for precisely this case, quoted here from its `COPYING.txt`:

> **Bootloader Exception**
>
> In addition to the permissions in the GNU General Public License, the authors
> give you unlimited permission to link or embed compiled bootloader and
> related files into combinations with other programs, and to distribute those
> combinations without any restriction coming from the use of those files.

So the piece of PyInstaller that ends up inside `creabus-tool` — the bootloader —
is explicitly exempted. Using PyInstaller as a build tool does not affect the
licence of what it builds either, in the same way that compiling a program
with GCC does not make it GPL.

This matters twice here. The free version is GPL-3.0, and PyInstaller's
GPL-2.0-**or-later** permits that combination. The Pro version is not GPL, and
the Bootloader Exception is what permits *that* — without it, shipping a
proprietary build made with PyInstaller would not be lawful. Both rest on text
the PyInstaller authors wrote deliberately for this purpose.

## Bundled into the release binaries

A PyInstaller build embeds the Python runtime and the shared libraries it
depends on. Beyond the ones already listed above, a Linux build contains:

| Component | Licence |
| --- | --- |
| OpenSSL / libcrypto | Apache-2.0 |
| zlib | zlib License |
| bzip2 | BSD-4-Clause-like (bzip2 licence) |
| xz / liblzma | 0BSD / public domain |
| libffi | MIT |
| expat | MIT |
| FreeType | FreeType License (BSD-style, attribution required) |
| libpng | PNG Reference Library License (zlib-like) |
| Brotli | MIT |
| fontconfig | MIT-style |
| libX11, libXext, libXft, libXrender, libXScrnSaver | MIT/X11 |
| libbsd, libmd | BSD-3-Clause / BSD-2-Clause |

Windows and macOS builds bundle the same Python and Tcl/Tk, with that
platform's system libraries in place of the X11 ones.

This list reflects what the current build actually embeds. To re-check it after
a dependency or Python version change:

```bash
python - <<'EOF'
from PyInstaller.archive.readers import CArchiveReader
r = CArchiveReader("dist/creabus-tool")
print("\n".join(sorted(n for n in r.toc if ".so" in n or n.endswith(".dll"))))
EOF
```

Full licence texts are distributed with each component and are available at the
project links above.

---

## Names and trademarks

* **Modbus** is a registered trademark of Schneider Electric USA, Inc. This
  project is not affiliated with, endorsed by, or sponsored by Schneider
  Electric or the Modbus Organization. The name is used only descriptively, to
  say which protocol this tool speaks, and the Modbus Organization's logo is
  not used anywhere in this project.
* **Eastron**, **SDM120** and **SDM630** are names of a third party's products,
  used only to identify which device a bundled profile imitates. This project
  is not affiliated with or endorsed by Eastron. The profiles were written from
  the publicly documented register maps; they contain no vendor firmware, no
  vendor source code, and no text copied from a vendor manual.
* The CreaBus Tool logo is original artwork made for this project and is
  deliberately not modelled on any existing mark. The artwork is covered by the
  same GPL as the code; the *name* and the mark are not — see below.
* **CreaBus**, **CreaBus Tool** and **Creavision Technology**, and the
  Creavision Technology logo, are marks of Creavision Technology. The GPL
  covers the code, not the right to use the company's name, its product names
  or its logo to endorse or promote a derived product. If you distribute a
  modified version, please give it a different name — see the trademark note
  in the README.
