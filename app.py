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

# ====================== 应用初始化 ======================
app = Flask(__name__)


# ====================== 日志系统配置 ======================
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
file_handler = logging.FileHandler(
    os.path.join(log_dir, "latest.log"), mode="a", encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(message)s"))

# 新增分类日志处理器
system_handler = logging.FileHandler(
    os.path.join(log_dir, "system.log"), mode="a", encoding="utf-8"
)
system_handler.setLevel(logging.INFO)
system_handler.setFormatter(logging.Formatter("%(message)s"))
system_handler.addFilter(LogTypeFilter("SYSTEM"))

app_handler = logging.FileHandler(
    os.path.join(log_dir, "app.log"), mode="a", encoding="utf-8"
)
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

# 配置应用日志
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


# ====================== 日志工具 ======================
def log_message(message: str, log_type: str = "SYSTEM"):
    """统一日志记录函数"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{log_type}] {message}"
    state.log_buffer.write(log_entry + "\n")
    app.logger.info(log_entry, extra={"log_type": log_type})


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
        log_message(f"尝试登录: {cookie_dict}")
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
        log_message("用户未登录", "APPERR")
        return jsonify(success=False, error="请先登录")

    try:
        url = f"https://welearn.sflep.com/ajax/authCourse.aspx?action=gmc&nocache={round(random.random(), 16)}"
        response = client.get(
            url, headers={"Referer": "https://welearn.sflep.com/student/index.aspx"}
        )
        log_message(f"获取课程列表: {response.text}", "APPDEBUG")
        courses: List[CourseInfo] = response.json()["clist"]
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
        url = f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}"
        response = client.get(
            url,
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        log_message(f"成功获取到返回：{response.text}", "APPDEBUG")
        _global.uid = re.search('"uid":(.*?),', response.text).group(1)
        _global.classid = re.search('"classid":"(.*?)"', response.text).group(1)
        log_message(
            f"成功解析到单元元数据: uid={_global.uid}, classid={_global.classid}",
            "APPDEBUG",
        )
        url = "https://welearn.sflep.com/ajax/StudyStat.aspx"
        response = client.get(
            url,
            params={"action": "courseunits", "cid": _global.cid, "uid": _global.uid},
            headers={"Referer": "https://welearn.sflep.com/student/course_info.aspx"},
        )
        log_message(f"获取课程单元列表: {response.text}", "APPDEBUG")
        lessons = response.json()["info"]
        _global.lessonList = lessons
        _global.lessonIndex = [i.get("id") for i in lessons]
        return jsonify(success=True, error="", lessons=lessons)
    except Exception as e:
        return jsonify(success=False, error=str(e))


@app.route("/api/startTask", methods=["POST"])
def start_task():
    """启动任务接口"""
    try:
        if not state.task_status in ["idle", "completed", "error"]:
            return jsonify(success=False, error="已有任务在进行中")
        state.progress = {"current": 0, "total": 0}
        data = request.json
        log_message(data)
        task_type = data["type"]
        lessons = data["lessonIds"]

        if task_type == "brain_burst":
            rate = data["rate"]
            thread = BrainBurstThread(lessons, rate)
        elif task_type == "away_from_keyboard":
            duration = data["time"]
            thread = AwayFromKeyboardThread(lessons, duration)
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
            "success": True,
        }
    )


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
def validate_cookies(cookies: Dict[str, str]) -> bool:
    """验证Cookie有效性"""
    try:
        test_url = "https://welearn.sflep.com/user/userinfo.aspx"
        resp = client.get(test_url, cookies=cookies)
        log_message(f"Cookie验证返回：{resp.text}", "APPDEBUG")
        return "我的资料" in resp.text
    except Exception as e:
        log_message(f"Cookie验证失败: {str(e)}")
        return False


class BrainBurstThread(threading.Thread):
    """智能刷课线程"""

    def __init__(self, lessonIds: List[str], rate: int | str):
        super().__init__()
        self.daemon = True
        self.lessonIds = lessonIds
        self.rate = int(rate) if "-" not in rate else tuple(map(int, rate.split("-")))

    def run(self):
        try:
            state.task_status = "brain_burst"
            infoHeaders = {
                "Referer": f"https://welearn.sflep.com/student/course_info.aspx?cid={_global.cid}",
            }
            for lesson in self.lessonIds:  # 获取课程详细列表
                response = client.get(
                    f"https://welearn.sflep.com/ajax/StudyStat.aspx?action=scoLeaves&cid={_global.cid}&uid={_global.uid}&unitidx={_global.lessonIndex.index(lesson)}&classid={_global.classid}",
                    headers=infoHeaders,
                )
                resp = response
                if "异常" in response.text or "出错了" in response.text:
                    state.task_status = "error"
                    log_message(
                        f"获取课程 {lesson} 详细列表失败: {response.text}", "APPERR"
                    )
                    return
                state.progress = {
                    "current": state.progress["current"],
                    "total": len(response.json()["info"]) * len(self.lessonIds),
                }
                for section in response.json()["info"]:  # 获取课程的小节列表并刷课
                    log_message(
                        f"获取到课程 {lesson} 的详细信息 {response.json()}", "APPDEBUG"
                    )
                    if section["isvisible"] == "false":
                        log_message(f'跳过未开放课程 {section["location"]}', "APPERR")
                        log_message(f"课程 {lesson} 的返回信息：{section}", "APPDEBUG")
                    elif "未" in section["iscomplete"]:
                        log_message(f'正在完成 {section["location"]}', "APPINFO")
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
                        client.post(
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
                        response = client.post(
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
                            continue
                        else:  # 第二种刷课法
                            response = client.post(
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
                                continue
                    else:
                        state.progress["current"] += 1
                        log_message(f'跳过已完成课程 {section["location"]}', "APPINFO")
                log_message(f"课程 {lesson} 刷课完成", "APPINFO")
            state.task_status = "completed"
        except Exception as e:
            log_message(f"刷课任务出错: {str(e)}")
            state.progress = {
                "current": 0,
                "total": 1
            }
            state.task_status = "error"


class AwayFromKeyboardThread(threading.Thread):
    """挂机刷时长线程"""

    def __init__(self, lessonIds: List[str], duration: str):
        super().__init__()
        self.lessonIds = lessonIds
        self.duration = duration
        self.daemon = True
        self.wrong_lessons = []
        self.max_threads = 64
        self.retry_delay = 3
        self.max_retries = 100

    def _http_request_with_retry(self, method, url, **kwargs):
        """带有重试机制的 HTTP 请求"""
        for attempt in range(self.max_retries):
            try:
                response = client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.HTTPError, Exception) as e:
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
            log_message(f'开始处理: {section["location"]}', "APPINFO")
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
                headers={
                    "Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"
                },
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
                    headers={
                        "Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"
                    },
                )

            back = json.loads(response.text)["comment"]
            if "cmi" in back:
                cmi = json.loads(back)["cmi"]
                session_time = cmi.get("session_time", "0")
                total_time = cmi.get("total_time", "0")
            else:
                session_time = total_time = "0"

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
                headers={
                    "Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"
                },
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
                            "session_time": str(int(session_time) + current_time),
                            "total_time": str(int(total_time) + current_time),
                        },
                        headers={
                            "Referer": "https://welearn.sflep.com/student/StudyCourse.aspx"
                        },
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
            log_message(
                f'完成学习: {section["location"]} 耗时: {learn_time}秒', "APPINFO"
            )

        except Exception as e:
            self.wrong_lessons.append(section["location"])
            log_message(f'处理失败: {section["location"]} - {str(e)}', "APPERR")

    def run(self):
        """线程主函数"""
        try:
            state.task_status = "away_from_keyboard"
            total_lessons = 0

            for lesson_id in self.lessonIds:
                unit_index = _global.lessonIndex.index(lesson_id)
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

                total_lessons += len(sections)
                state.progress["total"] = total_lessons

                thread_pool = []
                for section in sections:
                    if state.stop_event.is_set():
                        break
                    if section["isvisible"] == "false":
                        continue
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
            log_message(
                f"刷时长任务完成，失败章节数: {len(self.wrong_lessons)}", "APPINFO"
            )
        except Exception as e:
            state.task_status = "error"
            log_message(f"刷时长任务异常终止: {str(e)}", "APPERR")


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

    port = get_port()
    log_message(f"已获取可用端口: {port}，绑定到 0.0.0.0:{port}")
    log_message(
        f"将自动打开浏览器访问 http://127.0.0.1:{port}，你也可以自己打开浏览器进行访问"
    )
    webbrowser.open(f"http://127.0.0.1:{port}")
    # 启动应用
    with LogCapture(state.log_buffer):
        app.run(host="0.0.0.0", port=port, debug=False)
