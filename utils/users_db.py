import os
import json
import hashlib
import bcrypt
from pathlib import Path
from typing import Optional, Tuple, Dict, List, Union

class UsersDB:
    def __init__(self, database: Union[str, Path]):
        self.database = str(database)
        self.users: Dict[str, dict] = {}
        self.admin_user: Tuple[Optional[str], dict] = (None, {})
        self._database_hash: Optional[str] = None
        self._db_format: Optional[str] = None
        self.load_users()

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt. (Conservée pour compat API, non utilisée en txt)"""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def _parse_userpass(s: str) -> Optional[tuple[str, str]]:
        """Parse 'username:password' and return (username, password) or None if invalid."""
        if ":" not in s:
            return None
        username, password = s.split(":", 1)
        username = username.strip()
        if not username:
            return None
        return username, password

    def calculate_file_hash(self) -> str:
        """Calculate the SHA256 hash of the database file."""
        if os.path.exists(self.database):
            with open(self.database, "rb") as f:
                file_data = f.read()
                return hashlib.sha256(file_data).hexdigest()
        return ""

    def _load_from_plaintext(self) -> dict:
        """
        Load users from a plaintext file that contains one 'username:password[:admin]' per line.
        Lines starting with '#' and empty lines are ignored.
        Passwords are kept IN CLEAR (no hashing).
        """
        users: Dict[str, dict] = {}
        if not os.path.exists(self.database):
            return users

        with open(self.database, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                username = parts[0].strip()
                username = parts[0].strip()
                if not username:
                    continue
                admin_flag = False
                if len(parts) >= 3:
                    tail = parts[-1].strip().lower()
                    if tail in ("admin", "true", "1", "yes", "y", "on"):
                         admin_flag = True
                         password = ":".join(parts[1:-1])
                    else:
                         password = ":".join(parts[1:])
                elif len(parts) == 2:
                     password = parts[1]
                else:
                     continue

                user_id = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
                users[user_id] = {
                    "username": username,
                    "password": password,
                }
                if admin_flag:
                    users[user_id]["admin"] = True

        self._db_format = "plaintext"
        return users
    
    def _write_plaintext(self, users: dict) -> None:
        lines: list[str] = []
        lines.append("# users file: 'username:password[:admin]'\n")
        for user in users.values():
            username = user.get("username", "")
            password = user.get("password", "")
            if not isinstance(password, str):
                password = str(password)
            is_admin = user.get("admin", False)
            if is_admin:
                line = f"{username}:{password}:admin"
            else:
                line = f"{username}:{password}"
            lines.append(line)

        with open(self.database, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    def load_users(self) -> dict:
        current_hash = self.calculate_file_hash()
        if current_hash == self._database_hash:
            return self.users

        if os.path.exists(self.database):
            try:
                with open(self.database, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.users = data
                        self._db_format = "json"
                    else:
                        self.users = self._load_from_plaintext()
            except json.JSONDecodeError:
                self.users = self._load_from_plaintext()
        else:
            self.users = {}
            self._db_format = None

        self._database_hash = current_hash
        return self.users

    def save_users(self, users: dict) -> None:
        if self._db_format is None:
            if str(self.database).lower().endswith(".txt"):
                self._db_format = "plaintext"
            else:
                self._db_format = "json"

        if self._db_format == "plaintext":
            self._write_plaintext(users)
        else:
            with open(self.database, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)

        self._database_hash = self.calculate_file_hash()

    def add_user_from_string(self, userpass: str, admin: bool = False) -> None:
        parsed = self._parse_userpass(userpass)
        if not parsed:
            raise ValueError("Invalid format. Use 'username:password'")
        username, password = parsed

        user_id = hashlib.sha256(username.encode()).hexdigest()[:8]
        self.load_users()

        user = {"username": username, "password": password}
        if admin:
            user["admin"] = True
        self.users[user_id] = user
        self.save_users(self.users)

    def get_user(self, username: str = "", user_id: str = "") -> tuple[Optional[str], dict]:
        self.load_users()

        if user_id:
            return user_id, self.users.get(user_id, {})

        if not username:
            return None, {}

        if ":" in username:
            parsed = self._parse_userpass(username)
            if not parsed:
                return None, {}
            uname, pwd = parsed
            for uid, user_data in self.users.items():
                if user_data.get("username") == uname:
                    stored = user_data.get("password", "")
                    if isinstance(stored, str):
                        if stored.startswith("$2a$") or stored.startswith("$2b$") or stored.startswith("$2y$"):
                            try:
                                if bcrypt.checkpw(pwd.encode("utf-8"), stored.encode("utf-8")):
                                    return uid, user_data
                                return None, {}
                            except ValueError:
                                return None, {}
                        if stored == pwd:
                            return uid, user_data
                    return None, {}
            return None, {}

        for uid, user_data in self.users.items():
            if user_data.get("username") == username:
                return uid, user_data
        return None, {}

    def check_username_password(self, username: str, password: str) -> bool:
        """Check credentials from a username and password (plaintext compare in txt mode)."""
        
        user_id, user_data = self.get_user(username=username) 
        
        if not user_data or "password" not in user_data:
            return False

        stored = user_data["password"]
        if not isinstance(stored, str):
            return False

        if stored.startswith("$2a$") or stored.startswith("$2b$") or stored.startswith("$2y$"):
            try:
                return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
            except ValueError:
                return False
        return stored == password

    def get_admin_user(self) -> Optional[tuple[Optional[str], dict]]:
        """Get the admin user from the database."""
        self.load_users()
        self.admin_user = (None, {})
        for uid, user_data in self.users.items():
            if user_data.get("admin"):
                self.admin_user = (uid, user_data)
                return self.admin_user
        return None