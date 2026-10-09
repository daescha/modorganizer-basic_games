import shutil
import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

import mobase

from .. import lspk
from .bg3_tool_plugin import BG3ToolPlugin
from .icons import exchange


def pak_mod_name(pak: Path) -> str:
    try:
        if meta := lspk.read_meta(pak):
            attr = ElementTree.fromstring(meta).find(".//attribute[@id='Name']")
            if attr is not None and (name := attr.get("value", "").strip()):
                return name
    except (
        lspk.UnsupportedPak,
        OSError,
        struct.error,
        zlib.error,
        ElementTree.ParseError,
    ):
        pass
    return pak.stem


def find_importable_paks(
    game_mods: Path, mo2_mods: Path
) -> tuple[list[Path], list[Path]]:
    paks = sorted(game_mods.glob("*.pak")) if game_mods.is_dir() else []
    managed = {p.name.lower() for p in mo2_mods.glob("*/**/*.pak")}
    importable = [p for p in paks if p.name.lower() not in managed]
    return importable, [p for p in paks if p.name.lower() in managed]


class BG3ToolImportMods(BG3ToolPlugin):
    icon_bytes = exchange
    sub_name = "Import Game Mods"
    desc = "Move paks from the game's own Mods folder into new MO2 mods."

    def display(self):
        from ...game_baldursgate3 import BG3Game

        game_plugin = self._organizer.managedGame()
        if not isinstance(game_plugin, BG3Game):
            return
        window = game_plugin.utils.main_window
        game_mods = Path(game_plugin.documentsDirectory().absoluteFilePath("Mods"))
        paks, skipped = find_importable_paks(
            game_mods, Path(self._organizer.modsPath())
        )
        skipped_text = "".join(f"\n{p.name}" for p in skipped)
        if skipped_text:
            skipped_text = f"\n\nAlready in an MO2 mod, skipped:{skipped_text}"
        if not paks:
            QMessageBox.information(
                window,
                self.sub_name,
                f"No paks to import in {game_mods}.{skipped_text}",
            )
            return
        dialog = QDialog(window)
        dialog.setWindowTitle(self.sub_name)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(f"Move these paks from {game_mods} into new MO2 mods:{skipped_text}")
        )
        list_widget = QListWidget()
        for pak in paks:
            item = QListWidgetItem(f"{pak_mod_name(pak)} ({pak.name})")
            item.setData(Qt.ItemDataRole.UserRole, pak)
            item.setCheckState(Qt.CheckState.Checked)
            list_widget.addItem(item)
        layout.addWidget(list_widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)  # pyright: ignore[reportUnknownMemberType]
        buttons.rejected.connect(dialog.reject)  # pyright: ignore[reportUnknownMemberType]
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            pak: Path = item.data(Qt.ItemDataRole.UserRole)
            mod = self._organizer.createMod(mobase.GuessedString(pak_mod_name(pak)))
            target = Path(mod.absolutePath(), "Mods")
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(pak, target / pak.name)
        self._organizer.refresh()
