#!/usr/bin/env pwsh
# 端到端演示：Task → Plan → 依赖/重试 → 审批 → 周期 Schedule
# 用法： powershell -ExecutionPolicy Bypass -File reference\examples\run-examples.ps1
# 说明： 所有状态写在 $env:ATS_ROOT（默认 <skill_root>\.ats-examples），不触碰项目 .ats

$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Ats       = Join-Path $SkillRoot "scripts\ats.py"
$AtsPlan   = Join-Path $SkillRoot "scripts\ats_plan.py"

if (-not $env:ATS_ROOT) {
    $env:ATS_ROOT = Join-Path $SkillRoot ".ats-examples"
}

Write-Host "skill root : $SkillRoot"
Write-Host "ATS_ROOT   : $env:ATS_ROOT"
Write-Host ""

# ---------------------------------------------------------------- 工具函数

function Invoke-Ats {
    param([string[]]$Arguments, [switch]$Plan)
    $exe = if ($Plan) { $AtsPlan } else { $Ats }
    $raw  = & python $exe @Arguments
    $code = $LASTEXITCODE
    $data = $null
    if (-not $raw) { return [pscustomobject]@{ Code = $code; Data = $null } }
    try { $data = ($raw -join "`n") | ConvertFrom-Json } catch { $data = $null }
    return [pscustomobject]@{ Code = $code; Data = $data }
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 72)
    Write-Host $Title
    Write-Host ("=" * 72)
}

function Show-Result {
    param($Result, [int]$MaxLength = 400)
    $json = $Result.Data | ConvertTo-Json -Depth 6 -Compress
    if (-not $json) { $json = "<no json>" }
    if ($json.Length -gt $MaxLength) { $json = $json.Substring(0, $MaxLength) + "..." }
    Write-Host ("exit={0}  {1}" -f $Result.Code, $json)
}

# 清理上次演示留下的实体（失败忽略）
function Reset-Example {
    $env:ATS_ACTOR_LEVEL = "L3"
    foreach ($ident in @("ci-lint-test", "dep-retry", "gated", "patrol-sample")) {
        Invoke-Ats -Arguments @("approve", "--all") | Out-Null
        # 第一次 delete 落在 plan 上，第二次才落在 schedule 上
        Invoke-Ats -Plan -Arguments @("delete", $ident) 2>$null | Out-Null
        Invoke-Ats -Plan -Arguments @("delete", $ident) 2>$null | Out-Null
    }
    Invoke-Ats -Arguments @("delete", "build1") 2>$null | Out-Null
}

Reset-Example
Push-Location $SkillRoot

try {
    # ------------------------------------------------------------ 1. 策略
    Write-Section "1. 查看生效策略"
    $r = Invoke-Ats -Arguments @("policy", "--json")
    Show-Result $r 300

    # ------------------------------------------------------------ 2. Task 层
    Write-Section "2. Task：create → run → check → show"
    $r = Invoke-Ats -Arguments @("create", "--spec", "reference\examples\task-spec.json")
    Show-Result $r 200

    $r = Invoke-Ats -Arguments @("run", "build1", "--wait")
    Write-Host ("task status = {0}, exit_code = {1}" -f $r.Data.task.status, $r.Data.task.exit_code)

    $r = Invoke-Ats -Arguments @("show", "build1", "--lines", "10")
    Write-Host ("stdout tail = {0}" -f $r.Data.stdout_tail)

    # ------------------------------------------------------------ 3. 一次性计划
    Write-Section "3. Plan：提交 + 执行（两个 Stage）"
    $r = Invoke-Ats -Plan -Arguments @("submit_plan", "--spec", "reference\examples\plan-oneshot.json")
    Show-Result $r 240

    $r = Invoke-Ats -Plan -Arguments @("run_plan", "ci-lint-test-a1")
    Write-Host ("plan status = {0}" -f $r.Data.status)
    Write-Host ("counts      = {0}" -f ($r.Data.counts | ConvertTo-Json -Compress))
    Write-Host ("run_dir     = {0}" -f $r.Data.run_dir)

    # ------------------------------------------------------------ 4. 依赖与重试
    Write-Section "4. Plan：depends_on 失败传播 + retry"
    $r = Invoke-Ats -Plan -Arguments @("submit_plan", "--spec", "reference\examples\plan-deps-retry.json")
    Show-Result $r 240

    $r = Invoke-Ats -Plan -Arguments @("run_plan", "dep-retry-a1")
    Write-Host ("plan status = {0}" -f $r.Data.status)
    Write-Host ("counts      = {0}" -f ($r.Data.counts | ConvertTo-Json -Compress))

    $r = Invoke-Ats -Plan -Arguments @("show", "dep-retry-a1", "--json")
    foreach ($step in $r.Data.summary.steps) {
        Write-Host ("  {0,-12} {1,-10} attempts={2} skipped_by={3} blocked_by={4}" -f `
            $step.step_id, $step.status, $step.attempts, $step.skipped_by, $step.blocked_by)
    }

    # ------------------------------------------------------------ 5. 审批闭环
    Write-Section "5. 审批：L0 身份被拦 → L3 批准 → 恢复"
    $env:ATS_ACTOR_LEVEL = "L0"

    $r = Invoke-Ats -Plan -Arguments @("submit_plan", "--spec", "reference\examples\plan-approval-gated.json")
    Show-Result $r 240

    $r = Invoke-Ats -Plan -Arguments @("run_plan", "gated-a1")
    Write-Host ("plan status = {0}" -f $r.Data.status)
    $apId = $null
    if ($r.Data.approval) {
        $apId = $r.Data.approval.id
        Write-Host ("approval_id = {0}" -f $apId)
        Write-Host ("required    = {0}" -f $r.Data.approval.required_level)
    }

    if ($apId) {
        $env:ATS_ACTOR_LEVEL = "L3"
        $r = Invoke-Ats -Arguments @("approve", $apId, "--note", "示例批准")
        Write-Host ("approve -> {0}" -f $r.Data.status)

        $r = Invoke-Ats -Plan -Arguments @("resume", "gated", "--approval", $apId)
        Write-Host ("resume  -> {0}" -f $r.Data.status)
        Write-Host ("counts  = {0}" -f ($r.Data.counts | ConvertTo-Json -Compress))
    }

    # ------------------------------------------------------------ 6. 周期计划
    Write-Section "6. Schedule：every=5 / max_runs=3 / start_at=0"
    $env:ATS_ACTOR_LEVEL = "L3"

    $r = Invoke-Ats -Plan -Arguments @("submit_schedule", "--spec", "reference\examples\schedule-recurring.json")
    Show-Result $r 240

    for ($i = 1; $i -le 3; $i++) {
        $r = Invoke-Ats -Plan -Arguments @("check_schedule", "patrol-sample")
        $status    = $r.Data.status
        $triggered = ($r.Data.triggered -join ",")
        $nextPoll  = $r.Data.next_poll
        Write-Host ("poll {0}: status={1} triggered=[{2}] runs={3} next_poll={4}" -f `
            $i, $status, $triggered, $r.Data.runs, $nextPoll)
        if ($nextPoll -gt 0) { Start-Sleep -Seconds ([math]::Min($nextPoll, 6)) }
    }

    # ------------------------------------------------------------ 7. 收尾查询
    Write-Section "7. 历史与日志"
    $r = Invoke-Ats -Plan -Arguments @("history", "ci-lint-test", "--logs", "--lines", "5")
    Write-Host ("runs = {0}" -f $r.Data.count)
    foreach ($run in $r.Data.runs) {
        Write-Host ("  {0,-16} status={1,-8} counts={2}" -f `
            $run.run, $run.status, ($run.counts | ConvertTo-Json -Compress))
    }

    $lines = & python $Ats logs --lines 8
    $lines | ForEach-Object { Write-Host "  $_" }

    Write-Section "完成"
    Write-Host "全部样例已执行。清理方式："
    Write-Host ("  Remove-Item -Recurse -Force '{0}'" -f $env:ATS_ROOT)
}
finally {
    Pop-Location
    Remove-Item Env:\ATS_ACTOR_LEVEL -ErrorAction SilentlyContinue
}
