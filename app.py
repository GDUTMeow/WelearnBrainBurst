from __future__ import annotations
import json
import logging
import os
import re
import sys
import time
import random
import threading
import httpx
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

# ====================== 全局变量 ======================
class GLOBAL:
    cid: int = -1
    uid: int = -1
    classid:int = -1

_global = GLOBAL()

# ====================== 应用初始化 ======================
app = Flask(__name__)

# ====================== 日志系统配置 ======================
# 创建日志目录
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 配置文件处理器
file_handler = logging.FileHandler(
    os.path.join(log_dir, "latest.log"), mode="a", encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(message)s"))

# 配置控制台处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))

# 配置应用日志
app.logger.setLevel(logging.INFO)
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)


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

    current: int
    total: int


TaskStatusType = Literal[
    "idle", "brain_burst", "away_from_keyboard", "error", "completed", "nologon"
]


# ====================== 全局状态管理 ======================
class GlobalState:
    """全局状态管理器"""

    def __init__(self):
        self.reset()

    def reset(self):
        """重置所有状态"""
        self.current_task: Optional[threading.Thread] = None
        self.task_status: TaskStatusType = "nologon"
        self.progress: ProgressInfo = {"current": 0, "total": 0}
        self.log_buffer: IO[str] = StringIO()
        self.stop_event = threading.Event()
        self.active_threads: List[threading.Thread] = []
        self.cookies: Optional[Dict[str, str]] = None
        self.cid: Optional[str] = None
        self.uid: Optional[str] = None
        self.classid: Optional[str] = None


state = GlobalState()
client = httpx.Client()


# ====================== 日志工具 ======================
class LogCapture:
    """日志捕获上下文管理器"""

    def __init__(self, buffer: StringIO):
        self.buffer = buffer
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

    def __enter__(self):
        sys.stdout = self.buffer
        sys.stderr = self.buffer
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr


def log_message(message: str, log_type: str = "SYSTEM"):
    """统一日志记录函数"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{log_type}] {message}"
    state.log_buffer.write(log_entry + "\n")
    app.logger.info(log_entry)


# ====================== 中间件 ======================
@app.before_request
def record_request_start():
    """记录请求开始时间"""
    request.start_time = time.time()


@app.after_request
def log_access(response: Response):
    """记录访问日志"""
    latency = time.time() - request.start_time
    latency_ms = int(latency * 1000)

    log_entry = (
        f"{request.remote_addr} "
        f'"{request.method} {request.path}" '
        f"{response.status_code} "
        f"{latency_ms}ms "
        f"\"{request.headers.get('User-Agent', '')}\""
    )

    log_message(log_entry, "ACCESS")
    return response


# ====================== 工具函数 ======================
def get_user_info(client: httpx.Client) -> Dict[str, Optional[str]]:
    """
    从用户信息页面提取用户详细信息
    :param client: 已认证的httpx客户端
    :return: 包含用户信息的字典，格式为:
        {
            "username": "用户名",
            "name": "姓名",
            "student_id": "学号",
            "school": "学校",
            "birth_year": "出生年份"
        }
    """
    info_url = "https://welearn.sflep.com/user/userinfo.aspx"

    try:
        # 发送GET请求获取用户信息页面
        response = client.get(info_url)
        response.raise_for_status()

        # 使用BeautifulSoup解析HTML
        soup = BeautifulSoup(response.text, "html.parser")
        user_div = soup.find("div", id="user1")

        if not user_div:
            raise ValueError("未找到用户信息面板")

        # 使用CSS选择器精确查找各字段
        return {
            "username": _find_input_value(user_div, "lblAccount"),
            "name": _find_input_value(user_div, "txtName"),
            "student_id": _find_input_value(user_div, "txtStuNo"),
            "school": _find_selected_school(user_div),
            "birth_year": _find_selected_birthyear(user_div),
        }

    except httpx.HTTPStatusError as e:
        print(f"请求失败，状态码: {e.response.status_code}")
    except Exception as e:
        print(f"获取用户信息出错: {str(e)}")

    # 发生错误时返回空值字典
    return {
        key: None for key in ["username", "name", "student_id", "school", "birth_year"]
    }


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


# ====================== 路由定义 ======================
@app.route("/")
def index():
    """主页面"""
    return render_template("index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    """静态文件路由"""
    return send_from_directory("static", filename)


@app.route("/api/login", methods=["POST"])
def api_login(cookies: str = None):
    """登录接口"""
    state.reset()
    try:
        if not cookies:
            cookies = request.json.get("cookies", "")
        cookie_dict = dict(
            map(lambda x: x.split("=", 1), filter(None, cookies.split(";")))
        )

        if validate_cookies(cookie_dict):
            client.cookies.update(cookie_dict)
            state.cookies = cookie_dict
            with open("config.json", "w") as f:
                json.dump({"cookies": cookies}, f)
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
        return jsonify(success=False, error=str(e))


@app.route("/api/getCourses", methods=["GET"])
def get_courses():
    """获取课程列表"""
    if not state.cookies or state.task_status == "nologon":
        return jsonify(success=False, error="请先登录")

    try:
        url = f"https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc&nocache={round(random.random(), 16)}"
        response = client.get(
            url, headers={"Referer": "https://welearn.sflep.com/student/index.aspx"}
        )
        courses: List[CourseInfo] = response.json()["clist"]
        return jsonify(success=True, error="", courses=courses)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/getLessions", methods=["GET"])
def get_lessions():
    """获取课程节次列表"""
    if not state.cookies or state.task_status == "nologon":
        return jsonify(success=False, error="请先登录")

    try:
        _global.cid = request.args.get("cid")
        url = f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
        log_message(f"获取课程节次列表: {url}")
        response = client.get(
            url,
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        _global.uid = re.search('"uid":(.*?),', response.text).group(1)
        _global.classid = re.search('"classid":"(.*?)"', response.text).group(1)
        url = "https://welearn.sflep.com/ajax/StudyStat.aspx"
        response = client.get(
            url,
            params={"action": "courseunits", "cid": _global.cid, "uid": _global.uid},
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        lessions = response.json()["info"]
        return jsonify(success=True, error="", lessions=lessions)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/startTask", methods=["POST"])
def start_task():
    """启动任务接口"""
    try:
        if state.task_status != "idle":
            return jsonify(success=False, error="已有任务在进行中")

        data = request.json
        task_type = data["type"]
        unit_idx = data["unitIndex"]
        params = data["params"]

        if task_type == "brain_burst":
            thread = BrainBurstThread(unit_idx, params)
        elif task_type == "away_from_keyboard":
            thread = AwayFromKeyboardThread(unit_idx, params)
        else:
            return jsonify(success=False, error="未知任务类型")

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
        }
    )


@app.route("/api/getUserInfo", methods=["GET"])
def get_user_info_route():
    return get_user_info(client)


# ====================== 业务逻辑 ======================
def validate_cookies(cookies: Dict[str, str]) -> bool:
    """验证Cookie有效性"""
    try:
        test_url = "https://welearn.sflep.com/user/userinfo.aspx"
        resp = client.get(test_url, cookies=cookies)
        return "我的资料" in resp.text
    except Exception as e:
        log_message(f"Cookie验证失败: {str(e)}")
        return False


class BrainBurstThread(threading.Thread):
    """智能刷课线程"""

    def __init__(self, unit_idx: int, mycrate: Union[int, Tuple[int, int]]):
        super().__init__()
        self.unit_idx = unit_idx
        self.mycrate = mycrate
        self.daemon = True

    def run(self):
        try:
            state.task_status = "brain_burst"
            # 这里添加具体的刷课逻辑
            # 示例进度更新
            for i in range(1, 101):
                if state.stop_event.is_set():
                    break
                state.progress = {"current": i, "total": 100}
                time.sleep(0.1)
            state.task_status = "completed"
        except Exception as e:
            log_message(f"刷课任务出错: {str(e)}")
            state.task_status = "error"


class AwayFromKeyboardThread(threading.Thread):
    """挂机刷时长线程"""

    def __init__(self, unit_idx: int, duration: Union[int, Tuple[int, int]]):
        super().__init__()
        self.unit_idx = unit_idx
        self.duration = duration
        self.daemon = True

    def run(self):
        try:
            state.task_status = "away_from_keyboard"
            # 这里添加具体的挂机逻辑
            # 示例进度更新
            total = (
                random.randint(*self.duration)
                if isinstance(self.duration, tuple)
                else self.duration
            )
            for i in range(total):
                if state.stop_event.is_set():
                    break
                state.progress = {"current": i, "total": total}
                time.sleep(1)
            state.task_status = "completed"
        except Exception as e:
            log_message(f"挂机任务出错: {str(e)}")
            state.task_status = "error"


# ====================== 启动配置 ======================
if __name__ == "__main__":
    # 加载配置文件
    try:
        with open("config.json") as f:
            config = json.load(f)
            if cookies_str := config.get("cookies"):
                cookie_dict = dict(
                    map(lambda x: x.split("=", 1), cookies_str.split(";"))
                )
                log_message("配置加载成功，正在尝试自动登录……")
                if validate_cookies(cookie_dict):
                    state.cookies = cookie_dict
                    client.cookies.update(cookie_dict)
                    log_message("自动登录成功")
                    state.task_status = "idle"
                else:
                    log_message("自动登录失败，请更新 Cookie")
    except FileNotFoundError:
        log_message("未找到配置文件")
    except Exception as e:
        log_message(f"配置加载失败: {str(e)}")

    # 启动应用
    with LogCapture(state.log_buffer):
        app.run(host="0.0.0.0", port=5000, debug=True)
