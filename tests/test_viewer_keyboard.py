"""Aqua Tk keycodes from Tk's documented C layout, without injecting OS input."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from remotedesk.ui import viewer
from remotedesk.host.desktop import Desktop


def key(virtual, keysym, char="", modifiers=0):
    character = ord(char) if char else 0xf8fe
    return SimpleNamespace(keycode=(virtual << 24) | (modifiers << 22) | character,
                           keysym=keysym, char=char)


class IdleJobs:
    def __init__(self):
        self.jobs = {}
        self.next_id = 0

    def after_idle(self, callback, *args):
        self.next_id += 1
        self.jobs[self.next_id] = (callback, args)
        return self.next_id

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def flush(self):
        for job in list(self.jobs):
            callback, args = self.jobs.pop(job)
            callback(*args)


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch.object(viewer.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.view = viewer._Viewer.__new__(viewer._Viewer)
        self.view._closed = self.view._ended = False
        self.view._image_rect = (0, 0, 100, 100)
        self.view._input = viewer._InputBuffer()
        self.view._keys_down = {}
        self.view._key_release_jobs = {}
        self.view._buttons_down = set()
        self.view.root = IdleJobs()
        self.view._safe_status = Mock()

    def sent(self):
        return [(e["key"], e["down"]) for e in self.view._input.take()]

    def test_shift_tap_releases_even_though_its_modifier_bit_changes(self):
        for virtual, name in ((56, "Shift_L"), (60, "Shift_R")):
            with self.subTest(name=name):
                self.view._key_press(key(virtual, name, modifiers=1))
                self.view._key_release(key(virtual, name))
                expected = name.lower()
                self.assertEqual(self.sent(), [(expected, True), (expected, False)])
                self.assertEqual(self.view._keys_down, {})

    def test_shift_can_be_tapped_repeatedly(self):
        for _ in range(3):
            self.view._key_press(key(56, "Shift_L", modifiers=1))
            self.view._key_release(key(56, "Shift_L"))
        self.assertEqual(self.sent(), [("shift_l", True), ("shift_l", False)] * 3)

    def test_release_shift_before_letter_keeps_original_letter_release(self):
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._key_press(key(0, "A", "A", 1))
        self.view._key_release(key(56, "Shift_L"))
        self.view._key_release(key(0, "a", "a"))
        self.view.root.flush()
        self.assertEqual(self.sent(), [("shift_l", True), ("A", True),
                                     ("shift_l", False), ("A", False)])
        self.assertEqual(self.view._keys_down, {})

    def test_modifier_changes_while_letter_held_do_not_create_second_held_key(self):
        self.view._key_press(key(0, "a", "a"))
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._key_release(key(0, "A", "A", 1))
        self.view.root.flush()
        self.view._key_release(key(56, "Shift_L"))
        self.assertEqual(self.sent(), [("a", True), ("shift_l", True),
                                     ("a", False), ("shift_l", False)])

    def test_shift_tab_and_ctrl_shift_letter_use_named_or_virtual_shortcut_keys(self):
        self.view._key_press(key(59, "Control_L"))
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._key_press(key(8, "C", "\x03", 1))
        self.view._key_release(key(8, "C", "\x03", 1))
        self.view._key_press(key(48, "ISO_Left_Tab", "\t", 1))
        self.view._key_release(key(48, "ISO_Left_Tab", "\t", 1))
        self.view._key_release(key(56, "Shift_L"))
        self.view._key_release(key(59, "Control_L"))
        self.assertEqual(self.sent(), [("ctrl_l", True), ("shift_l", True),
                                     ("c", True), ("c", False), ("tab", True),
                                     ("tab", False), ("shift_l", False), ("ctrl_l", False)])

    def test_shifted_symbol_release_and_option_release_are_balanced(self):
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._key_press(key(18, "exclam", "!", 1))
        self.view._key_release(key(56, "Shift_L"))
        self.view._key_release(key(18, "1", "1"))
        self.view.root.flush()
        self.view._key_press(key(58, "Alt_L", modifiers=2))
        self.view._key_release(key(58, "Alt_L"))
        self.assertEqual(self.sent(), [("shift_l", True), ("!", True),
                                     ("shift_l", False), ("!", False),
                                     ("alt_l", True), ("alt_l", False)])

    def test_repeat_cancels_pending_release_with_changed_character(self):
        self.view._key_press(key(0, "a", "a"))
        self.view._key_release(key(0, "a", "a"))
        self.view._key_press(key(0, "A", "A", 1))
        self.view.root.flush()
        self.view._key_release(key(0, "A", "A", 1))
        self.view.root.flush()
        self.assertEqual(self.sent(), [("a", True), ("a", True), ("a", False)])

    def test_focus_loss_releases_all_and_ignores_late_key_up(self):
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._release_all()
        self.view._key_release(key(56, "Shift_L"))
        self.assertEqual(self.view._input.take(), [{"type": "release"}])
        self.assertFalse(self.view._keys_down)

    def test_shift_tap_reaches_host_as_balanced_press_release(self):
        shift = object()
        desktop = Desktop()
        desktop._key_names = {"shift_l": shift}
        desktop._keyboard = Mock()
        self.view._key_press(key(56, "Shift_L", modifiers=1))
        self.view._key_release(key(56, "Shift_L"))
        with patch.object(desktop, "_require_open"):
            desktop.apply(self.view._input.take())
        desktop._keyboard.press.assert_called_once_with(shift)
        desktop._keyboard.release.assert_called_once_with(shift)
        self.assertFalse(desktop._held_keys)

    def test_synthetic_text_does_not_collapse_into_one_physical_key(self):
        for virtual in (0x80, 0x81, 0xff):
            self.assertNotEqual(viewer._key_identity(key(virtual, "a", "a")),
                                viewer._key_identity(key(virtual, "b", "b")))

    def test_windows_and_linux_keep_native_keycodes(self):
        for platform in ("win32", "linux"):
            with patch.object(viewer.sys, "platform", platform):
                self.assertEqual(viewer._key_identity(SimpleNamespace(keycode=65)), 65)

    def test_windows_win_keys_map_to_command_and_shortcut_menu_sends_balanced_keys(self):
        with patch.object(viewer.sys, "platform", "win32"):
            self.assertEqual(viewer._key_name(SimpleNamespace(keysym="Win_L", char="")), "cmd_l")
            self.assertEqual(viewer._key_name(SimpleNamespace(keysym="Win_R", char="")), "cmd_r")
        self.view.canvas = Mock()
        self.view._send_shortcut(("ctrl_l", "left"))
        self.assertEqual(self.sent(), [
            ("ctrl_l", True), ("left", True), ("left", False), ("ctrl_l", False),
        ])
        self.view.canvas.focus_set.assert_called_once_with()

    def test_mac_keyboard_sets_flags_changed_arrow_flags_and_cycles_ime_on_caps_lock(self):
        from remotedesk.host.desktop import _MacKeyboard

        class FakeKeyCode:
            def __init__(self, vk):
                self.vk = vk

        class FakeKey:
            pass

        vks = {
            "cmd": 55, "cmd_l": 55, "cmd_r": 54,
            "ctrl": 59, "ctrl_l": 59, "ctrl_r": 62,
            "alt": 58, "alt_l": 58, "alt_r": 61, "alt_gr": 61,
            "shift": 56, "shift_l": 56, "shift_r": 60,
            "caps_lock": 57, "left": 123, "f5": 96, "tab": 48,
        }
        for name, vk in vks.items():
            item = FakeKey()
            item.value = FakeKeyCode(vk)
            setattr(FakeKey, name, item)

        posted = []

        class FakeQuartz:
            kCGHIDEventTap = 0
            kCGEventFlagsChanged = 12
            kCGEventFlagMaskCommand = 0x100000
            kCGEventFlagMaskControl = 0x40000
            kCGEventFlagMaskAlternate = 0x80000
            kCGEventFlagMaskShift = 0x20000
            kCGEventFlagMaskNumericPad = 0x200000
            kCGEventFlagMaskSecondaryFn = 0x800000

            @staticmethod
            def CGEventCreateKeyboardEvent(source, vk, down):
                return {"vk": vk, "down": down, "type": "key", "flags": 0, "unicode": None}

            @staticmethod
            def CGEventSetType(event, kind):
                event["type"] = kind

            @staticmethod
            def CGEventSetFlags(event, flags):
                event["flags"] = flags

            @staticmethod
            def CGEventKeyboardSetUnicodeString(event, length, text):
                event["unicode"] = text

            @staticmethod
            def CGEventPost(tap, event):
                posted.append(event)

        cycled = []
        kb = _MacKeyboard(
            SimpleNamespace(_mapping={"f": 3}),
            FakeKey,
            quartz=FakeQuartz,
            cycle_input_source=lambda: (cycled.append(True) or True),
        )
        # Ctrl + Left must emit FlagsChanged and arrow NumericPad|SecondaryFn flags.
        kb.press(FakeKey.ctrl_l)
        kb.press(FakeKey.left)
        kb.release(FakeKey.left)
        kb.release(FakeKey.ctrl_l)
        self.assertEqual(posted[0]["type"], FakeQuartz.kCGEventFlagsChanged)
        self.assertEqual(posted[0]["flags"], FakeQuartz.kCGEventFlagMaskControl)
        arrow_expected = (
            FakeQuartz.kCGEventFlagMaskControl
            | FakeQuartz.kCGEventFlagMaskNumericPad
            | FakeQuartz.kCGEventFlagMaskSecondaryFn
        )
        self.assertEqual(posted[1]["vk"], 123)
        self.assertEqual(posted[1]["flags"], arrow_expected)
        self.assertEqual(posted[2]["flags"], arrow_expected)
        self.assertEqual(posted[3]["type"], FakeQuartz.kCGEventFlagsChanged)
        self.assertEqual(posted[3]["flags"], 0)

        # Caps Lock cycles macOS input source once on press and does not post raw vk 57.
        before = len(posted)
        kb.press(FakeKey.caps_lock)
        kb.release(FakeKey.caps_lock)
        self.assertEqual(cycled, [True])
        self.assertEqual(len(posted), before)


if __name__ == "__main__":
    unittest.main()
