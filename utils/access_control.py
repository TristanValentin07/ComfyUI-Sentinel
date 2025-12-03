import os
import heapq
import copy
import contextvars
import json
import time
import threading  # <--- 1. AJOUT IMPORT
from aiohttp import web
from typing import Optional
import logging 
from datetime import datetime

import folder_paths
from server import PromptServer
from execution import PromptQueue, MAXIMUM_HISTORY_SIZE

from .users_db import UsersDB


class AccessControl:
    def __init__(self, users_db: UsersDB, server: PromptServer):
        self.users_db = users_db
        self.server = server

        # gestion du cache
        self._cache = {} 
        self._cache_ttl = 60
        self._cache_lock = threading.Lock()
    

        self.config = {}
        utils_dir = os.path.dirname(os.path.abspath(__file__))
        root_node_dir = os.path.dirname(utils_dir)
        config_path = os.path.join(root_node_dir, "config.json")
        
        print(f"[Sentinel] INIT: Chemin calculé du config : {config_path}")
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                print(f"[Sentinel] SUCCESS: Config chargée.")
            except Exception as e:
                print(f"[Sentinel] CRITICAL ERROR: Le fichier existe mais est illisible: {e}")
        else:
            fallback_path = os.path.join(utils_dir, "config.json")
            if os.path.exists(fallback_path):
                try:
                    with open(fallback_path, 'r', encoding='utf-8') as f:
                        self.config = json.load(f)
                    print(f"[Sentinel] SUCCESS: Config chargée (depuis utils).")
                except:
                    pass
            
            if not self.config:
                print(f"[Sentinel] CRITICAL ERROR: Fichier config.json INTROUVABLE.")

        self._current_user = contextvars.ContextVar("user_id", default=None)
        self._current_username = contextvars.ContextVar("username", default=None)
        
        self.__current_user_id = None 
        self.__current_username = None

        self.__get_output_directory = folder_paths.get_output_directory
        self.__get_temp_directory = folder_paths.get_temp_directory
        
        self.__get_filename_list = folder_paths.get_filename_list
        self.__get_full_path = folder_paths.get_full_path

        self.__prompt_queue = self.server.prompt_queue
        self.__prompt_queue_put = self.server.prompt_queue.put

    @property
    def folder_paths(self) -> tuple:
        return (
            self.__get_output_directory(),
            self.__get_temp_directory(),
            folder_paths.get_input_directory(), 
        )

    def set_current_user_id(self, user_id: str) -> None:
        self._current_user.set(user_id)
        self.__current_user_id = user_id 

    def set_current_username(self, username: str) -> None:
        self._current_username.set(username)
        self.__current_username = username 

    def get_current_user_id(self) -> str:
        user_id = self._current_user.get()
        if user_id:
            return user_id
        return self.__current_user_id 

    def get_current_username(self) -> str:
        username = self._current_username.get()
        if username:
            return username
        return self.__current_username 

    
    def get_user_output_directory(self) -> str:
        base_output_path = self.config.get("user_outputs_base", "")
        
        if base_output_path:
            base_output_path = os.path.normpath(base_output_path)

        username = self.get_current_username() 
        
        if not username:
            return self.__get_output_directory()
        
        if not base_output_path:
             return os.path.join(self.__get_output_directory(), username)

        try:
            date_folder = datetime.now().strftime("%Y-%m-%d")
            full_day_path = os.path.join(base_output_path, username, "ComfyUI", date_folder)
            os.makedirs(full_day_path, exist_ok=True)
            return full_day_path
        
        except Exception as e:
            print(f"[Sentinel] ERROR: Failed to create output directory: {e}")
            return os.path.join(base_output_path, username)

    def get_user_temp_directory(self) -> str:
        return self.__get_temp_directory()
        
    def patched_get_filename_list(self, folder_name: str) -> list[str]:
        if folder_name not in ["loras", "checkpoints"]:
             return self.__get_filename_list(folder_name)

        # 3. UTILISATION DU VERROU
        # On utilise le lock ici. Si quelqu'un est déjà en train de scanner ou lire le cache,
        # les autres attendent à l'entrée de ce bloc 'with'.
        with self._cache_lock:
            cache_username = self.get_current_username() or "global"
            cache_key = f"{cache_username}_{folder_name}"
            current_time = time.time()

            # Vérification rapide (maintenant protégée)
            if cache_key in self._cache:
                last_scan_time, cached_files = self._cache[cache_key]
                if current_time - last_scan_time < self._cache_ttl:
                    return cached_files

            # Si on arrive ici, c'est qu'il faut scanner.
            # Comme on est dans le "with lock", personne d'autre ne peut lancer un scan en même temps.
            
            all_found_files = set() 
            
            if folder_name == "loras":
                extensions = folder_paths.supported_pt_extensions
            elif folder_name == "checkpoints":
                extensions = folder_paths.supported_pt_extensions
            else:
                extensions = []

            # 1. Scan LOCAUX
            local_dir = os.path.join(folder_paths.base_path, "models", folder_name)
            if os.path.isdir(local_dir):
                try:
                    files, _ = folder_paths.recursive_search(local_dir)
                    all_found_files.update(folder_paths.filter_files_extensions(files, extensions))
                except Exception as e:
                    print(f"[Sentinel] LIST: Error in local path: {e}")

            # 2. Scan RESEAU
            username = self.get_current_username()
            base_path = ""
            
            if folder_name == "loras":
                base_path = self.config.get("user_loras_base", "")
            elif folder_name == "checkpoints":
                base_path = self.config.get("user_checkpoints_base", "")
                
            if base_path:
                base_path = os.path.normpath(base_path)
                try:
                    if username:
                        user_path = os.path.join(base_path, username)
                        if os.path.isdir(user_path):
                            files, _ = folder_paths.recursive_search(user_path)
                            user_files = folder_paths.filter_files_extensions(files, extensions)
                            for f in user_files:
                                all_found_files.add(os.path.join(username, f).replace("\\", "/"))

                    common_path = os.path.join(base_path, "common")
                    if os.path.isdir(common_path):
                        files, _ = folder_paths.recursive_search(common_path)
                        common_files = folder_paths.filter_files_extensions(files, extensions)
                        for f in common_files:
                            all_found_files.add(os.path.join("common", f).replace("\\", "/"))
            
                except Exception as e:
                    print(f"[Sentinel] LIST: Error scanning network paths: {e}")

            # Sauvegarde en cache (toujours protégé par le lock)
            final_list = sorted(list(all_found_files))
            self._cache[cache_key] = (current_time, final_list)

            return final_list
        
    def patched_get_full_path(self, folder_name: str, filename: str) -> str | None:
        if folder_name == "loras":
            base_lora_path = self.config.get("user_loras_base", "")
            if base_lora_path:
                base_lora_path = os.path.normpath(base_lora_path)
                
                username = self.get_current_username()
                filename = filename.replace("\\", "/")
                
                if username and filename.startswith(f"{username}/"):
                    full_path = os.path.join(base_lora_path, filename)
                    if os.path.isfile(full_path):
                        print(f"[Sentinel] LORA LOAD: Found (User Prefix): {full_path}")
                        return full_path

                if filename.startswith("common/"):
                    full_path = os.path.join(base_lora_path, filename)
                    if os.path.isfile(full_path):
                         print(f"[Sentinel] LORA LOAD: Found (Common Prefix): {full_path}")
                         return full_path
                
                if username:
                    user_path = os.path.join(base_lora_path, username, filename)
                    if os.path.isfile(user_path):
                        return user_path

                common_path = os.path.join(base_lora_path, "common", filename)
                if os.path.isfile(common_path):
                    return common_path

            return self.__get_full_path(folder_name, filename)

        return self.__get_full_path(folder_name, filename)

    def patch_folder_paths(self) -> None:
        folder_paths.get_filename_list = self.patched_get_filename_list
        folder_paths.get_output_directory = self.get_user_output_directory
        folder_paths.get_full_path = self.patched_get_full_path
        self.patch_prompt_queue()
        
    
    def create_folder_access_control_middleware(
        self, folder_paths: tuple = ()
    ) -> web.middleware:
        folder_paths = folder_paths or self.folder_paths

        @web.middleware
        async def folder_access_control_middleware(
            request: web.Request, handler
        ) -> web.Response:
            if not request.path.startswith(folder_paths):
                return await handler(request)

            user_id = request.get("user_id")
            user_id, user = self.users_db.get_user(user_id)

            try:
                path_parts = request.path.strip("/").split("/")
                folder_user_id = path_parts[1]
            except:
                return web.HTTPNotFound(reason="Folder not found.")

            if folder_user_id == "public":
                return await handler(request)

            if (
                not user_id
                or not user
                or len(path_parts) < 2
                or (user_id != folder_user_id and not user.get("admin"))
            ):
                return web.HTTPForbidden(
                    reason="You do not have access to this folder."
                )

            return await handler(request)

        return folder_access_control_middleware

    def user_queue_put(self, item):
        username = self.get_current_username()
        user_id = self.get_current_user_id() 
        print(f"Sauvegarde en cours")

        if username: 
            try:
                prompt_json = item[2]
                for node_id, node_data in prompt_json.items():
                    class_type = node_data.get("class_type")
                    inputs = node_data.get("inputs", {})

                    if class_type.startswith("SaveImage") and "filename_prefix" in inputs:
                        original_prefix = inputs.get("filename_prefix", "ComfyUI")
                        final_prefix_name = original_prefix.replace("\\", "/").split('/')[-1]
                        inputs["filename_prefix"] = final_prefix_name
                        print(f"[Sentinel] EXEC: Filename prefix cleaned to: {final_prefix_name}")
            
            except Exception as e:
                print(f"[Sentinel] ERROR patching workflow JSON: {e}")
        else:
            print("[Sentinel] EXEC: Pas de username dans le contexte.")

        item_with_user = {"prompt": item, "user_id": user_id} 
        self.__prompt_queue_put(item_with_user)


    def user_queue_get(self, timeout=None):
        user_queue = self.__prompt_queue.queue
        with self.__prompt_queue.not_empty:
            while len(user_queue) == 0:
                self.__prompt_queue.not_empty.wait(timeout=timeout)
                if timeout is not None and len(user_queue) == 0:
                    return None
            item = heapq.heappop(user_queue)
            i = self.__prompt_queue.task_counter
            self.__prompt_queue.currently_running[i] = copy.deepcopy(item)
            self.__prompt_queue.task_counter += 1
            self.server.queue_updated()
            return (item["prompt"], i) 


    def user_queue_task_done(
        self, item_id, history_result, status: Optional["PromptQueue.ExecutionStatus"]
    ):
        with self.__prompt_queue.mutex:
            prompt = self.__prompt_queue.currently_running.pop(item_id)
            if len(self.__prompt_queue.history) > MAXIMUM_HISTORY_SIZE:
                self.__prompt_queue.history.pop(next(iter(self.__prompt_queue.history)))

            status_dict: Optional[dict] = None
            if status is not None:
                status_dict = copy.deepcopy(status._asdict())

            self.__prompt_queue.history[prompt["prompt"][1]] = {
                "prompt": prompt["prompt"],
                "outputs": {},
                "status": status_dict,
                "user_id": prompt["user_id"],
            }
            self.__prompt_queue.history[prompt["prompt"][1]].update(history_result)
            self.server.queue_updated()

    def user_queue_get_current_queue(self):
        with self.__prompt_queue.mutex:
            out = []
            for x in self.__prompt_queue.currently_running.values():
                out += [x]
            return (out, copy.deepcopy(self.__prompt_queue.queue))

    def user_queue_wipe_queue(self):
        with self.__prompt_queue.mutex:
            self.__prompt_queue.queue = [
                item
                for item in self.__prompt_queue.queue
                if item["user_id"] != self.get_current_user_id()
            ]
            self.server.queue_updated()

    def user_queue_delete_queue_item(self, function):
        with self.__prompt_queue.mutex:
            for x in range(len(self.__prompt_queue.queue)):
                if (
                    function(self.__prompt_queue.queue[x])
                    and self.__prompt_queue.queue[x]["user_id"]
                    == self.get_current_user_id()
                ):
                    if len(self.__prompt_queue.queue) == 1:
                        self.__prompt_queue.wipe_queue()
                    else:
                        self.__prompt_queue.pop(x)
                        heapq.heapify(self.__prompt_queue.queue)
                    self.server.queue_updated()
                    return True
            return False

    def user_queue_get_history(self, prompt_id=None, max_items=None, offset=-1):
        with self.__prompt_queue.mutex:
            user_history = {
                k: v
                for k, v in self.__prompt_queue.history.items()
                if v["user_id"] == self.get_current_user_id()
            }
            if prompt_id is None:
                out = {}
                i = 0
                if offset < 0 and max_items is not None:
                    offset = len(user_history) - max_items
                for k in user_history:
                    if i >= offset:
                        out[k] = user_history[k]
                        if max_items is not None and len(out) >= max_items:
                            break
                    i += 1
                return out
            elif prompt_id in user_history:
                return {prompt_id: copy.deepcopy(user_history[prompt_id])}
            else:
                return {}

    def user_queue_wipe_history(self):
        with self.__prompt_queue.mutex:
            self.__prompt_queue.history = {
                k: v
                for k, v in self.__prompt_queue.history.items()
                if v["user_id"] != self.get_current_user_id()
            }

    def patch_prompt_queue(self):
        self.__prompt_queue.put = self.user_queue_put
        self.__prompt_queue.get = self.user_queue_get
        self.__prompt_queue.task_done = self.user_queue_task_done
        self.__prompt_queue.get_current_queue = self.user_queue_get_current_queue
        self.__prompt_queue.wipe_queue = self.user_queue_wipe_queue
        self.__prompt_queue.delete_queue_item = self.user_queue_delete_queue_item
        self.__prompt_queue.get_history = self.user_queue_get_history
        self.__prompt_queue.wipe_history = self.user_queue_wipe_history

    def create_manager_access_control_middleware(
        self, manager_directory: str = "/extensions/comfyui-manager", manager_routes: tuple = ()
    ) -> web.middleware:
        @web.middleware
        async def manager_access_control_middleware(
            request: web.Request, handler
        ) -> web.Response:
            user_id = request.get("user_id")
            
            if self.users_db.get_admin_user()[0] == user_id or (not request.path.startswith(manager_routes) and not request.path.lower().startswith(manager_directory)):
                return await handler(request)

            return web.HTTPForbidden(
                reason="You do not have access to comfyui manager."
            )

        return manager_access_control_middleware