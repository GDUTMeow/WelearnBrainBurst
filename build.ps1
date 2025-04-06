param(
    [string]$Version = "1.0"
)

# 设置编码为 UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 定义日志函数
function Write-Log {
    param (
        [ValidateSet("INFO", "WARN", "ERROR", "DEBUG")]
        [string]$Level,
        [string]$Message
    )
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $formattedMessage = "$timestamp [$Level]: $Message"
    
    $color = switch ($Level) {
        "INFO"  { "Green" }
        "WARN"  { "Yellow" }
        "ERROR" { "Red" }
        "DEBUG" { "Cyan" }
        default { "White" }
    }

    Write-Host $formattedMessage -ForegroundColor $color
    Add-Content -Path "deploy.log" -Value $formattedMessage
}

# 检查命令是否存在
function Test-CommandExistence {
    param ([string]$Command)
    return Get-Command $Command -ErrorAction SilentlyContinue
}

# 初始化执行时间
$scriptStartTime = Get-Date

# 步骤执行函数
function Execute-Step {
    param (
        [string]$StepName,
        [ScriptBlock]$StepScript
    )

    Write-Log -Level "INFO" -Message "开始步骤: $StepName"
    $stepStart = Get-Date

    try {
        $output = & $StepScript 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw $output
        }
        $duration = (Get-Date) - $stepStart
        Write-Log -Level "INFO" -Message "步骤 '$StepName' 成功，耗时: $($duration.ToString("hh\:mm\:ss"))"
    }
    catch {
        $duration = (Get-Date) - $stepStart
        Write-Log -Level "ERROR" -Message "步骤 '$StepName' 失败，耗时: $($duration.ToString("hh\:mm\:ss"))"
        Write-Log -Level "DEBUG" -Message "错误详情: $($_.Exception.Message)"
        exit 1
    }
}

# 检查 Python
Execute-Step -StepName "检查 Python 环境" -StepScript {
    if (-not (Test-CommandExistence "python")) {
        throw "Python 未安装"
    }
    $version = python --version 2>&1
    Write-Log -Level "DEBUG" -Message "检测到 Python 版本: $version"
}

# 处理虚拟环境
Execute-Step -StepName "虚拟环境管理" -StepScript {
    if (-not (Test-Path "venv")) {
        Write-Log -Level "INFO" -Message "创建虚拟环境..."
        python -m venv venv
    }
    & .\venv\Scripts\Activate.ps1
}

# 安装依赖
Execute-Step -StepName "安装项目依赖" -StepScript {
    pip install -r requirements.txt
    pip install nuitka
}

# 编译程序（MinGW 配置）
Execute-Step -StepName "Nuitka 编译" -StepScript {
    $compileArgs = @(
        "--standalone",
        "--mingw",  # 强制使用 MinGW
        "--plugin-enable=tk-inter",
        "--include-data-dir=templates=templates",
        "--include-data-dir=static=static",
        "--output-dir=build",
        "--remove-output",
        "--windows-icon-from-ico=static/img/icon.ico",
        "--windows-company-name=GamerNoTitle",
        "--windows-product-name=WelearnBrainBurst",
        "--lto=yes",
        "--assume-yes-for-downloads",
        "app.py"
    )

    Write-Log -Level "DEBUG" -Message "编译参数: $($compileArgs -join ' ')"
    nuitka @compileArgs
}

# 验证输出结构
Execute-Step -StepName "验证编译结果" -StepScript {
    $requiredFiles = @(
        "build/app.dist/app.exe",
        "build/app.dist/templates",
        "build/app.dist/static"
    )

    $requiredFiles | ForEach-Object {
        if (-not (Test-Path $_)) {
            throw "关键文件缺失: $_"
        }
    }

    Get-ChildItem -Recurse build/app.dist | ForEach-Object {
        Write-Log -Level "DEBUG" -Message "生成文件: $($_.FullName)"
    }
}

# 清理和报告
$totalTime = (Get-Date) - $scriptStartTime
Write-Log -Level "INFO" -Message "编译完成! 总耗时: $($totalTime.ToString("hh\:mm\:ss"))"
deactivate