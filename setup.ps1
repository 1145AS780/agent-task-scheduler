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
$KeyPath = Join-Path $Root 'ats_guard_key.json'
$ManifestPath = Join-Path $Root 'ats_guard_manifest.json'

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

Write-Rule
Write-Bi '  ATS 一键设置' '  ATS one-click setup' 'Cyan'
Write-Bi ('  状态目录：{0}' -f $Root) ('  State directory: {0}' -f $Root)
Write-Rule

Write-Host ''
Write-Bi '[1/3] 检查 python ...' '[1/3] Checking python ...'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $zh = @'
[停止] 这台电脑上找不到 python。

请先安装 Python 3，安装时一定要勾选 "Add Python to PATH"。
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

ATS 只支持 Python 3。请安装 Python 3，安装时一定要勾选 "Add Python to PATH"，
装好之后重新双击本脚本即可。
'@
    $en = @'
[STOP] The python on this computer is not Python 3.

ATS only supports Python 3. Install Python 3 and make sure to tick "Add Python to PATH"
during installation, then double-click this script again.
'@
    Exit-With 1 $zh $en
}
Write-Host ('      OK  ' + $verText)

Write-Host ''
Write-Bi '[2/3] 检查是否已经设置好 ...' '[2/3] Checking whether it is already set up ...'
$verifyRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root) -Quiet
if ($verifyRc -eq 0) {
    Write-Host ''
    Write-Bi '      [完成] 这个目录已经设置好了，什么都不用做。' '      [Done] This directory is already set up. Nothing to do.' 'Green'
    Write-Bi '             想直接试试：python scripts\ats.py list' '             Try it right away: python scripts\ats.py list'
    Exit-With 0 ''
}
Write-Bi '      还没有设置好，继续第 3 步。' '      Not set up yet. Continuing to step 3.'

if ((-not (Test-Path $KeyPath)) -and (Test-Path $ManifestPath)) {
    $zh = @'
[停止] 密钥文件不见了，但清单还在。

这种情况本脚本不处理。请从 git 恢复这两个文件：
  ats_guard_key.json
  ats_guard_manifest.json

确认要推倒重来，就由人手动删掉这两个文件，再跑一次本脚本。
'@
    $en = @'
[STOP] The key file is gone, but the manifest is still there.

This script does not handle that case. Restore both files from git:
  ats_guard_key.json
  ats_guard_manifest.json

If you really want to start over, delete both files by hand and run this script again.
'@
    Exit-With 4 $zh $en
}

Write-Host ''
Write-Bi '[3/3] 设置信任根' '[3/3] Setting up the trust root'
Write-Host ''
Write-Bi '      接下来会让你输入一个口令。它像一把钥匙：' '      You will be asked for a passphrase next. Think of it as a key:'
Write-Bi '        - 至少 8 个字符' '        - at least 8 characters'
Write-Bi '        - 只存在你自己的脑子里和这台电脑上，不会上传' '        - it lives only in your head and on this machine, and is never uploaded'
Write-Bi '        - 输入时屏幕上不显示，这是正常的' '        - it is not shown on screen while you type, and that is normal'
Write-Bi '        - 以后脚本改过要重新设置，必须输入同一个口令' '        - when the scripts change you must set up again with the same passphrase'
Write-Host ''

$secure = Read-Host -AsSecureString -Prompt 'passphrase (>= 8 chars)'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if (-not $plain) {
    Exit-With 2 '[停止] 口令是空的，什么都没有改。' '[STOP] The passphrase is empty. Nothing was changed.'
}
if ($plain.Length -lt 8) {
    $plain = $null
    Exit-With 3 '[停止] 口令少于 8 个字符，什么都没有改。' '[STOP] The passphrase is shorter than 8 characters. Nothing was changed.'
}

$helper = @'
import os, sys
sys.path.insert(0, os.path.join(os.environ['ATS_REPO'], 'scripts'))
from pathlib import Path
import ats_guard as g

root = Path(os.environ['ATS_ROOT_DIR'])
pw = sys.stdin.readline().rstrip('\r\n').lstrip('\ufeff')
if not pw:
    sys.exit(2)

rc = 0 if (root / 'ats_guard_key.json').is_file() else g.cmd_init(root, True, pw)
sys.exit(rc or g.cmd_seal(root, True, pw))
'@

$helperPath = Join-Path $env:TEMP ('ats_setup_' + [guid]::NewGuid().ToString('N') + '.py')
$sealRc = 1
try {
    Set-Content -Path $helperPath -Value $helper -Encoding UTF8

    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'python'
    $psi.Arguments = '"' + $helperPath + '"'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.EnvironmentVariables['ATS_REPO'] = $RepoDir
    $psi.EnvironmentVariables['ATS_ROOT_DIR'] = $Root
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'

    $proc = [Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Write($plain)
    $proc.StandardInput.Close()
    $proc.WaitForExit()
    $sealRc = $proc.ExitCode
} finally {
    $plain = $null
    if (Test-Path $helperPath) { Remove-Item -Force $helperPath -ErrorAction SilentlyContinue }
}

if ($sealRc -ne 0) {
    $zh = @'
[失败] 设置没有成功。看上面 JSON 里的 "error"：

  passphrase_too_short   口令少于 8 位
  passphrase_mismatch    口令和上次设置时不一样

重跑一次本脚本，输入上次那个口令即可。
'@
    $en = @'
[FAILED] Setup did not succeed. Check the "error" field in the JSON above:

  passphrase_too_short   the passphrase is shorter than 8 characters
  passphrase_mismatch    the passphrase differs from the one used last time

Run this script again and type the passphrase you used before.
'@
    Exit-With $sealRc $zh $en
}

Write-Host ''
$finalRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root)
if ($finalRc -ne 0) {
    $zh = @'
[失败] 设置完了，但检查仍有问题，按上面 problems 里的条目处理。
'@
    $en = @'
[FAILED] Setup finished, but the check still reports problems. Work through the entries under "problems" above.
'@
    Exit-With $finalRc $zh $en
}

Write-Rule
Write-Bi '  [完成] 设置好了，现在可以用了' '  [Done] Setup complete. You can use it now.' 'Green'
Write-Rule
Write-Host ''
Write-Bi ('  看看现在有什么：      {0}' -f 'python scripts\ats.py list') ('  {0,-32}{1}' -f 'See what is there now:', 'python scripts\ats.py list')
Write-Bi ('  建一个任务：          {0}' -f 'python scripts\ats.py create demo --command "echo hi" --level L1') ('  {0,-32}{1}' -f 'Create a task:', 'python scripts\ats.py create demo --command "echo hi" --level L1')
Write-Bi ('  看有哪些命令：        {0}' -f 'python scripts\ats.py --help') ('  {0,-32}{1}' -f 'List all commands:', 'python scripts\ats.py --help')
Write-Bi ('  忘了怎么用：          {0}' -f '打开 README.md 或 SKILL.md') ('  {0,-32}{1}' -f 'Full usage:', 'open README.md or SKILL.md')
Write-Host ''
Write-Bi '  脚本改过之后要重新设置：再跑一次本脚本，输入同一个口令。' '  After the scripts change, set up again: run this script and type the same passphrase.'

Exit-With 0 ''