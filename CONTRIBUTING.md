# Contributing to CreaBus Tool

Thanks for taking a look. Bug reports, device profiles and pull requests are
all welcome.

## Reporting a bug

Open an issue with:

* what you were doing (server or client, TCP or RTU, which profile),
* what you expected and what happened instead,
* the version — `creabus-tool --version`, or `python main.py --version`,
* your OS and, if you built from source, your Python version.

For protocol problems the **Debug log** tab with *Log raw frames (hex)* ticked
is worth more than any description — **Save...** writes it to a file you can
attach. Please check it for anything you would rather not publish (IP
addresses, host names) before you do.

## Setting up

```bash
git clone https://github.com/CreavisionTechnology/creabus-tool.git
cd creabus-tool
pip install -r requirements.txt -r requirements-dev.txt
python main.py
```

Linux needs Tk for the window: `sudo apt install python3-tk`.

## Branching

`main` is always releasable. Work happens on a branch off it, one branch per
change, merged back when the tests are green:

```bash
git checkout main && git pull
git checkout -b feature/short-description
# ... work, run the self tests ...
git push -u origin feature/short-description
```

Use `feature/` for new capability, `fix/` for a bug. Releases are tags on
`main` (`v0.0.1`), and pushing the tag is what builds and publishes them.

## Before you open a pull request

Run the three self tests. They are quick, they need no hardware, and CI runs
exactly the same three on Windows, Linux and macOS:

```bash
python tools/selftest.py          # the device and the protocol
python tools/selftest_links.py    # RTU framing and the client, over a loopback
python tools/uitest.py            # the real window, in all four modes
```

On a headless Linux machine the last one needs a virtual display:
`xvfb-run -a python tools/uitest.py`.

If you changed the UI, regenerate the screenshots rather than taking new ones
by hand — the script drives the real window on a virtual display, so nothing
from your desktop can end up in the image:

```bash
xvfb-run -a python tools/screenshots.py      # needs imagemagick
```

## House style

The code is deliberately plain: the standard library where possible, no
framework, and a module per concern. Match what is around your change.

* **No new required dependencies.** PyYAML and pyserial are both optional and
  degrade gracefully; anything new should too, or it should not be there.
* **Keep transports and roles separate.** `pdu.py` knows the protocol and
  nothing about sockets or serial ports; `server.py` and `client.py` know the
  roles; `rtu.py` and the TCP endpoint know the wire. A change that blurs
  those lines usually belongs somewhere else.
* **Comment the why, not the what.** The existing comments explain awkward
  protocol corners and platform differences — that is the bar.
* Every source file starts with the two SPDX header lines. Copy them into any
  new file.

## Adding a device profile

New profiles are very welcome — they are the easiest useful contribution.

1. Copy `devices/_template.yaml`, which uses every option once with comments.
2. Fill in the register map from the device's published documentation. Do not
   paste text out of a vendor manual: describe the registers, do not copy the
   prose.
3. Check it loads and looks right: `python main.py --device devices/yours.yaml`.
4. If you own the hardware, point the client at the real device and confirm the
   values match. Say so in the pull request — it is the difference between a
   profile people can trust and one they cannot.

`devices/README.md` is the full schema reference.

## Licensing of contributions

CreaBus Tool is free software under the
[GNU General Public License v3 or later](LICENSE). Creavision Technology also
sells **CreaBus Tool Pro**, which is not under the GPL, and contributions may
end up in it.

That arrangement needs your permission, so it is written out in full in
[CLA.md](CLA.md) — please read it before you spend an evening on a patch. The
short version: **you keep your copyright**, nothing is assigned, your work
stays in the free version forever, and Creavision may also use it in the paid
one. If you would rather not agree to that, bug reports and register maps are
just as welcome and need no agreement at all.

You accept it by signing off each commit, which `git commit -s` does for you:

```bash
git commit -s -m "Fix the thing"
```

Please do not paste in code from another project, even a permissively licensed
one, without saying so in the pull request — keeping the provenance of this
code base clean and single-licensed is deliberate, and it is what makes the
dual licence above possible at all. Code copied from a permissive project
cannot be relicensed by us, and code copied from a GPL project could not go in
the Pro version; either way it has to be flagged, not discovered later.
