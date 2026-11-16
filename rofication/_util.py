import os
import secrets
import stat
from collections.abc import MutableSequence, Callable
from subprocess import CalledProcessError, check_output
from typing import Optional, Tuple


def _create_temporary_file(directory: str, name: str, mode: int) -> Tuple[int, str]:
    """Create a new, empty file with a name of its own next to the file it is going to replace."""
    for _ in range(100):
        path = os.path.join(directory, f'.{name}.{secrets.token_hex(4)}.tmp')
        try:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode), path
        except FileExistsError:
            continue
    raise FileExistsError(f'No free temporary file name for {os.path.join(directory, name)}')


def write_file_atomically(filename: str, data: bytes) -> None:
    """Replace the contents of a file in a single step: at any moment the file holds either its old
    contents or all of the new ones, also when writing fails or the process is killed half-way.

    A symbolic link is followed, and stays in place. The file keeps its permission bits; a new file
    gets the ones a new file normally gets. Raises OSError when the file can't be written, and then
    leaves it as it was, without leaving another file behind."""
    target = os.path.realpath(filename)
    directory, name = os.path.split(target)
    try:
        mode: Optional[int] = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    # the new contents of an existing file are only readable by the owner until it has its mode
    fd, temporary = _create_temporary_file(directory, name, 0o666 if mode is None else 0o600)
    try:
        with open(fd, 'wb') as f:
            f.write(data)
            f.flush()
            if mode is not None:
                # only after writing: a write clears the set-user-ID and set-group-ID bits
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class Event:
    def __init__(self) -> None:
        self._observers: MutableSequence[Callable] = []

    def __iadd__(self, observer: Callable) -> 'Event':
        self._observers.append(observer)
        return self

    def notify(self, *args, **kwargs) -> None:
        for observer in self._observers:
            observer(*args, **kwargs)


class Resource:
    def __init__(self, default: str, xres_name: str, env_name: Optional[str] = None):
        self.default: str = default
        self.xres_name: str = xres_name
        self.env_name: Optional[str] = env_name

    def fetch(self) -> str:
        env_val = None
        if self.env_name:
            env_val = os.getenv(self.env_name)

        # avoid calling a resource getter if the environment variable is set
        if env_val is not None:
            return env_val

        # Regolith reads resources with xrescat (X11) or trawlcat (Wayland);
        # either may be missing, so try both and fall back to the default.
        getters = ('xrescat', 'trawlcat')
        if os.getenv('XDG_SESSION_TYPE') == 'wayland':
            getters = getters[::-1]
        for getter in getters:
            try:
                return check_output((getter, self.xres_name, self.default), universal_newlines=True)
            except (OSError, CalledProcessError):
                continue
        return self.default
