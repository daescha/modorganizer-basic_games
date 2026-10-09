import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree

sys.modules.setdefault("mobase", MagicMock())

from games.baldursgate3 import bg3_utils  # noqa: E402


class NodeStringTest(unittest.TestCase):
    def test_special_characters_round_trip(self):
        name = """Tom & Jerry's <"Best"> Mod"""
        node = bg3_utils.get_node_string(folder=name, name=name, uuid="u")
        attrs = {
            a.get("id"): a.get("value")
            for a in ElementTree.fromstring(node).iter("attribute")
        }
        self.assertEqual((attrs["Folder"], attrs["Name"]), (name, name))


class ProfilePathTest(unittest.TestCase):
    def test_modsettings_path_follows_profile_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            organizer = MagicMock()
            utils = bg3_utils.BG3Utils("Baldur's Gate 3 Plugin")
            utils.init(organizer)
            for profile in ("a", "b"):
                organizer.profilePath.return_value = str(Path(tmp, profile))
                self.assertEqual(
                    utils.modsettings_path, Path(tmp, profile, "modsettings.lsx")
                )


class WaitForFuturesTest(unittest.TestCase):
    def test_all_done_reports_progress(self):
        with ThreadPoolExecutor() as pool:
            futures = [pool.submit(time.sleep, 0.05) for _ in range(3)]
            seen: list[int] = []
            self.assertTrue(
                bg3_utils.wait_for_futures(futures, lambda: False, seen.append)
            )
        self.assertEqual(seen[-1], 3)

    def test_cancel_aborts(self):
        event = threading.Event()
        with ThreadPoolExecutor() as pool:
            futures = [pool.submit(event.wait)]
            self.assertFalse(
                bg3_utils.wait_for_futures(futures, lambda: True, lambda _: None)
            )
            event.set()

    def test_stall_aborts(self):
        event = threading.Event()
        with ThreadPoolExecutor() as pool:
            futures = [pool.submit(event.wait)]
            self.assertFalse(
                bg3_utils.wait_for_futures(
                    futures, lambda: False, lambda _: None, stall_timeout=0.3
                )
            )
            event.set()


class DependencyOrderTest(unittest.TestCase):
    def test_warns_on_missing_and_late_dependencies(self):
        order: list[tuple[str, str, list[str]]] = [
            ("a", "A", ["b", "c", "x"]),
            ("b", "B", []),
            ("d", "D", ["a"]),
        ]
        self.assertEqual(
            bg3_utils.dependency_order_warnings(order, {"a", "b", "c", "d"}),
            [
                "A depends on b, which loads after it",
                "A depends on c, which is not in the load order",
            ],
        )

    def test_correct_order_has_no_warnings(self):
        order: list[tuple[str, str, list[str]]] = [("b", "B", []), ("a", "A", ["b"])]
        self.assertEqual(bg3_utils.dependency_order_warnings(order, {"a", "b"}), [])
