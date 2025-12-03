"""
使用 chrome-mcp 实现自动获取邮箱和验证码登录 Gemini Business
参考 jmzc 文件夹中的实现方式
"""

import json
import time
import re
import base64
import random
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlparse
import os

def random_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
    """随机延迟，模拟真实用户行为
    
    Args:
        min_ms: 最小延迟（毫秒）
        max_ms: 最大延迟（毫秒）
    """
    delay = random.uniform(min_ms, max_ms) / 1000.0
    time.sleep(delay)

def human_like_delay(page, min_ms: int = 500, max_ms: int = 2000) -> None:
    """在 Playwright 页面中随机延迟，模拟真实用户行为
    
    Args:
        page: Playwright 页面对象
        min_ms: 最小延迟（毫秒）
        max_ms: 最大延迟（毫秒）
    """
    delay = random.randint(min_ms, max_ms)
    page.wait_for_timeout(delay)

# 注意：这个脚本需要使用 chrome-mcp 工具
# 由于无法直接调用 chrome-mcp，这里提供使用 Playwright 的实现
# 实际使用时可以通过 chrome-mcp 的 API 调用

# 临时邮箱配置说明：
# 1. 优先使用：在账号配置中为每个账号配置 tempmail_url 和 tempmail_name（数据库或 JSON）
#    例如：{"tempmail_url": "https://tempmail.example.com/?jwt=...", "tempmail_name": "邮箱1 (xxx@example.com)"}
# 2. 备用选择：如果账号没有配置 tempmail_url，会从下面的 TEMPMAIL_URLS 列表中选择
# 格式：每个 URL 可以是完整的 jwt 链接（直接使用该邮箱），或根地址（会创建新邮箱）
TEMPMAIL_URLS = [
    # 在此添加临时邮箱 URL（如果需要）
    # 示例：
    # (
    #     "https://tempmail.example.com/"
    #     "?jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    # ),
]

# 邮箱选择策略：'round_robin'（轮换）或 'random'（随机）
TEMPMAIL_SELECTION_STRATEGY = 'round_robin'

# 当前使用的邮箱索引（用于轮换）
_current_tempmail_index = 0

GEMINI_LOGIN_URL = "https://auth.business.gemini.google/login?continueUrl=https://business.gemini.google/"
GETOXSRF_URL = "https://business.gemini.google/auth/getoxsrf"

def select_tempmail_url(account_config: Optional[Dict] = None) -> tuple[str, Optional[str]]:
    """选择要使用的临时邮箱 URL
    
    Args:
        account_config: 账号配置字典，如果包含 tempmail_url 字段，则使用该 URL
    
    Returns:
        (tempmail_url, tempmail_name): 临时邮箱 URL 和名称
    """
    global _current_tempmail_index
    
    # 如果账号配置中指定了邮箱 URL，优先使用
    if account_config and "tempmail_url" in account_config:
        url = account_config["tempmail_url"]
        name = account_config.get("tempmail_name", "配置的邮箱")
        # 调试日志已关闭
        # print(f"[临时邮箱] 使用账号配置的邮箱: {name}")
        return url, name
    
    # 否则从 TEMPMAIL_URLS 中选择
    if not TEMPMAIL_URLS:
        raise ValueError("未配置临时邮箱 URL，请在 TEMPMAIL_URLS 中添加至少一个邮箱 URL，或在账号配置中添加 tempmail_url")
    
    if TEMPMAIL_SELECTION_STRATEGY == 'random':
        import random
        selected_url = random.choice(TEMPMAIL_URLS)
        selected_index = TEMPMAIL_URLS.index(selected_url)
        # 调试日志已关闭
        # print(f"[临时邮箱] 随机选择邮箱 {selected_index + 1}/{len(TEMPMAIL_URLS)}")
    else:  # round_robin
        selected_url = TEMPMAIL_URLS[_current_tempmail_index]
        selected_index = _current_tempmail_index
        # 调试日志已关闭
        # print(f"[临时邮箱] 轮换选择邮箱 {selected_index + 1}/{len(TEMPMAIL_URLS)}")
        # 更新索引，下次使用下一个邮箱
        _current_tempmail_index = (_current_tempmail_index + 1) % len(TEMPMAIL_URLS)
    
    # 从 URL 中提取邮箱名称（如果有 jwt）
    name = None
    if 'jwt=' in selected_url:
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(selected_url)
            params = parse_qs(parsed.query)
            if 'jwt' in params:
                jwt_token = params['jwt'][0]
                payload = jwt_token.split('.')[1]
                padding = '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload + padding)
                data = json.loads(decoded)
                if 'address' in data:
                    name = data['address']
        except:
            pass
    
    return selected_url, name

def extract_verification_code(text: str) -> Optional[str]:
    """从文本中提取验证码（支持中英文格式）"""
    # 先按行精确匹配提示语，避免误匹配 Cloudflare/Logo/verification 等单词
    lines = text.splitlines()
    for line in lines:
        line_lower = line.lower()
        idx = -1
        
        # 中文提示语
        if "一次性验证码为" in line:
            idx = line.index("一次性验证码为")
        elif "一次性验证为" in line:  # 处理被截断的情况（API 方式可能因为 Quoted-Printable 解码导致"码"字丢失）
            idx = line.index("一次性验证为")
        elif "验证码为" in line:
            idx = line.index("验证码为")
        elif "验证为" in line:  # 处理被截断的情况
            idx = line.index("验证为")
        elif "您的验证码是" in line:
            idx = line.index("您的验证码是")
        # 英文提示语
        elif "your one-time verification code is" in line_lower:
            idx = line_lower.index("your one-time verification code is")
        elif "verification code is" in line_lower:
            idx = line_lower.index("verification code is")
        elif "one-time verification code is" in line_lower:
            idx = line_lower.index("one-time verification code is")
        
        if idx >= 0:
            # 只在提示语之后的子串中查找，避免页眉里的 "Logo" 等干扰
            sub = line[idx:]
            # 在子串中取第一个 6 位的大写字母数字串
            # 验证码可能是：纯字母（如 VACBHW、TJE5R8）、纯数字（如 123456）、或字母数字混合（如 RP9J4H、6C5C5C）
            candidates = re.findall(r'[A-Z0-9]{6}', sub)
            if candidates:
                code = candidates[0].strip()
                # 要求：长度恰好 6，且至少包含一个字母（避免纯数字 ID 如 2992025 被误匹配）
                # 注意：纯字母验证码（如 VACBHW）也应该被接受
                if len(code) == 6 and any(c.isalpha() for c in code):
                    print(f"[临时邮箱] 行级匹配到验证码: {code} (来源行: {line.strip()[:80]}...)")
                    return code.upper()

    # 如果行级匹配失败，再用全局模式做兜底
    patterns = [
        # 中文模式
        r'一次性验证码为[：:]\s*([A-Z0-9]{6})',
        r'一次性验证为[：:]\s*([A-Z0-9]{6})',  # 处理被截断的情况
        r'验证码为[：:]\s*([A-Z0-9]{6})',
        r'验证为[：:]\s*([A-Z0-9]{6})',  # 处理被截断的情况
        r'验证码[：:是]\s*([A-Z0-9]{6})',
        r'您的验证码是[：:]\s*([A-Z0-9]{6})',
        # 英文模式
        r'your one-time verification code is[：:]\s*([A-Z0-9]{6})',
        r'one-time verification code is[：:]\s*([A-Z0-9]{6})',
        r'verification code is[：:]\s*([A-Z0-9]{6})',
        r'code is[：:]\s*([A-Z0-9]{6})',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = match.group(1).strip()
            # 要求：长度恰好 6，且至少包含一个字母（避免纯数字 ID 被误匹配）
            # 注意：纯字母验证码（如 VACBHW）也应该被接受
            if len(code) == 6 and any(c.isalpha() for c in code):
                print(f"[临时邮箱] 模式匹配到验证码: {code}")
                return code.upper()

    print("[临时邮箱] 未能从邮件文本中提取验证码")
    return None

def get_email_from_tempmail(page, tempmail_url: str) -> Optional[str]:
    """从临时邮箱服务获取邮箱地址"""
    # 调试日志已关闭
    # print("[临时邮箱] 正在访问临时邮箱网站...")
    page.goto(tempmail_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)  # 短暂等待页面加载
    
    # 判断 URL 是否包含 jwt（如果包含，说明已经指定了邮箱，不需要创建新邮箱）
    is_jwt_url = 'jwt=' in tempmail_url
    
    if not is_jwt_url:
        # 如果 URL 不包含 jwt，需要创建新邮箱
        # 切换到"创建新邮箱"标签页
        # 调试日志已关闭
        # print("[临时邮箱] 切换到'创建新邮箱'标签页...")
        tab_selectors = [
            "div[data-name='register']",
            "//div[contains(@class, 'n-tabs-tab')][contains(., '创建新邮箱')]",
        ]
        
        for selector in tab_selectors:
            try:
                if selector.startswith("//"):
                    tab = page.locator(selector).first
                else:
                    tab = page.locator(selector).first
                
                if tab.is_visible():
                    tab_class = tab.get_attribute("class") or ""
                    if "n-tabs-tab--active" not in tab_class:
                        tab.click()
                        # 调试日志已关闭
                        # print("[临时邮箱] ✓ 已切换到'创建新邮箱'标签页")
                        page.wait_for_timeout(500)  # 短暂等待切换完成
                    else:
                        # 调试日志已关闭
                        # print("[临时邮箱] ✓ 已在'创建新邮箱'标签页")
                        pass
                    break
            except:
                continue
        
        # 点击"创建新邮箱"按钮
        # 调试日志已关闭
        # print("[临时邮箱] 点击'创建新邮箱'按钮...")
        button_selectors = [
            "//button[contains(., '创建新邮箱')]",
            "//button[contains(@class, 'n-button--block')][contains(., '创建新邮箱')]",
            "button:has-text('创建新邮箱')",
        ]
        
        for selector in button_selectors:
            try:
                if selector.startswith("//"):
                    button = page.locator(selector).first
                else:
                    button = page.locator(selector).first
                
                if button.is_visible():
                    button.click()
                    # 调试日志已关闭
                    # print("[临时邮箱] ✓ 已点击'创建新邮箱'按钮")
                    page.wait_for_timeout(3000)  # 等待邮箱生成
                    break
            except:
                continue
    else:
        # 调试日志已关闭
        # print("[临时邮箱] URL 包含 jwt，使用已指定的邮箱，无需创建新邮箱")
        pass
    
    # 关闭凭证对话框（如果有）
    try:
        close_btn = page.locator("button.n-dialog__close, button.n-base-close").first
        if close_btn.is_visible():
            close_btn.click()
            # 调试日志已关闭
            # print("[临时邮箱] ✓ 已关闭凭证对话框")
            page.wait_for_timeout(500)  # 短暂等待关闭完成
    except:
        pass
    
    # 提取邮箱地址
    # 调试日志已关闭
    # print("[临时邮箱] 正在提取邮箱地址...")
    email = None
    
    # 方法1：从JWT token中提取
    try:
        page_text = page.locator("body").text_content() or ""
        jwt_pattern = r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
        jwt_matches = re.findall(jwt_pattern, page_text)
        if jwt_matches:
            jwt_token = jwt_matches[0]
            payload = jwt_token.split('.')[1]
            padding = '=' * (4 - len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload + padding)
            data = json.loads(decoded)
            if 'address' in data:
                email = data['address']
                # 调试日志已关闭
                # print(f"[临时邮箱] ✓ 从JWT token中提取到邮箱: {email}")
    except:
        pass
    
    # 方法2：从输入框获取
    if not email:
        try:
            email_input = page.locator("input[type='text'], input[readonly]").first
            email = email_input.input_value()
            if email and '@' in email:
                # 调试日志已关闭
                # print(f"[临时邮箱] ✓ 从输入框获取到邮箱: {email}")
                pass
        except:
            pass
    
    # 方法3：从页面文本中提取（增加长度过滤，避免匹配到过短的邮箱地址）
    if not email:
        try:
            page_text = page.locator("body").text_content() or ""
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            matches = re.findall(email_pattern, page_text)
            # 过滤掉本地部分太短的邮箱（例如 "30@..."），要求至少 6 个字符
            candidates = [
                m for m in matches
                if len(m.split("@")[0]) >= 6
            ]
            if candidates:
                email = candidates[0]
                # 调试日志已关闭
                # print(f"[临时邮箱] ✓ 从页面文本提取到邮箱: {email}")
        except:
            pass
    
    if email and '@' in email:
        return email
    else:
        print("[临时邮箱] ✗ 无法获取邮箱地址")
        return None

# 尝试导入 API 客户端
try:
    import sys
    from pathlib import Path
    project_root = Path(__file__).parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from app.tempmail_api import get_verification_code_from_api
    TEMPMAIL_API_AVAILABLE = True
except ImportError:
    TEMPMAIL_API_AVAILABLE = False
    print("[临时邮箱] API 模块未找到，将使用浏览器方式")


# 客户端实例缓存（以 tempmail_url 为键，用于在重试时复用 last_max_id）
_tempmail_client_cache = {}

def get_verification_code_from_tempmail(page, timeout=120, tempmail_url: Optional[str] = None, retry_mode: bool = False, account_config: Optional[Dict] = None, force_api: bool = False) -> Optional[str]:
    """从临时邮箱服务获取验证码（自动选择 API 或浏览器方式）
    
    Args:
        page: Playwright 页面对象
        timeout: 超时时间（秒）
        tempmail_url: 临时邮箱 URL
        retry_mode: 是否为重试模式（True：立即刷新并提取，不等待；False：等待邮件到达）
        account_config: 账号配置字典（可选，用于其他用途）
        force_api: 是否强制使用 API 方式（True：即使失败也不回退到浏览器方式）
    
    Returns:
        验证码字符串，如果未找到则返回 None
    """
    # 优先尝试 API 方式
    if (TEMPMAIL_API_AVAILABLE and 
        tempmail_url and 
        'jwt=' in tempmail_url):
        try:
            print("[临时邮箱] 尝试使用 API 方式获取验证码...")
            # 从全局配置中获取 Worker URL（如果存在）
            worker_url = None
            try:
                from app.account_manager import account_manager
                worker_url = account_manager.config.get("tempmail_worker_url")
                if worker_url:
                    print(f"[临时邮箱] 使用全局配置的 Worker URL: {worker_url}")
            except:
                pass
            
            # 使用缓存来复用客户端实例（以便在重试时保持 last_max_id）
            client = None
            if tempmail_url in _tempmail_client_cache:
                client = _tempmail_client_cache[tempmail_url]
            else:
                try:
                    from app.tempmail_api import TempMailAPIClient
                    client = TempMailAPIClient(tempmail_url, worker_url)
                    api_email = client.get_email_address()
                    # 缓存客户端实例
                    _tempmail_client_cache[tempmail_url] = client
                    # if api_email:
                    #     print(f"[临时邮箱 API] 从 JWT 提取的邮箱地址: {api_email}")
                    # else:
                    #     print(f"[临时邮箱 API] ⚠ 无法从 JWT 中提取邮箱地址")
                except Exception as e:
                    # 只在失败时显示
                    print(f"[临时邮箱 API] ⚠ 验证邮箱地址失败: {e}")
            
            # 如果客户端创建成功，直接使用它来获取验证码（以便复用 last_max_id）
            if client:
                code = client.get_verification_code(
                    timeout, 
                    retry_mode,
                    extract_code_func=extract_verification_code
                )
            else:
                # 如果客户端创建失败，回退到原来的方式
                code = get_verification_code_from_api(
                    tempmail_url, 
                    timeout, 
                    retry_mode,
                    extract_code_func=extract_verification_code,
                    worker_url=worker_url
                )
            if code:
                return code
            if force_api:
                print("[临时邮箱] API 方式未获取到验证码（强制 API 模式，不回退到浏览器方式）")
                return None
            print("[临时邮箱] API 方式未获取到验证码，回退到浏览器方式...")
        except Exception as e:
            if force_api:
                print(f"[临时邮箱] API 方式失败: {e}（强制 API 模式，不回退到浏览器方式）")
                return None
            print(f"[临时邮箱] API 方式失败: {e}，回退到浏览器方式...")
    
    # 如果强制使用 API 方式但 API 不可用，直接返回 None
    if force_api:
        print("[临时邮箱] 强制 API 模式，但 API 不可用或 URL 无效，返回 None")
        return None
    
    # 回退到浏览器方式（原有实现）
    return get_verification_code_from_tempmail_browser(page, timeout, tempmail_url, retry_mode)


def get_verification_code_from_tempmail_browser(page, timeout=120, tempmail_url: Optional[str] = None, retry_mode: bool = False) -> Optional[str]:
    """从临时邮箱服务获取验证码（使用浏览器自动化方式）
    
    Args:
        page: Playwright 页面对象
        timeout: 超时时间（秒）
        tempmail_url: 临时邮箱 URL
        retry_mode: 是否为重试模式（True：立即刷新并提取，不等待；False：等待邮件到达）
    """
    if retry_mode:
        # 调试日志已关闭
        # print(f"[临时邮箱] 刷新邮件并重新提取验证码...")
        pass
    else:
        # 调试日志已关闭
        # print(f"[临时邮箱] 等待验证码邮件（最多 {timeout} 秒）...")
        pass
    
    # 如果提供了 tempmail_url 且当前页面不在该 URL 上，导航到该 URL
    if tempmail_url:
        try:
            current_url = page.url
            if tempmail_url not in current_url:
                # 调试日志已关闭
                # print("[临时邮箱] 当前页面不在临时邮箱 URL，重新导航...")
                page.goto(tempmail_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1000)  # 短暂等待页面加载
        except:
            pass
    
    # 切换到收件箱标签
    # 调试日志已关闭
    # print("[临时邮箱] 切换到收件箱标签...")
    mailbox_tab_selectors = [
        "div[data-name='mailbox']",
        "//div[contains(@class, 'n-tabs-tab')][contains(., '收件箱')]",
    ]
    
    mailbox_tab_found = False
    for selector in mailbox_tab_selectors:
        try:
            if selector.startswith("//"):
                mailbox_tab = page.locator(selector).first
            else:
                mailbox_tab = page.locator(selector).first
            
            if mailbox_tab.is_visible():
                tab_class = mailbox_tab.get_attribute("class") or ""
                if "n-tabs-tab--active" not in tab_class:
                    mailbox_tab.click()
                    # 调试日志已关闭
                    # print("[临时邮箱] ✓ 已切换到收件箱")
                    page.wait_for_timeout(500)  # 短暂等待切换完成
                else:
                    # 调试日志已关闭
                    # print("[临时邮箱] ✓ 已在收件箱")
                    pass
                mailbox_tab_found = True
                break
        except:
            continue
    
    if not mailbox_tab_found:
        # 调试日志已关闭
        # print("[临时邮箱] ⚠ 未找到收件箱标签，继续尝试...")
        pass
    
    start_time = time.time()
    attempts = 0
    if retry_mode:
        # 重试模式：只尝试一次，不等待
        max_attempts = 1
    else:
        # 正常模式：等待邮件到达
        max_attempts = timeout // 10
    last_max_id = 0  # 记录上一次看到的最高ID，确保只处理真正的新邮件
    
    while attempts < max_attempts:
        attempts += 1
        elapsed = int(time.time() - start_time)
        # 调试日志已关闭
        # print(f"[临时邮箱] 等待验证码... ({elapsed}秒/{timeout}秒)")
        
        # 第一次获取验证码时，先等待几秒再刷新，避免获取到历史的错误验证码
        if attempts == 1 and not retry_mode:
            # 调试日志已关闭
            # print("[临时邮箱] 第一次获取验证码，等待 10 秒后再刷新（确保新验证码邮件已发送）...")
            # 使用随机延迟（模拟真实用户等待邮件的时间）
            human_like_delay(page, 8000, 12000)  # 等待 8-12 秒，确保验证码邮件已发送
        
        # 点击刷新按钮（每次循环都刷新）
        refresh_selectors = [
            "button:has-text('刷新')",
            "//button[contains(., '刷新')]",
            "//span[contains(text(), '刷新')]/parent::button",
        ]
        
        refresh_clicked = False
        for selector in refresh_selectors:
            try:
                if selector.startswith("//"):
                    refresh_btn = page.locator(selector).first
                else:
                    refresh_btn = page.locator(selector).first
                
                if refresh_btn.is_visible():
                    refresh_btn.click()
                    if attempts == 1:
                        # 调试日志已关闭
                        # print("[临时邮箱] ✓ 已点击刷新按钮")
                        pass
                    refresh_clicked = True
                    break
            except:
                continue
        
        if not refresh_clicked and attempts == 1:
            # 调试日志已关闭
            # print("[临时邮箱] ⚠ 未找到刷新按钮，刷新页面...")
            page.reload()
        
        # 等待邮件列表真正更新（增加等待时间，确保新邮件加载完成）
        if attempts == 1:
            # 调试日志已关闭
            # print("[临时邮箱] 等待邮件列表加载...")
            pass
        page.wait_for_timeout(3000)  # 等待刷新生效
        page.wait_for_timeout(2000)  # 再等待一下，确保新邮件真正出现在列表中
        
        # 查找邮件列表（参考 jmzc 的选择器）
        email_list_selectors = [
            "li.n-list-item",
            ".n-list-item",
            "//li[contains(@class, 'n-list-item')]",
        ]
        
        mail_items = []
        for selector in email_list_selectors:
            try:
                if selector.startswith("//"):
                    mail_items = page.locator(selector).all()
                else:
                    mail_items = page.locator(selector).all()
                if len(mail_items) > 0:
                    if attempts == 1:
                        # 调试日志已关闭
                        # print(f"[临时邮箱] ✓ 找到 {len(mail_items)} 封邮件")
                        pass
                    break
            except:
                continue
        
        # 查找包含关键词的邮件（参考 jmzc 的关键词），并按 ID 选择最新的一封
        keywords = ['gemini', 'google', 'verify', 'verification', 'code', '验证', '验证码']
        candidates = []
        for mail_item in mail_items:
            try:
                mail_text = mail_item.text_content() or ""
                if any(keyword.lower() in mail_text.lower() for keyword in keywords):
                    # 尝试从文本中提取 ID（支持 "ID: 310" 或跨行格式）
                    # 使用多行模式匹配，因为 ID 可能在单独一行
                    id_match = re.search(r'ID:\s*(\d+)', mail_text, re.MULTILINE)
                    mail_id = int(id_match.group(1)) if id_match else -1
                    if mail_id > 0:  # 只添加成功提取到 ID 的邮件
                        candidates.append((mail_id, mail_item, mail_text))
            except:
                continue

        # 按 ID 从大到小排序，优先使用最新的一封（ID 最大）
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            # 只处理第一封（ID 最大的）
            mail_id, mail_item, mail_text = candidates[0]
            
            # 只有当新邮件的ID大于之前记录的最高ID时，才认为是真正的新邮件
            # 或者如果ID等于last_max_id但之前提取失败，允许重试
            if mail_id > last_max_id:
                # 调试日志已关闭
                # print(f"[临时邮箱] ✓ 找到 {len(candidates)} 封验证码邮件，发现新邮件 (ID: {mail_id})")
                # 注意：只有在成功提取验证码后才更新 last_max_id
                pass
            elif mail_id == last_max_id:
                # 如果ID相同，说明是同一封邮件，可能是之前提取失败，允许重试
                # 调试日志已关闭
                # print(f"[临时邮箱] ✓ 找到验证码邮件，重试提取验证码 (ID: {mail_id})")
                pass
            else:
                # 调试日志已关闭
                # print(f"[临时邮箱] ⚠ 当前最高ID ({mail_id}) 未超过之前记录 ({last_max_id})，继续等待新邮件...")
                pass
                # 继续下一轮循环，等待真正的新邮件
                continue
        else:
            # 如果没有找到包含关键词的邮件，继续下一轮循环
            continue

        # 只处理 ID 最大的那一封邮件
        try:
            # 调试日志已关闭
            # print(f"[临时邮箱] ✓ 找到验证码邮件，正在打开... (ID: {mail_id})")
            # 确保点击的是邮件项本身，而不是其中的按钮
            # 先尝试点击邮件项的主要区域（避免点击到按钮）
            try:
                # 尝试点击邮件项内的文本区域或主要内容区域
                mail_text_elem = mail_item.locator("div:first-child, span:first-child").first
                if mail_text_elem.is_visible():
                    mail_text_elem.click()
                else:
                    mail_item.click()
            except:
                # 如果失败，直接点击邮件项
                mail_item.click()
            
            # 等待邮件详情加载完成
            page.wait_for_timeout(3000)
            
            # 检查是否误跳转到发送邮件页面
            try:
                page_text = page.locator("body").text_content() or ""
                if "发送邮件" in page_text and "申请权限" in page_text:
                    # 调试日志已关闭
                    # print("[临时邮箱] ⚠ 检测到跳转到发送邮件页面，返回收件箱...")
                    # 尝试返回收件箱
                    try:
                        # 点击返回按钮或收件箱标签
                        back_btn = page.locator("button:has-text('返回'), a:has-text('收件箱'), div[data-name='mailbox']").first
                        if back_btn.is_visible():
                            back_btn.click()
                            page.wait_for_timeout(2000)
                            # 调试日志已关闭
                            # print("[临时邮箱] ✓ 已返回收件箱")
                            # 继续下一轮循环，重新选择邮件
                            continue
                    except:
                        # 如果找不到返回按钮，重新加载收件箱页面
                        if tempmail_url:
                            page.goto(tempmail_url, wait_until="networkidle", timeout=30000)
                        else:
                            # 如果没有提供 tempmail_url，尝试使用第一个邮箱 URL
                            if not TEMPMAIL_URLS:
                                raise ValueError("未配置临时邮箱 URL，请在账号配置中添加 tempmail_url")
                            page.goto(TEMPMAIL_URLS[0], wait_until="networkidle", timeout=30000)
                        page.wait_for_timeout(1000)  # 短暂等待页面加载
                        # 切换到收件箱标签
                        try:
                            mailbox_tab = page.locator("div[data-name='mailbox'], //div[contains(@class, 'n-tabs-tab')][contains(., '收件箱')]").first
                            if mailbox_tab.is_visible():
                                mailbox_tab.click()
                                page.wait_for_timeout(2000)
                        except:
                            pass
                        # 调试日志已关闭
                        # print("[临时邮箱] ✓ 已重新加载收件箱")
                        continue
            except:
                pass
            
            # 等待邮件详情区域出现（包含"验证码"或"verification"等关键词）
            try:
                # 等待页面包含验证码相关关键词，最多等待5秒
                page.wait_for_function(
                    "document.body.innerText.includes('验证码') || document.body.innerText.includes('verification') || document.body.innerText.includes('code')",
                    timeout=5000
                )
                # 调试日志已关闭
                # print("[临时邮箱] ✓ 邮件详情已加载")
            except:
                # 调试日志已关闭
                # print("[临时邮箱] ⚠ 等待邮件详情超时，继续尝试...")
                pass
            
            # 如果有"显示纯文本邮件"按钮，优先点击以获得更干净的正文
            try:
                plain_text_btn_selectors = [
                    "button:has-text('显示纯文本邮件')",
                    "//button[contains(., '显示纯文本邮件')]",
                    "//span[contains(text(), '显示纯文本邮件')]/parent::button",
                ]
                # 注意：不直接使用 "button.n-button--info-type.n-button--small-type"，因为可能匹配到其他按钮
                for p_selector in plain_text_btn_selectors:
                    try:
                        if p_selector.startswith("//"):
                            p_btn = page.locator(p_selector).first
                        else:
                            p_btn = page.locator(p_selector).first
                        if p_btn.is_visible():
                            # 再次确认按钮文本，避免误点击
                            btn_text = p_btn.text_content() or ""
                            if "显示纯文本邮件" in btn_text or "纯文本" in btn_text:
                                p_btn.click()
                                # 调试日志已关闭
                                # print("[临时邮箱] ✓ 已点击"显示纯文本邮件"按钮")
                                # 等待纯文本内容加载
                                page.wait_for_timeout(3000)
                                
                                # 检查是否误跳转
                                try:
                                    page_text_check = page.locator("body").text_content() or ""
                                    if "发送邮件" in page_text_check and "申请权限" in page_text_check:
                                        # 调试日志已关闭
                                        # print("[临时邮箱] ⚠ 点击后误跳转到发送邮件页面，返回...")
                                        # 返回收件箱并继续下一轮循环
                                        if tempmail_url:
                                            page.goto(tempmail_url, wait_until="networkidle", timeout=30000)
                                        else:
                                            # 如果没有提供 tempmail_url，尝试使用第一个邮箱 URL
                                            if not TEMPMAIL_URLS:
                                                raise ValueError("未配置临时邮箱 URL，请在账号配置中添加 tempmail_url")
                                            page.goto(TEMPMAIL_URLS[0], wait_until="networkidle", timeout=30000)
                                        page.wait_for_timeout(1000)  # 短暂等待页面加载
                                        try:
                                            mailbox_tab = page.locator("div[data-name='mailbox']").first
                                            if mailbox_tab.is_visible():
                                                mailbox_tab.click()
                                                page.wait_for_timeout(2000)
                                        except:
                                            pass
                                        continue
                                except:
                                    pass
                                
                                # 再次等待包含验证码的内容出现
                                try:
                                    page.wait_for_function(
                                        "document.body.innerText.includes('验证码') || document.body.innerText.includes('verification') || document.body.innerText.includes('一次性验证码')",
                                        timeout=3000
                                    )
                                except:
                                    pass
                                break
                    except:
                        continue
            except:
                pass
            
            # 提取邮件内容（参考 jmzc 的提取逻辑）
            try:
                # 尝试多种方式获取邮件内容，优先从邮件详情区域获取
                mail_content = ""
                
                # 方法1：尝试多个可能的邮件内容区域选择器
                content_selectors = [
                    "div[class*='email-content']",
                    "div[class*='mail-content']",
                    "div[class*='content']",
                    "div[class*='message']",
                    "div[class*='body']",
                    "pre",  # 纯文本邮件可能用 <pre> 标签
                    "div[role='article']",
                ]
                
                for selector in content_selectors:
                    try:
                        elements = page.locator(selector).all()
                        for elem in elements:
                            try:
                                text = elem.text_content() or ""
                                # 只选择包含验证码相关关键词的内容区域
                                if any(kw in text.lower() for kw in ['验证码', 'verification', 'code', '一次性']):
                                    if len(text) > len(mail_content):
                                        mail_content = text
                            except:
                                continue
                        if mail_content:
                            # 调试日志已关闭
                            # print(f"[临时邮箱] ✓ 从邮件内容区域提取到文本（长度: {len(mail_content)}）")
                            break
                    except:
                        continue
                
                # 方法2：如果方法1失败，尝试从整个页面提取，但过滤掉UI元素
                if not mail_content or len(mail_content) < 50:
                    try:
                        # 尝试找到包含"一次性验证码为"或"verification code"的文本节点
                        all_text = page.locator("body").text_content() or ""
                        # 尝试提取包含验证码关键词的段落
                        lines = all_text.splitlines()
                        relevant_lines = []
                        for line in lines:
                            if any(kw in line.lower() for kw in ['验证码', 'verification', 'code', '一次性', 'gemini']):
                                relevant_lines.append(line)
                        if relevant_lines:
                            mail_content = "\n".join(relevant_lines)
                            # 调试日志已关闭
                            # print(f"[临时邮箱] ✓ 从页面文本中提取到相关内容（{len(relevant_lines)}行）")
                    except:
                        pass
                
                # 方法3：最后兜底，使用整个body（但会包含UI噪音）
                if not mail_content or len(mail_content) < 50:
                    mail_content = page.locator("body").text_content() or ""
                    # 调试日志已关闭
                    # print("[临时邮箱] ⚠ 使用整个页面文本（可能包含UI噪音）")
                
                # 记录浏览器方式获取的内容用于对比（仅第一次，当 API 方式失败后）
                if not hasattr(get_verification_code_from_tempmail_browser, '_content_comparison_logged'):
                    print(f"[临时邮箱] 邮件内容对比 - 浏览器方式获取的邮件内容（前500字符）:\n{mail_content[:500]}")
                    get_verification_code_from_tempmail_browser._content_comparison_logged = True
                
                code = extract_verification_code(mail_content)
                if code:
                    # 调试日志已关闭
                    # print(f"[临时邮箱] ✓ 提取到验证码: {code}")
                    # 只有在成功提取验证码后，才更新 last_max_id，避免重复处理
                    if mail_id > last_max_id:
                        last_max_id = mail_id
                    return code
                else:
                    # 调试日志已关闭
                    # print(f"[临时邮箱] ⚠ 邮件内容已获取，但未找到验证码")
                    # print(f"[临时邮箱] 邮件内容预览: {mail_content[:300]}...")
                    # 如果是重试模式，提取失败立即返回 None，让调用者决定是否继续
                    if retry_mode:
                        # 调试日志已关闭
                        # print(f"[临时邮箱] 重试模式：提取失败，返回 None")
                        return None
                    # 如果提取失败，不更新 last_max_id，允许下次重试同一封邮件
                    # 继续下一轮循环（等待刷新后重试）
            except Exception as e:
                # 调试日志已关闭
            # print(f"[临时邮箱] ⚠ 提取邮件内容时出错: {e}")
                import traceback
                traceback.print_exc()
                # 如果出错，继续下一轮循环
        except Exception as e:
            # 调试日志已关闭
            # print(f"[临时邮箱] ⚠ 打开邮件时出错: {e}")
            import traceback
            traceback.print_exc()
            # 如果出错，继续下一轮循环
    
    # 调试日志已关闭（保留关键错误信息）
    # print("[临时邮箱] ✗ 超时，未收到验证码邮件")
    return None

def wait_for_recaptcha_ready(page, timeout: int = 10) -> bool:
    """等待 reCAPTCHA 准备好（检查 iframe 是否存在）
    
    Args:
        page: Playwright 页面对象
        timeout: 超时时间（秒）
    
    Returns:
        bool: reCAPTCHA 是否已准备好
    """
    try:
        waited = 0
        check_interval = 1  # 每秒检查一次
        
        while waited < timeout:
            # 检查是否有 reCAPTCHA iframe
            recaptcha_iframe = page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA']")
            if recaptcha_iframe.count() > 0:
                print("[登录] ✓ 检测到 reCAPTCHA iframe，reCAPTCHA 已准备好")
                return True
            
            # 检查是否有 reCAPTCHA 容器
            recaptcha_container = page.locator("#recaptcha-container")
            if recaptcha_container.count() > 0:
                print("[登录] ✓ 检测到 reCAPTCHA 容器，reCAPTCHA 已准备好")
                return True
            
            # 使用随机延迟（模拟真实用户检查邮件的间隔）
            human_like_delay(page, check_interval * 1000 - 500, check_interval * 1000 + 1000)
            waited += check_interval
        
        return False
    except:
        return False

def wait_for_recaptcha_complete(page, timeout: int = 30) -> bool:
    """等待 reCAPTCHA 验证完成（检查 g-recaptcha-response 是否有值）
    
    Args:
        page: Playwright 页面对象
        timeout: 超时时间（秒）
    
    Returns:
        bool: reCAPTCHA 是否已完成验证
    """
    try:
        waited = 0
        check_interval = 1  # 每秒检查一次
        
        while waited < timeout:
            # 检查 g-recaptcha-response 是否有值
            recaptcha_response = page.locator("#g-recaptcha-response")
            if recaptcha_response.count() > 0:
                try:
                    response_value = recaptcha_response.input_value()
                    if response_value and len(response_value) > 0:
                        print(f"[登录] ✓ reCAPTCHA 验证完成（响应长度: {len(response_value)}）")
                        return True
                except:
                    pass
            
            # 检查是否有可见的 reCAPTCHA 挑战框（如果出现，说明需要用户交互）
            try:
                # 检查是否有可见的 reCAPTCHA 挑战框（通常是一个较大的 iframe）
                visible_challenge = page.locator("iframe[src*='recaptcha'][src*='bframe']")
                if visible_challenge.count() > 0:
                    # 检查 iframe 是否可见（宽度和高度大于某个阈值）
                    for i in range(visible_challenge.count()):
                        try:
                            box = visible_challenge.nth(i).bounding_box()
                            if box and box['width'] > 100 and box['height'] > 100:
                                print("[登录] ⚠ 检测到可见的 reCAPTCHA 挑战框，等待用户完成挑战...")
                                # 等待挑战框消失（用户完成挑战）
                                challenge_wait = 0
                                while challenge_wait < 60:  # 最多等待60秒
                                    try:
                                        box_check = visible_challenge.nth(i).bounding_box()
                                        if not box_check or box_check['width'] < 100 or box_check['height'] < 100:
                                            print("[登录] ✓ reCAPTCHA 挑战框已消失，验证可能已完成")
                                            break
                                    except:
                                        break
                                    page.wait_for_timeout(2000)
                                    challenge_wait += 2
                                
                                # 再次检查响应值
                                try:
                                    response_value = recaptcha_response.input_value()
                                    if response_value and len(response_value) > 0:
                                        print(f"[登录] ✓ reCAPTCHA 验证完成（挑战后响应长度: {len(response_value)}）")
                                        return True
                                except:
                                    pass
                        except:
                            continue
            except:
                pass
            
            # 使用随机延迟（模拟真实用户检查邮件的间隔）
            human_like_delay(page, check_interval * 1000 - 500, check_interval * 1000 + 1000)
            waited += check_interval
            
            # 每5秒输出一次等待状态
            if waited % 5 == 0:
                print(f"[登录] 等待 reCAPTCHA 验证完成... (已等待 {waited} 秒)")
        
        print(f"[登录] ⚠ 等待 reCAPTCHA 验证完成超时（{timeout} 秒），继续执行...")
        return False
    except Exception as e:
        print(f"[登录] ⚠ 检查 reCAPTCHA 状态时出错: {e}")
        return False

def login_with_email_and_code(page, email: str, code: str) -> bool:
    """使用邮箱和验证码登录"""
    # 调试日志已关闭
    # print(f"[登录] 正在使用邮箱 {email} 和验证码登录...")
    
    # 此函数假设：
    # - main() 中已经在 login_page 上输入过邮箱并点击了 "使用邮箱继续"
    # - 此时页面应该处于"输入验证码"的状态
    # 为了兼容性，我们会优先直接查找验证码输入框，只在找不到时才尝试再次提交邮箱

    # 记录当前 URL（用于调试）
    current_url = page.url
    print(f"[登录] 当前 URL: {current_url}")

    # 优先直接查找验证码输入框（避免再次触发"发送验证码"）
    # 调试日志已关闭
    # print("[登录] 尝试直接查找验证码输入框...")
    code_input_selectors = [
        "input[name='pinInput']",
        "input[type='text'][placeholder*='code' i]",
        "input[placeholder*='验证码' i]",
        "input[name='code']",
        "input[autocomplete='one-time-code']",
    ]
    
    code_input = None
    for selector in code_input_selectors:
        try:
            code_input = page.locator(selector).first
            if code_input.is_visible():
                # 调试日志已关闭
                # print("[登录] ✓ 在当前页面找到了验证码输入框")
                break
        except:
            continue
    
    # 如未能直接找到验证码输入框，说明可能还停留在“邮箱输入”页面
    # 为了兼容性，这里才尝试再提交一次邮箱
    if not code_input or not code_input.is_visible():
        # 调试日志已关闭
        # print("[登录] ⚠ 当前未找到验证码输入框，可能仍在邮箱输入页面，尝试再次提交邮箱...")

        email_input_selectors = [
            "#email-input",
            "input[aria-label='邮箱']",
            "input[type='text'][name='loginHint']",
        ]

        email_input = None
        for selector in email_input_selectors:
            try:
                email_input = page.locator(selector).first
                if email_input.is_visible():
                    break
            except:
                continue

        if not email_input or not email_input.is_visible():
            print("[登录] ✗ 未找到邮箱输入框，也未找到验证码输入框，无法继续登录流程")
            return False

        try:
            # 输入前随机延迟
            human_like_delay(page, 500, 1500)
            email_input.fill(email)
            # 调试日志已关闭
            # print(f"[登录] ✓ 已重新填写邮箱: {email}")
            # 输入后随机延迟
            human_like_delay(page, 1000, 2500)
        except Exception as e:
            print(f"[登录] ✗ 重新填写邮箱失败: {e}")
            return False

        # 点击继续按钮（可能会再次触发发送验证码，但主流程里我们只会获取一次验证码）
        try:
            continue_btn = page.locator("button:has-text('Continue'), button:has-text('继续')").first
            if continue_btn.is_visible():
                # 点击前随机延迟
                human_like_delay(page, 800, 2000)
                continue_btn.click()
                # 调试日志已关闭
                # print("[登录] ✓ 已重新点击继续按钮")
                # 点击后随机延迟
                human_like_delay(page, 2000, 4000)
        except:
            # 调试日志已关闭
            # print("[登录] ⚠ 未找到继续按钮，尝试按 Enter...")
            # 按 Enter 前随机延迟
            human_like_delay(page, 500, 1500)
            email_input.press("Enter")
            human_like_delay(page, 2000, 4000)

        # 再次尝试查找验证码输入框
        # 调试日志已关闭
        # print("[登录] 再次查找验证码输入框...")
        code_input = None
        for selector in code_input_selectors:
            try:
                code_input = page.locator(selector).first
                if code_input.is_visible():
                    # 调试日志已关闭
                    # print("[登录] ✓ 已找到验证码输入框")
                    break
            except:
                continue

        # 仍然没找到时，最后兜底：选择第一个可见的 text 输入框（排除邮箱输入框）
        if (not code_input) or (not code_input.is_visible()):
            try:
                # 调试日志已关闭
                # print("[登录] ⚠ 未通过特定选择器找到验证码输入框，尝试兜底策略...")
                text_inputs = page.locator("input[type='text']").all()
                for inp in text_inputs:
                    try:
                        elem_id = (inp.get_attribute("id") or "").lower()
                        name_attr = (inp.get_attribute("name") or "").lower()
                        aria_label = (inp.get_attribute("aria-label") or "").lower()
                        if "email" in name_attr or "loginhint" in name_attr:
                            continue
                        if elem_id == "email-input":
                            continue
                        if "邮箱" in aria_label:
                            continue
                        if inp.is_visible():
                            code_input = inp
                            # 调试日志已关闭
                            # print("[登录] ✓ 通过兜底策略选中了一个可能的验证码输入框")
                            break
                    except:
                        continue
            except Exception as e:
                # 调试日志已关闭
                # print(f"[登录] ⚠ 兜底查找验证码输入框时出错: {e}")
                pass

    if not code_input or not code_input.is_visible():
        print("[登录] ✗ 未找到验证码输入框")
        return False
    
    # 输入验证码前随机延迟（模拟用户查看邮件）
    human_like_delay(page, 1000, 3000)
    # 模拟人类输入验证码的速度（逐字符输入，带延迟）
    code_input.fill(code)
    # 调试日志已关闭
    # print(f"[登录] ✓ 已填写验证码: {code}")
    # 输入后随机延迟（模拟用户检查验证码）
    human_like_delay(page, 1500, 3000)
    
    # 点击验证按钮
    try:
        verify_btn = page.locator("button:has-text('Verify'), button:has-text('验证'), button:has-text('Continue')").first
        if verify_btn.is_visible():
            # 点击前随机延迟（模拟用户犹豫）
            human_like_delay(page, 800, 2000)
            verify_btn.click()
            # 调试日志已关闭
            # print("[登录] ✓ 已点击验证按钮")
            # 点击后随机延迟（等待页面响应）
            human_like_delay(page, 2000, 4000)
    except:
        # 调试日志已关闭
        # print("[登录] ⚠ 未找到验证按钮，尝试按 Enter...")
        # 按 Enter 前随机延迟
        human_like_delay(page, 500, 1500)
        code_input.press("Enter")
        human_like_delay(page, 2000, 4000)
    
    # 检查是否有"验证码有误"或"验证码输入次数已超出上限"的错误提示
    # 注意：这个检查只在验证码页面执行，因为错误提示只会在验证码页面显示
    def check_verification_code_errors():
        """检查验证码错误提示（仅在验证码页面执行）"""
        # 先检查是否在验证码页面
        current_url_check = page.url
        if "accountverification" not in current_url_check or "verify-oob-code" not in current_url_check:
            return None  # 不在验证码页面，不检查
        
        try:
            page_text = page.locator("body").text_content() or ""
            # 扩展错误关键词，包括超时、过期等
            error_keywords = [
                "验证码有误", "验证码错误", "验证码无效", "验证码不正确",
                "code is incorrect", "invalid code", "incorrect code", 
                "wrong code", "code expired", "验证码已过期", "验证码超时",
                "请重试", "try again", "retry", "timeout", "expired"
            ]
            if any(keyword.lower() in page_text.lower() for keyword in error_keywords):
                print("[登录] ✗ 检测到验证码错误提示，需要重新获取验证码")
                return "CODE_ERROR"
            
            # 检查是否有"验证码输入次数已超出上限"的提示
            limit_exceeded_keywords = ["验证码输入次数已超出上限", "验证码输入次数", "超出上限", "请重新发送", "重新发送验证码"]
            if any(keyword in page_text for keyword in limit_exceeded_keywords):
                print("[登录] ⚠ 检测到验证码输入次数已超出上限")
                # 返回特殊值，让调用者重新执行整个登录流程
                return "LIMIT_EXCEEDED"
        except:
            pass
        return None
    
    # 立即检查一次（点击验证按钮后）
    error_result = check_verification_code_errors()
    if error_result:
        return error_result
    
    # 检查是否登录成功（等待页面跳转）
    # 调试日志已关闭
    # print("[登录] 等待页面跳转...")
    try:
        # 等待页面跳转到 business.gemini.google 主域名（排除 accountverification 等子域名）
        # 最多等待 30 秒，每 2 秒检查一次
        max_wait = 30
        waited = 0
        while waited < max_wait:
            current_url = page.url
            # 每10秒打印一次当前URL
            if waited % 10 == 0 and waited > 0:
                print(f"[登录] 等待跳转中... 当前 URL: {current_url} (已等待 {waited} 秒)")
            # 检查是否跳转到主域名（不是 accountverification 等子域名）
            if ("business.gemini.google" in current_url 
                and "accountverification" not in current_url
                and "login" not in current_url 
                and "auth" not in current_url):
                # 调试日志已关闭
                # print(f"[登录] ✓ 页面已跳转到: {current_url}")
                # print("[登录] ✓ 登录成功！")
                # 额外等待一下，确保 Cookie 已设置
                page.wait_for_timeout(3000)
                return True
            
            # 如果检测到 auth.business.gemini.google，这可能是正常的中间跳转步骤
            # 允许短暂停留在 auth 页面，继续等待最终跳转到主域名
            if "auth.business.gemini.google" in current_url:
                # 不立即打印警告，先等待看是否会跳转到主域名（这是正常流程）
                # 如果长时间停留在 auth 页面（超过 15 秒），才认为验证码可能无效
                max_wait_auth = 15
                waited_auth = 0
                auth_detected = True
                while waited_auth < max_wait_auth:
                    current_url_auth = page.url
                    # 每5秒打印一次当前URL
                    if waited_auth % 5 == 0 and waited_auth > 0:
                        print(f"[登录] 等待跳转中... 当前 URL: {current_url_auth} (已等待 {waited_auth} 秒)")
                    # 如果跳转到主域名，说明登录成功（这是正常流程）
                    if ("business.gemini.google" in current_url_auth 
                        and "accountverification" not in current_url_auth
                        and "login" not in current_url_auth 
                        and "auth" not in current_url_auth):
                        print("[登录] ✓ 登录成功！")
                        page.wait_for_timeout(3000)
                        return True
                    # 如果跳转回 accountverification 页面，说明验证码错误，需要重新输入
                    if "accountverification" in current_url_auth and "verify-oob-code" in current_url_auth:
                        print("[登录] ✗ 已跳转回验证码输入页面，验证码可能无效，需要重新获取验证码")
                        return "CODE_ERROR"
                    # 如果已经不在 auth 页面，跳出这个循环，继续主循环
                    if "auth.business.gemini.google" not in current_url_auth:
                        auth_detected = False
                        break
                    page.wait_for_timeout(1000)
                    waited_auth += 1
                
                # 如果等待超时仍在 auth 页面，才认为验证码可能无效
                if auth_detected and "auth.business.gemini.google" in page.url:
                    print("[登录] ⚠ 在 auth 页面停留时间过长（15秒），验证码可能无效，需要重新获取验证码")
                    return "CODE_ERROR"
            
            # 在等待跳转时，如果仍在验证码页面，检查是否有验证码错误提示
            # 注意：错误提示只会在验证码页面显示，所以只在验证码页面检查
            if "accountverification" in current_url and "verify-oob-code" in current_url:
                error_result = check_verification_code_errors()
                if error_result:
                    return error_result
            
            # 在等待跳转时，每次循环都检查是否有验证码错误提示
            error_result = check_verification_code_errors()
            if error_result:
                return error_result
            
            page.wait_for_timeout(2000)
            waited += 2
        
        # 如果超时，检查当前 URL
        current_url = page.url
        # 调试日志已关闭
        # print(f"[登录] ⚠ 等待跳转超时，当前URL: {current_url}")
        
        # 如果已经在 accountverification 页面，可能还需要等待进一步跳转
        if "accountverification" in current_url:
            # 调试日志已关闭
            # print("[登录] 检测到 accountverification 页面，等待进一步跳转...")
            pass
            # 在无头模式下，应该等待验证流程自然完成，不要过早主动导航
            # 等待更长时间（最多 40 秒），让验证流程自然完成
            max_wait_redirect = 40
            waited_redirect = 0
            redirect_occurred = False
            
            while waited_redirect < max_wait_redirect:
                current_url_check = page.url
                if ("business.gemini.google" in current_url_check 
                    and "accountverification" not in current_url_check
                    and "login" not in current_url_check 
                    and "auth" not in current_url_check):
                    # 调试日志已关闭
                    # print(f"[登录] ✓ 页面已跳转到: {current_url_check}")
                    # print("[登录] ✓ 登录成功！")
                    page.wait_for_timeout(3000)
                    redirect_occurred = True
                    return True
                
                # 每 2 秒检查一次
                page.wait_for_timeout(2000)
                waited_redirect += 2
            
            # 如果等待超时，检查页面状态
            if not redirect_occurred:
                current_url_final = page.url
                # 调试日志已关闭
                # print(f"[登录] ⚠ 等待跳转超时（{max_wait_redirect}秒），当前URL: {current_url_final}")
                
                # 如果仍在 accountverification 页面，说明验证码可能还未成功提交，或者还在处理中
                # 不应该返回 True，而应该继续等待或返回错误
                if "accountverification" in current_url_final:
                    print("[登录] ⚠ 仍在 accountverification 页面，验证码可能还未成功提交，继续等待...")
                    # 再等待一段时间，看是否跳转（最多再等待 20 秒）
                    max_wait_final = 20
                    waited_final = 0
                    while waited_final < max_wait_final:
                        current_url_final_check = page.url
                        # 如果跳转到主域名，说明登录成功
                        if ("business.gemini.google" in current_url_final_check 
                            and "accountverification" not in current_url_final_check
                            and "login" not in current_url_final_check 
                            and "auth" not in current_url_final_check):
                            print("[登录] ✓ 登录成功！")
                            page.wait_for_timeout(3000)
                            return True
                        # 如果跳转到 auth 页面，说明验证码无效
                        if "auth.business.gemini.google" in current_url_final_check:
                            print("[登录] ✗ 跳转到 auth 页面，验证码可能无效")
                            return "CODE_ERROR"
                        page.wait_for_timeout(2000)
                        waited_final += 2
                    
                    # 如果等待超时仍在 accountverification 页面，返回 False
                    if "accountverification" in page.url:
                        print("[登录] ✗ 等待超时，仍在验证码页面，验证码可能未成功提交")
                        return False
                elif "login" in current_url_final or "auth" in current_url_final:
                    # 如果被重定向回登录页面或 auth 页面，说明验证失败
                    if "auth.business.gemini.google" in current_url_final:
                        print("[登录] ✗ 被重定向到 auth 页面，验证码可能无效，需要重新获取验证码")
                        return "CODE_ERROR"
                    else:
                        print("[登录] ✗ 被重定向回登录页面，验证可能失败")
                        return False
                else:
                    # 其他情况，认为可能已登录成功
                    # 调试日志已关闭
                    # print(f"[登录] ✓ 当前URL: {current_url_final}，可能已登录成功")
                    return True
    except:
        # 如果等待超时，检查当前状态
        current_url = page.url
        print(f"[登录] ⚠ 当前URL: {current_url}")
        
        # 检查是否有验证码错误提示或"验证码输入次数已超出上限"
        try:
            page_text = page.locator("body").text_content() or ""
            error_keywords = ["验证码有误", "验证码错误", "code is incorrect", "invalid code", "incorrect code", "请重试"]
            if any(keyword in page_text for keyword in error_keywords):
                print("[登录] ✗ 检测到验证码错误提示，需要重新获取验证码")
                return "CODE_ERROR"
            
            # 检查是否有"验证码输入次数已超出上限"的提示
            limit_exceeded_keywords = ["验证码输入次数已超出上限", "验证码输入次数", "超出上限", "请重新发送", "重新发送验证码"]
            if any(keyword in page_text for keyword in limit_exceeded_keywords):
                print("[登录] ⚠ 检测到验证码输入次数已超出上限")
                # 返回特殊值，让调用者重新执行整个登录流程
                return "LIMIT_EXCEEDED"
        except:
            pass
        
            # 如果仍在验证码页面，先检查是否有错误提示（验证码错误、超时等）
        if "accountverification" in current_url and "verify-oob-code" in current_url:
            print(f"[登录] 检测到仍在验证码页面，检查是否有错误提示... (当前 URL: {current_url})")
            try:
                page_text = page.locator("body").text_content() or ""
                
                # 扩展错误关键词，包括超时、过期等
                error_keywords = [
                    "验证码有误", "验证码错误", "验证码无效", "验证码不正确",
                    "code is incorrect", "invalid code", "incorrect code", 
                    "wrong code", "code expired", "验证码已过期", "验证码超时",
                    "请重试", "try again", "retry", "timeout", "expired"
                ]
                if any(keyword.lower() in page_text.lower() for keyword in error_keywords):
                    print("[登录] ✗ 检测到验证码错误或超时提示，需要重新获取验证码")
                    return "CODE_ERROR"
            except Exception as e:
                print(f"[登录] ⚠ 检查验证码页面错误提示时出错: {e}")
            
            # 如果仍在验证码页面但没有明确的错误提示，也可能是超时或其他问题，返回 CODE_ERROR 以便重试
            print("[登录] ⚠ 仍在验证码页面，可能需要重新获取验证码")
            return "CODE_ERROR"
        
        # 如果 URL 已经包含 business.gemini.google 主域名（排除 accountverification），可能已经成功
        if ("business.gemini.google" in current_url 
            and "accountverification" not in current_url
            and "login" not in current_url 
            and "auth" not in current_url):
            print("[登录] ✓ 登录成功（URL 已跳转）！")
            page.wait_for_timeout(3000)
            return True
        
        print("[登录] ✗ 登录失败或仍在登录页面")
        return False

def extract_cookies_and_csesidx(page) -> Optional[Dict[str, str]]:
    """提取 Cookie 和 csesidx"""
    print("[提取] 正在提取 Cookie 和 csesidx...")
    
    # 检查当前 URL，如果在 accountverification 页面，需要等待跳转或导航
    current_url_check = page.url
    print(f"[提取] 当前 URL: {current_url_check}")
    if "accountverification" in current_url_check:
        print("[提取] 检测到 accountverification 页面，等待跳转到主域名...")
        # 在无头模式下，JavaScript 重定向可能不会自动触发，需要更主动地处理
        max_wait_redirect = 20  # 等待最多 20 秒
        waited_redirect = 0
        redirect_occurred = False
        
        while waited_redirect < max_wait_redirect:
            current_url_check_loop = page.url
            if ("business.gemini.google" in current_url_check_loop 
                and "accountverification" not in current_url_check_loop):
                print("[提取] ✓ 已跳转到主域名")
                redirect_occurred = True
                break
            page.wait_for_timeout(2000)
            waited_redirect += 2
            
            # 在无头模式下，尝试触发页面交互以促进跳转
            if waited_redirect >= 5 and waited_redirect % 5 == 0:
                try:
                    # 尝试通过 JavaScript 导航
                    page.evaluate("() => { if (window.location.href.includes('accountverification')) { window.location.href = 'https://business.gemini.google/'; } }")
                    page.wait_for_timeout(1000)
                except:
                    pass
        
        # 如果等待超时，直接导航到主域名
        if not redirect_occurred:
            print("[提取] 等待跳转超时，直接导航到主域名...")
            try:
                # 先尝试通过 JavaScript 导航
                try:
                    page.evaluate("() => { window.location.href = 'https://business.gemini.google/'; }")
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.wait_for_timeout(3000)
                except:
                    # 如果 JavaScript 导航失败，使用 goto
                    page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)
                print("[提取] ✓ 已导航到 business.gemini.google 主域名")
            except Exception as e:
                print(f"[提取] ⚠ 导航失败: {e}，继续尝试提取 Cookie...")
    
    # 等待页面完全加载和 Cookie 设置
    print("[提取] 等待页面加载和 Cookie 设置...")
    page.wait_for_timeout(5000)  # 等待 5 秒
    
    # 确保导航到正确的页面
    current_url = page.url
    print(f"[提取] 检查页面状态，当前 URL: {current_url}")
    if "login" in current_url or "auth" in current_url or "accountverification" in current_url:
        if "accountverification" in current_url:
            print("[提取] 当前在 accountverification 页面，等待跳转到主域名...")
        elif "auth.business.gemini.google" in current_url:
            # 不立即打印警告，先等待看是否会跳转到主域名（这是正常流程）
            # 等待跳转（最多等待 10 秒）
            max_wait_auth = 10
            waited_auth = 0
            while waited_auth < max_wait_auth:
                current_url_auth = page.url
                # 如果跳转回 accountverification 页面，说明需要重新输入验证码
                if "accountverification" in current_url_auth:
                    print("[提取] ✗ 已跳转回验证码输入页面，验证码可能无效，需要重新获取验证码")
                    return None  # 返回 None，让调用者重新获取验证码
                # 如果跳转到主域名，说明登录成功（这是正常流程）
                if ("business.gemini.google" in current_url_auth 
                    and "accountverification" not in current_url_auth
                    and "login" not in current_url_auth 
                    and "auth" not in current_url_auth):
                    print("[提取] ✓ 已跳转到主域名")
                    break
                page.wait_for_timeout(1000)
                waited_auth += 1
            
            # 如果等待超时仍在 auth 页面，才认为验证码可能无效
            if "auth.business.gemini.google" in page.url:
                print("[提取] ✗ 仍在 auth 页面，验证码可能无效")
                return None
        else:
            print("[提取] 当前仍在登录页面，等待跳转...")
        
        try:
            # 等待页面跳转到 business.gemini.google 主域名（排除 accountverification 等子域名）
            page.wait_for_function(
                "() => window.location.href.includes('business.gemini.google') && !window.location.href.includes('accountverification') && !window.location.href.includes('login') && !window.location.href.includes('auth')",
                timeout=30000
            )
            print("[提取] ✓ 页面已跳转到 business.gemini.google 主域名")
        except:
            # 如果等待超时，检查是否仍在 auth 页面
            if "auth.business.gemini.google" in page.url:
                print("[提取] ✗ 仍在 auth 页面，验证码可能无效")
                return None
            # 如果等待超时，尝试直接导航
            print("[提取] 等待跳转超时，尝试直接导航到主域名...")
            try:
                page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)
                print("[提取] ✓ 已导航到 business.gemini.google 主域名")
            except:
                print("[提取] ⚠ 导航失败，继续尝试提取 Cookie...")
    
    # 再次等待，确保 Cookie 已设置
    page.wait_for_timeout(3000)
    
    # 获取所有 Cookie（重试机制）
    secure_c_ses = None
    host_c_oses = None
    
    for retry in range(3):
        cookies = page.context.cookies()
        print(f"[提取] 获取到 {len(cookies)} 个 Cookie (重试 {retry + 1}/3)")
        
        # 打印所有 Cookie 名称用于调试
        cookie_names = [c.get('name', '') for c in cookies]
        print(f"[提取] Cookie 列表: {', '.join(cookie_names[:10])}{'...' if len(cookie_names) > 10 else ''}")
        
        for cookie in cookies:
            if cookie['name'] == '__Secure-C_SES':
                secure_c_ses = cookie['value']
            elif cookie['name'] == '__Host-C_OSES':
                host_c_oses = cookie['value']
        
        if secure_c_ses:
            break
        
        if retry < 2:
            print(f"[提取] 未找到 __Secure-C_SES Cookie，等待后重试 ({retry + 1}/3)...")
            # 尝试重新加载页面以触发 Cookie 设置
            if retry == 1:
                print("[提取] 尝试重新加载页面...")
                try:
                    page.reload(wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)
                except:
                    pass
            else:
                page.wait_for_timeout(3000)
    
    # 如果还是没找到，尝试从 document.cookie 获取
    if not secure_c_ses:
        print("[提取] 尝试从 document.cookie 获取...")
        try:
            page_cookies = page.evaluate("document.cookie")
            if page_cookies:
                # 解析 Cookie 字符串
                for cookie_pair in page_cookies.split(';'):
                    cookie_pair = cookie_pair.strip()
                    if cookie_pair.startswith('__Secure-C_SES='):
                        secure_c_ses = cookie_pair.split('=', 1)[1]
                    elif cookie_pair.startswith('__Host-C_OSES='):
                        host_c_oses = cookie_pair.split('=', 1)[1]
        except Exception as e:
            print(f"[提取] 从 document.cookie 获取失败: {e}")
        
        # 如果仍然没找到，尝试访问 API 端点以触发 Cookie 设置
        if not secure_c_ses:
            print("[提取] 尝试访问 API 端点以触发 Cookie 设置...")
            try:
                # 访问一个需要认证的页面
                page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(5000)
                
                # 再次尝试获取 Cookie
                cookies = page.context.cookies()
                for cookie in cookies:
                    if cookie['name'] == '__Secure-C_SES':
                        secure_c_ses = cookie['value']
                    elif cookie['name'] == '__Host-C_OSES':
                        host_c_oses = cookie['value']
            except Exception as e:
                print(f"[提取] 访问 API 端点失败: {e}")
    
    # 获取 csesidx 和 team_id
    current_url = page.url
    csesidx = None
    team_id = None
    
    # 从 URL 中提取 csesidx
    match = re.search(r'csesidx[=:](\d+)', current_url)
    if match:
        csesidx = match.group(1)
        print(f"[提取] ✓ 从URL提取到 csesidx: {csesidx}")
    else:
        # 从页面中提取
        try:
            page_text = page.locator("body").text_content() or ""
            match = re.search(r'csesidx[=:](\d+)', page_text)
            if match:
                csesidx = match.group(1)
                print(f"[提取] ✓ 从页面提取到 csesidx: {csesidx}")
        except:
            pass
    
    # 从 URL 路径中提取 team_id（格式：/cid/{team_id}）
    try:
        parsed_url = urlparse(current_url)
        path_parts = parsed_url.path.split('/')
        cid_index = path_parts.index('cid') if 'cid' in path_parts else -1
        if cid_index >= 0 and cid_index + 1 < len(path_parts):
            team_id = path_parts[cid_index + 1]
            # 调试日志已关闭
            # print(f"[提取] ✓ 从URL提取到 team_id: {team_id}")
    except:
        pass
    
    # 如果从 URL 中没找到 team_id，尝试从页面中提取
    if not team_id:
        try:
            page_text = page.locator("body").text_content() or ""
            # 尝试从页面文本中查找 team_id（可能在 JavaScript 变量或其他地方）
            team_id_match = re.search(r'team[_-]?id["\']?\s*[:=]\s*["\']?([a-f0-9-]+)', page_text, re.IGNORECASE)
            if team_id_match:
                team_id = team_id_match.group(1)
                # 调试日志已关闭
                #                 print(f"[提取] ✓ 从页面提取到 team_id: {team_id}")
        except:
            pass
    
    if secure_c_ses:
        print(f"[提取] ✓ 提取到 __Secure-C_SES: {secure_c_ses[:50]}...")
    else:
        # 保留关键错误信息
        print("[提取] ✗ 未提取到 __Secure-C_SES")
    
    if host_c_oses:
        print(f"[提取] ✓ 提取到 __Host-C_OSES: {host_c_oses[:50]}...")
    else:
        print("[提取] ⚠ 未提取到 __Host-C_OSES（可能正常）")
    
    if team_id:
        print(f"[提取] ✓ 提取到 team_id: {team_id}")
    else:
        print("[提取] ⚠ 未提取到 team_id（可能正常，某些账号可能没有）")
    
    if secure_c_ses and csesidx:
        result = {
            "secure_c_ses": secure_c_ses,
            "host_c_oses": host_c_oses or "",
            "csesidx": csesidx
        }
        if team_id:
            result["team_id"] = team_id
        return result
    else:
        return None

def test_cookie_with_jwt(account: Dict[str, str]) -> bool:
    """通过 JWT 测试 Cookie 是否有效"""
    import requests
    
    secure_c_ses = account.get("secure_c_ses")
    host_c_oses = account.get("host_c_oses", "")
    csesidx = account.get("csesidx")
    
    if not secure_c_ses or not csesidx:
        print("[验证] ✗ 缺少 secure_c_ses 或 csesidx")
        return False
    
    url = f"{GETOXSRF_URL}?csesidx={csesidx}"
    headers = {
        "accept": "*/*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "cookie": f'__Secure-C_SES={secure_c_ses}; __Host-C_OSES={host_c_oses}',
    }
    
    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=30)
        if resp.status_code == 200:
            text = resp.text
            if text.startswith(")]}'\n") or text.startswith(")]}'"):
                text = text[4:].strip()
            data = json.loads(text)
            key_id = data.get("keyId")
            if key_id:
                print(f"[验证] ✓ JWT 验证成功 - key_id: {key_id[:50]}...")
                return True
            else:
                print("[验证] ✗ JWT 验证失败 - 缺少 keyId")
                return False
        else:
            print(f"[验证] ✗ JWT 验证失败 - HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"[验证] ✗ JWT 验证失败 - {e}")
        return False

def main():
    """主函数 - 使用 Playwright 实现（可以替换为 chrome-mcp）"""
    import os
    import sys
    from playwright.sync_api import sync_playwright
    
    # 自动检测是否应该使用无头模式
    # 1. 检查命令行参数 --headless
    # 2. 检查是否有 DISPLAY 环境变量（Linux/Unix）
    # 3. 检查是否在 Windows（通常有图形界面）
    use_headless = True
    if "--headless" in sys.argv:
        use_headless = True
    elif os.name != "nt":  # 非 Windows 系统
        # Linux/Unix: 检查是否有 DISPLAY 环境变量
        if not os.environ.get("DISPLAY"):
            use_headless = True
            print("[提示] 未检测到 DISPLAY 环境变量，将使用无头模式")
    
    print("="*60)
    print("自动获取邮箱和验证码登录 Gemini Business")
    print("="*60)
    if use_headless:
        print("[模式] 无头模式（headless=True）")
    else:
        print("[模式] 可视化模式（headless=False）")
    print()
    
    with sync_playwright() as p:
        # Linux 系统需要添加额外的启动参数
        launch_args = []
        if os.name != 'nt':  # 非 Windows 系统
            launch_args = ['--no-sandbox', '--disable-setuid-sandbox']
        
        # 添加反检测参数，降低被 reCAPTCHA 识别的风险
        launch_args.extend([
            '--disable-blink-features=AutomationControlled',  # 禁用自动化控制特征
            '--disable-dev-shm-usage',  # 避免共享内存问题
            '--no-first-run',  # 跳过首次运行
            '--no-default-browser-check',  # 跳过默认浏览器检查
            '--disable-infobars',  # 禁用信息栏
            '--disable-web-security',  # 禁用 Web 安全（谨慎使用）
            '--disable-features=IsolateOrigins,site-per-process',  # 禁用某些安全特性
        ])
        
        # 使用原来的无头模式
        if use_headless:
            browser = p.chromium.launch(headless=True, args=launch_args)
        else:
            # 有头模式
            browser = p.chromium.launch(headless=False, args=launch_args)
        
        # 创建浏览器上下文，使用真实的用户代理和视口
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            # 添加额外的反检测措施
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )
        
        # 注入脚本以隐藏自动化特征（超级增强版，更强力绕过 reCAPTCHA）
        context.add_init_script("""
            (function() {
                // 1. 完全移除 webdriver 特征（必须在最前面执行）
                try {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                        configurable: true,
                        enumerable: false
                    });
                } catch(e) {}
                
                try {
                    delete navigator.__proto__.webdriver;
                } catch(e) {}
                
                try {
                    delete navigator.webdriver;
                } catch(e) {}
                
                // 覆盖 Object.getOwnPropertyDescriptor（必须在定义后立即覆盖）
                const originalGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
                Object.getOwnPropertyDescriptor = function(obj, prop) {
                    if (obj === navigator && prop === 'webdriver') {
                        return undefined;
                    }
                    return originalGetOwnPropertyDescriptor.apply(this, arguments);
                };
                
                // 覆盖 Object.getOwnPropertyNames（防止枚举 webdriver）
                const originalGetOwnPropertyNames = Object.getOwnPropertyNames;
                Object.getOwnPropertyNames = function(obj) {
                    const names = originalGetOwnPropertyNames.apply(this, arguments);
                    if (obj === navigator) {
                        return names.filter(name => name !== 'webdriver');
                    }
                    return names;
                };
                
                // 覆盖 Object.keys（防止枚举 webdriver）
                const originalKeys = Object.keys;
                Object.keys = function(obj) {
                    const keys = originalKeys.apply(this, arguments);
                    if (obj === navigator) {
                        return keys.filter(key => key !== 'webdriver');
                    }
                    return keys;
                };
                
                // 2. 创建完整的 chrome 对象（模拟真实 Chrome）
                window.chrome = {
                    runtime: {
                        onConnect: undefined,
                        onMessage: undefined
                    },
                    loadTimes: function() {
                        return {
                            commitLoadTime: Date.now() / 1000 - Math.random() * 2,
                            connectionInfo: 'http/1.1',
                            finishDocumentLoadTime: Date.now() / 1000 - Math.random(),
                            finishLoadTime: Date.now() / 1000 - Math.random() * 0.5,
                            firstPaintAfterLoadTime: 0,
                            firstPaintTime: Date.now() / 1000 - Math.random() * 1.5,
                            navigationType: 'Other',
                            npnNegotiatedProtocol: 'unknown',
                            requestTime: Date.now() / 1000 - Math.random() * 3,
                            startLoadTime: Date.now() / 1000 - Math.random() * 3.5,
                            wasAlternateProtocolAvailable: false,
                            wasFetchedViaSpdy: false,
                            wasNpnNegotiated: false
                        };
                    },
                    csi: function() {
                        return {
                            startE: Date.now(),
                            onloadT: Date.now() + Math.random() * 100,
                            pageT: Math.random() * 100,
                            tran: 15
                        };
                    },
                    app: {
                        isInstalled: false,
                        InstallState: {
                            DISABLED: 'disabled',
                            INSTALLED: 'installed',
                            NOT_INSTALLED: 'not_installed'
                        },
                        RunningState: {
                            CANNOT_RUN: 'cannot_run',
                            READY_TO_RUN: 'ready_to_run',
                            RUNNING: 'running'
                        }
                    }
                };
                
                // 3. 覆盖 permissions API
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => {
                    if (parameters.name === 'notifications') {
                        return Promise.resolve({ state: Notification.permission });
                    }
                    return originalQuery ? originalQuery(parameters) : Promise.resolve({ state: 'granted' });
                };
                
                // 4. 创建真实的 plugins 数组
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [];
                        for (let i = 0; i < 5; i++) {
                            plugins.push({
                                name: `Plugin ${i}`,
                                description: `Plugin ${i} Description`,
                                filename: `plugin${i}.dll`,
                                length: 1
                            });
                        }
                        return plugins;
                    },
                    configurable: true
                });
                
                // 5. 覆盖 languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
                    configurable: true
                });
                
                // 6. 覆盖 getBattery
                if (navigator.getBattery) {
                    navigator.getBattery = () => Promise.resolve({
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: 0.95 + Math.random() * 0.05
                    });
                }
                
                // 7. 覆盖 connection
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 50 + Math.random() * 50,
                        downlink: 10 + Math.random() * 5,
                        saveData: false,
                        onchange: null
                    }),
                    configurable: true
                });
                
                // 8. 覆盖硬件信息
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8,
                    configurable: true
                });
                
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8,
                    configurable: true
                });
                
                // 9. 覆盖 platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32',
                    configurable: true
                });
                
                // 10. 增强 Canvas 指纹保护
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                
                HTMLCanvasElement.prototype.toDataURL = function(type) {
                    const context = this.getContext('2d');
                    if (context) {
                        const imageData = context.getImageData(0, 0, this.width, this.height);
                        // 添加微小的随机噪声（人眼不可见）
                        for (let i = 0; i < imageData.data.length; i += 4) {
                            if (Math.random() < 0.01) {
                                imageData.data[i] = Math.min(255, imageData.data[i] + Math.random() * 2 - 1);
                            }
                        }
                        context.putImageData(imageData, 0, 0);
                    }
                    return originalToDataURL.apply(this, arguments);
                };
                
                // 11. 增强 WebGL 指纹保护
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    if (parameter === 7936) {
                        return 'WebGL 2.0';
                    }
                    if (parameter === 7937) {
                        return 'WebGL GLSL ES 3.00';
                    }
                    return getParameter.apply(this, arguments);
                };
                
                // 12. 覆盖 AudioContext（音频指纹保护）
                if (window.AudioContext || window.webkitAudioContext) {
                    const AudioContext = window.AudioContext || window.webkitAudioContext;
                    const originalCreateOscillator = AudioContext.prototype.createOscillator;
                    AudioContext.prototype.createOscillator = function() {
                        const oscillator = originalCreateOscillator.apply(this, arguments);
                        const originalFrequency = oscillator.frequency.value;
                        Object.defineProperty(oscillator.frequency, 'value', {
                            get: () => originalFrequency + (Math.random() * 0.0001 - 0.00005),
                            set: function(v) { originalFrequency = v; }
                        });
                        return oscillator;
                    };
                }
                
                // 13. 添加鼠标和键盘事件监听器（模拟真实用户）
                window._mouseEvents = 0;
                window._keyboardEvents = 0;
                
                document.addEventListener('mousemove', () => { window._mouseEvents++; }, true);
                document.addEventListener('click', () => { window._mouseEvents++; }, true);
                document.addEventListener('keydown', () => { window._keyboardEvents++; }, true);
                document.addEventListener('mousedown', () => { window._mouseEvents++; }, true);
                document.addEventListener('mouseup', () => { window._mouseEvents++; }, true);
                document.addEventListener('keyup', () => { window._keyboardEvents++; }, true);
                document.addEventListener('keypress', () => { window._keyboardEvents++; }, true);
                
                // 模拟一些初始鼠标移动和键盘事件（让事件计数器不为0）
                setTimeout(() => {
                    // 触发多个鼠标移动事件（模拟真实用户移动鼠标）
                    for (let i = 0; i < 5; i++) {
                        setTimeout(() => {
                            const event = new MouseEvent('mousemove', {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: Math.random() * window.innerWidth,
                                clientY: Math.random() * window.innerHeight
                            });
                            document.dispatchEvent(event);
                            window._mouseEvents++;
                        }, i * 50);
                    }
                    
                    // 触发一些键盘事件（模拟真实用户可能按下的键）
                    setTimeout(() => {
                        const keyEvent = new KeyboardEvent('keydown', {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            key: 'Tab',
                            code: 'Tab',
                            keyCode: 9
                        });
                        document.dispatchEvent(keyEvent);
                        window._keyboardEvents++;
                    }, 300);
                }, 200);
                
                // 定期触发鼠标移动事件（模拟真实用户偶尔移动鼠标）
                setInterval(() => {
                    if (Math.random() < 0.3) { // 30% 概率触发
                        const event = new MouseEvent('mousemove', {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX: Math.random() * window.innerWidth,
                            clientY: Math.random() * window.innerHeight
                        });
                        document.dispatchEvent(event);
                        window._mouseEvents++;
                    }
                }, 2000); // 每2秒检查一次
                
                // 14. 覆盖 toString 方法（防止检测）
                const originalToString = Function.prototype.toString;
                Function.prototype.toString = function() {
                    if (this === navigator.getBattery || this === navigator.permissions.query) {
                        return 'function ' + this.name + '() { [native code] }';
                    }
                    return originalToString.apply(this, arguments);
                };
                
                // 15. 覆盖 Object.getOwnPropertyDescriptor（防止检测 webdriver）
                const originalGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
                Object.getOwnPropertyDescriptor = function(obj, prop) {
                    if (obj === navigator && prop === 'webdriver') {
                        return undefined;
                    }
                    return originalGetOwnPropertyDescriptor.apply(this, arguments);
                };
                
                // 16. 添加真实的屏幕信息
                Object.defineProperty(screen, 'availWidth', {
                    get: () => 1920,
                    configurable: true
                });
                Object.defineProperty(screen, 'availHeight', {
                    get: () => 1040,
                    configurable: true
                });
                Object.defineProperty(screen, 'width', {
                    get: () => 1920,
                    configurable: true
                });
                Object.defineProperty(screen, 'height', {
                    get: () => 1080,
                    configurable: true
                });
                Object.defineProperty(screen, 'colorDepth', {
                    get: () => 24,
                    configurable: true
                });
                Object.defineProperty(screen, 'pixelDepth', {
                    get: () => 24,
                    configurable: true
                });
                
                // 17. 拦截 browserinfo API 调用（防止发送检测信息）
                const originalFetch = window.fetch;
                window.fetch = function(...args) {
                    const url = args[0];
                    if (typeof url === 'string' && url.includes('browserinfo')) {
                        // 修改请求体，确保发送正确的屏幕尺寸
                        if (args[1] && args[1].body) {
                            try {
                                const body = JSON.parse(args[1].body);
                                if (body['f.req'] && Array.isArray(body['f.req'])) {
                                    const reqData = JSON.parse(body['f.req'][0]);
                                    // 确保屏幕尺寸正确：[高度, 宽度]
                                    if (reqData[1] && Array.isArray(reqData[1])) {
                                        reqData[1] = [null, 1080, 1920];
                                    }
                                    // 确保窗口尺寸正确
                                    if (reqData[2] && Array.isArray(reqData[2])) {
                                        reqData[2] = [null, window.innerHeight, window.innerWidth];
                                    }
                                    body['f.req'][0] = JSON.stringify(reqData);
                                    args[1].body = JSON.stringify(body);
                                }
                            } catch(e) {
                                // 如果解析失败，继续使用原始请求
                            }
                        }
                    }
                    return originalFetch.apply(this, args);
                };
                
                // 18. 拦截 XMLHttpRequest（防止通过 XHR 发送检测信息）
                const originalXHROpen = XMLHttpRequest.prototype.open;
                const originalXHRSend = XMLHttpRequest.prototype.send;
                
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this._url = url;
                    return originalXHROpen.apply(this, [method, url, ...rest]);
                };
                
                XMLHttpRequest.prototype.send = function(data) {
                    if (this._url && this._url.includes('browserinfo') && data) {
                        try {
                            const body = JSON.parse(data);
                            if (body['f.req'] && Array.isArray(body['f.req'])) {
                                const reqData = JSON.parse(body['f.req'][0]);
                                // 确保屏幕尺寸正确
                                if (reqData[1] && Array.isArray(reqData[1])) {
                                    reqData[1] = [null, 1080, 1920];
                                }
                                // 确保窗口尺寸正确
                                if (reqData[2] && Array.isArray(reqData[2])) {
                                    reqData[2] = [null, window.innerHeight, window.innerWidth];
                                }
                                body['f.req'][0] = JSON.stringify(reqData);
                                data = JSON.stringify(body);
                            }
                        } catch(e) {
                            // 如果解析失败，继续使用原始数据
                        }
                    }
                    return originalXHRSend.apply(this, [data]);
                };
                
                // 19. 持续监控并修复 webdriver 属性（防止被重新设置）
                setInterval(() => {
                    if (navigator.webdriver !== undefined) {
                        try {
                            Object.defineProperty(navigator, 'webdriver', {
                                get: () => undefined,
                                configurable: true,
                                enumerable: false
                            });
                        } catch(e) {}
                    }
                }, 100);
            })();
        """)
        
        # 创建两个标签页：一个用于临时邮箱，一个用于登录
        email_page = context.new_page()
        login_page = context.new_page()
        
        try:
            # 步骤0：选择要使用的临时邮箱 URL
            tempmail_url, tempmail_name = select_tempmail_url()
            
            # 步骤1：获取临时邮箱
            print("\n" + "="*60)
            print("步骤1: 获取临时邮箱")
            print("="*60)
            email = get_email_from_tempmail(email_page, tempmail_url)
            
            if not email:
                print("\n✗ 无法获取邮箱，退出")
                return
            
            # 步骤2：在登录页面输入邮箱
            print("\n" + "="*60)
            print("步骤2: 在登录页面输入邮箱")
            print("="*60)
            login_page.goto(GEMINI_LOGIN_URL, wait_until="networkidle", timeout=60000)
            # 页面加载后随机延迟（模拟用户阅读页面）
            human_like_delay(login_page, 2000, 4000)
            
            # 填写邮箱（使用实际页面的 id / aria-label / name）
            try:
                email_input = login_page.locator(
                    "#email-input, input[aria-label='邮箱'], input[type='text'][name='loginHint']"
                ).first
                if email_input.is_visible():
                    # 输入前随机延迟（模拟用户思考）
                    human_like_delay(login_page, 500, 1500)
                    # 模拟人类输入速度（逐字符输入）
                    email_input.fill(email)
                    print(f"[登录] ✓ 已填写邮箱: {email}")
                    # 输入后随机延迟（模拟用户检查输入）
                    human_like_delay(login_page, 1000, 2500)
                    
                    # 点击继续
                    try:
                        continue_btn = login_page.locator(
                            "button#log-in-button, button:has-text('Continue'), button:has-text('继续')"
                        ).first
                        if continue_btn.is_visible():
                            # 在点击前等待 reCAPTCHA 准备好
                            if wait_for_recaptcha_ready(login_page, timeout=5):
                                print("[登录] reCAPTCHA 已准备好，准备点击按钮...")
                            
                            # 点击前随机延迟（模拟用户犹豫）
                            human_like_delay(login_page, 800, 2000)
                            continue_btn.click()
                            print("[登录] ✓ 已点击继续按钮")
                            
                            # 点击后等待 reCAPTCHA 验证完成
                            wait_for_recaptcha_complete(login_page, timeout=30)
                            
                            # 点击后随机延迟（模拟等待页面响应）
                            human_like_delay(login_page, 2000, 4000)
                    except:
                        email_input.press("Enter")
                        login_page.wait_for_timeout(5000)
            except Exception as e:
                print(f"[登录] ⚠ 填写邮箱时出错: {e}")
            
            # 步骤3和4：获取验证码并登录（支持验证码错误时自动重试）
            max_retry = 3  # 最多重试3次
            retry_count = 0
            success = False
            
            while retry_count < max_retry and not success:
                if retry_count > 0:
                    print("\n" + "="*60)
                    print(f"步骤3-4: 刷新邮件并重新获取验证码 (重试 {retry_count}/{max_retry-1})")
                    print("="*60)
                    # 重试模式：立即刷新并提取，不等待
                    code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=True, account_config=None)
                else:
                    print("\n" + "="*60)
                    print("步骤3: 获取验证码")
                    print("="*60)
                    # 第一次：等待邮件到达
                    code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=False, account_config=None)
                
                if not code:
                    if retry_count > 0:
                        print("\n✗ 重试时无法获取验证码，退出")
                    else:
                        print("\n✗ 无法获取验证码，退出")
                    return
                
                print("\n" + "="*60)
                print("步骤4: 填写验证码并登录")
                print("="*60)
                result = login_with_email_and_code(login_page, email, code)
                
                if result == "CODE_ERROR":
                    print("\n[重试] 验证码错误，将刷新邮件并重新获取最新验证码...")
                    retry_count += 1
                    # 等待一下再继续，避免过快重试
                    time.sleep(3)
                    continue
                elif result is True:
                    success = True
                    break
                else:
                    print("\n✗ 登录失败")
                    return
            
            if not success:
                print(f"\n✗ 登录失败（已重试 {retry_count} 次）")
                return
            
            # 步骤5：提取 Cookie 和 csesidx
            print("\n" + "="*60)
            print("步骤5: 提取 Cookie 和 csesidx")
            print("="*60)
            cookies_data = extract_cookies_and_csesidx(login_page)
            
            if not cookies_data:
                print("\n✗ 无法提取 Cookie 和 csesidx")
                return
            
            # 步骤6：验证 Cookie 是否有效
            print("\n" + "="*60)
            print("步骤6: 验证 Cookie 是否有效")
            print("="*60)
            is_valid = test_cookie_with_jwt(cookies_data)
            
            # 最终结果
            print("\n" + "="*60)
            print("最终结果")
            print("="*60)
            print(f"邮箱: {email}")
            print(f"验证码: {code}")
            print(f"Cookie 提取: {'✓ 成功' if cookies_data else '✗ 失败'}")
            print(f"JWT 验证: {'✓ 有效' if is_valid else '✗ 无效'}")
            
            if is_valid:
                print("\n✓ 登录成功，Cookie 有效！")
                print("\n提取的 Cookie 信息：")
                print(f"  secure_c_ses: {cookies_data['secure_c_ses'][:50]}...")
                print(f"  host_c_oses: {cookies_data.get('host_c_oses', 'N/A')[:50] if cookies_data.get('host_c_oses') else 'N/A'}...")
                print(f"  csesidx: {cookies_data['csesidx']}")
                
                # 保存到配置文件（可选）
                save_input = input("\n是否保存到配置文件？(y/n): ").strip().lower()
                if save_input == 'y':
                    save_to_config(cookies_data, tempmail_name=tempmail_name)
            else:
                print("\n✗ Cookie 无效，登录可能失败")
            
            print("\n[提示] 脚本执行完毕，浏览器窗口将保持打开，请手动关闭。")
            
        except Exception as e:
            print(f"\n✗ 发生错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # 在退出 Playwright 上下文前等待用户确认，避免浏览器立即被关闭
            try:
                input("\n[提示] 按回车键关闭浏览器并退出脚本...")
            except EOFError:
                # 在某些非交互环境下可能没有标准输入，直接跳过
                pass

def save_to_config(cookies_data: Dict[str, str], account_index: Optional[int] = None, tempmail_name: Optional[str] = None):
    """保存 Cookie 到配置（支持数据库和 JSON）
    
    Args:
        cookies_data: Cookie 数据
        account_index: 如果提供，更新指定索引的账号；否则创建新账号
        tempmail_name: 临时邮箱名称（用于显示）
    """
    try:
        # 尝试使用 account_manager（如果可用，会自动使用数据库）
        try:
            import sys
            from pathlib import Path
            # 添加项目根目录到路径
            project_root = Path(__file__).parent
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            
            from app.account_manager import account_manager
            
            # 确保配置已加载
            if account_manager.config is None:
                account_manager.load_config()
            
            account_data = {
                "secure_c_ses": cookies_data["secure_c_ses"],
                "host_c_oses": cookies_data.get("host_c_oses", ""),
                "csesidx": cookies_data["csesidx"],
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                "available": True,
            }
            
            # 如果提取到了 team_id，添加到账号中
            if "team_id" in cookies_data and cookies_data["team_id"]:
                account_data["team_id"] = cookies_data["team_id"]
            
            # 如果提供了邮箱名称，保存它
            if tempmail_name:
                account_data["tempmail_name"] = tempmail_name
            
            # 更新现有账号或创建新账号
            if account_index is not None and 0 <= account_index < len(account_manager.accounts):
                # 更新现有账号，只更新 Cookie 相关字段，保留其他所有字段
                old_account = account_manager.accounts[account_index]
                
                # 只更新 Cookie 相关字段
                old_account["secure_c_ses"] = account_data["secure_c_ses"]
                old_account["host_c_oses"] = account_data["host_c_oses"]
                old_account["csesidx"] = account_data["csesidx"]
                
                # 如果新数据有 team_id，更新它；否则保留原有的（包括空字符串）
                if "team_id" in cookies_data and cookies_data["team_id"]:
                    old_account["team_id"] = cookies_data["team_id"]
                # 如果新数据没有 team_id，保留原有的（不覆盖）
                
                # 如果提供了邮箱名称，更新它
                if tempmail_name:
                    old_account["tempmail_name"] = tempmail_name
                # 如果新数据没有 tempmail_name，保留原有的（不覆盖）
                
                # 保留 tempmail_url（如果存在）
                # 不更新 user_agent，保留原有的
                
                # 清除过期标记
                old_account.pop("cookie_expired", None)
                old_account.pop("cookie_expired_time", None)
                
                # 恢复账号可用状态
                old_account["available"] = True
                
                account_manager.config["accounts"] = account_manager.accounts
                account_manager.save_config()
                print(f"[保存] ✓ 已更新账号 {account_index}")
            else:
                # 创建新账号
                account_manager.accounts.append(account_data)
                account_manager.config["accounts"] = account_manager.accounts
                account_manager.save_config()
                print(f"[保存] ✓ 已保存新账号（账号 {len(account_manager.accounts) - 1}）")
            
            return
        except ImportError:
            # account_manager 不可用，回退到 JSON
            pass
        except Exception as e:
            print(f"[保存] 使用 account_manager 失败: {e}，回退到 JSON")
            import traceback
            traceback.print_exc()
        
        # 回退到 JSON 方式（向后兼容）
        config_file = Path("business_gemini_session.json")
        
        if not config_file.exists():
            print("[保存] ✗ 配置文件不存在")
            return
        
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        
        if "accounts" not in config:
            config["accounts"] = []
        
        account_data = {
            "secure_c_ses": cookies_data["secure_c_ses"],
            "host_c_oses": cookies_data.get("host_c_oses", ""),
            "csesidx": cookies_data["csesidx"],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "available": True,
            "quota_usage": {
                "text_queries": 0,
                "images": 0,
                "videos": 0,
                "search_queries": 0,
                "deep_research": 0
            },
            "quota_reset_date": None,
        }
        
        # 如果提取到了 team_id，添加到账号中
        if "team_id" in cookies_data and cookies_data["team_id"]:
            account_data["team_id"] = cookies_data["team_id"]
        
        # 如果提供了邮箱名称，保存它
        if tempmail_name:
            account_data["tempmail_name"] = tempmail_name
        
        # 更新现有账号或创建新账号
        if account_index is not None and 0 <= account_index < len(config["accounts"]):
            # 更新现有账号，保留原有的一些字段
            old_account = config["accounts"][account_index]
            # 保留 quota_usage 和 quota_reset_date（如果存在）
            if "quota_usage" in old_account:
                account_data["quota_usage"] = old_account["quota_usage"]
            if "quota_reset_date" in old_account:
                account_data["quota_reset_date"] = old_account["quota_reset_date"]
            # 保留 team_id（如果新数据没有）
            if "team_id" not in account_data and "team_id" in old_account:
                account_data["team_id"] = old_account["team_id"]
            # 保留 tempmail_url（如果存在）
            if "tempmail_url" in old_account:
                account_data["tempmail_url"] = old_account["tempmail_url"]
            # 清除过期标记
            account_data.pop("cookie_expired", None)
            account_data.pop("cookie_expired_time", None)
            
            config["accounts"][account_index] = account_data
            print(f"[保存] ✓ 已更新配置文件中的账号 {account_index}")
        else:
            # 创建新账号
            config["accounts"].append(account_data)
            print(f"[保存] ✓ 已保存到配置文件（账号 {len(config['accounts'])}）")
        
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        
    except Exception as e:
        print(f"[保存] ✗ 保存失败: {e}")
        import traceback
        traceback.print_exc()

def refresh_single_account(account_idx: int, account: dict, headless: bool = True, mode: str = "auto") -> bool:
    """刷新单个账号的 Cookie（使用临时邮箱方式）
    
    Args:
        account_idx: 账号索引
        account: 账号配置字典
        headless: 是否使用无头模式
        mode: 获取验证码的模式（已废弃，现在自动切换）
            - "auto": 自动选择（优先 API 方式，整个流程失败后自动切换到浏览器方式重新执行）
            - "api": 强制使用 API 方式（整个流程使用 API 方式获取验证码）
            - "browser": 强制使用浏览器方式（整个流程使用浏览器方式获取验证码）
    
    Returns:
        bool: 是否刷新成功
    """
    import os
    
    # 默认使用自动模式：先尝试 API 方式，失败后自动切换到浏览器方式
    if mode == "auto":
        # 先尝试 API 方式
        print(f"[登录] 尝试使用 API 方式刷新账号 {account_idx}...")
        success = _refresh_single_account_internal(account_idx, account, headless, mode="api")
        
        if success:
            print(f"[登录] ✓ API 方式刷新账号 {account_idx} 成功")
            return True
        else:
            # API 方式失败，自动切换到浏览器方式重新执行整个流程
            print(f"[登录] ⚠ API 方式刷新账号 {account_idx} 失败，自动切换到浏览器方式重新执行...")
            return _refresh_single_account_internal(account_idx, account, headless, mode="browser")
    else:
        # 使用指定的模式（api 或 browser）
        return _refresh_single_account_internal(account_idx, account, headless, mode=mode)

def _refresh_single_account_internal(account_idx: int, account: dict, headless: bool = True, mode: str = "api") -> bool:
    """刷新单个账号的 Cookie（内部实现函数）
    
    Args:
        account_idx: 账号索引
        account: 账号配置字典
        headless: 是否使用无头模式
        mode: 获取验证码的模式
            - "api": 强制使用 API 方式（整个流程使用 API 方式获取验证码）
            - "browser": 强制使用浏览器方式（整个流程使用浏览器方式获取验证码）
    
    Returns:
        bool: 是否刷新成功
    """
    import os
    
    # 如果 headless 参数为 True，但系统有图形界面，可以提示
    # 如果 headless 参数为 False，但系统无图形界面，自动切换为无头模式
    if not headless and os.name != "nt":  # 非 Windows 系统
        if not os.environ.get("DISPLAY"):
            print("[提示] 未检测到 DISPLAY 环境变量，将使用无头模式")
            headless = True
    
    # 调试日志已关闭
    # print("="*60)
    # print(f"自动刷新账号 {account_idx} 的 Cookie（使用临时邮箱）")
    # print("="*60)
    # if headless:
    #     print("[模式] 无头模式（headless=True）")
    # else:
    #     print("[模式] 可视化模式（headless=False）")
    # print()
    
    try:
        # 选择临时邮箱（优先使用账号配置中的邮箱）
        try:
            print(f"[登录] 正在选择临时邮箱...")
            tempmail_url, tempmail_name = select_tempmail_url(account)
            if not tempmail_name:
                tempmail_name = account.get("tempmail_name", "未知邮箱")
            print(f"[登录] ✓ 已选择临时邮箱: {tempmail_name}")
        except Exception as e:
            print(f"[单个账号刷新] ✗ 账号 {account_idx} 选择邮箱失败: {e}")
            return False
        
        # 使用 Playwright 刷新
        import os
        from playwright.sync_api import sync_playwright
        
        print(f"[登录] 正在启动浏览器...")
        with sync_playwright() as p:
            # Linux 系统需要添加额外的启动参数
            launch_args = []
            if os.name != 'nt':  # 非 Windows 系统
                launch_args = ['--no-sandbox', '--disable-setuid-sandbox']
            
            # 添加反检测参数，降低被 reCAPTCHA 识别的风险
            launch_args.extend([
                '--disable-blink-features=AutomationControlled',  # 禁用自动化控制特征
                '--disable-dev-shm-usage',  # 避免共享内存问题
                '--no-first-run',  # 跳过首次运行
                '--no-default-browser-check',  # 跳过默认浏览器检查
                '--disable-infobars',  # 禁用信息栏
                '--disable-web-security',  # 禁用 Web 安全（谨慎使用）
                '--disable-features=IsolateOrigins,site-per-process',  # 禁用某些安全特性
            ])
            
            # 使用原来的无头模式
            if headless:
                browser = p.chromium.launch(headless=True, args=launch_args)
            else:
                # 有头模式
                browser = p.chromium.launch(headless=False, args=launch_args)
            print(f"[登录] ✓ 浏览器已启动")
            
            # 创建浏览器上下文，使用真实的用户代理和视口
            print(f"[登录] 正在创建浏览器上下文...")
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                # 添加额外的反检测措施
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                }
            )
            
            # 注入脚本以隐藏自动化特征（超级增强版，更强力绕过 reCAPTCHA）
            context.add_init_script("""
                (function() {
                    // 1. 完全移除 webdriver 特征（必须在最前面执行）
                    try {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true,
                            enumerable: false
                        });
                    } catch(e) {}
                    
                    try {
                        delete navigator.__proto__.webdriver;
                    } catch(e) {}
                    
                    try {
                        delete navigator.webdriver;
                    } catch(e) {}
                    
                    // 覆盖 Object.getOwnPropertyDescriptor（必须在定义后立即覆盖）
                    const originalGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
                    Object.getOwnPropertyDescriptor = function(obj, prop) {
                        if (obj === navigator && prop === 'webdriver') {
                            return undefined;
                        }
                        return originalGetOwnPropertyDescriptor.apply(this, arguments);
                    };
                    
                    // 覆盖 Object.getOwnPropertyNames（防止枚举 webdriver）
                    const originalGetOwnPropertyNames = Object.getOwnPropertyNames;
                    Object.getOwnPropertyNames = function(obj) {
                        const names = originalGetOwnPropertyNames.apply(this, arguments);
                        if (obj === navigator) {
                            return names.filter(name => name !== 'webdriver');
                        }
                        return names;
                    };
                    
                    // 覆盖 Object.keys（防止枚举 webdriver）
                    const originalKeys = Object.keys;
                    Object.keys = function(obj) {
                        const keys = originalKeys.apply(this, arguments);
                        if (obj === navigator) {
                            return keys.filter(key => key !== 'webdriver');
                        }
                        return keys;
                    };
                    
                    // 2. 创建完整的 chrome 对象（模拟真实 Chrome）
                    window.chrome = {
                        runtime: {
                            onConnect: undefined,
                            onMessage: undefined
                        },
                        loadTimes: function() {
                            return {
                                commitLoadTime: Date.now() / 1000 - Math.random() * 2,
                                connectionInfo: 'http/1.1',
                                finishDocumentLoadTime: Date.now() / 1000 - Math.random(),
                                finishLoadTime: Date.now() / 1000 - Math.random() * 0.5,
                                firstPaintAfterLoadTime: 0,
                                firstPaintTime: Date.now() / 1000 - Math.random() * 1.5,
                                navigationType: 'Other',
                                npnNegotiatedProtocol: 'unknown',
                                requestTime: Date.now() / 1000 - Math.random() * 3,
                                startLoadTime: Date.now() / 1000 - Math.random() * 3.5,
                                wasAlternateProtocolAvailable: false,
                                wasFetchedViaSpdy: false,
                                wasNpnNegotiated: false
                            };
                        },
                        csi: function() {
                            return {
                                startE: Date.now(),
                                onloadT: Date.now() + Math.random() * 100,
                                pageT: Math.random() * 100,
                                tran: 15
                            };
                        },
                        app: {
                            isInstalled: false,
                            InstallState: {
                                DISABLED: 'disabled',
                                INSTALLED: 'installed',
                                NOT_INSTALLED: 'not_installed'
                            },
                            RunningState: {
                                CANNOT_RUN: 'cannot_run',
                                READY_TO_RUN: 'ready_to_run',
                                RUNNING: 'running'
                            }
                        }
                    };
                    
                    // 3. 覆盖 permissions API
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => {
                        if (parameters.name === 'notifications') {
                            return Promise.resolve({ state: Notification.permission });
                        }
                        return originalQuery ? originalQuery(parameters) : Promise.resolve({ state: 'granted' });
                    };
                    
                    // 4. 创建真实的 plugins 数组
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => {
                            const plugins = [];
                            for (let i = 0; i < 5; i++) {
                                plugins.push({
                                    name: `Plugin ${i}`,
                                    description: `Plugin ${i} Description`,
                                    filename: `plugin${i}.dll`,
                                    length: 1
                                });
                            }
                            return plugins;
                        },
                        configurable: true
                    });
                    
                    // 5. 覆盖 languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en-US', 'en'],
                        configurable: true
                    });
                    
                    // 6. 覆盖 getBattery
                    if (navigator.getBattery) {
                        navigator.getBattery = () => Promise.resolve({
                            charging: true,
                            chargingTime: 0,
                            dischargingTime: Infinity,
                            level: 0.95 + Math.random() * 0.05
                        });
                    }
                    
                    // 7. 覆盖 connection
                    Object.defineProperty(navigator, 'connection', {
                        get: () => ({
                            effectiveType: '4g',
                            rtt: 50 + Math.random() * 50,
                            downlink: 10 + Math.random() * 5,
                            saveData: false,
                            onchange: null
                        }),
                        configurable: true
                    });
                    
                    // 8. 覆盖硬件信息
                    Object.defineProperty(navigator, 'hardwareConcurrency', {
                        get: () => 8,
                        configurable: true
                    });
                    
                    Object.defineProperty(navigator, 'deviceMemory', {
                        get: () => 8,
                        configurable: true
                    });
                    
                    // 9. 覆盖 platform
                    Object.defineProperty(navigator, 'platform', {
                        get: () => 'Win32',
                        configurable: true
                    });
                    
                    // 10. 增强 Canvas 指纹保护
                    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                    
                    HTMLCanvasElement.prototype.toDataURL = function(type) {
                        const context = this.getContext('2d');
                        if (context) {
                            const imageData = context.getImageData(0, 0, this.width, this.height);
                            // 添加微小的随机噪声（人眼不可见）
                            for (let i = 0; i < imageData.data.length; i += 4) {
                                if (Math.random() < 0.01) {
                                    imageData.data[i] = Math.min(255, imageData.data[i] + Math.random() * 2 - 1);
                                }
                            }
                            context.putImageData(imageData, 0, 0);
                        }
                        return originalToDataURL.apply(this, arguments);
                    };
                    
                    // 11. 增强 WebGL 指纹保护
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) {
                            return 'Intel Inc.';
                        }
                        if (parameter === 37446) {
                            return 'Intel Iris OpenGL Engine';
                        }
                        if (parameter === 7936) {
                            return 'WebGL 2.0';
                        }
                        if (parameter === 7937) {
                            return 'WebGL GLSL ES 3.00';
                        }
                        return getParameter.apply(this, arguments);
                    };
                    
                    // 12. 覆盖 AudioContext（音频指纹保护）
                    if (window.AudioContext || window.webkitAudioContext) {
                        const AudioContext = window.AudioContext || window.webkitAudioContext;
                        const originalCreateOscillator = AudioContext.prototype.createOscillator;
                        AudioContext.prototype.createOscillator = function() {
                            const oscillator = originalCreateOscillator.apply(this, arguments);
                            const originalFrequency = oscillator.frequency.value;
                            Object.defineProperty(oscillator.frequency, 'value', {
                                get: () => originalFrequency + (Math.random() * 0.0001 - 0.00005),
                                set: function(v) { originalFrequency = v; }
                            });
                            return oscillator;
                        };
                    }
                    
                    // 13. 添加鼠标和键盘事件监听器（模拟真实用户）
                    let mouseEvents = 0;
                    let keyboardEvents = 0;
                    
                    document.addEventListener('mousemove', () => { mouseEvents++; }, true);
                    document.addEventListener('click', () => { mouseEvents++; }, true);
                    document.addEventListener('keydown', () => { keyboardEvents++; }, true);
                    
                    // 14. 覆盖 toString 方法（防止检测）
                    const originalToString = Function.prototype.toString;
                    Function.prototype.toString = function() {
                        if (this === navigator.getBattery || this === navigator.permissions.query) {
                            return 'function ' + this.name + '() { [native code] }';
                        }
                        return originalToString.apply(this, arguments);
                    };
                    
                    // 15. 覆盖 Object.getOwnPropertyDescriptor（防止检测 webdriver）- 已在前面定义，这里移除重复
                    
                    // 16. 添加真实的屏幕信息
                    Object.defineProperty(screen, 'availWidth', {
                        get: () => 1920,
                        configurable: true
                    });
                    Object.defineProperty(screen, 'availHeight', {
                        get: () => 1040,
                        configurable: true
                    });
                    Object.defineProperty(screen, 'width', {
                        get: () => 1920,
                        configurable: true
                    });
                    Object.defineProperty(screen, 'height', {
                        get: () => 1080,
                        configurable: true
                    });
                    Object.defineProperty(screen, 'colorDepth', {
                        get: () => 24,
                        configurable: true
                    });
                    Object.defineProperty(screen, 'pixelDepth', {
                        get: () => 24,
                        configurable: true
                    });
                    
                    // 17. 拦截 browserinfo API 调用（防止发送检测信息）
                    const originalFetch = window.fetch;
                    window.fetch = function(...args) {
                        const url = args[0];
                        if (typeof url === 'string' && url.includes('browserinfo')) {
                            // 修改请求体，确保发送正确的屏幕尺寸
                            if (args[1] && args[1].body) {
                                try {
                                    const body = JSON.parse(args[1].body);
                                    if (body['f.req'] && Array.isArray(body['f.req'])) {
                                        const reqData = JSON.parse(body['f.req'][0]);
                                        // 确保屏幕尺寸正确：[高度, 宽度]
                                        if (reqData[1] && Array.isArray(reqData[1])) {
                                            reqData[1] = [null, 1080, 1920];
                                        }
                                        // 确保窗口尺寸正确
                                        if (reqData[2] && Array.isArray(reqData[2])) {
                                            reqData[2] = [null, window.innerHeight, window.innerWidth];
                                        }
                                        body['f.req'][0] = JSON.stringify(reqData);
                                        args[1].body = JSON.stringify(body);
                                    }
                                } catch(e) {
                                    // 如果解析失败，继续使用原始请求
                                }
                            }
                        }
                        return originalFetch.apply(this, args);
                    };
                    
                    // 18. 拦截 XMLHttpRequest（防止通过 XHR 发送检测信息）
                    const originalXHROpen = XMLHttpRequest.prototype.open;
                    const originalXHRSend = XMLHttpRequest.prototype.send;
                    
                    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                        this._url = url;
                        return originalXHROpen.apply(this, [method, url, ...rest]);
                    };
                    
                    XMLHttpRequest.prototype.send = function(data) {
                        if (this._url && this._url.includes('browserinfo') && data) {
                            try {
                                const body = JSON.parse(data);
                                if (body['f.req'] && Array.isArray(body['f.req'])) {
                                    const reqData = JSON.parse(body['f.req'][0]);
                                    // 确保屏幕尺寸正确
                                    if (reqData[1] && Array.isArray(reqData[1])) {
                                        reqData[1] = [null, 1080, 1920];
                                    }
                                    // 确保窗口尺寸正确
                                    if (reqData[2] && Array.isArray(reqData[2])) {
                                        reqData[2] = [null, window.innerHeight, window.innerWidth];
                                    }
                                    body['f.req'][0] = JSON.stringify(reqData);
                                    data = JSON.stringify(body);
                                }
                            } catch(e) {
                                // 如果解析失败，继续使用原始数据
                            }
                        }
                        return originalXHRSend.apply(this, [data]);
                    };
                    
                    // 19. 持续监控并修复 webdriver 属性（防止被重新设置）
                    setInterval(() => {
                        if (navigator.webdriver !== undefined) {
                            try {
                                Object.defineProperty(navigator, 'webdriver', {
                                    get: () => undefined,
                                    configurable: true,
                                    enumerable: false
                                });
                            } catch(e) {}
                        }
                    }, 100);
                    
                    // 20. 诊断脚本：检测可能导致"出了点问题"错误的检测点
                    window._detectionDiagnostics = {
                        check: function() {
                            try {
                            const diagnostics = {
                                timestamp: new Date().toISOString(),
                                // 1. webdriver 检测
                                webdriver: {
                                    navigator_webdriver: navigator.webdriver,
                                    proto_webdriver: (function() {
                                        try {
                                            return navigator.__proto__ && navigator.__proto__.webdriver !== undefined;
                                        } catch(e) {
                                            return false;
                                        }
                                    })(),
                                    keys_contains: Object.keys(navigator).includes('webdriver'),
                                    getOwnPropertyNames_contains: Object.getOwnPropertyNames(navigator).includes('webdriver'),
                                    getOwnPropertyDescriptor: (function() {
                                        try {
                                            return Object.getOwnPropertyDescriptor(navigator, 'webdriver') !== undefined;
                                        } catch(e) {
                                            return false;
                                        }
                                    })()
                                },
                                // 2. Chrome 对象检测
                                chrome: {
                                    exists: !!window.chrome,
                                    runtime: !!window.chrome?.runtime,
                                    loadTimes: typeof window.chrome?.loadTimes === 'function',
                                    csi: typeof window.chrome?.csi === 'function'
                                },
                                // 3. 浏览器指纹
                                fingerprint: {
                                    userAgent: navigator.userAgent,
                                    platform: navigator.platform,
                                    languages: navigator.languages,
                                    plugins: navigator.plugins.length,
                                    hardwareConcurrency: navigator.hardwareConcurrency,
                                    deviceMemory: navigator.deviceMemory,
                                    screen: {
                                        width: screen.width,
                                        height: screen.height,
                                        availWidth: screen.availWidth,
                                        availHeight: screen.availHeight
                                    },
                                    window: {
                                        innerWidth: window.innerWidth,
                                        innerHeight: window.innerHeight,
                                        outerWidth: window.outerWidth,
                                        outerHeight: window.outerHeight
                                    }
                                },
                                // 4. 自动化特征检测
                                automation: {
                                    has_playwright: window.__playwright !== undefined || window.__pw_manual !== undefined,
                                    has_puppeteer: window.__PUPPETEER_WORLD__ !== undefined || window.puppeteer !== undefined,
                                    has_selenium: window.__selenium_unwrapped !== undefined || window.__selenium_evaluate !== undefined,
                                    has_webdriver: window.__webdriver_script_fn !== undefined || window.__driver_evaluate !== undefined,
                                    has_phantom: window.callPhantom !== undefined || window._phantom !== undefined
                                },
                                // 5. 事件监听器检测
                                events: {
                                    mouseEvents: window._mouseEvents || 0,
                                    keyboardEvents: window._keyboardEvents || 0
                                },
                                // 6. Canvas 指纹检测
                                canvas: {
                                    toDataURL: typeof HTMLCanvasElement.prototype.toDataURL === 'function',
                                    getImageData: typeof CanvasRenderingContext2D.prototype.getImageData === 'function'
                                },
                                // 7. WebGL 指纹检测
                                webgl: {
                                    getParameter: typeof WebGLRenderingContext.prototype.getParameter === 'function',
                                    getExtension: typeof WebGLRenderingContext.prototype.getExtension === 'function'
                                },
                                // 8. 权限 API 检测
                                permissions: {
                                    query: typeof navigator.permissions?.query === 'function',
                                    getBattery: typeof navigator.getBattery === 'function'
                                },
                                // 9. 网络请求检测
                                network: {
                                    fetch: typeof window.fetch === 'function',
                                    xhr: typeof XMLHttpRequest !== 'undefined',
                                    fetch_intercepted: window._fetchIntercepted || false,
                                    xhr_intercepted: window._xhrIntercepted || false
                                },
                                // 10. reCAPTCHA 检测
                                recaptcha: {
                                    grecaptcha_exists: typeof grecaptcha !== 'undefined',
                                    enterprise: typeof grecaptcha?.enterprise !== 'undefined',
                                    ready: typeof grecaptcha?.ready === 'function'
                                }
                            };
                            
                            // 检测潜在问题
                            const issues = [];
                            try {
                                if (diagnostics.webdriver && diagnostics.webdriver.navigator_webdriver !== undefined) {
                                    issues.push('navigator.webdriver 仍然存在');
                                }
                                if (diagnostics.webdriver && diagnostics.webdriver.proto_webdriver) {
                                    issues.push('navigator.__proto__.webdriver 仍然存在');
                                }
                                if (diagnostics.webdriver && diagnostics.webdriver.keys_contains) {
                                    issues.push('Object.keys(navigator) 包含 webdriver');
                                }
                                if (diagnostics.chrome && !diagnostics.chrome.exists) {
                                    issues.push('window.chrome 对象不存在');
                                }
                                if (diagnostics.automation && (diagnostics.automation.has_playwright || diagnostics.automation.has_puppeteer || 
                                    diagnostics.automation.has_selenium || diagnostics.automation.has_webdriver)) {
                                    issues.push('检测到自动化框架特征');
                                }
                                if (diagnostics.events && diagnostics.events.mouseEvents === 0 && diagnostics.events.keyboardEvents === 0) {
                                    issues.push('未检测到鼠标或键盘事件（可能被检测为自动化）');
                                }
                            } catch(e) {
                                issues.push('诊断过程出错: ' + e.message);
                            }
                            
                            diagnostics.issues = issues;
                            diagnostics.riskLevel = issues.length > 3 ? 'HIGH' : issues.length > 1 ? 'MEDIUM' : 'LOW';
                            
                            return diagnostics;
                            } catch(e) {
                                return {
                                    error: true,
                                    errorMessage: e.message,
                                    errorStack: e.stack ? e.stack.substring(0, 200) : 'No stack',
                                    timestamp: new Date().toISOString()
                                };
                            }
                        }
                    };
                })();
            """)
            print(f"[登录] ✓ 浏览器上下文已创建")
            
            try:
                # 创建两个标签页：一个用于临时邮箱，一个用于登录
                print(f"[登录] 正在创建页面标签...")
                email_page = context.new_page()
                login_page = context.new_page()
                print(f"[登录] ✓ 页面标签已创建")
                
                try:
                    # 步骤1：获取临时邮箱
                    print(f"[登录] 正在获取临时邮箱地址...")
                    email = get_email_from_tempmail(email_page, tempmail_url)
                    
                    if not email:
                        print(f"[单个账号刷新] ✗ 账号 {account_idx} 无法获取邮箱")
                        return False
                    print(f"[登录] ✓ 已获取临时邮箱: {email}")
                    
                    # 步骤2：在登录页面输入邮箱并点击继续，触发发送验证码邮件
                    print(f"[登录] 正在导航到登录页面...")
                    login_page.goto(GEMINI_LOGIN_URL, wait_until="networkidle", timeout=60000)
                    print(f"[登录] ✓ 已导航到登录页面")
                    # 页面加载后随机延迟（模拟用户阅读页面）
                    human_like_delay(login_page, 2000, 4000)
                    
                    try:
                        email_input = login_page.locator(
                            "#email-input, input[aria-label='邮箱'], input[type='text'][name='loginHint']"
                        ).first
                        if email_input.is_visible():
                            # 输入前随机延迟（模拟用户思考）
                            human_like_delay(login_page, 500, 1500)
                            email_input.fill(email)
                            # 调试日志已关闭
                            # print(f"[登录] ✓ 已填写邮箱: {email}")
                            # 输入后随机延迟（模拟用户检查输入）
                            human_like_delay(login_page, 1000, 2500)
                            
                            # 点击继续，触发发送验证码邮件
                            try:
                                continue_btn = login_page.locator(
                                    "button#log-in-button, button:has-text('Continue'), button:has-text('继续')"
                                ).first
                                if continue_btn.is_visible():
                                    # 在点击前等待 reCAPTCHA 准备好
                                    if wait_for_recaptcha_ready(login_page, timeout=5):
                                        print("[登录] reCAPTCHA 已准备好，准备点击按钮...")
                                    
                                    # 点击前随机延迟（模拟用户犹豫）
                                    human_like_delay(login_page, 800, 2000)
                                    # 调试日志已关闭
                                    # print(f"[登录] 点击继续按钮前的 URL: {login_page.url}")
                                    continue_btn.click()
                                    # 调试日志已关闭
                                    # print("[登录] ✓ 已点击继续按钮，等待验证码邮件发送...")
                                    
                                    # 点击后等待 reCAPTCHA 验证完成
                                    wait_for_recaptcha_complete(login_page, timeout=30)
                                    
                                    # 点击后随机延迟（等待页面响应，验证码邮件发送可能需要时间）
                                    human_like_delay(login_page, 2000, 4000)
                                    
                                    # 在等待跳转的过程中，也检查是否在登录页面上出现了成功提示框
                                    try:
                                        # 检查是否有成功提示框（可能在登录页面上短暂出现）
                                        aside_check = login_page.locator("aside.zyTWof-Ng57nc").count()
                                        if aside_check > 0:
                                            sent_elements = login_page.locator("aside.zyTWof-Ng57nc div.zyTWof-gIZMF").all()
                                            for sent_element in sent_elements:
                                                try:
                                                    text = sent_element.text_content() or ""
                                                    if "验证码已发送" in text and "请查收您的邮件" in text:
                                                        print("[登录] ✓ 在登录页面上检测到验证码已发送的成功提示")
                                                        break
                                                except:
                                                    continue
                                    except:
                                        pass
                                    
                                    # 等待页面响应和跳转（最多等待 30 秒，网络慢时需要更长时间）
                                    # 调试日志已关闭
                                    # print("[登录] 等待页面跳转到验证码输入页面...")
                                    max_wait_redirect = 30  # 增加等待时间，适应网络慢的情况
                                    waited_redirect = 0
                                    redirect_success = False
                                    
                                    while waited_redirect < max_wait_redirect:
                                        current_url_after_click = login_page.url
                                        
                                        # 识别不同的 URL 状态：
                                        # 1. auth.business.gemini.google/login - 输入邮箱页面
                                        # 2. auth.business.gemini.google/login/email - 已输入邮箱后的页面（可能显示错误或等待跳转）
                                        # 3. accountverification.business.gemini.google/v1/verify-oob-code - 验证码输入页面
                                        
                                        # 如果仍在 auth.business.gemini.google/login 或 login/email 页面，说明网络慢，继续等待
                                        if ("auth.business.gemini.google" in current_url_after_click 
                                            and "accountverification" not in current_url_after_click):
                                            # 检查是否是 login/email 页面（已输入邮箱后的页面）
                                            if "/login/email" in current_url_after_click:
                                                # 检查是否有错误提示
                                                try:
                                                    page_text = login_page.locator("body").text_content() or ""
                                                    if "server cannot process" in page_text.lower() or "try something else" in page_text.lower():
                                                        print("[登录] ⚠ 检测到服务器错误页面，可能需要重新输入邮箱")
                                                        # 可以尝试重新填写邮箱，或者返回错误
                                                        # 这里先继续等待，看是否会跳转
                                                except:
                                                    pass
                                            # 继续等待跳转到验证码页面
                                            # 调试日志已关闭
                                            # print(f"[登录] 仍在 auth 页面，等待跳转到验证码页面... (已等待 {waited_redirect}秒)")
                                            login_page.wait_for_timeout(2000)
                                            waited_redirect += 2
                                            continue
                                        
                                        # 检查是否跳转到验证码输入页面
                                        code_input_check = login_page.locator("input[name='pinInput'], input[type='text']:not([name='loginHint'])").first
                                        if code_input_check.is_visible():
                                            # 调试日志已关闭
                                            # print(f"[登录] ✓ 确认已跳转到验证码输入页面，当前 URL: {login_page.url}")
                                            # print("[登录] ✓ 验证码邮件应该已发送")
                                            redirect_success = True
                                            break
                                        
                                        # 检查是否在验证码输入页面（通过 URL）
                                        # 必须是 accountverification.business.gemini.google/v1/verify-oob-code
                                        if ("accountverification" in current_url_after_click 
                                            and "verify-oob-code" in current_url_after_click):
                                            # 调试日志已关闭
                                            # print(f"[登录] ✓ 已跳转到验证码页面，当前 URL: {current_url_after_click}")
                                            redirect_success = True
                                            break
                                        
                                        login_page.wait_for_timeout(2000)
                                        waited_redirect += 2
                                    
                                    if not redirect_success:
                                        current_url_final = login_page.url
                                        # 如果仍在 auth 页面，说明网络慢或有问题，需要继续等待
                                        if ("auth.business.gemini.google" in current_url_final 
                                            and "accountverification" not in current_url_final):
                                            print(f"[登录] ⚠ 等待跳转超时（{max_wait_redirect}秒），仍在 auth 页面，继续等待跳转到验证码页面...")
                                            # 继续等待，直到跳转到验证码页面（最多再等待 30 秒）
                                            max_wait_additional = 30
                                            waited_additional = 0
                                            while waited_additional < max_wait_additional:
                                                current_url_additional = login_page.url
                                                # 检查是否跳转到验证码页面（必须是 verify-oob-code）
                                                if ("accountverification" in current_url_additional 
                                                    and "verify-oob-code" in current_url_additional):
                                                    print(f"[登录] ✓ 已跳转到验证码页面: {current_url_additional}")
                                                    redirect_success = True
                                                    break
                                                # 如果仍在 auth 页面，继续等待
                                                if ("auth.business.gemini.google" in current_url_additional 
                                                    and "accountverification" not in current_url_additional):
                                                    login_page.wait_for_timeout(2000)
                                                    waited_additional += 2
                                                    continue
                                                # 其他情况，可能已经跳转，检查验证码输入框
                                                code_input_check = login_page.locator("input[name='pinInput'], input[type='text']:not([name='loginHint'])").first
                                                if code_input_check.is_visible():
                                                    redirect_success = True
                                                    break
                                                login_page.wait_for_timeout(2000)
                                                waited_additional += 2
                                            
                                            if not redirect_success:
                                                print(f"[登录] ✗ 等待跳转超时（总计 {max_wait_redirect + max_wait_additional}秒），无法跳转到验证码页面")
                                                return False
                                        else:
                                            # 调试日志已关闭
                                            # print(f"[登录] ⚠ 等待跳转超时（{max_wait_redirect}秒），当前 URL: {current_url_final}")
                                            # print("[登录] ⚠ 可能未完全跳转到验证码输入页面，继续尝试...")
                                            pass
                                    
                                    # 确认已跳转到验证码页面后，等待并检测验证码邮件发送成功的提示
                                    if redirect_success:
                                        # 再次验证当前 URL，确保真的在验证码页面
                                        final_url_verify = login_page.url
                                        print(f"[登录] 当前 URL: {final_url_verify}")
                                        if ("accountverification" in final_url_verify 
                                            and "verify-oob-code" in final_url_verify):
                                            print(f"[登录] ✓ 已确认跳转到验证码页面")
                                            print(f"[登录] 使用的邮箱地址: {email}")
                                            
                                            # 等待并检测"验证码已发送"的提示框元素
                                            # 根据实际页面分析，提示框是一个 <aside> 元素，包含 <div class="zyTWof-gIZMF">
                                            print("[登录] 等待验证码邮件发送成功的提示框...")
                                            
                                            max_wait_sent = 90  # 增加等待时间到 90 秒（无头模式可能需要更长时间，且不使用方式4）
                                            waited_sent = 0
                                            email_sent_confirmed = False
                                            resend_already_clicked = False  # 记录是否已经点击过"重新发送验证码"
                                            
                                            # 先等待页面稳定（无头模式下可能需要更多时间）
                                            # 由于 reCAPTCHA 验证完成后页面才跳转，验证码邮件可能还在发送中
                                            # 增加等待时间，让验证码邮件有时间发送成功并显示提示框
                                            login_page.wait_for_timeout(5000)  # 等待 5 秒让页面加载
                                            
                                            # 在验证码页面，可能需要等待 reCAPTCHA 完成后，"重新发送验证码"按钮才会出现
                                            # 先检查并等待 reCAPTCHA 完成（如果存在）
                                            print("[登录] 检查验证码页面的 reCAPTCHA 状态...")
                                            if wait_for_recaptcha_ready(login_page, timeout=5):
                                                print("[登录] ✓ 检测到验证码页面的 reCAPTCHA iframe，等待 reCAPTCHA 验证完成...")
                                                wait_for_recaptcha_complete(login_page, timeout=30)
                                            else:
                                                print("[登录] ℹ 验证码页面未检测到 reCAPTCHA，继续等待按钮出现...")
                                            
                                            # 在跳转到验证码页面后，需要先等待"重新发送验证码"按钮出现
                                            # 这表示页面已经加载完成，然后才会出现"验证码已发送"的提示框
                                            # 根据观察，按钮可能需要等待 reCAPTCHA 完成或页面加载完成后才会出现
                                            print("[登录] 等待重新发送验证码按钮出现（表示页面已加载完成）...")
                                            resend_btn_selectors_wait = [
                                                "button[aria-label='重新发送验证码']",
                                                "button:has-text('重新发送验证码')",
                                                "button:has-text('重新发送')",
                                                "button:has-text('Resend')",
                                                "//button[contains(., '重新发送')]",
                                                "//button[contains(., 'Resend')]",
                                            ]
                                            resend_btn_found = False
                                            max_wait_resend_btn = 30  # 最多等待30秒
                                            waited_resend_btn = 0
                                            while waited_resend_btn < max_wait_resend_btn and not resend_btn_found:
                                                for selector_wait in resend_btn_selectors_wait:
                                                    try:
                                                        if selector_wait.startswith("//"):
                                                            resend_btn_wait = login_page.locator(selector_wait).first
                                                        else:
                                                            resend_btn_wait = login_page.locator(selector_wait).first
                                                        btn_count_wait = resend_btn_wait.count()
                                                        if btn_count_wait > 0:
                                                            try:
                                                                resend_btn_wait.wait_for(state="visible", timeout=2000)
                                                                resend_btn_found = True
                                                                print(f"[登录] ✓ 重新发送验证码按钮已出现（选择器: {selector_wait}），等待验证码已发送提示框...")
                                                                break
                                                            except:
                                                                continue
                                                    except:
                                                        continue
                                                if not resend_btn_found:
                                                    login_page.wait_for_timeout(2000)
                                                    waited_resend_btn += 2
                                            
                                            if not resend_btn_found:
                                                print("[登录] ⚠ 等待30秒后仍未找到重新发送验证码按钮，继续检测提示框...")
                                            else:
                                                # 如果找到了"重新发送验证码"按钮，也将其视为验证码已发送成功的标志
                                                # 因为有时候不会出现"验证码已发送"的提示框，但按钮出现就表示验证码已发送
                                                print("[登录] ✓ 检测到重新发送验证码按钮出现，视为验证码已发送成功")
                                                email_sent_confirmed = True
                                            
                                            # 等待按钮出现后，再等待一下让"验证码已发送"提示框出现（如果还没有确认）
                                            if not email_sent_confirmed:
                                                login_page.wait_for_timeout(3000)  # 额外等待 3 秒
                                            
                                            while waited_sent < max_wait_sent and not email_sent_confirmed:
                                                try:
                                                    # 在检测循环中，也检查"重新发送验证码"按钮是否出现
                                                    # 如果按钮出现且可见，也视为验证码已发送成功
                                                    if not resend_btn_found:
                                                        for selector_check in resend_btn_selectors_wait:
                                                            try:
                                                                if selector_check.startswith("//"):
                                                                    resend_btn_check = login_page.locator(selector_check).first
                                                                else:
                                                                    resend_btn_check = login_page.locator(selector_check).first
                                                                btn_count_check = resend_btn_check.count()
                                                                if btn_count_check > 0:
                                                                    try:
                                                                        resend_btn_check.wait_for(state="visible", timeout=1000)
                                                                        is_disabled_check = resend_btn_check.is_disabled()
                                                                        # 如果按钮可见且未禁用，视为验证码已发送成功
                                                                        if not is_disabled_check:
                                                                            print(f"[登录] ✓ 检测到重新发送验证码按钮出现且可用（选择器: {selector_check}），视为验证码已发送成功")
                                                                            email_sent_confirmed = True
                                                                            resend_btn_found = True
                                                                            break
                                                                    except:
                                                                        continue
                                                            except:
                                                                continue
                                                        if email_sent_confirmed:
                                                            break
                                                    
                                                    # 先检查是否有错误提示"出了点问题"，如果有则等待并点击"重新发送验证码"
                                                    try:
                                                        page_text_check = login_page.locator("body").text_content() or ""
                                                        if "出了点问题" in page_text_check and "请稍后再试" in page_text_check:
                                                            print("[登录] ⚠ 检测到错误提示：出了点问题，等待重新发送验证码按钮出现...")
                                                            # 记录点击前的页面状态
                                                            try:
                                                                before_click_url = login_page.url
                                                                before_click_text = page_text_check[:300] if len(page_text_check) > 300 else page_text_check
                                                                print(f"[登录] 点击前 URL: {before_click_url}")
                                                                print(f"[登录] 点击前页面文本预览: {before_click_text}")
                                                            except:
                                                                pass
                                                            
                                                            resend_btn_selectors = [
                                                                "button[aria-label='重新发送验证码']",
                                                                "button:has-text('重新发送验证码')",
                                                                "button:has-text('重新发送')",
                                                                "button:has-text('Resend')",
                                                                "//button[contains(., '重新发送')]",
                                                                "//button[contains(., 'Resend')]",
                                                            ]
                                                            resend_clicked = False
                                                            clicked_selector = None
                                                            # 等待按钮出现（最多等待10秒）
                                                            max_wait_btn = 10
                                                            waited_btn = 0
                                                            while waited_btn < max_wait_btn and not resend_clicked:
                                                                for selector in resend_btn_selectors:
                                                                    try:
                                                                        if selector.startswith("//"):
                                                                            resend_btn = login_page.locator(selector).first
                                                                        else:
                                                                            resend_btn = login_page.locator(selector).first
                                                                        # 等待按钮可见且未禁用
                                                                        btn_count = resend_btn.count()
                                                                        if btn_count > 0:
                                                                            try:
                                                                                resend_btn.wait_for(state="visible", timeout=2000)
                                                                                
                                                                                # 在点击按钮前等待 reCAPTCHA 准备好
                                                                                if wait_for_recaptcha_ready(login_page, timeout=5):
                                                                                    print("[登录] reCAPTCHA 已准备好，准备点击按钮...")
                                                                                
                                                                                is_disabled = resend_btn.is_disabled()
                                                                                if not is_disabled:
                                                                                    # 记录点击前的按钮状态
                                                                                    try:
                                                                                        btn_text = resend_btn.text_content() or ""
                                                                                        print(f"[登录] 找到按钮 (选择器: {selector}): 文本='{btn_text}', 禁用={is_disabled}")
                                                                                    except:
                                                                                        print(f"[登录] 找到按钮 (选择器: {selector}): 禁用={is_disabled}")
                                                                                    
                                                                                    # 模拟真实用户行为：先移动鼠标到按钮位置
                                                                                    try:
                                                                                        box = resend_btn.bounding_box()
                                                                                        if box:
                                                                                            # 随机移动到按钮附近，模拟真实鼠标移动
                                                                                            login_page.mouse.move(
                                                                                                box['x'] + box['width'] / 2 + random.uniform(-5, 5),
                                                                                                box['y'] + box['height'] / 2 + random.uniform(-5, 5)
                                                                                            )
                                                                                            login_page.wait_for_timeout(random.randint(100, 300))  # 随机延迟100-300ms
                                                                                    except:
                                                                                        pass
                                                                                    
                                                                                    # 点击按钮（使用更真实的点击方式，带随机延迟）
                                                                                    resend_btn.click(delay=random.randint(50, 150))  # 随机延迟50-150ms
                                                                                    clicked_selector = selector
                                                                                    print(f"[登录] ✓ 已点击重新发送验证码按钮 (选择器: {selector})")
                                                                                    
                                                                                    # 点击后等待 reCAPTCHA 验证完成
                                                                                    wait_for_recaptcha_complete(login_page, timeout=30)
                                                                                    
                                                                                    # 等待页面响应（增加等待时间）
                                                                                    login_page.wait_for_timeout(2000)  # 等待2秒让页面响应
                                                                                    
                                                                                    # 检查点击后的页面状态，并立即尝试检测成功提示
                                                                                    try:
                                                                                        after_click_url = login_page.url
                                                                                        after_click_text = login_page.locator("body").text_content() or ""
                                                                                        after_click_preview = after_click_text[:500] if len(after_click_text) > 500 else after_click_text
                                                                                        print(f"[登录] 点击后 URL: {after_click_url}")
                                                                                        print(f"[登录] 点击后页面文本预览: {after_click_preview}")
                                                                                        
                                                                                        # 检查是否有新的错误提示
                                                                                        if "出了点问题" in after_click_text:
                                                                                            print("[登录] ⚠ 点击后仍然显示错误提示：出了点问题")
                                                                                        if "验证码已发送" in after_click_text or "请查收您的邮件" in after_click_text:
                                                                                            print("[登录] ✓ 点击后检测到成功提示（页面文本）")
                                                                                        
                                                                                        # 立即检查页面是否有 aside 元素（在等待前检查，避免元素消失）
                                                                                        aside_check = login_page.locator("aside.zyTWof-Ng57nc").count()
                                                                                        div_check = login_page.locator("div.zyTWof-gIZMF").count()
                                                                                        print(f"[登录] 点击后立即元素检查: aside.zyTWof-Ng57nc={aside_check}, div.zyTWof-gIZMF={div_check}")
                                                                                        
                                                                                        # 如果元素存在，立即尝试检测成功提示（避免等待后元素消失）
                                                                                        if aside_check > 0:
                                                                                            try:
                                                                                                sent_elements = login_page.locator("aside.zyTWof-Ng57nc div.zyTWof-gIZMF").all()
                                                                                                if sent_elements:
                                                                                                    found_error = False
                                                                                                    for sent_element in sent_elements:
                                                                                                        try:
                                                                                                            text = sent_element.text_content() or ""
                                                                                                            print(f"[登录] 点击后立即检测 - 元素文本: {text[:100]}")
                                                                                                            # 检查是否是成功提示
                                                                                                            if "验证码已发送" in text and "请查收您的邮件" in text:
                                                                                                                email_sent_confirmed = True
                                                                                                                print(f"[登录] ✓ 点击后立即通过方式1检测到成功提示: {text}")
                                                                                                                break
                                                                                                            # 检查是否是错误提示
                                                                                                            elif "出了点问题" in text or ("错误" in text and "请稍后再试" in text):
                                                                                                                found_error = True
                                                                                                                print(f"[登录] ⚠ 点击后检测到错误提示: {text[:100]}")
                                                                                                                print(f"[登录] ℹ 错误提示框可能会自动消失，继续等待成功提示...")
                                                                                                        except:
                                                                                                            continue
                                                                                                    if email_sent_confirmed:
                                                                                                        print(f"[登录] ✓ 点击后立即检测到成功提示，将跳出检测循环")
                                                                                                        break
                                                                                                    # 如果只找到错误提示，继续等待（错误提示框可能会消失，然后出现成功提示）
                                                                                                    if found_error and not email_sent_confirmed:
                                                                                                        print(f"[登录] ℹ 检测到错误提示但未检测到成功提示，将等待更长时间（10秒）让成功提示出现...")
                                                                                                        login_page.wait_for_timeout(10000)  # 等待10秒，让错误提示消失，成功提示出现
                                                                                            except Exception as e:
                                                                                                print(f"[登录] ⚠ 点击后立即检测元素时出错: {e}")
                                                                                    except Exception as e:
                                                                                        print(f"[登录] ⚠ 检查点击后页面状态时出错: {e}")
                                                                                    
                                                                                    resend_clicked = True
                                                                                    resend_already_clicked = True  # 标记已经点击过
                                                                                    # 如果已经检测到成功提示，跳出按钮查找循环
                                                                                    if email_sent_confirmed:
                                                                                        break
                                                                                    break
                                                                                else:
                                                                                    if waited_btn == 0:
                                                                                        print(f"[登录] 按钮 (选择器: {selector}) 存在但被禁用")
                                                                            except Exception as e:
                                                                                if waited_btn == 0:
                                                                                    print(f"[登录] 等待按钮可见时出错 (选择器: {selector}): {str(e)[:100]}")
                                                                        else:
                                                                            if waited_btn == 0 and selector == resend_btn_selectors[0]:
                                                                                print(f"[登录] 按钮 (选择器: {selector}) 未找到")
                                                                    except Exception as e:
                                                                        if waited_btn == 0:
                                                                            print(f"[登录] 检查按钮时出错 (选择器: {selector}): {str(e)[:100]}")
                                                                        continue
                                                                if not resend_clicked:
                                                                    login_page.wait_for_timeout(2000)
                                                                    waited_btn += 2
                                                            if not resend_clicked:
                                                                print("[登录] ⚠ 等待10秒后仍未找到或无法点击重新发送验证码按钮")
                                                                # 输出当前页面状态用于调试
                                                                try:
                                                                    debug_url = login_page.url
                                                                    debug_text = login_page.locator("body").text_content() or ""
                                                                    debug_preview = debug_text[:300] if len(debug_text) > 300 else debug_text
                                                                    print(f"[登录] 调试信息 - 当前 URL: {debug_url}")
                                                                    print(f"[登录] 调试信息 - 页面文本预览: {debug_preview}")
                                                                except:
                                                                    pass
                                                            else:
                                                                # 点击成功，如果还没有检测到成功提示，再等待一下让提示框出现
                                                                if not email_sent_confirmed:
                                                                    print("[登录] 等待提示框出现（额外等待3秒）...")
                                                                    login_page.wait_for_timeout(3000)
                                                                else:
                                                                    print("[登录] ✓ 已在点击后立即检测到成功提示，跳过额外等待")
                                                    except Exception as e:
                                                        print(f"[登录] ⚠ 处理错误提示时出错: {e}")
                                                        import traceback
                                                        traceback.print_exc()
                                                    
                                                    # 方式1：检测提示框 <aside> 元素（最准确）
                                                    # 提示框结构：<aside class="zyTWof-Ng57nc..."> -> <div class="zyTWof-YAxtVc"> -> <div class="zyTWof-gIZMF" id="c6">
                                                    # 注意：在无头模式下，使用 attached 状态更可靠，因为元素可能在 DOM 中但视觉上不可见
                                                    try:
                                                        # 先检测 aside 元素是否已附加到 DOM（无头模式下更可靠）
                                                        # 增加超时时间和重试逻辑，因为无头模式下可能需要更多时间
                                                        aside_element = login_page.locator("aside.zyTWof-Ng57nc")
                                                        # 在无头模式下，先等待一下，让元素有时间加载
                                                        if waited_sent == 0:
                                                            login_page.wait_for_timeout(3000)  # 初始等待3秒
                                                        
                                                        # 记录检测前的页面状态（仅在第一次或每10秒）
                                                        if waited_sent == 0 or waited_sent % 10 == 0:
                                                            try:
                                                                current_url_debug = login_page.url
                                                                page_text_debug = login_page.locator("body").text_content() or ""
                                                                page_text_preview = page_text_debug[:200] if len(page_text_debug) > 200 else page_text_debug
                                                                print(f"[登录] 方式1检测前 - URL: {current_url_debug}")
                                                                print(f"[登录] 方式1检测前 - 页面文本预览: {page_text_preview}")
                                                            except:
                                                                pass
                                                        
                                                        # 尝试等待元素出现（最多等待5秒）
                                                        try:
                                                            aside_element.wait_for(state="attached", timeout=5000)
                                                        except Exception as wait_e:
                                                            # 如果等待超时，继续尝试检查元素是否存在
                                                            if waited_sent == 0 or waited_sent % 10 == 0:
                                                                print(f"[登录] 方式1: 等待 aside 元素附加超时: {str(wait_e)[:100]}")
                                                        
                                                        # 检查元素是否存在
                                                        aside_count = aside_element.count()
                                                        if aside_count > 0:
                                                            if waited_sent == 0 or waited_sent % 10 == 0:
                                                                print(f"[登录] 方式1: 找到 aside 元素（count={aside_count}）")
                                                            # 元素存在，检测内部的提示文本元素（可能有多个，需要找到正确的）
                                                            # 尝试直接查找包含文本的元素
                                                            sent_elements = login_page.locator("aside.zyTWof-Ng57nc div.zyTWof-gIZMF").all()
                                                            if sent_elements:
                                                                if waited_sent == 0 or waited_sent % 10 == 0:
                                                                    print(f"[登录] 方式1: 找到 {len(sent_elements)} 个 div.zyTWof-gIZMF 元素")
                                                                for sent_element in sent_elements:
                                                                    try:
                                                                        # 等待元素可见或附加
                                                                        try:
                                                                            sent_element.wait_for(state="attached", timeout=2000)
                                                                        except:
                                                                            pass
                                                                        text = sent_element.text_content() or ""
                                                                        if waited_sent == 0 or waited_sent % 10 == 0:
                                                                            print(f"[登录] 方式1: 检查元素文本: {text[:100]}")
                                                                        if "验证码已发送" in text and "请查收您的邮件" in text:
                                                                            email_sent_confirmed = True
                                                                            print(f"[登录] ✓ 通过方式1（提示框 aside）检测到提示: {text}")
                                                                            break
                                                                    except Exception as elem_e:
                                                                        if waited_sent == 0 or waited_sent % 10 == 0:
                                                                            print(f"[登录] 方式1: 检查元素时出错: {str(elem_e)[:100]}")
                                                                        continue
                                                                if email_sent_confirmed:
                                                                    break
                                                            else:
                                                                if waited_sent == 0 or waited_sent % 10 == 0:
                                                                    print(f"[登录] 方式1: aside 元素存在（count={aside_count}），但内部 div.zyTWof-gIZMF 未找到")
                                                                    # 尝试查找 aside 内的所有元素
                                                                    try:
                                                                        all_children = login_page.locator("aside.zyTWof-Ng57nc *").all()
                                                                        print(f"[登录] 方式1: aside 内共有 {len(all_children)} 个子元素")
                                                                        for i, child in enumerate(all_children[:5]):  # 只显示前5个
                                                                            try:
                                                                                child_tag = child.evaluate("el => el.tagName")
                                                                                child_class = child.evaluate("el => el.className") or ""
                                                                                print(f"[登录] 方式1: 子元素 {i+1}: {child_tag}, class={child_class[:50]}")
                                                                            except:
                                                                                pass
                                                                    except:
                                                                        pass
                                                        else:
                                                            if waited_sent == 0 or waited_sent % 10 == 0:
                                                                print(f"[登录] 方式1: aside.zyTWof-Ng57nc 元素未找到（可能还未加载，已等待 {waited_sent} 秒）")
                                                                # 检查是否有其他 aside 元素
                                                                try:
                                                                    all_asides = login_page.locator("aside").all()
                                                                    print(f"[登录] 方式1: 页面中共有 {len(all_asides)} 个 aside 元素")
                                                                    for i, aside in enumerate(all_asides[:3]):  # 只显示前3个
                                                                        try:
                                                                            aside_class = aside.evaluate("el => el.className") or ""
                                                                            print(f"[登录] 方式1: aside {i+1} class: {aside_class[:100]}")
                                                                        except:
                                                                            pass
                                                                except:
                                                                    pass
                                                    except Exception as e:
                                                        # 只在第一次或每10秒打印一次，避免日志过多
                                                        if waited_sent == 0 or waited_sent % 10 == 0:
                                                            print(f"[登录] 方式1（提示框 aside）检测失败: {str(e)[:100]}")
                                                            import traceback
                                                            traceback.print_exc()
                                                    
                                                    # 方式2：检测 div.zyTWof-gIZMF 元素（备用）
                                                    # 在无头模式下，直接检测文本元素更可靠
                                                    if not email_sent_confirmed:
                                                        try:
                                                            sent_element = login_page.locator("div.zyTWof-gIZMF")
                                                            # 先检查元素是否存在
                                                            element_count = sent_element.count()
                                                            if element_count > 0:
                                                                # 使用 attached 状态，因为无头模式下元素可能在 DOM 中
                                                                # 增加超时时间
                                                                try:
                                                                    sent_element.wait_for(state="attached", timeout=5000)
                                                                except:
                                                                    pass
                                                                # 获取所有匹配的元素，检查每个元素的文本
                                                                all_elements = sent_element.all()
                                                                for elem in all_elements:
                                                                    try:
                                                                        text = elem.text_content() or ""
                                                                        if "验证码已发送" in text and "请查收您的邮件" in text:
                                                                            email_sent_confirmed = True
                                                                            print(f"[登录] ✓ 通过方式2（div.zyTWof-gIZMF）检测到提示: {text}")
                                                                            break
                                                                    except:
                                                                        continue
                                                                if email_sent_confirmed:
                                                                    break
                                                            else:
                                                                if waited_sent == 0 or waited_sent % 10 == 0:
                                                                    print(f"[登录] 方式2: div.zyTWof-gIZMF 元素未找到（可能还未加载，已等待 {waited_sent} 秒）")
                                                        except Exception as e:
                                                            if waited_sent == 0 or waited_sent % 10 == 0:
                                                                print(f"[登录] 方式2（div.zyTWof-gIZMF）检测失败: {str(e)[:100]}")
                                                    
                                                    # 方式3：检测动态 ID（c1-c20，因为 ID 可能会变化）
                                                    # 在无头模式下，通过文本内容匹配更可靠
                                                    # 注意：需要检查所有可能的ID，因为提示框的ID可能不是固定的
                                                    if not email_sent_confirmed:
                                                        try:
                                                            # 尝试匹配所有可能的 ID 模式（c1-c20）
                                                            # 优先检查常见的 c6-c10，然后检查其他ID
                                                            id_range = list(range(6, 11)) + list(range(1, 6)) + list(range(11, 21))
                                                            found_elements = []
                                                            for id_num in id_range:
                                                                try:
                                                                    sent_element = login_page.locator(f"#c{id_num}")
                                                                    # 使用 count() 检查元素是否存在，比 wait_for 更快
                                                                    element_count = sent_element.count()
                                                                    if element_count > 0:  # 元素存在
                                                                        # 尝试等待元素附加
                                                                        try:
                                                                            sent_element.wait_for(state="attached", timeout=2000)
                                                                        except:
                                                                            pass
                                                                        text = sent_element.text_content() or ""
                                                                        # 检查是否包含我们需要的文本
                                                                        if "验证码已发送" in text and "请查收您的邮件" in text:
                                                                            email_sent_confirmed = True
                                                                            print(f"[登录] ✓ 通过方式3（ID #c{id_num}）检测到提示: {text}")
                                                                            break
                                                                        # 记录找到的元素（用于调试），但排除错误提示
                                                                        elif "出了点问题" not in text and "错误" not in text:
                                                                            found_elements.append(f"#c{id_num}: {text[:50]}")
                                                                except:
                                                                    continue
                                                            if email_sent_confirmed:
                                                                break
                                                            # 如果找到元素但文本不匹配，打印调试信息（排除错误提示）
                                                            if found_elements and (waited_sent == 0 or waited_sent % 10 == 0):
                                                                print(f"[登录] 方式3: 找到元素但文本不匹配: {found_elements[:3]}")
                                                        except Exception as e:
                                                            if waited_sent == 0 or waited_sent % 10 == 0:
                                                                print(f"[登录] 方式3（动态 ID）检测失败: {str(e)[:100]}")
                                                    
                                                    # 方式4已移除：页面文本检测不准确，只使用方式1-3（元素检测）
                                                    
                                                except Exception as e:
                                                    if waited_sent % 10 == 0:
                                                        print(f"[登录] 检测过程出错: {str(e)[:100]}")
                                                
                                                login_page.wait_for_timeout(2000)
                                                waited_sent += 2
                                                
                                                # 每 10 秒打印一次等待状态（便于调试）
                                                if waited_sent % 10 == 0 and waited_sent > 0:
                                                    print(f"[登录] 仍在等待验证码邮件发送成功的提示框... (已等待 {waited_sent} 秒)")
                                                    # 打印当前页面状态
                                                    try:
                                                        current_url = login_page.url
                                                        print(f"[登录] 当前 URL: {current_url}")
                                                    except:
                                                        pass
                                            
                                            if email_sent_confirmed:
                                                print("[登录] ✓ 检测到验证码邮件发送成功的提示，开始获取验证码...")
                                                # 为了确保邮件发送成功，在确认验证码已发送成功后，再点击一次"重新发送验证码"按钮
                                                if resend_already_clicked:
                                                    print("[登录] ℹ 已在检测循环中点击过重新发送验证码按钮，跳过重复点击")
                                                else:
                                                    # 再点击一次"重新发送验证码"按钮，确保邮件发送成功
                                                    print("[登录] 为了确保邮件发送成功，再点击一次重新发送验证码按钮...")
                                                    try:
                                                        resend_btn_selectors_ensure = [
                                                            "button[aria-label='重新发送验证码']",
                                                            "button:has-text('重新发送验证码')",
                                                            "button:has-text('重新发送')",
                                                            "button:has-text('Resend')",
                                                        ]
                                                        resend_clicked_ensure = False
                                                        for selector_ensure in resend_btn_selectors_ensure:
                                                            try:
                                                                resend_btn_ensure = login_page.locator(selector_ensure).first
                                                                if resend_btn_ensure.count() > 0:
                                                                    resend_btn_ensure.wait_for(state="visible", timeout=5000)
                                                                    if not resend_btn_ensure.is_disabled():
                                                                        # 在点击前等待 reCAPTCHA 准备好
                                                                        if wait_for_recaptcha_ready(login_page, timeout=5):
                                                                            print("[登录] reCAPTCHA 已准备好，准备点击重新发送验证码按钮...")
                                                                        
                                                                        # 模拟真实用户行为：先移动鼠标到按钮位置
                                                                        try:
                                                                            box = resend_btn_ensure.bounding_box()
                                                                            if box:
                                                                                login_page.mouse.move(
                                                                                    box['x'] + box['width'] / 2 + random.uniform(-5, 5),
                                                                                    box['y'] + box['height'] / 2 + random.uniform(-5, 5)
                                                                                )
                                                                                login_page.wait_for_timeout(random.randint(100, 300))
                                                                        except:
                                                                            pass
                                                                        
                                                                        # 记录点击前的页面状态
                                                                        try:
                                                                            before_click_url_ensure = login_page.url
                                                                            before_click_text_ensure = login_page.locator("body").text_content() or ""
                                                                            before_click_preview_ensure = before_click_text_ensure[:500] if len(before_click_text_ensure) > 500 else before_click_text_ensure
                                                                            print(f"[登录] 点击前 URL: {before_click_url_ensure}")
                                                                            print(f"[登录] 点击前页面文本预览: {before_click_preview_ensure}")
                                                                        except:
                                                                            pass
                                                                        
                                                                        # 点击按钮
                                                                        resend_btn_ensure.click(delay=random.randint(50, 150))
                                                                        print(f"[登录] ✓ 已点击重新发送验证码按钮（确保邮件发送成功）")
                                                                        
                                                                        # 点击后等待 reCAPTCHA 验证完成
                                                                        wait_for_recaptcha_complete(login_page, timeout=30)
                                                                        
                                                                        # 等待一下让页面响应
                                                                        login_page.wait_for_timeout(2000)
                                                                        
                                                                        # 点击后检查页面是否有错误提示
                                                                        try:
                                                                            after_click_url_ensure = login_page.url
                                                                            print(f"[登录] 点击后 URL: {after_click_url_ensure}")
                                                                            
                                                                            # 先检查特定的错误提示元素（aside.zyTWof-Ng57nc 中的错误提示）
                                                                            error_detected = False
                                                                            error_text_found = ""
                                                                            
                                                                            try:
                                                                                # 检查 aside 元素中的错误提示
                                                                                aside_elements = login_page.locator("aside.zyTWof-Ng57nc").all()
                                                                                for aside_elem in aside_elements:
                                                                                    try:
                                                                                        # 检查 aside 是否可见
                                                                                        if aside_elem.is_visible():
                                                                                            aside_text = aside_elem.text_content() or ""
                                                                                            # 检查是否包含错误提示
                                                                                            if "出了点问题" in aside_text and "请稍后再试" in aside_text:
                                                                                                error_detected = True
                                                                                                error_text_found = aside_text[:200]
                                                                                                print(f"[登录] ⚠ 检测到错误提示元素（aside）: {error_text_found}")
                                                                                                
                                                                                                break
                                                                                    except:
                                                                                        continue
                                                                            except:
                                                                                pass
                                                                            
                                                                            # 如果没有在 aside 中找到，检查 div.zyTWof-gIZMF 中的错误提示
                                                                            if not error_detected:
                                                                                try:
                                                                                    error_divs = login_page.locator("div.zyTWof-gIZMF").all()
                                                                                    for error_div in error_divs:
                                                                                        try:
                                                                                            if error_div.is_visible():
                                                                                                div_text = error_div.text_content() or ""
                                                                                                # 检查是否包含错误提示
                                                                                                if "出了点问题" in div_text and "请稍后再试" in div_text:
                                                                                                    error_detected = True
                                                                                                    error_text_found = div_text[:200]
                                                                                                    print(f"[登录] ⚠ 检测到错误提示元素（div）: {error_text_found}")
                                                                                                    
                                                                                                    break
                                                                                        except:
                                                                                            continue
                                                                                except:
                                                                                    pass
                                                                            
                                                                            # 检查是否有成功提示（aside 或 div 中的成功提示）
                                                                            success_detected = False
                                                                            success_text_found = ""
                                                                            
                                                                            try:
                                                                                # 检查 aside 元素中的成功提示
                                                                                aside_elements = login_page.locator("aside.zyTWof-Ng57nc").all()
                                                                                for aside_elem in aside_elements:
                                                                                    try:
                                                                                        if aside_elem.is_visible():
                                                                                            aside_text = aside_elem.text_content() or ""
                                                                                            # 检查是否包含成功提示
                                                                                            if "验证码已发送" in aside_text and "请查收您的邮件" in aside_text:
                                                                                                success_detected = True
                                                                                                success_text_found = aside_text[:200]
                                                                                                print(f"[登录] ✓ 检测到成功提示元素（aside）: {success_text_found}")
                                                                                                break
                                                                                    except:
                                                                                        continue
                                                                            except:
                                                                                pass
                                                                            
                                                                            if not success_detected:
                                                                                try:
                                                                                    success_divs = login_page.locator("div.zyTWof-gIZMF").all()
                                                                                    for success_div in success_divs:
                                                                                        try:
                                                                                            if success_div.is_visible():
                                                                                                div_text = success_div.text_content() or ""
                                                                                                # 检查是否包含成功提示
                                                                                                if "验证码已发送" in div_text and "请查收您的邮件" in div_text:
                                                                                                    success_detected = True
                                                                                                    success_text_found = div_text[:200]
                                                                                                    print(f"[登录] ✓ 检测到成功提示元素（div）: {success_text_found}")
                                                                                                    break
                                                                                        except:
                                                                                            continue
                                                                                except:
                                                                                    pass
                                                                            
                                                                            # 输出检测结果
                                                                            if error_detected:
                                                                                if success_detected:
                                                                                    print(f"[登录] ⚠ 同时也检测到成功提示，页面状态可能不稳定，继续等待确认...")
                                                                            elif success_detected:
                                                                                print(f"[登录] ✓ 检测到成功提示，验证码邮件应该已发送成功")
                                                                            else:
                                                                                print(f"[登录] ℹ 未检测到明显的错误或成功提示元素，页面状态正常")
                                                                        except Exception as check_e:
                                                                            print(f"[登录] ⚠ 检查页面状态时出错: {check_e}")
                                                                        
                                                                        resend_clicked_ensure = True
                                                                        resend_already_clicked = True  # 标记已点击
                                                                        break
                                                            except:
                                                                continue
                                                        
                                                        if not resend_clicked_ensure:
                                                            print("[登录] ⚠ 未找到或无法点击重新发送验证码按钮")
                                                            print("[登录] ℹ 可能的原因：")
                                                            print("  1. 按钮尚未出现（需要等待更长时间）")
                                                            print("  2. 按钮被禁用（需要等待冷却时间）")
                                                            print("  3. 页面结构发生变化（选择器不匹配）")
                                                            print("[登录] ℹ 继续尝试获取验证码（可能邮件已发送，只是页面显示错误）...")
                                                    except Exception as e:
                                                        print(f"[登录] ⚠ 点击重新发送验证码按钮时出错: {e}，继续获取验证码...")
                                            else:
                                                print(f"[登录] ✗ 等待超时（{max_wait_sent}秒），未检测到验证码邮件发送成功的提示（方式1-3均失败）")
                                                print("[登录] ✗ 验证码可能未发送成功，无法继续")
                                                print("[登录] ℹ 提示：系统只使用元素检测方式（方式1-3），不使用页面文本检测")
                                                return False
                                            
                                            login_page.wait_for_timeout(2000)  # 额外等待 2 秒，确保邮件已到达
                                        else:
                                            print(f"[登录] ⚠ 警告：redirect_success=True 但 URL 不匹配验证码页面")
                                            print(f"[登录] 当前 URL: {final_url_verify}")
                                            print(f"[登录] 继续尝试等待跳转...")
                                            # 继续等待跳转
                                            login_page.wait_for_timeout(5000)
                                    else:
                                        # 如果未成功跳转，返回错误
                                        print("[登录] ✗ 未能跳转到验证码页面，无法继续")
                                        return False
                                else:
                                    # 调试日志已关闭
                                    # print("[登录] ⚠ 未找到继续按钮，尝试按 Enter...")
                                    email_input.press("Enter")
                                    login_page.wait_for_timeout(5000)
                            except Exception as e:
                                # 调试日志已关闭
                                # print(f"[登录] ⚠ 点击继续按钮时出错: {e}，尝试按 Enter...")
                                email_input.press("Enter")
                                login_page.wait_for_timeout(5000)
                        else:
                            print(f"[单个账号刷新] ✗ 账号 {account_idx} 未找到邮箱输入框")
                            return False
                    except Exception as e:
                        print(f"[单个账号刷新] ✗ 账号 {account_idx} 填写邮箱时出错: {e}")
                        import traceback
                        traceback.print_exc()
                        return False
                    
                    # 确认已跳转到验证码页面后，才开始等待验证码邮件
                    # 检查当前 URL，确保在验证码页面
                    final_url_check = login_page.url
                    print(f"[登录] 最终 URL 检查: {final_url_check}")
                    if ("accountverification" not in final_url_check 
                        or "verify-oob-code" not in final_url_check):
                        # 如果不在验证码页面，尝试再次等待
                        print(f"[登录] ⚠ 未在验证码页面，当前 URL: {final_url_check}")
                        print("[登录] 等待跳转到验证码页面...")
                        try:
                            login_page.wait_for_url("**/accountverification.business.gemini.google/v1/verify-oob-code**", timeout=10000)
                            print(f"[登录] ✓ 已跳转到验证码页面: {login_page.url}")
                        except:
                            print(f"[登录] ✗ 未能跳转到验证码页面，当前 URL: {login_page.url}")
                            return False
                    else:
                        print(f"[登录] ✓ 确认在验证码页面: {final_url_check}")
                    
                    # 步骤3和4：获取验证码并登录（支持验证码错误时自动重试）
                    # 根据 mode 参数决定使用哪种方式获取验证码
                    if mode == "api":
                        print(f"[登录] 使用 API 方式获取验证码（强制模式）")
                        force_api_mode = True
                        force_browser_mode = False
                    elif mode == "browser":
                        print(f"[登录] 使用浏览器方式获取验证码（强制模式）")
                        force_api_mode = False
                        force_browser_mode = True
                    else:  # mode == "auto"
                        print(f"[登录] 自动选择获取验证码方式（优先 API，失败后回退到浏览器）")
                        force_api_mode = False
                        force_browser_mode = False
                    
                    # 调试日志已关闭
                    # print(f"\n[账号 {account_idx}] 步骤3: 等待验证码邮件并获取验证码...")
                    max_retry = 3
                    retry_count = 0
                    success = False
                    use_browser_mode = force_browser_mode  # 如果强制使用浏览器方式，直接设置
                    limit_exceeded_detected = False  # 标志：是否检测到"验证码输入次数已超出上限"
                    
                    while retry_count < max_retry and not success:
                        if retry_count > 0:
                            # 调试日志已关闭
                            # print(f"\n[账号 {account_idx}] 重试: 验证码错误，刷新邮件并重新获取验证码 (重试 {retry_count}/{max_retry-1})...")
                            # 如果强制使用浏览器方式，或者检测到"验证码输入次数已超出上限"，或者重试次数超过1次，强制使用浏览器方式
                            if force_browser_mode or limit_exceeded_detected or (not force_api_mode and retry_count >= 2):
                                if not use_browser_mode:
                                    print(f"[临时邮箱] ⚠ API 方式获取验证码多次失败，切换到浏览器方式从临时邮箱获取验证码...")
                                    use_browser_mode = True
                            
                            if use_browser_mode:
                                # 强制使用浏览器方式
                                code = get_verification_code_from_tempmail_browser(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=True)
                            elif force_api_mode:
                                # 强制使用 API 方式（即使失败也不切换到浏览器方式）
                                code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=True, account_config=account, force_api=True)
                            else:
                                # 自动模式：重试模式，立即刷新并提取，不等待
                                code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=True, account_config=account, force_api=False)
                        else:
                            # 第一次：等待邮件到达
                            if force_browser_mode:
                                # 强制使用浏览器方式
                                code = get_verification_code_from_tempmail_browser(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=False)
                            elif force_api_mode:
                                # 强制使用 API 方式
                                code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=False, account_config=account, force_api=True)
                            else:
                                # 自动模式：优先尝试 API 方式
                                code = get_verification_code_from_tempmail(email_page, timeout=120, tempmail_url=tempmail_url, retry_mode=False, account_config=account, force_api=False)
                        
                        if not code:
                            if retry_count > 0:
                                print(f"[单个账号刷新] ✗ 账号 {account_idx} 重试时无法获取验证码")
                            else:
                                print(f"[单个账号刷新] ✗ 账号 {account_idx} 无法获取验证码")
                            
                            # 如果 API 方式获取验证码失败，且还没有使用浏览器方式，尝试重新执行整个登录流程
                            if not use_browser_mode and retry_count < max_retry - 1:
                                print(f"[登录] ⚠ 获取验证码失败，将重新执行整个登录流程（重新输入邮箱并发送验证码）...")
                                # 重新导航到登录页面
                                try:
                                    login_page.goto(GEMINI_LOGIN_URL, wait_until="networkidle", timeout=60000)
                                    login_page.wait_for_timeout(3000)
                                    
                                    # 重新输入邮箱
                                    email_input = login_page.locator(
                                        "#email-input, input[aria-label='邮箱'], input[type='text'][name='loginHint']"
                                    ).first
                                    if email_input.is_visible():
                                        email_input.fill(email)
                                        login_page.wait_for_timeout(2000)
                                        
                                        # 重新点击继续按钮
                                        continue_btn = login_page.locator(
                                            "button#log-in-button, button:has-text('Continue'), button:has-text('继续')"
                                        ).first
                                        if continue_btn.is_visible():
                                            # 在点击前等待 reCAPTCHA 准备好
                                            if wait_for_recaptcha_ready(login_page, timeout=5):
                                                print("[登录] reCAPTCHA 已准备好，准备点击按钮...")
                                            
                                            continue_btn.click()
                                            
                                            # 点击后等待 reCAPTCHA 验证完成
                                            wait_for_recaptcha_complete(login_page, timeout=30)
                                            
                                            # 等待页面跳转到验证码页面
                                            login_page.wait_for_timeout(5000)
                                            
                                            # 等待跳转到验证码页面（最多等待30秒）
                                            max_wait_redirect_retry = 30
                                            waited_redirect_retry = 0
                                            redirect_success_retry = False
                                            
                                            while waited_redirect_retry < max_wait_redirect_retry:
                                                current_url_retry = login_page.url
                                                if ("accountverification" in current_url_retry 
                                                    and "verify-oob-code" in current_url_retry):
                                                    redirect_success_retry = True
                                                    print(f"[登录] ✓ 已重新跳转到验证码页面: {current_url_retry}")
                                                    break
                                                login_page.wait_for_timeout(2000)
                                                waited_redirect_retry += 2
                                            
                                            if redirect_success_retry:
                                                # 等待"重新发送验证码"按钮出现
                                                print("[登录] 等待重新发送验证码按钮出现...")
                                                resend_btn_selectors_retry = [
                                                    "button[aria-label='重新发送验证码']",
                                                    "button:has-text('重新发送验证码')",
                                                    "button:has-text('重新发送')",
                                                    "button:has-text('Resend')",
                                                ]
                                                resend_btn_found_retry = False
                                                max_wait_resend_btn_retry = 30
                                                waited_resend_btn_retry = 0
                                                
                                                while waited_resend_btn_retry < max_wait_resend_btn_retry and not resend_btn_found_retry:
                                                    for selector_retry in resend_btn_selectors_retry:
                                                        try:
                                                            resend_btn_retry = login_page.locator(selector_retry).first
                                                            if resend_btn_retry.count() > 0:
                                                                resend_btn_retry.wait_for(state="visible", timeout=2000)
                                                                resend_btn_found_retry = True
                                                                print(f"[登录] ✓ 重新发送验证码按钮已出现")
                                                                break
                                                        except:
                                                            continue
                                                    if not resend_btn_found_retry:
                                                        login_page.wait_for_timeout(2000)
                                                        waited_resend_btn_retry += 2
                                                
                                                # 等待一下让验证码邮件发送
                                                login_page.wait_for_timeout(3000)
                                                
                                                # 继续重试获取验证码
                                                retry_count += 1
                                                continue
                                            else:
                                                print(f"[登录] ✗ 重新执行登录流程后未能跳转到验证码页面")
                                                break
                                        else:
                                            print(f"[登录] ✗ 重新执行登录流程时未找到继续按钮")
                                            break
                                    else:
                                        print(f"[登录] ✗ 重新执行登录流程时未找到邮箱输入框")
                                        break
                                except Exception as e:
                                    print(f"[登录] ✗ 重新执行登录流程时出错: {e}")
                                    break
                            else:
                                break
                        
                        # 调试日志已关闭
                        # print(f"\n[账号 {account_idx}] 步骤4: 填写验证码并登录...")
                        result = login_with_email_and_code(login_page, email, code)
                        
                        if result == "LIMIT_EXCEEDED":
                            # 检测到"验证码输入次数已超出上限"，点击"重新发送验证码"按钮，然后重新获取验证码（先用API，不行再用浏览器方式）
                            limit_exceeded_detected = True
                            print(f"[登录] ⚠ 检测到验证码输入次数已超出上限，点击重新发送验证码按钮，然后重新获取验证码...")
                            
                            # 步骤1：点击"重新发送验证码"按钮
                            try:
                                print(f"[登录] 正在尝试点击重新发送验证码按钮...")
                                resend_btn_selectors_limit = [
                                    "button[aria-label='重新发送验证码']",
                                    "button:has-text('重新发送验证码')",
                                    "button:has-text('重新发送')",
                                    "button:has-text('Resend')",
                                ]
                                resend_clicked_limit = False
                                for selector_limit in resend_btn_selectors_limit:
                                    try:
                                        resend_btn_limit = login_page.locator(selector_limit).first
                                        if resend_btn_limit.count() > 0:
                                            resend_btn_limit.wait_for(state="visible", timeout=5000)
                                            if not resend_btn_limit.is_disabled():
                                                # 在点击前等待 reCAPTCHA 准备好
                                                if wait_for_recaptcha_ready(login_page, timeout=5):
                                                    print("[登录] reCAPTCHA 已准备好，准备点击重新发送验证码按钮...")
                                                
                                                # 模拟真实用户行为：先移动鼠标到按钮位置
                                                try:
                                                    box = resend_btn_limit.bounding_box()
                                                    if box:
                                                        login_page.mouse.move(
                                                            box['x'] + box['width'] / 2 + random.uniform(-5, 5),
                                                            box['y'] + box['height'] / 2 + random.uniform(-5, 5)
                                                        )
                                                        login_page.wait_for_timeout(random.randint(100, 300))
                                                except:
                                                    pass
                                                
                                                # 记录点击前的页面状态
                                                try:
                                                    before_click_url_limit = login_page.url
                                                    before_click_text_limit = login_page.locator("body").text_content() or ""
                                                    before_click_preview_limit = before_click_text_limit[:500] if len(before_click_text_limit) > 500 else before_click_text_limit
                                                    print(f"[登录] 点击前 URL: {before_click_url_limit}")
                                                    print(f"[登录] 点击前页面文本预览: {before_click_preview_limit}")
                                                except:
                                                    pass
                                                
                                                resend_btn_limit.click(delay=random.randint(50, 150))
                                                print(f"[登录] ✓ 已点击重新发送验证码按钮")
                                                
                                                # 点击后等待 reCAPTCHA 验证完成
                                                wait_for_recaptcha_complete(login_page, timeout=30)
                                                
                                                # 等待一下让页面响应和邮件发送
                                                login_page.wait_for_timeout(3000)
                                                
                                                # 点击后检查页面是否有错误提示
                                                try:
                                                    after_click_url_limit = login_page.url
                                                    print(f"[登录] 点击后 URL: {after_click_url_limit}")
                                                    
                                                    # 先检查特定的错误提示元素（aside.zyTWof-Ng57nc 中的错误提示）
                                                    error_detected = False
                                                    error_text_found = ""
                                                    
                                                    try:
                                                        # 检查 aside 元素中的错误提示
                                                        aside_elements = login_page.locator("aside.zyTWof-Ng57nc").all()
                                                        for aside_elem in aside_elements:
                                                            try:
                                                                # 检查 aside 是否可见
                                                                if aside_elem.is_visible():
                                                                    aside_text = aside_elem.text_content() or ""
                                                                    # 检查是否包含错误提示
                                                                    if "出了点问题" in aside_text and "请稍后再试" in aside_text:
                                                                        error_detected = True
                                                                        error_text_found = aside_text[:200]
                                                                        print(f"[登录] ⚠ 检测到错误提示元素（aside）: {error_text_found}")
                                                                        
                                                                        break
                                                            except:
                                                                continue
                                                    except:
                                                        pass
                                                    
                                                    # 如果没有在 aside 中找到，检查 div.zyTWof-gIZMF 中的错误提示
                                                    if not error_detected:
                                                        try:
                                                            error_divs = login_page.locator("div.zyTWof-gIZMF").all()
                                                            for error_div in error_divs:
                                                                try:
                                                                    if error_div.is_visible():
                                                                        div_text = error_div.text_content() or ""
                                                                        # 检查是否包含错误提示
                                                                        if "出了点问题" in div_text and "请稍后再试" in div_text:
                                                                            error_detected = True
                                                                            error_text_found = div_text[:200]
                                                                            print(f"[登录] ⚠ 检测到错误提示元素（div）: {error_text_found}")
                                                                            
                                                                            break
                                                                except:
                                                                    continue
                                                        except:
                                                            pass
                                                    
                                                    # 检查是否有成功提示（aside 或 div 中的成功提示）
                                                    success_detected = False
                                                    success_text_found = ""
                                                    
                                                    try:
                                                        # 检查 aside 元素中的成功提示
                                                        aside_elements = login_page.locator("aside.zyTWof-Ng57nc").all()
                                                        for aside_elem in aside_elements:
                                                            try:
                                                                if aside_elem.is_visible():
                                                                    aside_text = aside_elem.text_content() or ""
                                                                    # 检查是否包含成功提示
                                                                    if "验证码已发送" in aside_text and "请查收您的邮件" in aside_text:
                                                                        success_detected = True
                                                                        success_text_found = aside_text[:200]
                                                                        print(f"[登录] ✓ 检测到成功提示元素（aside）: {success_text_found}")
                                                                        break
                                                            except:
                                                                continue
                                                    except:
                                                        pass
                                                    
                                                    if not success_detected:
                                                        try:
                                                            success_divs = login_page.locator("div.zyTWof-gIZMF").all()
                                                            for success_div in success_divs:
                                                                try:
                                                                    if success_div.is_visible():
                                                                        div_text = success_div.text_content() or ""
                                                                        # 检查是否包含成功提示
                                                                        if "验证码已发送" in div_text and "请查收您的邮件" in div_text:
                                                                            success_detected = True
                                                                            success_text_found = div_text[:200]
                                                                            print(f"[登录] ✓ 检测到成功提示元素（div）: {success_text_found}")
                                                                            break
                                                                except:
                                                                    continue
                                                        except:
                                                            pass
                                                    
                                                    # 输出检测结果
                                                    if error_detected:
                                                        if success_detected:
                                                            print(f"[登录] ⚠ 同时也检测到成功提示，页面状态可能不稳定")
                                                    elif success_detected:
                                                        print(f"[登录] ✓ 检测到成功提示，验证码邮件应该已发送成功")
                                                    else:
                                                        print(f"[登录] ℹ 未检测到明显的错误或成功提示元素，页面状态正常，继续获取验证码...")
                                                except Exception as check_e:
                                                    print(f"[登录] ⚠ 检查页面状态时出错: {check_e}")
                                                
                                                resend_clicked_limit = True
                                                break
                                    except:
                                        continue
                                
                                if not resend_clicked_limit:
                                    print(f"[登录] ⚠ 未找到或无法点击重新发送验证码按钮，继续重新获取验证码...")
                            except Exception as e:
                                print(f"[登录] ⚠ 点击重新发送验证码按钮时出错: {e}，继续重新获取验证码...")
                            
                            # 步骤2：重置验证码获取的状态（清除 last_max_id，让系统重新记录当前最大邮件ID）
                            try:
                                if 'tempmail_url' in locals() and tempmail_url and tempmail_url in _tempmail_client_cache:
                                    client = _tempmail_client_cache[tempmail_url]
                                    client.last_max_id = 0
                                    print(f"[登录] ✓ 已重置邮件ID缓存，系统将重新记录当前最大邮件ID")
                            except:
                                pass
                            
                            # 步骤3：继续重试获取验证码（先用API方式，不行再用浏览器方式）
                            # 不强制使用浏览器方式，让系统先尝试API方式
                            print(f"[登录] ✓ 已点击重新发送验证码按钮，将重新获取验证码（先用API方式，不行再用浏览器方式）")
                            retry_count += 1
                            continue
                        elif result == "CODE_ERROR":
                            # 调试日志已关闭
                            # print("\n[重试] 验证码错误，将刷新邮件并重新获取最新验证码...")
                            retry_count += 1
                            time.sleep(3)
                            continue
                        elif result is True:
                            success = True
                            break
                        else:
                            # 调试日志已关闭
                            # if not headless:
                            #     print(f"\n[单个账号刷新] ✗ 账号 {account_idx} 登录失败")
                            pass
                            break
                    
                    if not success:
                        print(f"[单个账号刷新] ✗ 账号 {account_idx} 登录失败（已重试 {retry_count} 次）")
                        return False
                    
                    # 步骤5：提取 Cookie 和 csesidx
                    # 调试日志已关闭
                    # if not headless:
                    #     print(f"\n[账号 {account_idx}] 步骤5: 提取 Cookie 和 csesidx...")
                    cookies_data = extract_cookies_and_csesidx(login_page)
                    
                    if not cookies_data:
                        print(f"[单个账号刷新] ✗ 账号 {account_idx} 无法提取 Cookie")
                        return False
                    
                    # 步骤6：验证 Cookie 是否有效
                    # 调试日志已关闭
                    # if not headless:
                    #     print(f"\n[账号 {account_idx}] 步骤6: 验证 Cookie 是否有效...")
                    is_valid = test_cookie_with_jwt(cookies_data)
                    
                    if is_valid:
                        print(f"[单个账号刷新] ✓ 账号 {account_idx} Cookie 验证成功")
                        # 保存到配置文件（更新现有账号）
                        save_to_config(cookies_data, account_index=account_idx, tempmail_name=tempmail_name)
                        
                        # 推送 WebSocket 更新事件，让前端及时刷新显示
                        try:
                            from app.websocket_manager import emit_account_update
                            from app.account_manager import account_manager
                            with account_manager.lock:
                                updated_account = account_manager.accounts[account_idx].copy()
                            emit_account_update(account_idx, updated_account)
                        except Exception as e:
                            # WebSocket 推送失败不影响刷新流程
                            pass
                        
                        print(f"[单个账号刷新] ✓ 账号 {account_idx} 刷新完成")
                        return True
                    else:
                        print(f"[单个账号刷新] ✗ 账号 {account_idx} Cookie 无效")
                        return False
                
                except Exception as e:
                    print(f"[单个账号刷新] ✗ 账号 {account_idx} 处理失败: {e}")
                    import traceback
                    traceback.print_exc()
                    return False
                finally:
                    email_page.close()
                    login_page.close()
            
            finally:
                browser.close()
    
    except Exception as e:
        print(f"[单个账号刷新] ✗ 发生错误: {e}")
        import traceback
        traceback.print_exc()
        return False

def refresh_expired_accounts(headless: bool = None):
    """批量刷新过期的 Cookie
    
    Args:
        headless: 是否使用无头模式。如果为 None，则自动检测（Linux 无图形界面时自动使用无头模式）
    """
    import os
    
    # 如果未指定 headless，自动检测
    if headless is None:
        if os.name != "nt":  # 非 Windows 系统
            # Linux/Unix: 检查是否有 DISPLAY 环境变量
            headless = not bool(os.environ.get("DISPLAY"))
        else:
            # Windows: 默认使用无头模式
            headless = True
    
    # 优先尝试从 account_manager 的内存中读取（包含 cookie_expired 标记）
    accounts = []
    try:
        from app.account_manager import account_manager
        # 直接使用内存中的 accounts，而不是重新从数据库加载
        # 因为 cookie_expired 字段只存在于内存中，不会保存到数据库
        with account_manager.lock:
            accounts = [acc.copy() for acc in account_manager.accounts]
        print(f"[批量刷新] 从内存读取账号列表（共 {len(accounts)} 个账号）")
    except ImportError:
        # account_manager 不可用，回退到 JSON
        pass
    except Exception as e:
        print(f"[批量刷新] 从内存读取失败: {e}，回退到 JSON")
    
    # 如果内存读取失败，回退到 JSON
    if not accounts:
        config_file = Path("business_gemini_session.json")
        if not config_file.exists():
            print("[批量刷新] ✗ 配置文件不存在且无法从内存读取")
            return
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            accounts = config.get("accounts", [])
            print(f"[批量刷新] 从 JSON 文件读取账号列表（共 {len(accounts)} 个账号）")
        except Exception as e:
            print(f"[批量刷新] ✗ 读取配置文件失败: {e}")
            return
    
    if not accounts:
        print("[批量刷新] ✗ 没有账号")
        return
    
    # 找出所有过期的账号
    expired_accounts = []
    for idx, account in enumerate(accounts):
        if account.get("cookie_expired", False):
            expired_accounts.append((idx, account))
            print(f"[批量刷新] 发现过期账号 {idx}: csesidx={account.get('csesidx', 'N/A')}, cookie_expired={account.get('cookie_expired')}")
    
    if not expired_accounts:
        print("[批量刷新] ✓ 没有过期的账号（检查了所有账号的 cookie_expired 标记）")
        return
    
    print(f"[批量刷新] 找到 {len(expired_accounts)} 个过期的账号，开始刷新...")
    
    # 使用统一的刷新函数，确保批量模式和手动模式流程一致
    # 直接调用 refresh_single_account，避免重复实现，保证流程完全一致
    success_count = 0
    fail_count = 0
    
    for account_idx, account in expired_accounts:
        print("\n" + "="*60)
        print(f"刷新账号 {account_idx} (csesidx: {account.get('csesidx', 'N/A')})")
        print("="*60)
        
        # 直接调用 refresh_single_account，确保流程一致
        try:
            success = refresh_single_account(account_idx, account, headless=headless)
            if success:
                success_count += 1
                print(f"[批量刷新] ✓ 账号 {account_idx} 刷新成功")
            else:
                fail_count += 1
                print(f"[批量刷新] ✗ 账号 {account_idx} 刷新失败")
        except Exception as e:
            fail_count += 1
            print(f"[批量刷新] ✗ 账号 {account_idx} 刷新时发生错误: {e}")
            import traceback
            traceback.print_exc()
        
        # 等待一下再处理下一个账号，避免请求过快
        if account_idx < len(expired_accounts) - 1:  # 不是最后一个账号
            time.sleep(2)
    
    print(f"\n[批量刷新] ✓ 所有账号处理完成（成功: {success_count}, 失败: {fail_count}, 总计: {len(expired_accounts)}）")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        # 批量刷新模式
        # 检查是否有 --headless 参数
        use_headless = "--headless" in sys.argv
        refresh_expired_accounts(headless=use_headless)
    else:
        # 单个登录模式
        main()

