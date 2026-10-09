from pathlib import Path
from typing import TypeGuard

import mobase

from ...basic_features import BasicModDataChecker, GlobPatterns, utils
from . import bg3_utils


def nested_mod_paks(
    filetree: mobase.IFileTree,
) -> tuple[list[mobase.FileTreeEntry], list[mobase.IFileTree], bool]:
    """Return paks in Mods/ subfolders, those subfolders, and whether moving the paks up is clash-free."""
    mods = mods_dir(filetree)
    if mods is None:
        return [], [], True
    paks: list[mobase.FileTreeEntry] = []
    subdirs: list[mobase.IFileTree] = []

    def walk(node: mobase.IFileTree) -> None:
        for e in node:
            if is_dir(e):
                walk(e)
            elif e.name().casefold().endswith(".pak"):
                paks.append(e)

    for e in mods:
        if is_dir(e):
            before = len(paks)
            walk(e)
            if len(paks) > before:
                subdirs.append(e)
    names = [e.name().casefold() for e in mods if not is_dir(e)]
    names += [p.name().casefold() for p in paks]
    return paks, subdirs, len(names) == len(set(names))


def mods_dir(filetree: mobase.IFileTree) -> mobase.IFileTree | None:
    return next(
        (e for e in filetree if is_dir(e) and e.name().casefold() == "mods"), None
    )


def is_dir(entry: mobase.FileTreeEntry) -> TypeGuard[mobase.IFileTree]:
    return isinstance(entry, mobase.IFileTree)


class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                valid=[
                    "*.pak",
                    str(Path("Mods") / "*.pak"),  # standard mods
                    "bin",  # native mods / Script Extender
                    "Script Extender",  # mods which are configured via jsons in this folder
                    "Data",  # loose file mods
                ]
                + [str(Path("*") / f) for f in bg3_utils.loose_file_folders],
                move={
                    "Root/": "",  # root builder not needed
                    "*.dll": "bin/",
                    "ScriptExtenderSettings.json": "bin/",
                }
                | {f: "Data/" for f in bg3_utils.loose_file_folders},
                delete=["info.json", "*.txt"],
            )
        )

    def dataLooksValid(
        self, filetree: mobase.IFileTree
    ) -> mobase.ModDataChecker.CheckReturn:
        invalid, valid, fixable = (
            mobase.ModDataChecker.INVALID,
            mobase.ModDataChecker.VALID,
            mobase.ModDataChecker.FIXABLE,
        )
        rank = (invalid, valid, fixable).index
        status = invalid
        paks, _, movable = nested_mod_paks(filetree)
        if paks:
            if not movable:
                return invalid
            status = fixable
        rp = self._regex_patterns
        for entry in filetree:
            name = entry.name().casefold()
            if rp.unfold.match(name):
                if utils.is_directory(entry):
                    status = max(status, self.dataLooksValid(entry), key=rank)
                else:
                    status = invalid
                    break
            elif rp.valid.match(name):
                status = max(status, valid, key=rank)
            elif isinstance(entry, mobase.IFileTree):
                if all(rp.valid.match(e.pathFrom(filetree)) for e in entry):
                    status = max(status, valid, key=rank)
            elif rp.delete.match(name) or rp.move_match(name) is not None:
                status = fixable
            else:
                status = invalid
                break
        return status

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree:
        paks, subdirs, _ = nested_mod_paks(filetree)
        for pak in paks:
            filetree.move(pak, "Mods/")
        for subdir in subdirs:
            subdir.detach()
        mods = mods_dir(filetree)
        if mods is None or any(is_dir(e) for e in mods):
            return super().fix(filetree)
        mods.detach()
        super().fix(filetree)
        filetree.insert(mods)
        return filetree
