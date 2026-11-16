<#
.SYNOPSIS
  Back up KalingApp's database (Supabase Postgres) to a file on this PC.

.DESCRIPTION
  The Supabase plan KalingApp runs on has no automatic backups, so this is
  the backup. Run it regularly (weekly is a good habit, and before any big
  change) from PowerShell on your own computer.

  What is in a backup: every table KalingApp keeps in the database (all of
  the "public" schema's tables, not the schema itself, so a backup restores
  into any database) -- accounts, bookings, donor questionnaires, chats and
  messages, articles, notifications, the audit log. What is NOT: the
  serology photo files themselves (they live in Supabase Storage, not in the
  database), and Supabase's own internal schemas.

  A backup holds mothers' personal and health information (RA 10173). Keep
  it on this computer's own encrypted drive, never in a shared folder, an
  email, a chat or any git repository. Only the newest -Keep backups are
  kept; older ones are deleted, so personal data the app has since removed
  doesn't live on in old files.

  Connection details: in Supabase, open the project, press "Connect" and
  copy the "Session pooler" connection string (port 5432). The first run
  asks for it and saves it WITHOUT the password, next to the backups, in
  connection.txt. Every run asks for the database password in a hidden
  prompt and never stores it.

.PARAMETER OutDir
  Where backups go. Default: KalingApp-backups in your user folder.

.PARAMETER Keep
  How many backups to keep. Older ones are deleted. Default: 8.

.PARAMETER Password
  The database password as a SecureString, to skip the prompt (for example
  from a scheduled task). Leave it out to be asked.

.PARAMETER SslMode
  "require" (the default; always use it for Supabase). "disable" is only for
  testing against a database on this computer.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File "C:\PUP Files\KalingApp\backend\tools\backup-database.ps1"

.NOTES
  RESTORING (deliberately, never by accident). pg_restore asks for the
  password. Check a backup by restoring it somewhere harmless first.

  Into a new, empty database (a new Supabase project -- the safe way; point
  DATABASE_URL at it once it checks out):

    pg_restore --no-owner --no-privileges --single-transaction `
      --dbname "postgresql://USER@HOST:5432/postgres" "kalingapp-db-YYYYMMDD-HHMMSS.dump"

  Over the live database, replacing its tables with the backup's (everything
  since the backup is lost) -- add --clean --if-exists:

    pg_restore --clean --if-exists --no-owner --no-privileges --single-transaction `
      --dbname "postgresql://USER@HOST:5432/postgres" "kalingapp-db-YYYYMMDD-HHMMSS.dump"
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path $HOME 'KalingApp-backups'),
    [ValidateRange(1, 1000)][int]$Keep = 8,
    [securestring]$Password,
    [ValidateSet('require', 'disable')][string]$SslMode = 'require'
)

$ErrorActionPreference = 'Stop'

function Find-PgTool([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    # The wildcard goes in the file path itself: Windows PowerShell 5.1 doesn't
    # look inside wildcard-matched folders when given -Filter instead.
    $installed = @(Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$Name.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending)
    if ($installed) { return $installed[0].FullName }
    throw "$Name was not found. Install the PostgreSQL client tools (for example: winget install PostgreSQL.PostgreSQL.17), then run this again."
}

$pgDump = Find-PgTool 'pg_dump'
$pgRestore = Find-PgTool 'pg_restore'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# --- Where the database is (saved without the password) --------------------
$connectionFile = Join-Path $OutDir 'connection.txt'
if (Test-Path $connectionFile) {
    $connection = ([IO.File]::ReadAllText($connectionFile)).Trim()
} else {
    Write-Host 'Paste the Supabase "Session pooler" connection string (Supabase -> your project -> Connect).'
    Write-Host 'Leave its password part as shown (e.g. [YOUR-PASSWORD]): the password is asked for next, and never saved.'
    $connection = (Read-Host 'Connection string').Trim()
}
# Any password in the pasted string is dropped before it is even parsed.
$connection = $connection -replace '^(postgres(?:ql)?://[^:@/]+):[^@]*@', '$1@'
try { $uri = [System.Uri]$connection } catch { throw 'That is not a connection string like postgresql://postgres.xxxx:[YOUR-PASSWORD]@xxxx.pooler.supabase.com:5432/postgres' }
if ($uri.Scheme -notin @('postgres', 'postgresql')) { throw 'The connection string must start with postgresql://' }
$user = [System.Uri]::UnescapeDataString(($uri.UserInfo -split ':')[0])
$hostName = $uri.Host
$port = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
$database = $uri.AbsolutePath.TrimStart('/')
if (-not $database) { $database = 'postgres' }
if (-not $user -or -not $hostName) { throw 'The connection string is missing the user or the host.' }
if ($port -eq 6543) { throw 'That is the Transaction pooler (port 6543). pg_dump needs the Session pooler (port 5432): Supabase -> Connect -> Session pooler.' }
if (-not (Test-Path $connectionFile)) {
    [IO.File]::WriteAllText($connectionFile, "postgresql://${user}@${hostName}:${port}/${database}")
    Write-Host "Saved the connection details (no password) in $connectionFile"
}

# --- The backup ------------------------------------------------------------------
if (-not $Password) { $Password = Read-Host "Database password for $user" -AsSecureString }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$file = Join-Path $OutDir "kalingapp-db-$stamp.dump"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
try {
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $env:PGSSLMODE = $SslMode
    Write-Host "Backing up $database on $hostName ..."
    & $pgDump --host $hostName --port $port --username $user --dbname $database --no-password `
        --table 'public.*' --format custom --no-owner --no-privileges --file $file
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path $file) { Remove-Item $file -Force }
        throw "pg_dump failed (exit code $LASTEXITCODE). Nothing was saved. Check the password and the connection string."
    }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGSSLMODE -ErrorAction SilentlyContinue
}

# --- Check it can be read back, and holds KalingApp's tables ----------------
$listing = & $pgRestore --list $file
if ($LASTEXITCODE -ne 0) {
    Remove-Item $file -Force
    throw 'The backup could not be read back, so it was deleted. Run this again.'
}
$tables = @($listing | Select-String -SimpleMatch 'TABLE DATA public ').Count
$hasAccounts = @($listing | Select-String -SimpleMatch 'TABLE DATA public accounts_user ').Count -gt 0
$sizeMb = [math]::Round((Get-Item $file).Length / 1MB, 2)
if (-not $hasAccounts) {
    Write-Warning "The backup has no accounts table -- is this KalingApp's database? Kept anyway: $file"
}
Write-Host "Saved $file ($sizeMb MB, $tables tables)."

# --- Keep only the newest backups -------------------------------------------
Get-ChildItem $OutDir -Filter 'kalingapp-db-*.dump' | Sort-Object Name -Descending | Select-Object -Skip $Keep |
    ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Host "Removed old backup $($_.Name)"
    }

Write-Host ''
Write-Host 'Done. This file holds mothers'' personal and health information: keep it on this computer only.'
Write-Host 'Serology photo files are not in it (they are in Supabase Storage).'
