#!/bin/bash
# 设置脚本为UTF-8编码（Linux默认UTF-8，但可通过环境变量确保）

# 定义日志函数
function write_log() {
    LEVEL=$1
    MESSAGE=$2
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    FORMATTED_MESSAGE="[$TIMESTAMP] [$LEVEL]: $MESSAGE"
    
    # ANSI颜色码
    case $LEVEL in
        "INFO")  COLOR="\033[0;32m" ;;  # 绿色
        "WARN")  COLOR="\033[1;33m" ;;  # 黄色
        "ERROR") COLOR="\033[0;31m" ;;  # 红色
        "DEBUG") COLOR="\033[0;36m" ;;  # 青色
        *)       COLOR="\033[0m" ;;     # 默认白色
    esac
    
    echo -e "${COLOR}${FORMATTED_MESSAGE}\033[0m"  # 输出到控制台
    echo "$FORMATTED_MESSAGE" >> deploy.log  # 输出到日志文件
}

# 检查命令是否存在
function command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# 初始化执行时间
SCRIPT_START_TIME=$(date +%s)

# 步骤执行函数
function execute_step() {
    STEP_NAME=$1
    STEP_SCRIPT=$2
    
    write_log "INFO" "开始步骤: $STEP_NAME"
    STEP_START=$(date +%s)
    
    eval "$STEP_SCRIPT"
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -ne 0 ]; then
        STEP_DURATION=$(( $(date +%s) - $STEP_START ))
        write_log "ERROR" "步骤 '$STEP_NAME' 失败，耗时: $(printf '%02d:%02d:%02d' $((STEP_DURATION/3600)) $(( (STEP_DURATION%3600)/60 )) $((STEP_DURATION%60)))"
        exit 1
    fi
    
    STEP_DURATION=$(( $(date +%s) - $STEP_START ))
    write_log "INFO" "步骤 '$STEP_NAME' 成功，耗时: $(printf '%02d:%02d:%02d' $((STEP_DURATION/3600)) $(( (STEP_DURATION%3600)/60 )) $((STEP_DURATION%60)))"
}

# 参数处理
VERSION=${1:-"1.0"}  # 默认版本为1.0，如果提供参数则使用

# 步骤1: 检查 Python
execute_step "检查 Python 环境" "
    if ! command_exists python; then
        write_log 'ERROR' 'Python 未安装'
        exit 1
    fi
    PYTHON_VERSION=\$(python --version 2>&1)
    write_log 'DEBUG' \"检测到 Python 版本: \$PYTHON_VERSION\"
"

# 步骤2: 处理虚拟环境
execute_step "虚拟环境管理" "
    if [ ! -d 'venv' ]; then
        write_log 'INFO' '创建虚拟环境...'
        python -m venv .venv
    fi
    source ./.venv/bin/activate  # 激活虚拟环境
"

# 步骤3: 安装依赖
execute_step "安装项目依赖" "
    pip install -r requirements.txt
    pip install nuitka
"

# 步骤4: Nuitka 编译
execute_step "Nuitka 编译" "
    COMPILE_ARGS=(
        '--standalone'
        '--include-data-dir=templates=templates'
        '--include-data-dir=static=static'
        '--output-dir=build'
        '--remove-output'
        '--lto=yes'
        '--assume-yes-for-downloads'
        'app.py'
    )
    
    write_log 'DEBUG' \"编译参数: \${COMPILE_ARGS[*]}\"
    nuitka \"\${COMPILE_ARGS[@]}\"
"

# 步骤5: 验证输出结构
execute_step "验证编译结果" "
    REQUIRED_FILES=(
        'build/app.dist/app'
        'build/app.dist/templates'
        'build/app.dist/static'
    )
    
    for file in \"\${REQUIRED_FILES[@]}\"; do
        if [ ! -e \"\$file\" ]; then
            write_log 'ERROR' \"关键文件缺失: \$file\"
            exit 1
        fi
    done
    
    find build/app.dist -type f | while read -r file; do
        write_log 'DEBUG' \"生成文件: \$file\"
    done
"

# 清理和报告
TOTAL_TIME=$(( $(date +%s) - $SCRIPT_START_TIME ))
write_log "INFO" "编译完成! 总耗时: $(printf '%02d:%02d:%02d' $((TOTAL_TIME/3600)) $(( (TOTAL_TIME%3600)/60 )) $((TOTAL_TIME%60)))"

# 退出虚拟环境
deactivate