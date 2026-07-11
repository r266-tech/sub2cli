Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DefaultModel = "gpt-5.6-sol"
$DefaultApiBaseUrl = "https://api.openai.com/v1"

function Test-Truthy {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    return $Value -match '^(1|true|yes|on)$'
}

function Normalize-ApiUrl {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SUB2CLI_API_URL is required."
    }
    $Url = $Value.Trim().TrimEnd("/")
    if ($Url -notmatch '^https?://') {
        throw "SUB2CLI_API_URL must start with http:// or https://: $Value"
    }
    if ($Url -notmatch '/v1$') {
        $Url = "$Url/v1"
    }
    return $Url
}

function Read-ApiKey {
    $RawKey = [Environment]::GetEnvironmentVariable("SUB2CLI_API_KEY")
    if (-not [string]::IsNullOrWhiteSpace($RawKey)) {
        return $RawKey
    }

    $Secure = Read-Host "API key" -AsSecureString
    if ($Secure.Length -eq 0) {
        throw "SUB2CLI_API_KEY is required."
    }

    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr)
    }
}

function Escape-TomlString {
    param([string]$Value)
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Content
    )
    $Encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $Encoding)
}

function Get-CodexHome {
    $Override = [Environment]::GetEnvironmentVariable("CODEX_HOME")
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        return $Override
    }

    $HomeOverride = [Environment]::GetEnvironmentVariable("CODEX_PROVIDER_HOME")
    if (-not [string]::IsNullOrWhiteSpace($HomeOverride)) {
        return (Join-Path $HomeOverride ".codex")
    }

    $UserProfile = [Environment]::GetEnvironmentVariable("USERPROFILE")
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        $UserProfile = [Environment]::GetFolderPath("UserProfile")
    }
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        throw "Could not find USERPROFILE for Codex config."
    }
    return (Join-Path $UserProfile ".codex")
}

function Get-ItemIfExists {
    param([string]$Path)
    try {
        return Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
}

function Assert-NoProviderPoolState {
    param([string]$CodexHome)

    # Only a definite not-found result is safe to ignore. Permission and I/O
    # errors must bubble to the outer catch and stop before any overwrite.
    $ProviderSlots = Join-Path $CodexHome "provider-slots.json"
    if ($null -ne (Get-ItemIfExists $ProviderSlots)) {
        throw ("Existing provider-slots.json/connection-pool state detected; the one-line setup refused to overwrite it.`n" +
            "No ChatGPT/Codex configuration or pool state was changed.`n" +
            "Open ChatGPT Settings to edit the existing profile, or use this installer with a fresh Windows profile.")
    }
}

function Assert-DirectBootstrapSafe {
    param(
        [string]$CodexHome,
        [string]$ConfigToml
    )

    if ($null -ne (Get-ItemIfExists $ConfigToml)) {
        throw ("Existing config.toml/custom configuration detected; the one-line setup refused to overwrite it.`n" +
            "No ChatGPT/Codex configuration or pool state was changed.`n" +
            "Open ChatGPT Settings to edit the existing profile, or use this installer with a fresh Windows profile.")
    }

    Assert-NoProviderPoolState $CodexHome
}

function Test-FileTextEquals {
    param(
        [string]$Path,
        [string]$ExpectedContent
    )

    if ($null -eq (Get-ItemIfExists $Path)) {
        return $false
    }
    return [IO.File]::ReadAllText($Path) -ceq $ExpectedContent
}

function Test-FileBytesEqual {
    param(
        [string]$LeftPath,
        [string]$RightPath
    )

    if ($null -eq (Get-ItemIfExists $LeftPath) -or
        $null -eq (Get-ItemIfExists $RightPath)) {
        return $false
    }
    $LeftBytes = [IO.File]::ReadAllBytes($LeftPath)
    $RightBytes = [IO.File]::ReadAllBytes($RightPath)
    return [Collections.StructuralComparisons]::StructuralEqualityComparer.Equals(
        $LeftBytes,
        $RightBytes
    )
}

function Assert-FileTextEquals {
    param(
        [string]$Path,
        [string]$ExpectedContent,
        [string]$Label
    )

    if (-not (Test-FileTextEquals $Path $ExpectedContent)) {
        throw "$Label changed during the one-line setup; refusing to overwrite concurrent state."
    }
}

function Assert-OriginalAuthState {
    param(
        [string]$AuthJson,
        [bool]$AuthExisted,
        [string]$AuthBackup
    )

    $AuthItem = Get-ItemIfExists $AuthJson
    if ($AuthExisted) {
        if ($null -eq $AuthItem -or -not (Test-FileBytesEqual $AuthJson $AuthBackup)) {
            throw "auth.json changed during the one-line setup; refusing to overwrite concurrent state."
        }
        return
    }
    if ($null -ne $AuthItem) {
        throw "auth.json appeared during the one-line setup; refusing to overwrite concurrent state."
    }
}

function Move-ToRecoveryBackup {
    param(
        [string]$Source,
        [string]$BackupDir,
        [string]$Label,
        [string]$TransactionId
    )

    if ($null -eq (Get-ItemIfExists $Source)) {
        return
    }
    $RecoveryId = [Guid]::NewGuid().ToString("N")
    $RecoveryPath = Join-Path $BackupDir "$Label-concurrent-$TransactionId-$RecoveryId"
    Move-Item -LiteralPath $Source -Destination $RecoveryPath
}

function Restore-HeldFileWithoutOverwrite {
    param(
        [string]$HeldPath,
        [string]$LivePath,
        [string]$BackupDir,
        [string]$Label,
        [string]$TransactionId
    )

    if ($null -eq (Get-ItemIfExists $HeldPath)) {
        return
    }
    if ($null -eq (Get-ItemIfExists $LivePath)) {
        try {
            Move-Item -LiteralPath $HeldPath -Destination $LivePath
            return
        }
        catch {
            # Only treat the error as a destination race if a live file now
            # exists. Permission/I/O failures with no winner still fail closed.
            if ($null -eq (Get-ItemIfExists $LivePath)) {
                throw
            }
        }
    }
    Move-ToRecoveryBackup $HeldPath $BackupDir $Label $TransactionId
}

function Rollback-OwnedLiveFile {
    param(
        [string]$LivePath,
        [string]$ExpectedContent,
        [string]$QuarantinePath,
        [string]$OriginalHeldPath,
        [string]$BackupDir,
        [string]$Label,
        [string]$TransactionId
    )

    if ($null -eq (Get-ItemIfExists $LivePath)) {
        if (-not [string]::IsNullOrWhiteSpace($OriginalHeldPath)) {
            Move-ToRecoveryBackup $OriginalHeldPath $BackupDir $Label $TransactionId
        }
        return
    }

    # Move the current live path aside with no overwrite. We only delete the
    # quarantined version after proving it is byte-for-byte our staged text.
    Move-Item -LiteralPath $LivePath -Destination $QuarantinePath
    if (Test-FileTextEquals $QuarantinePath $ExpectedContent) {
        Remove-Item -LiteralPath $QuarantinePath -Force
        if (-not [string]::IsNullOrWhiteSpace($OriginalHeldPath)) {
            Restore-HeldFileWithoutOverwrite `
                $OriginalHeldPath $LivePath $BackupDir $Label $TransactionId
        }
        return
    }

    # Another process replaced or edited the file after our commit. Put that
    # version back without clobbering anything that appeared in the meantime.
    Restore-HeldFileWithoutOverwrite `
        $QuarantinePath $LivePath $BackupDir $Label $TransactionId
    if (-not [string]::IsNullOrWhiteSpace($OriginalHeldPath)) {
        Move-ToRecoveryBackup $OriginalHeldPath $BackupDir $Label $TransactionId
    }
}

function Restart-ChatGPTIfNeeded {
    if ((Test-Truthy ([Environment]::GetEnvironmentVariable("SUB2CLI_API_NO_RESTART"))) -or
        (Test-Truthy ([Environment]::GetEnvironmentVariable("SUB2CLI_NO_RESTART")))) {
        Write-Host "  ChatGPT/Codex restart skipped."
        return
    }

    try {
        Get-Process -Name "ChatGPT", "Codex" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1

        $Candidates = @()
        $Override = [Environment]::GetEnvironmentVariable("SUB2CLI_CHATGPT_APP")
        if ([string]::IsNullOrWhiteSpace($Override)) {
            $Override = [Environment]::GetEnvironmentVariable("SUB2CLI_CODEX_APP")
        }
        if (-not [string]::IsNullOrWhiteSpace($Override)) {
            $Candidates += $Override
        }
        $LocalAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
        if (-not [string]::IsNullOrWhiteSpace($LocalAppData)) {
            $Candidates += (Join-Path $LocalAppData "Programs\ChatGPT\ChatGPT.exe")
            $Candidates += (Join-Path $LocalAppData "Programs\Codex\Codex.exe")
        }
        $ProgramFiles = [Environment]::GetEnvironmentVariable("ProgramFiles")
        if (-not [string]::IsNullOrWhiteSpace($ProgramFiles)) {
            $Candidates += (Join-Path $ProgramFiles "ChatGPT\ChatGPT.exe")
            $Candidates += (Join-Path $ProgramFiles "Codex\Codex.exe")
        }
        $ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
        if (-not [string]::IsNullOrWhiteSpace($ProgramFilesX86)) {
            $Candidates += (Join-Path $ProgramFilesX86 "ChatGPT\ChatGPT.exe")
            $Candidates += (Join-Path $ProgramFilesX86 "Codex\Codex.exe")
        }

        foreach ($Candidate in $Candidates) {
            if (-not [string]::IsNullOrWhiteSpace($Candidate) -and (Test-Path -LiteralPath $Candidate)) {
                Start-Process -FilePath $Candidate | Out-Null
                Write-Host "  ChatGPT/Codex restarted: $Candidate"
                return
            }
        }

        if (Get-Command Get-StartApps -ErrorAction SilentlyContinue) {
            $StartApp = Get-StartApps |
                Where-Object { $_.Name -in @("ChatGPT", "Codex") } |
                Select-Object -First 1
            if ($null -ne $StartApp) {
                Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\$($StartApp.AppID)" | Out-Null
                Write-Host "  ChatGPT/Codex restarted from the Start menu."
                return
            }
        }

        Write-Host "  ChatGPT config is ready. Reopen the ChatGPT app manually."
    }
    catch {
        Write-Warning "ChatGPT config was saved, but automatic restart failed. Reopen the ChatGPT app manually."
    }
}

$ApiKey = $null
$AuthObject = $null
$AuthContent = $null
$AuthFinalContent = $null
$ConfigContent = $null
$ConfigFinalContent = $null
$AuthStage = $null
$ConfigStage = $null
$AuthOriginalHeldPath = $null
$AuthRollbackQuarantine = $null
$ConfigRollbackQuarantine = $null
$FailureException = $null

try {
    $ApiUrl = Normalize-ApiUrl ([Environment]::GetEnvironmentVariable("SUB2CLI_API_URL"))
    $CodexHome = Get-CodexHome
    $AuthJson = Join-Path $CodexHome "auth.json"
    $ConfigToml = Join-Path $CodexHome "config.toml"
    Assert-DirectBootstrapSafe $CodexHome $ConfigToml

    $ApiKey = Read-ApiKey
    $Model = [Environment]::GetEnvironmentVariable("SUB2CLI_API_MODEL")
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = $DefaultModel
    }

    # Read-ApiKey may be interactive. Revalidate after that wait so state
    # created by ChatGPT/Codex or sub2cli-inject in the meantime is preserved.
    Assert-DirectBootstrapSafe $CodexHome $ConfigToml

    $BackupRoot = Join-Path $CodexHome "provider-switch-backups"
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $TransactionId = [Guid]::NewGuid().ToString("N")
    $BackupDir = Join-Path $BackupRoot "install-api-$Stamp-$PID-$TransactionId"
    $AuthBackup = Join-Path $BackupDir "auth.json"

    New-Item -ItemType Directory -Force -Path $CodexHome, $BackupRoot | Out-Null
    New-Item -ItemType Directory -Path $BackupDir | Out-Null

    $AuthExisted = $null -ne (Get-ItemIfExists $AuthJson)
    if ($AuthExisted) {
        Copy-Item -LiteralPath $AuthJson -Destination $AuthBackup
        Assert-OriginalAuthState $AuthJson $true $AuthBackup
    }

    # Recheck after backup I/O, then fully stage both files before either live
    # path is touched.
    Assert-DirectBootstrapSafe $CodexHome $ConfigToml

    $AuthObject = [ordered]@{
        OPENAI_API_KEY = $ApiKey
        auth_mode = "apikey"
    }
    $AuthContent = ($AuthObject | ConvertTo-Json -Depth 3)
    $AuthFinalContent = $AuthContent + "`n"

    $ConfigContent = @"
model = "$(Escape-TomlString $Model)"
model_provider = "OpenAI"
api_base_url = "$DefaultApiBaseUrl"
disable_response_storage = true

[model_providers.OpenAI]
name = "OpenAI"
base_url = "$(Escape-TomlString $ApiUrl)"
wire_api = "responses"
requires_openai_auth = true
"@
    $ConfigFinalContent = $ConfigContent.TrimEnd() + "`n"
    $AuthStage = "$AuthJson.$PID.$TransactionId.stage"
    $ConfigStage = "$ConfigToml.$PID.$TransactionId.stage"
    $AuthOriginalHeldPath = "$AuthJson.$PID.$TransactionId.original"
    $AuthRollbackQuarantine = "$AuthJson.$PID.$TransactionId.rollback"
    $ConfigRollbackQuarantine = "$ConfigToml.$PID.$TransactionId.rollback"

    Write-Utf8NoBom $AuthStage $AuthFinalContent
    Write-Utf8NoBom $ConfigStage $ConfigFinalContent

    Assert-DirectBootstrapSafe $CodexHome $ConfigToml
    Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup

    $ConfigCommitted = $false
    $AuthOriginalHeld = $false
    $AuthCommitted = $false
    try {
        # Move-Item without -Force is an atomic same-volume rename on Windows
        # and refuses an existing destination. A concurrent config wins safely.
        Move-Item -LiteralPath $ConfigStage -Destination $ConfigToml
        $ConfigStage = $null
        $ConfigCommitted = $true
        Assert-NoProviderPoolState $CodexHome
        Assert-FileTextEquals $ConfigToml $ConfigFinalContent "config.toml"
        Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup

        if ($AuthExisted) {
            # Hold the exact original with a no-clobber rename, validate it
            # against the backup, then create the new auth path without Force.
            Move-Item -LiteralPath $AuthJson -Destination $AuthOriginalHeldPath
            $AuthOriginalHeld = $true
            if (-not (Test-FileBytesEqual $AuthOriginalHeldPath $AuthBackup)) {
                throw "auth.json changed during the one-line setup; refusing to overwrite concurrent state."
            }
        }
        Move-Item -LiteralPath $AuthStage -Destination $AuthJson
        $AuthStage = $null
        $AuthCommitted = $true

        # A pool/config writer may race after either commit. Success is only
        # reported while both live files are still exactly the staged versions.
        Assert-NoProviderPoolState $CodexHome
        Assert-FileTextEquals $ConfigToml $ConfigFinalContent "config.toml"
        Assert-FileTextEquals $AuthJson $AuthFinalContent "auth.json"

        if ($AuthOriginalHeld) {
            Remove-Item -LiteralPath $AuthOriginalHeldPath -Force
            $AuthOriginalHeld = $false
        }
    }
    catch {
        $CommitFailure = $_.Exception
        $RollbackErrors = [Collections.Generic.List[string]]::new()

        try {
            if ($AuthCommitted) {
                Rollback-OwnedLiveFile `
                    $AuthJson $AuthFinalContent $AuthRollbackQuarantine `
                    $(if ($AuthOriginalHeld) { $AuthOriginalHeldPath } else { "" }) `
                    $BackupDir "auth.json" $TransactionId
                $AuthCommitted = $false
                $AuthOriginalHeld = $false
            }
            elseif ($AuthOriginalHeld) {
                Restore-HeldFileWithoutOverwrite `
                    $AuthOriginalHeldPath $AuthJson $BackupDir "auth.json" $TransactionId
                $AuthOriginalHeld = $false
            }
        }
        catch {
            $RollbackErrors.Add("auth rollback: $($_.Exception.Message)")
        }

        try {
            if ($ConfigCommitted) {
                Rollback-OwnedLiveFile `
                    $ConfigToml $ConfigFinalContent $ConfigRollbackQuarantine "" `
                    $BackupDir "config.toml" $TransactionId
                $ConfigCommitted = $false
            }
        }
        catch {
            $RollbackErrors.Add("config rollback: $($_.Exception.Message)")
        }

        if ($RollbackErrors.Count -gt 0) {
            throw ("$($CommitFailure.Message)`nRollback warning: " +
                ($RollbackErrors -join "; "))
        }
        throw $CommitFailure
    }

    Write-Host "ChatGPT API configured."
    Write-Host "  url: $ApiUrl"
    Write-Host "  auth: $AuthJson"
    Write-Host "  config: $ConfigToml"
    Write-Host "  backup: $BackupDir"
    Restart-ChatGPTIfNeeded
}
catch {
    $FailureException = $_.Exception
}
finally {
    # Best-effort removal of plaintext staging files and script-scope secret
    # references. The caller-facing one-line wrapper also clears its env vars.
    foreach ($TemporaryPath in @($AuthStage, $ConfigStage)) {
        if (-not [string]::IsNullOrWhiteSpace($TemporaryPath)) {
            Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($null -ne $AuthObject) {
        $AuthObject.Clear()
    }
    $ApiKey = $null
    $AuthContent = $null
    $AuthFinalContent = $null
    $AuthObject = $null
}

if ($null -ne $FailureException) {
    # Do not call exit here: README executes this script in a child ScriptBlock.
    # An unhandled throw keeps -File non-zero while allowing the caller's
    # finally block to clear environment variables and the interactive shell to
    # remain alive.
    throw $FailureException
}
