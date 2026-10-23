# Import every value azd knows about into the current PowerShell session.
# Useful before running any of the scripts under scripts/ that expect
# post-provision env vars (create-prompt-agents.py, connect-knowledge-base.py,
# create-department-indexes.py, publish-prompts.py).
#
# Usage:
#   . .\scripts\load-azd-env.ps1            # note the leading dot — dot-source
#   python scripts\create-prompt-agents.py
#
# The dot-source is required so the env vars survive after the script exits.
[CmdletBinding()]
param(
    [string]$Environment = $null
)

$ErrorActionPreference = "Stop"

$args = @("env", "get-values")
if ($Environment) { $args += @("-e", $Environment) }

$loaded = 0
azd @args | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        $name = $Matches[1]
        $value = $Matches[2]
        # Strip the outer wrapping double-quotes that `azd env get-values`
        # emits around every value.
        if ($value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        # `azd env get-values` also escapes embedded double quotes with a
        # backslash (`\"`). PowerShell's simple `.Trim('"')` used to leave
        # them in, which broke JSON parsing on values like
        # FOUNDRY_PROJECT_ENDPOINTS. Un-escape here so downstream Python
        # sees real JSON.
        $value = $value -replace '\\"', '"'
        Set-Item -Path "env:$name" -Value $value
        $loaded++
    }
}

Write-Host "Loaded $loaded env vars from azd$(if ($Environment) { " env '$Environment'" } else { '' })." -ForegroundColor Green
