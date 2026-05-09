<#
.SYNOPSIS
    Syncs the approved Obsidian vault to s3://mce-second-brain/ami-context/

.DESCRIPTION
    Reads VAULT_PATH from the environment (or a local .env file in the repo root),
    then runs `aws s3 sync --delete` to keep S3 in sync with the vault.
    Logs results to %LOCALAPPDATA%\MagicContentEngine\vault-sync.log.
    Exits with code 0 on success, non-zero on any AWS CLI error.

.NOTES
    Designed to be called by Windows Task Scheduler.
    Schedule: Sunday 10pm NZT (Pacific/Auckland) — see scripts/README.md.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$S3_DESTINATION = "s3://mce-second-brain/ami-context/"
$LOG_DIR         = Join-Path $env:LOCALAPPDATA "MagicContentEngine"
$LOG_FILE        = Join-Path $LOG_DIR "vault-sync.log"

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$Level, [string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    $line = "$timestamp [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Ensure log directory exists
# ---------------------------------------------------------------------------
if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}

Write-Log "INFO" "=== vault-sync started ==="

# ---------------------------------------------------------------------------
# Resolve VAULT_PATH
# Priority: environment variable > .env file in repo root
# ---------------------------------------------------------------------------
$vaultPath = $env:VAULT_PATH

if (-not $vaultPath) {
    # Walk up from the script location to find the repo root .env
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $dotEnvPath = Join-Path $repoRoot ".env"

    if (Test-Path $dotEnvPath) {
        Write-Log "INFO" "VAULT_PATH not set in environment — reading from $dotEnvPath"
        Get-Content $dotEnvPath | ForEach-Object {
            if ($_ -match '^\s*VAULT_PATH\s*=\s*(.+)$') {
                $vaultPath = $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
}

if (-not $vaultPath) {
    Write-Log "ERROR" "VAULT_PATH is not set. Set it as an environment variable or add VAULT_PATH=/path/to/vault to the .env file in the repo root."
    exit 1
}

# Expand any environment variable references inside the path (e.g. %USERPROFILE%)
$vaultPath = [System.Environment]::ExpandEnvironmentVariables($vaultPath)

if (-not (Test-Path $vaultPath)) {
    Write-Log "ERROR" "VAULT_PATH does not exist: $vaultPath"
    exit 1
}

Write-Log "INFO" "Vault path : $vaultPath"
Write-Log "INFO" "Destination: $S3_DESTINATION"

# ---------------------------------------------------------------------------
# Verify AWS CLI is available
# ---------------------------------------------------------------------------
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Log "ERROR" "AWS CLI not found. Install it from https://aws.amazon.com/cli/ and ensure it is on PATH."
    exit 1
}

# ---------------------------------------------------------------------------
# Run aws s3 sync
# ---------------------------------------------------------------------------
Write-Log "INFO" "Running: aws s3 sync `"$vaultPath`" $S3_DESTINATION --delete"

$syncOutput = & aws s3 sync "$vaultPath" $S3_DESTINATION --delete 2>&1
$exitCode   = $LASTEXITCODE

# Log every line of output
foreach ($outputLine in $syncOutput) {
    if ($outputLine -match "^upload:") {
        Write-Log "INFO" $outputLine
    } elseif ($outputLine -match "^delete:") {
        Write-Log "INFO" $outputLine
    } elseif ($outputLine -match "^error|^fatal|An error" -or $outputLine -match "(?i)error") {
        Write-Log "ERROR" $outputLine
    } else {
        Write-Log "INFO" $outputLine
    }
}

# ---------------------------------------------------------------------------
# Summarise results
# ---------------------------------------------------------------------------
$uploaded = ($syncOutput | Where-Object { $_ -match "^upload:" } | Measure-Object).Count
$deleted  = ($syncOutput | Where-Object { $_ -match "^delete:" } | Measure-Object).Count

Write-Log "INFO" "Sync complete — uploaded: $uploaded, deleted: $deleted, exit code: $exitCode"

if ($exitCode -ne 0) {
    Write-Log "ERROR" "aws s3 sync exited with code $exitCode — check output above for details."
    Write-Log "INFO" "=== vault-sync finished (FAILED) ==="
    exit $exitCode
}

Write-Log "INFO" "=== vault-sync finished (OK) ==="
exit 0
