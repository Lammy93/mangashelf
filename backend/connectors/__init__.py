from .base import SourceConnector, get_connector, get_all_connectors, register_connector
from .mangadex import MangaDexConnector
from .mangafox import MangaFoxConnector
from .weebcentral import WeebCentralConnector

register_connector(MangaDexConnector())
register_connector(MangaFoxConnector())
register_connector(WeebCentralConnector())
