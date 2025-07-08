import concurrent.futures
import configparser
import difflib
import hashlib
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback
import typing
from configparser import SectionProxy
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

import mobase
from PyQt6.QtCore import qWarning, qInfo, qDebug
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
                "*/Public", "*/Localization", "*/Generated", "*/ScriptExtender", "*/Mods", # unpacked mods that will be packed right before game is run
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
    GameValidShortNames = ["bg3"]

    GameBinary = "bin/bg3.exe"
    GameDataPath = "Data"
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3"
    GameSavesDirectory = "%GAME_DOCUMENTS%/PlayerProfiles/Public/Savegames/Story"
    GameSaveExtension = "lsv"

    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    mod_cache = {}
    divine_file = str(Path(__file__).resolve().parent / 'baldursgate3/Divine.exe')
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
        self.folder_pattern = re.compile("(Script Extender)|(Root)|(Generated)|(Public)")

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".webp")))
        self._register_feature(BasicLocalSavegames(self.savesDirectory()))

        self._organizer.onAboutToRun(self.construct_modsettings_xml)
        self._organizer.onFinishedRun(self.on_finished_run)
        if DEBUG:
            self._organizer.onUserInterfaceInitialized(lambda _: None if self.construct_modsettings_xml() else None)
        self._organizer.modList().onModInstalled(self.on_mod_installed)
        self.log_dir = Path(organizer.basePath()) / "logs/"
        self.modsettings_backup = Path(organizer.basePath()) / 'temp/modsettings.lsx'
        self.modsettings_path = Path(organizer.overwritePath()) / "PlayerProfiles/Public/modsettings.lsx"
        return True

    def mappings(self) -> typing.List[mobase.Mapping]:
        mappings = []
        def map_files(path,  dest_func, pattern='**',):
            for file in list(Path(path).glob(pattern)):
                qDebug(f"mapping {file} to {self.documentsDirectory().absoluteFilePath(dest_func(file))}")
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

    def on_finished_run(self, x, y):
        if DEBUG:
            for x in difflib.unified_diff(open(self.modsettings_backup).readlines(), open(self.modsettings_path).readlines(),
                                          fromfile=str(self.modsettings_backup), tofile=str(self.modsettings_path), lineterm=''):
                qDebug(x)
        for path in Path(self.dataDirectory().absolutePath()).parent.rglob("*.log"):
            (self.log_dir / path.name).unlink(missing_ok=True)
            qDebug(f"moving {path} to {self.log_dir}")
            shutil.move(path, self.log_dir)

    def active_mods(self) -> list[IModInterface]:
        return [self._organizer.modList().getMod(mod_name) for mod_name in
                filter(lambda mod: self._organizer.modList().state(mod) & mobase.ModState.ACTIVE, self._organizer.modList().allModsByProfilePriority())]

    def on_mod_installed(self, mod: mobase.IModInterface) -> None:
        for file in list(Path(mod.absolutePath()).glob("**/*.pak")):
            self._get_metadata(mod, file, True)

    def get_metadata(self, mod):
        return ''.join([self._get_metadata(mod, file) for file in sorted(list(Path(mod.absolutePath()).glob("**/*.pak")) + [f for f in Path(mod.absolutePath()).glob("*") if f.is_dir()])])

    def construct_modsettings_xml(self, _: str=None) -> bool:
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            metadata = {mod: executor.submit(self.get_metadata, mod) for mod in self.active_mods()}

        with open(self.modsettings_path, 'w') as f:
            f.write(self.mod_settings_xml_start + ''.join(metadata[mod].result(2) for mod in self.active_mods()) + self.mod_settings_xml_end)
        if DEBUG:
            shutil.copy(self.modsettings_path, self.modsettings_backup)
        return True

    def _get_metadata(self, mod: mobase.IModInterface, file: Path,
                      force_recreate: bool = DEBUG, rm_extracted: bool = not DEBUG) -> str:
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
            return self.types.get(attr_id) if attr is None else attr.get('value', self.types.get(attr_id))

        def extract_data(output_dir: Path, ) -> bool:
            args = [self.divine_file, "-a", "extract-single-file", "-g", "bg3", "-f", "meta.lsx",
                    "-s", str(file), "-d", str(output_dir), "-l", "debug" if DEBUG else "info"]
            result = subprocess.run(args, creationflags=subprocess.CREATE_NO_WINDOW, check=not DEBUG,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if result.returncode != 0:
                qWarning(f"{' '.join(args)} returned {result.stdout}, code {result.returncode}")
                return False
            if not output_dir.exists():
                qInfo(f"No meta.lsx files found in {file.name}, {file.name} determined to be an override mod")
                return False
            return True

        def parse_meta_lsx(meta_file: Path, section: SectionProxy):
            root = ElementTree.parse(meta_file).getroot().find(f".//node[@id='ModuleInfo']")
            if root is None:
                qInfo(f"No ModuleInfo node found in meta.lsx for {mod.name()} ")
                return
            folder_name = get_attr_value(root, 'Folder')
            if file.is_file():
                if file not in self.mod_cache:
                    # a mod which has a meta.lsx and is not an override mod meets at least one of three conditions:
                    # 1. it has files in Public/Engine/Timeline/MaterialGroups, or
                    # 2. it has files in Mods/<folder_name>/ other than the meta.lsx file, or
                    # 3. it has files in Public/<folder_name>
                    result = subprocess.run(
                        [self.divine_file, "-a", "list-package", "-g", "bg3", "-s", str(file), "--use-regex",
                         "-x", rf"(/{folder_name}/(?!meta\.lsx))|(Public/Engine/Timeline/MaterialGroups)", ],
                        creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
                    self.mod_cache[file] = result.returncode == 0 and result.stdout.strip()
            else:
                self.mod_cache[file] = len(list(file.glob(f"*/{folder_name}/**"))) > 1 or len(list(file.glob(f"Public/Engine/Timeline/MaterialGroups/*"))) > 0
            if not self.mod_cache[file]:
                qInfo(f"pak {file.name} determined to be an override mod")
                section['override'] = 'True'
                section['Folder'] = folder_name
            else:
                for key in self.types:
                    section[key] = get_attr_value(root, key)
        meta_ini = Path(mod.absolutePath()) / "meta.ini"
        config = configparser.ConfigParser()
        config.read(meta_ini, encoding='utf-8')
        def metadata_to_ini(condition, to_parse):
            config[file.name] = {}
            if condition:
                parse_meta_lsx(to_parse(), config[file.name])
            else:
                config[file.name]['override'] = 'True'
            with open(meta_ini, "w", encoding='utf-8') as f:
                config.write(f)
            return get_module_short_desc()
        try:
            if file.name.endswith("pak"):
                meta_file = (Path(self._organizer.basePath()) /
                              f'temp/extracted_metadata/{str(file.name)[:int(len(str(file.name)) / 2)]}-{hashlib.md5(str(file).encode(), usedforsecurity=False).hexdigest()[:5]}.lsx')
                try:
                    if not force_recreate and config.has_section(file.name) and ('override' in config[file.name].keys() or 'Folder' in config[file.name].keys()):
                        return get_module_short_desc()
                    meta_file.parent.mkdir(parents=True, exist_ok=True)
                    meta_file.unlink(missing_ok=True)
                    return metadata_to_ini(extract_data(meta_file), lambda: meta_file)
                finally:
                    if rm_extracted:
                        meta_file.unlink(missing_ok=True)
            elif file.is_dir() and self.folder_pattern.search(file.name):
                # qDebug(f"directory is not packable: {file}")
                return ''
            elif next(file.glob("Public/*"), False) or next(file.glob("Mods/*"), False) or next(file.glob("Generated/*"), False) or next(file.glob("Localization/*"), False) or next(file.glob("ScriptExtender/*"), False):
                qDebug(f"packable dir: {file}")
                pak_path = Path(self._organizer.overwritePath()) / f"Mods/{file.name}.pak"
                pak_path.unlink(missing_ok=True)
                args = [self.divine_file, "-a", "create-package", "-g", "bg3",
                        "-s", str(file), "-d", str(pak_path), "-l", "debug" if DEBUG else "info"]
                result = subprocess.run(args, creationflags=subprocess.CREATE_NO_WINDOW, check=not DEBUG,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                if result.returncode != 0:
                    qWarning(f"{' '.join([self.divine_file, "-a", "create-package", "-g", "bg3",
                        "-s", f'"{file}"',
                                          "-d", f'"{pak_path}"',
                                          "-l", "trace" if DEBUG else "info"]).replace(str(Path.home()), '~', 1).replace(str(Path.home()), '$HOME')} returned {str(result.stdout)}, code {result.returncode}")
                    return ''
                meta_file = list(file.glob("Mods/*/meta.lsx"))
                return metadata_to_ini(len(meta_file) > 0, lambda: meta_file[0])
            else:
                # qDebug(f"non packable dir, unlikely to be used by the game: {file}")
                return ''
        except Exception:
            qWarning(traceback.format_exc())
            return ''
