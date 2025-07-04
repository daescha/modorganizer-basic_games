import concurrent.futures
import configparser
import hashlib
import multiprocessing
import os
import pathlib
import re
import shutil
import subprocess
import traceback
import typing
from configparser import SectionProxy
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

import mobase
from PyQt6.QtCore import qWarning, qInfo
from mobase import IModInterface

from ..basic_features import (
    BasicGameSaveGameInfo,
    BasicLocalSavegames,
    BasicModDataChecker,
    GlobPatterns,
)
from ..basic_game import BasicGame


class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(GlobPatterns(
            valid=[
                "*.pak", # standard mods
                "Public", "Generated", # override mods with loose files
                "Root", # native mods using root builder
                'Script Extender', # mods which are configured via jsons in this folder
                'info.json', '*.txt' # additional files commonly included with bg3 mods which will not be used but do not need to be deleted
            ],
            move={
                "bin": "Root/bin",
                "*.dll": "Root/bin/",
            }
        ))

DEBUG: bool = False
class BG3Game(BasicGame, mobase.IPluginFileMapper):
    Name = "Baldur's Gate 3 Plugin"
    Author = "daescha"
    Version = "0.1.0"
    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["baldursgate3"]

    GameBinary = r"bin\bg3.exe"
    GameDataPath = "Data"
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3"
    GameSavesDirectory = "%GAME_DOCUMENTS%/PlayerProfiles/Public/Savegames/Story"
    GameSaveExtension = "lsv"

    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    mod_cache = {}
    divine_file = str(pathlib.Path(__file__).resolve().parent / 'baldursgate3/Divine.exe')
    max_workers = min(multiprocessing.cpu_count(), 16)
    types = {"Folder": '', "MD5": '', "Name":'', "PublishHandle":'0', "UUID":'', "Version64":'0'}
    mod_settings_xml_start = '''<?xml version="1.0" encoding="UTF-8"?>
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
    mod_settings_xml_end = '''
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

        self._organizer.onAboutToRun(self.on_about_to_run) # on Executable Start
        # self._organizer.onFinishedRun()  # on Executable Stop
        if DEBUG:
            self._organizer.onUserInterfaceInitialized(lambda _: None if self.on_about_to_run() else None) # on Mod Organizer 2 Load
        self._organizer.modList().onModInstalled(self.on_mod_installed)  # on Mod Installed
        return True

    def mappings(self) -> typing.List[mobase.Mapping]:
        mappings = []
        def map_files(path,  dest_func, pattern='**',):
            for file in list(pathlib.Path(path).glob(pattern)):
                # qDebug(f'mapping mo {os.path.relpath(file, self.mopath)} to larian {os.path.relpath(abs_destdir / str(file.name), self.larpath)}')
                mappings.append(mobase.Mapping(
                    source=str(file),
                    destination=self.documentsDirectory().absoluteFilePath(dest_func(file)),
                    is_directory=file.is_dir(),
                    create_target=True,
                ))
        for mod in self.active_mods():
            map_files(mod.absolutePath(), lambda file: 'Mods/' + str(file.name), pattern='**/*.pak')
            map_files(mod.absolutePath() + "/Script Extender", lambda file: os.path.relpath(file, mod.absolutePath()))
        map_files(self._organizer.overwritePath(),  lambda file: os.path.relpath(file, self._organizer.overwritePath()))
        return mappings

    def active_mods(self) -> list[IModInterface]:
        return [self._organizer.modList().getMod(mod_name) for mod_name in
                filter(lambda mod: self._organizer.modList().state(mod) & mobase.ModState.ACTIVE, self._organizer.modList().allModsByProfilePriority())]

    def on_mod_installed(self, mod: mobase.IModInterface) -> None:
        for file in list((pathlib.Path(mod.absolutePath())).glob("**/*.pak")):
            self._get_metadata(mod, file, True)

    def on_about_to_run(self, _: str=None) -> bool:
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            metadata = {mod: [executor.submit(self._get_metadata, mod, file)
                              for file in list(pathlib.Path(mod.absolutePath()).glob("**/*.pak"))] for mod in self.active_mods()}
        with open(pathlib.Path(self._organizer.overwritePath()) / "PlayerProfiles/Public/modsettings.lsx", 'w') as f:
            f.write(self.mod_settings_xml_start + ''.join(future.result(2) for mod in self.active_mods() for future in metadata[mod])
                    + self.mod_settings_xml_end)
        return True
    def _get_metadata(self, mod: mobase.IModInterface, file: pathlib.Path,
                      force_recreate: bool = DEBUG, rm_extracted: bool = not DEBUG) -> str:
        def get_module_short_desc(section: SectionProxy) -> str:
            return '' if 'override' in section.keys() or 'Folder' not in section.keys() else f'''
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="{section['Folder']}"/>
                            <attribute id="MD5" type="LSString" value="{section['MD5']}"/>
                            <attribute id="Name" type="LSString" value="{section['Name']}"/>
                            <attribute id="PublishHandle" type="uint64" value="{section['PublishHandle']}"/>
                            <attribute id="UUID" type="guid" value="{section['UUID']}"/>
                            <attribute id="Version64" type="int64" value="{section['Version64']}"/>
                        </node>'''

        def get_attr_value(root: Element, attr_id: str) -> str:
            attr = root.find(f".//attribute[@id='{attr_id}']")
            return self.types.get(attr_id) if attr is None else attr.get('value', self.types.get(attr_id))

        def extract_data(output_dir: pathlib.Path, section: SectionProxy) -> None:
            file_str = str(file)
            result = subprocess.run(
                    [self.divine_file, "-a", "extract-package", "-g", "bg3",  "-x", "*/meta.lsx",
                     "-s", file_str, "-d", str(output_dir),],
                    creationflags=subprocess.CREATE_NO_WINDOW, check=not DEBUG, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                qWarning(str(result.stdout))
                qWarning(' '.join([self.divine_file, "-a", "extract-package", "-g", "bg3",  "-x", "*/meta.lsx",
                     "-s", f'"{file_str}"', "-d", f'"{str(output_dir)}"',]))
                return
            meta_files = list(output_dir.glob("**/meta.lsx"))
            if not meta_files:
                qInfo(f"No meta.lsx files found in extracted PAK: {file.name}")
                return
            root = ElementTree.parse(str(meta_files[0])).getroot().find(f".//node[@id='ModuleInfo']")
            if root is None:
                qInfo(f"No ModuleInfo node found in meta.lsx for {mod.name()} ")
                return
            folder_name = get_attr_value(root, 'Folder')
            if file_str not in self.mod_cache:
                result = subprocess.run(
                    [self.divine_file, "-a", "list-package", "-g", "bg3", "-s", file_str, ],
                    creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
                self.mod_cache[file_str] = result.returncode == 0 and re.search(
                    rf"(/{folder_name}/(?!meta\.lsx))|(Public/Engine/Timeline/MaterialGroups)", result.stdout, re.MULTILINE)
            if not self.mod_cache[file_str]:
                section['override'] = 'True'
            else:
                for key in self.types:
                    section[key] = get_attr_value(root, key)

        config = configparser.ConfigParser()
        config.read(pathlib.Path(mod.absolutePath()) / "meta.ini", encoding='utf-8')
        output_dir = (pathlib.Path(self._organizer.basePath()) /
                      f'temp/extracted_pak_data/{str(file.name)[:int(len(str(file.name)) / 2)]}-{hashlib.md5(str(file).encode()).hexdigest()[:5]}')
        try:
            if not force_recreate and config.has_section(file.name) and ('override' in config[file.name].keys() or 'Folder' in config[file.name].keys()):
                return get_module_short_desc(config[file.name])
            config[file.name] = {}
            shutil.rmtree(output_dir, ignore_errors=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            extract_data(output_dir, config[file.name])
            with open(pathlib.Path(mod.absolutePath()) / "meta.ini", "w", encoding='utf-8') as f:
                config.write(f)
        except Exception:
            qWarning(traceback.format_exc())
        finally:
            if rm_extracted and os.path.exists(output_dir):
                shutil.rmtree(output_dir, ignore_errors=True)
        return get_module_short_desc(config[file.name])
