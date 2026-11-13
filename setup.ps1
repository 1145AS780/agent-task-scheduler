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

function Exit-With {
    param([int]$Code, [string]$Message)
    if ($Message) { Write-Host ''; Write-Host $Message -ForegroundColor Yellow }
    Write-Host ''
    Write-Host '按回车键关闭这个窗口 ...' -ForegroundColor DarkGray
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
Write-Host '  ATS 一键设置' -ForegroundColor Cyan
Write-Host ("  状态目录：{0}" -f $Root)
Write-Rule

Write-Host ''
Write-Host '[1/3] 检查 python ...'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Exit-With 1 @'
[停止] 这台电脑上找不到 python。

请先安装 Python 3，安装时一定要勾选 "Add Python to PATH"。
装好之后重新双击本脚本即可。
'@
}
$verText = (& python --version 2>&1 | Out-String).Trim()
if ($verText -notmatch 'Python\s+3') {
    Exit-With 1 @'
[停止] 这台电脑上的 python 不是 Python 3。

ATS 只支持 Python 3。请安装 Python 3，安装时一定要勾选 "Add Python to PATH"，
装好之后重新双击本脚本即可。
'@
}
Write-Host ('      OK  ' + $verText)

Write-Host ''
Write-Host '[2/3] 检查是否已经设置好 ...'
$verifyRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root) -Quiet
if ($verifyRc -eq 0) {
    Write-Host ''
    Write-Host '      [完成] 这个目录已经设置好了，什么都不用做。' -ForegroundColor Green
    Write-Host '             想直接试试：python scripts\ats.py status'
    Exit-With 0 ''
}
Write-Host '      还没有设置好，继续第 3 步。'

if ((-not (Test-Path $KeyPath)) -and (Test-Path $ManifestPath)) {
    Exit-With 4 @'
[停止] 密钥文件不见了，但清单还在。

这种情况本脚本不处理。请从 git 恢复这两个文件：
  ats_guard_key.json
  ats_guard_manifest.json

确认要推倒重来，就由人手动删掉这两个文件，再跑一次本脚本。
'@
}

Write-Host ''
Write-Host '[3/3] 设置信任根'
Write-Host ''
Write-Host '      接下来会让你输入一个口令。它像一把钥匙：'
Write-Host '        - 至少 8 个字符'
Write-Host '        - 只存在你自己的脑子里和这台电脑上，不会上传'
Write-Host '        - 输入时屏幕上不显示，这是正常的'
Write-Host '        - 以后脚本改过要重新设置，必须输入同一个口令'
Write-Host ''

$secure = Read-Host -AsSecureString -Prompt 'passphrase (>= 8 chars)'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if (-not $plain) {
    Exit-With 2 '[停止] 口令是空的，什么都没有改。'
}
if ($plain.Length -lt 8) {
    $plain = $null
    Exit-With 3 '[停止] 口令少于 8 个字符，什么都没有改。'
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
    Exit-With $sealRc @'
[失败] 设置没有成功。看上面 JSON 里的 "error"：

  passphrase_too_short   口令少于 8 位
  passphrase_mismatch    口令和上次设置时不一样

重跑一次本脚本，输入上次那个口令即可。
'@
}

Write-Host ''
$finalRc = Invoke-Guard -GuardArgs @('verify', '--root', $Root)
if ($finalRc -ne 0) {
    Exit-With $finalRc @'
[失败] 设置完了，但检查仍有问题，按上面 problems 里的条目处理。
'@
}

Write-Rule
Write-Host '  [完成] 设置好了，现在可以用了' -ForegroundColor Green
Write-Rule
Write-Host ''
Write-Host '  试一下：              python scripts\ats.py status'
Write-Host '  看有哪些命令：        python scripts\ats.py --help'
Write-Host '  忘了怎么用：          打开 README.md 或 SKILL.md'
Write-Host ''
Write-Host '  脚本改过之后要重新设置：再跑一次本脚本，输入同一个口令。'

Exit-With 0 ''