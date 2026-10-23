# Local development

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
running the stack locally or committing from Windows. Keep each entry short and cite a source.

- **Use port 8001 and `127.0.0.1`.** On Windows wslrelay grabs `:8000` (`ERR_CONNECTION_RESET` on every
  endpoint), and `localhost` can fail where `127.0.0.1` works (IPv6 vs IPv4). `scripts/dev-api.*` already uses
  8001. Source: [local-dev.md](../../local-dev.md#common-gotchas).
- **Cosmos-backed routes fail on a laptop.** Cosmos DB has public access disabled in the Bicep, so approvals,
  sessions, harness runs, voice tickets and `/api/v1` answer 503 locally unless `COSMOS_ENDPOINT` points at an
  account you can reach; run the portal against a deployed API for those flows. Sources:
  [README](../../../README.md#run-locally), [local-dev.md](../../local-dev.md#environment).
- **Line endings: LF, with two CRLF exceptions.** `.editorconfig` sets `end_of_line = lf` and there is no
  `.gitattributes`; the index is LF apart from `scripts/configure-github-oidc.py` and
  `scripts/tests/test_oidc_apim.py`. New or edited files can come out CRLF on Windows and show as a whole-file diff:
  check `git ls-files --eol <file>` and `git diff --stat`, and fix with `git -c core.autocrlf=input add <file>`,
  delete the file, then `git checkout-index -- <file>`. Source: `git ls-files --eol`, `.editorconfig`.
