import os
import heapq
import copy
import contextvars
import json
import threading
from aiohttp import web
from typing import Optional
import logging 
from datetime import datetime

import folder_paths
from server import PromptServer
from execution import PromptQueue, MAXIMUM_HISTORY_SIZE

from .users_db import UsersDB


class ComparableQueueItem:
    """Wrapper class to make dictionary queue items comparable for heapq."""
    def __init__(self, data):
        self.data = data
    
    def __lt__(self, other):
        """Compare based on priority number from the prompt tuple."""
        if not isinstance(other, ComparableQueueItem):
            return NotImplemented
        try:
            # Get priority number from item["prompt"][0]
            self_priority = self.data["prompt"][0]
            other_priority = other.data["prompt"][0]
            return self_priority < other_priority
        except (KeyError, IndexError, TypeError):
            # Fallback comparison if structure is unexpected
            return id(self) < id(other)
    
    def __getitem__(self, key):
        """Allow dict-like access."""
        return self.data[key]
    
    def __setitem__(self, key, value):
        """Allow dict-like assignment."""
        self.data[key] = value
    
    def __contains__(self, key):
        """Allow 'in' operator."""
        return key in self.data
    
    def get(self, key, default=None):
        """Allow dict-like get method."""
        return self.data.get(key, default)
    
    def keys(self):
        """Allow dict-like keys method."""
        return self.data.keys()
    
    def values(self):
        """Allow dict-like values method."""
        return self.data.values()
    
    def items(self):
        """Allow dict-like items method."""
        return self.data.items()
    
    def __repr__(self):
        return f"ComparableQueueItem({self.data})"


class AccessControl:
    def __init__(self, users_db: UsersDB, server: PromptServer):
        self.users_db = users_db
        self.server = server

        # --- CHARGEMENT DE LA CONFIGURATION ---
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
        # ----------------------------------------------

        self._current_user = contextvars.ContextVar("user_id", default=None)
        self._current_username = contextvars.ContextVar("username", default=None)
        
        self.__current_user_id = None 
        self.__current_username = None
        
        # Thread-safe mapping from prompt_id to user info (for worker thread context)
        self.__prompt_to_user = {}  # {prompt_id: {"user_id": ..., "username": ...}}
        self.__prompt_to_user_lock = threading.Lock()

        self.__get_output_directory = folder_paths.get_output_directory
        self.__get_temp_directory = folder_paths.get_temp_directory
        self.__get_input_directory = folder_paths.get_input_directory
        self.__get_user_directory = folder_paths.get_user_directory
        self.__get_public_user_directory = folder_paths.get_public_user_directory
        
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
        # First try context variable (works in request handler thread)
        user_id = self._current_user.get()
        if user_id:
            return user_id
        
        # If not in context, try to get from execution context (works in worker thread)
        try:
            from comfy_execution.utils import get_executing_context
            exec_context = get_executing_context()
            if exec_context and exec_context.prompt_id:
                prompt_id = exec_context.prompt_id
                # First try the prompt_to_user mapping (fastest and most reliable)
                with self.__prompt_to_user_lock:
                    user_info = self.__prompt_to_user.get(prompt_id)
                    if user_info and "user_id" in user_info:
                        return user_info["user_id"]
                
                # Fallback: Look up user info from currently_running using prompt_id
                for item_data in self.__prompt_queue.currently_running.values():
                    if isinstance(item_data, dict) and "prompt" in item_data:
                        if item_data["prompt"][1] == prompt_id:  # prompt_id is at index 1
                            user_id = item_data.get("user_id")
                            if user_id:
                                # Cache it in prompt_to_user for faster future lookups
                                username = item_data.get("username")  # Try to get from item first
                                if not username:
                                    try:
                                        _, user = self.users_db.get_user(user_id=user_id)
                                        if user and "username" in user:
                                            username = user["username"]
                                    except:
                                        pass
                                with self.__prompt_to_user_lock:
                                    self.__prompt_to_user[prompt_id] = {"user_id": user_id, "username": username}
                                return user_id
            
            # If no prompt_id or not found in prompt_to_user, check currently_running as fallback
            # This handles cases where SaveImage runs after execution completes
            if self.__prompt_queue.currently_running:
                for item_data in reversed(list(self.__prompt_queue.currently_running.values())):
                    if isinstance(item_data, dict):
                        user_id = item_data.get("user_id")
                        if user_id:
                            # Try to cache it if we can get prompt_id
                            if "prompt" in item_data and isinstance(item_data["prompt"], (list, tuple)) and len(item_data["prompt"]) > 1:
                                item_prompt_id = item_data["prompt"][1]
                                username = item_data.get("username")
                                with self.__prompt_to_user_lock:
                                    self.__prompt_to_user[item_prompt_id] = {"user_id": user_id, "username": username}
                            return user_id
                
                # If we're in worker thread but couldn't find user, return None
                if prompt_id:
                    print(f"[Sentinel] WARNING: Could not find user_id for prompt_id {prompt_id} in worker thread")
                return None
        except Exception as e:
            # Log error for debugging
            print(f"[Sentinel] ERROR in get_current_user_id: {e}")
            import traceback
            traceback.print_exc()
        
        # For request handler thread: don't use class-level variable as fallback - it can be stale!
        # If we can't determine the user, return None to avoid privacy leaks
        return None 

    def get_current_username(self) -> str:
        import traceback
        import threading
        
        # First try context variable (works in request handler thread)
        username = self._current_username.get()
        if username:
            return username
        
        # Try to get prompt_id from execution context first (most accurate)
        prompt_id = None
        try:
            from comfy_execution.utils import get_executing_context
            exec_context = get_executing_context()
            if exec_context and exec_context.prompt_id:
                prompt_id = exec_context.prompt_id
                # Try the prompt_to_user mapping (should be set by patched_execute_async or worker thread)
                with self.__prompt_to_user_lock:
                    user_info = self.__prompt_to_user.get(prompt_id)
                    if user_info and "username" in user_info:
                        return user_info["username"]
        except Exception:
            pass  # Execution context not available, continue to fallback
        
        # CRITICAL: In worker thread, check currently_running as fallback
        # This handles cases where execution context is not set yet or SaveImage runs after execution
        try:
            if prompt_id:
                # If we have prompt_id, look for the specific item
                for item_data in self.__prompt_queue.currently_running.values():
                    if isinstance(item_data, dict) and "prompt" in item_data:
                        if item_data["prompt"][1] == prompt_id:  # Match by prompt_id
                            stored_username = item_data.get("username")
                            if stored_username:
                                # Cache it
                                with self.__prompt_to_user_lock:
                                    self.__prompt_to_user[prompt_id] = {
                                        "user_id": item_data.get("user_id"),
                                        "username": stored_username
                                    }
                                return stored_username
                            
                            # If no username but we have user_id, try to get from users_db
                            user_id = item_data.get("user_id")
                            if user_id:
                                try:
                                    _, user = self.users_db.get_user(user_id=user_id)
                                    if user and "username" in user:
                                        username = user["username"]
                                        # Cache it
                                        with self.__prompt_to_user_lock:
                                            self.__prompt_to_user[prompt_id] = {"user_id": user_id, "username": username}
                                        return username
                                except Exception:
                                    pass
            
            # If no prompt_id or not found, get the most recent item (fallback)
            # This handles cases where SaveImage is called from callbacks after execution completes
            if self.__prompt_queue.currently_running:
                # Iterate in reverse order to get the most recent item first
                for item_data in reversed(list(self.__prompt_queue.currently_running.values())):
                    if isinstance(item_data, dict):
                        stored_username = item_data.get("username")
                        if stored_username:
                            # Try to cache it if we can get prompt_id
                            if "prompt" in item_data and isinstance(item_data["prompt"], (list, tuple)) and len(item_data["prompt"]) > 1:
                                item_prompt_id = item_data["prompt"][1]
                                with self.__prompt_to_user_lock:
                                    self.__prompt_to_user[item_prompt_id] = {
                                        "user_id": item_data.get("user_id"),
                                        "username": stored_username
                                    }
                            return stored_username
        except Exception as e:
            # Log error for debugging (only first few times)
            if not hasattr(self, '_username_error_logged'):
                self._username_error_logged = 0
            if self._username_error_logged < 3:
                print(f"[Sentinel] ERROR checking currently_running in get_current_username: {e}")
                self._username_error_logged += 1
        
        # If we can't determine the user, return None to avoid privacy leaks
        return None 

    
    def get_user_output_directory(self) -> str:
        import threading
        
        base_output_path = self.config.get("user_outputs_base", "")
        
        # CORRECTION CRITIQUE : On normalise les slashs (transforme / en \ sur Windows)
        if base_output_path:
            base_output_path = os.path.normpath(base_output_path)

        username = self.get_current_username()
        # Only log occasionally to avoid spam
        if not hasattr(self, '_output_dir_log_counter'):
            self._output_dir_log_counter = 0
        self._output_dir_log_counter += 1
        thread_name = threading.current_thread().name
        if 'prompt_worker' in thread_name or self._output_dir_log_counter % 50 == 0:
            print(f"[Sentinel] DEBUG get_user_output_directory: username={username} (thread: {thread_name}, call #{self._output_dir_log_counter})")
        
        if not username:
            fallback_dir = self.__get_output_directory()
            if self._output_dir_log_counter <= 3:  # Log first 3 warnings
                print(f"[Sentinel] WARNING get_user_output_directory: No username, using fallback directory: {fallback_dir}")
            return fallback_dir
        
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

    
    def patched_get_request_user_filepath(self, original_get_request_user_filepath, access_control_instance):
        """Patch UserManager.get_request_user_filepath to redirect workflows to Sentinel user directory."""
        def get_request_user_filepath(self, request, file, type="userdata", create_dir=True):
            # Check if this is a workflow file request
            if file and 'workflows' in file:
                # Get Sentinel username
                username = access_control_instance.get_current_username()
                if username:
                    # Get Sentinel workflows directory
                    workflows_dir = access_control_instance.get_user_workflows_directory()
                    # Replace "workflows" with "__workflows" in the file path if needed
                    if file.startswith('workflows/'):
                        file = file.replace('workflows/', '__workflows/', 1)
                    elif file == 'workflows':
                        file = '__workflows'
                    
                    # Construct the full path
                    file_path = os.path.join(workflows_dir, file) if file != '__workflows' else workflows_dir
                    
                    # Ensure parent directory exists
                    if create_dir:
                        parent = os.path.dirname(file_path) if file != '__workflows' else workflows_dir
                        os.makedirs(parent, exist_ok=True)
                    
                    return file_path
            
            # For non-workflow requests, use original behavior
            return original_get_request_user_filepath(self, request, file, type, create_dir)
        
        return get_request_user_filepath
    
    def patched_get_public_user_directory(self, original_get_public_user_directory):
        """Patch get_public_user_directory to return Sentinel user-specific directories."""
        def get_public_user_directory(user_id: str) -> str | None:
            # Check if this user_id matches a Sentinel username
            username = self.get_current_username()
            if username and user_id == username:
                # This is a Sentinel user - return their __userdata directory
                comfyui_user_base = self.config.get("comfyui_user_base", "")
                if comfyui_user_base:
                    # Return the __userdata directory
                    return self._get_userdata_directory()
            
            # For non-Sentinel users or when Sentinel is not active, use original behavior
            return original_get_public_user_directory(user_id)
        
        return get_public_user_directory
    
    def _get_comfyui_user_base(self) -> str:
        """Get the base ComfyUI user directory, replacing [userid] placeholder with username."""
        comfyui_user_base = self.config.get("comfyui_user_base", "")
        
        if not comfyui_user_base:
            # If not configured, use default user directory
            username = self.get_current_username()
            if username:
                return os.path.join(self.__get_user_directory(), username)
            return os.path.join(self.__get_user_directory(), "default")
        
        # Normalize path
        comfyui_user_base = os.path.normpath(comfyui_user_base)
        
        # Get current username
        username = self.get_current_username()
        
        if not username:
            # If no user logged in, return default user directory
            return os.path.join(self.__get_user_directory(), "default")
        
        # Replace [userid] placeholder with username
        comfyui_user_base = comfyui_user_base.replace("[userid]", username)
        
        return comfyui_user_base
    
    def get_user_workflows_directory(self) -> str:
        """Get user-specific workflows directory.
        
        Structure: {comfyui_user_base}/__userdata/workflows/
        """
        base = self._get_comfyui_user_base()
        workflows_dir = os.path.join(base, "__userdata", "workflows")
        
        # Ensure directory exists
        try:
            os.makedirs(workflows_dir, exist_ok=True)
        except Exception as e:
            print(f"[Sentinel] ERROR: Failed to create user workflows directory {workflows_dir}: {e}")
            return os.path.join(self.__get_user_directory(), "default", "__userdata", "workflows")
        
        return workflows_dir
    
    def get_shared_workflows_directory(self) -> str:
        """Get shared workflows directory accessible to all users.
        
        Structure: {comfyui_shared_base}/__userdata/workflows/
        """
        comfyui_shared_base = self.config.get("comfyui_shared_base", "")
        
        if not comfyui_shared_base:
            return None
        
        # Normalize path
        comfyui_shared_base = os.path.normpath(comfyui_shared_base)
        workflows_dir = os.path.join(comfyui_shared_base, "__userdata", "workflows")
        
        # Ensure directory exists
        try:
            os.makedirs(workflows_dir, exist_ok=True)
        except Exception as e:
            print(f"[Sentinel] ERROR: Failed to create shared workflows directory {workflows_dir}: {e}")
            return None
        
        return workflows_dir
    
    def _get_userdata_directory(self, is_shared=False) -> str:
        """Get the __userdata directory for user preferences and workflows.
        
        Args:
            is_shared: If True, return shared __userdata directory instead of user-specific.
        
        Structure: {comfyui_user_base}/__userdata/ or {comfyui_shared_base}/__userdata/
        """
        if is_shared:
            shared_base = self.config.get("comfyui_shared_base", "")
            if shared_base:
                userdata_dir = os.path.join(shared_base, "__userdata")
                # Ensure directory exists
                try:
                    os.makedirs(userdata_dir, exist_ok=True)
                except Exception as e:
                    print(f"[Sentinel] ERROR: Failed to create shared userdata directory {userdata_dir}: {e}")
                    return None
                return userdata_dir
            return None
        else:
            base = self._get_comfyui_user_base()
            userdata_dir = os.path.join(base, "__userdata")
            
            # Ensure directory exists
            try:
                os.makedirs(userdata_dir, exist_ok=True)
            except Exception as e:
                print(f"[Sentinel] ERROR: Failed to create userdata directory {userdata_dir}: {e}")
                return os.path.join(self.__get_user_directory(), "default", "__userdata")
            
            return userdata_dir
    
    def get_user_temp_directory(self) -> str:
        """Get user-specific temp directory for preview images.
        
        Structure: {comfyui_user_base}/__temp/
        """
        comfyui_user_base = self.config.get("comfyui_user_base", "")
        
        if not comfyui_user_base:
            # If not configured, use default temp directory
            return self.__get_temp_directory()
        
        base = self._get_comfyui_user_base()
        temp_dir = os.path.join(base, "__temp")
        
        # Ensure directory exists
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except Exception as e:
            print(f"[Sentinel] ERROR: Failed to create user temp directory {temp_dir}: {e}")
            return self.__get_temp_directory()
        
        return temp_dir
    
    def get_user_input_directory(self) -> str:
        """Get user-specific input directory.
        
        Structure: {comfyui_user_base}/__input/
        """
        comfyui_user_base = self.config.get("comfyui_user_base", "")
        
        if not comfyui_user_base:
            # If not configured, return default input directory
            return self.__get_input_directory()
        
        base = self._get_comfyui_user_base()
        input_dir = os.path.join(base, "__input")
        
        # Ensure directory exists
        try:
            os.makedirs(input_dir, exist_ok=True)
        except Exception as e:
            print(f"[Sentinel] ERROR: Failed to create user input directory {input_dir}: {e}")
            return self.__get_input_directory()
        
        return input_dir
        
    def patched_get_filename_list(self, folder_name: str) -> list[str]:
        if folder_name not in ["loras", "checkpoints", "input"]:
             return self.__get_filename_list(folder_name)
        
        # Handle input folder separately
        if folder_name == "input":
            return self._get_input_filename_list()

        all_found_files = set()
        
        if folder_name == "loras":
            extensions = folder_paths.supported_pt_extensions
        elif folder_name == "checkpoints":
            extensions = folder_paths.supported_pt_extensions
        else:
            extensions = []

        # 1. Use cached version for base directory (much faster!)
        # This uses the cache we've already set up, avoiding expensive re-scans
        try:
            base_files = self.__get_filename_list(folder_name)
            all_found_files.update(base_files)
        except Exception as e:
            print(f"[Sentinel] LIST: Error getting base files: {e}")

        # 2. Scan RESEAU (user-specific directories)
        username = self.get_current_username()
        base_path = ""
        
        if folder_name == "loras":
            base_path = self.config.get("user_loras_base", "")
        elif folder_name == "checkpoints":
            base_path = self.config.get("user_checkpoints_base", "")
            
        if not base_path:
            return sorted(list(all_found_files))
            
        # CORRECTION : Normalisation ici aussi par sécurité
        base_path = os.path.normpath(base_path)

        # Check if fast cache mode is enabled to optimize scanning
        from comfy.cli_args import args
        use_fast_cache = getattr(args, 'fast_file_list_cache', False)

        try:
            # Dossier utilisateur
            if username:
                user_path = os.path.join(base_path, username)
                if os.path.isdir(user_path):
                    if use_fast_cache:
                        # In fast cache mode, use a simpler scan to avoid expensive mtime checks
                        # Only get filenames, skip metadata
                        for root, dirs, files in os.walk(user_path):
                            # Filter hidden directories
                            dirs[:] = [d for d in dirs if not d.startswith(".")]
                            for file in files:
                                if not file.startswith("."):
                                    file_ext = os.path.splitext(file)[1].lower()
                                    if file_ext in extensions:
                                        rel_path = os.path.relpath(os.path.join(root, file), user_path)
                                        all_found_files.add(os.path.join(username, rel_path).replace("\\", "/"))
                    else:
                        # Normal mode: use recursive_search (slower but includes metadata)
                        files, _ = folder_paths.recursive_search(user_path)
                        user_files = folder_paths.filter_files_extensions(files, extensions)
                        for f in user_files:
                            all_found_files.add(os.path.join(username, f).replace("\\", "/"))

            # Dossier commun
            common_path = os.path.join(base_path, "common")
            if os.path.isdir(common_path):
                if use_fast_cache:
                    # In fast cache mode, use a simpler scan
                    for root, dirs, files in os.walk(common_path):
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for file in files:
                            if not file.startswith("."):
                                file_ext = os.path.splitext(file)[1].lower()
                                if file_ext in extensions:
                                    rel_path = os.path.relpath(os.path.join(root, file), common_path)
                                    all_found_files.add(os.path.join("common", rel_path).replace("\\", "/"))
                else:
                    # Normal mode: use recursive_search
                    files, _ = folder_paths.recursive_search(common_path)
                    common_files = folder_paths.filter_files_extensions(files, extensions)
                    for f in common_files:
                        all_found_files.add(os.path.join("common", f).replace("\\", "/"))
        
        except Exception as e:
            print(f"[Sentinel] LIST: Error scanning network paths: {e}")

        return sorted(list(all_found_files))
    
    def _get_input_filename_list(self) -> list[str]:
        """Get list of input files for current user only."""
        all_found_files = set()
        
        # Get user-specific input directory
        user_input_dir = self.get_user_input_directory()
        username = self.get_current_username()
        user_id = self.get_current_user_id()
        
        # If no user logged in, return empty list (privacy: don't show any files)
        if not username and not user_id:
            return []
        
        # Check if comfyui_user_base is configured
        comfyui_user_base = self.config.get("comfyui_user_base", "")
        if not comfyui_user_base:
            # If not configured, use default behavior (show all files)
            return self.__get_filename_list("input")
        
        # Only scan user's input directory
        try:
            if os.path.isdir(user_input_dir):
                # Get image extensions
                image_extensions = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tiff', '.tif']
                
                # Scan user's input directory recursively
                from comfy.cli_args import args
                use_fast_cache = getattr(args, 'fast_file_list_cache', False)
                
                if use_fast_cache:
                    # Fast cache mode: simple walk
                    for root, dirs, files in os.walk(user_input_dir):
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for file in files:
                            if not file.startswith("."):
                                file_ext = os.path.splitext(file)[1].lower()
                                if file_ext in image_extensions:
                                    rel_path = os.path.relpath(os.path.join(root, file), user_input_dir)
                                    all_found_files.add(rel_path.replace("\\", "/"))
                else:
                    # Normal mode: use recursive_search
                    files, _ = folder_paths.recursive_search(user_input_dir)
                    for f in files:
                        file_ext = os.path.splitext(f)[1].lower()
                        if file_ext in image_extensions:
                            rel_path = os.path.relpath(f, user_input_dir)
                            all_found_files.add(rel_path.replace("\\", "/"))
        except Exception as e:
            print(f"[Sentinel] LIST: Error scanning user input directory: {e}")
        
        return sorted(list(all_found_files))
        
    def patched_get_full_path(self, folder_name: str, filename: str) -> str | None:
        if folder_name == "loras":
            base_lora_path = self.config.get("user_loras_base", "")
            if base_lora_path:
                # Normalisation du chemin de base
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
                        print(f"[Sentinel] LORA LOAD: Found (User Path): {user_path}")
                        return user_path

                common_path = os.path.join(base_lora_path, "common", filename)
                if os.path.isfile(common_path):
                    print(f"[Sentinel] LORA LOAD: Found (Common Path): {common_path}")
                    return common_path

            return self.__get_full_path(folder_name, filename)

        return self.__get_full_path(folder_name, filename)

    def patched_save_images(self, original_save_images):
        """Wrapper for SaveImage.save_images to get output directory dynamically."""
        def save_images(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
            # CRITICAL: Always update output_dir dynamically based on current user context
            # This is essential because node instances are cached by node ID and shared across users
            # When User B loads User A's workflow, they share the same cached node instance
            # which still has self.output_dir pointing to User A's directory
            old_output_dir = getattr(self, 'output_dir', None)
            
            # ALWAYS get the current user's output directory - don't rely on cached value
            current_output_dir = folder_paths.get_output_directory()
            self.output_dir = current_output_dir
            
            # Debug logging (only first few times to avoid spam)
            if not hasattr(self, '_save_images_log_count'):
                self._save_images_log_count = 0
            self._save_images_log_count += 1
            if self._save_images_log_count <= 3 or old_output_dir != current_output_dir:
                username = self.get_current_username() or "unknown"
                print(f"[Sentinel] DEBUG: SaveImage.save_images - user={username}, updated output_dir from '{old_output_dir}' to '{current_output_dir}'")
            
            # Call the original save_images method
            # The original method uses self.output_dir, which we just updated
            return original_save_images(self, images, filename_prefix, prompt, extra_pnginfo)
        return save_images
    
    def patch_folder_paths(self) -> None:
        folder_paths.get_filename_list = self.patched_get_filename_list
        folder_paths.get_output_directory = self.get_user_output_directory
        folder_paths.get_input_directory = self.get_user_input_directory
        folder_paths.get_temp_directory = self.get_user_temp_directory
        folder_paths.get_full_path = self.patched_get_full_path
        # Patch get_public_user_directory for workflow isolation
        if not hasattr(folder_paths, '_original_get_public_user_directory'):
            folder_paths._original_get_public_user_directory = folder_paths.get_public_user_directory
        folder_paths.get_public_user_directory = self.patched_get_public_user_directory(folder_paths._original_get_public_user_directory)
        # Patch UserManager.get_request_user_filepath for workflow isolation
        self.patch_user_manager()
        self.patch_prompt_queue()
    
    def patch_user_manager(self) -> None:
        """Patch UserManager to use Sentinel authentication for user separation."""
        try:
            from app import user_manager as um_module
            if hasattr(self.server, 'user_manager'):
                user_manager = self.server.user_manager
                # Store original methods
                if not hasattr(user_manager, '_original_get_request_user_id'):
                    user_manager._original_get_request_user_id = user_manager.get_request_user_id
                if not hasattr(user_manager, '_original_get_request_user_filepath'):
                    user_manager._original_get_request_user_filepath = user_manager.get_request_user_filepath
                if not hasattr(user_manager, '_original_add_routes'):
                    user_manager._original_add_routes = user_manager.add_routes
                
                # Patch get_request_user_id to return Sentinel username when Sentinel user is logged in
                def patched_get_request_user_id(request):
                    # Check if Sentinel user is logged in
                    username = self.get_current_username()
                    if username:
                        # Ensure Sentinel user exists in ComfyUI's users dict (for compatibility)
                        # This allows ComfyUI's user directory logic to work with Sentinel authentication
                        if username not in user_manager.users:
                            user_manager.users[username] = username
                        return username
                    
                    # Fall back to original behavior (returns "default" when multi-user is disabled)
                    return user_manager._original_get_request_user_id(request)
                
                user_manager.get_request_user_id = patched_get_request_user_id
                
                # Patch get_request_user_filepath to handle workflow mapping
                def patched_get_request_user_filepath(request, file, type="userdata", create_dir=True):
                    # Get the user_id (which will be Sentinel username if logged in, thanks to patched_get_request_user_id)
                    user_id = user_manager.get_request_user_id(request)
                    
                    # Check if this is a Sentinel user (not "default")
                    username = self.get_current_username()
                    if username and user_id == username:
                        # This is a Sentinel user - handle all userdata requests
                        # Get the user root from get_public_user_directory (which we patched)
                        user_root = folder_paths.get_public_user_directory(user_id)
                        if user_root:
                            # Check if this is a workflow-related request
                            is_workflow_request = False
                            if file:
                                is_workflow_request = 'workflows' in file
                            else:
                                # Check query parameter for workflows (used in /v2/userdata?path=workflows)
                                if hasattr(request.rel_url, 'query') and 'path' in request.rel_url.query:
                                    path_query = request.rel_url.query['path']
                                    is_workflow_request = 'workflows' in path_query
                            
                            if is_workflow_request:
                                # Check if this is a shared workflow
                                if file and (file.startswith('Shared/') or file.startswith('workflows/Shared/') or file.startswith('workflows/global/')):
                                    # Check if this is a DELETE request - block deletion of shared workflows
                                    if request.method == 'DELETE':
                                        # Return None to block deletion (will result in 403 error)
                                        print(f"[Sentinel] BLOCKED: Attempt to delete shared workflow: {file}")
                                        return None
                                    
                                    # Shared workflow - use shared workflows directory
                                    shared_workflows_dir = self.get_shared_workflows_directory()
                                    if shared_workflows_dir:
                                        # Remove "Shared/" or "workflows/Shared/" or "workflows/global/" prefix (backward compatibility)
                                        if file.startswith('Shared/'):
                                            relative_path = file[len('Shared/'):]
                                        elif file.startswith('workflows/Shared/'):
                                            relative_path = file[len('workflows/Shared/'):]
                                        else:
                                            relative_path = file[len('workflows/global/'):]
                                        file_path = os.path.join(shared_workflows_dir, relative_path)
                                        
                                        # Ensure parent directory exists
                                        if create_dir:
                                            parent = os.path.dirname(file_path)
                                            os.makedirs(parent, exist_ok=True)
                                        
                                        return file_path
                                    # Fall through to user workflows if global directory not configured
                                
                                # User workflow - use user workflows directory
                                workflows_dir = self.get_user_workflows_directory()
                                
                                if file:
                                    # File path is like "workflows/myworkflow.json" - map to workflows_dir
                                    if file.startswith('workflows/'):
                                        # Remove "workflows/" prefix and join with workflows_dir
                                        relative_path = file[len('workflows/'):]
                                        file_path = os.path.join(workflows_dir, relative_path)
                                    elif file == 'workflows':
                                        file_path = workflows_dir
                                    else:
                                        # File doesn't start with workflows/ - join directly
                                        file_path = os.path.join(workflows_dir, file)
                                else:
                                    # file is None - check if path query is "workflows"
                                    if hasattr(request.rel_url, 'query') and request.rel_url.query.get('path') == 'workflows':
                                        file_path = workflows_dir
                                    else:
                                        # Return user root for non-workflow root listing
                                        file_path = user_root
                                
                                # Ensure parent directory exists
                                if create_dir:
                                    parent = os.path.dirname(file_path) if file_path != workflows_dir else workflows_dir
                                    os.makedirs(parent, exist_ok=True)
                                
                                return file_path
                            else:
                                # Non-workflow request - construct path relative to user_root
                                if file is None:
                                    file_path = user_root
                                else:
                                    # URL decode if needed
                                    if "%" in file:
                                        from urllib import parse
                                        file = parse.unquote(file)
                                    
                                    # Construct absolute path
                                    file_path = os.path.abspath(os.path.join(user_root, file))
                                    
                                    # Basic security check: ensure path is within user_root
                                    # Use string comparison instead of os.path.commonpath for UNC paths
                                    user_root_norm = os.path.normpath(user_root)
                                    file_path_norm = os.path.normpath(file_path)
                                    if not file_path_norm.startswith(user_root_norm):
                                        return None
                                
                                # Ensure parent directory exists
                                if create_dir:
                                    parent = os.path.dirname(file_path) if file_path != user_root else user_root
                                    os.makedirs(parent, exist_ok=True)
                                
                                return file_path
                    
                    # For non-Sentinel users, use original behavior
                    # But we need to handle the case where Sentinel user directory might be on different drive
                    # by catching the ValueError and falling back to a safe path construction
                    try:
                        return user_manager._original_get_request_user_filepath(request, file, type, create_dir)
                    except ValueError as e:
                        # Handle case where paths are on different drives (UNC vs local)
                        if "don't have the same drive" in str(e) or "Paths don't have the same drive" in str(e):
                            # This shouldn't happen for non-Sentinel users, but handle it gracefully
                            # For Sentinel users, we already handled it above
                            # For non-Sentinel users, return None to indicate invalid path
                            return None
                        raise
                
                user_manager.get_request_user_filepath = patched_get_request_user_filepath
                
                # Store reference for middleware
                self._patched_user_manager = user_manager
                
                # Patch AppSettings to merge global settings with user settings
                # Global settings will override user settings for UI elements
                from app import app_settings
                if hasattr(user_manager, 'settings'):
                    app_settings_instance = user_manager.settings
                    if not hasattr(app_settings_instance, '_original_get_settings'):
                        app_settings_instance._original_get_settings = app_settings_instance.get_settings
                    
                    def patched_get_settings(request):
                        # Get user settings first
                        user_settings = app_settings_instance._original_get_settings(request)
                        
                        # Get shared settings from shared __userdata directory
                        shared_settings = {}
                        shared_userdata_dir = self._get_userdata_directory(is_shared=True)
                        if shared_userdata_dir:
                            shared_settings_file = os.path.join(shared_userdata_dir, "comfy.settings.json")
                            if os.path.exists(shared_settings_file):
                                try:
                                    with open(shared_settings_file, 'r') as f:
                                        shared_settings = json.load(f)
                                    print(f"[Sentinel] DEBUG: Loaded shared settings from {shared_settings_file}")
                                except Exception as e:
                                    print(f"[Sentinel] WARNING: Could not load shared settings: {e}")
                        
                        # Merge settings: shared settings override user settings
                        # Use deep merge for nested dictionaries
                        merged_settings = self._deep_merge_settings(user_settings.copy(), shared_settings)
                        
                        return merged_settings
                    
                    app_settings_instance.get_settings = patched_get_settings
                    print("[Sentinel] Patched AppSettings.get_settings to merge global settings")
        except Exception as e:
            print(f"[Sentinel] WARNING: Could not patch UserManager: {e}")
            import traceback
            traceback.print_exc()
    
    def _deep_merge_settings(self, user_settings, shared_settings):
        """Deep merge shared settings into user settings, with shared taking precedence."""
        import copy
        result = copy.deepcopy(user_settings)
        
        for key, value in shared_settings.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                # Recursively merge nested dictionaries
                result[key] = self._deep_merge_settings(result[key], value)
            else:
                # Shared settings override user settings
                result[key] = copy.deepcopy(value)
        
        return result
    
    def patch_save_image_nodes(self):
        """Patch SaveImage and PreviewImage nodes to get output directory dynamically."""
        # Patch SaveImage to get output directory dynamically
        try:
            import nodes
            if hasattr(nodes, 'SaveImage'):
                # CRITICAL: Patch __init__ to ensure output_dir is always set dynamically
                # This prevents cached node instances from using stale user directories
                if not hasattr(nodes.SaveImage, '_original_init'):
                    nodes.SaveImage._original_init = nodes.SaveImage.__init__
                
                def patched_save_image_init(self):
                    # Call original __init__ first
                    nodes.SaveImage._original_init(self)
                    # Then update output_dir dynamically based on current user context
                    # This ensures even cached instances get the correct directory
                    self.output_dir = folder_paths.get_output_directory()
                
                nodes.SaveImage.__init__ = patched_save_image_init
                
                # Store original method
                if not hasattr(nodes.SaveImage, '_original_save_images'):
                    nodes.SaveImage._original_save_images = nodes.SaveImage.save_images
                # Patch with dynamic output directory (double-check in save_images too)
                nodes.SaveImage.save_images = self.patched_save_images(nodes.SaveImage._original_save_images)
            
            # Patch PreviewImage to get temp directory dynamically (same approach as SaveImage)
            if hasattr(nodes, 'PreviewImage'):
                # CRITICAL: Patch __init__ to ensure output_dir is always set dynamically
                if not hasattr(nodes.PreviewImage, '_original_init'):
                    nodes.PreviewImage._original_init = nodes.PreviewImage.__init__
                
                def patched_preview_image_init(self):
                    # Call original __init__ first
                    nodes.PreviewImage._original_init(self)
                    # Then update output_dir dynamically based on current user context
                    self.output_dir = folder_paths.get_temp_directory()
                
                nodes.PreviewImage.__init__ = patched_preview_image_init
                
                # Store original method
                if not hasattr(nodes.PreviewImage, '_original_save_images'):
                    nodes.PreviewImage._original_save_images = nodes.PreviewImage.save_images
                # Patch with dynamic temp directory (double-check in save_images too)
                def patched_preview_save_images(original_save_images):
                    def save_images(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
                        # Update output_dir dynamically based on current user context
                        self.output_dir = folder_paths.get_temp_directory()
                        # Call the original save_images method
                        return original_save_images(self, images, filename_prefix, prompt, extra_pnginfo)
                    return save_images
                nodes.PreviewImage.save_images = patched_preview_save_images(nodes.PreviewImage._original_save_images)
        except Exception as e:
            print(f"[Sentinel] WARNING: Could not patch SaveImage/PreviewImage: {e}")
            import traceback
            traceback.print_exc()
        
        # Note: LoadImage and LoadImageMask don't need patching for user isolation
        # because folder_paths.get_input_directory() already returns user-specific directory.
        # The original code using os.listdir(get_input_directory()) will only show files
        # from the current user's directory, just like LoadVideo, LoadAudio, and Load3D.
        # 
        # If recursive scanning (subdirectories) is needed in the future, we can patch
        # them to use folder_paths.get_filename_list("input") instead.
        
    
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
        import traceback
        import threading
        
        # Extract prompt_id from item to track it
        prompt_id = None
        if isinstance(item, (list, tuple)) and len(item) > 1:
            prompt_id = item[1]
        
        username = self.get_current_username()
        user_id = self.get_current_user_id()
        
        if not username:
            print(f"[Sentinel] WARNING: No username found in context when adding prompt to queue (prompt_id={prompt_id})")

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
            
            except Exception as e:
                print(f"[Sentinel] ERROR patching workflow JSON: {e}")

        # Store both user_id and username in the item for worker thread access
        item_with_user = {"prompt": item, "user_id": user_id, "username": username}
        
        # Wrap in ComparableQueueItem to make it comparable for heapq
        wrapped_item = ComparableQueueItem(item_with_user)
        self.__prompt_queue_put(wrapped_item)


    def user_queue_get(self, timeout=None):
        # Try to patch prompt_worker lazily if not already patched
        # This avoids circular import issues during Sentinel initialization
        if not getattr(self, '_prompt_worker_patched', False):
            try:
                import sys
                if 'main' in sys.modules:
                    main = sys.modules['main']
                    if hasattr(main, 'prompt_worker') and not hasattr(main, '_original_prompt_worker'):
                        # Ensure lock exists
                        if not hasattr(self, '_prompt_worker_patch_lock'):
                            self._prompt_worker_patch_lock = threading.Lock()
                        
                        with self._prompt_worker_patch_lock:
                            # Double-check after acquiring lock
                            if not getattr(self, '_prompt_worker_patched', False) and hasattr(main, 'prompt_worker') and not hasattr(main, '_original_prompt_worker'):
                                main._original_prompt_worker = main.prompt_worker
                                patched_worker = self.patched_prompt_worker(main._original_prompt_worker, self)
                                main.prompt_worker = patched_worker
                                self._prompt_worker_patched = True
                                print("[Sentinel] SUCCESS: Lazy-patched prompt_worker to ensure task_done is always called")
            except Exception:
                # Silently fail - we'll try again next time
                pass
        
        user_queue = self.__prompt_queue.queue
        with self.__prompt_queue.not_empty:
            while len(user_queue) == 0:
                self.__prompt_queue.not_empty.wait(timeout=timeout)
                if timeout is not None and len(user_queue) == 0:
                    return None
            item = heapq.heappop(user_queue)
            i = self.__prompt_queue.task_counter
            # Unwrap if it's a ComparableQueueItem, otherwise use as-is
            if isinstance(item, ComparableQueueItem):
                item_data = item.data
            else:
                item_data = item
            # Add timestamp to track how long item has been running
            import time
            item_data["_start_time"] = time.time()
            self.__prompt_queue.currently_running[i] = copy.deepcopy(item_data)
            self.__prompt_queue.task_counter += 1
            self.server.queue_updated()
            return (item_data["prompt"], i) 


    def user_queue_task_done(
        self, item_id, history_result, status: Optional["PromptQueue.ExecutionStatus"], process_item=None
    ):
        item_removed = False
        try:
            with self.__prompt_queue.mutex:
                # CRITICAL: Use pop with default to avoid KeyError if item_id doesn't exist
                # This can happen if task_done is called twice or item was already removed
                prompt = self.__prompt_queue.currently_running.pop(item_id, None)
                if prompt is None:
                    print(f"[Sentinel] WARNING: task_done called for item_id {item_id} but item not found in currently_running")
                    # Still update queue status to ensure UI is notified
                    self.server.queue_updated()
                    return
                
                item_removed = True  # Mark that we successfully removed the item
                
                if len(self.__prompt_queue.history) > MAXIMUM_HISTORY_SIZE:
                    self.__prompt_queue.history.pop(next(iter(self.__prompt_queue.history)))

                status_dict: Optional[dict] = None
                if status is not None:
                    status_dict = copy.deepcopy(status._asdict())

                # Extract the tuple from the dict structure
                prompt_tuple = prompt["prompt"] if isinstance(prompt, dict) and "prompt" in prompt else prompt
                
                # Apply process_item if provided (it expects and returns a tuple)
                if process_item is not None:
                    prompt_tuple = process_item(prompt_tuple)
                
                # Get user_id if available
                user_id = prompt.get("user_id") if isinstance(prompt, dict) else None
                
                prompt_id = prompt_tuple[1] if len(prompt_tuple) > 1 else "unknown"
                
                self.__prompt_queue.history[prompt_tuple[1]] = {
                    "prompt": prompt_tuple,
                    "outputs": {},
                    "status": status_dict,
                    "user_id": user_id,
                }
                self.__prompt_queue.history[prompt_tuple[1]].update(history_result)
                
                # DON'T clean up prompt_to_user mapping here - save_images might be called asynchronously
                # or get_user_output_directory might be called from UI callbacks after execution completes
                # Keep the mapping - it will be cleaned up when a new prompt starts or after a delay
                
                self.server.queue_updated()
                print(f"[Sentinel] DEBUG: task_done completed for prompt_id={prompt_id}, item_id={item_id}")
        except Exception as e:
            # CRITICAL: Always remove item from currently_running even if there's an error
            # This prevents items from getting stuck
            print(f"[Sentinel] ERROR in user_queue_task_done for item_id {item_id}: {e}")
            import traceback
            traceback.print_exc()
            if not item_removed:
                # Only try to remove if we didn't already remove it
                try:
                    with self.__prompt_queue.mutex:
                        self.__prompt_queue.currently_running.pop(item_id, None)
                        self.server.queue_updated()
                        print(f"[Sentinel] DEBUG: Removed stuck item_id {item_id} after error")
                except Exception as e2:
                    print(f"[Sentinel] CRITICAL: Failed to remove item_id {item_id} even in error handler: {e2}")

    def user_queue_get_current_queue(self):
        with self.__prompt_queue.mutex:
            # Convert dict items back to tuples for running queue (ComfyUI expects tuples)
            out = []
            for x in self.__prompt_queue.currently_running.values():
                if isinstance(x, dict) and "prompt" in x:
                    # Extract the original tuple from the dict
                    out.append(x["prompt"])
                else:
                    out.append(x)
            # Unwrap ComparableQueueItem instances for the queue copy and extract tuples
            unwrapped_queue = []
            for item in self.__prompt_queue.queue:
                if isinstance(item, ComparableQueueItem):
                    # Extract the original tuple from the wrapped dict
                    unwrapped_queue.append(item.data["prompt"])
                else:
                    unwrapped_queue.append(item)
            return (out, copy.deepcopy(unwrapped_queue))
    
    def user_queue_get_current_queue_volatile(self):
        with self.__prompt_queue.mutex:
            # Convert dict items back to tuples for running queue (ComfyUI expects tuples)
            running = []
            for x in self.__prompt_queue.currently_running.values():
                if isinstance(x, dict) and "prompt" in x:
                    # Extract the original tuple from the dict
                    running.append(x["prompt"])
                else:
                    running.append(x)
            # Unwrap ComparableQueueItem instances for queued items
            queued = []
            for item in self.__prompt_queue.queue:
                if isinstance(item, ComparableQueueItem):
                    # Extract the original tuple from the wrapped dict
                    queued.append(item.data["prompt"])
                else:
                    queued.append(item)
            return (running, queued)

    def user_queue_wipe_queue(self):
        with self.__prompt_queue.mutex:
            current_user_id = self.get_current_user_id()
            self.__prompt_queue.queue = [
                item
                for item in self.__prompt_queue.queue
                if (item.data if isinstance(item, ComparableQueueItem) else item)["user_id"] != current_user_id
            ]
            self.server.queue_updated()

    def user_queue_delete_queue_item(self, function):
        with self.__prompt_queue.mutex:
            current_user_id = self.get_current_user_id()
            for x in range(len(self.__prompt_queue.queue)):
                item = self.__prompt_queue.queue[x]
                item_data = item.data if isinstance(item, ComparableQueueItem) else item
                if (
                    function(item_data)
                    and item_data["user_id"] == current_user_id
                ):
                    if len(self.__prompt_queue.queue) == 1:
                        self.__prompt_queue.wipe_queue()
                    else:
                        self.__prompt_queue.queue.pop(x)
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

    def delete_running_item_by_prompt_id(self, prompt_id):
        """
        Remove a stuck item from currently_running by prompt_id.
        This is a recovery mechanism for when items get stuck due to exceptions.
        Returns True if an item was removed, False otherwise.
        """
        with self.__prompt_queue.mutex:
            for item_id, item in list(self.__prompt_queue.currently_running.items()):
                # item structure: dict with "prompt" key containing tuple (number, prompt_id, ...)
                if isinstance(item, dict) and "prompt" in item:
                    item_prompt_id = item["prompt"][1] if len(item["prompt"]) > 1 else None
                elif isinstance(item, (list, tuple)) and len(item) > 1:
                    item_prompt_id = item[1]
                else:
                    continue
                    
                if item_prompt_id == prompt_id:
                    self.__prompt_queue.currently_running.pop(item_id)
                    self.server.queue_updated()
                    print(f"[Sentinel] Removed stuck item from queue (prompt_id: {prompt_id}, item_id: {item_id})")
                    return True
            return False

    def delete_running_items_by_user(self, user_identifier):
        """
        Remove stuck items from currently_running for a specific user.
        user_identifier can be a user_id or username.
        Returns the number of items removed.
        """
        removed_count = 0
        with self.__prompt_queue.mutex:
            for item_id, item in list(self.__prompt_queue.currently_running.items()):
                # item structure: dict with "prompt", "user_id", "username" keys
                if isinstance(item, dict):
                    item_user_id = item.get("user_id")
                    item_username = item.get("username")
                    
                    # Match if any of the identifiers match
                    if (item_user_id == user_identifier or item_username == user_identifier):
                        self.__prompt_queue.currently_running.pop(item_id)
                        removed_count += 1
                        prompt_id = item["prompt"][1] if "prompt" in item and len(item["prompt"]) > 1 else "unknown"
                        print(f"[Sentinel] Removed stuck item from queue (user: {user_identifier}, prompt_id: {prompt_id}, item_id: {item_id})")
            if removed_count > 0:
                self.server.queue_updated()
        return removed_count

    def emergency_clear_all_running(self):
        """
        EMERGENCY: Clear ALL items from currently_running.
        Use only when queue is completely blocked.
        Returns the number of items cleared.
        """
        with self.__prompt_queue.mutex:
            cleared_count = len(self.__prompt_queue.currently_running)
            self.__prompt_queue.currently_running.clear()
            self.server.queue_updated()
            print(f"[Sentinel] EMERGENCY CLEAR: Removed {cleared_count} item(s) from currently_running")
            return cleared_count

    def get_stuck_items(self, user_identifier=None):
        """
        Get list of items that are stuck in currently_running.
        If user_identifier is provided, only return items for that user.
        Returns a list of dictionaries with item information.
        """
        stuck_items = []
        with self.__prompt_queue.mutex:
            for item_id, item in self.__prompt_queue.currently_running.items():
                # item structure: dict with "prompt", "user_id", "username" keys
                if isinstance(item, dict) and "prompt" in item:
                    prompt_id = item["prompt"][1] if len(item["prompt"]) > 1 else None
                    user_id = item.get("user_id")
                    username = item.get("username")
                    
                    # If user_identifier is provided, filter by user
                    if user_identifier:
                        if not (user_id == user_identifier or username == user_identifier):
                            continue
                    
                    stuck_items.append({
                        "item_id": item_id,
                        "prompt_id": prompt_id,
                        "user_id": user_id,
                        "username": username,
                    })
        return stuck_items

    def patched_execute_async(self, original_execute_async, access_control_instance):
        """Wrapper for PromptExecutor.execute_async to set user context before execution."""
        async def execute_async(executor_self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            # Look up user info from currently_running using prompt_id
            user_id = None
            username = None
            import threading
            
            try:
                for item_data in executor_self.server.prompt_queue.currently_running.values():
                    if isinstance(item_data, dict) and "prompt" in item_data:
                        item_prompt_id = item_data["prompt"][1] if len(item_data["prompt"]) > 1 else None
                        
                        if item_prompt_id == prompt_id:
                            # First try to get username directly from item_data (most reliable - stored by user_queue_put)
                            username = item_data.get("username")
                            user_id = item_data.get("user_id")
                            
                            # If username not stored, try to get from user_id (fallback for backward compatibility)
                            if not username and user_id:
                                # Get username from users_db (must use keyword argument)
                                _, user = access_control_instance.users_db.get_user(user_id=user_id)
                                if user and "username" in user:
                                    username = user["username"]
                            
                            if user_id and username:
                                break
                            else:
                                print(f"[Sentinel] WARNING execute_async: Found item but missing user_id or username: user_id={user_id}, username={username}")
            except Exception as e:
                print(f"[Sentinel] ERROR: Could not set user context: {e}")
                import traceback
                traceback.print_exc()
            
            # CRITICAL: Store user info in prompt_to_user mapping BEFORE calling original_execute_async
            # This ensures cache key generation (which happens early in execute_async) can access user context
            if user_id and username:
                with access_control_instance.__prompt_to_user_lock:
                    access_control_instance.__prompt_to_user[prompt_id] = {
                        "user_id": user_id,
                        "username": username
                    }
            else:
                print(f"[Sentinel] WARNING: Could not find user for prompt_id={prompt_id}, user_id={user_id}")
            
            # Set user context variables for this execution
            if user_id:
                access_control_instance.set_current_user_id(user_id)
            if username:
                access_control_instance.set_current_username(username)
            
            try:
                # Call the original execute_async
                # Cache key generation happens inside execute_async, so prompt_to_user must be populated by now
                result = await original_execute_async(executor_self, prompt, prompt_id, extra_data, execute_outputs)
                
                # DON'T clear context variables here - save_images might be called asynchronously
                # The prompt_to_user mapping will persist and be used by get_current_username()
                # Context variables will be cleared when the next prompt starts or when explicitly cleared
                
                return result
            except Exception as e:
                print(f"[Sentinel] ERROR execute_async: Exception during execution: {e}")
                import traceback
                traceback.print_exc()
                raise
            # Note: We intentionally DON'T clear context variables in finally block
            # because save_images and other operations might be called asynchronously
            # The prompt_to_user mapping persists and is cleaned up when task_done is called
        
        return execute_async
    
    def patched_prompt_worker(self, original_prompt_worker, access_control_instance):
        """Wrapper for prompt_worker to ensure task_done is always called, even on exceptions."""
        def prompt_worker(q, server_instance):
            import time
            import gc
            import traceback
            import execution
            import comfy.model_management
            from comfy.cli_args import args
            try:
                import hook_breaker_ac10a0
            except ImportError:
                hook_breaker_ac10a0 = None
            
            current_time: float = 0.0
            cache_type = execution.CacheType.CLASSIC
            if args.cache_lru > 0:
                cache_type = execution.CacheType.LRU
            elif args.cache_ram > 0:
                cache_type = execution.CacheType.RAM_PRESSURE
            elif args.cache_none:
                cache_type = execution.CacheType.NONE

            e = execution.PromptExecutor(server_instance, cache_type=cache_type, cache_args={ "lru" : args.cache_lru, "ram" : args.cache_ram } )
            last_gc_collect = 0
            need_gc = False
            gc_collect_interval = 10.0

            while True:
                timeout = 1000.0
                if need_gc:
                    timeout = max(gc_collect_interval - (current_time - last_gc_collect), 0.0)

                queue_item = q.get(timeout=timeout)
                if queue_item is not None:
                    item, item_id = queue_item
                    execution_start_time = time.perf_counter()
                    prompt_id = item[1]
                    server_instance.last_prompt_id = prompt_id

                    sensitive = item[5]
                    extra_data = item[3].copy()
                    for k in sensitive:
                        extra_data[k] = sensitive[k]
                    
                    # CRITICAL: Set user context BEFORE execution starts
                    # Extract user info from extra_data (set in user_queue_put)
                    user_id = extra_data.get("user_id")
                    username = extra_data.get("username")
                    
                    # Store in prompt_to_user mapping for get_current_username() to find
                    if user_id or username:
                        with access_control_instance.__prompt_to_user_lock:
                            access_control_instance.__prompt_to_user[prompt_id] = {
                                "user_id": user_id,
                                "username": username
                            }
                        print(f"[Sentinel] DEBUG: Set user context in worker thread - prompt_id={prompt_id}, user_id={user_id}, username={username}")

                    # Wrap execute in try-except-finally to ensure task_done is always called
                    # This prevents items from getting stuck in currently_running when exceptions occur
                    try:
                        e.execute(item[2], prompt_id, extra_data, item[4])
                        need_gc = True
                    except Exception as ex:
                        # Log the exception but ensure we still call task_done
                        print(f"[Sentinel] ERROR: Exception during prompt execution for prompt_id {prompt_id}: {ex}")
                        traceback.print_exc()
                        e.success = False
                        e.history_result = {"outputs": {}, "meta": {}}
                        e.status_messages = []
                        need_gc = True
                    finally:
                        # Always call task_done to remove item from currently_running
                        # This prevents the UI from showing infinite queue
                        remove_sensitive = lambda prompt: prompt[:5] + prompt[6:]
                        q.task_done(item_id,
                                    e.history_result,
                                    status=execution.PromptQueue.ExecutionStatus(
                                        status_str='success' if e.success else 'error',
                                        completed=e.success,
                                        messages=e.status_messages), process_item=remove_sensitive)
                        
                        # CRITICAL: Send executing message with node=None to clear executing state
                        # This must be sent AFTER task_done to ensure queue status is updated first
                        # Note: task_done already calls queue_updated(), but we send executing message
                        # to explicitly clear the executing state in the UI
                        if server_instance.client_id is not None:
                            server_instance.send_sync("executing", {"node": None, "prompt_id": prompt_id}, server_instance.client_id)
                        
                        # Debug logging to help diagnose stuck queue issues
                        remaining = q.get_tasks_remaining()
                        running_count = len(q.currently_running) if hasattr(q, 'currently_running') else 0
                        queue_count = len(q.queue) if hasattr(q, 'queue') else 0
                        print(f"[Sentinel] DEBUG: After task_done - queue_remaining={remaining}, running={running_count}, queued={queue_count}, prompt_id={prompt_id}, success={e.success}")

                    current_time = time.perf_counter()
                    execution_time = current_time - execution_start_time

                    # Log Time in a more readable way after 10 minutes
                    if execution_time > 600:
                        execution_time = time.strftime("%H:%M:%S", time.gmtime(execution_time))
                        logging.info(f"Prompt executed in {execution_time}")
                    else:
                        logging.info("Prompt executed in {:.2f} seconds".format(execution_time))

                flags = q.get_flags()
                free_memory = flags.get("free_memory", False)

                if flags.get("unload_models", free_memory):
                    comfy.model_management.unload_all_models()
                    need_gc = True
                    last_gc_collect = 0

                if free_memory:
                    e.reset()
                    need_gc = True
                    last_gc_collect = 0

                if need_gc:
                    current_time = time.perf_counter()
                    if (current_time - last_gc_collect) > gc_collect_interval:
                        gc.collect()
                        comfy.model_management.soft_empty_cache()
                        last_gc_collect = current_time
                        need_gc = False
                        if hook_breaker_ac10a0:
                            hook_breaker_ac10a0.restore_functions()
        
        return prompt_worker

    def user_queue_get_tasks_remaining(self):
        """Get number of tasks remaining, accounting for Sentinel's wrapped queue items."""
        with self.__prompt_queue.mutex:
            # Count items in queue (may be wrapped in ComparableQueueItem)
            queue_count = len(self.__prompt_queue.queue)
            # Count items in currently_running (may be dicts with "prompt" key)
            running_count = len(self.__prompt_queue.currently_running)
            
            # Auto-cleanup: Remove any items that have been running for more than 5 minutes
            # This prevents stuck items from blocking the queue indefinitely
            # Reduced from 1 hour to 5 minutes for faster recovery
            import time
            current_time = time.time()
            cleaned_count = 0
            for item_id, item in list(self.__prompt_queue.currently_running.items()):
                # Check if item has a timestamp (we'll add this in user_queue_get)
                if isinstance(item, dict) and "_start_time" in item:
                    elapsed = current_time - item["_start_time"]
                    if elapsed > 300:  # 5 minutes (reduced from 1 hour)
                        prompt_id = item.get('prompt', [None, 'unknown'])[1] if isinstance(item.get('prompt'), (list, tuple)) and len(item.get('prompt', [])) > 1 else 'unknown'
                        username = item.get('username') or item.get('user_id') or 'unknown'
                        print(f"[Sentinel] AUTO-CLEANUP: Removing stuck item (running {elapsed:.1f}s >5min): item_id={item_id}, prompt_id={prompt_id}, user={username}")
                        self.__prompt_queue.currently_running.pop(item_id, None)
                        cleaned_count += 1
                elif isinstance(item, dict):
                    # Item doesn't have timestamp - might be from before patch
                    # Check if it's been in queue too long by checking if it's been there since startup
                    # For now, just log it
                    prompt_id = item.get('prompt', [None, 'unknown'])[1] if isinstance(item.get('prompt'), (list, tuple)) and len(item.get('prompt', [])) > 1 else 'unknown'
                    print(f"[Sentinel] WARNING: Item in queue without timestamp: item_id={item_id}, prompt_id={prompt_id}")
                    # Add timestamp now to track it
                    item["_start_time"] = current_time
            
            if cleaned_count > 0:
                print(f"[Sentinel] AUTO-CLEANUP: Removed {cleaned_count} stuck item(s) from queue")
                self.server.queue_updated()
                running_count -= cleaned_count
            
            return queue_count + running_count

    def patch_prompt_queue(self):
        self.__prompt_queue.put = self.user_queue_put
        self.__prompt_queue.get = self.user_queue_get
        self.__prompt_queue.task_done = self.user_queue_task_done
        self.__prompt_queue.get_current_queue = self.user_queue_get_current_queue
        self.__prompt_queue.get_current_queue_volatile = self.user_queue_get_current_queue_volatile
        self.__prompt_queue.get_tasks_remaining = self.user_queue_get_tasks_remaining
        
        self.__prompt_queue.wipe_queue = self.user_queue_wipe_queue
        self.__prompt_queue.delete_queue_item = self.user_queue_delete_queue_item
        self.__prompt_queue.get_history = self.user_queue_get_history
        self.__prompt_queue.wipe_history = self.user_queue_wipe_history
        
        # Patch prompt_worker to ensure task_done is always called
        # NOTE: We defer this patching to avoid circular import issues during Sentinel initialization
        # The patching will happen lazily in user_queue_get when the first item is retrieved
        # This ensures main.py is fully loaded before we try to patch
        self._prompt_worker_patched = False
        self._prompt_worker_patch_lock = threading.Lock()
        print("[Sentinel] DEBUG: prompt_worker patching deferred to avoid circular import (will patch on first queue access)")
        
        # Patch PromptExecutor to set user context before execution
        try:
            import execution
            if hasattr(execution, 'PromptExecutor'):
                # Store original method
                if not hasattr(execution.PromptExecutor, '_original_execute_async'):
                    execution.PromptExecutor._original_execute_async = execution.PromptExecutor.execute_async
                # Patch with user context setting (pass self as access_control_instance)
                execution.PromptExecutor.execute_async = self.patched_execute_async(
                    execution.PromptExecutor._original_execute_async, 
                    self
                )
                
                # CRITICAL: Patch the execute function to update SaveImage output_dir when nodes are retrieved from cache
                # This ensures cached node instances get the correct directory even for old workflows
                if not hasattr(execution, '_original_execute'):
                    execution._original_execute = execution.execute
                
                async def patched_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id, execution_list, pending_subgraph_results, pending_async_nodes, ui_outputs):
                    # CRITICAL: Update output_dir BEFORE execution for SaveImage nodes retrieved from cache
                    # This ensures cached node instances get the correct directory even for old workflows
                    unique_id = current_item
                    class_type = dynprompt.get_node(unique_id)['class_type']
                    
                    # If this is a SaveImage or PreviewImage node, update output_dir BEFORE execution
                    # This is critical because the node instance might be cached from a previous user
                    if class_type in ('SaveImage', 'PreviewImage'):
                        obj = caches.objects.get(unique_id)
                        if obj is not None:
                            # Update output_dir to current user's directory BEFORE execution
                            if class_type == 'SaveImage':
                                old_dir = getattr(obj, 'output_dir', None)
                                obj.output_dir = folder_paths.get_output_directory()
                                if old_dir != obj.output_dir:
                                    username = self.get_current_username() or "unknown"
                                    print(f"[Sentinel] DEBUG: Updated cached SaveImage output_dir from '{old_dir}' to '{obj.output_dir}' for user={username}")
                            elif class_type == 'PreviewImage':
                                obj.output_dir = folder_paths.get_temp_directory()
                    
                    # Call original execute function
                    return await execution._original_execute(server, dynprompt, caches, current_item, extra_data, executed, prompt_id, execution_list, pending_subgraph_results, pending_async_nodes, ui_outputs)
                
                execution.execute = patched_execute
        except Exception as e:
            print(f"[Sentinel] WARNING: Could not patch PromptExecutor: {e}")
            import traceback
            traceback.print_exc()
        
        # Patch cache key generation to include user context
        self._patch_cache_for_user_isolation()
    
    def _patch_cache_for_user_isolation(self):
        """Patch ComfyUI's cache to include user context in cache keys."""
        try:
            import comfy_execution.caching as caching
            import execution
            access_control_instance = self
            
            # Patch IsChangedCache to store prompt_id for cache key generation
            if hasattr(execution, 'IsChangedCache'):
                if not hasattr(execution.IsChangedCache, '_original_init'):
                    execution.IsChangedCache._original_init = execution.IsChangedCache.__init__
                
                def patched_is_changed_cache_init(self, prompt_id, dynprompt, outputs_cache):
                    execution.IsChangedCache._original_init(self, prompt_id, dynprompt, outputs_cache)
                    # Store prompt_id for later use in cache key generation
                    self._sentinel_prompt_id = prompt_id
                
                execution.IsChangedCache.__init__ = patched_is_changed_cache_init
            
            # Patch CacheKeySetInputSignature to include user context
            if hasattr(caching, 'CacheKeySetInputSignature'):
                # Store original method
                if not hasattr(caching.CacheKeySetInputSignature, '_original_get_node_signature'):
                    caching.CacheKeySetInputSignature._original_get_node_signature = caching.CacheKeySetInputSignature.get_node_signature
                
                async def patched_get_node_signature(self, dynprompt, node_id):
                    # Get user context from prompt_to_user mapping using prompt_id
                    username = None
                    prompt_id = None
                    try:
                        # Try to get prompt_id from is_changed_cache
                        prompt_id = getattr(self.is_changed_cache, '_sentinel_prompt_id', None)
                        if not prompt_id:
                            # Fallback: try to get from prompt_id attribute
                            prompt_id = getattr(self.is_changed_cache, 'prompt_id', None)
                        
                        if prompt_id:
                            # First try prompt_to_user mapping (fastest)
                            with access_control_instance.__prompt_to_user_lock:
                                user_info = access_control_instance.__prompt_to_user.get(prompt_id)
                                if user_info and "username" in user_info:
                                    username = user_info["username"]
                            
                            # If not in mapping yet, check currently_running (fallback)
                            if not username:
                                for item_data in access_control_instance.__prompt_queue.currently_running.values():
                                    if isinstance(item_data, dict) and "prompt" in item_data:
                                        item_prompt_id = item_data["prompt"][1] if len(item_data["prompt"]) > 1 else None
                                        if item_prompt_id == prompt_id:
                                            username = item_data.get("username")
                                            if username:
                                                # Cache it in prompt_to_user for future lookups
                                                with access_control_instance.__prompt_to_user_lock:
                                                    access_control_instance.__prompt_to_user[prompt_id] = {
                                                        "user_id": item_data.get("user_id"),
                                                        "username": username
                                                    }
                                                break
                    except Exception as e:
                        print(f"[Sentinel] WARNING cache: Error getting username: {e}")
                    
                    # Get original signature
                    signature = await caching.CacheKeySetInputSignature._original_get_node_signature(self, dynprompt, node_id)
                    
                    # Add user context to signature to make cache user-specific
                    if username:
                        # Convert signature to list if it's a tuple, add username, then convert back
                        if isinstance(signature, tuple):
                            signature = list(signature)
                        elif not isinstance(signature, list):
                            signature = [signature]
                        signature.append(("__sentinel_user__", username))
                        signature = tuple(signature) if isinstance(signature, list) else signature
                        pass  # User context added to cache key
                    else:
                        print(f"[Sentinel] WARNING cache: No username found for cache key generation (prompt_id={prompt_id}, node_id={node_id})")
                    
                    return signature
                
                caching.CacheKeySetInputSignature.get_node_signature = patched_get_node_signature
                print("[Sentinel] Patched cache key generation to include user context")
        except Exception as e:
            print(f"[Sentinel] WARNING: Could not patch cache for user isolation: {e}")
            import traceback
            traceback.print_exc()

    def create_user_context_middleware(self) -> web.middleware:
        """Middleware to ensure user context is set before /prompt endpoint runs."""
        @web.middleware
        async def user_context_middleware(request: web.Request, handler) -> web.Response:
            # Only process /prompt endpoint
            if request.path == "/prompt" and request.method == "POST":
                import threading
                
                # Extract user info from request (set by JWT middleware)
                user_id = request.get("user_id")
                username = request.get("user")  # JWT middleware stores username as "user"
                
                # Set context variables before calling handler (which will call user_queue_put)
                if user_id:
                    self.set_current_user_id(user_id)
                else:
                    print(f"[Sentinel] WARNING MIDDLEWARE: No user_id in request!")
                    
                if username:
                    self.set_current_username(username)
                else:
                    print(f"[Sentinel] WARNING MIDDLEWARE: No username in request!")
            
            return await handler(request)
        
        return user_context_middleware

    def create_workflow_response_middleware(self) -> web.middleware:
        """Middleware to map __userdata/workflows back to workflows and merge global workflows."""
        @web.middleware
        async def workflow_response_middleware(request: web.Request, handler) -> web.Response:
            # Intercept both /api/userdata and /userdata endpoints for workflows
            is_userdata_endpoint = request.path in ["/api/userdata", "/userdata"]
            
            if is_userdata_endpoint:
                # Check if this is a workflow listing request
                is_workflow_listing = False
                dir_query = ""
                if hasattr(request.rel_url, 'query') and 'dir' in request.rel_url.query:
                    dir_query = request.rel_url.query['dir']
                    # Check for workflows listing
                    # Also handle empty dir_query (root listing) which should show Shared folder
                    is_workflow_listing = (dir_query == 'workflows' or dir_query.startswith('workflows/') or dir_query == '')
                
                response = await handler(request)
                
                # Process workflow listing responses
                # Also process root listing (empty dir_query) to show Shared folder
                should_process = (is_workflow_listing or dir_query == '') and response.status == 200 and response.content_type == "application/json"
                if should_process:
                    print(f"[Sentinel] DEBUG: Processing /api/userdata workflow listing, dir='{dir_query}'")
                    try:
                        # Read the response body - aiohttp web.Response objects
                        # For web.json_response(), the JSON data is stored in _body before serialization
                        import json
                        body_data = None
                        
                        # Try different ways to access the body
                        if hasattr(response, '_body') and response._body:
                            body_data = response._body
                        elif hasattr(response, 'body') and response.body:
                            body_data = response.body
                        elif hasattr(response, '_payload'):
                            body_data = response._payload
                        
                        if body_data:
                            if isinstance(body_data, bytes):
                                data = json.loads(body_data.decode('utf-8'))
                            elif isinstance(body_data, str):
                                data = json.loads(body_data)
                            else:
                                # Already a dict/list (for json_response, _body contains the Python object)
                                data = body_data
                        else:
                            # Body not available - this shouldn't happen for json_response
                            print(f"[Sentinel] DEBUG: Response type: {type(response)}, has body: {hasattr(response, 'body')}, has _body: {hasattr(response, '_body')}")
                            raise ValueError("Could not access response body")
                        
                        # /api/userdata returns either:
                        # - List of strings (file paths) when full_info=false
                        # - List of FileInfo dicts when full_info=true
                        # - List of lists when split=true
                        
                        if isinstance(data, list):
                            # Get query parameters to match the response format
                            full_info = request.rel_url.query.get('full_info', '').lower() == 'true'
                            recurse = request.rel_url.query.get('recurse', '').lower() == 'true'
                            
                            # Map paths in the response
                            for i, item in enumerate(data):
                                if isinstance(item, dict) and "path" in item:
                                    # Map __userdata/workflows back to workflows
                                    if item["path"].startswith("__userdata/workflows"):
                                        item["path"] = item["path"].replace("__userdata/workflows", "workflows", 1)
                                    elif "/__userdata/workflows/" in item["path"]:
                                        item["path"] = item["path"].replace("/__userdata/workflows/", "/workflows/")
                                    elif item["path"].endswith("/__userdata/workflows"):
                                        item["path"] = item["path"].replace("/__userdata/workflows", "/workflows")
                                    # Also handle old __workflows format for backward compatibility
                                    elif item["path"].startswith("__workflows"):
                                        item["path"] = item["path"].replace("__workflows", "workflows", 1)
                                    elif "/__workflows/" in item["path"]:
                                        item["path"] = item["path"].replace("/__workflows/", "/workflows/")
                                    elif item["path"].endswith("/__workflows"):
                                        item["path"] = item["path"].replace("/__workflows", "/workflows")
                                elif isinstance(item, str):
                                    # Simple string path format - update in place
                                    if item.startswith("__userdata/workflows"):
                                        data[i] = item.replace("__userdata/workflows", "workflows", 1)
                                    elif "/__userdata/workflows/" in item:
                                        data[i] = item.replace("/__userdata/workflows/", "/workflows/")
                            
                            # If this is a root listing (empty dir_query), add "Shared" as a folder
                            if dir_query == '':
                                # Add Shared folder entry (shared workflows)
                                shared_workflows_dir = self.get_shared_workflows_directory()
                                if shared_workflows_dir and os.path.exists(shared_workflows_dir):
                                    if full_info:
                                        data.append({
                                            "name": "Shared",
                                            "path": "Shared",
                                            "type": "directory"
                                        })
                                    else:
                                        data.append("Shared")
                            
                            # Add shared workflows to the list (when listing workflows directory, not root)
                            if (dir_query == 'workflows' or dir_query.startswith('workflows/')) and dir_query != '':
                                print(f"[Sentinel] DEBUG: Adding shared workflows to listing")
                                shared_workflows_dir = self.get_shared_workflows_directory()
                                print(f"[Sentinel] DEBUG: Shared workflows directory: {shared_workflows_dir}")
                                if shared_workflows_dir:
                                    if not os.path.exists(shared_workflows_dir):
                                        print(f"[Sentinel] DEBUG: Shared workflows directory does not exist: {shared_workflows_dir}")
                                    else:
                                        try:
                                            import glob
                                            # Scan shared workflows directory (only JSON files)
                                            workflow_extensions = {'.json', '.workflow'}
                                            files_found = 0
                                            
                                            if recurse:
                                                pattern = os.path.join(glob.escape(shared_workflows_dir), '**', '*')
                                                for file_path in glob.glob(pattern, recursive=True):
                                                    if os.path.isfile(file_path):
                                                        file_name = os.path.basename(file_path)
                                                        if not file_name.startswith("."):
                                                            file_ext = os.path.splitext(file_name)[1].lower()
                                                            if file_ext in workflow_extensions:
                                                                rel_path = os.path.relpath(file_path, shared_workflows_dir).replace(os.sep, '/')
                                                                
                                                                if full_info:
                                                                    # Use get_file_info format
                                                                    from app.user_manager import get_file_info
                                                                    file_info = get_file_info(file_path, shared_workflows_dir)
                                                                    file_info['path'] = f"Shared/{rel_path}"
                                                                    data.append(file_info)
                                                                else:
                                                                    # Simple string format
                                                                    data.append(f"Shared/{rel_path}")
                                                                
                                                                files_found += 1
                                            else:
                                                # Non-recursive: only root directory
                                                for file_name in os.listdir(shared_workflows_dir):
                                                    if not file_name.startswith("."):
                                                        file_path = os.path.join(shared_workflows_dir, file_name)
                                                        if os.path.isfile(file_path):
                                                            file_ext = os.path.splitext(file_name)[1].lower()
                                                            if file_ext in workflow_extensions:
                                                                if full_info:
                                                                    from app.user_manager import get_file_info
                                                                    file_info = get_file_info(file_path, shared_workflows_dir)
                                                                    file_info['path'] = f"Shared/{file_name}"
                                                                    data.append(file_info)
                                                                else:
                                                                    data.append(f"Shared/{file_name}")
                                                                
                                                                files_found += 1
                                            
                                            if files_found > 0:
                                                print(f"[Sentinel] DEBUG: Added {files_found} shared workflow(s) to listing")
                                            else:
                                                print(f"[Sentinel] DEBUG: No workflow files found in shared directory: {shared_workflows_dir}")
                                        except Exception as e:
                                            import traceback
                                            print(f"[Sentinel] WARNING: Could not scan shared workflows: {e}")
                                            traceback.print_exc()
                                else:
                                    print(f"[Sentinel] DEBUG: Shared workflows directory not configured")
                            
                            # Re-sort after adding global workflows
                            # Only sort if data contains dicts (full_info mode)
                            if len(data) > 0 and isinstance(data[0], dict):
                                data.sort(key=lambda x: (x.get('type', 'file') != 'directory', x.get('name', '').lower()))
                            elif len(data) > 0 and isinstance(data[0], str):
                                data.sort()
                        
                        # Create new response with mapped data
                        from aiohttp import web
                        # Create a new response with the modified data
                        new_response = web.json_response(data)
                        return new_response
                    except Exception as e:
                        # If mapping fails, return original response
                        print(f"[Sentinel] WARNING: Could not map workflow paths in response: {e}")
                        return response
                
                return response
            
            return await handler(request)
        
        return workflow_response_middleware
    
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