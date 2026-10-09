import configparser
import functools
import shutil
import traceback
import typing
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from time import monotonic
from xml.sax.saxutils import escape

from PyQt6.QtCore import (
    QCoreApplication,
    QDir,
    QEventLoop,
    Qt,
    qInfo,
    qWarning,
)
from PyQt6.QtWidgets import QApplication, QMainWindow, QProgressDialog

import mobase

loose_file_folders = {
    "Public",
    "Mods",
    "Generated",
    "Localization",
    "ScriptExtender",
}


def _mod_entries(mod_dir: Path) -> list[tuple[str, str, list[str]]]:
    config = configparser.ConfigParser(interpolation=None)
    config.read(mod_dir / "meta.ini", encoding="utf-8")
    return [
        (
            s["UUID"],
            s.get("Name", s["UUID"]),
            s.get("dependencies", "").split(",") if s.get("dependencies") else [],
        )
        for s in map(config.__getitem__, config.sections())
        if s.get("UUID") and "override" not in s
    ]


def dependency_order_warnings(
    order: list[tuple[str, str, list[str]]], installed: set[str]
) -> list[str]:
    position = {uuid: i for i, (uuid, _, _) in enumerate(order)}
    warnings: list[str] = []
    for i, (_, name, deps) in enumerate(order):
        for dep in filter(installed.__contains__, deps):
            if dep not in position:
                warnings.append(
                    f"{name} depends on {dep}, which is not in the load order"
                )
            elif position[dep] > i:
                warnings.append(f"{name} depends on {dep}, which loads after it")
    return warnings


def get_node_string(
    folder: str = "",
    md5: str = "",
    name: str = "",
    publish_handle: str = "0",
    uuid: str = "",
    version64: str = "0",
) -> str:
    folder, md5, name, publish_handle, uuid, version64 = (
        escape(v, {'"': "&quot;"})
        for v in (folder, md5, name, publish_handle, uuid, version64)
    )
    return f"""
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="{folder}"/>
                            <attribute id="MD5" type="LSString" value="{md5}"/>
                            <attribute id="Name" type="LSString" value="{name}"/>
                            <attribute id="PublishHandle" type="uint64" value="{publish_handle}"/>
                            <attribute id="UUID" type="guid" value="{uuid}"/>
                            <attribute id="Version64" type="int64" value="{version64}"/>
                        </node>"""


class BG3Utils:
    _mod_settings_xml_start = """\
<?xml version="1.0" encoding="UTF-8"?>
<save>
    <version major="4" minor="8" revision="0" build="500"/>
    <region id="ModuleSettings">
        <node id="root">
            <children>
                <node id="Mods">
                    <children>""" + get_node_string(
        folder="GustavX",
        name="GustavX",
        uuid="cb555efe-2d9e-131f-8195-a89329d218ea",
        version64="36028797018963968",
    )
    _mod_settings_xml_end = """
                    </children>
                </node>
            </children>
        </node>
    </region>
</save>"""

    def __init__(self, name: str):
        self.main_window = None
        self._name = name
        from . import lslib_retriever, pak_parser

        self.lslib_retriever = lslib_retriever.LSLibRetriever(self)
        self.pak_parser = pak_parser.BG3PakParser(self)

    def init(self, organizer: mobase.IOrganizer):
        self._organizer = organizer

    @functools.cached_property
    def autobuild_paks(self):
        return bool(self.get_setting("autobuild_paks"))

    @functools.cached_property
    def remove_extracted_metadata(self):
        return bool(self.get_setting("remove_extracted_metadata"))

    @functools.cached_property
    def force_load_dlls(self):
        return bool(self.get_setting("force_load_dlls"))

    @functools.cached_property
    def log_diff(self):
        return bool(self.get_setting("log_diff"))

    @functools.cached_property
    def convert_yamls_to_json(self):
        return bool(self.get_setting("convert_yamls_to_json"))

    @functools.cached_property
    def log_dir(self):
        return create_dir_if_needed(Path(self._organizer.basePath()) / "logs")

    @functools.cached_property
    def modsettings_backup(self):
        return create_dir_if_needed(
            self.plugin_data_path / "temp" / "modsettings.lsx", is_file=True
        )

    @property
    def modsettings_path(self):
        return create_dir_if_needed(
            Path(self._organizer.profilePath()) / "modsettings.lsx", is_file=True
        )

    @functools.cached_property
    def plugin_data_path(self) -> Path:
        """Gets the path to the data folder for the current plugin."""
        return create_dir_if_needed(
            Path(self._organizer.pluginDataPath(), self._name).absolute()
        )

    @functools.cached_property
    def tools_dir(self):
        return create_dir_if_needed(self.plugin_data_path / "tools")

    @functools.cached_property
    def overwrite_path(self):
        return create_dir_if_needed(Path(self._organizer.overwritePath()))

    def active_mods(self) -> list[mobase.IModInterface]:
        modlist = self._organizer.modList()
        return [
            modlist.getMod(mod_name)
            for mod_name in filter(
                lambda mod: modlist.state(mod) & mobase.ModState.ACTIVE,
                modlist.allModsByProfilePriority(),
            )
        ]

    def _set_setting(self, key: str, value: mobase.MoVariant):
        self._organizer.setPluginSetting(self._name, key, value)

    def get_setting(self, key: str) -> mobase.MoVariant:
        return self._organizer.pluginSetting(self._name, key)

    def tr(self, trstr: str) -> str:
        return QCoreApplication.translate(self._name, trstr)

    def create_progress_window(
        self, title: str, max_progress: int, msg: str = "", cancelable: bool = True
    ) -> QProgressDialog:
        progress = QProgressDialog(
            self.tr(msg if msg else title),
            self.tr("Cancel") if cancelable else None,
            0,
            max_progress,
            self.main_window,
        )
        progress.setWindowTitle(self.tr(f"BG3 Plugin: {title}"))
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.show()
        return progress

    def on_user_interface_initialized(self, window: QMainWindow) -> None:
        self.main_window = window

    def on_settings_changed(
        self,
        plugin_name: str,
        setting: str,
        old: mobase.MoVariant,
        new: mobase.MoVariant,
    ) -> None:
        if self._name != plugin_name:
            return
        if setting in {
            "autobuild_paks",
            "remove_extracted_metadata",
            "force_load_dlls",
            "log_diff",
            "convert_yamls_to_json",
        } and hasattr(self, setting):
            delattr(self, setting)

    def construct_modsettings_xml(
        self,
        exec_path: str = "",
        working_dir: typing.Optional[QDir] = None,
        args: str = "",
        force_reparse_metadata: bool = False,
    ) -> bool:
        if "bin/bg3" not in exec_path:
            return True
        active_mods = self.active_mods()
        metadata = self.parse_mods(
            {mod.name(): Path(mod.absolutePath()) for mod in active_mods},
            force_reparse_metadata,
        )
        if metadata is None:
            qWarning("modsettings.lsx generation canceled or timed out, not launching")
            return False
        for warning in dependency_order_warnings(
            [
                m
                for mod in active_mods
                if mod.name() in metadata
                for m in _mod_entries(Path(mod.absolutePath()))
            ],
            {
                m[0]
                for name in self._organizer.modList().allMods()
                for m in _mod_entries(
                    Path(self._organizer.modList().getMod(name).absolutePath())
                )
            },
        ):
            qWarning(warning)
        qInfo(f"writing mod load order to {self.modsettings_path}")
        self.modsettings_path.parent.mkdir(parents=True, exist_ok=True)
        self.modsettings_path.write_text(
            (
                self._mod_settings_xml_start
                + "".join(
                    metadata[mod.name()]
                    for mod in active_mods
                    if mod.name() in metadata
                )
                + self._mod_settings_xml_end
            ),
            encoding="utf-8",
        )
        qInfo(
            f"backing up generated file {self.modsettings_path} to {self.modsettings_backup}, "
            f"check the backup after the executable runs for differences with the file used by the game if you encounter issues"
        )
        self.modsettings_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.modsettings_path, self.modsettings_backup)
        return True

    def parse_mods(self, mods: dict[str, Path], force: bool) -> dict[str, str] | None:
        from .pak_parser import NeedsDivine

        metadata: dict[str, str] = {}
        while mods:
            progress = self.create_progress_window(
                "Generating modsettings.xml", len(mods)
            )
            pool = ThreadPoolExecutor()
            futures = {
                pool.submit(
                    self.pak_parser.get_metadata_for_files_in_mod, path, force
                ): name
                for name, path in mods.items()
            }

            def pump(done: int, progress: QProgressDialog = progress):
                progress.setValue(done)
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)

            finished = wait_for_futures(futures, progress.wasCanceled, pump)
            pool.shutdown(wait=finished, cancel_futures=True)
            progress.close()
            if not finished:
                return None
            retry: dict[str, Path] = {}
            for future, name in futures.items():
                try:
                    metadata[name], config = future.result()
                    if config is not None:
                        with open(mods[name] / "meta.ini", "w", encoding="utf-8") as f:
                            config.write(f)
                except NeedsDivine:
                    retry[name] = mods[name]
                except Exception:
                    qWarning(f"skipping {name}: {traceback.format_exc()}")
            if retry and not self.lslib_retriever.download_lslib_if_missing():
                qWarning(f"skipping mods that need Divine: {sorted(retry)}")
                break
            mods = retry
        return metadata

    def on_mod_installed(self, mod: mobase.IModInterface) -> None:
        self.parse_mods({mod.name(): Path(mod.absolutePath())}, True)


def wait_for_futures(
    futures: typing.Collection[Future[typing.Any]],
    canceled: typing.Callable[[], bool],
    progress: typing.Callable[[int], None],
    stall_timeout: float = 600,
) -> bool:
    pending = set(futures)
    deadline = monotonic() + stall_timeout
    while pending:
        if canceled() or monotonic() > deadline:
            return False
        done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
        if done:
            deadline = monotonic() + stall_timeout
        progress(len(futures) - len(pending))
    return True


def create_dir_if_needed(path: Path, is_file: bool = False) -> Path:
    (path.parent if is_file else path).mkdir(parents=True, exist_ok=True)
    return path


def remove_empty_dirs(root: Path) -> set[Path]:
    removed: set[Path] = set()
    for folder, _, _ in root.walk(top_down=False):
        if folder == root:
            continue
        try:
            folder.rmdir()
            removed.add(folder)
        except OSError:
            pass
    return removed
