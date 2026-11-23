$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'VirtualDesktop.psd1') -Force -WarningAction SilentlyContinue

$last = -1
while ($true) {
  try {
    $index = Get-DesktopIndex (Get-CurrentDesktop)
  } catch {
    Start-Sleep -Milliseconds 500
    continue
  }
  if ($index -ne $last) {
    $last = $index
    [Console]::WriteLine("INDEX=$index")
    [Console]::Out.Flush()
  }
  Start-Sleep -Milliseconds 500
}
