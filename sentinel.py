import os
import jwt
import uuid
from aiohttp import web

from server import PromptServer

from .utils import *

instance = PromptServer.instance
app = instance.app
routes = instance.routes

logger = Logger(LOG_FILE, LOG_LEVELS)
sanitizer = Sanitizer()
ip_filter = IPFilter(WHITELIST, BLACKLIST)
timeout = Timeout(ip_filter, BLACKLIST_AFTER_ATTEMPTS)
users_db = UsersDB(USERS_FILE)
access_control = AccessControl(users_db, instance)
jwt_auth = JWTAuth(
    users_db, access_control, logger, SECRET_KEY, TOKEN_EXPIRE_MINUTES, TOKEN_ALGORITHM
)


@routes.get("/login")
async def get_login(request: web.Request) -> web.Response:
    if not users_db.load_users():   
        return web.json_response(
                {"error": "Bloquage dans /login, la database s'est mal chargée"},
                status=503
            )


    token = jwt_auth.get_token_from_request(request)
    if token:
        return web.HTTPFound("/logout")
    return web.FileResponse(os.path.join(HTML_DIR, "login.html"))


@routes.post("/login")
async def post_login(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)
    username = sanitized_data.get("username")
    password = sanitized_data.get("password")

    if not username or not password:
        return web.json_response(
            {"error": "Missing login credentials (username and password)"}, status=400
        )

    if users_db.check_username_password(username, password):
        timeout.remove_failed_attempts(ip)

        user_id, _ = users_db.get_user(username)
        token = jwt_auth.create_access_token({"id": user_id, "username": username})
        response = web.json_response(
            {
                "message": "Login successful",
                "jwt_token": token,
            }
        )
        secure_flag = request.headers.get("X-Forwarded-Proto", "http") == "https"
        response.set_cookie(
            "jwt_token", token, httponly=True, secure=secure_flag, samesite="Strict"
        )
        logger.login_success(ip, username)
        return response

    logger.login_attempt(ip, username, password)
    timeout.add_failed_attempt(ip)
    return web.json_response({"error": "Invalid username or password"}, status=401)


@routes.get("/generate_token")
async def get_generate_token(request: web.Request) -> web.Response:
    if not users_db.load_users():
        return web.json_response(
                {"error": "Bloquage /generate_token, la database s'est mal chargée"},
                status=503
            )


    token = jwt_auth.get_token_from_request(request)
    if token:
        return web.HTTPFound("/logout")
    return web.FileResponse(os.path.join(HTML_DIR, "generate_token.html"))


@routes.post("/generate_token")
async def post_generate_token(request: web.Request) -> web.Response:
    sanitized_data = request.get("_sanitized_data", {})
    ip = get_ip(request)
    username = sanitized_data.get("username")
    password = sanitized_data.get("password")

    try:
        expire_hours = int(
            sanitized_data.get("expire_hours", TOKEN_EXPIRE_MINUTES / 60)
        )

    except ValueError:
        return web.json_response(
            {"error": "Expiration hours must be a number"},
            status=400,
        )

    if expire_hours > MAX_TOKEN_EXPIRE_MINUTES / 60:
        return web.json_response(
            {
                "error": f"Expiration hours must be smaller than {MAX_TOKEN_EXPIRE_MINUTES / 60}"
            },
            status=400,
        )

    if not username or not password:
        return web.json_response(
            {"error": "Missing login credentials (username and password)"}, status=400
        )

    if users_db.check_username_password(username, password):
        timeout.remove_failed_attempts(ip)

        user_id, _ = users_db.get_user(username)
        token = jwt_auth.create_access_token(
            {"id": user_id, "username": username}, expire_minutes=(expire_hours * 60)
        )
        response = web.json_response(
            {
                "message": "JWT Token successfully generated",
                "jwt_token": token,
            }
        )
        secure_flag = request.headers.get("X-Forwarded-Proto", "http") == "https"
        response.set_cookie(
            "jwt_token", token, httponly=True, secure=secure_flag, samesite="Strict"
        )
        
        logger.generate_success(ip, username, expire_hours)
        
        return response

    logger.generate_attempt(ip, username, password, expire_hours)
    timeout.add_failed_attempt(ip)
    return web.json_response({"error": "Invalid username or password"}, status=401)


@routes.get("/logout")
async def get_logout(request: web.Request) -> web.Response:
    ip = get_ip(request)
    free_memory = request.query.get("free_memory", "false").lower() == "true"
    unload_models = request.query.get("unload_models", "false").lower() == "true"

    token = jwt_auth.get_token_from_request(request)
    if token and FREE_MEMORY_ON_LOGOUT:
        try:
            username = jwt_auth.decode_access_token(token).get("username")
            if free_memory or unload_models:
                if hasattr(instance, "post_free"):
                    json_data = {
                        "unload_models": unload_models,
                        "free_memory": free_memory,
                    }
                    mock_request = web.Request(
                        app=app,
                        method="POST",
                        path="/free",
                        headers={},
                        match_info={},
                        payload=None,
                    )
                    mock_request._post = json_data
                    await instance.post_free(mock_request)
                    logger.memory_free(ip, username, free_memory, unload_models)

            logger.logout(ip, username)
        except jwt.ExpiredSignatureError:
            pass
        except jwt.InvalidTokenError:
            pass
        except Exception as e:
            logger.error(f"Unexpected error during logout: {e}")

    response = web.HTTPFound("/login")
    response.del_cookie("jwt_token", path="/")

    return response


app.add_routes(
    [
        web.static("/sentinel/css", CSS_DIR),
        web.static("/sentinel/js", JS_DIR),
        web.static("/sentinel/assets", ASSETS_DIR),
    ]
)

if FORCE_HTTPS:
    app.middlewares.append(create_https_middleware(MATCH_HEADERS))

app.middlewares.append(ip_filter.create_ip_filter_middleware())
app.middlewares.append(sanitizer.create_sanitizer_middleware())
app.middlewares.append(
    timeout.create_time_out_middleware(
        limited=("/login", "/generate_token")
    )
)
app.middlewares.append(
    jwt_auth.create_jwt_middleware(
        public=("/login", "/logout", "/generate_token"),
        public_prefixes=("/sentinel"),
    )
)

if SEPERATE_USERS:
    app.middlewares.append(access_control.create_folder_access_control_middleware())
    app.middlewares.append(access_control.create_workflow_response_middleware())

    access_control.patch_folder_paths()
    access_control.patch_prompt_queue()

if MANAGER_ADMIN_ONLY:
    app.middlewares.append(
        access_control.create_manager_access_control_middleware(
            manager_directory="/extensions/comfyui-manager",
            manager_routes=(
                "api/customnode",
                "api/snapshot",
                "/api/manager",
                "api/comfyui_manager",
                "api/externalmodel",
            ),
        )
    )

# Recovery endpoints for stuck queue items
@routes.get("/sentinel/queue/stuck")
async def get_stuck_items(request: web.Request) -> web.Response:
    """
    Get list of stuck items in the queue.
    Optional query parameter: user_id or username to filter by user.
    Admin only or own user items only.
    """
    if not SEPERATE_USERS:
        return web.json_response({"error": "User separation not enabled"}, status=400)
    
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return web.json_response({"error": "Authentication required"}, status=401)
    
    try:
        payload = jwt_auth.decode_access_token(token)
        current_user_id = payload.get("id")
        current_username = payload.get("username")
        is_admin = users_db.get_admin_user()[0] == current_user_id
        
        # Get filter from query parameter
        user_filter = request.rel_url.query.get('user_id') or request.rel_url.query.get('username')
        
        # If filtering by specific user, check permissions
        if user_filter:
            if not is_admin and user_filter != current_user_id and user_filter != current_username:
                return web.json_response({"error": "Access denied"}, status=403)
            stuck_items = access_control.get_stuck_items(user_filter)
        else:
            # No filter - return all items if admin, otherwise only current user's items
            if is_admin:
                stuck_items = access_control.get_stuck_items()
            else:
                stuck_items = access_control.get_stuck_items(current_user_id) + access_control.get_stuck_items(current_username)
                # Remove duplicates
                seen = set()
                unique_items = []
                for item in stuck_items:
                    key = (item.get("item_id"), item.get("prompt_id"))
                    if key not in seen:
                        seen.add(key)
                        unique_items.append(item)
                stuck_items = unique_items
        
        return web.json_response({"stuck_items": stuck_items, "count": len(stuck_items)})
    except jwt.ExpiredSignatureError:
        return web.json_response({"error": "Token expired"}, status=401)
    except jwt.InvalidTokenError:
        return web.json_response({"error": "Invalid token"}, status=401)
    except Exception as e:
        logger.error(f"Error getting stuck items: {e}")
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/sentinel/queue/clear_stuck")
async def post_clear_stuck(request: web.Request) -> web.Response:
    """
    Clear stuck items from the queue.
    Can clear by prompt_id or by user (user_id or username).
    This is a recovery mechanism for when items get stuck in currently_running.
    
    Request body examples:
    - {"prompt_id": "abc123"} - Clear specific prompt
    - {"user_id": "e530612"} - Clear all items for a user
    - {"username": "john"} - Clear all items for a username
    """
    if not SEPERATE_USERS:
        return web.json_response({"error": "User separation not enabled"}, status=400)
    
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return web.json_response({"error": "Authentication required"}, status=401)
    
    try:
        payload = jwt_auth.decode_access_token(token)
        current_user_id = payload.get("id")
        current_username = payload.get("username")
        is_admin = users_db.get_admin_user()[0] == current_user_id
        
        json_data = await request.json()
        
        cleared_count = 0
        cleared_items = []
        
        # Clear by prompt_id
        if "prompt_id" in json_data:
            prompt_id = json_data["prompt_id"]
            # Check if user has permission to clear this prompt
            stuck_items = access_control.get_stuck_items()
            can_clear = False
            for item in stuck_items:
                if item.get("prompt_id") == prompt_id:
                    item_user_id = item.get("user_id")
                    item_username = item.get("username")
                    if is_admin or item_user_id == current_user_id or item_username == current_username:
                        can_clear = True
                        break
            
            if not can_clear:
                return web.json_response({"error": "Access denied"}, status=403)
            
            if access_control.delete_running_item_by_prompt_id(prompt_id):
                cleared_count += 1
                cleared_items.append({"type": "prompt_id", "id": prompt_id})
        
        # Clear by user/user_id/username
        user_identifier = json_data.get("user_id") or json_data.get("username")
        
        if user_identifier:
            # Check permissions
            if not is_admin and user_identifier != current_user_id and user_identifier != current_username:
                return web.json_response({"error": "Access denied"}, status=403)
            
            count = access_control.delete_running_items_by_user(user_identifier)
            cleared_count += count
            if count > 0:
                cleared_items.append({
                    "type": "user",
                    "identifier": user_identifier,
                    "count": count
                })
        
        response = {
            "cleared_count": cleared_count,
            "cleared_items": cleared_items
        }
        
        if cleared_count > 0:
            logger.info(f"Cleared {cleared_count} stuck queue item(s): {cleared_items}")
        
        return web.json_response(response)
    except jwt.ExpiredSignatureError:
        return web.json_response({"error": "Token expired"}, status=401)
    except jwt.InvalidTokenError:
        return web.json_response({"error": "Invalid token"}, status=401)
    except Exception as e:
        logger.error(f"Error clearing stuck items: {e}")
        return web.json_response({"error": str(e)}, status=500)


@routes.post("/sentinel/queue/emergency_clear")
async def post_emergency_clear(request: web.Request) -> web.Response:
    """
    EMERGENCY: Clear ALL stuck items from the queue.
    This should only be used when the queue is completely blocked.
    Admin only.
    """
    if not SEPERATE_USERS:
        return web.json_response({"error": "User separation not enabled"}, status=400)
    
    token = jwt_auth.get_token_from_request(request)
    if not token:
        return web.json_response({"error": "Authentication required"}, status=401)
    
    try:
        payload = jwt_auth.decode_access_token(token)
        current_user_id = payload.get("id")
        is_admin = users_db.get_admin_user()[0] == current_user_id
        
        if not is_admin:
            return web.json_response({"error": "Admin access required"}, status=403)
        
        # Clear ALL items from currently_running using emergency method
        cleared_count = access_control.emergency_clear_all_running()
        
        logger.warning(f"EMERGENCY CLEAR: Removed {cleared_count} item(s) from queue by admin {payload.get('username')}")
        
        return web.json_response({
            "message": f"Emergency clear completed",
            "cleared_count": cleared_count
        })
    except jwt.ExpiredSignatureError:
        return web.json_response({"error": "Token expired"}, status=401)
    except jwt.InvalidTokenError:
        return web.json_response({"error": "Invalid token"}, status=401)
    except Exception as e:
        logger.error(f"Error in emergency clear: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)