Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | ForEach-Object { Write-Output "$($_.Id) | $($_.MainWindowTitle)" }
