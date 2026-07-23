# Security policy

## What this document is for

This is about **technical security vulnerabilities in M Maps** — bugs that could
break the app's safety model. Examples of what belongs here:

- Someone on the LAN (or the wider network) could control another person's
  spoof session without intending to use `--lan` / without understanding the risk
- Privilege escalation beyond what the admin password dialog is supposed to allow
- A way to leave a simulated location stuck when the user thought they had fully
  cleared it / restored real GPS
- Path or process issues that let untrusted input run as root via the elevated
  server path

Please **do not** use this channel to debate the ethics of location spoofing in
general — the product disclaimer ("harmless pranks only") already lives in the
README. Security reports should describe a concrete technical flaw.

## How to report a vulnerability

**Prefer a private report** so the issue isn't public before a fix exists:

1. Open a **private vulnerability report** on this repository:  
   [Security → Report a vulnerability](https://github.com/mayoka0/m-maps/security/advisories/new)  
   (GitHub Security Advisories — only maintainers see the details until published.)
2. If that form isn't available to you, contact the maintainer privately via
   [@mayoka0](https://github.com/mayoka0) on GitHub and say you have a security
   report (don't paste exploit details in a public issue or PR).

Please include:

- What you found and why it matters
- Steps to reproduce (macOS version, iOS version if relevant, how you launched
  the app — `app` / `serve` / `serve --lan`)
- Impact as you understand it
- Any suggested fix (optional)

## What to expect

- We'll try to acknowledge private reports in a reasonable time.
- We'll work on a fix before any public disclosure when the report is valid.
- Credit is welcome if you want it; say so in the report.

## Out of scope (usually)

- "Someone with physical access / admin on my Mac can spoof my phone" — by design
  the tool requires admin and a USB-connected phone that trusts this Mac
- Rate limits or abuse of third-party services (OSRM, Nominatim, map tiles) used
  only from the browser/UI
- Feature requests or ethics discussions (use Issues for the former)
