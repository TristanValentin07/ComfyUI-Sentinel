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
