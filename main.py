import asyncio
import io
import base64
import sys
import os
import datetime
import tempfile
import uuid
import time
import json
import subprocess
from typing import Optional, List, Dict, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image, BaseMessageComponent


DEFAULT_SYSTEM_PROMPT = """角色设定：诺星缘。
你会根据用户屏幕内容给出简短、自然、口语化的互动。
要求：
1) 直接评论，不要前言；
2) 最多三句，最好两句内；
3) 具体提及屏幕内容；
4) 不要用括号描述动作。"""


class ScreenCompanion(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # 自动任务
        self.is_running = False
        self.auto_tasks: Dict[str, asyncio.Task] = {}
        self.task_counter = 0

        # 后台控制
        self.running = True
        self.background_tasks: List[asyncio.Task] = []

        # 临时截图目录（不立即删除，交给清理任务）
        self.temp_dir = os.path.join(tempfile.gettempdir(), "astrbot_screen_companion")
        os.makedirs(self.temp_dir, exist_ok=True)

        # 启动清理任务（清理旧截图）
        self.background_tasks.append(asyncio.create_task(self._cleanup_temp_task()))

        logger.info("ScreenCompanion 初始化完成")

    async def stop(self):
        """插件停止时清理任务"""
        logger.info("停止 ScreenCompanion，开始清理任务")
        self.is_running = False

        for task_id, task in list(self.auto_tasks.items()):
            task.cancel()
        self.auto_tasks.clear()

        self.running = False
        for t in self.background_tasks:
            t.cancel()
        self.background_tasks.clear()

    # ---------------------------
    # 基础检查
    # ---------------------------
    def _check_dependencies(self):
        missing = []
        backend = self.config.get("capture_backend", "powershell" if sys.platform == "win32" else "pyautogui")

        try:
            from PIL import Image as PILImage  # noqa
        except ImportError:
            missing.append("Pillow")

        if backend == "pyautogui":
            try:
                import pyautogui  # noqa
            except ImportError:
                missing.append("pyautogui")

        if sys.platform == "win32":
            # 可选：活动窗口识别
            if self.config.get("capture_mode", "fullscreen") == "active_window":
                try:
                    import pygetwindow  # noqa
                except ImportError:
                    missing.append("pygetwindow")

        if missing:
            return False, f"缺少依赖: {', '.join(missing)}。请安装: pip install {' '.join(missing)}"
        return True, ""

    def _check_env(self):
        ok, msg = self._check_dependencies()
        if not ok:
            return False, msg

        backend = self.config.get("capture_backend", "powershell" if sys.platform == "win32" else "pyautogui")
        if backend == "powershell" and sys.platform == "win32":
            return True, ""

        try:
            import pyautogui
            if sys.platform.startswith("linux"):
                if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                    return False, "Linux 未检测到可用图形会话（DISPLAY/WAYLAND_DISPLAY）。"
            size = pyautogui.size()
            if size[0] <= 0 or size[1] <= 0:
                return False, "屏幕尺寸异常，可能无桌面权限。"
            return True, ""
        except Exception as e:
            return False, f"环境检查失败: {e}"

    # ---------------------------
    # 截图
    # ---------------------------
    def _capture_with_powershell_png(self) -> bytes:
        """Windows 原生截图，返回 PNG bytes"""
        out_png = os.path.join(self.temp_dir, f"ps_capture_{uuid.uuid4()}.png")
        ps = f"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $vs.Width, $vs.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($vs.Left, $vs.Top, 0, 0, $bmp.Size)
$bmp.Save('{out_png.replace("'", "''")}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
"""
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=20
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "PowerShell 截图失败")

            with open(out_png, "rb") as f:
                return f.read()
        finally:
            try:
                if os.path.exists(out_png):
                    os.remove(out_png)
            except Exception:
                pass

    async def _capture_screen_bytes(self) -> Tuple[bytes, str]:
        """
        返回: (jpeg_bytes, active_window_title)
        """
        def _core():
            from PIL import Image as PILImage

            backend = self.config.get("capture_backend", "powershell" if sys.platform == "win32" else "pyautogui")
            mode = self.config.get("capture_mode", "fullscreen")
            active_window_title = ""
            img = None

            # 尝试获取活动窗口标题
            if sys.platform == "win32":
                try:
                    import pygetwindow as gw
                    w = gw.getActiveWindow()
                    if w:
                        active_window_title = w.title or ""
                except Exception:
                    pass

            if backend == "powershell" and sys.platform == "win32":
                raw_png = self._capture_with_powershell_png()
                img = PILImage.open(io.BytesIO(raw_png)).convert("RGB")
            else:
                import pyautogui
                shot = None

                if mode == "active_window" and sys.platform == "win32":
                    try:
                        import pygetwindow as gw
                        w = gw.getActiveWindow()
                        if w and w.width > 0 and w.height > 0:
                            active_window_title = w.title or active_window_title
                            shot = pyautogui.screenshot(region=(w.left, w.top, w.width, w.height))
                    except Exception as e:
                        logger.debug(f"活动窗口截图失败，回退全屏: {e}")

                if shot is None:
                    shot = pyautogui.screenshot()

                img = shot.convert("RGB") if shot.mode != "RGB" else shot

            quality = self.config.get("image_quality", 75)
            try:
                quality = max(10, min(100, int(quality)))
            except Exception:
                quality = 75

            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality)
            return out.getvalue(), active_window_title

        return await asyncio.to_thread(_core)

    def _save_temp_jpg(self, image_bytes: bytes) -> str:
        path = os.path.join(self.temp_dir, f"screen_{uuid.uuid4()}.jpg")
        with open(path, "wb") as f:
            f.write(image_bytes)
        return path

    async def _cleanup_temp_task(self):
        """每5分钟清理一次临时目录中超过 keep_minutes 的文件"""
        keep_minutes = int(self.config.get("temp_keep_minutes", 30))
        while self.running:
            try:
                now = time.time()
                for name in os.listdir(self.temp_dir):
                    p = os.path.join(self.temp_dir, name)
                    if not os.path.isfile(p):
                        continue
                    age = now - os.path.getmtime(p)
                    if age > keep_minutes * 60:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"清理临时文件失败: {e}")
            await asyncio.sleep(300)

    # ---------------------------
    # 视觉API + LLM互动
    # ---------------------------
    async def _call_external_vision_api(self, image_bytes: bytes) -> str:
        import aiohttp

        api_url = self.config.get("vision_api_url", "").strip()
        api_key = self.config.get("vision_api_key", "").strip()
        api_model = self.config.get("vision_api_model", "").strip()
        image_prompt = self.config.get(
            "image_prompt",
            "请详细分析这张屏幕截图，识别屏幕内容、用户操作、关键细节。"
        )

        if not api_url:
            return "未配置视觉API地址。"

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": api_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": image_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }
            ],
            "stream": False
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.error(f"视觉API失败: {resp.status} {text}")
                        return f"视觉识别失败（HTTP {resp.status}）"

                    try:
                        data = json.loads(text)
                    except Exception:
                        return "视觉API返回非JSON。"

                    # 兼容 OpenAI 风格
                    if isinstance(data, dict):
                        if "choices" in data and data["choices"]:
                            c0 = data["choices"][0]
                            if isinstance(c0, dict):
                                msg = c0.get("message", {})
                                if isinstance(msg, dict) and msg.get("content"):
                                    return str(msg["content"])
                                if c0.get("text"):
                                    return str(c0["text"])
                        if data.get("response"):
                            return str(data["response"])
                        if data.get("content"):
                            return str(data["content"])

                    return "视觉API返回结构无法识别。"
        except Exception as e:
            logger.error(f"调用视觉API异常: {e}")
            return f"视觉识别异常: {e}"

    def _identify_scene(self, title: str) -> str:
        if not title:
            return "未知"
        t = title.lower()
        if any(k in t for k in ["code", "vscode", "visual studio", "pycharm", "idea"]):
            return "编程"
        if any(k in t for k in ["chrome", "edge", "firefox", "browser", "浏览器"]):
            return "浏览"
        if any(k in t for k in ["word", "excel", "powerpoint", "wps"]):
            return "办公"
        if any(k in t for k in ["qq", "wechat", "discord", "telegram", "slack"]):
            return "聊天"
        if any(k in t for k in ["game", "steam", "valorant", "dota", "cs", "minecraft"]):
            return "游戏"
        return "未知"

    def _build_time_prompt(self) -> str:
        h = datetime.datetime.now().hour
        if 6 <= h < 12:
            return "现在是早上。"
        if 12 <= h < 18:
            return "现在是下午。"
        if 18 <= h < 23:
            return "现在是晚上。"
        return "现在是深夜，注意休息。"

    async def _analyze_screen(self, image_bytes: bytes, active_window_title: str = "", custom_prompt: str = "") -> str:
        provider = self.context.get_using_provider()
        if not provider:
            return "未检测到可用LLM提供商。"

        system_prompt = self.config.get("system_prompt", "").strip() or DEFAULT_SYSTEM_PROMPT

        # 先视觉识别
        recognition = await self._call_external_vision_api(image_bytes)

        scene = self._identify_scene(active_window_title)
        time_prompt = self._build_time_prompt()

        prompt = (
            f"用户屏幕识别结果：{recognition}\n"
            f"活动窗口：{active_window_title or '未知'}\n"
            f"场景：{scene}\n"
            f"{time_prompt}\n"
        )
        if custom_prompt:
            prompt += f"附加要求：{custom_prompt}\n"

        prompt += (
            "请以诺星缘口吻直接回复。"
            "要具体提到屏幕内容，简短自然，不要开场白，不要括号动作。"
            "最多三句话，最好两句话。"
        )

        try:
            rsp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
            if rsp and hasattr(rsp, "completion_text") and rsp.completion_text:
                return rsp.completion_text.strip()
            return "我看到了你的屏幕，但这次没组织好回复。"
        except Exception as e:
            logger.error(f"LLM互动失败: {e}")
            return "我看到了你的屏幕，但互动生成失败了。"

    # ---------------------------
    # 命令
    # ---------------------------
    @filter.command("kpcap")
    async def kpcap(self, event: AstrMessageEvent):
        """仅截图并发送（调试命令）"""
        ok, msg = self._check_env()
        if not ok:
            yield event.plain_result(f"⚠️ 无法截图：\n{msg}")
            return

        try:
            image_bytes, title = await asyncio.wait_for(self._capture_screen_bytes(), timeout=20)
            img_path = self._save_temp_jpg(image_bytes)

            await self.context.send_message(event.unified_msg_origin, MessageChain([Image(file=img_path)]))
            yield event.plain_result(f"截图成功。活动窗口：{title or '未知'}")
        except Exception as e:
            logger.error(f"/kpcap 失败: {e}")
            yield event.plain_result(f"截图失败: {e}")

    @filter.command("kp")
    async def kp(self, event: AstrMessageEvent):
        """截图 + 视觉识别 + 人格互动"""
        ok, msg = self._check_env()
        if not ok:
            yield event.plain_result(f"⚠️ 无法使用屏幕观察：\n{msg}")
            return

        try:
            image_bytes, title = await asyncio.wait_for(self._capture_screen_bytes(), timeout=20)
            img_path = self._save_temp_jpg(image_bytes)

            text = await asyncio.wait_for(self._analyze_screen(image_bytes, active_window_title=title), timeout=150)

            # 先发图，再发文
            await self.context.send_message(event.unified_msg_origin, MessageChain([Image(file=img_path)]))

            # 文本长了就分段
            parts = self._split_message(text, max_length=1000)
            if len(parts) == 1:
                yield event.plain_result(parts[0])
            else:
                for i, p in enumerate(parts):
                    if i < len(parts) - 1:
                        await self.context.send_message(event.unified_msg_origin, MessageChain([Plain(p)]))
                        await asyncio.sleep(0.4)
                    else:
                        yield event.plain_result(p)

        except asyncio.TimeoutError:
            yield event.plain_result("操作超时，请检查网络或视觉API。")
        except Exception as e:
            logger.error(f"/kp 失败: {e}")
            yield event.plain_result(f"执行失败: {e}")

    @filter.command("kps")
    async def kps(self, event: AstrMessageEvent):
        """快捷开关自动观察"""
        if self.is_running:
            self.is_running = False
            for _, t in list(self.auto_tasks.items()):
                t.cancel()
            self.auto_tasks.clear()
            yield event.plain_result("已关闭自动观察。")
        else:
            if not self.config.get("enabled", True):
                yield event.plain_result("配置中 enabled=false，自动观察未启用。")
                return
            self.is_running = True
            task_id = f"task_{self.task_counter}"
            self.task_counter += 1
            self.auto_tasks[task_id] = asyncio.create_task(self._auto_screen_task(event, task_id=task_id))
            yield event.plain_result(f"已开启自动观察：{task_id}")

    @filter.command_group("kpi")
    def kpi_group(self):
        pass

    @kpi_group.command("start")
    async def kpi_start(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            yield event.plain_result("配置中 enabled=false，自动观察未启用。")
            return
        if not self.is_running:
            self.is_running = True
        task_id = f"task_{self.task_counter}"
        self.task_counter += 1
        self.auto_tasks[task_id] = asyncio.create_task(self._auto_screen_task(event, task_id=task_id))
        yield event.plain_result(f"✅ 已启动 {task_id}")

    @kpi_group.command("stop")
    async def kpi_stop(self, event: AstrMessageEvent, task_id: str = None):
        if task_id:
            t = self.auto_tasks.get(task_id)
            if not t:
                yield event.plain_result(f"任务 {task_id} 不存在")
                return
            t.cancel()
            del self.auto_tasks[task_id]
            if not self.auto_tasks:
                self.is_running = False
            yield event.plain_result(f"已停止 {task_id}")
        else:
            self.is_running = False
            for _, t in list(self.auto_tasks.items()):
                t.cancel()
            self.auto_tasks.clear()
            yield event.plain_result("已停止所有自动任务")

    @kpi_group.command("list")
    async def kpi_list(self, event: AstrMessageEvent):
        if not self.auto_tasks:
            yield event.plain_result("当前没有运行中的自动任务")
            return
        msg = "运行中的任务：\n" + "\n".join([f"- {k}" for k in self.auto_tasks.keys()])
        yield event.plain_result(msg)

    @kpi_group.command("add")
    async def kpi_add(self, event: AstrMessageEvent, interval: int, *prompt):
        if interval < 10:
            interval = 10
        custom_prompt = " ".join(prompt).strip()
        if not self.is_running:
            self.is_running = True
        task_id = f"task_{self.task_counter}"
        self.task_counter += 1
        self.auto_tasks[task_id] = asyncio.create_task(
            self._auto_screen_task(event, task_id=task_id, interval=interval, custom_prompt=custom_prompt)
        )
        yield event.plain_result(f"✅ 已添加 {task_id}，间隔 {interval}s")

    # ---------------------------
    # 自动任务
    # ---------------------------
    async def _auto_screen_task(self, event: AstrMessageEvent, task_id: str, interval: Optional[int] = None, custom_prompt: str = ""):
        logger.info(f"自动任务启动: {task_id}")
        try:
            while self.is_running:
                # 间隔
                check_interval = interval if interval is not None else int(self.config.get("check_interval", 180))
                if check_interval < 10:
                    check_interval = 10

                # 可中断等待
                for _ in range(check_interval):
                    if not self.is_running:
                        break
                    await asyncio.sleep(1)

                if not self.is_running:
                    break

                # 触发概率
                prob = int(self.config.get("trigger_probability", 30))
                prob = max(0, min(100, prob))
                rnd = __import__("random").randint(1, 100)
                if rnd > prob:
                    continue

                ok, msg = self._check_env()
                if not ok:
                    logger.warning(f"自动任务环境不可用: {msg}")
                    continue

                try:
                    image_bytes, title = await asyncio.wait_for(self._capture_screen_bytes(), timeout=20)
                    text = await asyncio.wait_for(
                        self._analyze_screen(image_bytes, active_window_title=title, custom_prompt=custom_prompt),
                        timeout=150
                    )

                    # 自动任务默认只发文字，防刷屏；如需发图可配 send_image_in_auto=true
                    send_image = bool(self.config.get("send_image_in_auto", False))
                    if send_image:
                        img_path = self._save_temp_jpg(image_bytes)
                        await self.context.send_message(
                            event.unified_msg_origin,
                            MessageChain([Plain(text), Image(file=img_path)])
                        )
                    else:
                        await self.context.send_message(event.unified_msg_origin, MessageChain([Plain(text)]))

                except asyncio.TimeoutError:
                    logger.warning(f"{task_id} 执行超时")
                except Exception as e:
                    logger.error(f"{task_id} 执行异常: {e}")

        except asyncio.CancelledError:
            logger.info(f"自动任务取消: {task_id}")
        finally:
            logger.info(f"自动任务结束: {task_id}")
            self.auto_tasks.pop(task_id, None)
            if not self.auto_tasks:
                self.is_running = False

    # ---------------------------
    # 工具
    # ---------------------------
    def _split_message(self, text: str, max_length: int = 1000) -> List[str]:
        if not text:
            return [""]
        out = []
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 <= max_length:
                cur = f"{cur}\n{line}".strip() if cur else line
            else:
                if cur:
                    out.append(cur)
                if len(line) <= max_length:
                    cur = line
                else:
                    # 超长行硬切
                    start = 0
                    while start < len(line):
                        out.append(line[start:start + max_length])
                        start += max_length
                    cur = ""
        if cur:
            out.append(cur)
        return out


