from playwright.sync_api import sync_playwright, TimeoutError
import time
import threading
import queue

class BrowserAutomationTool:
    """
    基于 Playwright 的隔离沙盒浏览器控制工具。
    允许大模型在前台或后台打开网页，通过 CSS Selector 精准操作 DOM。
    使用独立后台线程运行 Playwright 以避免与 FastAPI/Asyncio 发生冲突。
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(BrowserAutomationTool, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, headless: bool = False):
        if self._initialized: 
            return
        self.headless = headless
        self.cmd_queue = queue.Queue()
        self.res_queue = queue.Queue()
        self.thread = None
        self._initialized = True
        
    def _start_browser_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._browser_loop, daemon=True)
            self.thread.start()
            # Wait for initialization
            res = self.res_queue.get()
            if res.get("status") == "error":
                raise RuntimeError(f"Playwright 初始化失败: {res.get('message')}")

    def _browser_loop(self):
        """运行在独立线程中的 Playwright 事件循环"""
        try:
            with sync_playwright() as p:
                import os
                user_data_dir = os.path.join(os.getcwd(), "data", "browser_profile")
                os.makedirs(user_data_dir, exist_ok=True)
                
                context = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=self.headless,
                    viewport={'width': 1280, 'height': 800},
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    no_viewport=False
                )
                
                if len(context.pages) > 0:
                    page = context.pages[0]
                else:
                    page = context.new_page()
                
                # Signal successful init
                self.res_queue.put({"status": "success", "message": "init ok"})
                
                while True:
                    cmd = self.cmd_queue.get()
                    action = cmd.get("action")
                    
                    if action == "QUIT":
                        break
                        
                    try:
                        res_msg = self._handle_action(page, cmd)
                        self.res_queue.put({"status": "success", "message": res_msg})
                    except TimeoutError as e:
                        self.res_queue.put({"status": "error", "message": f"元素查找或加载超时 - {str(e)}"})
                    except Exception as e:
                        self.res_queue.put({"status": "error", "message": f"浏览器执行出错 - {str(e)}"})
                        
        except Exception as e:
            self.res_queue.put({"status": "error", "message": str(e)})

    def _handle_action(self, page, cmd: dict) -> str:
        action = cmd.get("action")
        url = cmd.get("url", "")
        selector = cmd.get("selector", "")
        text = cmd.get("text", "")
        key = cmd.get("key", "")
        wait_time = cmd.get("wait_time", 1)
        
        if action == "goto":
            if not url: return "Error: 缺少 URL 参数"
            if not url.startswith("http"): url = "https://" + url
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(wait_time)
            return f"已成功导航至 {page.url}\n网页标题: {page.title()}"
            
        elif action == "click":
            if not selector: return "Error: 缺少 CSS selector 参数"
            page.click(selector, timeout=5000)
            time.sleep(wait_time)
            return f"已成功点击元素: {selector}"
            
        elif action == "fill":
            if not selector or not text: return "Error: 缺少 selector 或 text 参数"
            page.fill(selector, text, timeout=5000)
            time.sleep(wait_time)
            return f"已在元素 {selector} 中输入文本"
            
        elif action == "press":
            if not key: return "Error: 缺少 key 参数（如 'Enter', 'Escape'）"
            if selector:
                page.press(selector, key, timeout=5000)
            else:
                page.keyboard.press(key)
            time.sleep(wait_time)
            return f"已按下按键: {key}"
            
        elif action == "upload":
            if not selector or not cmd.get("path"): return "Error: 缺少 selector 或 path(文件绝对路径) 参数"
            page.set_input_files(selector, cmd.get("path"), timeout=5000)
            time.sleep(wait_time)
            return f"已将文件 {cmd.get('path')} 上传至元素 {selector}"
            
        elif action == "read_dom":
            dom_script = '''
            () => {
                const clone = document.body.cloneNode(true);
                const removeSelectors = ['script', 'style', 'svg', 'noscript', 'iframe'];
                removeSelectors.forEach(sel => {
                    clone.querySelectorAll(sel).forEach(el => el.remove());
                });
                let walker = document.createTreeWalker(clone, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
                let result = "";
                while(walker.nextNode()) {
                    let node = walker.currentNode;
                    if (node.nodeType === Node.TEXT_NODE) {
                        let text = node.textContent.trim();
                        if(text) result += text + " ";
                    } else if (node.nodeType === Node.ELEMENT_NODE) {
                        if (['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'].includes(node.tagName)) {
                            let idInfo = node.id ? ` id="${node.id}"` : "";
                            let classInfo = node.className ? ` class="${node.className}"` : "";
                            let typeInfo = node.getAttribute('type') ? ` type="${node.getAttribute('type')}"` : "";
                            let hrefInfo = node.getAttribute('href') ? ` href="${node.getAttribute('href')}"` : "";
                            let placeholder = node.getAttribute('placeholder') ? ` placeholder="${node.getAttribute('placeholder')}"` : "";
                            result += `\\n[${node.tagName}${idInfo}${classInfo}${typeInfo}${hrefInfo}${placeholder}] `;
                        }
                    }
                }
                return result.replace(/\\s{2,}/g, ' ').trim();
            }
            '''
            simple_dom = page.evaluate(dom_script)
            
            max_len = 8000
            if len(simple_dom) > max_len:
                simple_dom = simple_dom[:max_len] + "... (DOM 已截断)"
            
            return f"当前 URL: {page.url}\n网页标题: {page.title()}\n\n可视及交互元素树:\n{simple_dom}"
            
        elif action == "get_url":
            return f"当前 URL: {page.url}"
            
        return f"Error: 不支持的浏览器动作 '{action}'"

    def execute(self, action: str, url: str = "", selector: str = "", text: str = "", 
                key: str = "", path: str = "", wait_time: int = 1) -> str:
        """执行浏览器自动化相关操作，代理到后台线程"""
        if action == "close":
            if self.thread and self.thread.is_alive():
                self.cmd_queue.put({"action": "QUIT"})
                self.thread.join(timeout=3)
                self.thread = None
            return "浏览器已关闭"

        try:
            self._start_browser_thread()
            self.cmd_queue.put({
                "action": action, 
                "url": url, 
                "selector": selector, 
                "text": text,
                "key": key,
                "path": path,
                "wait_time": wait_time
            })
            
            res = self.res_queue.get(timeout=30)
            if res.get("status") == "error":
                return f"Error: {res.get('message')}"
            return res.get("message", "")
        except queue.Empty:
            return "Error: 执行超时或浏览器线程无响应"
        except Exception as e:
            return f"Error: 内部通讯错误 - {str(e)}"

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "browser_automation",
                "description": (
                    "虚拟沙盒浏览器工具。使用 playwright 进行隔离环境下的网页控制与数据抓取。"
                    "大模型如果需要查询或者填写网页内容，应该首推使用此工具，而不是 pyautogui。"
                    "动作(action)支持："
                    "1. 'goto': 导航到指定网址 (需 url)。"
                    "2. 'read_dom': 获取当前页面的纯净交互元素DOM树(极度推荐在点击前先使用本命令以了解当前网页上的具体 CSS Seletor/class/id)。"
                    "3. 'click': 使用 CSS 选择器点击元素 (需 selector)。遇到真正的文件上传按钮切勿使用 click，而是使用 upload 动作。"
                    "4. 'fill': 向输入框填入文字 (需 selector 和 text)。"
                    "5. 'press': 按下键盘按键 (如 Enter、Escape)。"
                    "6. 'upload': 上传文件到网页表单 (非常重要：对准 <input type='file'> 元素的 selector 进行操作，用本地文件的绝对路径作为 path 参数)。"
                    "7. 'get_url': 读当前页面的真实 URL。"
                    "8. 'close': 关闭并清理浏览器沙盒环境。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["goto", "read_dom", "click", "fill", "press", "upload", "get_url", "close"],
                            "description": "要执行的浏览器指令",
                        },
                        "url": {
                            "type": "string",
                            "description": "目标网页地址 (用于 goto)，例如 'https://github.com'",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS 选择器 (用于 click, fill)，例如 '#search-input' 或 'button.submit-btn'",
                        },
                        "text": {
                            "type": "string",
                            "description": "要填入的文本内容 (用于 fill)",
                        },
                        "key": {
                            "type": "string",
                            "description": "按键名称 (用于 press)，例如 'Enter', 'Escape'",
                        },
                        "path": {
                            "type": "string",
                            "description": "本地文件的绝对路径 (用于 upload)"
                        },
                        "wait_time": {
                            "type": "integer",
                            "description": "执行动作后的硬等待秒数，默认1秒，如果网页加载慢可提高",
                        }
                    },
                    "required": ["action"],
                },
            },
        }
