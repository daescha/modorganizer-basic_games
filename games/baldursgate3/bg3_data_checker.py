from pathlib import Path
from typing import Callable, TypeGuard

import mobase

from ...basic_features import BasicModDataChecker, GlobPatterns, utils
from . import bg3_utils


def _paks(node: mobase.IFileTree) -> list[mobase.FileTreeEntry]:
    found: list[mobase.FileTreeEntry] = []
    for e in node:
        if is_dir(e):
            found += _paks(e)
        elif e.name().casefold().endswith(".pak"):
            found.append(e)
    return found


_known_folders = {
    f.casefold()
    for f in ["Mods", "Data", "bin", "Script Extender", "Root"]
    + list(bg3_utils.loose_file_folders)
}


def pak_variants(filetree: mobase.IFileTree) -> dict[str, mobase.IFileTree]:
    """Map folder name to folder for sibling folders that each hold exactly one pak."""
    node = filetree
    while len(entries := list(node)) == 1 and is_dir(entries[0]):
        node = entries[0]
    groups: dict[str, mobase.IFileTree] = {}
    for e in node:
        if not is_dir(e):
            if e.name().casefold().endswith(".pak"):
                return {}
        elif paks := _paks(e):
            if len(paks) != 1 or e.name().casefold() in _known_folders:
                return {}
            groups[e.name()] = e
    return groups if len(groups) > 1 else {}


def choose_variant_dialog(names: list[str]) -> str | None:
    from PyQt6.QtWidgets import QApplication, QInputDialog

    name, ok = QInputDialog.getItem(
        QApplication.activeWindow(),
        "Baldur's Gate 3: choose a variant",
        "This archive holds alternative versions of one mod. Keep which one?\n"
        "Cancel installs all of them.",
        names,
        0,
        False,
    )
    return name if ok else None


def mods_dir(filetree: mobase.IFileTree) -> mobase.IFileTree | None:
    return next(
        (e for e in filetree if is_dir(e) and e.name().casefold() == "mods"), None
    )


def is_dir(entry: mobase.FileTreeEntry) -> TypeGuard[mobase.IFileTree]:
    return isinstance(entry, mobase.IFileTree)


class BG3ModDataChecker(BasicModDataChecker):
    def __init__(
        self,
        choose_variant: Callable[[list[str]], str | None] = choose_variant_dialog,
    ):
        self._choose_variant = choose_variant
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
        if pak_variants(filetree):
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
        if variants := pak_variants(filetree):
            chosen = self._choose_variant(sorted(variants))
            if chosen in variants:
                for name, folder in variants.items():
                    if name != chosen:
                        folder.detach()
                folder = variants[chosen]
                wrapper = folder.parent()
                filetree.merge(folder)
                folder.detach()
                while wrapper is not None and wrapper.parent() is not None:
                    up = wrapper.parent()
                    if not list(wrapper):
                        wrapper.detach()
                    wrapper = up
        mods = mods_dir(filetree)
        if mods is None or any(is_dir(e) for e in mods):
            return super().fix(filetree)
        mods.detach()
        super().fix(filetree)
        filetree.insert(mods)
        return filetree
