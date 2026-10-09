import mobase

from .check_for_lslib_updates_plugin import BG3ToolCheckForLsLibUpdates
from .convert_jsons_to_yaml_plugin import BG3ToolConvertJsonsToYaml
from .extract_pak_plugin import BG3ToolExtractPak
from .reparse_pak_metadata_plugin import BG3ToolReparsePakMetadata


def createPlugins() -> list[mobase.IPluginTool]:
    return [
        BG3ToolCheckForLsLibUpdates(),
        BG3ToolReparsePakMetadata(),
        BG3ToolConvertJsonsToYaml(),
        BG3ToolExtractPak(),
    ]
