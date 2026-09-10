"""User-owned credentials, separate from repository identity and gateway state.

No network, enrollment, or credential output belongs in this module.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time


class CredentialError(RuntimeError):
    pass


CREDENTIAL_KEYS = {
    "base_url", "account_id", "token", "access_token", "refresh_token",
    "token_expires_at", "refresh_expires_at", "account_mode", "oauth_client_id",
    "oauth_scope", "logged_out", "credential_kind",
}
SELECTION_KEYS = {
    "project_id", "project_slug", "project_name", "project_encryption_state",
    "project_signing_key_fingerprint", "credential_profile",
}
_registry_lock = threading.Lock()
_thread_locks = {}
_held = threading.local()


def user_data_dir():
    if os.environ.get("NAPSEER_USER_DATA_DIR"):
        return Path(os.environ["NAPSEER_USER_DATA_DIR"]).expanduser().absolute()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "napseer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "napseer"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "napseer"


def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        raise CredentialError("Credential state cannot be opened safely.") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CredentialError("Credential state must be a regular file.")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CredentialError("Credential state exceeds the size limit.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, OSError):
        raise CredentialError("Credential state is unreadable; run nap auth login to repair it.") from None
    finally:
        if fd is not None:
            os.close(fd)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise CredentialError("Credential state must not be a symbolic link.")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def credential_lock(path, timeout=30):
    """Bounded process lock; nested calls in the same thread reuse the lock."""
    path = Path(path).absolute()
    with _registry_lock:
        thread_lock = _thread_locks.setdefault(str(path), threading.RLock())
    if not thread_lock.acquire(timeout=timeout):
        raise CredentialError("Credential update is busy; retry after the other login or refresh finishes.")
    try:
        held = getattr(_held, "paths", {})
        if str(path) in held:
            yield
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise CredentialError("Credential lock cannot be opened safely.") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CredentialError("Credential lock must be a regular file.")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise CredentialError("Credential update is busy; retry after the other login or refresh finishes.") from None
                    time.sleep(0.025)
            held[str(path)] = fd
            _held.paths = held
            try:
                yield
            finally:
                held.pop(str(path), None)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


class CredentialStore:
    def __init__(self, state_dir, legacy_path=None):
        self.state_dir = Path(state_dir)
        self.legacy = Path(legacy_path or self.state_dir / "auth.json")
        self.user = user_data_dir() / "credentials" / "default.json"
        project_key = hashlib.sha256(str(self.state_dir.parent.resolve()).encode()).hexdigest()
        self.project = user_data_dir() / "credentials" / "projects" / f"{project_key}.json"
        self.selection = self.state_dir / "state.json"

    def selected(self):
        bound = getattr(_held,"bindings",{}).get(str(self.state_dir.absolute()))
        if bound: return bound
        explicit = os.environ.get("NAPSEER_AUTH_FILE")
        if explicit:
            return Path(explicit).expanduser().absolute(), "explicit"
        if self.legacy != self.state_dir / "auth.json":
            return self.legacy, "explicit"
        if read_json(self.selection).get("credential_profile") == "user":
            return self.user, "user"
        if self.project.exists() or self.project.is_symlink():
            return self.project, "project"
        if self.legacy.exists() or self.legacy.is_symlink():
            return self.legacy, "legacy_project"
        return self.user, "user"

    @contextmanager
    def transaction(self):
        """Pin one credential source through read/refresh/write despite new overrides."""
        identity = str(self.state_dir.absolute())
        chosen = self.selected()
        with credential_lock(chosen[0]):
            bindings = getattr(_held,"bindings",{})
            previous = bindings.get(identity)
            bindings[identity] = chosen; _held.bindings = bindings
            try: yield
            finally:
                if previous: bindings[identity] = previous
                else: bindings.pop(identity,None)

    def use_user(self):
        current = read_json(self.user)
        if not current.get("token") or current.get("logged_out"):
            raise CredentialError("Run nap auth login before selecting the general user login.")
        with credential_lock(self.selection):
            selection = read_json(self.selection)
            selection["credential_profile"] = "user"
            atomic_json(self.selection, selection)
        return self.user

    def migrate(self):
        with credential_lock(self.legacy), credential_lock(self.project):
            legacy = read_json(self.legacy)
            if not legacy: raise CredentialError("There is no legacy project credential to migrate.")
            if self.project.exists(): raise CredentialError("A project credential already exists; migration will not overwrite it.")
            atomic_json(self.project, legacy)
            if read_json(self.project) != legacy: raise CredentialError("Migration verification failed; original credentials were preserved.")
            self.legacy.unlink()
            return self.project

    def read(self):
        path, source = self.selected()
        credentials = read_json(path)
        selection = {k: v for k, v in read_json(self.selection).items() if k in SELECTION_KEYS}
        # Project-bound grants retain their issued project. General grants only
        # consume repository selection; selection cannot confer authority.
        return {**selection, **credentials}

    def write(self, data):
        path, source = self.selected()
        with credential_lock(path):
            if source in {"legacy_project", "explicit"}:
                atomic_json(path, data)
                return
            credentials = {k: v for k, v in data.items() if k in CREDENTIAL_KEYS}
            if source == "project":
                credentials.update({k: v for k, v in data.items() if k in SELECTION_KEYS})
            current = read_json(path)
            # Reading project metadata must not churn or overwrite shared
            # credentials, including after another process logged out.
            if credentials != current:
                atomic_json(path, credentials)
            selection = {**read_json(self.selection), **{k: v for k, v in data.items() if k in SELECTION_KEYS}}
            if selection != read_json(self.selection):
                atomic_json(self.selection, selection)

    def login(self, credentials, project=False):
        target = self.project if project else self.user
        with credential_lock(target):
            atomic_json(target, credentials)
        if project:
            with credential_lock(self.selection):
                selection = read_json(self.selection); selection["credential_profile"] = "project"
                atomic_json(self.selection, selection)
        return target

    def logout(self, project=False):
        target = self.selected()[0] if project else self.user
        with credential_lock(target):
            previous = read_json(target)
            # Keep a tombstone: removing an override would silently restore the
            # more privileged user session on the next request.
            atomic_json(target, {"base_url": previous.get("base_url"), "logged_out": True})
        return target
