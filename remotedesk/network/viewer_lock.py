"""One viewer process per user and stable Tailscale node, without socket ports."""

from contextlib import contextmanager
import errno
import hashlib
import os
import sys

from remotedesk.login.preferences import settings_file


@contextmanager
def viewer_lock(node_id):
    """Hold an OS lock until the viewer closes; process death also releases it."""
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("设备身份无效，请刷新设备列表后重试。")
    name = hashlib.sha256(node_id.encode("utf-8")).hexdigest() + ".lock"
    directory = settings_file().parent / "viewer-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("查看器锁目录无效，请检查本机应用配置目录。")
    if os.name != "nt":
        directory.chmod(0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / name, flags, 0o600)
    # Never unlink the file: another process may already be waiting on its inode.
    # No contents are needed, even for a Windows byte lock past the empty EOF.
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        if sys.platform == "win32":
            import msvcrt

            def lock(acquire):
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock(acquire):
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)
        try:
            lock(True)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise RuntimeError(
                "这台设备的查看器已在其他窗口中打开，请切换到已有查看器，或先关闭它再连接。"
            ) from error
        try:
            yield
        finally:
            lock(False)
