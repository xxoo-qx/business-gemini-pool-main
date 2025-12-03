"""Cookie 刷新模块 - 使用 Playwright 自动化浏览器刷新 Cookie"""

import os
import time
import re
import threading
from typing import Optional, Dict
from datetime import datetime

# 从 config 导入 Playwright 可用性标志
from .config import PLAYWRIGHT_AVAILABLE, PLAYWRIGHT_BROWSER_INSTALLED

# 导入 Playwright（如果可用）
if PLAYWRIGHT_AVAILABLE:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .account_manager import account_manager

# 用于通知自动刷新线程立即检查过期账号的事件
_immediate_refresh_event = threading.Event()


def refresh_cookie_with_browser(account: dict, proxy: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    使用 Playwright 自动化浏览器刷新 Cookie
    返回: {"secure_c_ses": "...", "host_c_oses": "...", "csesidx": "..."} 或 None
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("[!] Playwright 未安装，无法自动刷新 Cookie")
        return None
    
    if not PLAYWRIGHT_BROWSER_INSTALLED:
        print("[!] Playwright 浏览器未安装，请运行: playwright install chromium")
        return None
    
    try:
        with sync_playwright() as p:
            # 启动浏览器
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox'] if os.name != 'nt' else []
                )
            except Exception as e:
                error_msg = str(e)
                if "Executable doesn't exist" in error_msg or "browser" in error_msg.lower():
                    print("[!] Playwright 浏览器未安装，请运行: playwright install chromium")
                    print(f"[!] 详细错误: {error_msg}")
                    return None
                raise
            
            # 获取现有 Cookie（如果可用）
            existing_secure_c_ses = account.get("secure_c_ses")
            existing_host_c_oses = account.get("host_c_oses")
            
            # 创建上下文，设置代理和 Cookie
            context_options = {
                "user_agent": account.get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'),
                "viewport": {"width": 1920, "height": 1080}
            }
            
            # 如果存在现有 Cookie，先设置它们以保持登录状态
            if existing_secure_c_ses:
                cookies_to_set = []
                
                # __Secure-C_SES 可能属于多个域名
                for domain in [".gemini.google", "business.gemini.google", ".google.com"]:
                    cookies_to_set.append({
                        "name": "__Secure-C_SES",
                        "value": existing_secure_c_ses,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "sameSite": "None"
                    })
                
                if existing_host_c_oses:
                    # __Host-C_OSES 必须是 business.gemini.google（不能有 domain）
                    cookies_to_set.append({
                        "name": "__Host-C_OSES",
                        "value": existing_host_c_oses,
                        "domain": "business.gemini.google",
                        "path": "/",
                        "secure": True,
                        "sameSite": "None"
                    })
                
                # 调试日志已关闭
                # print(f"[自动刷新] 设置 {len(cookies_to_set)} 个现有 Cookie")
                context_options["storage_state"] = {
                    "cookies": cookies_to_set,
                    "origins": []
                }
            else:
                # 调试日志已关闭
                # print("[自动刷新] 未找到现有 Cookie，将尝试从新会话获取")
                pass
            
            if proxy:
                context_options["proxy"] = {"server": proxy}
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            try:
                target_url = "https://business.gemini.google/"
                
                # 访问 business.gemini.google
                # 调试日志已关闭
                # print(f"[自动刷新] 正在访问 business.gemini.google...")
                # print(f"[自动刷新] 使用现有 Cookie: {'是' if existing_secure_c_ses else '否'}")
                
                # 使用 networkidle 等待网络请求完成，给更多时间让 Cookie 设置
                page.goto(target_url, wait_until="networkidle", timeout=60000)
                
                # 等待页面完全加载，包括 JavaScript 执行
                page.wait_for_timeout(8000)
                
                # 尝试等待页面加载完成（等待特定元素或网络请求）
                try:
                    # 等待页面标题或特定元素出现
                    page.wait_for_selector("body", timeout=10000)
                except:
                    pass
                
                # 再次等待，确保所有 Cookie 都已设置
                page.wait_for_timeout(3000)
                
                # 检查是否被重定向到登录页面
                current_url = page.url
                is_login_page = (
                    "accounts.google.com" in current_url or 
                    "signin" in current_url.lower() or
                    "auth.business.gemini.google/login" in current_url
                )
                
                if is_login_page:
                    print("[!] 检测到需要登录，Cookie 可能已过期或需要重新认证")
                    print(f"[!] 当前 URL: {current_url}")
                    # 等待一下，看是否会自动完成登录（如果有有效的 Cookie）
                    page.wait_for_timeout(5000)
                    current_url = page.url
                    if is_login_page and "auth.business.gemini.google/login" in current_url:
                        print("[!] 仍然在登录页面，Cookie 可能需要手动刷新")
                    else:
                        # 调试日志已关闭
                        # print(f"[自动刷新] 已通过认证，当前 URL: {current_url}")
                        pass
                else:
                    # 调试日志已关闭
                    # print(f"[自动刷新] 页面加载成功，当前 URL: {current_url}")
                    pass
                
                # 如果页面在登录页面，等待更长时间看是否能自动完成登录
                if is_login_page:
                    # 调试日志已关闭
                    # print("[自动刷新] 等待登录流程完成...")
                    # 等待页面可能的重定向
                    try:
                        page.wait_for_url("**/business.gemini.google/**", timeout=10000)
                        current_url = page.url
                        # 调试日志已关闭
                        # print(f"[自动刷新] 已重定向到: {current_url}")
                        pass
                    except:
                        # 如果超时，继续使用当前 URL
                        pass
                
                # 尝试触发一些网络请求，可能会设置新的 Cookie
                try:
                    # 尝试访问 API 端点，可能会刷新 Cookie
                    page.evaluate("""
                        () => {
                            // 尝试触发一些请求，可能会刷新 Cookie
                            fetch('https://business.gemini.google/', { 
                                method: 'GET',
                                credentials: 'include'
                            }).catch(() => {});
                        }
                    """)
                    page.wait_for_timeout(2000)
                except:
                    pass
                
                # 从 URL 中提取 csesidx（可能在 URL 参数中）
                current_url = page.url
                csesidx = None
                
                # 尝试从 URL 中提取 csesidx
                match = re.search(r'csesidx[=:](\d+)', current_url)
                if match:
                    csesidx = match.group(1)
                
                # 如果 URL 中没有，尝试从页面中查找
                if not csesidx:
                    try:
                        # 尝试从 localStorage、sessionStorage 或其他地方获取
                        csesidx = page.evaluate("""
                            () => {
                                // 1. 尝试从 URL 参数获取
                                const urlParams = new URLSearchParams(window.location.search);
                                let csesidx = urlParams.get('csesidx');
                                
                                // 2. 尝试从 URL 路径中提取
                                if (!csesidx) {
                                    const match = window.location.href.match(/csesidx[=:](\d+)/);
                                    if (match) csesidx = match[1];
                                }
                                
                                // 3. 尝试从 localStorage 获取
                                if (!csesidx) {
                                    try {
                                        csesidx = localStorage.getItem('csesidx') || 
                                                 localStorage.getItem('CSESIDX') ||
                                                 localStorage.getItem('csesIdx');
                                    } catch (e) {}
                                }
                                
                                // 4. 尝试从 sessionStorage 获取
                                if (!csesidx) {
                                    try {
                                        csesidx = sessionStorage.getItem('csesidx') || 
                                                 sessionStorage.getItem('CSESIDX') ||
                                                 sessionStorage.getItem('csesIdx');
                                    } catch (e) {}
                                }
                                
                                // 5. 尝试从全局变量获取
                                if (!csesidx) {
                                    if (window.csesidx) csesidx = String(window.csesidx);
                                    if (!csesidx && window.CSESIDX) csesidx = String(window.CSESIDX);
                                }
                                
                                // 6. 尝试从页面内容中查找（查找包含数字的 data 属性或 ID）
                                if (!csesidx) {
                                    const scripts = document.getElementsByTagName('script');
                                    for (let script of scripts) {
                                        if (script.textContent) {
                                            const match = script.textContent.match(/csesidx["']?\s*[:=]\s*["']?(\d+)/i);
                                            if (match) {
                                                csesidx = match[1];
                                                break;
                                            }
                                        }
                                    }
                                }
                                
                                return csesidx;
                            }
                        """)
                        if csesidx:
                            # 调试日志已关闭
                            # print(f"[自动刷新] 从页面获取到 csesidx: {csesidx[:10]}...")
                            pass
                    except Exception as e:
                        # 调试日志已关闭
                        # print(f"[自动刷新] 从页面获取 csesidx 时出错: {e}")
                        pass
                
                # 获取所有 Cookie（包括所有域名）
                all_cookies = context.cookies()
                # 调试日志已关闭
                # print(f"[自动刷新] 找到 {len(all_cookies)} 个 Cookie")
                
                # 打印所有 Cookie 名称用于调试
                cookie_names = [c['name'] for c in all_cookies]
                # 调试日志已关闭
                # print(f"[自动刷新] Cookie 列表: {', '.join(cookie_names[:10])}{'...' if len(cookie_names) > 10 else ''}")
                
                secure_c_ses = None
                host_c_oses = None
                
                # 从所有 Cookie 中查找（包括不同域名的）
                # 优先使用 business.gemini.google 域名的 Cookie
                for cookie in all_cookies:
                    cookie_name = cookie['name']
                    cookie_domain = cookie.get('domain', '')
                    
                    if cookie_name == '__Secure-C_SES':
                        # 优先使用 business.gemini.google 或 .gemini.google 的 Cookie
                        if not secure_c_ses or cookie_domain in ['business.gemini.google', '.gemini.google']:
                            secure_c_ses = cookie['value']
                            # 调试日志已关闭
                            # print(f"[自动刷新] 找到 __Secure-C_SES (domain: {cookie_domain})")
                    elif cookie_name == '__Host-C_OSES':
                        # __Host-C_OSES 必须是 business.gemini.google
                        if not host_c_oses or cookie_domain == 'business.gemini.google':
                            host_c_oses = cookie['value']
                            # 调试日志已关闭
                            # print(f"[自动刷新] 找到 __Host-C_OSES (domain: {cookie_domain})")
                            pass
                
                # 如果仍然没有找到，尝试从 document.cookie 获取（如果可能）
                if not secure_c_ses:
                    try:
                        page_cookies = page.evaluate("""
                            () => {
                                return document.cookie;
                            }
                        """)
                        if page_cookies:
                            # 调试日志已关闭
                            # print(f"[自动刷新] 尝试从 document.cookie 获取: {page_cookies[:100]}...")
                            # 解析 document.cookie
                            for cookie_str in page_cookies.split(';'):
                                cookie_str = cookie_str.strip()
                                if cookie_str.startswith('__Secure-C_SES='):
                                    secure_c_ses = cookie_str.split('=', 1)[1]
                                    # 调试日志已关闭
                                    # print(f"[自动刷新] 从 document.cookie 找到 __Secure-C_SES")
                                    pass
                                elif cookie_str.startswith('__Host-C_OSES='):
                                    host_c_oses = cookie_str.split('=', 1)[1]
                                    # 调试日志已关闭
                                    # print(f"[自动刷新] 从 document.cookie 找到 __Host-C_OSES")
                                    pass
                    except Exception as e:
                        # 调试日志已关闭
                        # print(f"[自动刷新] 无法从 document.cookie 获取: {e}")
                        pass
                
                # 如果 URL 中没有 csesidx，尝试从 Cookie 或其他方式获取
                if not csesidx:
                    # 尝试从页面 JavaScript 变量中获取
                    try:
                        csesidx = page.evaluate("""
                            () => {
                                // 尝试从全局变量或 localStorage 获取
                                if (window.csesidx) return window.csesidx;
                                if (window.location.pathname) {
                                    const match = window.location.pathname.match(/\\/(\\d+)/);
                                    if (match) return match[1];
                                }
                                return null;
                            }
                        """)
                    except:
                        pass
                
                # 如果仍然没有 csesidx，尝试从当前 URL 路径中提取
                if not csesidx:
                    url_parts = current_url.split('/')
                    for part in url_parts:
                        if part.isdigit() and len(part) > 6:  # csesidx 通常是较长的数字
                            csesidx = part
                            break
                
                if not secure_c_ses:
                    print("[!] 自动刷新失败: 未找到 __Secure-C_SES Cookie")
                    print("[!] 可能的原因:")
                    print("    1. Cookie 已过期，需要重新登录")
                    print("    2. 页面需要登录才能获取 Cookie")
                    print("    3. 网络请求未完成，Cookie 尚未设置")
                    print(f"[!] 当前页面 URL: {current_url}")
                    print(f"[!] 找到的 Cookie 数量: {len(all_cookies)}")
                    if existing_secure_c_ses:
                        print("[!] 提示: 现有 Cookie 可能已过期，请手动刷新或重新登录")
                    return None
                
                # 检查是否获取到了新的 Cookie（通过比较值）
                cookie_changed = False
                if existing_secure_c_ses:
                    if secure_c_ses != existing_secure_c_ses:
                        cookie_changed = True
                        print(f"[✓] 检测到 __Secure-C_SES 已更新（新值前10位: {secure_c_ses[:10]}...）")
                    else:
                        print(f"[!] __Secure-C_SES 值未变化，可能是旧的 Cookie")
                
                if existing_host_c_oses and host_c_oses:
                    if host_c_oses != existing_host_c_oses:
                        cookie_changed = True
                        print(f"[✓] 检测到 __Host-C_OSES 已更新")
                    elif not cookie_changed:
                        print(f"[!] __Host-C_OSES 值未变化")
                
                # 如果页面在登录页面且 Cookie 未变化，说明 Cookie 可能已过期
                if is_login_page and not cookie_changed and existing_secure_c_ses:
                    print("[!] 警告: 页面仍在登录页面，且 Cookie 值未变化")
                    print("[!] 这可能意味着 Cookie 已过期，需要手动登录获取新的 Cookie")
                    print("[!] 自动刷新失败: 无法获取新的 Cookie，现有 Cookie 可能已失效")
                    print("[!] 建议: 请在浏览器中登录 business.gemini.google，然后手动刷新 Cookie")
                    # 返回 None，表示自动刷新失败
                    return None
                
                if not csesidx:
                    # 如果没有找到新的 csesidx，使用旧的
                    old_csesidx = account.get("csesidx")
                    if old_csesidx:
                        csesidx = old_csesidx
                        print(f"[!] 未找到新的 csesidx，使用现有的: {csesidx[:10]}...")
                    else:
                        print("[!] 自动刷新失败: 未找到 csesidx，且账号配置中也没有")
                        return None
                else:
                    print(f"[✓] 成功获取到新的 csesidx: {csesidx[:10]}...")
                
                # 总结刷新结果
                if cookie_changed:
                    print(f"[✓] 自动刷新成功: 获取到新的 Cookie (csesidx: {csesidx[:10]}...)")
                else:
                    # 如果 Cookie 未变化但不在登录页面，可能仍然有效
                    if not is_login_page:
                        print(f"[!] 自动刷新完成: Cookie 值未变化，但页面已正常加载，使用现有 Cookie (csesidx: {csesidx[:10]}...)")
                    else:
                        # 这种情况应该已经在上面返回 None 了，但为了安全起见，这里也返回 None
                        print(f"[!] 自动刷新失败: Cookie 值未变化且页面仍在登录页面")
                        return None
                
                return {
                    "secure_c_ses": secure_c_ses,
                    "host_c_oses": host_c_oses or account.get("host_c_oses", ""),
                    "csesidx": csesidx
                }
                
            except PlaywrightTimeoutError:
                print("[!] 自动刷新失败: 页面加载超时")
                return None
            except Exception as e:
                error_msg = str(e)
                if "Executable doesn't exist" in error_msg or "browser" in error_msg.lower():
                    print("[!] Playwright 浏览器未安装，请运行: playwright install chromium")
                else:
                    print(f"[!] 自动刷新失败: {e}")
                return None
            finally:
                try:
                    context.close()
                    browser.close()
                except:
                    pass
                
    except Exception as e:
        error_msg = str(e)
        if "Executable doesn't exist" in error_msg or "browser" in error_msg.lower():
            print("[!] Playwright 浏览器未安装，请运行: playwright install chromium")
        else:
            print(f"[!] 自动刷新 Cookie 时发生错误: {e}")
        return None


def extract_cookies_from_active_session(account_idx: int, page, context) -> Optional[Dict[str, str]]:
    """
    从活跃的浏览器会话中提取 Cookie 和 csesidx
    """
    try:
        # 获取所有 Cookie
        all_cookies = context.cookies()
        secure_c_ses = None
        host_c_oses = None
        
        # 优先使用 business.gemini.google 域名的 Cookie
        for cookie in all_cookies:
            cookie_name = cookie['name']
            cookie_domain = cookie.get('domain', '')
            
            if cookie_name == '__Secure-C_SES':
                if not secure_c_ses or cookie_domain in ['business.gemini.google', '.gemini.google']:
                    secure_c_ses = cookie['value']
            elif cookie_name == '__Host-C_OSES':
                if not host_c_oses or cookie_domain == 'business.gemini.google':
                    host_c_oses = cookie['value']
        
        if not secure_c_ses:
            return None
        
        # 获取 csesidx
        csesidx = None
        current_url = page.url
        
        # 从 URL 中提取
        match = re.search(r'csesidx[=:](\d+)', current_url)
        if match:
            csesidx = match.group(1)
        
        # 从页面中获取
        if not csesidx:
            try:
                csesidx = page.evaluate("""
                    () => {
                        const urlParams = new URLSearchParams(window.location.search);
                        let csesidx = urlParams.get('csesidx');
                        if (!csesidx) {
                            const match = window.location.href.match(/csesidx[=:](\d+)/);
                            if (match) csesidx = match[1];
                        }
                        if (!csesidx) {
                            try {
                                csesidx = localStorage.getItem('csesidx') || 
                                         localStorage.getItem('CSESIDX');
                            } catch (e) {}
                        }
                        return csesidx;
                    }
                """)
            except:
                pass
        
        # 如果仍然没有，使用现有的
        if not csesidx:
            with account_manager.lock:
                csesidx = account_manager.accounts[account_idx].get("csesidx")
        
        return {
            "secure_c_ses": secure_c_ses,
            "host_c_oses": host_c_oses or account_manager.accounts[account_idx].get("host_c_oses", ""),
            "csesidx": csesidx
        }
    
    except Exception as e:
        print(f"[提取Cookie] 账号 {account_idx}: 提取失败: {e}")
        return None


def get_cookies_from_active_session(account_idx: int) -> Optional[Dict[str, str]]:
    """
    从活跃的浏览器会话中获取 Cookie（线程安全方式）
    不直接访问浏览器对象，而是从会话的 latest_cookies 中获取
    """
    with account_manager.lock:
        session = account_manager.browser_sessions.get(account_idx)
    
    if not session:
        return None
    
    # 从线程安全的 latest_cookies 中获取，避免跨线程访问浏览器对象
    latest_cookies = session.get("latest_cookies")
    if latest_cookies:
        # 返回副本，避免外部修改
        return latest_cookies.copy()
    
    return None


def auto_refresh_account_cookie(account_idx: int, account: dict) -> bool:
    """
    自动刷新指定账号的 Cookie
    优先从活跃的浏览器会话中获取，如果没有则启动浏览器会话或使用传统方法
    返回: True 如果成功，False 如果失败
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False
    
    # 首先尝试从活跃的浏览器会话中获取 Cookie
    cookies = get_cookies_from_active_session(account_idx)
    if cookies:
        # 调试日志已关闭
        # print(f"[自动刷新] 账号 {account_idx}: 从活跃会话获取到 Cookie")
        # print(f"[自动刷新] 账号 {account_idx}: Cookie 详情 - secure_c_ses: {cookies.get('secure_c_ses', 'N/A')[:20]}..., csesidx: {cookies.get('csesidx', 'N/A')}")
        pass
    else:
        # 如果没有活跃会话，检查是否应该启动一个
        with account_manager.lock:
            has_session = account_idx in account_manager.browser_sessions
            auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
        
        if not has_session and auto_refresh_enabled:
            # 启动浏览器会话（异步，不等待）
            # 调试日志已关闭
            # print(f"[自动刷新] 账号 {account_idx}: 未找到活跃会话，启动新的浏览器会话...")
            # 重新获取最新的账号信息（可能已被手动刷新）
            with account_manager.lock:
                if account_idx < len(account_manager.accounts):
                    latest_account = account_manager.accounts[account_idx].copy()
                    # 调试日志已关闭
                    # print(f"[自动刷新] 账号 {account_idx}: 使用最新的 Cookie (secure_c_ses: {latest_account.get('secure_c_ses', 'N/A')[:20]}...)")
                    pass
                else:
                    latest_account = account
            
            from .utils import get_proxy
            proxy = get_proxy()
            session_thread = threading.Thread(
                target=maintain_browser_session,
                args=(account_idx, latest_account, proxy),
                daemon=True
            )
            session_thread.start()
            
            # 等待一下，让浏览器会话初始化
            time.sleep(3)
            
            # 再次尝试从活跃会话获取
            cookies = get_cookies_from_active_session(account_idx)
            if cookies:
                # 调试日志已关闭
                # print(f"[自动刷新] 账号 {account_idx}: 从新启动的会话获取到 Cookie")
                pass
        
        # 如果还是没有，使用传统方法
        if not cookies:
            # 调试日志已关闭
            # print(f"[自动刷新] 账号 {account_idx}: 使用传统方法刷新 Cookie...")
            pass
            from .utils import get_proxy
            proxy = get_proxy()
            cookies = refresh_cookie_with_browser(account, proxy)
    
    if not cookies:
        # 保留关键错误信息
        print(f"[自动刷新] 账号 {account_idx}: 未能获取到 Cookie，刷新失败")
        return False
    
    # 调试日志已关闭
    # print(f"[自动刷新] 账号 {account_idx}: 开始更新账号配置...")
    
    # 更新账号 Cookie
    try:
        with account_manager.lock:
            acc = account_manager.accounts[account_idx]
            old_secure_c_ses = acc.get("secure_c_ses", "")
            # 强制更新 Cookie 相关字段，即使用户之前清空了它们
            acc["secure_c_ses"] = cookies["secure_c_ses"]
            acc["host_c_oses"] = cookies.get("host_c_oses", "")
            acc["csesidx"] = cookies.get("csesidx", "")
            
            # 清除 JWT 缓存和 session
            state = account_manager.account_states.get(account_idx, {})
            state["jwt"] = None
            state["jwt_time"] = 0
            state["session"] = None
            state["cookie_expired"] = False
            
            # 清除冷却状态（Cookie 已刷新，账号应该立即恢复可用）
            if "cooldown_until" in state:
                state.pop("cooldown_until", None)
            if "cooldown_reason" in state:
                state.pop("cooldown_reason", None)
            if "cooldown_until" in acc:
                acc.pop("cooldown_until", None)
            
            # 清除 Cookie 过期标记（不调用 mark_cookie_refreshed，避免重复保存和可能的死锁）
            if "cookie_expired" in acc:
                acc.pop("cookie_expired", None)
            if "cookie_expired_time" in acc:
                acc.pop("cookie_expired_time", None)
            
            # 恢复账号可用状态
            acc["available"] = True
            state["available"] = True
            # 调试日志已关闭
            # print(f"[自动刷新] 账号 {account_idx}: Cookie 过期标记已清除")
            
            account_manager.config["accounts"] = account_manager.accounts
            # 调试日志已关闭
            # print(f"[自动刷新] 账号 {account_idx}: 准备保存配置...")
        
        # 在释放锁后保存配置，避免阻塞
        account_manager.save_config()
        # 调试日志已关闭
        # print(f"[自动刷新] 账号 {account_idx}: 配置已保存")
        
        # 检查 Cookie 是否更新
        cookie_updated = old_secure_c_ses != cookies["secure_c_ses"]
        csesidx_info = f" (csesidx: {cookies.get('csesidx', 'N/A')[:10]}...)" if cookies.get("csesidx") else ""
        
        # 验证 Cookie 是否有效：尝试获取 JWT
        # 调试日志已关闭
        # print(f"[自动刷新] 账号 {account_idx}: 验证 Cookie 有效性...")
        try:
            from .utils import get_proxy
            from .jwt_utils import get_jwt_for_account
            proxy = get_proxy()
            # 使用更新后的账号信息获取 JWT
            updated_account = account_manager.accounts[account_idx]
            test_jwt = get_jwt_for_account(updated_account, proxy)
            # 调试日志已关闭
            # print(f"[自动刷新] 账号 {account_idx}: Cookie 验证成功，JWT 获取成功")
            pass
            
            if cookie_updated:
                print(f"[✓] 账号 {account_idx} Cookie 已自动刷新并更新{csesidx_info}")
            else:
                print(f"[✓] 账号 {account_idx} Cookie 已刷新（值未变化，已验证有效）{csesidx_info}")
            
            # 推送 WebSocket 更新事件，让前端及时刷新显示
            try:
                from .websocket_manager import emit_account_update
                with account_manager.lock:
                    updated_account = account_manager.accounts[account_idx].copy()
                emit_account_update(account_idx, updated_account)
            except Exception as e:
                # WebSocket 推送失败不影响刷新流程
                pass
            
            return True
        except Exception as e:
            error_msg = str(e)
            print(f"[!] 账号 {account_idx}: Cookie 验证失败: {error_msg}")
            
            # 如果 Cookie 无效，标记为过期
            with account_manager.lock:
                acc = account_manager.accounts[account_idx]
                acc["cookie_expired"] = True
                acc["cookie_expired_time"] = datetime.now().isoformat()
                state = account_manager.account_states.get(account_idx, {})
                state["cookie_expired"] = True
                account_manager.config["accounts"] = account_manager.accounts
            
            account_manager.save_config()
            print(f"[!] 账号 {account_idx}: Cookie 已标记为过期，需要手动刷新")
            
            if cookie_updated:
                print(f"[!] 账号 {account_idx} Cookie 已更新但验证失败，可能已过期{csesidx_info}")
            else:
                print(f"[!] 账号 {account_idx} Cookie 值未变化且验证失败，Cookie 已过期{csesidx_info}")
            
            return False
    except Exception as e:
        print(f"[!] 账号 {account_idx} 更新 Cookie 时发生错误: {e}")
        import traceback
        traceback.print_exc()
        return False


def maintain_browser_session(account_idx: int, account: dict, proxy: Optional[str] = None):
    """
    为指定账号维护一个持续运行的浏览器会话
    每 1 小时刷新一次页面以保持登录状态，然后提取 Cookie 和 csesidx
    """
    if not PLAYWRIGHT_AVAILABLE or not PLAYWRIGHT_BROWSER_INSTALLED:
        return
    
    REFRESH_INTERVAL = 1 * 3600  # 1 小时刷新一次
    
    try:
        # 在启动浏览器前，从 account_manager 获取最新的账号信息（可能已被手动刷新）
        with account_manager.lock:
            if account_idx < len(account_manager.accounts):
                account = account_manager.accounts[account_idx].copy()
            else:
                print(f"[浏览器会话] 账号 {account_idx}: 账号不存在，退出")
                return
        
        with sync_playwright() as p:
            # 启动浏览器
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox'] if os.name != 'nt' else []
            )
            
            # 获取现有 Cookie（使用最新的账号信息）
            existing_secure_c_ses = account.get("secure_c_ses")
            existing_host_c_oses = account.get("host_c_oses")
            
            # 创建上下文
            context_options = {
                "user_agent": account.get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'),
                "viewport": {"width": 1920, "height": 1080}
            }
            
            # 如果存在现有 Cookie，先设置它们
            if existing_secure_c_ses:
                cookies_to_set = []
                for domain in [".gemini.google", "business.gemini.google"]:
                    cookies_to_set.append({
                        "name": "__Secure-C_SES",
                        "value": existing_secure_c_ses,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "sameSite": "None"
                    })
                if existing_host_c_oses:
                    cookies_to_set.append({
                        "name": "__Host-C_OSES",
                        "value": existing_host_c_oses,
                        "domain": "business.gemini.google",
                        "path": "/",
                        "secure": True,
                        "sameSite": "None"
                    })
                context_options["storage_state"] = {
                    "cookies": cookies_to_set,
                    "origins": []
                }
            
            if proxy:
                context_options["proxy"] = {"server": proxy}
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            # 初始访问
            print(f"[浏览器会话] 账号 {account_idx}: 初始化浏览器会话...")
            page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
            
            # 检查是否被重定向到登录页面
            current_url = page.url
            is_login_page = (
                "accounts.google.com" in current_url or 
                "signin" in current_url.lower() or
                "auth.business.gemini.google/login" in current_url
            )
            
            if is_login_page:
                print(f"[浏览器会话] 账号 {account_idx}: 检测到登录页面，等待自动登录完成...")
                print(f"[浏览器会话] 账号 {account_idx}: 当前 URL: {current_url}")
                
                # 等待自动登录完成（Google 可能会自动完成登录流程）
                login_completed = False
                try:
                    # 等待页面重定向到 business.gemini.google
                    page.wait_for_url("**/business.gemini.google/**", timeout=15000)
                    current_url = page.url
                    # 再次确认 URL 确实跳转到了 business.gemini.google
                    if "business.gemini.google" in current_url and "login" not in current_url:
                        print(f"[浏览器会话] 账号 {account_idx}: 已自动完成登录，当前 URL: {current_url}")
                        is_login_page = False
                        login_completed = True
                    else:
                        print(f"[浏览器会话] 账号 {account_idx}: URL 未正确跳转，当前 URL: {current_url}")
                except:
                    # 如果超时，再次检查 URL
                    page.wait_for_timeout(5000)
                    current_url = page.url
                    is_login_page = (
                        "accounts.google.com" in current_url or 
                        "signin" in current_url.lower() or
                        "auth.business.gemini.google/login" in current_url
                    )
                    if not is_login_page and "business.gemini.google" in current_url:
                        print(f"[浏览器会话] 账号 {account_idx}: 已自动完成登录，当前 URL: {current_url}")
                        login_completed = True
                
                # 如果仍然在登录页面，验证 Cookie 是否有效
                if not login_completed:
                    print(f"[浏览器会话] 账号 {account_idx}: 仍然在登录页面，验证 Cookie 是否有效...")
                    # 即使还在登录页面，也先验证 Cookie 是否真的有效
                    # 因为有时 Cookie 有效但页面需要更多时间加载
                    try:
                        from .utils import get_proxy
                        from .jwt_utils import get_jwt_for_account
                        proxy = get_proxy()
                        # 使用当前账号的 Cookie 测试 JWT
                        test_jwt = get_jwt_for_account(account, proxy)
                        print(f"[浏览器会话] 账号 {account_idx}: Cookie 验证成功（JWT 获取成功），尝试导航到目标页面...")
                        
                        # Cookie 有效，尝试导航到 business.gemini.google 以保持会话活跃
                        try:
                            print(f"[浏览器会话] 账号 {account_idx}: 导航到 https://business.gemini.google/ 以保持会话活跃...")
                            
                            # 先尝试等待自动登录完成（如果当前在登录页面）
                            if is_login_page:
                                print(f"[浏览器会话] 账号 {account_idx}: 等待自动登录流程完成（最多30秒）...")
                                try:
                                    # 等待页面自动跳转到 business.gemini.google
                                    page.wait_for_url("**/business.gemini.google/**", timeout=30000)
                                    current_url = page.url
                                    if "business.gemini.google" in current_url and "login" not in current_url:
                                        print(f"[✓] 账号 {account_idx}: 自动登录成功，已跳转到: {current_url}")
                                        is_login_page = False
                                    else:
                                        print(f"[浏览器会话] 账号 {account_idx}: 等待超时或未正确跳转，尝试手动导航...")
                                except:
                                    print(f"[浏览器会话] 账号 {account_idx}: 自动登录等待超时，尝试手动导航...")
                            
                            # 如果仍在登录页面，尝试导航
                            if is_login_page:
                                page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=60000)
                                page.wait_for_timeout(10000)  # 等待更长时间，让自动登录完成
                                
                                # 再次检查是否自动跳转
                                current_url = page.url
                                if "business.gemini.google" in current_url and "login" not in current_url:
                                    print(f"[✓] 账号 {account_idx}: 导航成功，已到达目标页面")
                                    is_login_page = False
                                else:
                                    # 尝试点击"继续"按钮（如果有）
                                    try:
                                        # 查找可能的继续按钮
                                        continue_button = page.query_selector('button:has-text("Continue"), button:has-text("继续"), a:has-text("Continue"), a:has-text("继续")')
                                        if continue_button:
                                            print(f"[浏览器会话] 账号 {account_idx}: 找到继续按钮，尝试点击...")
                                            continue_button.click()
                                            page.wait_for_timeout(5000)
                                            current_url = page.url
                                            if "business.gemini.google" in current_url and "login" not in current_url:
                                                print(f"[✓] 账号 {account_idx}: 点击继续按钮后成功跳转")
                                                is_login_page = False
                                    except:
                                        pass
                            
                            # 最终检查 URL
                            current_url = page.url
                            is_still_login = (
                                "accounts.google.com" in current_url or 
                                "signin" in current_url.lower() or
                                "auth.business.gemini.google/login" in current_url
                            )
                            
                            # 检查页面标题和内容，确认是否真的在目标页面
                            try:
                                page_title = page.title()
                                page_url = page.url
                                
                                # 检查页面是否包含 Gemini 相关的内容
                                page_content = page.content()
                                has_gemini_content = "gemini" in page_content.lower() or "business" in page_content.lower()
                                
                                if is_still_login:
                                    print(f"[!] 账号 {account_idx}: 导航失败 - 仍在登录页面")
                                    print(f"[!] 账号 {account_idx}: 当前 URL: {current_url}")
                                    print(f"[!] 账号 {account_idx}: 页面标题: {page_title}")
                                    print(f"[!] 账号 {account_idx}: 但 Cookie 有效（JWT 验证成功），继续使用浏览器会话")
                                    print(f"[!] 账号 {account_idx}: 提示：可能需要手动在浏览器中完成一次登录，然后 Cookie 才能正常使用")
                                else:
                                    print(f"[✓] 账号 {account_idx}: 导航成功 - 已到达目标页面")
                                    print(f"[✓] 账号 {account_idx}: 当前 URL: {current_url}")
                                    print(f"[✓] 账号 {account_idx}: 页面标题: {page_title}")
                                    if has_gemini_content:
                                        print(f"[✓] 账号 {account_idx}: 页面内容验证通过（包含 Gemini 相关内容）")
                                    else:
                                        print(f"[!] 账号 {account_idx}: 警告 - 页面内容可能不完整")
                            except Exception as check_error:
                                print(f"[浏览器会话] 账号 {account_idx}: 检查页面内容时出错: {check_error}")
                                if is_still_login:
                                    print(f"[!] 账号 {account_idx}: 导航后仍在登录页面，但 Cookie 有效，继续使用浏览器会话")
                                else:
                                    print(f"[浏览器会话] 账号 {account_idx}: 已导航到: {current_url}")
                            
                            is_login_page = False  # Cookie 有效，继续使用
                        except Exception as nav_error:
                            print(f"[!] 账号 {account_idx}: 导航到目标页面时出错: {nav_error}")
                            print(f"[!] 账号 {account_idx}: 但 Cookie 有效（JWT 验证成功），继续使用浏览器会话")
                            is_login_page = False  # Cookie 有效，继续使用
                    except Exception as e:
                        error_msg = str(e)
                        print(f"[!] 账号 {account_idx}: Cookie 验证失败: {error_msg}")
                        print(f"[!] 账号 {account_idx}: Cookie 已过期，浏览器会话将退出")
                        
                        # 标记 Cookie 为过期
                        with account_manager.lock:
                            acc = account_manager.accounts[account_idx]
                            acc["cookie_expired"] = True
                            acc["cookie_expired_time"] = datetime.now().isoformat()
                            state = account_manager.account_states.get(account_idx, {})
                            state["cookie_expired"] = True
                            account_manager.config["accounts"] = account_manager.accounts
                        
                        account_manager.save_config()
                        print(f"[!] 账号 {account_idx}: Cookie 已标记为过期，需要手动刷新")
                        print(f"[!] 账号 {account_idx}: 浏览器会话将退出，请手动刷新 Cookie 后重新启用自动刷新")
                        # 退出会话
                        try:
                            context.close()
                            browser.close()
                        except:
                            pass
                        with account_manager.lock:
                            account_manager.browser_sessions.pop(account_idx, None)
                        return
            
            if is_login_page:
                # 如果最终还是登录页面，标记为过期
                print(f"[!] 账号 {account_idx}: 初始化时检测到登录页面，Cookie 可能已过期")
                print(f"[!] 账号 {account_idx}: 当前 URL: {current_url}")
                
                # 标记 Cookie 为过期
                with account_manager.lock:
                    acc = account_manager.accounts[account_idx]
                    acc["cookie_expired"] = True
                    acc["cookie_expired_time"] = datetime.now().isoformat()
                    state = account_manager.account_states.get(account_idx, {})
                    state["cookie_expired"] = True
                    account_manager.config["accounts"] = account_manager.accounts
                
                account_manager.save_config()
                print(f"[!] 账号 {account_idx}: Cookie 已标记为过期，需要手动刷新")
                print(f"[!] 账号 {account_idx}: 浏览器会话将退出，请手动刷新 Cookie 后重新启用自动刷新")
                # 退出会话
                try:
                    context.close()
                    browser.close()
                except:
                    pass
                with account_manager.lock:
                    account_manager.browser_sessions.pop(account_idx, None)
                return
            
            # 初始提取 Cookie
            initial_cookies = extract_cookies_from_active_session(account_idx, page, context)
            
            # 验证初始 Cookie 是否有效
            if initial_cookies:
                print(f"[浏览器会话] 账号 {account_idx}: 验证初始 Cookie 有效性...")
                try:
                    from .utils import get_proxy
                    from .jwt_utils import get_jwt_for_account
                    proxy = get_proxy()
                    # 使用提取的 Cookie 创建测试账号
                    test_account = account.copy()
                    test_account["secure_c_ses"] = initial_cookies["secure_c_ses"]
                    if initial_cookies.get("host_c_oses"):
                        test_account["host_c_oses"] = initial_cookies["host_c_oses"]
                    if initial_cookies.get("csesidx"):
                        test_account["csesidx"] = initial_cookies["csesidx"]
                    
                    test_jwt = get_jwt_for_account(test_account, proxy)
                    print(f"[浏览器会话] 账号 {account_idx}: 初始 Cookie 验证成功")
                except Exception as e:
                    error_msg = str(e)
                    print(f"[!] 账号 {account_idx}: 初始 Cookie 验证失败: {error_msg}")
                    print(f"[!] 账号 {account_idx}: Cookie 已过期，浏览器会话将退出")
                    # 标记 Cookie 为过期
                    with account_manager.lock:
                        acc = account_manager.accounts[account_idx]
                        acc["cookie_expired"] = True
                        acc["cookie_expired_time"] = datetime.now().isoformat()
                        state = account_manager.account_states.get(account_idx, {})
                        state["cookie_expired"] = True
                        account_manager.config["accounts"] = account_manager.accounts
                    
                    account_manager.save_config()
                    # 退出会话
                    try:
                        context.close()
                        browser.close()
                    except:
                        pass
                    with account_manager.lock:
                        account_manager.browser_sessions.pop(account_idx, None)
                    return
            
            # 保存会话信息（注意：playwright 对象需要保持在作用域内）
            with account_manager.lock:
                account_manager.browser_sessions[account_idx] = {
                    "browser": browser,
                    "context": context,
                    "page": page,
                    "last_refresh_time": time.time(),
                    "playwright": p,  # 保持 playwright 对象
                    "latest_cookies": initial_cookies.copy() if initial_cookies else None,  # 线程安全存储
                    "need_refresh": False  # 立即刷新标志
                }
            
            print(f"[浏览器会话] 账号 {account_idx}: 浏览器会话已启动，将每 1 小时刷新一次（或检测到 Cookie 更新时立即刷新）")
            
            # 持续刷新循环
            # 使用较短的检查间隔（1分钟），以便及时响应 Cookie 更新
            CHECK_INTERVAL = 60  # 1 分钟检查一次
            last_refresh_time = time.time()
            
            while True:
                try:
                    time.sleep(CHECK_INTERVAL)
                    
                    current_time = time.time()
                    time_since_last_refresh = current_time - last_refresh_time
                    should_refresh = time_since_last_refresh >= REFRESH_INTERVAL
                    
                    # 检查账号是否仍然启用自动刷新
                    auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
                    if not auto_refresh_enabled:
                        print(f"[浏览器会话] 账号 {account_idx}: 自动刷新已禁用，退出会话")
                        break
                    
                    # 检查账号是否仍然存在，并获取最新的 Cookie
                    with account_manager.lock:
                        if account_idx >= len(account_manager.accounts):
                            print(f"[浏览器会话] 账号 {account_idx}: 账号已删除，退出会话")
                            break
                        latest_account = account_manager.accounts[account_idx].copy()
                        # 检查是否需要立即刷新（手动刷新 Cookie 时设置）
                        session = account_manager.browser_sessions.get(account_idx)
                        need_immediate_refresh = session and session.get("need_refresh", False)
                        if need_immediate_refresh:
                            # 清除标志
                            session["need_refresh"] = False
                    
                    # 检查 Cookie 是否有更新（用户可能手动刷新了）
                    latest_secure_c_ses = latest_account.get("secure_c_ses")
                    latest_host_c_oses = latest_account.get("host_c_oses")
                    cookie_updated = (
                        latest_secure_c_ses != existing_secure_c_ses or
                        latest_host_c_oses != existing_host_c_oses
                    )
                    
                    # 如果需要立即刷新或 Cookie 已更新，或者到了刷新时间
                    if need_immediate_refresh or cookie_updated or should_refresh:
                        if need_immediate_refresh:
                            print(f"[浏览器会话] 账号 {account_idx}: 收到立即刷新通知（手动刷新 Cookie 触发）")
                        elif cookie_updated:
                            print(f"[浏览器会话] 账号 {account_idx}: 检测到 Cookie 已更新，立即刷新")
                        elif should_refresh:
                            print(f"[浏览器会话] 账号 {account_idx}: 到达刷新时间（1小时），刷新页面")
                    
                        # 如果 Cookie 已更新，更新浏览器上下文中的 Cookie
                        if cookie_updated:
                            print(f"[浏览器会话] 账号 {account_idx}: 更新浏览器上下文中的 Cookie...")
                            print(f"[浏览器会话] 账号 {account_idx}: 旧 secure_c_ses: {existing_secure_c_ses[:20] if existing_secure_c_ses else 'N/A'}...")
                            print(f"[浏览器会话] 账号 {account_idx}: 新 secure_c_ses: {latest_secure_c_ses[:20] if latest_secure_c_ses else 'N/A'}...")
                            
                            # 更新浏览器上下文中的 Cookie
                            cookies_to_update = []
                            if latest_secure_c_ses:
                                for domain in [".gemini.google", "business.gemini.google"]:
                                    cookies_to_update.append({
                                        "name": "__Secure-C_SES",
                                        "value": latest_secure_c_ses,
                                        "domain": domain,
                                        "path": "/",
                                        "secure": True,
                                        "sameSite": "None"
                                    })
                            if latest_host_c_oses:
                                cookies_to_update.append({
                                    "name": "__Host-C_OSES",
                                    "value": latest_host_c_oses,
                                    "domain": "business.gemini.google",
                                    "path": "/",
                                    "secure": True,
                                    "sameSite": "None"
                                })
                            
                            if cookies_to_update:
                                try:
                                    # 清除旧的 Cookie
                                    context.clear_cookies()
                                    # 添加新的 Cookie
                                    context.add_cookies(cookies_to_update)
                                    # 更新本地变量
                                    existing_secure_c_ses = latest_secure_c_ses
                                    existing_host_c_oses = latest_host_c_oses
                                    print(f"[浏览器会话] 账号 {account_idx}: Cookie 已更新到浏览器上下文")
                                except Exception as e:
                                    print(f"[!] 账号 {account_idx}: 更新浏览器 Cookie 时出错: {e}")
                        
                        print(f"[浏览器会话] 账号 {account_idx}: 刷新页面以保持登录状态...")
                        
                        # 检查当前是否在登录页面
                        current_url_before = page.url
                        is_on_login_page = (
                            "accounts.google.com" in current_url_before or 
                            "signin" in current_url_before.lower() or
                            "auth.business.gemini.google/login" in current_url_before
                        )
                        
                        # 刷新页面
                        try:
                            if is_on_login_page:
                                # 如果在登录页面，先更新 Cookie，然后导航到 business.gemini.google
                                print(f"[浏览器会话] 账号 {account_idx}: 当前在登录页面，更新 Cookie 后导航到 business.gemini.google...")
                                # Cookie 已经在上面更新了（如果有更新），现在导航到目标页面
                                page.goto("https://business.gemini.google/", wait_until="networkidle", timeout=60000)
                                page.wait_for_timeout(5000)
                                
                                # 检查导航结果
                                nav_url = page.url
                                nav_is_login = (
                                    "accounts.google.com" in nav_url or 
                                    "signin" in nav_url.lower() or
                                    "auth.business.gemini.google/login" in nav_url
                                )
                                
                                if nav_is_login:
                                    print(f"[!] 账号 {account_idx}: 刷新时导航失败 - 仍在登录页面: {nav_url}")
                                else:
                                    print(f"[✓] 账号 {account_idx}: 刷新时导航成功 - 已到达目标页面: {nav_url}")
                            else:
                                # 如果不在登录页面，直接刷新
                                print(f"[浏览器会话] 账号 {account_idx}: 刷新当前页面...")
                                page.reload(wait_until="networkidle", timeout=60000)
                                page.wait_for_timeout(5000)
                            
                            # 检查是否被重定向到登录页面
                            current_url = page.url
                            is_login_page = (
                                "accounts.google.com" in current_url or 
                                "signin" in current_url.lower() or
                                "auth.business.gemini.google/login" in current_url
                            )
                            
                            # 显示当前页面状态
                            try:
                                page_title = page.title()
                                if is_login_page:
                                    print(f"[!] 账号 {account_idx}: 刷新后检测到登录页面")
                                    print(f"[!] 账号 {account_idx}: 当前 URL: {current_url}")
                                    print(f"[!] 账号 {account_idx}: 页面标题: {page_title}")
                                else:
                                    print(f"[✓] 账号 {account_idx}: 刷新成功 - 当前在目标页面")
                                    print(f"[✓] 账号 {account_idx}: 当前 URL: {current_url}")
                                    print(f"[✓] 账号 {account_idx}: 页面标题: {page_title}")
                            except:
                                pass
                            
                            if is_login_page:
                                print(f"[!] 账号 {account_idx}: 刷新后检测到登录页面，验证 Cookie 是否仍然有效...")
                                print(f"[!] 账号 {account_idx}: 当前 URL: {current_url}")
                                
                                # 验证 Cookie 是否真的过期（通过 JWT 测试）
                                try:
                                    from .utils import get_proxy
                                    from .jwt_utils import get_jwt_for_account
                                    proxy = get_proxy()
                                    # 使用最新的账号信息测试 JWT
                                    with account_manager.lock:
                                        test_account = account_manager.accounts[account_idx].copy()
                                    
                                    test_jwt = get_jwt_for_account(test_account, proxy)
                                    print(f"[浏览器会话] 账号 {account_idx}: Cookie 验证成功（JWT 获取成功），虽然页面在登录页，但 Cookie 仍然有效")
                                    # Cookie 仍然有效，继续使用
                                    last_refresh_time = time.time()
                                    continue
                                except Exception as jwt_error:
                                    error_msg = str(jwt_error)
                                    print(f"[!] 账号 {account_idx}: Cookie 验证失败: {error_msg}")
                                    print(f"[!] 账号 {account_idx}: Cookie 已过期，浏览器会话无法自动刷新")
                                    
                                    # 标记 Cookie 为过期
                                    with account_manager.lock:
                                        acc = account_manager.accounts[account_idx]
                                        acc["cookie_expired"] = True
                                        acc["cookie_expired_time"] = datetime.now().isoformat()
                                        state = account_manager.account_states.get(account_idx, {})
                                        state["cookie_expired"] = True
                                        # 清除 JWT 缓存
                                        state["jwt"] = None
                                        state["jwt_time"] = 0
                                        state["session"] = None
                                        account_manager.config["accounts"] = account_manager.accounts
                                    
                                    account_manager.save_config()
                                    print(f"[!] 账号 {account_idx}: Cookie 已标记为过期，需要手动刷新")
                                    print(f"[!] 账号 {account_idx}: 浏览器会话将退出，请手动刷新 Cookie 后重新启用自动刷新")
                                    
                                    # 退出浏览器会话
                                    try:
                                        context.close()
                                        browser.close()
                                    except:
                                        pass
                                    with account_manager.lock:
                                        account_manager.browser_sessions.pop(account_idx, None)
                                    
                                    return  # 退出线程
                            
                            # 提取 Cookie 和 csesidx
                            cookies = extract_cookies_from_active_session(account_idx, page, context)
                            if cookies:
                                # 验证 Cookie 是否有效
                                print(f"[浏览器会话] 账号 {account_idx}: 验证 Cookie 有效性...")
                                try:
                                    from .utils import get_proxy
                                    from .jwt_utils import get_jwt_for_account
                                    proxy = get_proxy()
                                    # 使用更新后的账号信息获取 JWT
                                    with account_manager.lock:
                                        test_account = account_manager.accounts[account_idx].copy()
                                        test_account["secure_c_ses"] = cookies["secure_c_ses"]
                                        if cookies.get("host_c_oses"):
                                            test_account["host_c_oses"] = cookies["host_c_oses"]
                                        if cookies.get("csesidx"):
                                            test_account["csesidx"] = cookies["csesidx"]
                                    
                                    test_jwt = get_jwt_for_account(test_account, proxy)
                                    print(f"[浏览器会话] 账号 {account_idx}: Cookie 验证成功")
                                    
                                    # 更新账号 Cookie
                                    with account_manager.lock:
                                        acc = account_manager.accounts[account_idx]
                                        acc["secure_c_ses"] = cookies["secure_c_ses"]
                                        if cookies.get("host_c_oses"):
                                            acc["host_c_oses"] = cookies["host_c_oses"]
                                        if cookies.get("csesidx"):
                                            acc["csesidx"] = cookies["csesidx"]
                                        acc["cookie_refresh_time"] = datetime.now().isoformat()
                                        
                                        # 清除 Cookie 过期标记
                                        if "cookie_expired" in acc:
                                            acc.pop("cookie_expired", None)
                                        if "cookie_expired_time" in acc:
                                            acc.pop("cookie_expired_time", None)
                                        state = account_manager.account_states.get(account_idx, {})
                                        state["cookie_expired"] = False
                                        
                                        # 清除 JWT 缓存（因为 Cookie 已更新）
                                        state["jwt"] = None
                                        state["jwt_time"] = 0
                                        state["session"] = None
                                        
                                        account_manager.config["accounts"] = account_manager.accounts
                                    
                                    account_manager.save_config()
                                    
                                    # 线程安全地更新会话中的最新 Cookie
                                    if account_idx in account_manager.browser_sessions:
                                        account_manager.browser_sessions[account_idx]["last_refresh_time"] = time.time()
                                        account_manager.browser_sessions[account_idx]["latest_cookies"] = cookies.copy()
                                    
                                    print(f"[浏览器会话] 账号 {account_idx}: Cookie 已更新并验证有效 (csesidx: {cookies.get('csesidx', 'N/A')[:10]}...)")
                                    # 更新最后刷新时间
                                    last_refresh_time = time.time()
                                    # 更新本地 Cookie 变量
                                    existing_secure_c_ses = cookies["secure_c_ses"]
                                    if cookies.get("host_c_oses"):
                                        existing_host_c_oses = cookies["host_c_oses"]
                                except Exception as e:
                                    error_msg = str(e)
                                    print(f"[!] 账号 {account_idx}: Cookie 验证失败: {error_msg}")
                                    
                                    # 如果 Cookie 无效，标记为过期
                                    with account_manager.lock:
                                        acc = account_manager.accounts[account_idx]
                                        acc["cookie_expired"] = True
                                        acc["cookie_expired_time"] = datetime.now().isoformat()
                                        state = account_manager.account_states.get(account_idx, {})
                                        state["cookie_expired"] = True
                                        # 清除 JWT 缓存
                                        state["jwt"] = None
                                        state["jwt_time"] = 0
                                        state["session"] = None
                                        account_manager.config["accounts"] = account_manager.accounts
                                    
                                    account_manager.save_config()
                                    print(f"[!] 账号 {account_idx}: Cookie 已标记为过期，需要手动刷新")
                                    last_refresh_time = time.time()  # 更新刷新时间，避免频繁重试
                            else:
                                print(f"[浏览器会话] 账号 {account_idx}: 未能提取 Cookie，但会话继续运行")
                                last_refresh_time = time.time()  # 更新刷新时间，避免频繁重试
                        except Exception as e:
                            print(f"[浏览器会话] 账号 {account_idx}: 刷新页面时出错: {e}")
                            # 继续运行，下次再试
                            last_refresh_time = time.time()  # 更新刷新时间，避免频繁重试
                
                except Exception as e:
                    print(f"[浏览器会话] 账号 {account_idx}: 会话循环出错: {e}")
                    time.sleep(60)  # 出错后等待1分钟再继续
            
            # 清理
            try:
                context.close()
                browser.close()
            except:
                pass
            
            with account_manager.lock:
                account_manager.browser_sessions.pop(account_idx, None)
            
            print(f"[浏览器会话] 账号 {account_idx}: 浏览器会话已结束")
    
    except Exception as e:
        print(f"[浏览器会话] 账号 {account_idx}: 启动浏览器会话失败: {e}")
        with account_manager.lock:
            account_manager.browser_sessions.pop(account_idx, None)


def cookie_refresh_worker():
    """
    后台线程：定期检查并自动刷新 Cookie
    新方案：为每个账号维护持续运行的浏览器会话，每1小时刷新页面
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("[提示] Playwright 未安装，自动刷新 Cookie 功能已禁用")
        return
    
    if not PLAYWRIGHT_BROWSER_INSTALLED:
        print("[提示] Playwright 浏览器未安装，自动刷新 Cookie 功能已禁用")
        return
    
    # 等待一下，让主程序完全启动
    time.sleep(5)
    
    # 检查配置是否启用自动刷新
    auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
    if not auto_refresh_enabled:
        print("[提示] 自动刷新 Cookie 功能未启用（在系统设置中启用）")
        return
    
    print("[浏览器会话] 启动持续运行的浏览器会话...")
    
    # 为每个账号启动浏览器会话线程
    with account_manager.lock:
        accounts = account_manager.accounts.copy()
        from .utils import get_proxy
        proxy = get_proxy()
    
    started_count = 0
    for idx, acc in enumerate(accounts):
        if not acc.get("available", True):
            continue
        
        # 检查是否已经有活跃会话
        if idx in account_manager.browser_sessions:
            print(f"[浏览器会话] 账号 {idx} 已有活跃会话，跳过")
            continue
        
        # 启动浏览器会话线程
        session_thread = threading.Thread(
            target=maintain_browser_session,
            args=(idx, acc, proxy),
            daemon=True
        )
        session_thread.start()
        started_count += 1
        print(f"[浏览器会话] 账号 {idx} 的浏览器会话线程已启动")
    
    print(f"[浏览器会话] 已为 {started_count} 个账号启动浏览器会话（每1小时自动刷新）")


def auto_refresh_expired_cookies_worker():
    """
    后台线程：定期检查过期的 Cookie，使用临时邮箱自动刷新
    这是主要的 Cookie 自动刷新机制，通过临时邮箱登录来更新过期的 Cookie
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("[提示] Playwright 未安装，Cookie 自动刷新功能已禁用")
        return
    
    if not PLAYWRIGHT_BROWSER_INSTALLED:
        print("[提示] Playwright 浏览器未安装，Cookie 自动刷新功能已禁用")
        return
    
    # 等待一下，让主程序完全启动
    time.sleep(10)
    
    # 检查配置是否启用自动刷新
    auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
    if not auto_refresh_enabled:
        print("[提示] 自动刷新 Cookie 功能未启用（在系统设置中启用）")
        return
    
    print("[Cookie 自动刷新] 后台线程已启动，将每30分钟检查一次过期的 Cookie")
    
    # 检查间隔：30分钟
    CHECK_INTERVAL = 30 * 60
    
    # 记录上次检查时间，用于日志
    last_check_time = time.time()
    check_count = 0
    
    def _check_and_refresh_expired():
        """检查并刷新过期账号的内部函数"""
        expired_count = 0
        expired_indices = []
        total_accounts = 0
        
        # 先检查标记为过期的账号
        with account_manager.lock:
            total_accounts = len(account_manager.accounts)
            for idx, acc in enumerate(account_manager.accounts):
                if acc.get("cookie_expired", False):
                    expired_count += 1
                    expired_indices.append(idx)
        
        # 实际验证 cookie：对于标记为有效的账号，实际测试 cookie 是否真的有效
        print(f"[Cookie 自动刷新] 开始验证 Cookie 有效性...")
        from .utils import get_proxy
        from .jwt_utils import get_jwt_for_account
        from .exceptions import AccountAuthError, AccountRequestError
        
        proxy = get_proxy()
        verified_expired = []
        
        for idx, acc in enumerate(account_manager.accounts):
            # 跳过已经标记为过期的账号
            if acc.get("cookie_expired", False):
                continue
            
            # 检查是否有必要的 cookie 字段
            secure_c_ses = acc.get("secure_c_ses", "").strip()
            csesidx = acc.get("csesidx", "").strip()
            if not secure_c_ses or not csesidx:
                # Cookie 字段不完整，标记为过期
                print(f"[Cookie 自动刷新] 账号 {idx}: Cookie 字段不完整，标记为过期")
                with account_manager.lock:
                    acc["cookie_expired"] = True
                    acc["cookie_expired_time"] = datetime.now().isoformat()
                    state = account_manager.account_states.get(idx, {})
                    state["cookie_expired"] = True
                account_manager.save_config()
                if idx not in expired_indices:
                    expired_count += 1
                    expired_indices.append(idx)
                verified_expired.append(idx)
                continue
            
            # 实际验证 cookie：尝试获取 JWT
            try:
                test_jwt = get_jwt_for_account(acc, proxy)
                # 验证成功，cookie 有效
                # print(f"[Cookie 自动刷新] 账号 {idx}: Cookie 验证成功")
            except (AccountAuthError, AccountRequestError, ValueError) as e:
                # Cookie 验证失败，标记为过期
                error_msg = str(e)
                print(f"[Cookie 自动刷新] 账号 {idx}: Cookie 验证失败 - {error_msg}，标记为过期")
                with account_manager.lock:
                    acc["cookie_expired"] = True
                    acc["cookie_expired_time"] = datetime.now().isoformat()
                    state = account_manager.account_states.get(idx, {})
                    state["cookie_expired"] = True
                account_manager.save_config()
                if idx not in expired_indices:
                    expired_count += 1
                    expired_indices.append(idx)
                verified_expired.append(idx)
            except Exception as e:
                # 其他异常，记录但不标记为过期（可能是网络问题等）
                print(f"[Cookie 自动刷新] 账号 {idx}: Cookie 验证时发生异常（可能是网络问题）: {e}")
        
        if verified_expired:
            print(f"[Cookie 自动刷新] 通过实际验证发现 {len(verified_expired)} 个账号 Cookie 已失效: {verified_expired}")
        
        current_time = time.time()
        time_since_last = int(current_time - last_check_time)
        print(f"[Cookie 自动刷新] 检查: 共 {total_accounts} 个账号，{expired_count} 个过期 {f'(账号: {expired_indices})' if expired_indices else ''}")
        
        if expired_count > 0:
            print(f"[Cookie 自动刷新] 检测到 {expired_count} 个过期的账号，开始自动刷新...")
            
            # 导入并调用批量刷新函数
            try:
                import sys
                from pathlib import Path
                # 添加项目根目录到路径
                project_root = Path(__file__).parent.parent
                if str(project_root) not in sys.path:
                    sys.path.insert(0, str(project_root))
                
                from auto_login_with_email import refresh_expired_accounts
                
                # 检查是否强制使用无头/有头模式（通过环境变量）
                import os
                force_headless = os.environ.get('FORCE_HEADLESS') == '1'
                force_headed = os.environ.get('FORCE_HEADED') == '1'
                
                # 确定 headless 模式
                if force_headed:
                    use_headless = False  # 强制有头模式
                elif force_headless:
                    use_headless = True  # 强制无头模式
                else:
                    use_headless = True  # 默认无头模式（后台线程）
                
                refresh_expired_accounts(headless=use_headless)
                
                print("[Cookie 自动刷新] 批量刷新完成")
                
                # 重新加载配置，获取最新的账号状态
                account_manager.load_config()
                
            except ImportError as e:
                print(f"[Cookie 自动刷新] ✗ 导入刷新模块失败: {e}")
                print("    请确保 auto_login_with_email.py 文件存在")
            except Exception as e:
                print(f"[Cookie 自动刷新] ✗ 刷新过程出错: {e}")
                import traceback
                traceback.print_exc()
        
        return expired_count
    
    while True:
        try:
            # 等待立即刷新事件或定期检查时间
            # 使用 wait 的超时功能，既能响应立即刷新，又能定期检查
            event_set = _immediate_refresh_event.wait(timeout=CHECK_INTERVAL)
            
            if event_set:
                # 立即刷新事件被触发，清除事件并立即检查
                _immediate_refresh_event.clear()
                check_count += 1
                print(f"[Cookie 自动刷新] ⚡ 收到立即刷新通知，立即检查过期账号...")
                _check_and_refresh_expired()
                last_check_time = time.time()
            else:
                # 定期检查时间到了
                check_count += 1
                current_time = time.time()
                time_since_last = int(current_time - last_check_time)
                print(f"[Cookie 自动刷新] 第 {check_count} 次定期检查（距上次 {time_since_last} 秒）")
                _check_and_refresh_expired()
                last_check_time = current_time
            
        except KeyboardInterrupt:
            print("[Cookie 自动刷新] 线程被中断")
            break
        except Exception as e:
            print(f"[Cookie 自动刷新] 线程出错: {e}")
            import traceback
            traceback.print_exc()
            # 出错后等待一段时间再继续
            time.sleep(60)
