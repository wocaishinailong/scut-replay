"""Authenticated read-only platform adapter using a dedicated local browser profile."""
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from playwright.sync_api import sync_playwright

from .core import atomic_json, lesson_date

BASE = "https://video.jw.scut.edu.cn"


def safe_url(url):
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in
             {"token", "access_token", "authorization", "ticket", "password", "sign", "signature"}]
    return urlunparse(parsed._replace(query=urlencode(query)))


class SiteError(RuntimeError):
    pass


def schedule_days(response):
    result = response.get("result", {})
    # Verified platform response for a week with no enrolled lessons (e.g. summer break).
    if response.get("success") is True and result.get("code") == 400 and result.get("msg") == "课表为空":
        return []
    if response.get("success") is True and result.get("code") == 200 and isinstance(result.get("list"), list):
        return result["list"]
    raise SiteError("我的课表响应格式发生变化，未将空结果视为成功。")


class Site:
    def __init__(self, root: Path, *, headless=False, login_timeout=600):
        self.root = root
        self.headless = headless
        self.login_timeout = login_timeout
        self.auth = None
        self.observed = {}
        self.schedule_params = None

    def __enter__(self):
        profile = self.root / "browser"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        self.pw = sync_playwright().start()
        try:
            self.context = self.pw.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.on("request", self._request)
            self.page.on("response", self._response)
            self.page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            self.login()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _request(self, request):
        parsed = urlparse(request.url)
        if parsed.netloc == urlparse(BASE).netloc and parsed.path.startswith(("/courseapi/", "/userapi/", "/coursesourceapi/")):
            value = request.headers.get("authorization")
            if value:
                # Remains in memory; never printed or written to application logs.
                self.auth = value
            if parsed.path == "/courseapi/v2/schedule/get-week-schedules":
                self.schedule_params = dict(parse_qsl(parsed.query))

    def _response(self, response):
        parsed = urlparse(response.url)
        if parsed.netloc != urlparse(BASE).netloc:
            return
        if parsed.path.startswith(("/courseapi/", "/coursesourceapi/", "/pptnote/v1/schedule/")) and response.request.method == "GET":
            try:
                self.observed[safe_url(response.url)] = response.json()
            except Exception:
                pass

    def login(self):
        deadline = time.monotonic() + self.login_timeout
        announced = False
        while time.monotonic() < deadline:
            if self.auth and urlparse(self.page.url).netloc == urlparse(BASE).netloc:
                return
            if not announced:
                print("请在新打开的专用 Chrome 窗口完成学校登录；程序将自动继续。", flush=True)
                announced = True
                if self.headless:
                    raise SiteError("登录已失效，请先运行 login 命令。")
            self.page.wait_for_timeout(500)
        raise SiteError("等待登录超时。登录后可重新运行，已有处理记录不会丢失。")

    def get(self, path, params=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only same-origin API paths are accepted")
        response = self.context.request.get(
            BASE + path, params=params,
            headers={"Authorization": self.auth} if self.auth else {}, timeout=60000,
        )
        if response.status in (401, 403):
            raise SiteError(f"接口拒绝访问（HTTP {response.status}），请重新登录并确认课程权限。")
        if not response.ok:
            raise SiteError(f"平台接口失败：HTTP {response.status}")
        try:
            data = response.json()
        except Exception as exc:
            raise SiteError("平台返回了非 JSON 响应，可能需要重新登录。") from exc
        if data.get("code") in (5002, 401, 403):
            raise SiteError("平台登录失效或缺少课程权限。")
        return data

    def __exit__(self, *args):
        if hasattr(self, "context"):
            self.context.close()
        if hasattr(self, "pw"):
            self.pw.stop()

    def courses(self, start, end):
        """Discover only courses in this user's schedule; no school-wide search."""
        deadline = time.monotonic() + 30
        while self.schedule_params is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(200)
        if not self.schedule_params:
            raise SiteError("未捕获我的课程课表请求，请在网站首页确认课表已加载。")
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last or (last-first).days > 370:
            raise ValueError("课程发现范围须为最多370天的有效日期区间")
        found = {}
        cursor = first
        while cursor <= last:
            stop = min(last, cursor + timedelta(days=6))
            params = {**self.schedule_params, "start_at": cursor.isoformat(), "end_at": stop.isoformat()}
            response = self.get("/courseapi/v2/schedule/get-week-schedules", params)
            for day in schedule_days(response):
                if not start <= day["day"] <= end:
                    continue
                for item in day.get("course", []):
                    cid = str(item["course_id"])
                    row = found.setdefault(cid, {"course_id": cid, "name": item["course_title"],
                        "teacher": item.get("lecturer_name", ""), "first_date": day["day"], "last_date": day["day"]})
                    row["first_date"] = min(row["first_date"], day["day"])
                    row["last_date"] = max(row["last_date"], day["day"])
            cursor = stop + timedelta(days=1)
        courses = sorted(found.values(), key=lambda c: (c["name"], c["course_id"]))
        atomic_json(self.root / "courses.json", {"range": [start, end], "courses": courses})
        return courses

    def term_start(self, reference):
        response = self.get("/courseapi/index.php/v2/course/get-term-years", {"tenant":21})
        groups = response.get("result",{}).get("data",{})
        starts = []
        if isinstance(groups,dict):
            for rows in groups.values():
                if isinstance(rows,list):
                    for term in rows:
                        if term.get("begin_date", "9999") <= reference <= term.get("end_date", "0000"):
                            starts.append(term["begin_date"])
        return min(starts) if starts else None

    def catalogue(self, course):
        data = self.get("/courseapi/v2/course/catalogue", {"course_id": course["course_id"]})
        result = data.get("result", {})
        if not data.get("success") or not isinstance(result.get("data"), list):
            raise SiteError("课程目录响应无效，不能判断是否已获取全部课次。")
        output = []
        for row in result["data"]:
            if str(row.get("sub_delete", "0")) != "0" or row.get("sub_show") == "no":
                continue
            if str(row["course_id"]) != str(course["course_id"]):
                raise SiteError("课程目录包含不匹配的课程 ID")
            output.append({"course_id": str(row["course_id"]), "course_name": course["name"],
                "sub_id": str(row["sub_id"]), "title": row["title"],
                "date": lesson_date(row["title"], row.get("start_at")),
                "start_at": row.get("start_at"), "status": str(row["status"]),
                "teacher": row.get("lecturer_name", course.get("teacher", ""))})
        return output

    def resources(self, lesson):
        ids = {"course_id": lesson["course_id"], "sub_id": lesson["sub_id"]}
        response = self.get("/courseapi/v3/portal-home-setting/get-sub-info", ids)
        if response.get("code") != 0 or not isinstance(response.get("data"), dict):
            raise SiteError("无法读取课次详情")
        info = response["data"]
        if str(info.get("course_id")) != lesson["course_id"] or str(info.get("sub_id")) != lesson["sub_id"]:
            raise SiteError("返回的课次身份与请求不匹配")
        trans = self.get("/courseapi/v3/web-socket/search-trans-result", {"sub_id":lesson["sub_id"],"format":"json"})
        if trans.get("code") not in (0, 10002):
            raise SiteError("字幕接口失败，不能将失败当作没有字幕")
        slides, warnings, page = [], [], 1
        try:
            while True:
                ppt = self.get("/pptnote/v1/schedule/search-ppt", {**ids,"page":page,"per_page":100})
                if ppt.get("code") != 0:
                    raise SiteError("课件列表返回业务错误")
                rows = ppt.get("list", [])
                for item in rows:
                    content = item.get("content", {})
                    if isinstance(content, str):
                        content = json.loads(content)
                    slides.append({"sequence": len(slides)+1, "start_seconds": float(item["created_sec"]),
                                   "image_url": content.get("pptimgurl"), "original_page": None})
                if len(slides) >= int(ppt.get("total",len(slides))) or not rows:
                    break
                page += 1
                if page > 100:
                    raise SiteError("课件列表分页超限")
        except (SiteError, ValueError, KeyError) as exc:
            warnings.append(f"课件时间轴未完整获取：{type(exc).__name__}")
        # Use the player-returned main stream, never a guessed URL or a student-camera stream.
        playurl = info.get("playurl", {})
        if isinstance(playurl, dict):
            media = next((playurl[k] for k in ("0", "4", "1") if playurl.get(k)), None)
        elif isinstance(playurl, list):
            media = next((v for v in playurl if isinstance(v,str) and v), None)
        else:
            media = playurl if isinstance(playurl,str) else None
        if media and (urlparse(media).scheme != "https" or urlparse(media).netloc != urlparse(BASE).netloc):
            raise SiteError("媒体不在当前已验证的 HTTPS 站点，停止自动下载。")
        return {"media_url": media, "duration_seconds": float(info.get("duration") or 0),
                "resource_guid": info.get("resource_guid"), "start_at": info.get("start_at"),
                "status": str(info.get("sub_status")), "transcript_response": trans,
                "slides": slides, "warnings": warnings}
