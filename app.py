from __future__ import annotations
import json
import logging
import os
import re
import sys
import time
import random
import threading
import socket
import httpx
import webbrowser
import signal
from typing import (
    Any,
    Optional,
    TypedDict,
    Literal,
    Dict,
    List,
    Tuple,
    IO,
    Union,
    cast,
)
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    Response,
)
from io import StringIO
from werkzeug.wrappers.response import Response as WerkzeugResponse
from bs4 import BeautifulSoup
import tls_client
import base64

import selenium_auto


# ====================== 全局变量 ======================
class GLOBAL:
    cid: int = -1
    uid: int = -1
    classid: int = -1
    lessonList: List[Dict[str, str]] = []
    lessonIndex: List = []


_global = GLOBAL()

# 最大线程数量（挂机用）
MAX_THREADS = 32

# 中断任务时等待线程退出的秒数
TASK_JOIN_TIMEOUT = 5

# ====================== 运行模式 ======================
# mode1：原有的服务端刷课模式（登录后由本程序直接请求 Welearn 接口完成刷课 / 挂机）
# mode2：WeAutomaticallyLearn 模式（在浏览器课程页面里运行的自动答题脚本）
RunMode = Literal["mode1", "mode2"]
AVAILABLE_MODES: Tuple[RunMode, ...] = ("mode1", "mode2")
DEFAULT_MODE: RunMode = "mode1"

MODE_DESCRIPTIONS: Dict[RunMode, str] = {
    "mode1": "模式 1（服务端刷课）：登录后由本程序直接调用 Welearn 接口完成刷课与挂机任务。",
    "mode2": "模式 2（浏览器自动答题）：启动浏览器，读取课程页面中的答案并自动作答提交。",
}

CONFIG_PATH = "config.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODE2_SCRIPT_PATH = os.path.join(BASE_DIR, "static", "js", "mode2-autolearn.js")
MODE2_SCRIPT_URL = "/static/js/mode2-autolearn.js"

# ====================== 应用初始化 ======================
app = Flask(__name__)


# ====================== 日志系统配置 ======================
# UI 日志按用途拆分：APP=业务/任务进展，ACCESS=真实接口访问，SYSTEM=启动与环境。
LOG_KINDS: Tuple[str, ...] = ("APP", "ACCESS", "SYSTEM")

# 轮询类接口每秒都会被前端调用，记录它们只会淹没真正有价值的日志。
QUIET_PATHS = {"/api/status", "/api/getLog"}


class LogTypeFilter(logging.Filter):
    """按日志类型过滤的处理器"""

    def __init__(self, allowed_type):
        super().__init__()
        self.allowed_type = allowed_type

    def filter(self, record):
        return getattr(record, "log_type", "") == self.allowed_type


class AppLogFilter(logging.Filter):
    """过滤APP相关日志的处理器"""

    def filter(self, record):
        log_type = getattr(record, "log_type", "")
        return log_type.startswith("APP")


# 创建日志目录
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 原日志处理器（保留最新日志）
file_handler = logging.FileHandler(os.path.join(log_dir, "latest.log"), mode="w", encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(message)s"))

# 新增分类日志处理器
system_handler = logging.FileHandler(
    os.path.join(log_dir, "system.log"), mode="a", encoding="utf-8"
)
system_handler.setLevel(logging.INFO)
system_handler.setFormatter(logging.Formatter("%(message)s"))
system_handler.addFilter(LogTypeFilter("SYSTEM"))

app_handler = logging.FileHandler(os.path.join(log_dir, "app.log"), mode="a", encoding="utf-8")
app_handler.setLevel(logging.INFO)
app_handler.setFormatter(logging.Formatter("%(message)s"))
app_handler.addFilter(AppLogFilter())

access_handler = logging.FileHandler(
    os.path.join(log_dir, "access.log"), mode="a", encoding="utf-8"
)
access_handler.setLevel(logging.INFO)
access_handler.setFormatter(logging.Formatter("%(message)s"))
access_handler.addFilter(LogTypeFilter("ACCESS"))

# 控制台处理器保持不变
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))

# Werkzeug 的每条请求日志单独落到 console.log，避免混进业务日志
werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.handlers.clear()
werkzeug_handler = logging.FileHandler(
    os.path.join(log_dir, "console.log"), mode="a", encoding="utf-8"
)
werkzeug_handler.setFormatter(logging.Formatter("%(message)s"))
werkzeug_logger.addHandler(werkzeug_handler)
werkzeug_logger.propagate = False

# 配置应用日志（先清掉 Flask 可能预置的 handler，避免终端重复输出）
app.logger.handlers.clear()
app.logger.setLevel(logging.INFO)
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)
app.logger.addHandler(system_handler)
app.logger.addHandler(app_handler)
app.logger.addHandler(access_handler)


# ====================== 类型定义 ======================
class CourseInfo(TypedDict):
    """课程信息数据结构"""

    cid: str
    name: str
    per: int


class UnitInfo(TypedDict):
    """单元信息数据结构"""

    unitname: str
    name: str
    visible: str


class ProgressInfo(TypedDict):
    """进度信息数据结构"""

    current: int = 0
    total: int = 0


TaskStatusType = Literal[
    "idle",
    "brain_burst",
    "away_from_keyboard",
    "browser_burst",
    "error",
    "completed",
    "nologon",
]


# ====================== 全局状态管理 ======================
class GlobalState:
    """全局状态管理器"""

    def __init__(self):
        self.reset()

    def reset(self, keep_mode: bool = True):
        """重置所有状态（默认保留运行模式，模式只由用户显式切换）"""
        mode = getattr(self, "mode", DEFAULT_MODE) if keep_mode else DEFAULT_MODE
        self.current_task: Optional[threading.Thread] = None
        self.task_status: TaskStatusType = "nologon"
        self.progress: ProgressInfo = {"current": 0, "total": 0}
        self.log_buffers: Dict[str, IO[str]] = {kind: StringIO() for kind in LOG_KINDS}
        self.stop_event = threading.Event()
        self.active_threads: List[threading.Thread] = []
        self.cookies: Optional[Dict[str, str]] = None
        self.cid: Optional[str] = None
        self.uid: Optional[str] = None
        self.classid: Optional[str] = None
        self.mode: RunMode = mode


state = GlobalState()
client = httpx.Client()


# ====================== 日志工具 ======================
def log_kind(log_type: str) -> str:
    """把细分日志类型归并到 UI 的三个分类里"""
    if log_type == "ACCESS":
        return "ACCESS"
    if log_type.startswith("APP"):
        return "APP"
    return "SYSTEM"


def log_message(message: str, log_type: str = "SYSTEM"):
    """统一日志记录函数"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{log_type}] {message}"
    state.log_buffers[log_kind(log_type)].write(log_entry + "\n")
    app.logger.info(log_entry, extra={"log_type": log_type})


# ====================== 中间件 ======================
@app.before_request
def record_request_start():
    """记录请求开始时间"""
    request.start_time = time.time()


@app.after_request
def log_access(response: Response):
    """记录访问日志；轮询接口和静态资源不记，避免把日志冲成噪声"""
    if request.path in QUIET_PATHS or request.path.startswith("/static/"):
        return response
    latency = time.time() - request.start_time
    latency_ms = int(latency * 1000)

    log_entry = (
        f"{request.remote_addr} "
        f'"{request.method} {request.path}" '
        f"{response.status_code} "
        f"{latency_ms}ms "
        f'"{request.headers.get("User-Agent", "")}"'
    )

    log_message(log_entry, "ACCESS")
    return response


# ====================== 配置读写 ======================
def load_config() -> Dict[str, Any]:
    """读取配置文件，文件不存在或损坏时返回空配置"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(**values: Any) -> None:
    """合并写入配置文件，避免覆盖已保存的 Cookie / 运行模式"""
    config = load_config()
    config.update(values)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


# ====================== 工具函数 ======================
def get_port():
    for port in range(3000, 60001):  # 包含60000
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    raise ValueError("No available ports between 3000-60000")


def get_user_info(client: httpx.Client) -> Dict[str, Optional[str]]:
    """
    从用户信息页面提取用户详细信息（使用 tls_client 绕过 WAF）
    :param client: 保留参数以兼容，但内部使用 tls_client
    :return: 用户信息字典
    """
    info_url = "https://welearn.sflep.com/user/userinfo.aspx"

    try:
        session = build_tls_session()
        response = session.get(
            info_url, headers={"Referer": "https://welearn.sflep.com/student/index.aspx"}
        )
        soup = BeautifulSoup(response.text, "html.parser")
        user_div = soup.find("div", id="user1")
        if not user_div:
            raise ValueError("未找到用户信息面板")
        return {
            "username": _find_input_value(user_div, "lblAccount"),
            "name": _find_input_value(user_div, "txtName"),
            "student_id": _find_input_value(user_div, "txtStuNo"),
            "school": _find_selected_school(user_div),
            "birth_year": _find_selected_birthyear(user_div),
        }
    except Exception as e:
        print(f"获取用户信息出错: {str(e)}")
        return {key: None for key in ["username", "name", "student_id", "school", "birth_year"]}


def _find_input_value(soup: BeautifulSoup, id_fragment: str) -> Optional[str]:
    """查找包含指定ID片段的输入框值"""
    input_tag = soup.find("input", id=lambda x: x and id_fragment in x)
    return input_tag.get("value") if input_tag else None


def _find_selected_school(soup: BeautifulSoup) -> Optional[str]:
    """解析学校选择信息"""
    select_tag = soup.find("select", id=lambda x: x and "txtSchool" in x)
    if select_tag:
        selected = select_tag.find("option", selected=True)
        if selected:
            return selected.get("value")

    button = soup.find("button", class_="multiselect")
    if button and button.get("title"):
        return button["title"]

    return None


def _find_selected_birthyear(soup: BeautifulSoup) -> Optional[str]:
    """解析出生年份选择"""
    select_tag = soup.find("select", id=lambda x: x and "ddlYear" in x)
    if not select_tag:
        return None

    selected = select_tag.find("option", selected=True)
    return selected.get("value") if selected else None


# WAF 绕过部分
def to_hex_byte_array(byte_array: bytes) -> str:
    return "".join([f"{byte:02x}" for byte in byte_array])


def generate_cipher_text(password: str):
    # 获取当前时间戳
    T0 = int(round(time.time() * 1000))
    P = password.encode("utf-8")
    V = (T0 >> 16) & 0xFF
    for byte in P:
        V ^= byte
    remainder = V % 100
    T1 = int((T0 / 100) * 100 + remainder)
    P1 = to_hex_byte_array(P)
    S = f"{T1}*" + P1
    S_encoded = S.encode("utf-8")
    E = base64.b64encode(S_encoded).decode("utf-8")
    return [E, T1]


def build_tls_session() -> tls_client.Session:
    """构建一个带浏览器指纹的 tls_client 会话，并注入当前 Cookie。"""
    session = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
    session.headers.update(
        {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Chromium";v="120", "Not?A_Brand";v="8", "Google Chrome";v="120"',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
        }
    )
    try:
        if state.cookies:
            for k, v in state.cookies.items():
                for domain in ("welearn.sflep.com", ".sflep.com", "sso.sflep.com"):
                    try:
                        session.cookies.set(k, v, domain=domain)
                    except Exception:
                        pass
    except Exception:
        pass
    return session


# ====================== 路由定义 ======================
@app.route("/")
def index():
    """主页面"""
    return render_template("index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    """静态文件路由"""
    return send_from_directory("static", filename)


@app.route("/api/mode", methods=["GET"])
def api_get_mode():
    """获取当前运行模式"""
    return jsonify(
        success=True,
        error="",
        mode=state.mode,
        activeMode=state.mode,
        selectionDeferred=True,
        availableModes=list(AVAILABLE_MODES),
        descriptions=MODE_DESCRIPTIONS,
        scriptUrl=MODE2_SCRIPT_URL,
        description=MODE_DESCRIPTIONS.get(state.mode, ""),
    )


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    """兼容旧客户端：只校验候选模式，不提前改变当前任务模式。

    真正的运行模式只在 /api/startTask 接受最终任务时设置；这样课程、单元和
    小节信息在提交前始终走同一套查询 API，不会被模式选择提前拦截。
    """
    try:
        mode = (request.json or {}).get("mode")
        if mode not in AVAILABLE_MODES:
            return jsonify(success=False, error=f"未知模式：{mode}")
        return jsonify(
            success=True,
            error="",
            mode=state.mode,
            requestedMode=mode,
            deferred=True,
            description=MODE_DESCRIPTIONS.get(mode, ""),
            message="运行模式将在最终提交任务时生效",
        )
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/mode2/script", methods=["GET"])
def api_mode2_script():
    """返回模式 2 使用的浏览器自动答题脚本内容"""
    try:
        with open(MODE2_SCRIPT_PATH, encoding="utf-8") as f:
            script = f.read()
        return jsonify(success=True, error="", script=script, url=MODE2_SCRIPT_URL)
    except Exception as e:
        return jsonify(success=False, error=str(e), script="")


@app.route("/api/mode2/env", methods=["GET"])
def api_mode2_environment():
    """检测 Selenium 模式 2 所需环境，不启动浏览器。"""
    try:
        environment = selenium_auto.detect_environment()
        return jsonify(success=True, error="", **environment)
    except Exception as e:
        log_message(f"模式 2 环境检测失败：{e}", "APPERR")
        return jsonify(success=False, error=str(e), ready=False, missing=[])


@app.route("/api/mode2/install", methods=["POST"])
def api_mode2_install():
    """在用户明确确认后，后台安装一个固定组件或全部缺失组件。"""
    # 环境安装是提交 Selenium 任务前的准备动作，不改变当前运行模式。
    data = request.json or {}
    if data.get("confirmed") is not True:
        return jsonify(success=False, error="安装操作需要用户明确确认")
    component = data.get("component", "all")
    if component not in {"selenium", "chrome", "chromedriver", "all"}:
        return jsonify(success=False, error=f"不支持安装组件：{component}")
    result = selenium_auto.start_install(
        component,
        log=lambda message: log_message(message, "APPINFO"),
    )
    return jsonify(**result)


def parse_cookie_string(cookies: str) -> Dict[str, str]:
    """Parse a "k=v; k2=v2" cookie string into a dict.

    Browser cookie jars usually separate entries with "; " which leaves a
    leading space on every name after the first.  ChromeDriver rejects names
    containing whitespace, so stripping is mandatory for mode 2 login reuse.
    """
    parsed: Dict[str, str] = {}
    for part in cookies.split(";"):
        cleaned_part = part.strip()
        if not cleaned_part:
            continue
        eq_index = cleaned_part.find("=")
        if eq_index == -1:
            continue
        key = cleaned_part[:eq_index].strip()
        value = cleaned_part[eq_index + 1 :].strip()
        if key:
            parsed[key] = value
    return parsed


@app.route("/api/login", methods=["POST"])
def api_login(cookies: str = None):
    """登录接口"""
    state.reset()
    try:
        if not cookies:
            cookies = request.json.get("cookies", "")

        cookie_dict = {}
        for part in cookies.split(";"):
            cleaned_part = part.strip()  # 移除整个 "key=value" 部分的前后空格
            if not cleaned_part:
                continue  # 跳过空字符串

            eq_index = cleaned_part.find("=")
            if eq_index != -1:
                key = cleaned_part[:eq_index].strip()  # 移除键名部分的前后空格
                value = cleaned_part[eq_index + 1 :].strip()  # 移除值部分的前后空格
                if key:  # 确保键名不为空
                    cookie_dict[key] = value
            else:
                key = cleaned_part.strip()
                if key:
                    cookie_dict[key] = ""

        log_message(f"尝试登录: {cookie_dict}")
        if validate_cookies(cookie_dict):
            client.cookies.update(cookie_dict)
            state.cookies = cookie_dict
            save_config(cookies=cookies)  # 这里保存的是原始cookies字符串
            userinfo = get_user_info(client)
            log_message(f"登录成功: {userinfo}")
            state.task_status = "idle"
            return jsonify(
                success=True,
                error="",
                msg=f"登陆成功：欢迎回来，{userinfo.get('name')}({userinfo.get('student_id')})",
            )
        return jsonify(success=False, error="Cookie验证失败")
    except Exception as e:
        log_message(f"登录过程中发生异常: {str(e)}", "APPERR")
        return jsonify(success=False, error=str(e))


@app.route("/api/login_pw", methods=["POST"])
def api_login_pw():
    """使用用户名/密码登录"""
    state.reset()
    try:
        data = request.json or {}
        user = data.get("user")
        pwd = data.get("pwd")
        if not user or not pwd:
            return jsonify(success=False, error="缺少用户名或密码")

        log_message(f"尝试用户名/密码登录: {user}")
        session = tls_client.Session(client_identifier="chrome_120", debug=False)

        resp = session.get(
            "https://welearn.sflep.com/user/prelogin.aspx?loginret=http://welearn.sflep.com/user/loginredirect.aspx"
        )
        rurl = resp.headers.get("Location") or resp.text

        try:
            code_challenge = rurl.split("&")[4].split("=")[1]
            state_param = rurl.split("&")[6].split("=")[1]
        except Exception:
            m1 = re.search(r"code_challenge=([^&]+)", rurl)
            m2 = re.search(r"state=([^&]+)", rurl)
            code_challenge = m1.group(1) if m1 else ""
            state_param = m2.group(1) if m2 else ""

        rturl = f"/connect/authorize/callback?client_id=welearn_web&redirect_uri=https%3A%2F%2Fwelearn.sflep.com%2Fsignin-sflep&response_type=code&scope=openid%20profile%20email%20phone%20address&code_challenge={code_challenge}&code_challenge_method=S256&state={state_param}&x-client-SKU=ID_NET472&x-client-ver=6.32.1.0"

        while True:
            enpwd = generate_cipher_text(pwd)
            form_data = {
                "rturl": rturl,
                "account": user,
                "pwd": str(enpwd[0]),
                "ts": str(enpwd[1]),
            }

            response = session.post("https://sso.sflep.com/idsvr/account/login", data=form_data)
            try:
                rt_json = response.json()
            except Exception:
                return jsonify(success=False, error="登录返回解析失败")

            code = rt_json.get("code", -1)
            if code == -1:
                continue
            if code == 1:
                return jsonify(success=False, error="帐号或密码错误")

            next_url = f"https://sso.sflep.com/idsvr" + rt_json.get("data", "")

            while True:
                resp2 = session.get(next_url)
                if resp2.status_code in (301, 302, 303, 307, 308):
                    next_url = resp2.headers.get("Location")
                    if not next_url:
                        break
                else:
                    break

            if code == 0:
                try:
                    cookie_dict = {}
                    try:
                        cookie_dict = session.cookies.get_dict()
                    except Exception:
                        for c in getattr(session.cookies, "jar", []):
                            cookie_dict[c.name] = c.value

                    client.cookies.update(cookie_dict)
                    state.cookies = cookie_dict
                    save_config(cookies=";".join([f"{k}={v}" for k, v in cookie_dict.items()]))

                    userinfo = get_user_info(client)
                    log_message(f"登录成功: {userinfo}")
                    state.task_status = "idle"
                    return jsonify(
                        success=True, error="", msg=f"登陆成功：欢迎，{userinfo.get('name')}"
                    )
                except Exception as e:
                    return jsonify(success=False, error=str(e))

    except Exception as e:
        log_message(f"密码登录异常: {e}", "APPERR")
        return jsonify(success=False, error=str(e))


@app.route("/api/getCourses", methods=["GET"])
def get_courses():
    """获取课程列表"""
    if not state.cookies or state.task_status == "nologon":
        log_message("用户未登录", "APPERR")
        return jsonify(success=False, error="请先登录")

    try:
        url = f"https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc&nocache={round(random.random(), 16)}"
        session = build_tls_session()
        response = session.get(
            url,
            headers={"Referer": "https://welearn.sflep.com/student/index.aspx"},
        )
        courses: List[CourseInfo] = response.json()["clist"]
        log_message(f"课程列表：{len(courses)} 门课程", "APPDEBUG")
        return jsonify(success=True, error="", courses=courses)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/getLessons", methods=["GET"])
def get_lessons():
    """获取课程单元列表"""
    if not state.cookies or state.task_status == "nologon":
        log_message("用户未登录", "APPERR")
        return jsonify(success=False, error="请先登录")

    try:
        _global.cid = request.args.get("cid")
        session = build_tls_session()
        url = f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
        response = session.get(
            url,
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        log_message(f"课程信息页：{len(response.text)} 字节", "APPDEBUG")
        _global.uid = re.search('"uid":(.*?),', response.text).group(1)
        _global.classid = re.search('"classid":"(.*?)"', response.text).group(1)
        log_message(
            f"成功解析到单元元数据: uid={_global.uid}, classid={_global.classid}",
            "APPDEBUG",
        )
        url = "https://welearn.sflep.com/ajax/StudyStat.aspx"
        response = session.get(
            url,
            params={"action": "courseunits", "cid": _global.cid, "uid": _global.uid},
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        lessons = response.json()["info"]
        log_message(f"单元列表：{len(lessons)} 个单元", "APPDEBUG")
        _global.lessonList = lessons
        _global.lessonIndex = [i.get("id") for i in lessons]
        return jsonify(success=True, error="", lessons=lessons)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/getSections", methods=["GET"])
def get_sections():
    """获取某单元下的小节列表"""
    if not state.cookies or state.task_status == "nologon":
        log_message("用户未登录", "APPERR")
        return jsonify(success=False, error="请先登录")

    try:
        unit_id = request.args.get("unitId")
        if not unit_id:
            return jsonify(success=False, error="缺少参数 unitId")

        # 计算单元索引
        try:
            unit_index = _global.lessonIndex.index(unit_id)
        except ValueError:
            return jsonify(success=False, error="无效的 unitId")

        url = f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={_global.cid}&uid={_global.uid}&unitidx={unit_index}&classid={_global.classid}"
        session = build_tls_session()
        response = session.get(
            url,
            headers={
                "Referer": f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
            },
        )
        sections = response.json().get("info", [])
        log_message(f"小节列表：{len(sections)} 个小节", "APPDEBUG")
        return jsonify(success=True, error="", sections=sections)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/startTask", methods=["POST"])
def start_task():
    """启动模式 1 服务端任务或模式 2 自动答题任务"""
    try:
        data = request.json or {}
        task_type = data.get("type")
        if task_type not in {"brain_burst", "away_from_keyboard", "browser_burst"}:
            return jsonify(success=False, error="未知任务类型")
        # 模式只由最终提交的任务类型决定；此前的课程/单元/小节查询不看 mode。
        task_mode: RunMode = "mode2" if task_type == "browser_burst" else "mode1"
        if task_type == "browser_burst" and not state.cookies:
            return jsonify(success=False, error="模式 2 需要先登录并保存 Cookie")
        if not state.task_status in ["idle", "completed", "error"]:
            return jsonify(success=False, error="已有任务在进行中")
        # 清理已结束的线程；若还有任务在收尾（例如刚切换过模式），先拒绝新任务避免并发
        state.active_threads = [t for t in state.active_threads if t.is_alive()]
        if state.active_threads:
            return jsonify(success=False, error="上一个任务仍在收尾，请稍后再试")
        state.stop_event.clear()
        state.progress = {"current": 0, "total": 0}
        log_message(data)
        lessons = data.get("lessonIds", [])

        if task_type == "browser_burst":
            selected_sections = data.get("selectedSections")
            headless = bool(data.get("headless", True))
            try:
                idle_timeout = max(5, min(300, int(data.get("idleTimeout", 15))))
            except (TypeError, ValueError):
                return jsonify(success=False, error="空闲等待时间必须是整数")
            thread = SeleniumAutoLearnThread(
                lessons,
                selected_sections,
                headless=headless,
                idle_timeout=idle_timeout,
            )
        elif task_type == "brain_burst":
            rate = data["rate"]
            selected_sections = data.get("selectedSections")
            offset = data.get("offset")
            thread = BrainBurstThread(lessons, rate, selected_sections, offset)
        elif task_type == "away_from_keyboard":
            duration = data["time"]
            selected_sections = data.get("selectedSections")
            thread = AwayFromKeyboardThread(lessons, duration, selected_sections)
        else:
            return jsonify(success=False, error="未知任务类型")

        # 只有走到这里、任务参数与前置条件都通过后，才正式设置本次运行模式。
        state.mode = task_mode
        save_config(mode=state.mode)
        log_message(f"最终提交任务，运行模式设置为 {state.mode}", "APPINFO")
        state.active_threads.append(thread)
        thread.start()
        return jsonify(success=True, error="")
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/stop", methods=["POST"])
def stop_tasks():
    """停止所有任务"""
    try:
        state.stop_event.set()
        for t in state.active_threads:
            t.join(timeout=5)
        state.reset()
        return jsonify(success=True, error="")
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/status", methods=["GET"])
def get_status():
    """获取状态信息"""
    return jsonify(
        {
            "status": state.task_status,
            "progress": state.progress,
            "activeThreads": len(state.active_threads),
            "mode": state.mode,
            "success": True,
        }
    )


@app.route("/api/getLog", methods=["GET"])
def get_log():
    """返回指定分类的日志；type=app|access|system|all，默认只看业务日志"""
    try:
        wanted = (request.args.get("type") or "app").lower()
        if wanted == "all":
            kinds = LOG_KINDS
        else:
            kinds = tuple(kind for kind in LOG_KINDS if kind.lower() == wanted)
            if not kinds:
                return jsonify(
                    success=False, error="type 只能是 app、access、system 或 all", logs=""
                )
        if len(kinds) == 1:
            logs = state.log_buffers[kinds[0]].getvalue()
        else:
            # 合并时按行排序，保持时间顺序可读
            lines = []
            for kind in kinds:
                lines.extend(state.log_buffers[kind].getvalue().splitlines())
            logs = "\n".join(sorted(lines))
        return jsonify(success=True, logs=logs, type=wanted)
    except Exception as e:
        return jsonify(success=False, error=str(e), logs="")


@app.route("/api/getUserInfo", methods=["GET"])
def get_user_info_route():
    return get_user_info(client)


@app.route("/api/reset", methods=["GET"])
def reset():
    """重置状态"""
    stop_tasks()
    state.reset()
    return jsonify(success=True, error="")


@app.route("/api/shutdown", methods=["GET"])
def exit():
    """退出应用"""
    os.kill(os.getpid(), signal.SIGINT)


# ====================== 业务逻辑 ======================
def bootstrap_config() -> None:
    """启动时读取 Cookie；上次任务模式只作记录，不提前激活。"""
    try:
        config = load_config()
        if not config:
            log_message("未找到配置文件")
        saved_mode = config.get("mode")
        if saved_mode in AVAILABLE_MODES:
            log_message(f"上次任务使用模式：{saved_mode}；当前模式将在最终提交任务时确定")
        if cookies_str := config.get("cookies"):
            cookie_dict = parse_cookie_string(cookies_str)
            log_message("配置加载成功，正在尝试自动登录……")
            if validate_cookies(cookie_dict):
                state.cookies = cookie_dict
                client.cookies.update(cookie_dict)
                log_message("自动登录成功")
                state.task_status = "idle"
            else:
                log_message("自动登录失败，请更新 Cookie")
    except Exception as e:
        log_message(f"配置加载失败: {str(e)}")


def interrupt_tasks() -> int:
    """请求中断正在运行的服务端任务（保留登录状态），返回被请求中断的任务数。

    仍在收尾的线程会保留在 active_threads 中，避免与后续任务并发。
    """
    running = [t for t in state.active_threads if t.is_alive()]
    state.active_threads = running
    if not running:
        return 0
    state.stop_event.set()
    for t in running:
        t.join(timeout=TASK_JOIN_TIMEOUT)
    state.active_threads = [t for t in running if t.is_alive()]
    if state.task_status in ("brain_burst", "away_from_keyboard"):
        state.task_status = "idle" if state.cookies else "nologon"
    return len(running)


def validate_cookies(cookies: Dict[str, str]) -> bool:
    """验证Cookie有效性"""
    try:
        session = tls_client.Session(
            client_identifier="chrome_120", random_tls_extension_order=True
        )
        # 常见浏览器请求头
        session.headers.update(
            {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "cache-control": "no-cache",
                "upgrade-insecure-requests": "1",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "sec-ch-ua": '"Chromium";v="120", "Not?A_Brand";v="8", "Google Chrome";v="120"',
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-mobile": "?0",
            }
        )
        # 注入用户提供的 Cookie
        for k, v in cookies.items():
            try:
                session.cookies.set(k, v, domain="welearn.sflep.com")
                session.cookies.set(k, v, domain=".sflep.com")
                session.cookies.set(k, v, domain="sso.sflep.com")
            except Exception:
                pass

        test_url = "https://welearn.sflep.com/user/userinfo.aspx"
        resp = session.get(
            test_url, headers={"Referer": "https://welearn.sflep.com/student/index.aspx"}
        )
        log_message(f"Cookie 校验：HTTP {resp.status_code}，{len(resp.text)} 字节", "APPDEBUG")
        text = resp.text
        if ("我的资料" in text) or ('id="user1"' in text):
            return True

        # 若命中阿里云 WAF 405 页面，尝试预热并重试
        if ("<title>405</title>" in text) or ("Your Request ID is" in text):
            _ = session.get(
                "https://welearn.sflep.com/user/prelogin.aspx?loginret=https://welearn.sflep.com/user/loginredirect.aspx"
            )
            resp2 = session.get(
                test_url, headers={"Referer": "https://welearn.sflep.com/student/index.aspx"}
            )
            text2 = resp2.text
            log_message(f"Cookie 校验重试：HTTP {resp2.status_code}，{len(text2)} 字节", "APPDEBUG")
            return ("我的资料" in text2) or ('id="user1"' in text2)

        return False
    except Exception as e:
        log_message(f"Cookie验证失败: {str(e)}")
        return False


class BrainBurstThread(threading.Thread):
    """智能刷课线程"""

    def __init__(
        self,
        lessonIds: List[str],
        rate: int | str,
        selectedSections: Optional[Dict[str, List[str]]] = None,
        offset: Optional[str] = None,
    ):
        super().__init__()
        self.daemon = True
        self.lessonIds = lessonIds
        self.rate = int(rate) if "-" not in rate else tuple(map(int, rate.split("-")))
        self.selectedSections = selectedSections or {}
        self.offset = offset

    def run(self):
        try:
            state.task_status = "brain_burst"
            session = build_tls_session()
            infoHeaders = {
                "Referer": f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}",
            }
            # 重新计算总数
            state.progress = {"current": 0, "total": 0}
            for lesson in self.lessonIds:  # 获取课程详细列表
                response = session.get(
                    f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={_global.cid}&uid={_global.uid}&unitidx={_global.lessonIndex.index(lesson)}&classid={_global.classid}",
                    headers=infoHeaders,
                )
                if "异常" in response.text or "出错了" in response.text:
                    state.task_status = "error"
                    log_message(f"获取课程 {lesson} 详细列表失败: {response.text}", "APPERR")
                    return
                sections = response.json()["info"]
                # 仅处理选择的小节（如果有）
                if lesson in self.selectedSections and self.selectedSections[lesson]:
                    wanted = set(self.selectedSections[lesson])
                    sections = [s for s in sections if s.get("id") in wanted]
                # 累加总数
                state.progress["total"] += len(sections)
                for section in sections:  # 获取课程的小节列表并刷课
                    log_message(f"课程 {lesson} 详情：HTTP {response.status_code}", "APPDEBUG")
                    if section["isvisible"] == "false":
                        log_message(f"跳过未开放课程 {section['location']}", "APPERR")
                        log_message(
                            f"小节 {section.get('id')}：{section.get('iscomplete')}", "APPDEBUG"
                        )
                    elif "未" in section["iscomplete"]:
                        log_message(f"正在完成 {section['location']}", "APPINFO")
                        if isinstance(self.rate, int):
                            crate = self.rate
                        else:
                            crate = str(random.randint(*self.rate))
                        data = (
                            '{"cmi":{"completion_status":"completed","interactions":[],"launch_data":"","progress_measure":"1","score":{"scaled":"'
                            + str(crate)
                            + '","raw":"100"},"session_time":"0","success_status":"unknown","total_time":"0","mode":"normal"},"adl":{"data":[]},"cci":{"data":[],"service":{"dictionary":{"headword":"","short_cuts":""},"new_words":[],"notes":[],"writing_marking":[],"record":{"files":[]},"play":{"offline_media_id":"9999"}},"retry_count":"0","submit_time":""}}[INTERACTIONINFO]'
                        )
                        id = section["id"]
                        # 第一种刷课方法
                        session.post(
                            "https://welearn.sflep.com/Ajax/SCO.aspx",
                            data={
                                "action": "startsco160928",
                                "cid": _global.cid,
                                "scoid": id,
                                "uid": _global.uid,
                            },
                            headers={
                                "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={_global.cid}&classid={_global.classid}&sco={id}"
                            },
                        )
                        response = session.post(
                            "https://welearn.sflep.com/Ajax/SCO.aspx",
                            data={
                                "action": "setscoinfo",
                                "cid": _global.cid,
                                "scoid": id,
                                "uid": _global.uid,
                                "data": data,
                                "isend": "False",
                            },
                            headers={
                                "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={_global.cid}&classid={_global.classid}&sco={id}"
                            },
                        )
                        if '"ret":0' in response.text:
                            log_message(
                                f"刷课结果：以 {crate}% 的正确率使用“第一类刷课法”完成了课程 {section['location']}",
                                "APPINFO",
                            )
                            # 第 N 类刷课法 neta 了高数的第 N 类积分法
                            state.progress["current"] += 1
                            # 小节错开（仅刷课模式）
                            if self.offset:
                                try:
                                    if "-" in self.offset:
                                        a, b = map(int, self.offset.split("-"))
                                        time.sleep(random.uniform(a, b))
                                    else:
                                        time.sleep(int(self.offset))
                                except Exception:
                                    pass
                            continue
                        else:  # 第二种刷课法
                            response = session.post(
                                "https://welearn.sflep.com/Ajax/SCO.aspx",
                                data={
                                    "action": "savescoinfo160928",
                                    "cid": _global.cid,
                                    "scoid": id,
                                    "uid": _global.uid,
                                    "progress": "100",
                                    "crate": crate,
                                    "status": "unknwon",
                                    "cstatus": "completed",
                                    "trycount": "0",
                                },
                                headers={
                                    "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={_global.cid}&classid={_global.classid}&sco={id}"
                                },
                            )
                            if '"ret":0' in response.text:
                                log_message(
                                    f"刷课结果：以 {crate}% 的正确率使用“第二类刷课法”完成了课程 {section['location']}",
                                    "APPINFO",
                                )
                                state.progress["current"] += 1
                                if self.offset:
                                    try:
                                        if "-" in self.offset:
                                            a, b = map(int, self.offset.split("-"))
                                            time.sleep(random.uniform(a, b))
                                        else:
                                            time.sleep(int(self.offset))
                                    except Exception:
                                        pass
                                continue
                    else:
                        state.progress["current"] += 1
                        log_message(f"跳过已完成课程 {section['location']}", "APPINFO")
                log_message(f"课程 {lesson} 刷课完成", "APPINFO")
            state.task_status = "completed"
        except Exception as e:
            log_message(f"刷课任务出错: {str(e)}")
            state.progress = {"current": 0, "total": 1}
            state.task_status = "error"


class AwayFromKeyboardThread(threading.Thread):
    """挂机刷时长线程"""

    def __init__(
        self,
        lessonIds: List[str],
        duration: str,
        selectedSections: Optional[Dict[str, List[str]]] = None,
    ):
        super().__init__()
        self.lessonIds = lessonIds
        self.duration = duration
        self.daemon = True
        self.wrong_lessons = []
        self.max_threads = 64
        self.retry_delay = 3
        self.max_retries = 100
        self.selectedSections = selectedSections or {}

    def _http_request_with_retry(self, method, url, **kwargs):
        """带有重试机制的 HTTP 请求"""
        for attempt in range(self.max_retries):
            try:
                if method.upper() == "GET":
                    response = self.session.get(url, **kwargs)
                else:
                    response = self.session.post(url, **kwargs)
                return response
            except Exception as e:
                if attempt < self.max_retries - 1:
                    log_message(
                        f"请求 {url} 失败 ({str(e)})，{self.retry_delay} 秒后重试...",
                        "APPERR",
                    )
                    time.sleep(self.retry_delay)
                else:
                    log_message(f"请求 {url} 失败，已达最大重试次数", "APPERR")
                    raise

    def _process_section(self, section):
        """处理单个课程小节"""
        try:
            log_message(f"开始处理: {section['location']}", "APPINFO")
            scoid = section["id"]
            url = "https://welearn.sflep.com/Ajax/SCO.aspx"

            # 获取初始学习状态
            response = self._http_request_with_retry(
                "POST",
                url,
                data={
                    "action": "getscoinfo_v7",
                    "uid": _global.uid,
                    "cid": _global.cid,
                    "scoid": scoid,
                },
                headers={"Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"},
            )

            if "学习数据不正确" in response.text:
                self._http_request_with_retry(
                    "POST",
                    url,
                    data={
                        "action": "startsco160928",
                        "uid": _global.uid,
                        "cid": _global.cid,
                        "scoid": scoid,
                    },
                    headers={"Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"},
                )

            back = json.loads(response.text)["comment"]
            if "cmi" in back:
                cmi = json.loads(back)["cmi"]
                session_time = cmi.get("session_time", "0")
                total_time = cmi.get("total_time", "0")
            else:
                session_time = total_time = "0"

            # 选择目标学习时长（单位：秒）
            learn_time = (
                random.randint(*map(int, self.duration.split("-")))
                if "-" in self.duration
                else int(self.duration)
            )

            self._http_request_with_retry(
                "POST",
                url,
                data={
                    "action": "keepsco_with_getticket_with_updatecmitime",
                    "uid": _global.uid,
                    "cid": _global.cid,
                    "scoid": scoid,
                    "session_time": session_time,
                    "total_time": total_time,
                },
                headers={"Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"},
            )

            for current_time in range(1, learn_time + 1):
                time.sleep(1)
                if current_time % 60 == 0:
                    self._http_request_with_retry(
                        "POST",
                        url,
                        data={
                            "action": "keepsco_with_getticket_with_updatecmitime",
                            "uid": _global.uid,
                            "cid": _global.cid,
                            "scoid": scoid,
                            "session_time": str(int(session_time)),
                            "total_time": str(int(total_time)),
                        },
                        headers={"Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"},
                    )

            self._http_request_with_retry(
                "POST",
                url,
                data={
                    "action": "savescoinfo160928",
                    "cid": _global.cid,
                    "scoid": scoid,
                    "uid": _global.uid,
                    "progress": "100",
                    "crate": "100",
                    "status": "unknown",
                    "cstatus": "completed",
                    "trycount": "0",
                },
                headers={
                    "Referer": f"https://welearn.sflep.com/Student/StudyCourse.aspx?cid={_global.cid}&classid={_global.classid}&sco={scoid}"
                },
            )

            state.progress["current"] += 1
            log_message(f"完成学习: {section['location']} 耗时: {learn_time}秒", "APPINFO")

        except Exception as e:
            self.wrong_lessons.append(section["location"])
            log_message(f"处理失败: {section['location']} - {str(e)}", "APPERR")

    def run(self):
        """线程主函数"""
        try:
            state.task_status = "away_from_keyboard"
            self.session = build_tls_session()

            # 预加载所有选择单元的小节以计算总任务数，并保持与文档描述一致
            units_sections: List[List[Dict[str, Any]]] = []
            total_sections = 0

            for lesson_id in self.lessonIds:
                unit_index = _global.lessonIndex.index(lesson_id)
                sections = []
                while not state.stop_event.is_set():
                    try:
                        response = self._http_request_with_retry(
                            "GET",
                            f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={_global.cid}&uid={_global.uid}&unitidx={unit_index}&classid={_global.classid}",
                            headers={
                                "Referer": f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
                            },
                        )
                        sections = response.json().get("info", [])
                        break
                    except Exception:
                        time.sleep(self.retry_delay)
                if state.stop_event.is_set():
                    return
                # 过滤未开放小节
                visible_sections = [s for s in sections if s.get("isvisible") != "false"]
                # 如指定了选择的小节，则仅保留这些
                if lesson_id in self.selectedSections and self.selectedSections[lesson_id]:
                    wanted = set(self.selectedSections[lesson_id])
                    visible_sections = [s for s in visible_sections if s.get("id") in wanted]
                units_sections.append(visible_sections)
                total_sections += len(visible_sections)

            state.progress["total"] = total_sections

            # 并发处理每个单元的小节
            for sections in units_sections:
                thread_pool = []
                for section in sections:
                    if state.stop_event.is_set():
                        break
                    while len(thread_pool) >= self.max_threads:
                        thread_pool = [t for t in thread_pool if t.is_alive()]
                        time.sleep(1)
                        if state.stop_event.is_set():
                            break
                    t = threading.Thread(target=self._process_section, args=(section,))
                    t.start()
                    thread_pool.append(t)
                for t in thread_pool:
                    t.join()

            state.task_status = "completed"
            log_message(f"刷时长任务完成，失败章节数: {len(self.wrong_lessons)}", "APPINFO")
        except Exception as e:
            state.task_status = "error"
            log_message(f"刷时长任务异常终止: {str(e)}", "APPERR")


# 小节页出现练习内容的等待上限（秒），测试里可注入更小的值
SECTION_READY_TIMEOUT = 45
# 单个小节页面加载失败后的重试次数与页面加载超时
SECTION_LOAD_ATTEMPTS = 3
SECTION_LOAD_TIMEOUT = 45
# 连续这么多个小节加载失败就停下，避免在站点故障时把整个课程空跑一遍
SECTION_MAX_CONSECUTIVE_FAILURES = 5


class SectionUnavailable(RuntimeError):
    """站点拒绝进入某个小节（已完成且未开放复习等），可跳过并继续后续小节。"""


class SectionLoadFailed(RuntimeError):
    """小节页面重试后仍加载失败；跳过该小节，不影响其余小节。"""


class SeleniumAutoLearnThread(threading.Thread):
    """模式 2：使用 Selenium 打开真实课程页并在页面 DOM 中自动答题。"""

    def __init__(
        self,
        lesson_ids: List[str],
        selected_sections: Optional[Dict[str, List[str]]] = None,
        *,
        headless: bool = True,
        idle_timeout: int = 15,
    ):
        super().__init__(name="selenium-auto-learn", daemon=True)
        self.lesson_ids = lesson_ids
        self.selected_sections = selected_sections or {}
        self.headless = headless
        self.idle_timeout = idle_timeout
        self.driver: Any = None

    def _fetch_sections(self) -> List[Dict[str, Any]]:
        if not _global.cid or not _global.classid or not _global.uid:
            raise RuntimeError("请先在模式 2 中选择课程，无法获取课程元数据")
        all_sections: List[Dict[str, Any]] = []
        session = build_tls_session()
        for lesson_id in self.lesson_ids:
            try:
                unit_index = _global.lessonIndex.index(lesson_id)
            except ValueError as exc:
                raise RuntimeError(f"无效的单元 ID：{lesson_id}") from exc
            response = session.get(
                "https://welearn.sflep.com/ajax/StudyStat.aspx",
                params={
                    "action": "scoLeaves",
                    "cid": _global.cid,
                    "uid": _global.uid,
                    "unitidx": unit_index,
                    "classid": _global.classid,
                },
                headers={
                    "Referer": f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
                },
            )
            sections = response.json().get("info", [])
            # 该接口的 isvisible 并不可靠（实测某些单元整片为 "false"，但页面
            # 仍然展示并允许选择），因此可用性以用户勾选为准，而不是这个字段。
            wanted = self.selected_sections.get(lesson_id, [])
            if wanted:
                wanted_set = set(wanted)
                sections = [s for s in sections if s.get("id") in wanted_set]
            else:
                sections = [s for s in sections if s.get("isvisible") != "false"]
            all_sections.extend(sections)
        return all_sections

    def _course_url(self, scoid: str) -> str:
        return (
            "https://welearn.sflep.com/Student/StudyCourse.aspx"
            f"?cid={_global.cid}&classid={_global.classid}&sco={scoid}"
        )

    def _inject_for_current_page(self) -> None:
        source = selenium_auto.load_auto_learn_script()
        # 课程正文位于 contentFrame；必须注入到真正包含题目的文档，
        # 否则顶层实例永远没有提交记录，会被误判为「本节已完成」。
        where = selenium_auto.inject_into_exercise(self.driver, source)
        log_message(f"模式 2：已在 {where} 文档注入答题脚本", "APPINFO")

    def _open_section_page(self, label: str, url: str) -> bool:
        """打开练习页并等待文档就绪，失败按次数重试。

        返回 False 表示用户点了停止；重试仍失败时抛 SectionLoadFailed。
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, SECTION_LOAD_ATTEMPTS + 1):
            if state.stop_event.is_set():
                return False
            try:
                self.driver.get(url)
                if selenium_auto.wait_for_document_ready(
                    self.driver, timeout=SECTION_LOAD_TIMEOUT, stop_event=state.stop_event
                ):
                    return True
                if state.stop_event.is_set():
                    return False
                last_error = TimeoutError(f"页面 {SECTION_LOAD_TIMEOUT}s 内未完成加载")
            except Exception as exc:  # 导航超时、驱动报错都走同一套重试
                last_error = exc
            if attempt < SECTION_LOAD_ATTEMPTS:
                wait_seconds = attempt * 2
                log_message(
                    f"模式 2 打开小节失败（第 {attempt}/{SECTION_LOAD_ATTEMPTS} 次），"
                    f"{wait_seconds}s 后重试：{label} - {last_error}",
                    "APPERR",
                )
                if state.stop_event.wait(wait_seconds):
                    return False
        raise SectionLoadFailed(
            f"小节 {label} 重试 {SECTION_LOAD_ATTEMPTS} 次仍无法加载：{last_error}"
        )

    def _wait_for_section(self, section: Dict[str, Any]) -> int:
        label = section.get("location", section["id"])
        if not self._open_section_page(label, self._course_url(str(section["id"]))):
            return 0
        # 站点对已完成且未开放复习的小节会把 StudyCourse 重定向回课程页，
        # 这种情况必须明确报错，而不是干等练习框出现。
        deadline = time.monotonic() + SECTION_READY_TIMEOUT
        while time.monotonic() < deadline and not state.stop_event.is_set():
            if "course_info.aspx" in self.driver.current_url:
                raise SectionUnavailable(
                    f"课程 {label} 已被站点退回课程页（通常表示该小节已完成且未开放复习）"
                )
            if selenium_auto.find_exercise_frame(self.driver) is not None:
                break
            if selenium_auto._document_has_exercise(self.driver):
                break
            time.sleep(0.5)
        else:
            if not state.stop_event.is_set():
                # 出版社托管的视频/导读页没有 submitinfo，本来就没有可答的题，
                # 属于“该小节不适合模式 2”，不应让整批任务失败。
                raise SectionUnavailable(f"课程 {label} 没有练习内容（可能是视频或导读页）")
        self._inject_for_current_page()

        started = time.monotonic()
        last_changed = started
        previous = None
        settled_since: Optional[float] = None
        submitted = 0
        # 最长等待 30 分钟，避免页面异常时后台线程永久占用浏览器。
        hard_deadline = started + 1800
        while not state.stop_event.is_set():
            if time.monotonic() >= hard_deadline:
                raise TimeoutError(
                    f"课程 {section.get('location', section['id'])} 超过最大等待时间"
                )
            try:
                stats = selenium_auto.read_stats(self.driver)
                if stats is None:
                    self._inject_for_current_page()
                    time.sleep(1)
                    continue
                # 只把「确认提交成功」和「切换了题目页」算作进度；nextcmd 的点击次数
                # 会因为脚本反复点同一元素而持续增长，不能拿来判断是否还在推进。
                current = (
                    int(stats.get("submits", 0)),
                    str(stats.get("page", "")),
                )
                submitted = current[0]
                # 页面已是“已提交”状态（例如之前做过）时脚本不会再动手，等一个
                # 很短的空闲窗口即可收尾，不必耗满 idle_timeout。
                if stats.get("pageSettled") and current == previous:
                    if settled_since is None:
                        settled_since = time.monotonic()
                    elif time.monotonic() - settled_since >= 3:
                        return submitted
                else:
                    settled_since = None
                if previous is None or current != previous:
                    last_changed = time.monotonic()
                    if previous is not None and current[0] > previous[0]:
                        log_message(
                            f"模式 2 已提交 {current[0]} 题：{section.get('location', section['id'])}",
                            "APPINFO",
                        )
                previous = current
                # 最后一题没有可用的 nextcmd 后，统计值会稳定；连续 idle_timeout
                # 秒没有任何提交/跳题动作，即认为当前小节完成。
                if time.monotonic() - last_changed >= self.idle_timeout:
                    return submitted
            except Exception as exc:
                log_message(f"读取页面状态失败，将重试：{exc}", "APPERR")
            time.sleep(1)
        return submitted

    def run(self) -> None:
        state.task_status = "browser_burst"
        try:
            environment = selenium_auto.detect_environment()
            if not environment["ready"]:
                missing = ", ".join(environment["missing"])
                raise RuntimeError(f"模式 2 环境未就绪，缺少：{missing}")
            if not state.cookies:
                raise RuntimeError("模式 2 需要先登录并保存 Cookie")

            sections = self._fetch_sections()
            if not sections:
                wanted_total = sum(len(v) for v in self.selected_sections.values())
                raise RuntimeError(
                    f"没有找到可处理的小节（已选 {wanted_total} 节），"
                    "请确认单元与小节仍属于当前课程后重新选择"
                )
            # 已完成且未开放复习的小节，站点会直接退回课程页；提前剔除，
            # 避免为几百个注定跳过的小节逐个打开浏览器页面。
            pending_sections = [
                section
                for section in sections
                if not (
                    str(section.get("iscomplete", "")) == "已完成"
                    and str(section.get("enablereview", "")).lower() != "true"
                )
            ]
            state.progress = {"current": 0, "total": len(sections)}
            already_done = len(sections) - len(pending_sections)
            if already_done:
                log_message(
                    f"模式 2：所选小节中 {already_done} 节已完成且未开放复习，将直接跳过；"
                    f"实际处理 {len(pending_sections)} 节",
                    "APPINFO",
                )
            sections = pending_sections

            self.driver = selenium_auto.build_driver(
                headless=self.headless,
                logger=lambda message: log_message(f"模式 2 浏览器：{message}", "APPINFO"),
            )
            log_message("模式 2：打开登录域并载入 Cookie", "APPINFO")
            added = selenium_auto.inject_cookies(
                self.driver, state.cookies, stop_event=state.stop_event
            )
            if not added:
                raise RuntimeError("浏览器未成功载入任何 Cookie")
            log_message("模式 2：打开用户资料页验证登录", "APPINFO")
            self.driver.get("https://welearn.sflep.com/user/userinfo.aspx")
            selenium_auto.wait_for_document_ready(
                self.driver, timeout=45, stop_event=state.stop_event
            )
            if not selenium_auto.logged_in(self.driver):
                raise RuntimeError("Cookie 无效或已过期，请重新登录后再启动模式 2")

            # 提前跳过的小节也要计入进度与统计，否则进度条和汇总会对不上
            completed = failed = 0
            skipped = already_done
            state.progress["current"] = already_done
            consecutive_failures = 0
            for section in sections:
                if state.stop_event.is_set():
                    return
                label = section.get("location", section.get("id"))
                log_message(f"模式 2 开始处理：{label}", "APPINFO")
                try:
                    submitted = self._wait_for_section(section)
                except SectionUnavailable as exc:
                    state.progress["current"] += 1
                    skipped += 1
                    consecutive_failures = 0
                    log_message(f"模式 2 跳过小节：{exc}", "APPERR")
                    continue
                except SectionLoadFailed as exc:
                    state.progress["current"] += 1
                    failed += 1
                    consecutive_failures += 1
                    log_message(f"模式 2 小节加载失败，已跳过：{exc}", "APPERR")
                    if consecutive_failures >= SECTION_MAX_CONSECUTIVE_FAILURES:
                        raise RuntimeError(
                            f"连续 {consecutive_failures} 个小节加载失败，"
                            "已停止任务，请检查网络或稍后重试"
                        ) from exc
                    continue
                state.progress["current"] += 1
                consecutive_failures = 0
                if submitted:
                    completed += 1
                    log_message(f"模式 2 完成小节：{label}，提交 {submitted} 题", "APPINFO")
                else:
                    skipped += 1
                    log_message(
                        f"模式 2 跳过小节：{label} 没有待答题目（已完成或非练习页）",
                        "APPINFO",
                    )
            log_message(
                f"模式 2 全部结束：提交 {completed} 节、跳过 {skipped} 节、失败 {failed} 节",
                "APPINFO",
            )
            state.task_status = "completed"
        except Exception as exc:
            state.task_status = "error"
            log_message(f"模式 2 任务异常：{exc}", "APPERR")
        finally:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass


# ====================== 启动配置 ======================
if __name__ == "__main__":
    # 加载配置文件（恢复运行模式 + 尝试自动登录）
    bootstrap_config()

    port = get_port()
    log_message(
        f"启动完成：端口 {port}，Cookie {'已恢复' if state.cookies else '未登录'}",
        "SYSTEM",
    )
    log_message(f"已获取可用端口: {port}，绑定到 0.0.0.0:{port}")
    log_message(f"将自动打开浏览器访问 http://127.0.0.1:{port}，你也可以自己打开浏览器进行访问")
    webbrowser.open(f"http://127.0.0.1:{port}")
    # 启动应用
    app.run(host="0.0.0.0", port=port, debug=False)
