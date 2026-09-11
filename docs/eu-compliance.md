<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (c) 2026 Creavision Technology -->

# Selling this in the EU: what is done and what is not

This is an internal working note for Creavision Technology, kept in the
repository because it is about the code. **It is not legal advice and it is
not a declaration of conformity.** Nobody has certified anything here.

## The thing to be clear about first

There is no such thing as making source code "CRA compliant". The Cyber
Resilience Act (Regulation (EU) 2024/2847) places obligations on a
**manufacturer placing a product on the market** — documentation, a risk
assessment, a vulnerability handling process, reporting, and a declaration of
conformity that a person signs. Most of that is organisational. It cannot be
satisfied by editing files, and any tool or person telling you a repository is
"CRA ready" is selling something.

What the code *can* do is supply the technical inputs those obligations are
built on, and not make the assessment harder than it needs to be. That part is
done and is listed below.

## Does it even apply?

The CRA exempts free and open source software supplied outside a commercial
activity. **CreaBus Tool, given away under the GPL, is very likely outside
scope. CreaBus Tool Pro, sold, is very likely inside it.** That split is the
single most important fact on this page, and it is why the two are separate
products rather than one with a licence key.

Timing, as understood at the time of writing and worth re-checking, since the
dates are the part most likely to have moved: the Regulation entered into
force in December 2024; reporting obligations for actively exploited
vulnerabilities apply from September 2026; the full set applies from December
2027.

The revised **Product Liability Directive (EU) 2024/2853** is the other one to
read. It treats software as a product, which means the "as is, no liability"
text in the GPL does not do for a paid product what it does for a free one.

## What is actually done in this repository

| Input | Where | Status |
| --- | --- | --- |
| Software bill of materials, machine readable | `tools/sbom.py` → `sbom.cdx.json`, CycloneDX 1.5 | generated on the build machine, shipped in every release archive |
| Coordinated vulnerability disclosure policy | [`SECURITY.md`](../SECURITY.md) | published, with a contact address and response times |
| A stated security model, including what is *not* protected | [`SECURITY.md`](../SECURITY.md) | written honestly, including "Modbus has no security at all" |
| Untrusted input handled as untrusted | `creabus_tool/expressions.py`, `pdu.py`, `rtu.py` | profile expressions vetted against a syntax allowlist; network frames length and bounds checked; framer and client count capped |
| Secure by default configuration | — | no telemetry, no auto-update, no network call the user did not ask for, no credentials stored |
| Reproducible builds from a known toolchain | `.github/workflows/build.yml` | four platforms, tests gate the build, archives verified before publication |
| Regression tests for the security boundary | `tools/selftest.py` | fourteen escape attempts, run on every push on three platforms |
| Dependency licence and provenance record | [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) | every component, both compatibility questions answered |

## What is not done, and cannot be done here

These are Creavision's to do, before selling anything. They are listed so that
none of them is a surprise later.

1. **A risk assessment** for the product, written down, covering intended use
   and foreseeable misuse. This tool writes to industrial equipment; that is
   the central entry.
2. **Technical documentation** as the Regulation specifies, retained for the
   required period.
3. **A vulnerability handling procedure** that is operated, not just
   published: someone reads the mailbox, triage has a timescale, fixes are
   released, users are told.
4. **Reporting** of actively exploited vulnerabilities and severe incidents to
   ENISA and the relevant national CSIRT, within the deadlines.
5. **A support period** declared for the paid product, during which security
   updates are provided.
6. **A conformity assessment** and an **EU declaration of conformity**, signed,
   and **CE marking** on the product. Whether this can be self-assessed or
   needs a notified body depends on the product's classification — an
   engineering tool that writes to industrial equipment is worth having
   classified by someone qualified rather than assumed.
7. **A legal review** of the liability position for a paid product, under the
   Product Liability Directive and under Romanian implementing law.

Items 1 to 5 are work Creavision can do. Items 6 and 7 need somebody
qualified, and are the ones worth paying for.

## Two practical notes

**Do not claim conformity you do not have.** No "CRA compliant" on the
website, no CE mark on the free version, no security claims beyond what
`SECURITY.md` says. An overstated claim is both an offence and exactly the
kind of thing that damages a small company's name.

**Keep the free version's exemption clean.** The exemption turns on
commercial activity. Selling support for the free version, or making it a
time-limited trial for the paid one, weakens the argument that it is outside
scope. Keeping the GPL version genuinely free and genuinely complete is worth
more than it looks.
