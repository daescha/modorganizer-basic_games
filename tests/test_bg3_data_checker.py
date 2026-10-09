import importlib
import sys
import types
import unittest
from enum import IntEnum
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

mobase: Any = sys.modules.setdefault("mobase", MagicMock())


class _ModDataChecker:
    class CheckReturn(IntEnum):
        INVALID = 0
        FIXABLE = 1
        VALID = 2

    INVALID = CheckReturn.INVALID
    FIXABLE = CheckReturn.FIXABLE
    VALID = CheckReturn.VALID


def _detach(entry: Any) -> None:
    entry._up._children.remove(entry)
    entry._up = None


def _adopt(parent: Any, entry: Any) -> None:
    entry._up = parent
    if isinstance(entry, MagicMock):
        entry.parent.side_effect = lambda: entry._up
        entry.detach.side_effect = lambda: _detach(entry)


class _IFileTree:
    def __init__(self, name: str, children: list[Any]) -> None:
        self._name, self._children = name, children
        self._up: Any = None
        self.pathFrom = MagicMock(return_value=name)
        for c in children:
            c.pathFrom.return_value = f"{name}/{c.name()}"
            _adopt(self, c)

    def parent(self) -> Any:
        return self._up

    def detach(self) -> None:
        _detach(self)

    def merge(self, other: Any) -> None:
        for c in list(other):
            self.move(c, "")

    def insert(self, entry: Any) -> None:
        self.move(entry, "")

    def move(self, entry: Any, path: str) -> None:
        if entry._up is not None:
            _detach(entry)
        target: Any = self
        for part in filter(None, path.split("/")):
            found = next((c for c in target._children if c.name() == part), None)
            if found is None:
                found = _IFileTree(part, [])
                target._children.append(found)
                found._up = target
            target = found
        target._children.append(entry)
        _adopt(target, entry)

    def name(self) -> str:
        return self._name

    def isDir(self) -> bool:
        return True

    def __iter__(self):
        return iter(self._children)


mobase.ModDataChecker = _ModDataChecker
mobase.IFileTree = _IFileTree

# The repo root is a package, so the module's relative imports need a parent package name.
_pkg = types.ModuleType("basic_games")
_pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("basic_games", _pkg)

BG3ModDataChecker = importlib.import_module(
    "basic_games.games.baldursgate3.bg3_data_checker"
).BG3ModDataChecker


def _file(name: str) -> MagicMock:
    entry = MagicMock()
    entry.name.return_value = name
    entry.isDir.return_value = False
    return entry


class DataLooksValidTest(unittest.TestCase):
    def test_fixable_entry_survives_later_valid_directory(self):
        root = [_file("info.json"), _IFileTree("Extra", [_file("x.pak")])]
        self.assertEqual(
            BG3ModDataChecker().dataLooksValid(root),  # type: ignore[arg-type]
            _ModDataChecker.FIXABLE,
        )

    def test_mods_subfolder_paks_fixable(self):
        tree = _IFileTree(
            "", [_IFileTree("Mods", [_IFileTree("Sub", [_file("x.pak")])])]
        )
        checker = BG3ModDataChecker()
        self.assertEqual(checker.dataLooksValid(tree), _ModDataChecker.FIXABLE)  # type: ignore[arg-type]
        checker.fix(tree)  # type: ignore[arg-type]
        mods = next(e for e in tree if e.name() == "Mods")
        self.assertEqual([e.name() for e in mods], ["x.pak"])

    def test_mods_subfolder_pak_name_clash_invalid(self):
        tree = _IFileTree(
            "",
            [_IFileTree("Mods", [_file("x.pak"), _IFileTree("Sub", [_file("X.pak")])])],
        )
        self.assertEqual(
            BG3ModDataChecker().dataLooksValid(tree),  # type: ignore[arg-type]
            _ModDataChecker.INVALID,
        )


_mod = importlib.import_module("basic_games.games.baldursgate3.bg3_data_checker")
_utils = importlib.import_module("basic_games.games.baldursgate3.bg3_utils")


def _variant_tree() -> Any:
    return _IFileTree(
        "",
        [
            _IFileTree("Option A", [_file("a.pak")]),
            _IFileTree("Option B", [_file("b.pak"), _file("readme.md")]),
        ],
    )


def _pick_b(names: list[str]) -> str | None:
    return "Option B"


def _cancel(names: list[str]) -> str | None:
    return None


class PakVariantsTest(unittest.TestCase):
    def test_folders_with_one_pak_each_are_variants(self):
        self.assertEqual(
            sorted(_mod.pak_variants(_variant_tree())), ["Option A", "Option B"]
        )

    def test_single_wrapper_folder_is_skipped(self):
        tree = _IFileTree("", [_IFileTree("Wrap", list(_variant_tree()))])
        self.assertEqual(sorted(_mod.pak_variants(tree)), ["Option A", "Option B"])

    def test_folder_with_two_paks_is_no_variant_set(self):
        tree = _IFileTree(
            "",
            [
                _IFileTree("A", [_file("a.pak"), _file("c.pak")]),
                _IFileTree("B", [_file("b.pak")]),
            ],
        )
        self.assertEqual(_mod.pak_variants(tree), {})

    def test_root_pak_is_no_variant_set(self):
        tree = _variant_tree()
        tree._children.append(_file("main.pak"))
        self.assertEqual(_mod.pak_variants(tree), {})

    def test_known_folders_are_no_variants(self):
        tree = _IFileTree(
            "",
            [_IFileTree("Mods", [_file("a.pak")]), _IFileTree("B", [_file("b.pak")])],
        )
        self.assertEqual(_mod.pak_variants(tree), {})

    def test_fix_keeps_chosen_variant(self):
        tree = _variant_tree()
        checker = BG3ModDataChecker(choose_variant=_pick_b)
        self.assertEqual(checker.dataLooksValid(tree), _ModDataChecker.FIXABLE)
        checker.fix(tree)
        self.assertEqual(sorted(e.name() for e in tree), ["b.pak", "readme.md"])

    def test_fix_cancel_keeps_all(self):
        tree = _variant_tree()
        BG3ModDataChecker(choose_variant=_cancel).fix(tree)
        self.assertEqual(sorted(e.name() for e in tree), ["Option A", "Option B"])


def _lsx(*folders: str) -> str:
    nodes = "".join(_utils.get_node_string(folder=f) for f in folders)
    return f'<save><node id="Mods"><children>{nodes}</children></node></save>'


class ModsettingsEmptiedTest(unittest.TestCase):
    def test_emptied(self):
        self.assertTrue(_mod.modsettings_emptied(_lsx("GustavX", "A"), _lsx("GustavX")))

    def test_kept(self):
        self.assertFalse(
            _mod.modsettings_emptied(_lsx("GustavX", "A"), _lsx("GustavX", "A"))
        )

    def test_backup_without_mods(self):
        self.assertFalse(_mod.modsettings_emptied(_lsx("GustavX"), _lsx()))

    def test_unparsable_current_counts_as_emptied(self):
        self.assertTrue(_mod.modsettings_emptied(_lsx("A"), ""))


if __name__ == "__main__":
    unittest.main()
