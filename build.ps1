# 设置控制台编码为 UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Clear-Host

# 定义日志函数
function Write-Log {
    param (
        [ValidateSet("INFO", "WARN", "ERROR", "DEBUG")]
        [string]$Level,
        [string]$Message
    )
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $formattedMessage = "$timestamp [$Level]: $Message"
    
    switch ($Level) {
        "INFO"  { Write-Host $formattedMessage -ForegroundColor Green }
        "WARN"  { Write-Host $formattedMessage -ForegroundColor Yellow }
        "ERROR" { Write-Host $formattedMessage -ForegroundColor Red }
        "DEBUG" { Write-Host $formattedMessage -ForegroundColor Cyan }
        default { Write-Host $formattedMessage }
    }

    # 将日志写入文件
    Add-Content -Path "deploy.log" -Value $formattedMessage
}

# 函数用于检查命令是否存在
function Test-CommandExistence {
    param (
        [string]$Command
    )
    return Get-Command $Command -ErrorAction SilentlyContinue
}

# 记录脚本开始时间
$scriptStartTime = Get-Date

# 定义函数以测量和记录步骤时间
function Execute-Step {
    param (
        [string]$StepName,
        [ScriptBlock]$StepScript
    )

    Write-Log -Level "INFO" -Message "开始步骤: $StepName"
    $stepStart = Get-Date

    try {
        & $StepScript
        if ($?) {
            $stepEnd = Get-Date
            $duration = $stepEnd - $stepStart
            Write-Log -Level "INFO" -Message "步骤 '$StepName' 成功，耗时: $($duration.ToString())"
        } else {
            $stepEnd = Get-Date
            $duration = $stepEnd - $stepStart
            Write-Log -Level "ERROR" -Message "步骤 '$StepName' 失败，耗时: $($duration.ToString())"
            exit 1
        }
    } catch {
        $stepEnd = Get-Date
        $duration = $stepEnd - $stepStart
        Write-Log -Level "ERROR" -Message "步骤 '$StepName' 发生异常，耗时: $($duration.ToString())"
        Write-Log -Level "DEBUG" -Message $_.Exception.Message
        exit 1
    }
}

# 检查 Python 是否安装
Execute-Step -StepName "检查 Python3 是否安装" -StepScript {
    if (Test-CommandExistence python) {
        Write-Log -Level "INFO" -Message "Python 已经安装"
        python --version | ForEach-Object { Write-Log -Level "DEBUG" -Message $_ }
    } else {
        Write-Log -Level "ERROR" -Message "Python 没有安装，请安装后重试。"
        exit 1
    }
}

# 检查虚拟环境是否存在
Execute-Step -StepName "检查或创建虚拟环境" -StepScript {
    if (Test-Path "venv") {
        Write-Log -Level "INFO" -Message "检测到已创建的虚拟环境"
    } else {
        Write-Log -Level "INFO" -Message "正在创建虚拟环境 venv..."
        python -m venv venv
        if ($?) {
            Write-Log -Level "INFO" -Message "虚拟环境创建成功"
        } else {
            Write-Log -Level "ERROR" -Message "虚拟环境创建失败，请检查错误信息。"
            exit 1
        }
    }
}

# 激活虚拟环境
Execute-Step -StepName "激活虚拟环境" -StepScript {
    Write-Log -Level "INFO" -Message "正在激活虚拟环境..."
    try {
        & .\venv\Scripts\Activate.ps1
        if ($?) {
            Write-Log -Level "INFO" -Message "虚拟环境激活成功"
        } else {
            Write-Log -Level "ERROR" -Message "虚拟环境激活失败，请检查错误信息。"
            exit 1
        }
    } catch {
        Write-Log -Level "ERROR" -Message "无法激活虚拟环境。请确保执行策略允许运行脚本。"
        Write-Log -Level "WARN" -Message "您可以尝试运行： Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"
        exit 1
    }
}

Execute-Step -StepName "安装依赖" -StepScript {
    Write-Log -Level "INFO" -Message "正在安装必要的轮子..."
    pip install -r requirements.txt
    if ($?) {
        Write-Log -Level "INFO" -Message "轮子安装成功"
    } else {
        Write-Log -Level "ERROR" -Message "轮子安装失败，请检查错误信息。"
        exit 1
    }
}

# 编译 Python 程序
Execute-Step -StepName "编译程序" -StepScript {
    nuitka --standalone --include-data-dir=templates=templates `
    --include-data-dir=static=static `
    --output-dir=build --remove-output --show-progress `
    --windows-icon-from-ico=static/img/icon.ico `
    --windows-company-name=GamerNoTitle `
    --windows-product-name="WelearnBrainBurst" `
    --windows-file-version=1.0 `
    --windows-product-version=1.0 `
    --lto=yes --assume-yes-for-downloads app.py
}

# 记录脚本结束时间
$scriptEndTime = Get-Date
$scriptDuration = $scriptEndTime - $scriptStartTime
Write-Log -Level "INFO" -Message "脚本总执行时间: $($scriptDuration.ToString())"
# 退出虚拟环境
deactivate
# 完成
Write-Log -Level "INFO" -Message "编译完成！"
Pause
