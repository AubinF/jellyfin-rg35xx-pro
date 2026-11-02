param(
  [string]$BuildDir = "build-arm64",
  [string]$Output = "jellyfinrg35xx.zip"
)

$ErrorActionPreference = "Stop"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("jellyfin-rg35xx-port-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $stage "jellyfinrg35xx") -Force | Out-Null
Copy-Item -LiteralPath "port/JellyfinRG35XX.sh" -Destination (Join-Path $stage "Jellyfin RG35XX.sh")
Copy-Item -LiteralPath "port/port.json" -Destination (Join-Path $stage "jellyfinrg35xx/port.json")
Copy-Item -LiteralPath "port/README.md" -Destination (Join-Path $stage "jellyfinrg35xx/README.md")
Copy-Item -LiteralPath "port/gameinfo.xml" -Destination (Join-Path $stage "jellyfinrg35xx/gameinfo.xml")
Copy-Item -LiteralPath "app/jellyfin_rg35xx.py" -Destination (Join-Path $stage "jellyfinrg35xx/jellyfin_rg35xx.py")
Copy-Item -LiteralPath "app/playback.py" -Destination (Join-Path $stage "jellyfinrg35xx/playback.py")
Copy-Item -LiteralPath "app/library.py" -Destination (Join-Path $stage "jellyfinrg35xx/library.py")
Copy-Item -LiteralPath "app/music.py" -Destination (Join-Path $stage "jellyfinrg35xx/music.py")
Copy-Item -LiteralPath "app/downloads.py" -Destination (Join-Path $stage "jellyfinrg35xx/downloads.py")
Copy-Item -LiteralPath "app/ui.py" -Destination (Join-Path $stage "jellyfinrg35xx/ui.py")
Copy-Item -LiteralPath "app/assets" -Recurse -Destination (Join-Path $stage "jellyfinrg35xx/assets")
Copy-Item -LiteralPath "app/assets/portmaster-cover-640.png" -Destination (Join-Path $stage "jellyfinrg35xx/cover.png")
Copy-Item -LiteralPath "config/config.example.json" -Destination (Join-Path $stage "jellyfinrg35xx/config.example.json")
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $Output -Force
$archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $Output))
try {
  # Compress-Archive writes backslashes on Windows; ZIP paths are logically
  # slash-separated, so normalise before validating the portable layout.
  $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/').TrimEnd('/') })
} finally {
  $archive.Dispose()
}
$required = @('jellyfinrg35xx/jellyfin_rg35xx.py','jellyfinrg35xx/playback.py','jellyfinrg35xx/library.py','jellyfinrg35xx/music.py','jellyfinrg35xx/downloads.py','jellyfinrg35xx/ui.py','jellyfinrg35xx/config.example.json','jellyfinrg35xx/assets/jellyfin-icon.png','jellyfinrg35xx/assets/portmaster-cover-640.png','jellyfinrg35xx/cover.png','jellyfinrg35xx/gameinfo.xml','Jellyfin RG35XX.sh','jellyfinrg35xx/port.json','jellyfinrg35xx/README.md')
foreach ($item in $required) { if ($entries -notcontains $item) { throw "Invalid package: missing $item" } }
$manifest = Get-Content -LiteralPath (Join-Path $stage 'jellyfinrg35xx/port.json') -Raw | ConvertFrom-Json
$expectedName = [IO.Path]::GetFileName($Output)
if ($manifest.name -ne $expectedName) { throw "Invalid package: port.json name '$($manifest.name)' must equal '$expectedName'" }
$scriptCount = @($entries | Where-Object { $_ -match '^jellyfinrg35xx/jellyfin_rg35xx\.py$' }).Count
if ($scriptCount -ne 1) { throw 'Invalid package: missing Python app' }
Write-Output "Created $Output"
