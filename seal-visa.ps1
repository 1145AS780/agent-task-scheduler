$ErrorActionPreference = 'Stop'

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$RepoDir = $PSScriptRoot
if ($args.Count -gt 0 -and $args[0]) {
    $Root = $args[0]
} elseif ($env:ATS_ROOT) {
    $Root = $env:ATS_ROOT
} else {
    $Root = Join-Path $RepoDir '.ats'
}

$GuardScript = Join-Path $RepoDir 'scripts\ats_guard.py'
$AtsScript = Join-Path $RepoDir 'scripts\ats.py'
$PlanScript = Join-Path $RepoDir 'scripts\ats_plan.py'
$KeyPath = Join-Path $Root 'ats_guard_key.json'
$TasksDir = Join-Path $Root 'tasks'
$SchedulesDir = Join-Path $Root 'schedules'

function Write-Rule {
    Write-Host ''
    Write-Host ('=' * 58) -ForegroundColor DarkGray
}

function Write-Bi {
    param([string]$Zh, [string]$En, [string]$Color = '')
    if ($Zh) {
        if ($Color) { Write-Host $Zh -ForegroundColor $Color } else { Write-Host $Zh }
    }
    if ($En) {
        $enColor = 'DarkGray'
        if ($Color -eq 'Cyan' -or $Color -eq 'Green' -or $Color -eq 'Yellow') { $enColor = $Color }
        Write-Host $En -ForegroundColor $enColor
    }
}

function Exit-With {
    param([int]$Code, [string]$Zh, [string]$En)
    if ($Zh) { Write-Host ''; Write-Host $Zh -ForegroundColor Yellow }
    if ($En) { Write-Host ''; Write-Host $En -ForegroundColor DarkGray }
    Write-Host ''
    Write-Bi '按回车键关闭这个窗口 ...' 'Press Enter to close this window ...' 'DarkGray'
    Read-Host | Out-Null
    exit $Code
}

function Invoke-Guard {
    param([string[]]$GuardArgs, [switch]$Quiet)
    $lines = & python $GuardScript @GuardArgs 2>&1
    $rc = $LASTEXITCODE
    if (-not $Quiet) {
        foreach ($line in $lines) { Write-Host $line }
    }
    return $rc
}

function Quote-Arg {
    param([string]$Value)
    if ($Value -eq '') { return '""' }
    if ($Value -match '^[A-Za-z0-9_.:/=-]+$') { return $Value }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-PyWithPassphrase {
    param(
        [string]$ScriptPath,
        [string[]]$ScriptArgs,
        [string]$Passphrase,
        [switch]$Quiet
    )
    $parts = @('"' + $ScriptPath + '"')
    foreach ($a in $ScriptArgs) { $parts += (Quote-Arg ([string]$a)) }

    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'python'
    $psi.Arguments = ($parts -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
    $psi.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'

    $proc = [Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    $proc.StandardInput.Write($Passphrase)
    $proc.StandardInput.Close()
    $proc.WaitForExit()

    $outText = $outTask.Result
    $errText = $errTask.Result
    if (-not $Quiet) {
        if ($outText) {
            foreach ($line in ($outText -split "`r`n|`n")) { if ($line) { Write-Host $line } }
        }
        if ($errText) {
            foreach ($line in ($errText -split "`r`n|`n")) { if ($line) { Write-Host $line -ForegroundColor DarkGray } }
        }
    }
    return New-Object PSObject -Property @{
        Code = $proc.ExitCode
        Out = $outText
        Err = $errText
    }
}

function Get-TaskIds {
    $ids = @()
    if (Test-Path $TasksDir) {
        foreach ($d in (Get-ChildItem -Path $TasksDir -Directory -ErrorAction SilentlyContinue)) {
            if (Test-Path (Join-Path $d.FullName 'task.json')) { $ids += $d.Name }
        }
    }
    return $ids
}

function Get-PlanIds {
    $ids = @()
    if (Test-Path $SchedulesDir) {
        foreach ($sd in (Get-ChildItem -Path $SchedulesDir -Directory -ErrorAction SilentlyContinue)) {
            $pd = Join-Path $sd.FullName 'plans'
            if (-not (Test-Path $pd)) { continue }
            foreach ($p in (Get-ChildItem -Path $pd -Directory -ErrorAction SilentlyContinue)) {
                if (Test-Path (Join-Path $p.FullName 'plan.json')) { $ids += $p.Name }
            }
        }
    }
    return $ids
}

Write-Rule
Write-Bi '  ATS 快速封签 + 签证' '  ATS quick seal + visa' 'Cyan'
Write-Bi ('  状态目录：{0}' -f $Root) ('  State directory: {0}' -f $Root)
Write-Rule

Write-Host ''
Write-Bi '[1/4] 检查 python ...' '[1/4] Checking python ...'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $zh = @'
[停止] 这台电脑上找不到 python。

请先安装 Python 3，安装时一定要勾选 "Add Python to PATH"，
装好之后重新双击本脚本即可。
'@
    $en = @'
[STOP] python was not found on this computer.

Install Python 3 first, and make sure to tick "Add Python to PATH" during installation.
Then double-click this script again.
'@
    Exit-With 1 $zh $en
}
$verText = (& python --version 2>&1 | Out-String).Trim()
if ($verText -notmatch 'Python\s+3') {
    $zh = @'
[停止] 这台电脑上的 python 不是 Python 3。

ATS 只支持 Python 3。请安装 Python 3，安装时一定要勾选 "Add Python to PATH"。
'@
    $en = @'
[STOP] The python on this computer is not Python 3.

ATS only supports Python 3. Install Python 3 and tick "Add Python to PATH".
'@
    Exit-With 1 $zh $en
}
Write-Host ('      OK  ' + $verText)

Write-Host ''
Write-Bi '[2/4] 看看现在是什么状态 ...' '[2/4] Checking the current state ...'
$verifyRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root)
Write-Host ''

if (-not (Test-Path $KeyPath)) {
    $zh = @'
[停止] 密钥文件不在这里：这个目录还没有建立信任根，或者密钥被删了。

封签和签证都要用到这把钥匙，先双击 setup.bat 把信任根建起来，再回来跑本脚本。
'@
    $en = @'
[STOP] The key file is not here: this directory has no trust root yet, or the key was deleted.

Both sealing and visa issuance need that key. Run setup.bat first, then come back to this script.
'@
    Exit-With 3 $zh $en
}

$needSeal = $false
$sealBlocked = $false

if ($verifyRc -eq 0) {
    Write-Bi '      现在是封好的状态（校验通过），可以只发签证。' '      Everything matches the sealed baseline, so a visa alone is enough.' 'Green'
    $ans = Read-Host '      还要重新封签吗？(y/N)'
    if ($ans -match '^[yY]') { $needSeal = $true }
} elseif ($verifyRc -eq 3) {
    Write-Bi '      密钥还在，但封签清单丢了 —— 只能重新封签。' '      The key is still here but the manifest is gone, so sealing is the only way forward.' 'Yellow'
    $needSeal = $true
} else {
    Write-Bi '      校验没通过：有核心脚本和上次封签对不上（上面 problems 里列的就是）。' '      Verification failed: some core scripts differ from the sealed baseline (see "problems" above).' 'Yellow'
    $ans = Read-Host '      现在重新封签吗？(Y/n)'
    if ($ans -notmatch '^[nN]') {
        $needSeal = $true
    } else {
        $sealBlocked = $true
    }
}

Write-Host ''
$ans = Read-Host '      顺便发一张签证吗？(Y/n)'
$needVisa = $true
if ($ans -match '^[nN]') { $needVisa = $false }

if ((-not $needSeal) -and (-not $needVisa)) {
    Exit-With 0 '[完成] 什么都没改。' '[Done] Nothing was changed.'
}

Write-Host ''
Write-Bi '[3/4] 输入口令 ...' '[3/4] Enter the passphrase ...'
Write-Bi '      就是 setup 时你自己设的那个口令，封签和签证都用它。' '      This is the passphrase you set during setup; both sealing and visa issuance use it.'
Write-Bi '      输入时屏幕上不显示，这是正常的。' '      It is not shown on screen while you type, which is normal.'
Write-Host ''

$secure = Read-Host -AsSecureString -Prompt 'passphrase'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if (-not $plain) {
    Exit-With 2 '[停止] 口令是空的，什么都没有改。' '[STOP] The passphrase is empty. Nothing was changed.'
}

$authRes = Invoke-PyWithPassphrase -ScriptPath $GuardScript -ScriptArgs @('auth', '--root', $Root, '--passphrase-stdin') -Passphrase $plain -Quiet
if ($authRes.Code -ne 0) {
    if ($authRes.Out) { Write-Host $authRes.Out }
    if ($authRes.Err) { Write-Host $authRes.Err -ForegroundColor DarkGray }
    $zh = @'
[停止] 口令没通过校验，什么都没有改。看上面 JSON 里的 "error"：

  passphrase_mismatch   口令和 setup 时设的不一样，重跑一次，输入同一个口令
  key_missing           密钥文件不见了，先跑 setup.bat
'@
    $en = @'
[STOP] The passphrase was rejected. Nothing was changed. Check the "error" field in the JSON above:

  passphrase_mismatch   the passphrase differs from the one used during setup; run again with the same one
  key_missing           the key file is gone; run setup.bat first
'@
    $plain = $null
    Exit-With $authRes.Code $zh $en
}
Write-Host '      OK  口令通过'

Write-Host ''
Write-Bi '[4/4] 干活 ...' '[4/4] Doing the work ...'
Write-Host ''

if ($needSeal) {
    Write-Bi '      正在封签（重建信任基线）...' '      Sealing (rebuilding the trust baseline) ...'
    $sealRes = Invoke-PyWithPassphrase -ScriptPath $GuardScript -ScriptArgs @('seal', '--root', $Root, '--confirm', '--passphrase-stdin') -Passphrase $plain
    if ($sealRes.Code -ne 0) {
        $zh = @'
[失败] 封签没有成功。看上面 JSON 里的 "error"：

  ats_missing        scripts\ats.py 不在本目录，先从仓库把它恢复回来
  passphrase_...     口令问题，重跑本脚本
'@
        $en = @'
[FAILED] Sealing did not succeed. Check the "error" field in the JSON above:

  ats_missing       scripts\ats.py is missing; restore it from the repository first
  passphrase_...    a passphrase problem; run this script again
'@
        $plain = $null
        Exit-With $sealRes.Code $zh $en
    }
    $recheckRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root) -Quiet
    if ($recheckRc -ne 0) {
        $plain = $null
        Exit-With $recheckRc '[失败] 封签跑完了，但复查还有问题。' '[FAILED] Sealing finished, but the re-check still reports problems.'
    }
    Write-Bi '      封签完成，复查通过。' '      Sealed, and the re-check passes.' 'Green'
    $sealBlocked = $false
    Write-Host ''
}

$visaId = ''
if ($needVisa) {
    Write-Bi '      发哪种签证？' '      Which kind of visa?'
    Write-Bi '        1) digest   只跳过完整性校验：核心脚本改过、还没封签时，让命令先能跑起来（审批门照旧）' '        1) digest   only skips the digest check: for when core scripts changed but are not sealed yet (approvals still apply)'
    Write-Bi '        2) task     放行一条已经建好的任务：跳过这条任务的审批（前提是已封签）' '        2) task     clear one existing task: skips approval for that task only (requires a sealed baseline)'
    Write-Bi '        3) plan     放行一整个计划：计划里的任务审批全部跳过（前提是已封签）' '        3) plan     clear a whole plan: skips approval for every task in it (requires a sealed baseline)'
    if ($sealBlocked) {
        Write-Bi '      注意：现在没封签，选 2 / 3 会因为摘要不符被直接拒绝，选 1 才是当下有用的。' '      Note: the baseline is not sealed right now, so 2 / 3 would be rejected on digest mismatch; only 1 makes sense here.' 'Yellow'
    } elseif ($needSeal) {
        Write-Bi '      刚封完签，digest 签证已经用不上了，建议选 2 或 3。' '      The baseline is freshly sealed, so digest is pointless now; prefer 2 or 3.' 'Yellow'
    }
    Write-Host ''
    $pick = (Read-Host '      输入 1 / 2 / 3（回车 = 1）').Trim()
    if (-not $pick) { $pick = '1' }

    $taskId = ''
    $planId = ''
    if ($pick -eq '2') {
        Write-Host ''
        $taskIds = @(Get-TaskIds)
        if ($taskIds.Count -gt 0) {
            Write-Bi ('      现有任务：{0}' -f ($taskIds -join ', ')) ('      Existing tasks: {0}' -f ($taskIds -join ', '))
        } else {
            Write-Bi '      现在一条任务都没有，先用 ats.py create 建一条。' '      There are no tasks yet; create one with ats.py create first.'
        }
        Write-Host ''
        $taskId = (Read-Host '      task id').Trim()
        if (-not $taskId) {
            $plain = $null
            Exit-With 2 '[停止] task id 是空的，没有签发签证。' '[STOP] The task id is empty. No visa was issued.'
        }
    } elseif ($pick -eq '3') {
        Write-Host ''
        $planIds = @(Get-PlanIds)
        if ($planIds.Count -gt 0) {
            Write-Bi ('      现有计划：{0}' -f ($planIds -join ', ')) ('      Existing plans: {0}' -f ($planIds -join ', '))
        } else {
            Write-Bi '      现在一个计划都没有，先建一个计划再来。' '      There are no plans yet; create one first.'
        }
        Write-Host ''
        $planId = (Read-Host '      plan id').Trim()
        if (-not $planId) {
            $plain = $null
            Exit-With 2 '[停止] plan id 是空的，没有签发签证。' '[STOP] The plan id is empty. No visa was issued.'
        }
    } else {
        $pick = '1'
    }

    Write-Host ''
    $mins = (Read-Host '      有效期多少分钟？(回车 = 30)').Trim()
    if (-not $mins) { $mins = '30' }
    if (($mins -notmatch '^\d+$') -or ([int]$mins -lt 1)) {
        $plain = $null
        Exit-With 2 '[停止] 有效期必须是正整数（分钟），没有签发签证。' '[STOP] The lifetime must be a positive whole number of minutes. No visa was issued.'
    }

    if ($pick -eq '3') {
        $cnt = (Read-Host '      能用几次？(回车 = unlimited，计划跑完就散)').Trim()
        if (-not $cnt) { $cnt = 'unlimited' }
    } else {
        $cnt = (Read-Host '      能用几次？(回车 = 1)').Trim()
        if (-not $cnt) { $cnt = '1' }
    }

    $note = (Read-Host '      备注（写给自己看的，可空）').Trim()

    $visaArgs = @('--root', $Root, 'visa', 'issue', '--time', $mins, '--number', $cnt)
    if ($pick -eq '2') { $visaArgs += @('--task', $taskId) }
    if ($pick -eq '3') { $visaArgs += @('--plan', $planId) }
    if ($note) { $visaArgs += @('--note', $note) }
    $visaArgs += '--passphrase-stdin'

    Write-Host ''
    Write-Bi '      正在签发签证 ...' '      Issuing the visa ...'
    $visaRes = Invoke-PyWithPassphrase -ScriptPath $AtsScript -ScriptArgs $visaArgs -Passphrase $plain -Quiet

    $visaJson = $null
    try { $visaJson = $visaRes.Out | ConvertFrom-Json } catch { $visaJson = $null }
    if (($visaRes.Code -ne 0) -or ($null -eq $visaJson) -or (-not $visaJson.ok)) {
        if ($visaRes.Out) { Write-Host $visaRes.Out }
        if ($visaRes.Err) { Write-Host $visaRes.Err -ForegroundColor DarkGray }
        $zh = @'
[失败] 签证没有签发成功。看上面 JSON 里的 "error"：

  task_not_found      task id 写错了，或者那条任务还没建
  plan_not_found      plan id 写错了
  plan_spec_invalid   这个计划的 DAG 本身不合法，先修好再签
  bad_number          配额 / 有效期不合法（unlimited 只能用在 plan 上）
'@
        $en = @'
[FAILED] The visa was not issued. Check the "error" field in the JSON above:

  task_not_found     the task id is wrong, or the task was never created
  plan_not_found     the plan id is wrong
  plan_spec_invalid  the plan DAG itself is invalid; fix it before signing
  bad_number         the quota / lifetime is invalid (unlimited only works for plan)
'@
        $plain = $null
        Exit-With $visaRes.Code $zh $en
    }

    $visaId = [string]$visaJson.visa_id
    Write-Host ''
    Write-Bi ('      签证 ID：{0}' -f $visaId) ('      Visa id: {0}' -f $visaId) 'Green'
    Write-Bi ('      作用域：{0}' -f $visaJson.scope_type) ('      Scope: {0}' -f $visaJson.scope_type)
    Write-Bi ('      配额：{0}' -f $visaJson.number) ('      Quota: {0}' -f $visaJson.number)
    Write-Bi ('      到期：{0}' -f $visaJson.expires_iso) ('      Expires: {0}' -f $visaJson.expires_iso)
    if ($visaJson.note) {
        Write-Bi ('      备注：{0}' -f $visaJson.note) ('      Note: {0}' -f $visaJson.note)
    }
}

$plain = $null

Write-Rule
Write-Bi '  [完成]' '  [Done]' 'Green'
Write-Rule
Write-Host ''

if ($visaId) {
    $runLine = 'python scripts\ats.py run <task_id> --visa-id ' + $visaId
    $envLine = '$env:ATS_VISA_ID = "' + $visaId + '"'
    $revokeLine = 'python scripts\ats.py visa revoke ' + $visaId
    Write-Bi ('  拿这张签证去跑：    {0}' -f $runLine) ('  {0} {1}' -f 'Use the visa:', $runLine)
    Write-Bi ('  或者设环境变量：    {0}' -f $envLine) ('  {0} {1}' -f 'Or set an env var:', $envLine)
    Write-Bi ('  反悔就撤销：        {0}' -f $revokeLine) ('  {0} {1}' -f 'Revoke it:', $revokeLine)
}
Write-Bi ('  看所有签证：        {0}' -f 'python scripts\ats.py visa list') ('  {0} {1}' -f 'List visas:', 'python scripts\ats.py visa list')
Write-Host ''
Write-Bi '  核心脚本再改过，还要重新跑本脚本（封签 + 重新签证）。' '  When the core scripts change again, run this script again (seal + new visa).'

Exit-With 0 ''