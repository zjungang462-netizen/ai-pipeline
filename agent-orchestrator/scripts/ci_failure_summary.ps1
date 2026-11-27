param([Parameter(Mandatory = $true)][string]$Evidence)
# 把验收回执里的失败与错误用例打印到作业日志，免得必须下载产物才能定位。
$ErrorActionPreference = 'Continue'
$proof = Join-Path $Evidence 'verification.json'
if (Test-Path $proof) {
    $data = Get-Content $proof -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "tests_run=$($data.tests_run) failures=$(@($data.failures).Count) errors=$(@($data.errors).Count) duration_s=$($data.duration_s)"
    foreach ($item in @($data.failures) + @($data.errors)) {
        Write-Host "FAILED $($item.test)"
        $lines = @("$($item.traceback)" -split "`n")
        $lines | Where-Object { $_.Trim() } | Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" }
    }
}
$log = Join-Path $Evidence 'quick-regression.log'
if (-not (Test-Path $log)) { $log = Join-Path $Evidence 'full-regression.log' }
if (Test-Path $log) {
    Write-Host '--- regression log tail ---'
    Get-Content $log -Tail 40 -Encoding UTF8 | ForEach-Object { Write-Host $_ }
}
exit 0
