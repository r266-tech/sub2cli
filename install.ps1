Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DefaultModel = "gpt-5.5"
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

function Backup-IfExists {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (Test-Path -LiteralPath $Source) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

function Restart-CodexIfNeeded {
    if ((Test-Truthy ([Environment]::GetEnvironmentVariable("SUB2CLI_API_NO_RESTART"))) -or
        (Test-Truthy ([Environment]::GetEnvironmentVariable("SUB2CLI_NO_RESTART")))) {
        Write-Host "  Codex restart skipped."
        return
    }

    Get-Process -Name "Codex" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    $Candidates = @()
    $Override = [Environment]::GetEnvironmentVariable("SUB2CLI_CODEX_APP")
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        $Candidates += $Override
    }
    $LocalAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
    if (-not [string]::IsNullOrWhiteSpace($LocalAppData)) {
        $Candidates += (Join-Path $LocalAppData "Programs\Codex\Codex.exe")
    }
    $ProgramFiles = [Environment]::GetEnvironmentVariable("ProgramFiles")
    if (-not [string]::IsNullOrWhiteSpace($ProgramFiles)) {
        $Candidates += (Join-Path $ProgramFiles "Codex\Codex.exe")
    }
    $ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if (-not [string]::IsNullOrWhiteSpace($ProgramFilesX86)) {
        $Candidates += (Join-Path $ProgramFilesX86 "Codex\Codex.exe")
    }

    foreach ($Candidate in $Candidates) {
        if (-not [string]::IsNullOrWhiteSpace($Candidate) -and (Test-Path -LiteralPath $Candidate)) {
            Start-Process -FilePath $Candidate | Out-Null
            Write-Host "  Codex restarted: $Candidate"
            return
        }
    }

    Write-Host "  Codex config is ready. Restart Codex manually if it is already open."
}

try {
    $ApiUrl = Normalize-ApiUrl ([Environment]::GetEnvironmentVariable("SUB2CLI_API_URL"))
    $ApiKey = Read-ApiKey
    $Model = [Environment]::GetEnvironmentVariable("SUB2CLI_API_MODEL")
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = $DefaultModel
    }

    $CodexHome = Get-CodexHome
    $AuthJson = Join-Path $CodexHome "auth.json"
    $ConfigToml = Join-Path $CodexHome "config.toml"
    $BackupRoot = Join-Path $CodexHome "provider-switch-backups"
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupDir = Join-Path $BackupRoot "install-api-$Stamp"
    if (Test-Path -LiteralPath $BackupDir) {
        $BackupDir = "$BackupDir-$PID"
    }

    New-Item -ItemType Directory -Force -Path $CodexHome, $BackupDir | Out-Null
    Backup-IfExists $AuthJson (Join-Path $BackupDir "auth.json")
    Backup-IfExists $ConfigToml (Join-Path $BackupDir "config.toml")

    $AuthObject = [ordered]@{
        OPENAI_API_KEY = $ApiKey
        auth_mode = "apikey"
    }
    $AuthContent = ($AuthObject | ConvertTo-Json -Depth 3)
    $AuthTmp = "$AuthJson.$PID.tmp"
    Write-Utf8NoBom $AuthTmp ($AuthContent + "`n")
    Move-Item -LiteralPath $AuthTmp -Destination $AuthJson -Force

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
    $ConfigTmp = "$ConfigToml.$PID.tmp"
    Write-Utf8NoBom $ConfigTmp ($ConfigContent.TrimEnd() + "`n")
    Move-Item -LiteralPath $ConfigTmp -Destination $ConfigToml -Force

    Write-Host "Codex API configured."
    Write-Host "  url: $ApiUrl"
    Write-Host "  auth: $AuthJson"
    Write-Host "  config: $ConfigToml"
    Write-Host "  backup: $BackupDir"
    Restart-CodexIfNeeded
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
