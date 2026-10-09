import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("mobase", MagicMock())

from games.baldursgate3 import pak_parser  # noqa: E402
from tests.test_lspk import build_pak  # noqa: E402

META = b"""<save><region id="Config"><node id="ModuleInfo">
<attribute id="Folder" value="Foo"/><attribute id="Name" value="Foo Mod"/>
<attribute id="UUID" value="u-1"/></node></region></save>"""


class ParseModTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        utils = MagicMock(
            plugin_data_path=self.tmp / "data",
            tools_dir=self.tmp / "tools",
            autobuild_paks=False,
            remove_extracted_metadata=True,
        )
        self.parser = pak_parser.BG3PakParser(utils)
        self.mod = self.tmp / "mod"
        self.mod.mkdir()

    def test_empty_mod_has_nothing_to_write(self):
        self.assertEqual(
            self.parser.get_metadata_for_files_in_mod(self.mod, False), ("", None)
        )

    def test_returns_config_without_writing_meta_ini(self):
        pak = build_pak(
            self.mod,
            [("Mods/Foo/meta.lsx", META, 1), ("Mods/Foo/x.txt", b"x", 0)],
        )
        xml, config = self.parser.get_metadata_for_files_in_mod(self.mod, False)
        self.assertIn('value="Foo Mod"', xml)
        assert config is not None
        self.assertEqual(config[pak.name]["UUID"], "u-1")
        self.assertFalse((self.mod / "meta.ini").exists())

    def test_cached_meta_ini_needs_no_write(self):
        build_pak(self.mod, [("Mods/Foo/meta.lsx", META, 1)])
        _, config = self.parser.get_metadata_for_files_in_mod(self.mod, False)
        assert config is not None
        with open(self.mod / "meta.ini", "w", encoding="utf-8") as f:
            config.write(f)
        self.assertEqual(
            self.parser.get_metadata_for_files_in_mod(self.mod, False), ("", None)
        )

    def test_replaced_pak_is_reparsed(self):
        files = [("Mods/Foo/meta.lsx", META, 1), ("Mods/Foo/x.txt", b"x", 0)]
        build_pak(self.mod, files)
        _, config = self.parser.get_metadata_for_files_in_mod(self.mod, False)
        assert config is not None
        with open(self.mod / "meta.ini", "w", encoding="utf-8") as f:
            config.write(f)
        build_pak(
            self.mod,
            [("Mods/Foo/meta.lsx", META.replace(b"u-1", b"u-22"), 1), files[1]],
        )
        xml, _ = self.parser.get_metadata_for_files_in_mod(self.mod, False)
        self.assertIn('value="u-22"', xml)

    def test_unreadable_pak_needs_divine(self):
        build_pak(self.mod, [("Mods/Foo/meta.lsx", META, 3)])
        with self.assertRaises(pak_parser.NeedsDivine):
            self.parser.get_metadata_for_files_in_mod(self.mod, False)
