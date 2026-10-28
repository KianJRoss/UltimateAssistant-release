# Security policy

Ultimate Assistant runs on your own computer. It can use your AI accounts, read and write files in its
workspace, and, with the optional modules, control your desktop or phone. Treat access to the app (port 8765)
and its local Herald Router (port 18790) like access to your user account. Both listen only on this computer
by default.

## Supported versions

Security fixes go into the latest release. Please update to it and check the problem still happens before
reporting.

| Version | Supported |
| --- | --- |
| Latest 0.4.x | Yes |
| 0.3.x and older | No |

## Reporting a vulnerability

Please don't open a public issue for a suspected vulnerability. Use
[private vulnerability reporting](https://github.com/KianJRoss/UltimateAssistant-release/security/advisories/new)
and include:

- the version and operating system;
- steps to reproduce it and what an attacker could do;
- whether credentials or personal data were exposed; and
- a suggested fix, if you have one.

Leave out live tokens, login codes, private conversations and screen captures. Revoke any credential that was
exposed before you report. You should get a reply within seven days.

Problems in the Herald Router itself can also be reported to
[Herald](https://github.com/KianJRoss/herald-release/security).
