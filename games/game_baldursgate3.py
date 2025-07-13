import concurrent.futures
import configparser
import difflib
import hashlib
import itertools
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import tempfile
import traceback
import typing
import urllib.request
import zipfile
from configparser import SectionProxy
from pathlib import Path
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

import mobase
from PyQt6.QtCore import qWarning, qInfo, qDebug, Qt, QLoggingCategory, QCoreApplication
from PyQt6.QtWidgets import QMessageBox, QProgressDialog, QApplication, QMainWindow
from mobase import IModInterface

from ..basic_features import (
    BasicGameSaveGameInfo,
    BasicLocalSavegames,
    BasicModDataChecker,
    GlobPatterns,
)
from ..basic_game import BasicGame

_loose_file_folders = ['Public', 'Mods', 'Generated', 'Localization', 'ScriptExtender']
class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(GlobPatterns(
            valid=[
                "*.pak",  # standard mods
                "bin",  # native mods / Script Extender
                'Script Extender',  # mods which are configured via jsons in this folder
                'Data'
            ],
            move={
                     "Root/": "",  # root builder not needed
                     "*.dll": "bin/",
                     "ScriptExtenderSettings.json": "bin/"
                 } | {f: 'Data/' for f in _loose_file_folders},
            delete=["info.json", "*.txt"]
        ))

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
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3"
    GameSavesDirectory = "%GAME_DOCUMENTS%/PlayerProfiles/Public/Savegames/Story"
    GameSaveExtension = "lsv"
    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    _mod_cache = {}
    _divine_command = None
    _max_workers = None
    _folder_pattern = None
    _log_dir = None
    _modsettings_backup = None
    _modsettings_path = None
    _base_dlls = None
    _types = {"Folder": '', "MD5": '', "Name": '', "PublishHandle": '0', "UUID": '', "Version64": '0'}
    _mod_settings_xml_start = '''<?xml version="1.0" encoding="UTF-8"?>
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
                        </node>'''
    _mod_settings_xml_end = '''
                    </children>
                </node>
            </children>
        </node>
    </region>
</save>'''

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".webp")))
        self._register_feature(BasicLocalSavegames(self.savesDirectory()))
        organizer.onAboutToRun(self.construct_modsettings_xml)
        organizer.onFinishedRun(self.on_finished_run)
        organizer.onUserInterfaceInitialized(self.on_user_interface_initialized)
        organizer.modList().onModInstalled(self.on_mod_installed)
        return True
    def _get_setting(self, key: str) -> mobase.MoVariant:
        return self._organizer.pluginSetting(self.name(), key)

    def _set_setting(self, key: str, value: mobase.MoVariant):
        self._organizer.setPluginSetting(self.name(), key, value)
    def __tr(self, trstr):
        return QCoreApplication.translate(self.Name, trstr)
    def settings(self):
        """ Current list of game settings for Mod Organizer. """
        baseSettings = super().settings()
        customSettings = [
            mobase.PluginSetting("debug","debug plugin", False),
            mobase.PluginSetting("forceLoadDLLs","force load all dlls detected in active mods", True),
            mobase.PluginSetting("logDiff","log a diff of the modsettings.xml file befora and after the game runs to check for differences", False),
            # mobase.PluginSetting("redirect", "Enables automatic redirection of exe files being launched from a mod folder to their respective game folder equivalent.", True),
            # mobase.PluginSetting("installer", "Enables an installer plugin to automatically install root mods when detected.", False),
            # mobase.PluginSetting("priority", "The priority of the installer module for installing root mods.", 110),
            # mobase.PluginSetting("exclusions","List of files and folders to exclude from RootBuilder.", "Saves,Morrowind.ini,Data,Data Files"),
            # mobase.PluginSetting("copyfiles","Determines the files that should be copied.", "**"),
            # mobase.PluginSetting("linkfiles", "Determines the files that should be linked.", ""),
            # mobase.PluginSetting("usvfsfiles","Determines the files that should be mapped with usvfs", ""),
            # mobase.PluginSetting("copypriority","Priority order for determining when to copy files. Lower is better.", 1),
            # mobase.PluginSetting("linkpriority","Priority order for determining when to link files. Lower is better.", 2),
            # mobase.PluginSetting("usvfspriority","Priority order for determining when to usvfs map files. Lower is better.", 3),
            # mobase.PluginSetting("hash","Enables hashing as the method of change detection.", True),
            ]
        for setting in customSettings:
            setting.description = self.__tr(setting.description)
            baseSettings.append(setting)
        return baseSettings
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
        if self._get_setting("forceLoadDLLs"):
            qInfo('detecting dlls in enabled mods')
            libs = set()
            tree = self._organizer.virtualFileTree().find('bin')
            if self._base_dlls is None:
                base_bin = Path(self.gameDirectory().absoluteFilePath("bin"))
                self._base_dlls = {str(f.relative_to(base_bin)) for f in base_bin.glob("*.dll")}
            def cb(_, entry: mobase.FileTreeEntry) -> mobase.IFileTree.WalkReturn:
                relpath = entry.pathFrom(tree)
                if entry.hasSuffix('dll') and relpath not in self._base_dlls:
                    libs.add(relpath)
                return mobase.IFileTree.WalkReturn.CONTINUE
            tree.walk(cb)
            exes = self.executables()
            qDebug(f'dlls to force load: {libs}')
            return efls + [mobase.ExecutableForcedLoadSetting(exe.binary().fileName(), lib).withEnabled(True) for lib in libs for exe in exes]
        return efls

    def mappings(self) -> typing.List[mobase.Mapping]:
        qInfo('creating custom bg3 mappings')
        mappings = []
        docs_path = Path(self.documentsDirectory().path())
        def map_files(path,  dest_func, pattern='*', dest_dir=docs_path):
            for file in list(Path(path).rglob(pattern)):
                qDebug(f"mapping {file} to {dest_dir / dest_func(file)}")
                mappings.append(mobase.Mapping(
                    source=str(file),
                    destination=str(dest_dir / dest_func(file)),
                    is_directory=file.is_dir(),
                    create_target=True,
                ))
        for mod in self.active_mods():
            map_files(mod.absolutePath(), lambda file: f'Mods/{file.name}', pattern='*.pak')
            map_files(f"{mod.absolutePath()}/Script Extender", lambda file: os.path.relpath(file, mod.absolutePath()))
        map_files(self._organizer.overwritePath(),  lambda file: os.path.relpath(file, self._organizer.overwritePath()))
        return mappings

    def on_user_interface_initialized(self, window: QMainWindow) -> None:
        if self._get_setting('debug'):
            self.construct_modsettings_xml()
    def on_finished_run(self, x, y):
        if self._get_setting('logDiff'):
            for x in difflib.unified_diff(open(self._modsettings_backup).readlines(), open(self._modsettings_path).readlines(),
                                          fromfile=str(self._modsettings_backup), tofile=str(self._modsettings_path), lineterm=''):
                qDebug(x)
        if self._log_dir is None:
            self._log_dir = Path(self._organizer.basePath()) / "logs/"
        for path in Path(self._organizer.overwritePath()).rglob("*.log"):
            try:
                (self._log_dir / path.name).unlink(missing_ok=True)
                qDebug(f"moving {path} to {self._log_dir}")
                shutil.move(path, self._log_dir)
            except PermissionError as e:
                qDebug(str(e))

    def active_mods(self) -> list[IModInterface]:
        return [self._organizer.modList().getMod(mod_name) for mod_name in
                filter(lambda mod: self._organizer.modList().state(mod) & mobase.ModState.ACTIVE, self._organizer.modList().allModsByProfilePriority())]

    def on_mod_installed(self, mod: mobase.IModInterface) -> None:
        if self.check_bg3_paths():
            self.get_metadata(mod)

    def get_metadata(self, mod: mobase.IModInterface):
        return ''.join([self._get_metadata(mod, file) for file in sorted(list(Path(mod.absolutePath()).rglob("*.pak")) + [f for f in Path(mod.absolutePath()).glob("*") if f.is_dir()])])

    def construct_modsettings_xml(self, _: str=None) -> bool:
        if self._max_workers is None:
            self._max_workers = min(multiprocessing.cpu_count(), 16)
        if not self.check_bg3_paths():
            return True
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            metadata = {mod: executor.submit(self.get_metadata, mod) for mod in self.active_mods()}
        if self._modsettings_path is None:
            self._modsettings_path = Path(self._organizer.overwritePath()) / "PlayerProfiles/Public/modsettings.lsx"
        with open(self._modsettings_path, 'w+') as f:
            qInfo(f'writing mod load order to {self._modsettings_path}')
            f.write(self._mod_settings_xml_start + ''.join(metadata[mod].result(2) for mod in self.active_mods()) + self._mod_settings_xml_end)
        if self._modsettings_backup is None:
            self._modsettings_backup = Path(self._organizer.basePath()) / 'temp/modsettings.lsx'
        qInfo(f'backing up generated file {self._modsettings_path} to {self._modsettings_backup}, check the backup after the executable runs for differences with the file used by the game if you encounter issues')
        shutil.copy(self._modsettings_path, self._modsettings_backup)
        return True

    def _get_metadata(self, mod: mobase.IModInterface, file: Path,
                      force_recreate: bool|None = None, rm_extracted: bool|None = None) -> str:

        def run_divine(action: str, source: Path | str, extra_args='', loglvl='info') -> subprocess.CompletedProcess[str]:
            if self._divine_command is None:
                self._divine_command = f'{Path(self._organizer.basePath()) / "tools/Divine.exe"} -g bg3 -l {loglvl}'
            command = f'{self._divine_command} -a "{action}" -s "{source}" {extra_args}'
            result = subprocess.run(command, creationflags=subprocess.CREATE_NO_WINDOW, check=not self._get_setting('debug'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode:
                qWarning(f"{command.replace(str(Path.home()), '~', 1).replace(str(Path.home()), '$HOME')}"
                         f" returned stdout: {result.stdout}, stderr: {result.stderr}, code {result.returncode}")
            return result

        def get_module_short_desc() -> str:
            return '' if not config.has_section(file.name) or 'override' in config[file.name].keys() or 'Name' not in config[file.name].keys() else f'''
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="{config[file.name]['Folder']}"/>
                            <attribute id="MD5" type="LSString" value="{config[file.name]['MD5']}"/>
                            <attribute id="Name" type="LSString" value="{config[file.name]['Name']}"/>
                            <attribute id="PublishHandle" type="uint64" value="{config[file.name]['PublishHandle']}"/>
                            <attribute id="UUID" type="guid" value="{config[file.name]['UUID']}"/>
                            <attribute id="Version64" type="int64" value="{config[file.name]['Version64']}"/>
                        </node>'''

        def get_attr_value(root: Element, attr_id: str) -> str:
            attr = root.find(f".//attribute[@id='{attr_id}']")
            return self._types.get(attr_id) if attr is None else attr.get('value', self._types.get(attr_id))

        def extract_data(output_file: Path, ) -> bool:
            if run_divine('extract-single-file', file, extra_args=f'-d "{output_file}" -f meta.lsx').returncode:
                return False
            if not output_file.exists():
                qInfo(f"No meta.lsx files found in {file.name}, {file.name} determined to be an override mod")
                return False
            return True

        def parse_meta_lsx(meta_file: Path, section: SectionProxy):
            root = ElementTree.parse(meta_file).getroot().find(f".//node[@id='ModuleInfo']")
            if root is None:
                qInfo(f"No ModuleInfo node found in meta.lsx for {mod.name()} ")
                return
            folder_name = get_attr_value(root, 'Folder')
            if file.is_dir():
                self._mod_cache[file] = len(list(file.glob(f"*/{folder_name}/**"))) > 1 or len(list(file.glob(f"Public/Engine/Timeline/MaterialGroups/*"))) > 0
            elif file not in self._mod_cache:
                # a mod which has a meta.lsx and is not an override mod meets at least one of three conditions:
                # 1. it has files in Public/Engine/Timeline/MaterialGroups, or
                # 2. it has files in Mods/<folder_name>/ other than the meta.lsx file, or
                # 3. it has files in Public/<folder_name>
                result = run_divine('list-package', file, extra_args=f'--use-regex -x "(/{folder_name}/(?!meta\\.lsx))|(Public/Engine/Timeline/MaterialGroups)"')
                self._mod_cache[file] = not result.returncode and result.stdout.strip()
            if self._mod_cache[file]:
                for key in self._types:
                    section[key] = get_attr_value(root, key)
            else:
                qInfo(f"pak {file.name} determined to be an override mod")
                section['override'] = 'True'
                section['Folder'] = folder_name

        def metadata_to_ini(condition: bool, to_parse):
            config[file.name] = {}
            if condition:
                parse_meta_lsx(to_parse(), config[file.name])
            else:
                config[file.name]['override'] = 'True'
            with open(meta_ini, "w+", encoding='utf-8') as f:
                config.write(f)
            return get_module_short_desc()

        if force_recreate is None:
            force_recreate = self._get_setting('debug')
        if rm_extracted is None:
            rm_extracted = not self._get_setting('debug')
        if self._folder_pattern is None:
            self._folder_pattern = re.compile(f"Data|Script Extender|bin")
        meta_ini = Path(mod.absolutePath()) / "meta.ini"
        config = configparser.ConfigParser()
        config.read(meta_ini, encoding='utf-8')
        try:
            if file.name.endswith("pak"):
                meta_file = (Path(self._organizer.basePath()) /
                              f'temp/extracted_metadata/{file.name[:int(len(file.name) / 2)]}-{hashlib.md5(str(file).encode(), usedforsecurity=False).hexdigest()[:5]}.lsx')
                try:
                    if not force_recreate and config.has_section(file.name) and ('override' in config[file.name].keys() or 'Folder' in config[file.name].keys()):
                        return get_module_short_desc()
                    meta_file.parent.mkdir(parents=True, exist_ok=True)
                    meta_file.unlink(missing_ok=True)
                    return metadata_to_ini(extract_data(meta_file), lambda: meta_file)
                finally:
                    if rm_extracted:
                        meta_file.unlink(missing_ok=True)
            elif file.is_dir() and self._folder_pattern.search(file.name):
                # qDebug(f"directory is not packable: {file}")
                return ''
            elif next(itertools.chain(file.glob(f"{folder}/*") for folder in _loose_file_folders), False):
                qDebug(f"packable dir: {file}")
                pak_path = Path(self._organizer.overwritePath()) / f"Mods/{file.name}.pak"
                pak_path.unlink(missing_ok=True)
                if run_divine('create-package', file, extra_args=f'-d "{pak_path}"').returncode:
                    return ''
                meta_file = list(file.glob("Mods/*/meta.lsx"))
                return metadata_to_ini(len(meta_file) > 0, lambda: meta_file[0])
            else:
                # qDebug(f"non packable dir, unlikely to be used by the game: {file}")
                return ''
        except Exception:
            qWarning(traceback.format_exc())
            return ''

    def check_bg3_paths(self):
        tools_dir = Path(self._organizer.basePath()) / "Tools"
        if (tools_dir / "Divine.exe").exists():
            return True
        main_window = self._organizer.mainWindow() if hasattr(self._organizer, "mainWindow") else None
        msg_box = QMessageBox(main_window)
        msg_box.setWindowTitle("Baldur's Gate 3 Plugin - Missing dependencies")
        msg_box.setText("LSLib Tools are missing.\nThese are necessary for the plugin to work correctly.")
        download_btn = msg_box.addButton("Download", QMessageBox.ButtonRole.DestructiveRole)
        exit_btn = msg_box.addButton("Exit", QMessageBox.ButtonRole.ActionRole)
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.exec()

        if msg_box.clickedButton() == exit_btn:
            err = QMessageBox(main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(f"LSLib tools are required for the proper generation of the modsettings.xml file, file will not be generated")
            return False

        progress = QProgressDialog("Downloading LSLib...", "Cancel", 0, 100, main_window)
        progress.setWindowTitle("BG3 Plugin - Downloading")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.show()

        try:
            zip_path = tools_dir / f"ExportTool.zip"
            tools_dir.mkdir(exist_ok=True, parents=True)
            def reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    progress.setValue(min(int(block_num * block_size * 100 / total_size), 100))
                    QApplication.processEvents()

            with urllib.request.urlopen(' https://api.github.com/repos/Norbyte/lslib/releases/latest') as response:
                urllib.request.urlretrieve(json.loads(response.read().decode('utf-8'))['assets'][0]['browser_download_url'], str(zip_path), reporthook)

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for f in {
                    "CommandLineArgumentsParser.dll",
                    "Divine.dll", "Divine.dll.config", "Divine.exe", "Divine.runtimeconfig.json",
                    "LSLib.dll", "LSLibNative.dll",
                    "LZ4.dll", "System.IO.Hashing.dll", "ZstdSharp.dll",
                }:
                    shutil.move(zip_ref.extract(f'Packed/Tools/{f}', tools_dir), tools_dir / f)
            progress.setValue(100)
        except Exception as e:
            qDebug(f"Download failed: {e}")
            err = QMessageBox(main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(f"Failed to download LSLib tools:\n{traceback.format_exc()}")
            err.exec()
            return False
        return True