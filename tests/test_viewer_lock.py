"""Viewer exclusivity uses real local file locks, with no desktop or networking."""

import errno
import hashlib
import importlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

guard = importlib.import_module("remotedesk.network.viewer_lock")

_CHILD = """
import importlib, sys
from pathlib import Path
guard = importlib.import_module('remotedesk.network.viewer_lock')
guard.settings_file = lambda: Path(sys.argv[1]) / 'login-peer.json'
try:
    with guard.viewer_lock(sys.argv[2]):
        print('LOCKED', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except RuntimeError as error:
    print(str(error), flush=True)
    sys.exit(2)
"""


class ViewerLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "设置 with spaces"
        self.directory.mkdir()
        self.settings = patch.object(guard, "settings_file", return_value=self.directory / "login-peer.json")
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def child_command(self, node_id, mode="once"):
        return [sys.executable, "-u", "-c", _CHILD, str(self.directory), node_id, mode]

    def child(self, node_id):
        return subprocess.run(self.child_command(node_id), capture_output=True, text=True,
                              encoding="utf-8", env=self.environment, timeout=5)

    def lock_file(self, node_id):
        return self.directory / "viewer-locks" / (hashlib.sha256(node_id.encode()).hexdigest() + ".lock")

    def test_same_node_is_exclusive_across_processes_then_releases(self):
        with guard.viewer_lock("stable-node"):
            contender = self.child("stable-node")
            self.assertEqual(contender.returncode, 2, contender.stderr)
            self.assertIn("查看器已在其他窗口中打开", contender.stdout)
            self.assertIn("切换到已有查看器", contender.stdout)
        successor = self.child("stable-node")
        self.assertEqual(successor.returncode, 0, successor.stderr)
        self.assertEqual(successor.stdout.strip(), "LOCKED")

    def test_different_stable_nodes_can_have_viewers_simultaneously(self):
        with guard.viewer_lock("node-A"):
            independent = self.child("node-B")
            self.assertEqual(independent.returncode, 0, independent.stderr)
            self.assertEqual(independent.stdout.strip(), "LOCKED")
            self.assertEqual(self.child("node-A").returncode, 2)

    def test_process_death_releases_lock_without_deleting_its_file(self):
        owner = subprocess.Popen(self.child_command("crashed-node", "hold"),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=self.environment)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "LOCKED")
            path = self.lock_file("crashed-node")
            inode = path.stat().st_ino
            self.assertEqual(self.child("crashed-node").returncode, 2)
            owner.kill()
            owner.communicate(timeout=5)
            with guard.viewer_lock("crashed-node"):
                self.assertEqual(path.stat().st_ino, inode)
            self.assertTrue(path.is_file())
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=5)

    def test_body_exception_releases_lock_and_remains_the_original_exception(self):
        with self.assertRaisesRegex(ValueError, "viewer failed"):
            with guard.viewer_lock("exception-node"):
                raise ValueError("viewer failed")
        self.assertEqual(self.child("exception-node").returncode, 0)

    def test_ids_are_hashed_and_persistent_files_contain_no_identity(self):
        node_id = "../../设备\\name: weird * ? %f\n\x00"
        with guard.viewer_lock(node_id):
            path = self.lock_file(node_id)
            self.assertRegex(path.name, r"^[0-9a-f]{64}\.lock$")
            self.assertEqual(path.read_bytes(), b"")
        with guard.viewer_lock(node_id):
            self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(list(path.parent.iterdir()), [path])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_identity_does_not_create_any_lock_directory(self):
        for node_id in (None, 42, "", "  \n"):
            with self.subTest(node_id=node_id), self.assertRaises(ValueError):
                with guard.viewer_lock(node_id):
                    self.fail("invalid identity acquired a lock")
        self.assertFalse((self.directory / "viewer-locks").exists())

    def test_windows_locks_and_unlocks_same_byte_of_empty_persistent_file(self):
        calls = []

        def locking(descriptor, mode, length):
            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
            self.assertEqual(os.fstat(descriptor).st_size, 0)
            calls.append((descriptor, mode, length))

        windows = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"msvcrt": windows}):
            with guard.viewer_lock("windows-node"):
                self.assertEqual(len(calls), 1)
            self.assertEqual([(mode, size) for _, mode, size in calls], [(2, 1), (0, 1)])
            self.assertEqual(calls[0][0], calls[1][0])
        with self.assertRaises(OSError):
            os.fstat(calls[0][0])

    def test_windows_contention_is_immediate_and_does_not_unlock_other_owner(self):
        locking = Mock(side_effect=OSError(errno.EACCES, "locked"))
        windows = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"msvcrt": windows}):
            with self.assertRaisesRegex(RuntimeError, "先关闭它再连接"):
                with guard.viewer_lock("windows-node"):
                    self.fail("contended lock acquired")
        self.assertEqual(locking.call_count, 1)
        self.assertEqual(locking.call_args.args[1:], (2, 1))

    def test_windows_unexpected_os_error_is_not_mislabeled_as_another_viewer(self):
        windows = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                       locking=Mock(side_effect=OSError(errno.EIO, "disk error")))
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"msvcrt": windows}):
            with self.assertRaisesRegex(OSError, "disk error"):
                with guard.viewer_lock("windows-node"):
                    self.fail("broken lock acquired")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW unavailable")
    def test_lock_file_symlink_is_not_followed_or_modified(self):
        path = self.lock_file("symlink-node")
        path.parent.mkdir(mode=0o700)
        target = self.directory / "unrelated-file"
        target.write_bytes(b"untouched")
        path.symlink_to(target)
        with self.assertRaises(OSError):
            with guard.viewer_lock("symlink-node"):
                self.fail("symlink lock acquired")
        self.assertEqual(target.read_bytes(), b"untouched")


if __name__ == "__main__":
    unittest.main()
