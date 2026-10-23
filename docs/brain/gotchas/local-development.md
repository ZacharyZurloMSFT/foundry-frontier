# Local development

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
running the stack locally or committing from Windows. Keep each entry short and cite a source.

- **Use port 8001 and `127.0.0.1`.** On Windows wslrelay grabs `:8000` (`ERR_CONNECTION_RESET` on every
  endpoint), and `localhost` can fail where `127.0.0.1` works (IPv6 vs IPv4). `scripts/dev-api.*` already uses
  8001. Source: [local-dev.md](../../local-dev.md#common-gotchas).
- **gpt-5.x and o-series need `max_completion_tokens`**, not `max_tokens`; this only affects custom model code.
  Source: [local-dev.md](../../local-dev.md#common-gotchas).
- **`DeploymentNotFound` in the bake-off, or `DeploymentModelNotSupported` when adding a model.** Set
  `FOUNDRY_BAKEOFF_DEPLOYMENTS` to deployments that exist in your project; catalog availability varies by
  subscription and region. Sources: [local-dev.md](../../local-dev.md#common-gotchas),
  [departments.md](../../departments.md#adding-partner-models-to-the-bake-off-tab).
- **Line endings are mixed and there is no `.gitattributes`.** The index has CRLF (`README.md`,
  `docs/api-management.md`, `apps/api/app/insurance.py`) and LF (`docs/brain/`, `docs/ai-gateway.md`,
  `apps/mcp-*/app/main.py`). New or edited files can come out CRLF on Windows and show as a whole-file diff: check
  `git ls-files --eol <file>` and `git diff --stat`, and fix with `git -c core.autocrlf=input add <file>`, delete
  the file, then `git checkout -- <file>`. Source: `git ls-files --eol`; the repository has no `.gitattributes`.
