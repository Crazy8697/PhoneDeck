# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
# Build PhoneDeck (PyInstaller onedir: dist\PhoneDeck\PhoneDeck.exe) and its Windows installer.
#   .\build.ps1                  build only (dist\ and build\ are gitignored)
#   .\build.ps1 -Setup           build, then package dist\PhoneDeck as PhoneDeck-Setup-<version>.exe (+ .sha256,
#                                + the same installer as PhoneDeck-Setup.exe, the constant name for the updater
#                                and download links) with Inno Setup 6 (ISCC; a build tool only) into -SetupDir
#   .\build.ps1 -Setup -NoBuild  package the existing dist\PhoneDeck without rebuilding
#   .\build.ps1 -Icon            first rebuild phonedeck.ico + assets\icon\phonedeck-icon-512.png from the
#                                SVGs in assets\icon (make_icon.py; needs Inkscape), then build as usual
#   .\build.ps1 -PrivacyCheck    run tools\privacy_check.py on the tree (and on dist\PhoneDeck if built), nothing else
#   .\build.ps1 -Release -Version x.y.z
#                                publish, the Lazarus PDF way. Refuses unless the tree is clean in git, x.y.z is
#                                phonedeck.VERSION, installer\release-notes\vx.y.z.md exists, the release and the tag
#                                don't exist yet and tools\privacy_check.py finds nothing in the tree; then builds the
#                                Setup and runs the privacy check again on the frozen build (dist\PhoneDeck), refusing
#                                on any finding. Then:
#                                  1. -PublicSync: one new public commit "PhoneDeck x.y.z", plain push,
#                                  2. release vx.y.z on Crazy8697/PhoneDeck on that commit (draft, assets uploaded in
#                                     this order: PhoneDeck-Setup-x.y.z.exe, its .sha256, PhoneDeck-Setup.exe; then
#                                     published as latest, body from the release notes),
#                                  3. tag vx.y.z on the private commit, pushed to Forgejo only.
#                                winget (Stage 13) slots in after it.
#   .\build.ps1 -PublicSync [-FreshPublicHistory] [-NoPush] [-PublicDir <folder>]
#                                the public source path (the only route to GitHub): refuses unless the tree is clean
#                                in git; exports exactly the committed tree at HEAD (tools\public_export.py: git
#                                archive, a forbidden-path fence, and the privacy check on that exact tree); copies it
#                                into the public clone in -PublicDir and adds ONE commit there, authored by the GitHub
#                                noreply identity, never this machine's; pushes it to Crazy8697/PhoneDeck main
#                                (plain push, never forced). -FreshPublicHistory is for the very first sync only: it
#                                starts the public clone with git init instead of cloning, and refuses if the GitHub
#                                repo already has a main branch. The private working copy (its branches, tags, remotes
#                                and tracking) is never touched: the public clone is a separate folder.
# Needs a Python 3.11 venv with requirements.txt + requirements-build.txt installed (PHONEDECK_PY = its
# python.exe if it isn't .venv here). scrcpy + adb are bundled from SCRCPY_DIR (see phonedeck.spec).
param(
    [switch]$Setup,
    [switch]$Icon,
    [switch]$NoBuild,
    [switch]$PrivacyCheck,
    [switch]$Release,
    [string]$Version = "",
    [string]$SetupDir = (Join-Path $PSScriptRoot "dist\installer"),
    [switch]$PublicSync,
    [switch]$FreshPublicHistory,
    [switch]$NoPush,
    [string]$PublicDir = (Join-Path $env:LOCALAPPDATA "PhoneDeck-public\PhoneDeck")
)
$ErrorActionPreference = "Stop"
$PublicRepo = "Crazy8697/PhoneDeck"
$PublicUrl = "git@github.com:$PublicRepo.git"
$PublicName = "Crazy8697"
$PublicEmail = "261646346+Crazy8697@users.noreply.github.com"

# ---- the public source sync: one commit on the public history, from exactly the committed tree ----
function Sync-Public([string]$ver, [string]$py, [switch]$Fresh, [switch]$NoPush) {
    $dirty = git -C $PSScriptRoot status --porcelain
    if ($LASTEXITCODE -ne 0) { throw "git status failed" }
    if ($dirty) { throw "The PhoneDeck tree isn't clean in git; commit (to Forgejo) first." }

    $stage = Join-Path $env:TEMP ("phonedeck-public-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    try {
        & $py (Join-Path $PSScriptRoot "tools\public_export.py") $stage
        if ($LASTEXITCODE -ne 0) { throw "The public export refused (forbidden file or privacy finding); nothing was published." }
        if ($Fresh) {
            if (Test-Path (Join-Path $PublicDir ".git")) { throw "-FreshPublicHistory: $PublicDir already holds a public clone." }
            # (through cmd: PowerShell 5.1 turns a redirected native stderr into an error under "Stop")
            $remoteMain = (cmd /c "git ls-remote --heads $PublicUrl main 2>nul") -join ""
            if ($remoteMain) { throw "-FreshPublicHistory: $PublicRepo already has a main branch; refusing (never force-push)." }
            New-Item -ItemType Directory -Force $PublicDir | Out-Null
            git -C $PublicDir init -q -b main
            git -C $PublicDir remote add origin $PublicUrl
        } elseif (-not (Test-Path (Join-Path $PublicDir ".git"))) {
            New-Item -ItemType Directory -Force (Split-Path $PublicDir) | Out-Null
            git clone -q $PublicUrl $PublicDir
            if ($LASTEXITCODE -ne 0) { throw "Couldn't clone $PublicRepo" }
        } else {
            git -C $PublicDir pull -q --ff-only origin main
            if ($LASTEXITCODE -ne 0) { throw "Couldn't update the public clone" }
        }
        # replace the clone's files with exactly the exported tree
        git -C $PublicDir rm -r -q -f --ignore-unmatch -- . | Out-Null
        Get-ChildItem $PublicDir -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force
        Copy-Item (Join-Path $stage "*") $PublicDir -Recurse -Force
        git -C $PublicDir add -A
        $changes = git -C $PublicDir status --porcelain
        if (-not $changes) {
            $script:PublicCommit = (git -C $PublicDir rev-parse HEAD).Trim()
            Write-Host "Public source: nothing changed since the last sync ($script:PublicCommit)."
            return
        }
        # (the private history's hashes stay private: the message names the version only)
        $msg = "PhoneDeck $ver"
        git -C $PublicDir -c "user.name=$PublicName" -c "user.email=$PublicEmail" commit -q -m $msg
        if ($LASTEXITCODE -ne 0) { throw "Couldn't commit in the public clone" }
        $commit = (git -C $PublicDir rev-parse HEAD).Trim()
        $script:PublicCommit = $commit
        if ($NoPush) { Write-Host "Public source committed locally (not pushed): $commit in $PublicDir"; return }
        git -C $PublicDir push -q -u origin main
        if ($LASTEXITCODE -ne 0) { throw "Couldn't push the public source" }
        Write-Host "Public source pushed: $PublicRepo main = $commit"
    } finally {
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    }
}
# Python: $env:PHONEDECK_PY, else .venv next to this script, else python on PATH
$py = if ($env:PHONEDECK_PY) { $env:PHONEDECK_PY }
      elseif (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")) { Join-Path $PSScriptRoot ".venv\Scripts\python.exe" }
      else { "python" }

Push-Location $PSScriptRoot
try {
    $ver = (Select-String -Path (Join-Path $PSScriptRoot "phonedeck.py") -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
    if (-not $ver) { throw "VERSION not found in phonedeck.py" }

    $privacy = Join-Path $PSScriptRoot "tools\privacy_check.py"
    if ($PrivacyCheck) {
        $pargs = @($privacy)
        if (Test-Path (Join-Path $PSScriptRoot "dist\PhoneDeck\PhoneDeck.exe")) { $pargs += "--dist" }
        & $py @pargs
        if ($LASTEXITCODE -ne 0) { throw "The privacy check found something." }
        return
    }
    if ($PublicSync) {
        Sync-Public $ver $py -Fresh:$FreshPublicHistory -NoPush:$NoPush
        return
    }
    if ($FreshPublicHistory -or $NoPush) { throw "-FreshPublicHistory and -NoPush go with -PublicSync." }
    if ($Release) {
        if ($Version -ne $ver) { throw "-Version '$Version' doesn't match phonedeck.VERSION ($ver)." }
        $dirty = git -C $PSScriptRoot status --porcelain
        if ($LASTEXITCODE -ne 0) { throw "git status failed" }
        if ($dirty) { throw "The PhoneDeck tree isn't clean in git; commit (to Forgejo) first." }
        & $py $privacy
        if ($LASTEXITCODE -ne 0) { throw "The privacy check found something in the tree; nothing was built or published." }
        if ($NoBuild) { throw "-Release always builds fresh." }
        $notes = Join-Path $PSScriptRoot "installer\release-notes\v$ver.md"
        if (-not (Test-Path $notes)) { throw "No release notes: $notes" }
        # (through cmd: PowerShell 5.1 turns a redirected native stderr into an error under "Stop")
        cmd /c "gh release view v$ver --repo $PublicRepo >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { throw "Release v$ver already exists on $PublicRepo." }
        if (git -C $PSScriptRoot tag --list "v$ver") { throw "Tag v$ver already exists in the private repo." }
        $Setup = $true
    }

    if ($Icon) {
        & $py (Join-Path $PSScriptRoot "make_icon.py")
        if ($LASTEXITCODE -ne 0) { throw "make_icon.py failed ($LASTEXITCODE)" }
    }

    $dist = Join-Path $PSScriptRoot "dist\PhoneDeck"
    if (-not $NoBuild) {
        & $py -m PyInstaller --noconfirm --clean phonedeck.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    }
    if (-not (Test-Path (Join-Path $dist "PhoneDeck.exe"))) { throw "No build in $dist" }
    $mb = [math]::Round((Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "Built: $dist\PhoneDeck.exe  ($mb MB, version $ver)"

    if ($Setup) {
        # Inno Setup's compiler: $env:PHONEDECK_ISCC, else where its installer registered it
        $iscc = $env:PHONEDECK_ISCC
        if (-not $iscc) {
            foreach ($k in "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                           "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1") {
                $loc = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallLocation
                if ($loc -and (Test-Path (Join-Path $loc "ISCC.exe"))) { $iscc = Join-Path $loc "ISCC.exe"; break }
            }
        }
        if (-not $iscc -or -not (Test-Path $iscc)) { throw "Inno Setup 6 (ISCC.exe) not found; set PHONEDECK_ISCC" }
        New-Item -ItemType Directory -Force $SetupDir | Out-Null
        & $iscc /Q "/DAppVersion=$ver" "/DDistDir=$dist" "/O$SetupDir" (Join-Path $PSScriptRoot "installer\phonedeck.iss")
        if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
        $base = "PhoneDeck-Setup-$ver.exe"
        $out = Join-Path $SetupDir $base
        $hash = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
        Set-Content -Path "$out.sha256" -Value "$hash *$base" -Encoding ascii -NoNewline
        # the constant-named copy (README download button / updater, as Lazarus PDF does it)
        Copy-Item $out (Join-Path $SetupDir "PhoneDeck-Setup.exe") -Force
        $smb = [math]::Round((Get-Item $out).Length / 1MB, 1)
        Write-Host "Setup: $out  ($smb MB, version $ver)"
        Write-Host "SHA-256: $hash"
    }

    if ($Release) {
        & $py $privacy --dist
        if ($LASTEXITCODE -ne 0) { throw "The privacy check found something in the build; nothing was published." }
        Write-Host "Release gate passed for $ver."

        # 1. the public source: one new commit "PhoneDeck <ver>" on the public history (plain push)
        $script:PublicCommit = $null
        Sync-Public $ver $py
        if (-not $script:PublicCommit) { throw "The public sync didn't report its commit; nothing was released." }

        # 2. the release, on that commit. Created as a draft so the assets go up one at a time in a fixed
        #    order (the 1.4.0 updater takes the FIRST .exe: the versioned installer must be first; the new
        #    updater wants its .sha256), then published as the latest release in one step, so no one ever
        #    sees a published release without its checksum.
        $plain = Join-Path $SetupDir "PhoneDeck-Setup.exe"
        $assets = @($out, "$out.sha256", $plain)
        gh release create "v$ver" $assets[0] --repo $PublicRepo --target $script:PublicCommit `
            --title "PhoneDeck $ver" --notes-file $notes --draft
        if ($LASTEXITCODE -ne 0) { throw "gh release create failed ($LASTEXITCODE)" }
        foreach ($a in $assets[1..2]) {
            gh release upload "v$ver" $a --repo $PublicRepo
            if ($LASTEXITCODE -ne 0) { throw "Uploading $a failed; the release is still a draft on $PublicRepo." }
        }
        $names = (gh release view "v$ver" --repo $PublicRepo --json assets --jq ".assets[].name") -join ","
        $want = (($assets | ForEach-Object { Split-Path $_ -Leaf }) -join ",")
        if ($names -ne $want) { throw "Asset order is '$names', not '$want'; the release is still a draft." }
        gh release edit "v$ver" --repo $PublicRepo --draft=false --latest
        if ($LASTEXITCODE -ne 0) { throw "Publishing the draft failed; it is still a draft on $PublicRepo." }
        Write-Host "Released v$ver on $PublicRepo (public commit $script:PublicCommit), assets: $names"

        # 3. the same tag on the private commit, to Forgejo only
        git -C $PSScriptRoot tag "v$ver"
        git -C $PSScriptRoot push -q origin "refs/tags/v$ver"
        if ($LASTEXITCODE -ne 0) { throw "Pushing tag v$ver to Forgejo failed (the GitHub release is out)." }
        Write-Host "Tagged v$ver on the private commit and pushed the tag to Forgejo."
    }
} finally { Pop-Location }
