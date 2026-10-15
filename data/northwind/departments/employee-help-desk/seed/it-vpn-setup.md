# VPN Setup — Zero-Trust Remote Access

**Doc ID:** IT-410 · **Applies to:** All corporate + field laptops

Northwind uses **Contoso ZTA Client 4.2** for zero-trust remote access.
Legacy always-on VPN was retired on July 1, 2025 — do not install any
version older than 4.0.

## Install

1. Open the Company Portal on your Northwind-issued laptop.
2. Locate **Contoso ZTA Client** and click Install. Restart when prompted.
3. On first launch, sign in with your `@northwind.com` account.
4. Approve the MFA push in Authenticator.
5. When the tray icon turns green, you're on the corporate zone.

## What ZTA covers

- Access to `*.corp.northwind.com` internal sites.
- Access to file shares mounted under `\\fs01`, `\\fs02`.
- Access to Workday, Expensify, and Jira **is not** routed through ZTA —
  those are SSO-fronted and reachable from any network.

## Troubleshooting

- **"Posture check failed":** run Windows Update, reboot, retry. If it
  persists, open a ticket with **posture-check** in the subject.
- **"Sign-in loop":** clear the client cache from Settings ›
  Diagnostics › Reset credentials.
- **Field-tech offline handoff:** the client caches policy for 72 hours,
  so techs on a remote roof are covered for the standard workday.

For anything the above doesn't resolve, contact `it@northwind.com` or
open a ticket in `it.northwind.com/portal`.
