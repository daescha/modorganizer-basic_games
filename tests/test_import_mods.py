import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("mobase", MagicMock())
_pkg = "games.baldursgate3.plugins"
if _pkg not in sys.modules:
    sys.modules[_pkg] = types.ModuleType(_pkg)
    sys.modules[_pkg].__path__ = [
        str(Path(__file__).parents[1] / _pkg.replace(".", "/"))
    ]
    sys.modules[f"{_pkg}.bg3_tool_plugin"] = MagicMock(BG3ToolPlugin=object)
    sys.modules[f"{_pkg}.icons"] = MagicMock()

from games.baldursgate3.plugins.import_mods_plugin import (  # noqa: E402
    find_importable_paks,
    pak_mod_name,
)
from tests.test_lspk import build_pak  # noqa: E402

NAMED = (
    b'<save><region id="Config"><node id="ModuleInfo">'
    b'<attribute id="Name" type="LSString" value="Cool Mod"/></node></region></save>'
)


class ImportModsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def pak(self, name: str, meta: bytes | None) -> Path:
        files = [("Mods/X/meta.lsx", meta, 0)] if meta is not None else []
        return build_pak(self.tmp, files).rename(self.tmp / name)

    def test_name_from_meta(self):
        self.assertEqual(pak_mod_name(self.pak("a.pak", NAMED)), "Cool Mod")

    def test_name_falls_back_to_stem(self):
        self.assertEqual(pak_mod_name(self.pak("NoMeta.pak", None)), "NoMeta")
        self.assertEqual(pak_mod_name(self.pak("NoName.pak", b"<save/>")), "NoName")
        bad = self.tmp / "Bad.pak"
        bad.write_bytes(b"junk")
        self.assertEqual(pak_mod_name(bad), "Bad")

    def test_skips_paks_already_in_mo2(self):
        game, mo2 = self.tmp / "game", self.tmp / "mo2"
        (mo2 / "Existing" / "Mods").mkdir(parents=True)
        (mo2 / "Existing" / "Mods" / "Old.pak").touch()
        game.mkdir()
        for n in ("old.pak", "New.pak", "note.txt"):
            (game / n).touch()
        self.assertEqual(
            find_importable_paks(game, mo2), ([game / "New.pak"], [game / "old.pak"])
        )

    def test_missing_game_folder(self):
        self.assertEqual(find_importable_paks(self.tmp / "x", self.tmp), ([], []))


if __name__ == "__main__":
    unittest.main()
