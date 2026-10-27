# WelearnBrainBurst

这是一个 Welearn 的刷课脚本，旨在将大学生从繁重的课业中解放出来，从而提高学习效率。

> [!warning]
>
> 目前已知存在通过此绕过前端直打后端的方式获得的成绩可能会被响应课程的老师审计并清除成绩，后续会对整个刷课的方式进行修改，如果你发现成绩被清空了，就先不要用这个刷正确率了，刷时长应该是可以的
>
> **USE IT AT YOUR OWN RISK**

> [!Tip]
>
> 本脚本在新视野大学英语系列课程中测试通过，其他课程未测（我没有其他课程咋测嘛｡°(°¯᷄◠¯᷅°)°｡）

- Telegram 私信: https://t.me/PaffCreamPMBot  

- Telegram 群组: https://t.me/DohnaNyan  

- Telegram 频道: https://t.me/PaffChannel  

## 为什么叫 WelearnBrainBurst

在做这个程序的时候，就在想名字要取什么，然后想到了以前看过的番《加速世界》，里面加入 Burst Link 的口令就是 Brain Burst，就拿过来用了

（话说还有人记得加速世界嘛）

## 运行模式

程序现在支持两种运行模式。完成课程、单元和小节选择后，任务设置区才会显示本次最终提交的模式选择；在此之前不会设置后端运行模式，课程信息始终使用对应的查询 API，只有点击开始任务且参数校验通过后才会把模式写入 `config.json`。

### 模式 1 · 服务端刷课（原来的模式）

登录（Cookie 或账号密码）后，由本程序直接调用 Welearn 的接口完成任务：

- **刷课**：按设定的正确率直接上报小节完成状态
- **挂机**：模拟学习过程，按设定的时长逐节挂机

### 模式 2 · Selenium 浏览器自动答题（WeAutomaticallyLearn）

模式 2 使用 Selenium 驱动本机 Chrome，打开选中的 Welearn 小节页面，再把上游项目的答题逻辑注入真实页面 DOM。答案来自课程页面前端的 `div[data-itemtype="result"]`，由页面自己的提交按钮完成提交，不是模式 1 那种直接请求后端上报。

使用步骤：

1. 登录本程序（Cookie 或账号密码），选择课程、单元或小节。
2. 完成课程、单元或小节选择后，在任务设置区选择本次任务使用模式 2。此时不会提前修改后端模式；点击「开始 Selenium 自动答题」提交任务时，程序才检测 Python Selenium、Chrome 和 ChromeDriver。缺少组件时会显示安装按钮，点击后会再次询问确认，然后只执行固定的安装命令。
3. 确认模式 2 设置：**无头模式默认开启**，关闭后可看到 Selenium 打开的 Chrome 窗口；设置小节完成后的空闲等待时间，然后点击「开始 Selenium 自动答题」。
4. 环境未就绪时先完成检测与安装，再重新启动任务；答题全程由本程序自动执行，无需复制或下载脚本。

> Selenium 模式会复用本程序保存的 Cookie；Cookie 失效时请重新登录。ChromeDriver 优先使用本机驱动，其次使用 `chromedriver-py`，最后可由 Selenium Manager 联网自动下载。

> 模式 2 的作答行为来自 [KeqingMoe/WeAutomaticallyLearn](https://github.com/KeqingMoe/WeAutomaticallyLearn)（MIT License, Copyright (c) 2025 KeqingMoe），脚本头部保留了原始的版权与许可声明。

> 模式 2 只处理有练习内容的小节：已完成且未开放复习的小节会在开始前统一跳过（不会逐个打开页面），出版社托管的视频/导读页会在打开后识别并跳过。
>
> 单个小节页面加载失败会自动重试 3 次（每次间隔递增），仍失败则跳过该小节继续后面的；连续 5 个小节加载失败才停止任务。任务结束时会输出「提交 X 节、跳过 Y 节、失败 Z 节」的汇总。

## 日志

运行日志按用途分成三类，互不干扰：

| 文件 | 内容 |
| --- | --- |
| `logs/app.log` | 业务与任务进展（登录、选课、模式 2 每个小节的处理结果） |
| `logs/access.log` | 真实接口访问；`/api/status`、`/api/getLog` 等轮询接口和静态资源不记录 |
| `logs/system.log` | 启动信息、模式切换等系统事件 |
| `logs/latest.log` | 本次运行的全部日志（每次启动重写） |
| `logs/console.log` | Flask / Werkzeug 服务器自身的请求输出（每请求一行），与业务日志隔离 |
| `logs/chromedriver-*.log` | ChromeDriver 诊断日志，仅保留最近 5 份 |

接口 `GET /api/getLog?type=app|access|system|all` 可按分类取回日志。

## 前端组件

本地内置 Sober **1.1.10**（npm latest 稳定版），遵循 MIT License，许可见 `static/js/sober.LICENSE.txt`。模式 2 直接集成在课程任务设置区，使用 TextField 的辅助文本插槽、Switch、Chip 和 Dialog；单列排列设置、环境检测与提交操作，不另起大卡片，不提供手动脚本入口。更新组件时请同时核对 [官方组件文档](https://github.com/miralm-ltd/sober/tree/main/docs/zh-cn)，并保持本地脚本 URL 的版本参数一致。

## 开发与测试

```bash
uv venv .venv && uv pip install -p .venv -r requirements.txt pytest
ruff format app.py selenium_auto.py tests/
.venv/bin/python -m pytest tests -q
```

提交前请确认 `ruff format --check app.py selenium_auto.py tests/` 通过；行宽与引号风格由 `pyproject.toml` 的 `[tool.ruff]` 统一约束。

## 使用文档（二选一）

- https://gamernotitle.sg.larksuite.com/wiki/PLLywJxlViN31tkBiIflSaytgBg
- https://scriptkid.notion.site/welearnbrainburst

## Credit

- https://gitee.com/xxxhhy/welearn-curriculum-finsh
- https://gitee.com/xxxhhy/welearn-time-finsh
- https://github.com/KeqingMoe/WeAutomaticallyLearn （模式 2 的自动答题脚本，MIT License）
