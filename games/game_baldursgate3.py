import configparser
import datetime
import difflib
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import traceback
import urllib.request
import zipfile
from configparser import SectionProxy
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Optional
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from PyQt6 import QtCore
from PyQt6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QRunnable,
    Qt,
    QThreadPool,
    qDebug,
    qInfo,
    qWarning,
)
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
)

import mobase

from ..basic_features import (
    BasicGameSaveGameInfo,
    BasicLocalSavegames,
    BasicModDataChecker,
    GlobPatterns,
)
from ..basic_game import BasicGame

_loose_file_folders = ["Public", "Mods", "Generated", "Localization", "ScriptExtender"]


class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                valid=[
                    "*.pak",  # standard mods
                    "bin",  # native mods / Script Extender
                    "Script Extender",  # mods which are configured via jsons in this folder
                    "Data",  # loose file mods
                ],
                move={
                    "Root/": "",
                    "*.dll": "bin/",
                    "ScriptExtenderSettings.json": "bin/",
                }  # root builder not needed
                | {f: "Data/" for f in _loose_file_folders},
                delete=["info.json", "*.txt"],
            )
        )


class Worker(QRunnable):
    def __init__(
        self,
        fn: Callable[[mobase.IModInterface, bool], str],
        mod: mobase.IModInterface,
        auto_build_paks: bool,
        metadata: dict[str, str],
    ):
        super().__init__()
        self.fn = fn
        self.mod = mod
        self.autoBuildPaks = auto_build_paks
        self.metadata = metadata

    def run(self):
        self.metadata.update({self.mod.name(): self.fn(self.mod, self.autoBuildPaks)})


class BG3Game(BasicGame, mobase.IPluginFileMapper):
    Name = "Baldur's Gate 3 Plugin"
    Author = "daescha"
    Version = "0.1.0"
    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["bg3"]
    GameLauncher = "Launcher/LariLauncher.exe"
    GameBinary = "bin/bg3.exe"
    GameDataPath = ""
    GameDocumentsDirectory = (
        "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3"
    )
    GameSavesDirectory = "%GAME_DOCUMENTS%/PlayerProfiles/Public/Savegames/Story"
    GameSaveExtension = "lsv"
    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    _mod_cache: dict[Path, bool] = {}
    _types = {
        "Folder": "",
        "MD5": "",
        "Name": "",
        "PublishHandle": "0",
        "UUID": "",
        "Version64": "0",
    }
    _mod_settings_xml_start = """<?xml version="1.0" encoding="UTF-8"?>
<save>
    <version major="4" minor="8" revision="0" build="200"/>
    <region id="ModuleSettings">
        <node id="root">
            <children>
                <node id="Mods">
                    <children>
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="GustavX"/>
                            <attribute id="MD5" type="LSString" value=""/>
                            <attribute id="Name" type="LSString" value="GustavX"/>
                            <attribute id="PublishHandle" type="uint64" value="0"/>
                            <attribute id="UUID" type="guid" value="cb555efe-2d9e-131f-8195-a89329d218ea"/>
                            <attribute id="Version64" type="int64" value="36028797018963968"/>
                        </node>"""
    _mod_settings_xml_end = """
                    </children>
                </node>
            </children>
        </node>
    </region>
</save>"""

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".webp")))
        self._register_feature(BasicLocalSavegames(self.savesDirectory()))
        organizer.onAboutToRun(self._construct_modsettings_xml)
        organizer.onFinishedRun(self._on_finished_run)
        organizer.onUserInterfaceInitialized(self._on_user_interface_initialized)
        organizer.modList().onModInstalled(self._on_mod_installed)
        organizer.onPluginSettingChanged(self._on_settings_changed)
        return True

    def settings(self):
        base_settings = super().settings()
        custom_settings = [
            mobase.PluginSetting(
                "force_load_dlls",
                "Force load all dlls detected in active mods. Removes the need for 'Native Mod Loader' and similar mods.",
                True,
            ),
            mobase.PluginSetting(
                "log_diff",
                "Log a diff of the modsettings.xml file before and after the game runs to check for differences.",
                False,
            ),
            mobase.PluginSetting(
                "delete_levelcache_folders_older_than_x_days",
                "Maximum number of days a file in overwrite/LevelCache is allowed to exist before being deleted "
                "after the executable finishes. Set to negative to disable.",
                3,
            ),
            mobase.PluginSetting(
                "autobuild_paks",
                "Autobuild folders likely to be PAK folders with every run of an executable.",
                True,
            ),
            mobase.PluginSetting(
                "remove_extracted_metadata",
                "Remove extracted meta.lsx files when they are no longer needed.",
                True,
            ),
            mobase.PluginSetting(
                "force_reparse_metadata",
                "Force reparsing mod metadata immediately.",
                False,
            ),
            mobase.PluginSetting(
                "check_for_lslib_updates",
                "Check to see if there has been a new release of LSLib and create download dialog if so.",
                False,
            ),
        ]
        for setting in custom_settings:
            setting.description = self.__tr(setting.description)
            base_settings.append(setting)
        return base_settings

    def executables(self) -> list[mobase.ExecutableInfo]:
        return [
            mobase.ExecutableInfo(
                f"{self.gameName()}: DX11",
                self.gameDirectory().absoluteFilePath("bin/bg3_dx11.exe"),
            ),
            mobase.ExecutableInfo(
                f"{self.gameName()}: Vulkan",
                self.gameDirectory().absoluteFilePath(self.binaryName()),
            ),
            mobase.ExecutableInfo(
                "Larian Launcher",
                self.gameDirectory().absoluteFilePath(self.getLauncherName()),
            ),
        ]

    def executableForcedLoads(self) -> list[mobase.ExecutableForcedLoadSetting]:
        try:
            efls = super().executableForcedLoads()
        except AttributeError:
            efls = []
        if self._get_setting("force_load_dlls"):
            qInfo("detecting dlls in enabled mods")
            libs: set[str] = set()
            tree: mobase.IFileTree | mobase.FileTreeEntry | None = (
                self._organizer.virtualFileTree().find("bin")
            )
            if type(tree) is not mobase.IFileTree:
                return efls

            def find_dlls(
                _: Any, entry: mobase.FileTreeEntry
            ) -> mobase.IFileTree.WalkReturn:
                relpath = entry.pathFrom(tree)
                if (
                    relpath
                    and entry.hasSuffix("dll")
                    and relpath not in self._base_dlls
                ):
                    libs.add(relpath)
                return mobase.IFileTree.WalkReturn.CONTINUE

            tree.walk(find_dlls)
            exes = self.executables()
            qDebug(f"dlls to force load: {libs}")
            efls = efls + [
                mobase.ExecutableForcedLoadSetting(
                    exe.binary().fileName(), lib
                ).withEnabled(True)
                for lib in libs
                for exe in exes
            ]
        return efls

    def mappings(self) -> list[mobase.Mapping]:
        qInfo("creating custom bg3 mappings")
        mappings: list[mobase.Mapping] = []
        docs_path = Path(self.documentsDirectory().path())

        def map_files(
            path: Path,
            pattern: str = "*",
            rel: bool = True,
        ):
            dest_func: Callable[[Path], str] = (
                (lambda f: os.path.relpath(f, path))
                if rel
                else lambda f: f"Mods/{f.name}"
            )
            for file in list(path.rglob(pattern)):
                mappings.append(
                    mobase.Mapping(
                        source=str(file),
                        destination=str(docs_path / dest_func(file)),
                        is_directory=file.is_dir(),
                        create_target=True,
                    )
                )

        progress = self._create_progress_window(
            "Mapping files to documents folder", len(self._active_mods()) + 1
        )
        for mod in self._active_mods():
            modpath = Path(mod.absolutePath())
            map_files(modpath, pattern="*.pak", rel=False)
            map_files(modpath / "Script Extender")
            progress.setValue(progress.value() + 1)
            QApplication.processEvents()
        map_files(self._overwrite_path)
        progress.setValue(len(self._active_mods()) + 1)
        QApplication.processEvents()
        return mappings

    @cached_property
    def _base_dlls(self) -> set[str]:
        base_bin = Path(self.gameDirectory().absoluteFilePath("bin"))
        return {str(f.relative_to(base_bin)) for f in base_bin.glob("*.dll")}

    @cached_property
    def _plugin_data_path(self) -> Path:
        """Gets the path to the data folder for the current plugin."""
        return Path(self._organizer.pluginDataPath(), self.name()).absolute()

    @cached_property
    def _overwrite_path(self):
        return Path(self._organizer.overwritePath())

    @cached_property
    def _log_dir(self):
        return Path(self._organizer.basePath()) / "logs/"

    @cached_property
    def _modsettings_backup(self):
        return self._plugin_data_path / "temp/modsettings.lsx"

    @cached_property
    def _modsettings_path(self):
        return self._overwrite_path / "PlayerProfiles/Public/modsettings.lsx"

    @cached_property
    def _divine_command(self):
        return f"{self._tools_dir / 'Divine.exe'} -g bg3 -l info"

    @cached_property
    def _folder_pattern(self):
        return re.compile("Data|Script Extender|bin")

    @cached_property
    def _tools_dir(self):
        return self._plugin_data_path / "tools"

    @cached_property
    def _needed_lslib_files(self):
        return {
            self._tools_dir / x
            for x in {
                "CommandLineArgumentsParser.dll",
                "Divine.dll",
                "Divine.dll.config",
                "Divine.exe",
                "Divine.runtimeconfig.json",
                "LSLib.dll",
                "LSLibNative.dll",
                "LZ4.dll",
                "System.IO.Hashing.dll",
                "ZstdSharp.dll",
            }
        }

    def _get_setting(self, key: str) -> mobase.MoVariant:
        return self._organizer.pluginSetting(self.name(), key)

    def _set_setting(self, key: str, value: mobase.MoVariant):
        self._organizer.setPluginSetting(self.name(), key, value)

    def __tr(self, trstr: str) -> str:
        return QCoreApplication.translate(self.name(), trstr)

    def _active_mods(self) -> list[mobase.IModInterface]:
        modlist = self._organizer.modList()
        return [
            modlist.getMod(mod_name)
            for mod_name in filter(
                lambda mod: modlist.state(mod) & mobase.ModState.ACTIVE,
                modlist.allModsByProfilePriority(),
            )
        ]

    def _create_progress_window(
        self, title: str, max_progress: int, msg: str = ""
    ) -> QProgressDialog:
        progress = QProgressDialog(
            self.__tr(msg if msg else title),
            self.__tr("Cancel"),
            0,
            max_progress,
            self._main_window,
        )
        progress.setWindowTitle(self.__tr(f"BG3 Plugin: {title}"))
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.show()
        return progress

    def _on_settings_changed(
        self,
        plugin_name: str,
        setting: str,
        old: mobase.MoVariant,
        new: mobase.MoVariant,
    ) -> None:
        if self.name() != plugin_name or not new:
            return
        if setting == "check_for_lslib_updates":
            try:
                self._download_lslib_if_missing()
            finally:
                self._set_setting(setting, False)
        elif setting == "force_reparse_metadata":
            try:
                self._construct_modsettings_xml()
            finally:
                self._set_setting(setting, False)

    def _on_user_interface_initialized(self, window: QMainWindow) -> None:
        self._main_window = window
        pass

    def _on_finished_run(self, x: str, y: int):
        if self._get_setting("log_diff"):
            for x in difflib.unified_diff(
                open(self._modsettings_backup).readlines(),
                open(self._modsettings_path).readlines(),
                fromfile=str(self._modsettings_backup),
                tofile=str(self._modsettings_path),
                lineterm="",
            ):
                qDebug(x)
        for path in self._overwrite_path.rglob("*.log"):
            try:
                (self._log_dir / path.name).unlink(missing_ok=True)
                qDebug(f"moving {path} to {self._log_dir}")
                shutil.move(path, self._log_dir)
            except PermissionError as e:
                qDebug(str(e))
        days = self._get_setting("delete_levelcache_folders_older_than_x_days")
        if type(days) is int and days >= 0:
            cutoff_time = datetime.datetime.now() - datetime.timedelta(days=days)
            qDebug(f"cleaning folders in overwrite/LevelCache older than {cutoff_time}")
            for path in self._overwrite_path.glob("LevelCache/*"):
                if (
                    datetime.datetime.fromtimestamp(os.path.getmtime(path))
                    < cutoff_time
                ):
                    shutil.rmtree(path, ignore_errors=True)
        qDebug("cleaning empty dirs from overwrite directory")
        for folder in sorted(list(os.walk(self._overwrite_path))[1:], reverse=True):
            try:
                os.rmdir(folder[0])
            except OSError:
                pass

    def _construct_modsettings_xml(self, _: str = "") -> bool:
        if not self._download_lslib_if_missing():
            return True
        active_mods = self._active_mods()
        autobuild_paks = self._get_setting("autobuild_paks")
        progress = self._create_progress_window(
            "Generating modsettings.xml", len(active_mods)
        )
        threadpool = QThreadPool.globalInstance()
        if threadpool is None or type(autobuild_paks) is not bool:
            return False
        metadata: dict[str, str] = {}

        def get_runnable(mod: mobase.IModInterface):
            threadpool.start(
                QRunnable.create(
                    lambda: metadata.update(
                        self._get_metadata_for_files_in_mod(mod, autobuild_paks)
                    )
                )
            )

        for mod in active_mods:
            get_runnable(mod)
        count = 0
        num_active_mods = len(active_mods)
        total_intervals_to_wait = (num_active_mods * 2) + 20
        while len(metadata.keys()) < num_active_mods:
            progress.setValue(len(metadata.keys()))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
            count += 1
            if count == total_intervals_to_wait:
                remaining_mods = {mod.name() for mod in active_mods} - metadata.keys()
                qWarning(f"processing did not finish in time for: {remaining_mods}")
                progress.setValue(num_active_mods)
                break
            QtCore.QThread.msleep(100)
        qInfo(f"writing mod load order to {self._modsettings_path}")
        self._modsettings_path.write_text(
            (
                self._mod_settings_xml_start
                + "".join(
                    metadata[mod.name()]
                    for mod in active_mods
                    if mod.name() in metadata
                )
                + self._mod_settings_xml_end
            )
        )
        qInfo(
            f"backing up generated file {self._modsettings_path} to {self._modsettings_backup}, "
            f"check the backup after the executable runs for differences with the file used by the game if you encounter issues"
        )
        shutil.copy(self._modsettings_path, self._modsettings_backup)
        return True

    def _on_mod_installed(self, mod: mobase.IModInterface) -> None:
        if self._download_lslib_if_missing():
            self._get_metadata_for_files_in_mod(
                mod, bool(self._get_setting("autobuild_paks"))
            )

    def _get_metadata_for_files_in_mod(
        self, mod: mobase.IModInterface, auto_build_paks: bool
    ):
        return {
            mod.name(): "".join(
                [
                    self._get_metadata_for_file(mod, file)
                    for file in sorted(
                        list(Path(mod.absolutePath()).rglob("*.pak"))
                        + (
                            [
                                f
                                for f in Path(mod.absolutePath()).glob("*")
                                if f.is_dir()
                            ]
                            if auto_build_paks
                            else []
                        )
                    )
                ]
            )
        }

    def _get_metadata_for_file(
        self,
        mod: mobase.IModInterface,
        file: Path,
        force_recreate: Optional[bool] = None,
        rm_extracted: Optional[bool] = None,
    ) -> str:
        def run_divine(
            action: str, source: Path | str, extra_args: str = ""
        ) -> subprocess.CompletedProcess[str]:
            command = f'{self._divine_command} -a "{action}" -s "{source}" {extra_args}'
            result = subprocess.run(
                command,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode:
                qWarning(
                    f"{command.replace(str(Path.home()), '~', 1).replace(str(Path.home()), '$HOME')}"
                    f" returned stdout: {result.stdout}, stderr: {result.stderr}, code {result.returncode}"
                )
            return result

        def get_module_short_desc() -> str:
            return (
                ""
                if not config.has_section(file.name)
                or "override" in config[file.name].keys()
                or "Name" not in config[file.name].keys()
                else f"""
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="{config[file.name]["Folder"]}"/>
                            <attribute id="MD5" type="LSString" value="{config[file.name]["MD5"]}"/>
                            <attribute id="Name" type="LSString" value="{config[file.name]["Name"]}"/>
                            <attribute id="PublishHandle" type="uint64" value="{config[file.name]["PublishHandle"]}"/>
                            <attribute id="UUID" type="guid" value="{config[file.name]["UUID"]}"/>
                            <attribute id="Version64" type="int64" value="{config[file.name]["Version64"]}"/>
                        </node>"""
            )

        def get_attr_value(root: Element, attr_id: str) -> str:
            default_val = self._types.get(attr_id) or ""
            attr = root.find(f".//attribute[@id='{attr_id}']")
            return default_val if attr is None else attr.get("value", default_val)

        def extract_data(output_file: Path) -> bool:
            if run_divine(
                "extract-single-file",
                file,
                extra_args=f'-d "{output_file}" -f meta.lsx',
            ).returncode:
                return False
            if not output_file.exists():
                qInfo(
                    f"No meta.lsx files found in {file.name}, {file.name} determined to be an override mod"
                )
                return False
            return True

        def parse_meta_lsx(meta_file: Path, section: SectionProxy):
            root = (
                ElementTree.parse(meta_file).getroot().find(".//node[@id='ModuleInfo']")
            )
            if root is None:
                qInfo(f"No ModuleInfo node found in meta.lsx for {mod.name()} ")
                return
            folder_name = get_attr_value(root, "Folder")
            if file.is_dir():
                self._mod_cache[file] = (
                    len(list(file.glob(f"*/{folder_name}/**"))) > 1
                    or len(list(file.glob("Public/Engine/Timeline/MaterialGroups/*")))
                    > 0
                )
            elif file not in self._mod_cache:
                # a mod which has a meta.lsx and is not an override mod meets at least one of three conditions:
                # 1. it has files in Public/Engine/Timeline/MaterialGroups, or
                # 2. it has files in Mods/<folder_name>/ other than the meta.lsx file, or
                # 3. it has files in Public/<folder_name>
                result = run_divine(
                    "list-package",
                    file,
                    extra_args=f'--use-regex -x "(/{folder_name}/(?!meta\\.lsx))|(Public/Engine/Timeline/MaterialGroups)"',
                )
                self._mod_cache[file] = (
                    result.returncode == 0 and result.stdout.strip() != ""
                )
            if self._mod_cache[file]:
                for key in self._types:
                    section[key] = get_attr_value(root, key)
            else:
                qInfo(f"pak {file.name} determined to be an override mod")
                section["override"] = "True"
                section["Folder"] = folder_name

        def metadata_to_ini(condition: bool, to_parse: Callable[[], Path]):
            config[file.name] = {}
            if condition:
                parse_meta_lsx(to_parse(), config[file.name])
            else:
                config[file.name]["override"] = "True"
            with open(meta_ini, "w+", encoding="utf-8") as f:
                config.write(f)
            return get_module_short_desc()

        if force_recreate is None:
            force_recreate = bool(self._get_setting("force_reparse_metadata"))
        if rm_extracted is None:
            rm_extracted = bool(self._get_setting("remove_extracted_metadata"))
        meta_ini = Path(mod.absolutePath()) / "meta.ini"
        config = configparser.ConfigParser()
        config.read(meta_ini, encoding="utf-8")
        try:
            if file.name.endswith("pak"):
                meta_file = (
                    self._plugin_data_path
                    / f"temp/extracted_metadata/{file.name[: int(len(file.name) / 2)]}-{hashlib.md5(str(file).encode(), usedforsecurity=False).hexdigest()[:5]}.lsx"
                )
                try:
                    if (
                        not force_recreate
                        and config.has_section(file.name)
                        and (
                            "override" in config[file.name].keys()
                            or "Folder" in config[file.name].keys()
                        )
                    ):
                        return get_module_short_desc()
                    meta_file.parent.mkdir(parents=True, exist_ok=True)
                    meta_file.unlink(missing_ok=True)
                    return metadata_to_ini(extract_data(meta_file), lambda: meta_file)
                finally:
                    if rm_extracted:
                        meta_file.unlink(missing_ok=True)
            elif file.is_dir() and self._folder_pattern.search(file.name):
                # qDebug(f"directory is not packable: {file}")
                return ""
            elif next(
                itertools.chain(
                    file.glob(f"{folder}/*") for folder in _loose_file_folders
                ),
                False,
            ):
                qInfo(f"packable dir: {file}")
                pak_path = self._overwrite_path / f"Mods/{file.name}.pak"
                pak_path.unlink(missing_ok=True)
                if run_divine(
                    "create-package", file, extra_args=f'-d "{pak_path}"'
                ).returncode:
                    return ""
                meta_files = list(file.glob("Mods/*/meta.lsx"))
                return metadata_to_ini(len(meta_files) > 0, lambda: meta_files[0])
            else:
                # qDebug(f"non packable dir, unlikely to be used by the game: {file}")
                return ""
        except Exception:
            qWarning(traceback.format_exc())
            return ""

    def _download_lslib_if_missing(self):
        if not self._get_setting("check_for_lslib_updates") and all(
            x.exists() for x in self._needed_lslib_files
        ):
            return True
        try:
            self._tools_dir.mkdir(exist_ok=True, parents=True)
            downloaded = False

            def reporthook(block_num: int, block_size: int, total_size: int) -> None:
                if total_size > 0:
                    progress.setValue(
                        min(int(block_num * block_size * 100 / total_size), 100)
                    )
                    QApplication.processEvents()

            with urllib.request.urlopen(
                "https://api.github.com/repos/Norbyte/lslib/releases/latest"
            ) as response:
                assets = json.loads(response.read().decode("utf-8"))["assets"][0]
                zip_path = self._tools_dir / assets["name"]
                if not zip_path.exists():
                    old_archives = list(self._tools_dir.glob("*.zip"))
                    msg_box = QMessageBox(self._main_window)
                    msg_box.setWindowTitle(
                        self.__tr("Baldur's Gate 3 Plugin - Missing dependencies")
                    )
                    if old_archives:
                        msg_box.setText(self.__tr("LSLib update available."))
                    else:
                        msg_box.setText(
                            self.__tr(
                                "LSLib tools are missing.\nThese are necessary for the plugin to create the load order file for BG3."
                            )
                        )
                    msg_box.addButton(
                        self.__tr("Download"), QMessageBox.ButtonRole.DestructiveRole
                    )
                    exit_btn = msg_box.addButton(
                        self.__tr("Exit"), QMessageBox.ButtonRole.ActionRole
                    )
                    msg_box.setIcon(QMessageBox.Icon.Warning)
                    msg_box.exec()

                    if msg_box.clickedButton() == exit_btn:
                        if not old_archives:
                            err = QMessageBox(self._main_window)
                            err.setIcon(QMessageBox.Icon.Critical)
                            err.setText(
                                "LSLib tools are required for the proper generation of the modsettings.xml file, file will not be generated"
                            )
                            return False
                    else:
                        progress = self._create_progress_window(
                            "Downloading LSLib", 100
                        )
                        urllib.request.urlretrieve(
                            assets["browser_download_url"], str(zip_path), reporthook
                        )
                        downloaded = True
                        for archive in old_archives:
                            archive.unlink()
                        old_archives = []
                else:
                    old_archives = []
                    new_msg = QMessageBox(self._main_window)
                    new_msg.setIcon(QMessageBox.Icon.Information)
                    new_msg.setText(
                        self.__tr("Latest version of LSLib already downloaded!")
                    )

        except Exception as e:
            qDebug(f"Download failed: {e}")
            err = QMessageBox(self._main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(f"Failed to download LSLib tools:\n{traceback.format_exc()}")
            err.exec()
            return False
        try:
            if old_archives:
                zip_path = sorted(old_archives)[-1]
            if old_archives or not downloaded:
                dialog_message = "Ensuring all necessary LSLib files have been extracted from archive..."
                win_title = "Verifying LSLib files"
            else:
                dialog_message = "Extracting/Updating LSLib files..."
                win_title = "Extracting LSLib"
            x_progress = self._create_progress_window(
                win_title, len(self._needed_lslib_files), msg=dialog_message
            )
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                for file in self._needed_lslib_files:
                    if downloaded or not file.exists():
                        shutil.move(
                            zip_ref.extract(
                                f"Packed/Tools/{file.name}", self._tools_dir
                            ),
                            file,
                        )
                    x_progress.setValue(x_progress.value() + 1)
                    QApplication.processEvents()
            shutil.rmtree(self._tools_dir / "Packed", ignore_errors=True)
        except Exception as e:
            qDebug(f"Extraction failed: {e}")
            err = QMessageBox(self._main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(f"Failed to extract LSLib tools:\n{traceback.format_exc()}")
            err.exec()
            return False
        return True
