<#
.SYNOPSIS
  OpsPilot one-click incremental deploy: build images on dev machine -> load on target -> restart -> health check

.EXAMPLE
  .\deploy_update.ps1 -Host_ root@10.10.0.9
  .\deploy_update.ps1 -Host_ root@10.10.0.9 -FrontendOnly
  .\deploy_update.ps1 -Host_ root@10.10.0.9 -BackendTag 1.0.7 -FrontendTag 1.0.13
  .\deploy_update.ps1 -Host_ root@10.10.0.9 -CheckOnly
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Host_,
  [string]$BackendTag = "",
  [string]$FrontendTag = "",
  [switch]$BackendOnly,
  [switch]$FrontendOnly,
  [switch]$CheckOnly,
  [string]$StageDir = "/opt/opspilot-src/opspilot-offline-stage",
  [switch]$BackendScp
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Log([string]$m)  { Write-Host "[deploy] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die([string]$m)  { Write-Host "[error] $m" -ForegroundColor Red; exit 1 }

# 跑本机命令并返回退出码（吞掉 stderr 噪音，避免 PS 把 native stderr 当错误）
function Run-Native {
  param([string]$Exe, [string[]]$CmdArgs, [string]$Pipe = "")
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    if ($Pipe -ne "") {
      $Pipe | & $Exe @CmdArgs 2>$null | Out-Null
    } else {
      & $Exe @CmdArgs 2>$null | Out-Null
    }
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $old
  }
}

function Run-NativeOut {
  param([string]$Exe, [string[]]$CmdArgs)
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $Exe @CmdArgs 2>$null | Out-String
    $script:__native_exit = $LASTEXITCODE
    return $out.Trim()
  } finally {
    $ErrorActionPreference = $old
  }
}

if ($BackendOnly -and $FrontendOnly) { Die "cannot use -BackendOnly with -FrontendOnly" }

$DoBackend = -not $FrontendOnly.IsPresent
$DoFrontend = -not $BackendOnly.IsPresent

docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { Die "docker is not available" }

Log "probe target $Host_ ..."
$probe = "test -d '$StageDir' && test -f '$StageDir/.env' && test -f '$StageDir/app/docker-compose.yaml'"
if ((Run-Native "ssh" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $Host_, $probe)) -ne 0) {
  Die "target missing $StageDir or ssh unreachable (run deploy/offline install first, or pass -StageDir)"
}

$curBackend = Run-NativeOut "ssh" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $Host_, "grep -E '^OPSPILOT_BACKEND_IMAGE=' '$StageDir/.env' | tail -1 | cut -d= -f2")
$curFrontend = Run-NativeOut "ssh" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $Host_, "grep -E '^OPSPILOT_FRONTEND_IMAGE=' '$StageDir/.env' | tail -1 | cut -d= -f2")
if (-not $curBackend)  { $curBackend  = "opspilot/backend:1.0.0" }
if (-not $curFrontend) { $curFrontend = "opspilot/frontend:1.0.0" }
Log "target current: $curBackend / $curFrontend"

function Bump-Patch([string]$img) {
  $idx = $img.LastIndexOf(":")
  $repo = $img.Substring(0, $idx)
  $tag = $img.Substring($idx + 1)
  $base = ($tag -split "-")[0]
  $parts = @($base -split "\.")
  while ($parts.Count -lt 3) { $parts += "0" }
  $pat = [int]$parts[2] + 1
  return "{0}:{1}.{2}.{3}" -f $repo, $parts[0], $parts[1], $pat
}

if ($DoBackend -and -not $BackendTag)   { $BackendTag  = Bump-Patch $curBackend }
if ($DoFrontend -and -not $FrontendTag) { $FrontendTag = Bump-Patch $curFrontend }

$BackendImg  = "opspilot/backend:$BackendTag"
$FrontendImg = "opspilot/frontend:$FrontendTag"

if ($CheckOnly) {
  Log "health check only ..."
  $check = @'
curl -fsS -m 8 -o /dev/null -w "frontend %{http_code}\n" http://127.0.0.1:8080/ || true
curl -fsS -m 8 -o /dev/null -w "login-page %{http_code}\n" http://127.0.0.1:8001/api/settings/login-page || true
docker ps --format "{{.Names}} {{.Image}} {{.Status}}" | grep opspilot || true
'@
  $rc = Run-Native "ssh" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $Host_, "bash -s") $check
  exit $rc
}

Log "plan:"
if ($DoBackend)  { Log "  backend $BackendImg" }
if ($DoFrontend) { Log "  frontend $FrontendImg" }

function Build-Image([string]$tag, [string]$dir) {
  Log "build $tag ..."
  docker build -t $tag $dir
  if ($LASTEXITCODE -ne 0) { Die "build failed: $tag" }
}

if ($DoBackend)  { Build-Image $BackendImg  (Join-Path $Root "backend") }
if ($DoFrontend) { Build-Image $FrontendImg (Join-Path $Root "frontend") }

function Push-Image([string]$tag, [string]$kind) {
  Log "push $tag -> $Host_ ..."
  if ($kind -eq "backend" -and -not $BackendScp) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
      docker save $tag | ssh -o BatchMode=yes -o ConnectTimeout=15 $Host_ "docker load" 2>$null
      if ($LASTEXITCODE -ne 0) { Die "stream transfer failed: $tag" }
    } finally { $ErrorActionPreference = $old }
    return
  }
  $name = ($tag -replace "[/:]", "_") + ".tar"
  $tmp = Join-Path $env:TEMP $name
  docker save -o $tmp $tag
  if ($LASTEXITCODE -ne 0) { Die "docker save failed" }
  if ((Run-Native "scp" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $tmp, "${Host_}:/tmp/$name")) -ne 0) { Die "scp failed" }
  if ((Run-Native "ssh" @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", $Host_, "docker load -i /tmp/$name && rm -f /tmp/$name")) -ne 0) { Die "docker load failed" }
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

if ($DoBackend)  { Push-Image $BackendImg "backend" }
if ($DoFrontend) { Push-Image $FrontendImg "frontend" }

$services = @()
if ($DoBackend)  { $services += "backend" }
if ($DoFrontend) { $services += "frontend" }
$svc = ($services -join " ").Trim()

Log "update $StageDir/.env and restart ($svc) ..."

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("set -e")
if ($DoBackend) {
  $lines.Add("sed -i 's|^OPSPILOT_BACKEND_IMAGE=.*|OPSPILOT_BACKEND_IMAGE=$BackendImg|' '$StageDir/.env'")
}
if ($DoFrontend) {
  $lines.Add("sed -i 's|^OPSPILOT_FRONTEND_IMAGE=.*|OPSPILOT_FRONTEND_IMAGE=$FrontendImg|' '$StageDir/.env'")
}
$lines.Add("export PATH=`$PATH:/usr/libexec/docker/cli-plugins:/usr/lib/docker/cli-plugins:/usr/local/lib/docker/cli-plugins")
$lines.Add("cd '$StageDir'")
$lines.Add("set -a; . ./.env; set +a")
$lines.Add("if docker compose version >/dev/null 2>&1; then C='docker compose'; else C='docker-compose'; fi")
$lines.Add("`$C -f app/docker-compose.yaml --env-file .env up -d $svc")
$lines.Add("sleep 8")
$lines.Add("docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E 'opspilot-(backend|frontend)' || true")
$lines.Add("echo '--- health ---'")
$lines.Add("curl -fsS -m 8 -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1:8080/ || true")
$lines.Add("curl -fsS -m 8 -o /dev/null -w 'login-page %{http_code}\n' http://127.0.0.1:8001/api/settings/login-page || true")
$lines.Add("echo '--- backend log (errors) ---'")
$lines.Add("docker logs opspilot-backend --since 2m 2>&1 | grep -iE 'error|traceback' | head -10 || true")
$lines.Add("echo '--- env ---'")
$lines.Add("grep -E '^OPSPILOT_(BACKEND|FRONTEND)_IMAGE=' '$StageDir/.env' || true")

$oldEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  ($lines -join "`n") | ssh -o BatchMode=yes -o ConnectTimeout=15 $Host_ "bash -s" 2>$null
  $rc = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $oldEap
}
if ($rc -ne 0) { Die "target restart/verify failed (exit $rc)" }

Log "DONE"
