import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Set, List, Tuple, Optional, Union

SerializableType = Union[None, bool, str, int, float, Dict[str, Any], List[Any]]
EmbeddedRstParser = Callable[[str, int, bool], List[SerializableType]]
LEVEL_ERROR = 1
LEVEL_WARNING = 2


@dataclass
class Diagnostic:
    __slots__ = ('message', 'severity', 'start', 'end')

    severity: int
    message: str
    start: Tuple[int, int]
    end: Tuple[int, int]

    @classmethod
    def create(cls, severity: int, message: str,
               start: Union[int, Tuple[int, int]],
               end: Union[None, int, Tuple[int, int]] = None) -> 'Diagnostic':
        if isinstance(start, int):
            start_line, start_column = start, 0
        else:
            start_line, start_column = start

        if end is None:
            end_line, end_column = start_line, 1000
        elif isinstance(end, int):
            end_line, end_column = end, 1000
        else:
            end_line, end_column = end

        return cls(LEVEL_WARNING, message, (start_line, start_column), (end_line, end_column))

    @classmethod
    def warning(cls, message: str,
                start: Union[int, Tuple[int, int]],
                end: Union[None, int, Tuple[int, int]] = None) -> 'Diagnostic':
        return cls.create(LEVEL_WARNING, message, start, end)

    @classmethod
    def error(cls, message: str,
              start: Union[int, Tuple[int, int]],
              end: Union[None, int, Tuple[int, int]] = None) -> 'Diagnostic':
        return cls.create(LEVEL_ERROR, message, start, end)


@dataclass
class StaticAsset:
    __slots__ = ('fileid', 'checksum', 'data')

    fileid: str
    checksum: str
    data: Optional[bytes]

    def __hash__(self) -> int:
        return hash(self.checksum)

    @classmethod
    def load(cls, fileid: str, path: str) -> 'StaticAsset':
        with open(path, 'rb') as f:
            data = f.read()
        asset_hash = hashlib.blake2b(data, digest_size=32).hexdigest()
        return StaticAsset(fileid, asset_hash, data)


@dataclass
class Page:
    __slots__ = ('path', 'source', 'ast', 'diagnostics', 'static_assets')

    path: str
    source: str
    ast: SerializableType
    diagnostics: List[Diagnostic]

    static_assets: Set[StaticAsset]
