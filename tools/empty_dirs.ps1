# Remove folders that are completely empty.
#
# Deliberately not part of the pipeline.  jamp promises never to delete
# anything - that guarantee is worth more than the convenience of folding this
# in, and it is why --unnest and --include-merges leave their emptied folders
# behind.  This is the separate, explicit step that clears them.
#
#   .\tools\empty_dirs.ps1 -Root "E:\Audio\Music\Live Music\Phish"            preview
#   .\tools\empty_dirs.ps1 -Root "E:\Audio\Music\Live Music\Phish" -Commit    actually delete
#
# -Root is required.  It used to default to a library location from before the
# library moved, which was outside the folder the pipeline may touch.
#
# "Empty" means nothing at all inside: no files of any kind, zips included, no
# subfolders, nothing hidden.  Checked again immediately before each delete,
# and Remove-Item is called without -Recurse, so the filesystem itself refuses
# anything that is not empty - whatever this script believes.  A folder it
# cannot read is reported, never treated as empty.
#
# Folders holding files but no audio are listed and never touched: a wrapper's
# info file may be the only description of the recording that moved out of it.

param(
  [Parameter(Mandatory = $true)][string]$Root,
  [switch]$Commit
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Root)) { Write-Host "no such folder: $Root" -ForegroundColor Red; exit 1 }

# "Empty" means the folder contains NOTHING: no files of any kind (zips
# included), no subfolders, nothing hidden.  Checked again immediately before
# each delete, so a parent only goes once its children actually have.
# The unary comma is load-bearing.  PowerShell unrolls an array on the way out
# of a function, and an EMPTY array unrolls to nothing at all - so "return @()"
# handed back $null, every genuinely empty folder was reported as unreadable,
# and this script could never delete the one thing it exists to delete.  ",@()"
# returns the array itself.  $null still means unreadable, and unreadable is
# still not empty.
function Get-Entries($path) {
  try   { return ,@(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop) }
  catch { return $null }          # unreadable is NOT empty
}

Write-Host "scanning $Root" -ForegroundColor Cyan
$all = Get-ChildItem -LiteralPath $Root -Directory -Recurse -Force
# Deepest first, so a folder of empty folders empties from the bottom up.
$all = $all | Sort-Object { $_.FullName.Split('\').Count } -Descending

$empty = @(); $held = @(); $unreadable = @()
foreach ($d in $all) {
  $e = Get-Entries $d.FullName
  if ($null -eq $e)      { $unreadable += $d }
  elseif ($e.Count -eq 0) { $empty += $d }
  else {
    $files = @($e | Where-Object { -not $_.PSIsContainer })
    if ($files.Count -gt 0) {
      # Audio ANYWHERE beneath, not just sitting here: a folder whose discs
      # live in subfolders "1" and "2" holds plenty of audio, and calling it
      # "no audio" reads like a deletion candidate when it is nothing of the
      # kind.
      $audio = @(Get-ChildItem -LiteralPath $d.FullName -File -Recurse -Force -ErrorAction SilentlyContinue |
                 Where-Object { $_.Extension -match '^\.(flac|mp3|m4a|wav|shn|ape|wv|ogg|aiff?|wma)$' })
      if ($audio.Count -eq 0) { $held += [pscustomobject]@{ Dir = $d; Files = $files } }
    }
  }
}

Write-Host ""
Write-Host ("COMPLETELY EMPTY - nothing at all inside: " + $empty.Count) -ForegroundColor Green
foreach ($d in $empty) { Write-Host ("   " + $d.FullName.Replace($Root, '.')) }

Write-Host ""
Write-Host ("HOLDS FILES BUT NO AUDIO - never deleted, look yourself: " + $held.Count) -ForegroundColor Yellow
foreach ($h in $held) {
  Write-Host ("   " + $h.Dir.FullName.Replace($Root, '.'))
  foreach ($f in ($h.Files | Select-Object -First 4)) {
    Write-Host ("        " + $f.Name + "  (" + $f.Length + " bytes)") -ForegroundColor DarkGray
  }
}
if ($unreadable.Count -gt 0) {
  Write-Host ""
  Write-Host ("COULD NOT BE READ - treated as NOT empty: " + $unreadable.Count) -ForegroundColor Red
  foreach ($d in $unreadable) { Write-Host ("   " + $d.FullName.Replace($Root, '.')) }
}

Write-Host ""
if ($Commit) {
  $removed = 0
  foreach ($d in $empty) {
    if (-not (Test-Path -LiteralPath $d.FullName)) { continue }
    $again = Get-Entries $d.FullName
    if ($null -eq $again -or $again.Count -ne 0) {
      Write-Host ("   SKIPPED (no longer empty): " + $d.Name) -ForegroundColor Yellow
      continue
    }
    # No -Recurse on purpose: Remove-Item then physically cannot delete a
    # folder that has anything in it, whatever this script believes.
    Remove-Item -LiteralPath $d.FullName -Force
    $removed++
  }
  Write-Host ("removed " + $removed + " empty folders; nothing else was touched.") -ForegroundColor Green
} else {
  Write-Host "PREVIEW ONLY - nothing deleted. Re-run with -Commit." -ForegroundColor Yellow
}
