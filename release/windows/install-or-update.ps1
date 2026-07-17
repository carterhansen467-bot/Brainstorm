[CmdletBinding()]
param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-SafeRelativePath([string]$Relative) {
    if ([System.IO.Path]::IsPathRooted($Relative) -or
        $Relative -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "Unsafe path in release manifest: $Relative"
    }
}

try {
    $Source = Join-Path $PSScriptRoot "Brainstorm"
    $Manifest = Join-Path $PSScriptRoot "RELEASE-MANIFEST.sha256"
    if (-not (Test-Path -LiteralPath (Join-Path $Source "Brainstorm_main.lua") -PathType Leaf)) {
        throw "The Brainstorm payload is missing. Extract the complete ZIP before running the installer."
    }
    if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
        throw "RELEASE-MANIFEST.sha256 is missing. Extract a fresh copy of the release ZIP."
    }

    if ([string]::IsNullOrWhiteSpace($Destination)) {
        if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
            throw "APPDATA is unavailable; pass -Destination with the active Brainstorm mod folder."
        }
        $Destination = Join-Path $env:APPDATA "Balatro\Mods\Brainstorm"
    }
    $Destination = [System.IO.Path]::GetFullPath($Destination)

    if (Test-Path -LiteralPath $Destination) {
        $Marker = Join-Path $Destination "Brainstorm_main.lua"
        if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
            throw "Refusing to overwrite '$Destination' because it is not a recognizable Brainstorm install."
        }
    } else {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    Write-Host "Checking the release package..."
    $Entries = @()
    $ManifestPaths = @{}
    foreach ($Line in Get-Content -LiteralPath $Manifest) {
        if ([string]::IsNullOrWhiteSpace($Line)) { continue }
        if ($Line -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
            throw "Invalid line in release manifest: $Line"
        }
        $Expected = $Matches[1].ToLowerInvariant()
        $Relative = $Matches[2]
        Assert-SafeRelativePath $Relative
        if ($ManifestPaths.ContainsKey($Relative)) {
            throw "Release manifest repeats a payload path: $Relative"
        }
        $ManifestPaths[$Relative] = $true
        $SourceFile = Join-Path $Source ($Relative -replace '/', '\')
        if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
            throw "Release file is missing: $Relative"
        }
        $Actual = (Get-FileHash -LiteralPath $SourceFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Expected) {
            throw "Release file failed its checksum: $Relative. Extract a fresh ZIP."
        }
        $Entries += [PSCustomObject]@{
            Relative = $Relative
            Source = $SourceFile
            Target = Join-Path $Destination ($Relative -replace '/', '\')
        }
    }
    if ($Entries.Count -eq 0) {
        throw "Release manifest is empty. Extract a fresh copy of the release ZIP."
    }
    # A truncated manifest must not turn a full update into a partial one, and
    # an unexpected payload file (especially user-like state) must never be
    # copied merely because it was placed inside the extracted package.
    $PayloadRoot = [System.IO.Path]::GetFullPath($Source).TrimEnd('\') + '\'
    $PayloadFiles = @(Get-ChildItem -LiteralPath $Source -Recurse -File)
    foreach ($PayloadFile in $PayloadFiles) {
        if (-not $PayloadFile.FullName.StartsWith(
                $PayloadRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Release payload escaped its package root: $($PayloadFile.FullName)"
        }
        $Relative = $PayloadFile.FullName.Substring($PayloadRoot.Length).Replace('\', '/')
        if (-not $ManifestPaths.ContainsKey($Relative)) {
            throw "Release payload contains an unlisted file: $Relative"
        }
    }
    if ($PayloadFiles.Count -ne $Entries.Count) {
        throw "Release manifest does not exactly cover the extracted payload."
    }

    # Back up only files managed by this release. User-owned state is not in
    # the manifest and is never selected: seed_pools/, settings.lua, and the
    # native_search.* snapshot/checkpoint files remain where they are.
    $Backup = Join-Path ([System.IO.Path]::GetTempPath()) ("Brainstorm-update-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Backup -Force | Out-Null
    $Replaced = @()
    $Created = @()
    $Legacy = @(
        (Join-Path $Destination "Seed Pool Builder.exe"),
        (Join-Path $Destination "native\brainstorm_seed_pool.exe")
    )

    try {
        Write-Host "Installing into $Destination"
        foreach ($Entry in $Entries) {
            $TargetParent = Split-Path -Parent $Entry.Target
            if (-not (Test-Path -LiteralPath $TargetParent)) {
                New-Item -ItemType Directory -Path $TargetParent -Force | Out-Null
            }
            if (Test-Path -LiteralPath $Entry.Target -PathType Leaf) {
                $BackupFile = Join-Path $Backup ($Entry.Relative -replace '/', '\')
                $BackupParent = Split-Path -Parent $BackupFile
                New-Item -ItemType Directory -Path $BackupParent -Force | Out-Null
                Copy-Item -LiteralPath $Entry.Target -Destination $BackupFile -Force
                $Replaced += [PSCustomObject]@{ Target = $Entry.Target; Backup = $BackupFile }
            } else {
                $Created += $Entry.Target
            }
            Copy-Item -LiteralPath $Entry.Source -Destination $Entry.Target -Force
        }

        # win-v9 and earlier put these builder-only executables in the mod root
        # and native/. Remove just those two known duplicates after the new
        # grouped copies are safely installed. No wildcard deletion is used.
        foreach ($OldFile in $Legacy) {
            if (Test-Path -LiteralPath $OldFile -PathType Leaf) {
                $Name = ([System.BitConverter]::ToString(
                    [System.Security.Cryptography.SHA256]::Create().ComputeHash(
                        [System.Text.Encoding]::UTF8.GetBytes($OldFile))
                    )).Replace('-', '').ToLowerInvariant()
                $BackupFile = Join-Path $Backup ("legacy-" + $Name)
                Copy-Item -LiteralPath $OldFile -Destination $BackupFile -Force
                $Replaced += [PSCustomObject]@{ Target = $OldFile; Backup = $BackupFile }
                Remove-Item -LiteralPath $OldFile -Force
            }
        }
    } catch {
        Write-Host "The copy failed; restoring the previous managed files..." -ForegroundColor Yellow
        foreach ($Path in $Created) {
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
            }
        }
        foreach ($Item in $Replaced) {
            $Parent = Split-Path -Parent $Item.Target
            New-Item -ItemType Directory -Path $Parent -Force | Out-Null
            Copy-Item -LiteralPath $Item.Backup -Destination $Item.Target -Force
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $Backup) {
            Remove-Item -LiteralPath $Backup -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host "Update complete." -ForegroundColor Green
    Write-Host "Preserved: seed_pools, settings.lua, native_search.cfg, and scan checkpoints."
    Write-Host "The standalone app is now grouped under 'Seed Pool Builder'."
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Quit Balatro and the Seed Pool Builder, then try again with a freshly extracted ZIP."
    exit 1
}
