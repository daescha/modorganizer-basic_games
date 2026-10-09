from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog

from .bg3_tool_plugin import BG3ToolPlugin
from .icons import exchange


class BG3ToolExtractPak(BG3ToolPlugin):
    icon_bytes = exchange
    sub_name = "Extract Pak"
    desc = "Extract a pak and convert its lsf files to lsx for browsing."

    def display(self):
        from ...game_baldursgate3 import BG3Game

        game_plugin = self._organizer.managedGame()
        if not isinstance(game_plugin, BG3Game):
            return
        utils = game_plugin.utils
        pak, _ = QFileDialog.getOpenFileName(
            utils.main_window, self.sub_name, self._organizer.modsPath(), "Pak (*.pak)"
        )
        if not pak or not utils.lslib_retriever.download_lslib_if_missing():
            return
        out_dir = utils.plugin_data_path / "temp" / "extracted" / Path(pak).stem
        parser = utils.pak_parser
        if parser.run_divine(f'extract-package -d "{out_dir}"', pak).returncode:
            return
        parser.run_divine(
            f'convert-resources -d "{out_dir}" -i lsf -o lsx -x "*.lsf"', out_dir
        )
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(out_dir)))
