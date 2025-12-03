"""会话管理模块 - JWT、Session 创建和管理"""

import time
import uuid
import base64
import requests
from typing import Optional, Dict

from .config import CREATE_SESSION_URL, ADD_CONTEXT_FILE_URL
from .account_manager import account_manager
from .jwt_utils import get_jwt_for_account
from .exceptions import AccountRequestError, AccountError
from .utils import raise_for_account_response
from .media_handler import download_image_from_url


def get_headers(jwt: str) -> dict:
    """获取请求头"""
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://business.gemini.google",
        "referer": "https://business.gemini.google/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "x-server-timeout": "1800",
    }


def ensure_jwt_for_account(account_idx: int, account: dict):
    """确保指定账号的JWT有效，必要时刷新"""
    # 调试日志已关闭
    # print(f"[DEBUG][ensure_jwt_for_account] 开始 - 账号索引: {account_idx}, CSESIDX: {account.get('csesidx')}")
    start_time = time.time()
    
    # 先检查是否需要刷新（快速检查，最小化锁持有时间）
    need_refresh = False
    jwt = None
    with account_manager.lock:
        state = account_manager.account_states[account_idx]
        jwt = state.get("jwt")
        jwt_age = time.time() - state["jwt_time"] if jwt else float('inf')
        # 调试日志已关闭
        # print(f"[DEBUG][ensure_jwt_for_account] JWT状态 - 存在: {jwt is not None}, 年龄: {jwt_age:.2f}秒")
        need_refresh = jwt is None or jwt_age > 240
    
    # 如果需要刷新，在锁外进行网络请求（避免长时间阻塞）
    if need_refresh:
        # 调试日志已关闭
        # print(f"[DEBUG][ensure_jwt_for_account] 需要刷新JWT...")
        from .utils import get_proxy
        proxy = get_proxy()
        try:
            refresh_start = time.time()
            new_jwt = get_jwt_for_account(account, proxy, account_idx)  # 网络请求在锁外进行
            # 调试日志已关闭
            # print(f"[DEBUG][ensure_jwt_for_account] JWT刷新成功 - 耗时: {time.time() - refresh_start:.2f}秒")
            
            # 更新状态（重新获取锁）
            with account_manager.lock:
                state = account_manager.account_states[account_idx]
                state["jwt"] = new_jwt
                state["jwt_time"] = time.time()
                # JWT 刷新后，清除旧的 session，因为新的 JWT 与旧的 session 不匹配
                if state["session"] is not None:
                    # 调试日志已关闭
                    # print(f"[DEBUG][ensure_jwt_for_account] JWT已刷新，清除旧session: {state['session']}")
                    state["session"] = None
                jwt = new_jwt
        except Exception as e:
            # 调试日志已关闭
            # print(f"[DEBUG][ensure_jwt_for_account] JWT刷新失败: {e}")
            raise
    else:
        # 调试日志已关闭
        # print(f"[DEBUG][ensure_jwt_for_account] 使用缓存JWT")
        pass
    
    # 调试日志已关闭
    # print(f"[DEBUG][ensure_jwt_for_account] 完成 - 总耗时: {time.time() - start_time:.2f}秒")
    return jwt


def create_chat_session(jwt: str, team_id: str, proxy: str, account_idx: Optional[int] = None) -> str:
    """创建会话，返回session ID"""
    # 调试日志已关闭
    # print(f"[DEBUG][create_chat_session] 开始 - team_id: {team_id}")
    start_time = time.time()
    session_id = uuid.uuid4().hex[:12]
    # 调试日志已关闭
    # print(f"[DEBUG][create_chat_session] 生成session_id: {session_id}")
    body = {
        "configId": team_id,
        "additionalParams": {"token": "-"},
        "createSessionRequest": {
            "session": {"name": session_id, "displayName": session_id}
        }
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None
    # 调试日志已关闭
    # print(f"[DEBUG][create_chat_session] 发送请求到: {CREATE_SESSION_URL}")
    # print(f"[DEBUG][create_chat_session] 使用代理: {proxy}")
    
    request_start = time.time()
    try:
        resp = requests.post(
            CREATE_SESSION_URL,
            headers=get_headers(jwt),
            json=body,
            proxies=proxies,
            verify=False,
            timeout=30
        )
    except requests.RequestException as e:
        raise AccountRequestError(f"创建会话请求失败: {e}") from e
    # 调试日志已关闭
    # print(f"[DEBUG][create_chat_session] 请求完成 - 状态码: {resp.status_code}, 耗时: {time.time() - request_start:.2f}秒")

    if resp.status_code != 200:
        # 调试日志已关闭
        # print(f"[DEBUG][create_chat_session] 请求失败 - 响应: {resp.text[:500]}")
        if resp.status_code == 401:
            # 调试日志已关闭
            # print(f"[DEBUG][create_chat_session] 401错误 - 可能是team_id填错了")
            pass
        raise_for_account_response(resp, "创建会话", account_idx)

    data = resp.json()
    session_name = data.get("session", {}).get("name")
    # 调试日志已关闭
    # print(f"[DEBUG][create_chat_session] 完成 - session_name: {session_name}, 总耗时: {time.time() - start_time:.2f}秒")
    return session_name


def ensure_session_for_account(account_idx: int, account: dict, force_new: bool = False, conversation_id: Optional[str] = None):
    """确保指定账号的会话有效
    
    Args:
        account_idx: 账号索引
        account: 账号信息
        force_new: 是否强制创建新 session（用于新对话）
        conversation_id: 对话标识符（用于区分不同的对话）
    """
    # 调试日志已关闭
    # print(f"[DEBUG][ensure_session_for_account] 开始 - 账号索引: {account_idx}, force_new: {force_new}, conversation_id: {conversation_id}")
    start_time = time.time()
    
    jwt_start = time.time()
    jwt = ensure_jwt_for_account(account_idx, account)
    # 调试日志已关闭
    # print(f"[DEBUG][ensure_session_for_account] JWT获取完成 - 耗时: {time.time() - jwt_start:.2f}秒")
    
    with account_manager.lock:
        # 初始化对话 session 映射
        if account_idx not in account_manager.conversation_sessions:
            account_manager.conversation_sessions[account_idx] = {}
        
        # 如果有对话 ID，尝试使用该对话的 session（除非强制创建新 session）
        if conversation_id and not force_new:
            if conversation_id in account_manager.conversation_sessions[account_idx]:
                session = account_manager.conversation_sessions[account_idx][conversation_id]
                print(f"[检测] ✓ 复用对话 {conversation_id} 的现有 session: {session}")
                # 调试日志已关闭
                # print(f"[DEBUG][ensure_session_for_account] 完成 - 总耗时: {time.time() - start_time:.2f}秒")
                return session, jwt, account.get("team_id")
            else:
                print(f"[检测] ⚠️ 对话 {conversation_id} 没有已存在的 session，将创建新 session（force_new={force_new}）")
        
        state = account_manager.account_states[account_idx]
        # 调试日志已关闭
        # print(f"[DEBUG][ensure_session_for_account] 当前session状态: {state['session'] is not None}")
        
        # 如果需要强制创建新 session，或者当前没有 session，则创建新 session
        if force_new or state["session"] is None:
            if force_new and state["session"] is not None:
                print(f"[检测] ⚠️ 强制创建新 session，旧 session: {state['session']}")
            # 如果强制创建新 session，清除旧的 session 映射（如果有 conversation_id）
            if force_new and conversation_id:
                if conversation_id in account_manager.conversation_sessions[account_idx]:
                    old_session = account_manager.conversation_sessions[account_idx][conversation_id]
                    print(f"[检测] ⚠️ 清除对话 {conversation_id} 的旧 session: {old_session}（原因: force_new=True）")
                    del account_manager.conversation_sessions[account_idx][conversation_id]
            # 调试日志已关闭
            # print(f"[DEBUG][ensure_session_for_account] 需要创建新session...")
            from .utils import get_proxy
            proxy = get_proxy()
            team_id = account.get("team_id")
            session_start = time.time()
            new_session = create_chat_session(jwt, team_id, proxy, account_idx)
            print(f"[检测] ✓ 创建新 session: {new_session}（原因: force_new={force_new}, 旧session存在={state['session'] is not None}）")
            
            # 如果有对话 ID，保存到对话 session 映射中
            if conversation_id:
                account_manager.conversation_sessions[account_idx][conversation_id] = new_session
                print(f"[检测] ✓ 已保存对话 {conversation_id} 的 session: {new_session}")
            
            # 更新默认 session（用于非新对话的情况）
            state["session"] = new_session
            session = new_session
        else:
            # 调试日志已关闭
            # print(f"[DEBUG][ensure_session_for_account] 使用缓存session: {state['session']}")
            session = state["session"]
            # 如果有对话 ID，也保存到映射中（用于后续识别）
            if conversation_id:
                account_manager.conversation_sessions[account_idx][conversation_id] = session
        
        # 调试日志已关闭
        # print(f"[DEBUG][ensure_session_for_account] 完成 - 总耗时: {time.time() - start_time:.2f}秒")
        return state["session"], jwt, account.get("team_id")


def upload_file_to_gemini(jwt: str, session_name: str, team_id: str, 
                          file_content: bytes, filename: str, mime_type: str,
                          proxy: str = None, account_idx: Optional[int] = None) -> str:
    """
    上传文件到 Gemini，返回 Gemini 的 fileId
    
    Args:
        jwt: JWT 认证令牌
        session_name: 会话名称
        team_id: 团队ID
        file_content: 文件内容（字节）
        filename: 文件名
        mime_type: MIME 类型
        proxy: 代理地址
    
    Returns:
        str: Gemini 返回的 fileId
    """
    import base64
    
    start_time = time.time()
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] 开始上传文件: {filename}, MIME类型: {mime_type}, 文件大小: {len(file_content)} bytes")
    
    encode_start = time.time()
    file_contents_b64 = base64.b64encode(file_content).decode('utf-8')
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] Base64编码完成 - 耗时: {time.time() - encode_start:.2f}秒, 编码后大小: {len(file_contents_b64)} chars")
    
    body = {
        "addContextFileRequest": {
            "fileContents": file_contents_b64,
            "fileName": filename,
            "mimeType": mime_type,
            "name": session_name
        },
        "additionalParams": {"token": "-"},
        "configId": team_id
    }
    
    proxies = {"http": proxy, "https": proxy} if proxy else None
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] 准备发送请求到: {ADD_CONTEXT_FILE_URL}")
    # print(f"[DEBUG][upload_file_to_gemini] 使用代理: {proxy if proxy else '无'}")
    
    request_start = time.time()
    try:
        resp = requests.post(
            ADD_CONTEXT_FILE_URL,
            headers=get_headers(jwt),
            json=body,
            proxies=proxies,
            verify=False,
            timeout=60
        )
    except requests.RequestException as e:
        raise AccountRequestError(f"文件上传请求失败: {e}") from e
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] 请求完成 - 耗时: {time.time() - request_start:.2f}秒, 状态码: {resp.status_code}")
    
    if resp.status_code != 200:
        # 调试日志已关闭
        # print(f"[DEBUG][upload_file_to_gemini] 上传失败 - 响应内容: {resp.text[:500]}")
        raise_for_account_response(resp, "文件上传", account_idx)
    
    parse_start = time.time()
    data = resp.json()
    file_id = data.get("addContextFileResponse", {}).get("fileId")
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] 解析响应完成 - 耗时: {time.time() - parse_start:.2f}秒")
    
    if not file_id:
        # 调试日志已关闭
        # print(f"[DEBUG][upload_file_to_gemini] 响应中未找到fileId - 响应数据: {data}")
        raise ValueError(f"响应中未找到 fileId: {data}")
    
    # 调试日志已关闭
    # print(f"[DEBUG][upload_file_to_gemini] 上传成功 - fileId: {file_id}, 总耗时: {time.time() - start_time:.2f}秒")
    return file_id


def build_download_url(session_name: str, file_id: str) -> str:
    """构造正确的下载URL"""
    return f"https://biz-discoveryengine.googleapis.com/v1alpha/{session_name}:downloadFile?fileId={file_id}&alt=media"


def upload_inline_image_to_gemini(jwt: str, session_name: str, team_id: str, 
                                   image_data: Dict, proxy: str = None, account_idx: Optional[int] = None) -> Optional[str]:
    """上传内联图片到 Gemini，返回 fileId"""
    try:
        ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
        
        if image_data.get("type") == "base64":
            mime_type = image_data.get("mime_type", "image/png")
            file_content = base64.b64decode(image_data.get("data", ""))
            ext = ext_map.get(mime_type, ".png")
            filename = f"inline_{uuid.uuid4().hex[:8]}{ext}"
        elif image_data.get("type") == "url":
            file_content, mime_type = download_image_from_url(image_data.get("url"), proxy)
            ext = ext_map.get(mime_type, ".png")
            filename = f"url_{uuid.uuid4().hex[:8]}{ext}"
        else:
            return None
        
        return upload_file_to_gemini(jwt, session_name, team_id, file_content, filename, mime_type, proxy, account_idx)
    except AccountError:
        # 让账号相关错误向上抛出，以便触发冷却
        raise
    except Exception:
        return None

