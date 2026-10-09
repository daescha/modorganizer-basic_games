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
    entry.parent._children.remove(entry)
    entry.parent = None


class _IFileTree:
    def __init__(self, name: str, children: list[Any]) -> None:
        self._name, self._children = name, children
        self.parent: Any = None
        self.pathFrom = MagicMock(return_value=name)
        for c in children:
            c.pathFrom.return_value = f"{name}/{c.name()}"
            c.parent = self
            if isinstance(c, MagicMock):
                c.detach.side_effect = lambda c=c: _detach(c)

    def detach(self) -> None:
        self.parent._children.remove(self)
        self.parent = None

    def insert(self, entry: Any) -> None:
        self.move(entry, "")

    def move(self, entry: Any, path: str) -> None:
        if entry.parent is not None:
            entry.detach()
        target: Any = self
        for part in filter(None, path.split("/")):
            found = next((c for c in target._children if c.name() == part), None)
            if found is None:
                found = _IFileTree(part, [])
                target._children.append(found)
                found.parent = target
            target = found
        target._children.append(entry)
        entry.parent = target

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


if __name__ == "__main__":
    unittest.main()
