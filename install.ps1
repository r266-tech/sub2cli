Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DefaultModel = "gpt-5.6-sol"
$OfficialApiBaseUrl = "https://api.openai.com/v1"
$RelayProvider = "sub2api"

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
        $Key = $RawKey.Trim()
        if ($Key.Contains("`r") -or $Key.Contains("`n") -or $Key.IndexOf([char]0) -ge 0) {
            throw "SUB2CLI_API_KEY must be a single line."
        }
        return $Key
    }

    $Secure = Read-Host "API key" -AsSecureString
    if ($Secure.Length -eq 0) {
        throw "SUB2CLI_API_KEY is required."
    }

    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        $Key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr).Trim()
        if ([string]::IsNullOrWhiteSpace($Key)) {
            throw "SUB2CLI_API_KEY is required."
        }
        if ($Key.Contains("`r") -or $Key.Contains("`n") -or $Key.IndexOf([char]0) -ge 0) {
            throw "SUB2CLI_API_KEY must be a single line."
        }
        return $Key
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

function Test-AuthLooksLikeChatGpt {
    param([string]$Path)

    $Item = Get-ItemIfExists $Path
    if ($null -eq $Item) {
        return $false
    }
    try {
        $Text = [IO.File]::ReadAllText($Path)
    }
    catch {
        return $false
    }
    # Keep official login identity when present (same contract as install.sh).
    return ($Text -match '"auth_mode"\s*:\s*"chatgpt"') -or ($Text -match '"tokens"\s*:')
}

function Assert-DirectBootstrapSafe {
    param([string]$CodexHome)
    # One-line setup no longer refuses provider-slots.json: model routing is
    # rewritten directly onto config.toml. Multi-slot state is left untouched.
    if ([string]::IsNullOrWhiteSpace($CodexHome)) {
        throw "CODEX_HOME is required."
    }
}

function Get-TopLevelTomlString {
    param(
        [string]$Content,
        [string]$Key
    )

    $TableMatch = [regex]::Match($Content, '(?m)^[ \t]*\[')
    $Head = if ($TableMatch.Success) {
        $Content.Substring(0, $TableMatch.Index)
    }
    else {
        $Content
    }
    $EscapedKey = [regex]::Escape($Key)
    $KeyPattern = '(?:"{0}"|''{0}''|{0})' -f $EscapedKey
    $Pattern = '(?m)^[ \t]*{0}[ \t]*=[ \t]*([^\r\n]*?)[ \t]*\r?$' -f $KeyPattern
    $Match = [regex]::Match($Head, $Pattern)
    if (-not $Match.Success) {
        return [pscustomobject]@{ Present = $false; Value = $null }
    }
    $RawValue = $Match.Groups[1].Value
    $BasicString = [regex]::Match($RawValue, '^"([^"\\]*)"[ \t]*(?:#.*)?$')
    if ($BasicString.Success) {
        return [pscustomobject]@{ Present = $true; Value = $BasicString.Groups[1].Value }
    }
    $LiteralString = [regex]::Match($RawValue, "^'([^']*)'[ \t]*(?:#.*)?$")
    if ($LiteralString.Success) {
        return [pscustomobject]@{ Present = $true; Value = $LiteralString.Groups[1].Value }
    }
    throw ("Existing config.toml uses an unsupported value syntax for '$Key'; " +
        "the one-line setup refused to overwrite it.")
}

function Remove-TomlProviderBlock {
    param(
        [string]$Content,
        [string]$Provider
    )

    $EscapedProvider = [regex]::Escape($Provider)
    $Pattern = ('(?ms)^[ \t]*\[[ \t]*model_providers[ \t]*\.[ \t]*' +
        '(?:"{0}"|''{0}''|{0})[ \t]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|\z).*?' +
        '(?=^[ \t]*\[[^\r\n]*\][ \t]*(?:#[^\r\n]*)?\r?$|\z)') -f $EscapedProvider
    return [regex]::Replace(
        $Content,
        $Pattern,
        "",
        [Text.RegularExpressions.RegexOptions]::Multiline -bor
            [Text.RegularExpressions.RegexOptions]::Singleline
    )
}

function New-MergedConfigContent {
    param(
        [string]$ExistingContent,
        [string]$Model,
        [string]$ApiUrl,
        [string]$ApiKey
    )

    $ProviderSetting = Get-TopLevelTomlString $ExistingContent "model_provider"
    $ExistingProvider = if ($ProviderSetting.Present) { $ProviderSetting.Value } else { $null }
    if (-not [string]::IsNullOrWhiteSpace($ExistingProvider) -and
        $ExistingProvider -cnotin @("openai", "OpenAI", $RelayProvider)) {
        throw ("Existing config.toml selects custom provider '$ExistingProvider'; " +
            "the one-line setup refused to overwrite that provider selection.")
    }

    $OpenAiBaseUrlSetting = Get-TopLevelTomlString $ExistingContent "openai_base_url"
    $ExistingOpenAiBaseUrl = if ($OpenAiBaseUrlSetting.Present) { $OpenAiBaseUrlSetting.Value } else { $null }
    $ManagedBaseUrls = @(
        $OfficialApiBaseUrl.TrimEnd([char]"/"),
        $ApiUrl.TrimEnd([char]"/")
    )
    if (-not [string]::IsNullOrWhiteSpace($ExistingOpenAiBaseUrl) -and
        $ExistingOpenAiBaseUrl.TrimEnd([char]"/") -notin $ManagedBaseUrls -and
        $ExistingProvider -cnotin @("openai", "OpenAI", $RelayProvider)) {
        throw ("Existing config.toml already routes the built-in openai provider to another base URL; " +
            "the one-line setup refused to overwrite it.")
    }

    $ApiBaseUrlSetting = Get-TopLevelTomlString $ExistingContent "api_base_url"
    $ExistingApiBaseUrl = if ($ApiBaseUrlSetting.Present) { $ApiBaseUrlSetting.Value } else { $null }
    if (-not [string]::IsNullOrWhiteSpace($ExistingApiBaseUrl) -and
        $ExistingApiBaseUrl.TrimEnd([char]"/") -notin $ManagedBaseUrls) {
        throw ("Existing config.toml already sets api_base_url to another base URL; " +
            "the one-line setup refused to overwrite it.")
    }

    $TableMatch = [regex]::Match($ExistingContent, '(?m)^[ \t]*\[')
    if ($TableMatch.Success) {
        $Head = $ExistingContent.Substring(0, $TableMatch.Index)
        $Tables = $ExistingContent.Substring($TableMatch.Index)
    }
    else {
        $Head = $ExistingContent
        $Tables = ""
    }

    foreach ($Key in @("model", "model_provider", "openai_base_url", "api_base_url", "disable_response_storage")) {
        $EscapedKey = [regex]::Escape($Key)
        $KeyPattern = '(?:"{0}"|''{0}''|{0})' -f $EscapedKey
        $Pattern = '(?m)^[ \t]*{0}[ \t]*=.*(?:\r?\n|\z)' -f $KeyPattern
        $Head = [regex]::Replace($Head, $Pattern, "")
    }

    $Tables = Remove-TomlProviderBlock $Tables $RelayProvider
    if ($ExistingProvider -cin @("OpenAI", "openai")) {
        $Tables = Remove-TomlProviderBlock $Tables "OpenAI"
    }

    # Direct URL + key (no local proxy / no connection pool). Same product mode as install.sh.
$ManagedHead = @"
model = "$(Escape-TomlString $Model)"
model_provider = "$RelayProvider"
openai_base_url = "$(Escape-TomlString $ApiUrl)"
disable_response_storage = true
"@
    $ManagedHead = $ManagedHead.Trim()

    $ProviderBlock = @"
[model_providers.$RelayProvider]
name = "Sub2API"
base_url = "$(Escape-TomlString $ApiUrl)"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
experimental_bearer_token = "$(Escape-TomlString $ApiKey)"
"@
    $ProviderBlock = $ProviderBlock.Trim()

    $Parts = [Collections.Generic.List[string]]::new()
    $Parts.Add($ManagedHead)
    if (-not [string]::IsNullOrWhiteSpace($Head)) {
        $Parts.Add($Head.Trim())
    }
    if (-not [string]::IsNullOrWhiteSpace($Tables)) {
        $Parts.Add($Tables.Trim())
    }
    $Parts.Add($ProviderBlock)
    return ($Parts -join "`r`n`r`n") + "`r`n"
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

function Assert-OriginalConfigState {
    param(
        [string]$ConfigToml,
        [bool]$ConfigExisted,
        [string]$ConfigBackup
    )

    $ConfigItem = Get-ItemIfExists $ConfigToml
    if ($ConfigExisted) {
        if ($null -eq $ConfigItem -or -not (Test-FileBytesEqual $ConfigToml $ConfigBackup)) {
            throw "config.toml changed during the one-line setup; refusing to overwrite concurrent state."
        }
        return
    }
    if ($null -ne $ConfigItem) {
        throw "config.toml appeared during the one-line setup; refusing to overwrite concurrent state."
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
$ConfigOriginalHeldPath = $null
$AuthRollbackQuarantine = $null
$ConfigRollbackQuarantine = $null
$FailureException = $null

try {
    $ApiUrl = Normalize-ApiUrl ([Environment]::GetEnvironmentVariable("SUB2CLI_API_URL"))
    $CodexHome = Get-CodexHome
    $AuthJson = Join-Path $CodexHome "auth.json"
    $ConfigToml = Join-Path $CodexHome "config.toml"
    Assert-DirectBootstrapSafe $CodexHome

    $ApiKey = Read-ApiKey
    $Model = [Environment]::GetEnvironmentVariable("SUB2CLI_API_MODEL")
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = $DefaultModel
    }
    else {
        $Model = $Model.Trim()
    }

    # Read-ApiKey may be interactive. Revalidate after that wait so state
    # created by ChatGPT/Codex or sub2cli-inject in the meantime is preserved.
    Assert-DirectBootstrapSafe $CodexHome

    $BackupRoot = Join-Path $CodexHome "provider-switch-backups"
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $TransactionId = [Guid]::NewGuid().ToString("N")
    $BackupDir = Join-Path $BackupRoot "install-api-$Stamp-$PID-$TransactionId"
    $AuthBackup = Join-Path $BackupDir "auth.json"
    $ConfigBackup = Join-Path $BackupDir "config.toml"

    New-Item -ItemType Directory -Force -Path $CodexHome, $BackupRoot | Out-Null
    New-Item -ItemType Directory -Path $BackupDir | Out-Null

    $AuthExisted = $null -ne (Get-ItemIfExists $AuthJson)
    if ($AuthExisted) {
        Copy-Item -LiteralPath $AuthJson -Destination $AuthBackup
        Assert-OriginalAuthState $AuthJson $true $AuthBackup
    }
    $ConfigExisted = $null -ne (Get-ItemIfExists $ConfigToml)
    if ($ConfigExisted) {
        Copy-Item -LiteralPath $ConfigToml -Destination $ConfigBackup
        Assert-OriginalConfigState $ConfigToml $true $ConfigBackup
    }

    # Recheck after backup I/O, then fully stage both files before either live
    # path is touched.
    Assert-DirectBootstrapSafe $CodexHome
    Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup
    Assert-OriginalConfigState $ConfigToml $ConfigExisted $ConfigBackup

    $KeepChatGptAuth = $AuthExisted -and (Test-AuthLooksLikeChatGpt $AuthJson)
    $AuthObject = $null
    $AuthContent = $null
    $AuthFinalContent = $null
    $RewriteAuth = -not $KeepChatGptAuth

    if ($RewriteAuth) {
        $AuthObject = [ordered]@{
            OPENAI_API_KEY = $ApiKey
            auth_mode = "apikey"
        }
        $AuthContent = ($AuthObject | ConvertTo-Json -Depth 3)
        $AuthFinalContent = $AuthContent + "`n"
    }

    $ConfigContent = if ($ConfigExisted) {
        [IO.File]::ReadAllText($ConfigBackup)
    }
    else {
        ""
    }
    $ConfigFinalContent = New-MergedConfigContent $ConfigContent $Model $ApiUrl $ApiKey
    $AuthStage = if ($RewriteAuth) { "$AuthJson.$PID.$TransactionId.stage" } else { $null }
    $ConfigStage = "$ConfigToml.$PID.$TransactionId.stage"
    $AuthOriginalHeldPath = "$AuthJson.$PID.$TransactionId.original"
    $ConfigOriginalHeldPath = "$ConfigToml.$PID.$TransactionId.original"
    $AuthRollbackQuarantine = "$AuthJson.$PID.$TransactionId.rollback"
    $ConfigRollbackQuarantine = "$ConfigToml.$PID.$TransactionId.rollback"

    if ($RewriteAuth) {
        Write-Utf8NoBom $AuthStage $AuthFinalContent
    }
    Write-Utf8NoBom $ConfigStage $ConfigFinalContent

    Assert-DirectBootstrapSafe $CodexHome
    Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup
    Assert-OriginalConfigState $ConfigToml $ConfigExisted $ConfigBackup

    $ConfigCommitted = $false
    $ConfigOriginalHeld = $false
    $AuthOriginalHeld = $false
    $AuthCommitted = $false
    try {
        if ($ConfigExisted) {
            Move-Item -LiteralPath $ConfigToml -Destination $ConfigOriginalHeldPath
            $ConfigOriginalHeld = $true
            if (-not (Test-FileBytesEqual $ConfigOriginalHeldPath $ConfigBackup)) {
                throw "config.toml changed during the one-line setup; refusing to overwrite concurrent state."
            }
        }
        # Move-Item without -Force is an atomic same-volume rename on Windows.
        # A concurrently created replacement wins safely and triggers rollback.
        Move-Item -LiteralPath $ConfigStage -Destination $ConfigToml
        $ConfigStage = $null
        $ConfigCommitted = $true
        Assert-FileTextEquals $ConfigToml $ConfigFinalContent "config.toml"
        Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup

        if ($RewriteAuth) {
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
            Assert-FileTextEquals $AuthJson $AuthFinalContent "auth.json"
        }
        else {
            # Official login kept; model traffic uses experimental_bearer_token.
            Assert-OriginalAuthState $AuthJson $AuthExisted $AuthBackup
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
                    $ConfigToml $ConfigFinalContent $ConfigRollbackQuarantine `
                    $(if ($ConfigOriginalHeld) { $ConfigOriginalHeldPath } else { "" }) `
                    $BackupDir "config.toml" $TransactionId
                $ConfigCommitted = $false
                $ConfigOriginalHeld = $false
            }
            elseif ($ConfigOriginalHeld) {
                Restore-HeldFileWithoutOverwrite `
                    $ConfigOriginalHeldPath $ConfigToml $BackupDir "config.toml" $TransactionId
                $ConfigOriginalHeld = $false
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

    # The live files are fully committed. Cleanup failures must not trigger a
    # rollback after either rollback source has already been deleted.
    foreach ($HeldPath in @(
        $(if ($AuthOriginalHeld) { $AuthOriginalHeldPath } else { "" }),
        $(if ($ConfigOriginalHeld) { $ConfigOriginalHeldPath } else { "" })
    )) {
        if (-not [string]::IsNullOrWhiteSpace($HeldPath)) {
            Remove-Item -LiteralPath $HeldPath -Force -ErrorAction SilentlyContinue
            if ($null -ne (Get-ItemIfExists $HeldPath)) {
                Write-Warning "Committed configuration, but could not remove temporary original: $HeldPath"
            }
        }
    }
    $AuthOriginalHeld = $false
    $ConfigOriginalHeld = $false

    Write-Host "ChatGPT API configured (direct URL, no local proxy / pool)."
    Write-Host "  url: $ApiUrl"
    if ($KeepChatGptAuth) {
        Write-Host "  identity: kept existing ChatGPT/OpenAI login"
    }
    else {
        Write-Host "  identity: API-key auth written"
    }
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
