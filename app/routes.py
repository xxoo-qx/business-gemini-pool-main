"""Flask 路由模块
包含所有 API 端点和页面路由
"""

import json
import time
import uuid
import hashlib
import mimetypes
import re
import secrets
import traceback
import base64
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path

from flask import request, Response, jsonify, send_from_directory, abort, redirect, render_template

# 导入 WebSocket 管理器
from .websocket_manager import (
    emit_account_update,
    emit_cookie_refresh_progress,
    emit_system_log,
    emit_stats_update,
    emit_notification
)

# 导入配置和常量
from .config import IMAGE_CACHE_DIR, VIDEO_CACHE_DIR, CONFIG_FILE, PLAYWRIGHT_AVAILABLE, PLAYWRIGHT_BROWSER_INSTALLED

# 导入账号管理和文件管理
from .account_manager import account_manager
from .file_manager import file_manager

# 导入认证装饰器
from .auth import (
    require_api_auth,
    require_admin,
    is_admin_authenticated,
    get_admin_password_hash,
    set_admin_password,
    create_admin_token,
    get_admin_secret_key
)
from . import auth

# 导入会话管理
from .session_manager import ensure_session_for_account, ensure_jwt_for_account, upload_file_to_gemini, upload_inline_image_to_gemini

# 导入聊天处理
from .chat_handler import (
    stream_chat_with_images,
    stream_chat_realtime_generator,
    build_openai_response_content,
    get_image_base_url
)

# 导入媒体处理
from .media_handler import (
    cleanup_expired_images,
    cleanup_expired_videos,
    extract_images_from_openai_content,
    extract_images_from_files_array
)

# 导入 Cookie 刷新
from .cookie_refresh import auto_refresh_account_cookie

# 导入 JWT 工具
from .jwt_utils import get_jwt_for_account

# 导入工具函数
from .utils import check_proxy, seconds_until_next_pt_midnight

# 导入异常类
from .exceptions import (
    AccountRateLimitError,
    AccountAuthError,
    AccountRequestError,
    NoAvailableAccount
)

# 导入日志
from .logger import set_log_level, CURRENT_LOG_LEVEL_NAME, LOG_LEVELS, print


def register_routes(app):
    """注册所有路由到 Flask 应用"""
    
    # ==================== OpenAPI 接口 ====================
    
    @app.route('/v1/models', methods=['GET'])
    @require_api_auth
    def list_models():
        """获取模型列表"""
        models_config = account_manager.config.get("models", [])
        models_data = []
        
        for model in models_config:
            models_data.append({
                "id": model.get("id", "gemini-enterprise"),
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "permission": [],
                "root": model.get("id", "gemini-enterprise"),
                "parent": None
            })
        
        if not models_data:
            models_data.append({
                "id": "gemini-enterprise",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "permission": [],
                "root": "gemini-enterprise",
                "parent": None
            })
        
        if not any(model["id"] == "auto" for model in models_data):
            models_data.append({
                "id": "auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "permission": [],
                "root": "auto",
                "parent": None
            })
        
        return jsonify({"object": "list", "data": models_data})
    
    @app.route('/v1/files', methods=['POST'])
    @require_api_auth
    def upload_file():
        """OpenAI 兼容的文件上传接口"""
        request_start_time = time.time()
        print(f"\n{'='*60}")
        print(f"[文件上传] ===== 接口调用开始 =====")
        print(f"[文件上传] 请求时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        try:
            if 'file' not in request.files:
                return jsonify({"error": {"message": "No file provided", "type": "invalid_request_error"}}), 400
            
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": {"message": "No file selected", "type": "invalid_request_error"}}), 400
            
            file_content = file.read()
            mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'
            
            available_accounts = account_manager.get_available_accounts()
            if not available_accounts:
                next_cd = account_manager.get_next_cooldown_info()
                wait_msg = ""
                if next_cd:
                    wait_msg = f"（最近冷却账号 {next_cd['index']}，约 {int(next_cd['cooldown_until']-time.time())} 秒后可重试）"
                return jsonify({"error": {"message": f"没有可用的账号{wait_msg}", "type": "rate_limit"}}), 429

            max_retries = len(available_accounts)
            last_error = None
            gemini_file_id = None
            
            for retry_idx in range(max_retries):
                account_idx = None
                try:
                    account_idx, account = account_manager.get_next_account()
                    session, jwt, team_id = ensure_session_for_account(account_idx, account)
                    from .utils import get_proxy
                    proxy = get_proxy()
                    gemini_file_id = upload_file_to_gemini(jwt, session, team_id, file_content, file.filename, mime_type, proxy)
                    
                    if gemini_file_id:
                        openai_file_id = f"file-{uuid.uuid4().hex[:24]}"
                        file_manager.add_file(
                            openai_file_id=openai_file_id,
                            gemini_file_id=gemini_file_id,
                            session_name=session,
                            filename=file.filename,
                            mime_type=mime_type,
                            size=len(file_content)
                        )
                        return jsonify({
                            "id": openai_file_id,
                            "object": "file",
                            "bytes": len(file_content),
                            "created_at": int(time.time()),
                            "filename": file.filename,
                            "purpose": request.form.get('purpose', 'assistants')
                        })
                
                except AccountRateLimitError as e:
                    last_error = e
                    if account_idx is not None:
                        pt_wait = seconds_until_next_pt_midnight()
                        cooldown_seconds = max(account_manager.rate_limit_cooldown, pt_wait)
                        account_manager.mark_account_cooldown(account_idx, str(e), cooldown_seconds)
                    continue
                except AccountAuthError as e:
                    last_error = e
                    if account_idx is not None:
                        error_msg = str(e).lower()
                        if "session is not owned" in error_msg or "not owned by the provided user" in error_msg:
                            with account_manager.lock:
                                state = account_manager.account_states.get(account_idx)
                                if state and state.get("session"):
                                    state["session"] = None
                        account_manager.mark_account_unavailable(account_idx, str(e))
                        account_manager.mark_account_cooldown(account_idx, str(e), account_manager.auth_error_cooldown)
                    continue
                except AccountRequestError as e:
                    last_error = e
                    if account_idx is not None:
                        account_manager.mark_account_cooldown(account_idx, str(e), account_manager.generic_error_cooldown)
                    continue
                except NoAvailableAccount as e:
                    last_error = e
                    break
                except Exception as e:
                    last_error = e
                    if account_idx is None:
                        break
                    continue
            
            status_code = 429 if isinstance(last_error, (AccountRateLimitError, NoAvailableAccount)) else 500
            err_type = "rate_limit" if status_code == 429 else "api_error"
            return jsonify({"error": {"message": f"文件上传失败: {last_error or '没有可用的账号'}", "type": err_type}}), status_code
            
        except Exception as e:
            return jsonify({"error": {"message": str(e), "type": "api_error"}}), 500
    
    @app.route('/v1/files', methods=['GET'])
    @require_api_auth
    def list_files():
        """获取已上传文件列表"""
        files = file_manager.list_files()
        return jsonify({
            "object": "list",
            "data": [{
                "id": f["openai_file_id"],
                "object": "file",
                "bytes": f.get("size", 0),
                "created_at": f.get("created_at", int(time.time())),
                "filename": f.get("filename", ""),
                "purpose": "assistants"
            } for f in files]
        })
    
    @app.route('/v1/files/<file_id>', methods=['GET'])
    @require_api_auth
    def get_file(file_id):
        """获取文件信息"""
        file_info = file_manager.get_file(file_id)
        if not file_info:
            return jsonify({"error": {"message": "File not found", "type": "invalid_request_error"}}), 404
        
        return jsonify({
            "id": file_info["openai_file_id"],
            "object": "file",
            "bytes": file_info.get("size", 0),
            "created_at": file_info.get("created_at", int(time.time())),
            "filename": file_info.get("filename", ""),
            "purpose": "assistants"
        })
    
    @app.route('/v1/files/<file_id>', methods=['DELETE'])
    @require_api_auth
    def delete_file_route(file_id):
        """删除文件"""
        if file_manager.delete_file(file_id):
            return jsonify({
                "id": file_id,
                "object": "file",
                "deleted": True
            })
        return jsonify({"error": {"message": "File not found", "type": "invalid_request_error"}}), 404
    
    @app.route('/v1/chat/completions', methods=['POST'])
    @require_api_auth
    def chat_completions():
        """聊天对话接口（支持图片输入输出）"""
        # 记录 API 调用日志
        request_start_time = time.time()
        api_key_id = None
        requested_model = None  # 初始化，避免后续引用错误
        token = (
            request.headers.get("X-API-Token")
            or request.headers.get("Authorization", "").replace("Bearer ", "")
            or request.cookies.get("admin_token")
        )
        if token:
            from .auth import get_api_key_from_token
            api_key_obj = get_api_key_from_token(token)
            if api_key_obj:
                api_key_id = api_key_obj.id
        
        ip_address = request.remote_addr
        endpoint = "/v1/chat/completions"
        request_size = len(request.data) if request.data else 0
        
        try:
            cleanup_expired_images()
            cleanup_expired_videos()
            
            data = request.json
            requested_model = data.get('model', 'gemini-enterprise')  # 更新 requested_model
            auto_model_aliases = {"auto", "local-gemini-auto"}
            is_auto_model = requested_model in auto_model_aliases
            messages = data.get('messages', [])
            prompts = data.get('prompts', [])
            stream = data.get('stream', False)
            
            models_config = account_manager.config.get("models", [])
            selected_model_config = None
            if is_auto_model:
                selected_model_config = {
                    "id": requested_model,
                    "name": "Gemini Auto",
                    "description": "自动路由到最合适的 Gemini 模型",
                    "api_model_id": None,
                    "enabled": True
                }
            elif models_config:
                model_ids = [m.get("id") for m in models_config]
                for model in models_config:
                    if model.get("id") == requested_model:
                        selected_model_config = model
                        break
                if not selected_model_config:
                    if model_ids:
                        requested_model = model_ids[0]
                        selected_model_config = models_config[0]
                    else:
                        requested_model = "gemini-enterprise"
            
            video_identifiers = [requested_model or ""]
            if selected_model_config:
                video_identifiers.extend([
                    selected_model_config.get("id", ""),
                    selected_model_config.get("name", ""),
                    str(selected_model_config.get("api_model_id", ""))
                ])
            is_video_model = any("video" in (identifier or "").lower() for identifier in video_identifiers)
            
            user_message = ""
            input_images = []
            input_file_ids = []
            
            def extract_user_query(text: str) -> str:
                match = re.search(r'<user_query>(.*?)</user_query>', text, re.DOTALL)
                if match:
                    return match.group(1).strip()
                return text
            
            for msg in messages:
                if msg.get('role') == 'user':
                    content = msg.get('content', '')
                    text, images = extract_images_from_openai_content(content)
                    if text:
                        user_message = extract_user_query(text)
                    input_images.extend(images)
                    
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if item.get('type') == 'file' and item.get('file_id'):
                                    input_file_ids.append(item['file_id'])
                                elif item.get('type') == 'file' and isinstance(item.get('file'), dict):
                                    file_obj = item['file']
                                    fid = file_obj.get('file_id') or file_obj.get('id')
                                    if fid:
                                        input_file_ids.append(fid)
            
            for prompt in prompts:
                if prompt.get('role') == 'user':
                    prompt_text = prompt.get('text', '')
                    if prompt_text and not user_message:
                        user_message = prompt_text
                    elif prompt_text:
                        user_message = prompt_text
                    
                    files_array = prompt.get('files', [])
                    if files_array:
                        images_from_files = extract_images_from_files_array(files_array)
                        input_images.extend(images_from_files)
            
            gemini_file_ids = []
            file_sessions = []  # 记录文件关联的 session，用于后续复用会话
            for fid in input_file_ids:
                if not fid:
                    continue
                
                # 判断 file_id 格式：
                # 1. 如果是纯数字（可能是 Gemini 的 fileId），直接使用
                # 2. 如果是 OpenAI 格式（file-xxx），通过 file_manager 转换
                # 3. 如果都不是，尝试通过 file_manager 转换（兼容其他格式）
                if fid.isdigit():
                    # 纯数字，可能是 Gemini 的 fileId，直接使用
                    gemini_file_ids.append(fid)
                elif fid.startswith('file-'):
                    # OpenAI 格式，通过 file_manager 转换
                    file_info = file_manager.get_file(fid)
                    if file_info:
                        gemini_fid = file_info.get("gemini_file_id")
                        file_session = file_info.get("session_name")
                        if gemini_fid:
                            gemini_file_ids.append(gemini_fid)
                        if file_session:
                            file_sessions.append(file_session)
                            print(f"[检测] 📎 文件 {fid} 关联的 session: {file_session}")
                    else:
                        print(f"[警告] 文件ID {fid} 在文件管理器中未找到，可能已过期或不存在")
                else:
                    # 其他格式，尝试通过 file_manager 转换
                    file_info = file_manager.get_file(fid)
                    if file_info:
                        gemini_fid = file_info.get("gemini_file_id")
                        file_session = file_info.get("session_name")
                        if gemini_fid:
                            gemini_file_ids.append(gemini_fid)
                        if file_session:
                            file_sessions.append(file_session)
                            print(f"[检测] 📎 文件 {fid} 关联的 session: {file_session}")
                    else:
                        # 如果转换失败，假设是 Gemini fileId（兼容性处理）
                        print(f"[警告] 文件ID {fid} 格式未知，尝试直接使用（可能是 Gemini fileId）")
                        gemini_file_ids.append(fid)
            
            if not user_message and not input_images and not gemini_file_ids:
                return jsonify({"error": "No user message found"}), 400
            
            available_accounts = account_manager.get_available_accounts()
            if not available_accounts:
                next_cd = account_manager.get_next_cooldown_info()
                wait_msg = ""
                if next_cd:
                    wait_msg = f"（最近冷却账号 {next_cd['index']}，约 {int(next_cd['cooldown_until']-time.time())} 秒后可重试）"
                return jsonify({"error": f"没有可用的账号{wait_msg}"}), 429

            max_retries = len(available_accounts)
            last_error = None
            chat_response = None
            successful_account_idx = None
            
            # 优先使用前端传递的 conversation_id 和 is_new_conversation
            conversation_id = data.get('conversation_id')
            is_new_conversation = data.get('is_new_conversation', False)
            
            # 检测是否有图片输入
            has_images = bool(input_images or input_file_ids or gemini_file_ids)
            
            # 如果前端没有传递 conversation_id，则根据消息内容自动生成
            # 注意：对于其他客户端（如 Cursor、Cherry Studio），如果没有传递 conversation_id，
            # 我们使用第一条用户消息内容生成稳定的 ID，确保同一对话的后续请求使用相同的 ID
            content = ""  # 初始化 content 变量，用于后续检查
            if not conversation_id and messages:
                user_count = sum(1 for msg in messages if msg.get('role') == 'user')
                assistant_count = sum(1 for msg in messages if msg.get('role') == 'assistant')
                system_count = sum(1 for msg in messages if msg.get('role') == 'system')
                total_count = len(messages)
                last_is_user = messages and messages[-1].get('role') == 'user'
                first_user_msg = next((msg for msg in messages if msg.get('role') == 'user'), None)
                
                # 判断是否为新对话：只有第一条用户消息且没有 assistant 回复才是新对话
                # 如果有多条消息或已有 assistant 回复，说明是继续对话
                is_new_conversation = (user_count == 1 and assistant_count == 0 and total_count == 1)
                
                if first_user_msg:
                    # 按照 Gemini-Link-System 的逻辑：只使用第一条消息的文本部分生成会话键
                    # 完全忽略图片，因为图片上传到 session 后会持久化，不需要参与会话键计算
                    raw_content = first_user_msg.get('content', '')
                    content = ""
                    
                    # 如果内容是数组（包含文件等），只提取文本部分，完全忽略图片
                    if isinstance(raw_content, list):
                        text_parts = []
                        for item in raw_content:
                            if isinstance(item, dict) and item.get('type') == 'text':
                                text_parts.append(str(item.get('text', '')))
                        # 只使用文本部分，忽略所有图片/文件
                        content = '|'.join(text_parts) if text_parts else ""
                    else:
                        # 如果是字符串，需要移除 base64 图片数据
                        content_str = str(raw_content)
                        # 移除 base64 图片数据 URL
                        base64_pattern = r'data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+'
                        text_only = re.sub(base64_pattern, '', content_str, flags=re.MULTILINE)
                        # 清理多余的空白字符
                        content = re.sub(r'\s+', ' ', text_only).strip()
                    
                    # 如果内容为空，使用 "empty"（类似 Gemini-Link-System）
                    if not content:
                        content = "empty"
                    
                    # 生成 conversation_id（使用 MD5，类似 Gemini-Link-System）
                    generated_id = hashlib.md5(content.encode('utf-8')).hexdigest()[:16]
                    conversation_id = generated_id
                    
                    # 打印 conversation_id 生成内容（用于调试）
                    print(f"[检测] 🔍 conversation_id 生成内容: {content[:200]}... (长度: {len(content)}, has_images={has_images})")
                    print(f"[检测] 🔍 conversation_id MD5 结果: {conversation_id}")
                
                if is_new_conversation:
                    print(f"[聊天] 检测到新对话（user={user_count}, assistant={assistant_count}, system={system_count}, total={total_count}），对话ID: {conversation_id}，将创建新的 session")
                    if has_images:
                        print(f"[检测] ⚠️ 新对话包含图片输入（input_images={len(input_images)}, input_file_ids={len(input_file_ids)}, gemini_file_ids={len(gemini_file_ids)}）")
                elif conversation_id:
                    print(f"[聊天] 继续对话，对话ID: {conversation_id}")
                    if has_images:
                        print(f"[检测] ℹ️ 继续对话包含图片输入（input_images={len(input_images)}, input_file_ids={len(input_file_ids)}, gemini_file_ids={len(gemini_file_ids)}）")
            elif conversation_id:
                print(f"[聊天] 使用前端传递的对话ID: {conversation_id}, 新对话: {is_new_conversation}")
                if has_images:
                    print(f"[检测] ℹ️ 前端传递的对话包含图片输入（input_images={len(input_images)}, input_file_ids={len(input_file_ids)}, gemini_file_ids={len(gemini_file_ids)}），is_new_conversation={is_new_conversation}")
            
            preferred_account_idx = None
            if selected_model_config and "account_index" in selected_model_config:
                preferred_account_idx = selected_model_config.get("account_index")
                if preferred_account_idx >= 0 and preferred_account_idx < len(account_manager.accounts):
                    if account_manager.is_account_available(preferred_account_idx):
                        preferred_account_idx = preferred_account_idx
                    else:
                        preferred_account_idx = None
            
            try_without_model_id = is_auto_model
            
            # 检测是否是图片生成请求
            is_image_model = selected_model_config and selected_model_config.get("id") == "gemini-image"
            # 如果使用默认工具集，也可能生成图片，需要检查图片配额
            # 但为了性能，只在明确是图片模型时检查，普通模型在生成图片后再检查
            
            for retry_idx in range(max_retries):
                account_idx = None
                try:
                    # 被动检测方式：根据请求类型选择对应配额类型可用的账号
                    required_quota_type = None
                    if is_image_model:
                        required_quota_type = "images"
                    elif is_video_model:
                        required_quota_type = "videos"
                    # 文本查询不需要指定配额类型（因为所有请求都需要文本配额）
                    
                    if preferred_account_idx is not None and retry_idx == 0:
                        account = account_manager.accounts[preferred_account_idx]
                        account_idx = preferred_account_idx
                        # 检查首选账号的配额类型是否可用
                        if required_quota_type and not account_manager.is_account_available(account_idx, required_quota_type):
                            preferred_account_idx = None
                            account_idx, account = account_manager.get_next_account(required_quota_type)
                    else:
                        # 根据请求类型选择对应配额类型可用的账号
                        account_idx, account = account_manager.get_next_account(required_quota_type)
                    
                    # ⚠️ 特殊处理：如果当前请求是新对话且有文本（不是 "empty"），
                    # 检查是否有 "empty" 会话键的 session（可能是之前只有图片的请求创建的）
                    # 如果有，复用该 session，确保图片在同一个 session 中可见
                    should_check_empty_session = (
                        is_new_conversation and  # 是新对话
                        account_idx is not None and  # 已选择账号
                        not data.get('conversation_id') and  # 客户端没有传递 conversation_id
                        content and  # 有文本内容
                        content != "empty"  # 不是 "empty"
                    )
                    
                    if should_check_empty_session:
                        # 计算 "empty" 的 conversation_id
                        empty_id = hashlib.md5("empty".encode('utf-8')).hexdigest()[:16]
                        with account_manager.lock:
                            if account_idx in account_manager.conversation_sessions:
                                if empty_id in account_manager.conversation_sessions[account_idx]:
                                    existing_session = account_manager.conversation_sessions[account_idx][empty_id]
                                    print(f"[检测] 🔍 找到已存在的'只有图片'的 session: {existing_session} (conversation_id={empty_id})")
                                    print(f"[检测] 🔍 将使用该 session 而不是创建新的 (原 conversation_id={conversation_id})")
                                    # 使用已存在的 session 的 conversation_id
                                    conversation_id = empty_id
                                    is_new_conversation = False  # 不是新对话，是继续对话
                    
                    # 按照 Gemini-Link-System 的逻辑：
                    # 图片上传到 session 后会持久化，不需要特殊处理
                    # 如果找到缓存的 session，直接复用，图片已经在 session 中了
                    
                    # 调试：检测图片输入时的会话创建
                    if has_images and is_new_conversation:
                        print(f"[检测] ⚠️ 图片输入 + 新对话：force_new=True, conversation_id={conversation_id}")
                    elif has_images and not is_new_conversation:
                        print(f"[检测] ℹ️ 图片输入 + 继续对话：force_new=False, conversation_id={conversation_id}")
                    
                    # ⚠️ 重要：如果使用了 file_id，且文件关联了 session，应该使用该 session
                    # 而不是创建新的 session，否则文件在旧 session 中，聊天在新 session 中，会看不到文件
                    use_file_session = None
                    if file_sessions and len(file_sessions) > 0:
                        # 使用第一个文件的 session（如果所有文件都在同一个 session 中）
                        use_file_session = file_sessions[0]
                        # 检查是否所有文件都在同一个 session 中
                        if len(set(file_sessions)) > 1:
                            print(f"[警告] ⚠️ 多个文件关联了不同的 session: {set(file_sessions)}，将使用第一个: {use_file_session}")
                        print(f"[检测] 📎 检测到文件关联的 session: {use_file_session}，将使用该 session 进行聊天")
                    
                    if use_file_session:
                        # 使用文件关联的 session，而不是创建新的
                        jwt = ensure_jwt_for_account(account_idx, account)
                        session = use_file_session
                        team_id = account.get("team_id")
                        print(f"[检测] ✓ 使用文件关联的 session: {session}（跳过会话创建）")
                    else:
                        # 正常创建或复用 session
                        session, jwt, team_id = ensure_session_for_account(account_idx, account, force_new=is_new_conversation, conversation_id=conversation_id)
                    from .utils import get_proxy
                    proxy = get_proxy()
                    
                    # 按照 Gemini-Link-System 的逻辑：如果有图片且还没上传到当前 Session，先上传
                    # 注意：如果 session 是复用的，图片可能已经在 session 中了，但这次请求有新的图片，需要上传
                    if input_images:
                        for img in input_images:
                            uploaded_file_id = upload_inline_image_to_gemini(jwt, session, team_id, img, proxy, account_idx)
                            if uploaded_file_id:
                                gemini_file_ids.append(uploaded_file_id)
                                # 保存文件到 file_manager，关联 session（用于后续复用）
                                if file_manager:
                                    # 从图片数据中获取信息
                                    mime_type = img.get("mime_type", "image/png")
                                    if img.get("type") == "base64":
                                        # 计算 base64 数据的大小
                                        data = img.get("data", "")
                                        size = len(base64.b64decode(data)) if data else 0
                                    elif img.get("type") == "url":
                                        # URL 类型，无法直接获取大小，使用 0
                                        size = 0
                                    else:
                                        size = 0
                                    
                                    # 生成文件名
                                    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
                                    ext = ext_map.get(mime_type, ".png")
                                    filename = f"inline_{uploaded_file_id}{ext}"
                                    
                                    file_manager.add_file(
                                        openai_file_id=f"file-{uploaded_file_id}",  # 使用 OpenAI 格式的 file_id
                                        gemini_file_id=uploaded_file_id,
                                        session_name=session,
                                        filename=filename,
                                        mime_type=mime_type,
                                        size=size
                                    )
                    
                    api_model_id = None
                    if selected_model_config and not try_without_model_id:
                        api_model_id = selected_model_config.get("api_model_id")
                        if api_model_id is None or api_model_id == "null" or api_model_id == "":
                            api_model_id = None
                    
                    # 确定配额类型（用于错误检测时的按类型冷却）
                    request_quota_type = None
                    if is_image_model:
                        request_quota_type = "images"
                    elif is_video_model:
                        request_quota_type = "videos"
                    # 文本查询不需要指定配额类型（429 错误时冷却整个账号）
                    
                    # ✅ 流式模式：使用真正的流式生成器（边接收边解析边转发）
                    # 非流式模式：使用原来的函数（先收集完整响应再返回）
                    if stream:
                        # 准备流式生成器的参数
                        chat_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
                        created_ts = int(time.time())
                        
                        # 使用真正的流式生成器
                        stream_generator = stream_chat_realtime_generator(
                            jwt, session, user_message, proxy, team_id, 
                            gemini_file_ids, api_model_id, account_manager, 
                            account_idx, request_quota_type,
                            chat_id=chat_id, created=created_ts, model_name=requested_model,
                            host_url=request.host_url
                        )
                        successful_account_idx = account_idx
                        # 流式响应将在下面的 if stream 块中处理
                        chat_response = None  # 流式模式下不需要完整响应
                        break
                    else:
                        # 非流式模式：使用原来的函数
                        chat_response = stream_chat_with_images(jwt, session, user_message, proxy, team_id, gemini_file_ids, api_model_id, account_manager, account_idx, request_quota_type)
                        successful_account_idx = account_idx
                        break
                except AccountRateLimitError as e:
                    last_error = e
                    if account_idx is not None:
                        pt_wait = seconds_until_next_pt_midnight()
                        cooldown_seconds = max(account_manager.rate_limit_cooldown, pt_wait)
                        account_manager.mark_account_cooldown(account_idx, str(e), cooldown_seconds)
                    continue
                except AccountAuthError as e:
                    last_error = e
                    if account_idx is not None:
                        error_msg = str(e).lower()
                        if "session is not owned" in error_msg or "not owned by the provided user" in error_msg:
                            with account_manager.lock:
                                state = account_manager.account_states.get(account_idx)
                                if state and state.get("session"):
                                    state["session"] = None
                        account_manager.mark_account_unavailable(account_idx, str(e))
                        account_manager.mark_account_cooldown(account_idx, str(e), account_manager.auth_error_cooldown)
                    continue
                except AccountRequestError as e:
                    last_error = e
                    error_str = str(e).lower()
                    
                    # 检查是否是文件不存在的错误
                    if "file" in error_str and ("not found" in error_str or "404" in error_str):
                        # 文件不存在错误，提供更友好的提示
                        # 尝试从错误消息中提取 fileId（re 模块已在文件顶部导入）
                        file_id_match = re.search(r'File with ID "([^"]+)"', str(e))
                        if file_id_match:
                            file_id = file_id_match.group(1)
                            error_msg = f"文件不存在或已过期（File ID: {file_id}）。请重新上传文件或使用有效的文件ID。"
                        else:
                            error_msg = f"文件不存在或已过期。请重新上传文件。错误详情: {str(e)}"
                        
                        # 文件不存在不应该导致账号冷却，直接返回错误
                        return jsonify({
                            "error": {
                                "message": error_msg,
                                "type": "invalid_request_error",
                                "code": "file_not_found"
                            }
                        }), 400
                    
                    if "500" in error_str or "internal error" in error_str:
                        cooldown_time = 30
                        if account_idx is not None:
                            with account_manager.lock:
                                state = account_manager.account_states.get(account_idx)
                                if state and state.get("session"):
                                    state["session"] = None
                                if account_idx in account_manager.conversation_sessions:
                                    account_manager.conversation_sessions[account_idx] = {}
                        try_without_model_id = True
                    else:
                        cooldown_time = account_manager.generic_error_cooldown
                    
                    if account_idx is not None:
                        account_manager.mark_account_cooldown(account_idx, str(e), cooldown_time)
                    continue
                except Exception as e:
                    last_error = e
                    if account_idx is None:
                        break
                    continue
            
            # ✅ 流式模式：直接使用流式生成器
            if stream:
                # 检查是否有流式生成器（在循环中已创建）
                if 'stream_generator' not in locals():
                    error_message = last_error or "没有可用的账号"
                    status_code = 429 if isinstance(last_error, (AccountRateLimitError, NoAvailableAccount)) else 500
                    return jsonify({"error": f"所有账号请求失败: {error_message}"}), status_code
                
                def generate():
                    try:
                        # 使用真正的流式生成器，实时转发
                        for chunk in stream_generator:
                            yield chunk
                        
                        # 流式生成器结束后，处理图片/视频（需要下载）
                        # 注意：stream_chat_realtime_generator 返回 ChatResponse 对象
                        # 但我们需要手动获取它，这里暂时跳过图片处理（图片会在生成器中处理）
                        
                        # 发送结束标记
                        end_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": requested_model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop"
                            }]
                        }
                        yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                    except Exception as e:
                        # 错误处理
                        error_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": requested_model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop"
                            }],
                            "error": {"message": str(e)}
                        }
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                
                # 对于流式响应，在开始时就记录日志
                response_time = int((time.time() - request_start_time) * 1000)
                try:
                    from .api_key_manager import log_api_call
                    log_api_call(
                        api_key_id=api_key_id,
                        model=requested_model,
                        status="success",
                        response_time=response_time,
                        ip_address=ip_address,
                        endpoint=endpoint,
                        request_size=request_size,
                        response_size=None
                    )
                except Exception:
                    pass
                
                return Response(generate(), mimetype='text/event-stream')
            
            # 非流式模式：使用原来的逻辑
            if chat_response is None:
                error_message = last_error or "没有可用的账号"
                status_code = 429 if isinstance(last_error, (AccountRateLimitError, NoAvailableAccount)) else 500
                return jsonify({"error": f"所有账号请求失败: {error_message}"}), status_code

            # 被动检测方式：不再主动记录配额使用量
            # 配额错误会通过 HTTP 错误码（401, 403, 429）被动检测，并在 raise_for_account_response 中处理

            response_content = build_openai_response_content(chat_response, request.host_url, account_manager, request, data)

            if False:  # 原来的流式逻辑已移到上面
                def generate():
                    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
                    
                    # 如果 response_content 是数组（包含图片），需要分别发送文本和图片
                    if isinstance(response_content, list):
                        # 先发送文本部分
                        text_parts = [item for item in response_content if item.get("type") == "text"]
                        if text_parts:
                            text_content = " ".join(item.get("text", "") for item in text_parts)
                            if text_content.strip():
                                # 分块发送文本
                                words = text_content.split(" ")
                                for i, word in enumerate(words):
                                    chunk = {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": requested_model,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": word + (" " if i < len(words) - 1 else "")},
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        
                        # 然后发送图片/视频部分
                        # 注意：流式响应中 delta.content 必须是字符串，不能是对象
                        # 将图片 URL 作为字符串发送，这样兼容性更好（chat_history.html 可以通过正则识别）
                        media_parts = [item for item in response_content if item.get("type") == "image_url"]
                        for media_item in media_parts:
                            image_url = media_item.get("image_url", {}).get("url", "")
                            if image_url:
                                # 将图片 URL 作为字符串发送（换行分隔，便于 chat_history.html 识别）
                                # 使用换行符分隔，这样 chat_history.html 的 parseContentWithMedia 函数可以通过正则识别
                                image_url_text = f"\n{image_url}\n"
                                image_chunk = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": requested_model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": image_url_text},
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(image_chunk, ensure_ascii=False)}\n\n"
                    else:
                        # 纯文本，分块发送
                        if response_content and response_content.strip():
                            words = response_content.split(" ")
                            for i, word in enumerate(words):
                                chunk = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": requested_model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": word + (" " if i < len(words) - 1 else "")},
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 发送结束标记
                    end_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": requested_model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                
                # 对于流式响应，在开始时就记录日志（响应大小无法准确计算）
                response_time = int((time.time() - request_start_time) * 1000)
                try:
                    from .api_key_manager import log_api_call
                    log_api_call(
                        api_key_id=api_key_id,
                        model=requested_model,
                        status="success",
                        response_time=response_time,
                        ip_address=ip_address,
                        endpoint=endpoint,
                        request_size=request_size,
                        response_size=None  # 流式响应大小无法准确计算
                    )
                except Exception:
                    pass  # 日志记录失败不应影响主流程

                return Response(generate(), mimetype='text/event-stream')
            else:
                # 非流式响应：response_content 可能是字符串或数组
                response = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": requested_model,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_content  # 可以是字符串或数组
                        },
                        "finish_reason": "stop"
                    }],
                    "usage": {
                        "prompt_tokens": len(user_message),
                        "completion_tokens": len(chat_response.text),
                        "total_tokens": len(user_message) + len(chat_response.text)
                    }
                }
                # 记录成功日志
                response_time = int((time.time() - request_start_time) * 1000)
                response_size = len(json.dumps(response, ensure_ascii=False).encode())
                try:
                    from .api_key_manager import log_api_call
                    log_api_call(
                        api_key_id=api_key_id,
                        model=requested_model,
                        status="success",
                        response_time=response_time,
                        ip_address=ip_address,
                        endpoint=endpoint,
                        request_size=request_size,
                        response_size=response_size
                    )
                except Exception:
                    pass  # 日志记录失败不应影响主流程
                
                return jsonify(response)

        except Exception as e:
            # 记录失败日志
            response_time = int((time.time() - request_start_time) * 1000)
            error_message = str(e)[:500]  # 限制错误消息长度
            try:
                from .api_key_manager import log_api_call
                log_api_call(
                    api_key_id=api_key_id,
                    model=requested_model if 'requested_model' in locals() else None,
                    status="error",
                    response_time=response_time,
                    ip_address=ip_address,
                    endpoint=endpoint,
                    error_message=error_message,
                    request_size=request_size
                )
            except Exception:
                pass  # 日志记录失败不应影响主流程
            
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500
    
    # ==================== 图片服务接口 ====================
    
    @app.route('/image/<path:filename>')
    def serve_image(filename):
        """提供缓存图片的访问"""
        if '..' in filename or filename.startswith('/'):
            abort(404)
        
        filepath = IMAGE_CACHE_DIR / filename
        if not filepath.exists():
            abort(404)
        
        ext = filepath.suffix.lower()
        mime_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }
        mime_type = mime_types.get(ext, 'application/octet-stream')
        
        return send_from_directory(IMAGE_CACHE_DIR, filename, mimetype=mime_type)
    
    @app.route('/video/<path:filename>')
    def serve_video(filename):
        """提供缓存视频的访问"""
        if '..' in filename or filename.startswith('/'):
            abort(404)
        
        filepath = VIDEO_CACHE_DIR / filename
        if not filepath.exists():
            abort(404)
        
        mime_type = mimetypes.guess_type(str(filepath))[0] or 'application/octet-stream'
        return send_from_directory(VIDEO_CACHE_DIR, filename, mimetype=mime_type)
    
    @app.route('/health', methods=['GET'])
    def health_check():
        """健康检查"""
        return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})
    
    @app.route('/api/status', methods=['GET'])
    @require_admin
    def system_status():
        """获取系统状态"""
        total, available = account_manager.get_account_count()
        from .utils import get_proxy
        proxy_url = account_manager.config.get("proxy")
        proxy_enabled = account_manager.config.get("proxy_enabled", False)
        effective_proxy = get_proxy()  # 实际使用的代理（考虑开关状态）
        
        return jsonify({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "accounts": {
                "total": total,
                "available": available
            },
            "proxy": {
                "url": proxy_url,
                "enabled": proxy_enabled,
                "effective": effective_proxy,
                "available": check_proxy(effective_proxy) if effective_proxy else False
            },
            "models": account_manager.config.get("models", [])
        })
    
    # ==================== 管理接口 ====================
    
    @app.route('/')
    def index():
        """返回管理页面（需要登录）"""
        if not is_admin_authenticated():
            return redirect('/login')
        return render_template('index.html')
    
    @app.route('/login')
    def login_page():
        """登录页面"""
        if is_admin_authenticated():
            return redirect('/')
        return render_template('login.html')
    
    @app.route('/chat_history.html')
    def chat_history():
        """返回聊天记录页面（可独立访问，无需登录）"""
        return render_template('chat_history.html')
    
    @app.route('/account_extractor.html')
    def account_extractor():
        """返回账号信息提取工具页面"""
        if not is_admin_authenticated():
            return redirect('/login')
        return render_template('account_extractor.html')
    
    @app.route('/api/accounts', methods=['GET'])
    @require_admin
    def get_accounts():
        """获取账号列表"""
        # 确保配置已加载
        if account_manager.config is None:
            account_manager.load_config()
        
        # 如果账号列表为空，尝试从配置文件重新加载
        if not account_manager.accounts and account_manager.config:
            accounts_from_config = account_manager.config.get("accounts", [])
            if accounts_from_config:
                from .logger import print
                print(f"[警告] 账号列表为空，从配置文件重新加载 {len(accounts_from_config)} 个账号", _level="WARNING")
                account_manager.accounts = accounts_from_config
                # 重新初始化账号状态
                for i, acc in enumerate(account_manager.accounts):
                    available = acc.get("available", True)
                    # 被动检测模式：不再维护配额使用量字段
                    quota_usage = {}  # 保留用于向后兼容
                    quota_reset_date = None  # 保留用于向后兼容
                    account_manager.account_states[i] = {
                        "jwt": None,
                        "jwt_time": 0,
                        "session": None,
                        "available": available,
                        "cooldown_until": acc.get("cooldown_until"),
                        "cooldown_reason": acc.get("unavailable_reason") or acc.get("cooldown_reason") or "",
                        "quota_usage": quota_usage,  # 保留用于向后兼容
                        "quota_reset_date": quota_reset_date  # 保留用于向后兼容
                    }
        
        accounts_data = []
        now_ts = time.time()
        
        # 调试日志已关闭
        # from .logger import print
        # print(f"[DEBUG][get_accounts] 账号总数: {len(account_manager.accounts)}, account_states 数量: {len(account_manager.account_states)}", _level="DEBUG")
        
        # 批量获取所有账号的基本信息（最小化锁持有时间）
        accounts_snapshot = []
        states_snapshot = {}
        try:
            with account_manager.lock:
                # 快速复制账号和状态数据
                accounts_snapshot = [dict(acc) for acc in account_manager.accounts]  # 深拷贝避免后续修改
                states_snapshot = {k: dict(v) for k, v in account_manager.account_states.items()}  # 深拷贝
        except Exception as e:
            from .logger import print
            print(f"[错误] 获取账号快照失败: {e}", _level="ERROR")
            return jsonify({"accounts": [], "current_index": 0})
        
        # 在锁外处理每个账号（避免长时间持有锁）
        for i, acc in enumerate(accounts_snapshot):
            try:
                state = states_snapshot.get(i, {})
                cooldown_until = state.get("cooldown_until")
                cooldown_active = bool(cooldown_until and cooldown_until > now_ts)
                effective_available = state.get("available", True) and not cooldown_active
                
                # 安全获取配额信息，即使失败也不影响账号列表显示
                quota_info = {}
                try:
                    quota_info = account_manager.get_quota_info(i)
                except Exception as quota_error:
                    from .logger import print
                    print(f"[警告] 获取账号 {i} 配额信息失败: {quota_error}", _level="WARNING")
                    # 使用空的配额信息，确保账号列表仍能显示
                    quota_info = {}
                
                accounts_data.append({
                    "id": i,
                    "team_id": acc.get("team_id", ""),
                    "secure_c_ses": acc.get("secure_c_ses", ""),
                    "host_c_oses": acc.get("host_c_oses", ""),
                    "csesidx": acc.get("csesidx", ""),
                    "user_agent": acc.get("user_agent", ""),
                    "tempmail_name": acc.get("tempmail_name", ""),
                    "tempmail_url": acc.get("tempmail_url", ""),
                    "available": effective_available,
                    "unavailable_reason": acc.get("unavailable_reason", ""),
                    "cooldown_until": cooldown_until if cooldown_active else None,
                    "cooldown_reason": state.get("cooldown_reason", ""),
                    "has_jwt": state.get("jwt") is not None,
                    "cookie_expired": acc.get("cookie_expired", False) or state.get("cookie_expired", False),  # 从账号或状态中获取
                    "quota": quota_info
                })
            except Exception as e:
                # 即使单个账号处理失败，也继续处理其他账号
                from .logger import print
                print(f"[错误] 处理账号 {i} 时发生错误: {e}", _level="ERROR")
                import traceback
                print(traceback.format_exc(), _level="ERROR")
                # 至少返回基本信息
                accounts_data.append({
                    "id": i,
                    "team_id": acc.get("team_id", ""),
                    "secure_c_ses": acc.get("secure_c_ses", ""),
                    "host_c_oses": acc.get("host_c_oses", ""),
                    "csesidx": acc.get("csesidx", ""),
                    "user_agent": acc.get("user_agent", ""),
                    "tempmail_name": acc.get("tempmail_name", ""),
                    "tempmail_url": acc.get("tempmail_url", ""),
                    "available": False,
                    "unavailable_reason": f"处理错误: {str(e)}",
                    "cooldown_until": None,
                    "cooldown_reason": "",
                    "has_jwt": False,
                    "cookie_expired": acc.get("cookie_expired", False),  # 即使出错也返回 cookie_expired 状态
                    "quota": {}
                })
        
        # 调试日志已关闭
        # print(f"[DEBUG][get_accounts] 返回 {len(accounts_data)} 个账号", _level="DEBUG")
        
        return jsonify({
            "accounts": accounts_data,
            "current_index": account_manager.current_index
        })
    
    @app.route('/api/accounts', methods=['POST'])
    @require_admin
    def add_account():
        """添加账号"""
        data = request.json
        new_csesidx = data.get("csesidx", "")
        new_team_id = data.get("team_id", "")
        for acc in account_manager.accounts:
            if new_csesidx and acc.get("csesidx") == new_csesidx:
                return jsonify({"error": "账号已存在（同 csesidx）"}), 400
            if new_team_id and acc.get("team_id") == new_team_id and new_csesidx == acc.get("csesidx"):
                return jsonify({"error": "账号已存在（同 team_id + csesidx）"}), 400

        new_account = {
            "team_id": data.get("team_id", ""),
            "secure_c_ses": data.get("secure_c_ses", ""),
            "host_c_oses": data.get("host_c_oses", ""),
            "csesidx": data.get("csesidx", ""),
            "user_agent": data.get("user_agent", "Mozilla/5.0"),
            "available": True
        }
        
        # 被动检测模式：不再初始化配额使用量字段
        # 保留字段用于向后兼容，但不再使用
        # new_account["quota_usage"] = {...}
        # new_account["quota_reset_date"] = ...
        
        account_manager.accounts.append(new_account)
        idx = len(account_manager.accounts) - 1
        account_manager.account_states[idx] = {
            "jwt": None,
            "jwt_time": 0,
            "session": None,
            "available": True,
            "cooldown_until": None,
            "cooldown_reason": "",
            "quota_usage": {},  # 保留用于向后兼容
            "quota_reset_date": None  # 保留用于向后兼容
        }
        account_manager.config["accounts"] = account_manager.accounts
        account_manager.save_config()
        
        # 推送账号更新事件
        emit_account_update(idx, new_account)
        emit_notification("账号添加成功", f"账号 {idx} 已添加", "success")
        
        return jsonify({"success": True, "id": idx})
    
    @app.route('/api/accounts/<int:account_id>', methods=['PUT'])
    @require_admin
    def update_account(account_id):
        """更新账号"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        data = request.json
        acc = account_manager.accounts[account_id]
        
        # team_id 字段：允许设置为空字符串来清空
        if "team_id" in data:
            if data["team_id"]:
                acc["team_id"] = data["team_id"]
            else:
                # 如果为空字符串，清空该字段
                acc["team_id"] = ""
        # Cookie 相关字段：允许设置为空字符串来清空
        if "secure_c_ses" in data:
            if data["secure_c_ses"]:
                acc["secure_c_ses"] = data["secure_c_ses"]
            else:
                # 如果为空字符串，清空该字段
                acc["secure_c_ses"] = ""
        if "host_c_oses" in data:
            if data["host_c_oses"]:
                acc["host_c_oses"] = data["host_c_oses"]
            else:
                # 如果为空字符串，清空该字段
                acc["host_c_oses"] = ""
        if "csesidx" in data:
            if data["csesidx"]:
                acc["csesidx"] = data["csesidx"]
            else:
                # 如果为空字符串，清空该字段
                acc["csesidx"] = ""
        if "user_agent" in data:
            acc["user_agent"] = data["user_agent"]
        # 临时邮箱字段：允许设置为空字符串来清空
        if "tempmail_name" in data:
            if data["tempmail_name"]:
                acc["tempmail_name"] = data["tempmail_name"]
            else:
                # 如果为空字符串，删除该字段
                acc.pop("tempmail_name", None)
        if "tempmail_url" in data:
            if data["tempmail_url"]:
                acc["tempmail_url"] = data["tempmail_url"]
            else:
                # 如果为空字符串，删除该字段
                acc.pop("tempmail_url", None)
        
        # 检查 Cookie 字段是否被清空，如果是，标记为过期并触发自动刷新
        secure_c_ses = acc.get("secure_c_ses", "").strip()
        csesidx = acc.get("csesidx", "").strip()
        cookie_missing = not secure_c_ses or not csesidx
        
        if cookie_missing:
            # Cookie 字段缺失，标记为过期
            acc["cookie_expired"] = True
            acc["cookie_expired_time"] = datetime.now().isoformat()
            state = account_manager.account_states.get(account_id, {})
            state["cookie_expired"] = True
            # 标记账号为不可用
            acc["available"] = False
            state["available"] = False
            acc["unavailable_reason"] = "Cookie 信息不完整：缺少 secure_c_ses 或 csesidx"
            acc["unavailable_time"] = datetime.now().isoformat()
            print(f"[!] 账号 {account_id} Cookie 字段已清空，已标记为过期和不可用")
            
            # 如果自动刷新已启用，立即触发刷新检查
            auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
            if auto_refresh_enabled:
                try:
                    import sys
                    cookie_refresh_module = sys.modules.get('app.cookie_refresh')
                    if cookie_refresh_module and hasattr(cookie_refresh_module, '_immediate_refresh_event'):
                        cookie_refresh_module._immediate_refresh_event.set()
                        print(f"[Cookie 自动刷新] ⚡ 账号 {account_id} Cookie 已清空，已触发立即刷新检查")
                except (ImportError, AttributeError):
                    pass
        
        account_manager.config["accounts"] = account_manager.accounts
        account_manager.save_config()
        
        # 推送账号更新事件
        emit_account_update(account_id, acc)
        emit_notification("账号更新成功", f"账号 {account_id} 已更新", "success")
        
        return jsonify({"success": True})
    
    @app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
    @require_admin
    def delete_account(account_id):
        """删除账号"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        account_manager.accounts.pop(account_id)
        new_states = {}
        for i in range(len(account_manager.accounts)):
            if i < account_id:
                new_states[i] = account_manager.account_states.get(i, {})
            else:
                new_states[i] = account_manager.account_states.get(i + 1, {})
        account_manager.account_states = new_states
        account_manager.config["accounts"] = account_manager.accounts
        account_manager.save_config()
        
        # 推送账号删除事件
        emit_account_update(account_id, None)  # None 表示删除
        emit_notification("账号删除成功", f"账号 {account_id} 已删除", "success")
        
        return jsonify({"success": True})
    
    @app.route('/api/accounts/<int:account_id>/toggle', methods=['POST'])
    @require_admin
    def toggle_account(account_id):
        """切换账号状态"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        state = account_manager.account_states.get(account_id, {})
        current = state.get("available", True)
        state["available"] = not current
        account_manager.accounts[account_id]["available"] = not current
        
        if not current:
            account_manager.accounts[account_id].pop("unavailable_reason", None)
            account_manager.accounts[account_id].pop("unavailable_time", None)
            state.pop("cooldown_until", None)
            state.pop("cooldown_reason", None)
            account_manager.accounts[account_id].pop("cooldown_until", None)
        
        account_manager.save_config()
        return jsonify({"success": True, "available": not current})
    
    @app.route('/api/accounts/<int:account_id>/refresh-cookie', methods=['POST'])
    @require_admin
    def refresh_account_cookies(account_id):
        """刷新账号的secure_c_ses、host_c_oses和csesidx"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        data = request.json or {}
        acc = account_manager.accounts[account_id]
        
        if not data and PLAYWRIGHT_AVAILABLE:
            print(f"[手动刷新] 尝试自动刷新账号 {account_id} 的 Cookie...")
            success = auto_refresh_account_cookie(account_id, acc)
            if success:
                return jsonify({"success": True, "message": "Cookie已自动刷新", "auto": True})
            else:
                return jsonify({"error": "自动刷新失败，请手动提供 Cookie"}), 400
        
        if "secure_c_ses" in data:
            acc["secure_c_ses"] = data["secure_c_ses"]
        if "host_c_oses" in data:
            acc["host_c_oses"] = data["host_c_oses"]
        if "csesidx" in data and data["csesidx"]:
            acc["csesidx"] = data["csesidx"]
        
        with account_manager.lock:
            state = account_manager.account_states.get(account_id, {})
            state["jwt"] = None
            state["jwt_time"] = 0
            state["session"] = None
            account_manager.account_states[account_id] = state
            
            # 通知浏览器会话立即刷新（如果存在）
            if account_id in account_manager.browser_sessions:
                account_manager.browser_sessions[account_id]["need_refresh"] = True
                print(f"[手动刷新] 已通知账号 {account_id} 的浏览器会话立即刷新")
        
        account_manager.mark_cookie_refreshed(account_id)
        acc["cookie_refresh_time"] = datetime.now().isoformat()
        
        account_manager.config["accounts"] = account_manager.accounts
        account_manager.save_config()
        
        return jsonify({"success": True, "message": "Cookie已刷新"})
    
    @app.route('/api/accounts/<int:account_id>/auto-refresh-cookie', methods=['POST'])
    @require_admin
    def auto_refresh_account_cookies_route(account_id):
        """自动刷新账号的 Cookie（使用临时邮箱方式）"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        if not PLAYWRIGHT_AVAILABLE:
            return jsonify({
                "error": "Playwright 未安装，无法自动刷新",
                "detail": "请先安装 Playwright: pip install playwright && playwright install chromium"
            }), 400
        
        if not PLAYWRIGHT_BROWSER_INSTALLED:
            return jsonify({
                "error": "Playwright 浏览器未安装",
                "detail": "请运行命令安装浏览器: playwright install chromium"
            }), 400
        
        acc = account_manager.accounts[account_id]
        print(f"[手动触发] 正在使用临时邮箱自动刷新账号 {account_id} 的 Cookie...")
        
        # 先返回响应，避免长时间阻塞
        # 推送刷新开始事件
        try:
            emit_cookie_refresh_progress(account_id, "start", "开始刷新 Cookie...", 0.0)
        except Exception as e:
            print(f"[警告] WebSocket 推送失败: {e}")
        
        # 使用临时邮箱方式刷新
        try:
            import sys
            from pathlib import Path
            # 添加项目根目录到路径
            project_root = Path(__file__).parent.parent
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            
            from auto_login_with_email import refresh_single_account
            
            # 调用单个账号刷新函数（无头模式）
            # 从请求参数中获取 headless 设置
            # Windows 默认使用有头模式（False），方便调试和查看过程
            # Linux 服务器可以传递 headless=true 使用无头模式
            data = request.json or {}
            use_headless = data.get("headless", True)  # 默认 True（无头模式）
            success = refresh_single_account(account_id, acc, headless=use_headless)
            
            if success:
                # 重新加载配置，获取最新的账号状态
                account_manager.load_config()
                # 推送刷新成功事件
                try:
                    emit_cookie_refresh_progress(account_id, "success", "Cookie 刷新成功", 1.0)
                    emit_account_update(account_id, account_manager.accounts[account_id])
                    emit_notification("Cookie 刷新成功", f"账号 {account_id} 的 Cookie 已刷新", "success")
                except Exception as e:
                    print(f"[警告] WebSocket 推送失败: {e}")
                return jsonify({"success": True, "message": "Cookie已自动刷新（使用临时邮箱）"})
            else:
                # 推送刷新失败事件
                try:
                    emit_cookie_refresh_progress(account_id, "error", "Cookie 刷新失败", None)
                    emit_notification("Cookie 刷新失败", f"账号 {account_id} 的 Cookie 刷新失败", "error")
                except Exception as e:
                    print(f"[警告] WebSocket 推送失败: {e}")
                return jsonify({
                    "error": "自动刷新失败",
                    "detail": "请检查临时邮箱配置或手动刷新"
                }), 500
        except ImportError as e:
            return jsonify({
                "error": "导入刷新模块失败",
                "detail": f"请确保 auto_login_with_email.py 文件存在: {str(e)}"
            }), 500
        except Exception as e:
            # 捕获所有其他异常，避免 Werkzeug 错误
            error_msg = str(e)
            print(f"[错误] Cookie 刷新过程出错: {error_msg}")
            import traceback
            traceback.print_exc()
            try:
                emit_cookie_refresh_progress(account_id, "error", f"刷新过程出错: {error_msg}", None)
                emit_notification("Cookie 刷新失败", f"账号 {account_id} 的 Cookie 刷新失败: {error_msg}", "error")
            except:
                pass
            return jsonify({
                "error": "刷新过程出错",
                "detail": error_msg
            }), 500
        except Exception as e:
            print(f"[手动触发] 刷新过程出错: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": "自动刷新失败",
                "detail": f"刷新过程出错: {str(e)}"
            }), 500
    
    @app.route('/api/accounts/<int:account_id>/test', methods=['GET'])
    @require_admin
    def test_account(account_id):
        """测试账号JWT获取"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        account = account_manager.accounts[account_id]
        proxy = account_manager.config.get("proxy")
        
        # 检查 Cookie 字段是否存在
        secure_c_ses = account.get("secure_c_ses", "").strip()
        csesidx = account.get("csesidx", "").strip()
        if not secure_c_ses or not csesidx:
            # 提供更友好的错误提示
            missing_fields = []
            if not secure_c_ses:
                missing_fields.append("secure_c_ses")
            if not csesidx:
                missing_fields.append("csesidx")
            error_msg = f"Cookie 信息不完整：缺少 {', '.join(missing_fields)}。请刷新 Cookie 或手动填写。"
            
            # 标记账号为不可用，并设置 Cookie 过期
            reason = f"Cookie 信息不完整：缺少 {', '.join(missing_fields)}"
            account_manager.mark_account_unavailable(account_id, reason)
            
            # 手动设置 cookie_expired（因为 mark_account_unavailable 只在检测到 401/403 时设置）
            with account_manager.lock:
                account_manager.accounts[account_id]["cookie_expired"] = True
                account_manager.accounts[account_id]["cookie_expired_time"] = datetime.now().isoformat()
                state = account_manager.account_states.get(account_id, {})
                state["cookie_expired"] = True
            account_manager.save_config()
            
            # 如果自动刷新已启用，立即触发刷新检查
            auto_refresh_enabled = account_manager.config.get("auto_refresh_cookie", False)
            if auto_refresh_enabled:
                try:
                    import sys
                    cookie_refresh_module = sys.modules.get('app.cookie_refresh')
                    if cookie_refresh_module and hasattr(cookie_refresh_module, '_immediate_refresh_event'):
                        cookie_refresh_module._immediate_refresh_event.set()
                        print(f"[Cookie 自动刷新] ⚡ 账号 {account_id} Cookie 已清空，已触发立即刷新检查")
                except (ImportError, AttributeError):
                    pass
            
            return jsonify({
                "success": False, 
                "message": error_msg,
                "detail": "账号的 Cookie 字段为空，无法获取 JWT。请点击\"刷新Cookie\"按钮来更新 Cookie 信息。"
            })
        
        try:
            jwt = get_jwt_for_account(account, proxy)
            return jsonify({"success": True, "message": "JWT获取成功"})
        except AccountRateLimitError as e:
            pt_wait = seconds_until_next_pt_midnight()
            cooldown_seconds = max(account_manager.rate_limit_cooldown, pt_wait)
            account_manager.mark_account_cooldown(account_id, str(e), cooldown_seconds)
            return jsonify({"success": False, "message": str(e), "cooldown": cooldown_seconds})
        except AccountAuthError as e:
            account_manager.mark_account_unavailable(account_id, str(e))
            account_manager.mark_account_cooldown(account_id, str(e), account_manager.auth_error_cooldown)
            return jsonify({"success": False, "message": str(e), "cooldown": account_manager.auth_error_cooldown})
        except AccountRequestError as e:
            account_manager.mark_account_cooldown(account_id, str(e), account_manager.generic_error_cooldown)
            return jsonify({"success": False, "message": str(e), "cooldown": account_manager.generic_error_cooldown})
        except ValueError as e:
            # 处理 "缺少 secure_c_ses 或 csesidx" 错误
            if "缺少 secure_c_ses 或 csesidx" in str(e):
                error_msg = "Cookie 信息不完整：缺少 secure_c_ses 或 csesidx。请刷新 Cookie 或手动填写。"
                return jsonify({
                    "success": False, 
                    "message": error_msg,
                    "detail": "账号的 Cookie 字段为空，无法获取 JWT。请点击\"刷新Cookie\"按钮来更新 Cookie 信息。"
                })
            return jsonify({"success": False, "message": str(e)})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})
    
    @app.route('/api/accounts/<int:account_id>/quota', methods=['GET'])
    @require_admin
    def get_account_quota(account_id):
        """获取账号配额信息"""
        if account_id < 0 or account_id >= len(account_manager.accounts):
            return jsonify({"error": "账号不存在"}), 404
        
        quota_info = account_manager.get_quota_info(account_id)
        return jsonify({
            "account_id": account_id,
            "quota": quota_info
        })
    
    @app.route('/api/models', methods=['GET'])
    @require_admin
    def get_models_config():
        """获取模型配置"""
        models = account_manager.config.get("models", [])
        return jsonify({"models": models})
    
    @app.route('/api/models', methods=['POST'])
    @require_admin
    def add_model():
        """添加模型"""
        data = request.json
        new_model = {
            "id": data.get("id", ""),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "api_model_id": data.get("api_model_id"),
            "context_length": data.get("context_length", 32768),
            "max_tokens": data.get("max_tokens", 8192),
            "price_per_1k_tokens": data.get("price_per_1k_tokens"),
            "enabled": data.get("enabled", True),
            "account_index": data.get("account_index", 0)
        }
        
        if "models" not in account_manager.config:
            account_manager.config["models"] = []
        
        account_manager.config["models"].append(new_model)
        account_manager.save_config()
        
        return jsonify({"success": True})
    
    @app.route('/api/models/<model_id>', methods=['PUT'])
    @require_admin
    def update_model(model_id):
        """更新模型"""
        models = account_manager.config.get("models", [])
        for model in models:
            if model.get("id") == model_id:
                data = request.json
                if "name" in data:
                    model["name"] = data["name"]
                if "description" in data:
                    model["description"] = data["description"]
                if "api_model_id" in data:
                    model["api_model_id"] = data["api_model_id"]
                if "context_length" in data:
                    model["context_length"] = data["context_length"]
                if "max_tokens" in data:
                    model["max_tokens"] = data["max_tokens"]
                if "price_per_1k_tokens" in data:
                    model["price_per_1k_tokens"] = data["price_per_1k_tokens"]
                if "enabled" in data:
                    model["enabled"] = data["enabled"]
                if "account_index" in data:
                    model["account_index"] = data["account_index"]
                account_manager.save_config()
                return jsonify({"success": True})
        
        return jsonify({"error": "模型不存在"}), 404
    
    @app.route('/api/models/<model_id>', methods=['DELETE'])
    @require_admin
    def delete_model(model_id):
        """删除模型"""
        models = account_manager.config.get("models", [])
        for i, model in enumerate(models):
            if model.get("id") == model_id:
                models.pop(i)
                account_manager.save_config()
                return jsonify({"success": True})
        
        return jsonify({"error": "模型不存在"}), 404
    
    @app.route('/api/config', methods=['GET'])
    @require_admin
    def get_config():
        """获取完整配置"""
        config = dict(account_manager.config) if account_manager.config else {}
        
        # 添加服务信息（动态获取）
        try:
            # 获取实际运行的服务端口（后端端口，通常是 8000）
            # 从环境变量或 Flask 配置中获取
            actual_port = request.environ.get('SERVER_PORT', '8000')
            # 如果环境变量中没有，尝试从 request.url 解析
            if actual_port == '8000':
                try:
                    from urllib.parse import urlparse
                    # 尝试从 WSGI 环境变量获取
                    server_name = request.environ.get('SERVER_NAME', '')
                    if ':' in server_name:
                        actual_port = server_name.split(':')[1]
                    else:
                        # 默认使用 8000（gemini.py 中定义的端口）
                        actual_port = '8000'
                except Exception:
                    actual_port = '8000'
            
            # 获取外部访问地址（用于 API 地址显示）
            # 优先使用 X-Forwarded-Host（反向代理场景）
            forwarded_host = request.headers.get('X-Forwarded-Host', '')
            if forwarded_host:
                host_header = forwarded_host.split(',')[0].strip()  # 取第一个
            else:
                host_header = request.headers.get('Host', request.host)
            
            # 获取协议（优先使用 X-Forwarded-Proto）
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            if not scheme or scheme not in ['http', 'https']:
                scheme = 'https' if request.is_secure else 'http'
            
            # 解析外部访问的主机和端口（用于 API 地址）
            if ':' in host_header:
                external_host, external_port = host_header.rsplit(':', 1)
            else:
                external_host = host_header
                # 外部访问端口：根据协议使用默认端口
                external_port = '443' if scheme == 'https' else '80'
            
            # 如果 external_host 是 127.0.0.1 或 localhost，尝试从多个来源获取真实地址
            if external_host in ['127.0.0.1', 'localhost', '0.0.0.0']:
                # 1. 尝试从 Origin 头获取（AJAX 请求）
                origin = request.headers.get('Origin', '')
                if origin:
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(origin)
                        if parsed.hostname and parsed.hostname not in ['127.0.0.1', 'localhost', '0.0.0.0']:
                            external_host = parsed.hostname
                            if parsed.port:
                                external_port = str(parsed.port)
                            if parsed.scheme:
                                scheme = parsed.scheme
                    except Exception:
                        pass
                
                # 2. 如果 Origin 没有，尝试从 Referer 获取
                if external_host in ['127.0.0.1', 'localhost', '0.0.0.0']:
                    referer = request.headers.get('Referer', '')
                    if referer:
                        try:
                            from urllib.parse import urlparse
                            parsed = urlparse(referer)
                            if parsed.hostname and parsed.hostname not in ['127.0.0.1', 'localhost', '0.0.0.0']:
                                external_host = parsed.hostname
                                if parsed.port:
                                    external_port = str(parsed.port)
                                if parsed.scheme:
                                    scheme = parsed.scheme
                        except Exception:
                            pass
                
                # 3. 如果还是本地地址，尝试从配置的 image_base_url 获取（如果有）
                if external_host in ['127.0.0.1', 'localhost', '0.0.0.0']:
                    image_base_url = account_manager.config.get("image_base_url", "").strip()
                    if image_base_url:
                        try:
                            from urllib.parse import urlparse
                            parsed = urlparse(image_base_url)
                            if parsed.hostname and parsed.hostname not in ['127.0.0.1', 'localhost', '0.0.0.0']:
                                external_host = parsed.hostname
                                if parsed.port:
                                    external_port = str(parsed.port)
                                if parsed.scheme:
                                    scheme = parsed.scheme
                        except Exception:
                            pass
            
            # 构建外部访问 URL（反向代理场景下，通常不需要显示端口）
            # 如果使用 HTTPS，默认端口是 443，不显示端口
            # 如果使用 HTTP，默认端口是 80，不显示端口
            # 只有非标准端口才显示
            if scheme == 'https':
                # HTTPS 默认端口是 443，不显示
                if external_port and external_port != '443' and external_port != '80':
                    base_url = f"{scheme}://{external_host}:{external_port}"
                else:
                    base_url = f"{scheme}://{external_host}"
            else:
                # HTTP 默认端口是 80，不显示
                if external_port and external_port != '80':
                    base_url = f"{scheme}://{external_host}:{external_port}"
                else:
                    base_url = f"{scheme}://{external_host}"
            
            api_url = f"{base_url}/v1"
        except Exception as e:
            # 如果获取失败，使用请求的host作为后备
            try:
                base_url = f"{request.scheme}://{request.host}"
                api_url = f"{base_url}/v1"
                port = request.host.split(':')[-1] if ':' in request.host else '8000'
            except Exception:
                # 最后的默认值
                port = '8000'
                base_url = 'http://localhost:8000'
                api_url = f"{base_url}/v1"
        
        config['service'] = {
            "port": actual_port,  # 实际运行的后端端口（8000）
            "base_url": base_url,  # 外部访问的基础 URL
            "api_url": api_url  # 外部访问的 API 地址
        }
        
        # 添加账号信息（用于预览）
        config["accounts"] = account_manager.accounts
        # 移除已废弃的字段
        config.pop("api_tokens", None)  # 已废弃，使用新的 API 密钥管理系统
        
        return jsonify(config)
    
    @app.route('/api/config', methods=['PUT'])
    @require_admin
    def update_config():
        """更新配置"""
        data = request.json
        if "proxy" in data:
            account_manager.config["proxy"] = data["proxy"]
        if "proxy_enabled" in data:
            account_manager.config["proxy_enabled"] = data["proxy_enabled"]
        if "image_base_url" in data:
            account_manager.config["image_base_url"] = data["image_base_url"]
        if "upload_endpoint" in data:
            account_manager.config["upload_endpoint"] = data["upload_endpoint"]
        if "upload_api_token" in data:
            account_manager.config["upload_api_token"] = data["upload_api_token"]
        if "auto_refresh_cookie" in data:
            account_manager.config["auto_refresh_cookie"] = bool(data["auto_refresh_cookie"])
        if "tempmail_worker_url" in data:
            account_manager.config["tempmail_worker_url"] = data["tempmail_worker_url"] or None
        if "log_level" in data:
            try:
                set_log_level(data["log_level"], persist=True)
            except Exception as e:
                return jsonify({"error": str(e)}), 400
        account_manager.save_config()
        return jsonify({"success": True})
    
    @app.route('/api/logging', methods=['GET', 'POST'])
    @require_admin
    def logging_config():
        """获取或设置日志级别"""
        if request.method == 'GET':
            return jsonify({
                "level": CURRENT_LOG_LEVEL_NAME,
                "levels": list(LOG_LEVELS.keys())
            })
        
        data = request.json or {}
        level = data.get("level", "").upper()
        if level not in LOG_LEVELS:
            return jsonify({"error": "无效日志级别"}), 400
        
        set_log_level(level, persist=True)
        return jsonify({"success": True, "level": CURRENT_LOG_LEVEL_NAME})
    
    @app.route('/api/auth/login', methods=['POST'])
    def admin_login():
        """后台登录，返回 token。若尚未设置密码，则首次设置。"""
        from werkzeug.security import check_password_hash
        
        data = request.json or {}
        password = data.get("password", "")
        if not password:
            return jsonify({"error": "密码不能为空"}), 400
        
        stored_hash = get_admin_password_hash()
        if stored_hash:
            if not check_password_hash(stored_hash, password):
                return jsonify({"error": "密码错误"}), 401
        else:
            set_admin_password(password)
        
        token = create_admin_token()
        resp = jsonify({"token": token, "level": CURRENT_LOG_LEVEL_NAME})
        resp.set_cookie(
            "admin_token",
            token,
            max_age=86400,
            httponly=True,
            secure=False,
            samesite="Lax",
            path="/"
        )
        return resp
    
    @app.route('/api/auth/logout', methods=['POST'])
    def admin_logout():
        """注销登录，清除token"""
        resp = jsonify({"success": True})
        resp.set_cookie("admin_token", "", max_age=0, expires=0, path="/")
        return resp
    
    # ==================== API 密钥管理 ====================
    
    @app.route('/api/api-keys', methods=['GET'])
    @require_admin
    def list_api_keys():
        """获取 API 密钥列表"""
        try:
            from .api_key_manager import list_api_keys
            include_inactive = request.args.get('include_inactive', 'false').lower() == 'true'
            keys = list_api_keys(include_inactive=include_inactive)
            return jsonify({"success": True, "keys": keys})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-keys', methods=['POST'])
    @require_admin
    def create_api_key():
        """创建新的 API 密钥"""
        try:
            from .api_key_manager import create_api_key
            data = request.json or {}
            name = data.get("name", "")
            if not name:
                return jsonify({"error": "密钥名称不能为空"}), 400
            
            expires_days = data.get("expires_days")
            if expires_days is not None:
                try:
                    expires_days = int(expires_days)
                    if expires_days <= 0:
                        return jsonify({"error": "过期天数必须大于0"}), 400
                except (ValueError, TypeError):
                    return jsonify({"error": "过期天数格式错误"}), 400
            else:
                expires_days = None
            
            description = data.get("description", "")
            
            result = create_api_key(name, expires_days, description)
            return jsonify({"success": True, **result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-keys/<int:key_id>', methods=['DELETE'])
    @require_admin
    def delete_api_key(key_id):
        """删除 API 密钥"""
        try:
            from .api_key_manager import delete_api_key
            if delete_api_key(key_id):
                return jsonify({"success": True})
            return jsonify({"error": "API 密钥不存在"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-keys/<int:key_id>/revoke', methods=['POST'])
    @require_admin
    def revoke_api_key(key_id):
        """撤销 API 密钥"""
        try:
            from .api_key_manager import revoke_api_key
            if revoke_api_key(key_id):
                return jsonify({"success": True})
            return jsonify({"error": "API 密钥不存在"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-keys/<int:key_id>/stats', methods=['GET'])
    @require_admin
    def get_api_key_stats(key_id):
        """获取 API 密钥统计信息"""
        try:
            from .api_key_manager import get_api_key_stats
            days = request.args.get('days', 30, type=int)
            stats = get_api_key_stats(key_id, days)
            if stats:
                return jsonify({"success": True, "stats": stats})
            return jsonify({"error": "API 密钥不存在"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-keys/<int:key_id>/logs', methods=['GET'])
    @require_admin
    def get_api_key_logs(key_id):
        """获取 API 密钥调用日志"""
        try:
            from .api_key_manager import get_api_call_logs
            page = request.args.get('page', 1, type=int)
            page_size = request.args.get('page_size', 50, type=int)
            status = request.args.get('status')
            
            result = get_api_call_logs(key_id=key_id, page=page, page_size=page_size, status=status)
            return jsonify({"success": True, **result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/api-logs', methods=['GET'])
    @require_admin
    def get_api_logs():
        """获取所有 API 调用日志"""
        try:
            from .api_key_manager import get_api_call_logs
            page = request.args.get('page', 1, type=int)
            page_size = request.args.get('page_size', 50, type=int)
            status = request.args.get('status')
            key_id = request.args.get('key_id', type=int)
            
            result = get_api_call_logs(key_id=key_id, page=page, page_size=page_size, status=status)
            return jsonify({"success": True, **result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/config/import', methods=['POST'])
    @require_admin
    def import_config():
        """导入配置"""
        try:
            data = request.json
            if not data:
                return jsonify({"error": "请求数据为空"}), 400
            
            # 检查账号数据
            accounts = data.get("accounts", [])
            if not isinstance(accounts, list):
                return jsonify({"error": "账号数据格式错误，必须是数组"}), 400
            
            from .logger import print
            print(f"[配置导入] 导入 {len(accounts)} 个账号", _level="INFO")
            
            account_manager.config = data
            if data.get("log_level"):
                try:
                    set_log_level(data.get("log_level"), persist=False)
                except Exception:
                    pass
            if data.get("admin_secret_key"):
                account_manager.config["admin_secret_key"] = data.get("admin_secret_key")
                # 重新加载以更新全局变量
                get_admin_secret_key()
            else:
                get_admin_secret_key()
            account_manager.accounts = accounts
            account_manager.account_states = {}
            
            # 重新初始化账号状态（包括配额信息）
            for i, acc in enumerate(account_manager.accounts):
                available = acc.get("available", True)
                # 被动检测模式：不再使用配额使用量字段
                quota_usage = {}  # 保留用于向后兼容
                quota_reset_date = None  # 保留用于向后兼容
                account_manager.account_states[i] = {
                    "jwt": None,
                    "jwt_time": 0,
                    "session": None,
                    "available": available,
                    "cooldown_until": acc.get("cooldown_until"),
                    "cooldown_reason": acc.get("unavailable_reason") or acc.get("cooldown_reason") or "",
                    "quota_usage": quota_usage,
                    "quota_reset_date": quota_reset_date
                }
            
            account_manager.save_config()
            print(f"[配置导入] 配置导入成功，已保存 {len(account_manager.accounts)} 个账号", _level="INFO")
            return jsonify({"success": True, "accounts_count": len(account_manager.accounts)})
        except Exception as e:
            from .logger import print
            print(f"[配置导入] 导入失败: {e}", _level="ERROR")
            return jsonify({"error": str(e)}), 400
    
    @app.route('/api/proxy/test', methods=['POST'])
    @require_admin
    def test_proxy():
        """测试代理"""
        data = request.json
        # 测试时使用传入的代理或配置中的代理（不考虑开关状态）
        proxy_url = data.get("proxy") or account_manager.config.get("proxy")
        
        if not proxy_url:
            return jsonify({"success": False, "message": "未配置代理地址"})
        
        available = check_proxy(proxy_url)
        return jsonify({
            "success": available,
            "message": "代理可用" if available else "代理不可用或连接超时"
        })
    
    @app.route('/api/proxy/status', methods=['GET'])
    @require_admin
    def get_proxy_status():
        """获取代理状态"""
        from .utils import get_proxy
        proxy_url = account_manager.config.get("proxy")
        proxy_enabled = account_manager.config.get("proxy_enabled", False)
        effective_proxy = get_proxy()  # 实际使用的代理（考虑开关状态）
        
        if not proxy_url:
            return jsonify({"enabled": False, "url": None, "effective": None, "available": False})
        
        available = check_proxy(effective_proxy) if effective_proxy else False
        return jsonify({
            "enabled": proxy_enabled,
            "url": proxy_url,
            "effective": effective_proxy,
            "available": available
        })
    
    @app.route('/api/config/export', methods=['GET'])
    @require_admin
    def export_config():
        """导出配置（包含账号信息）"""
        config = dict(account_manager.config) if account_manager.config else {}
        # 添加账号信息
        config["accounts"] = account_manager.accounts
        # 移除已废弃的字段
        config.pop("api_tokens", None)  # 已废弃，使用新的 API 密钥管理系统
        return jsonify(config)

