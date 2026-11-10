"""Read LIRA PercentLS .rez files without calculating or rounding their values.

Port of LiraRez.cs: assigned-section checks drive group maxima; selected-section
percentages are retained separately. Numeric comparisons intentionally use doubles.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

NO_DATA = "[нет данных]"
MAX_BYTES = 50 * 1024 * 1024
SERVICE_VALUE = 1e20
MEASURES = ("1LS", "2LS", "LB")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_ELEMENT_LINE = re.compile(r"^\s*(\d+)\s*=\s*(.*)$")
_SECTION_LINE = re.compile(r"^\s*(\d+)\s*=(.*)$")
_NAME_FIELD = re.compile(r"\bName\s*=\s*\|([^|]*)\|")
_NAME_PREFIX = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\.\s+(.*)$")
_INT32_MAX = 2**31 - 1


class RezError(ValueError):
    """A malformed or unsupported steel result file."""


class RezMissing(FileNotFoundError):
    """No such file: reads as its message alone, with the path in ``filename``, as .NET's
    FileNotFoundException(message, fileName) does."""

    def __init__(self, message: str, filename: str) -> None:
        super().__init__(message)
        self.filename = filename

    def __str__(self) -> str:
        return str(self.args[0])


@dataclass
class RezSection:
    number: int
    name: str | None = None
    title: str | None = None
    stiffness: int | None = None
    material: int | None = None
    extra: int | None = None


@dataclass
class RezElement:
    number: int
    line: int
    sel: int
    asn: int
    sel_pct: list[str]
    asn_pct: list[str]


@dataclass
class RezFile:
    path: str
    sha256: str
    encoding_name: str
    general: dict[str, str] = field(default_factory=dict)
    assigned: dict[int, RezSection] = field(default_factory=dict)
    elements: dict[int, RezElement] = field(default_factory=dict)


@dataclass
class RezMax:
    text: str | None = None
    display: str = NO_DATA
    element: int = 0
    numeric: int = 0
    no_result: int = 0


@dataclass
class RezGroup:
    elements: list[int] = field(default_factory=list)
    uls: RezMax = field(default_factory=RezMax)
    sls: RezMax = field(default_factory=RezMax)
    local: RezMax = field(default_factory=RezMax)
    warnings: list[str] = field(default_factory=list)


def decode(data: bytes) -> tuple[str, str]:
    """Strict UTF-8 (including BOM), otherwise Windows-1251, as in LiraRez."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # .NET's Windows-1251 maps the undefined byte 0x98 to the C1 control U+0098.
        text = data.decode("cp1251", errors="replace").replace("\ufffd", "\x98")
        return text, "windows-1251"


def _bad(path: Path, line: int, what: str) -> RezError:
    where = f", строка {line}" if line > 0 else ""
    return RezError(f"{path.name}{where}: {what}")


def _int32(text: str) -> int:
    value = int(text)
    if value > _INT32_MAX:
        raise OverflowError("Value was either too large or too small for an Int32.")
    return value


def _section(file: RezFile, path: Path, line: str, no: int) -> None:
    match = _SECTION_LINE.match(line)
    if match is None:
        raise _bad(path, no, "неверная строка [Assigned Sections]")
    number = _int32(match[1])
    if number in file.assigned:
        raise _bad(path, no, f"повтор назначенного сечения {number}")
    section = RezSection(number)
    name = _NAME_FIELD.search(match[2])
    if name:
        section.name = name[1]
        prefix = _NAME_PREFIX.match(section.name)
        if prefix:
            section.stiffness, section.material, section.extra = (
                _int32(prefix[i]) for i in (1, 2, 3)
            )
            section.title = prefix[4].strip()
    file.assigned[number] = section


def _section_number(text: str) -> int | None:
    # NumberStyles.None accepts ASCII digits only, with neither sign nor whitespace.
    if not re.fullmatch(r"[0-9]+", text):
        return None
    value = int(text)
    return value if value <= _INT32_MAX else None


def _element(file: RezFile, path: Path, line: str, no: int) -> None:
    match = _ELEMENT_LINE.match(line)
    if match is None:
        raise _bad(path, no, "неверная строка [Elements]")
    fields = [part.strip() for part in match[2].split(",")]
    if len(fields) != 10:
        raise _bad(path, no, f"в строке [Elements] {len(fields)} полей вместо 10")
    number = _int32(match[1])
    if number in file.elements:
        raise _bad(path, no, f"повтор элемента {number}")
    selected = _section_number(fields[0])
    if fields[0] != "NR" and selected is None:
        raise _bad(path, no, f"номер подобранного сечения: {fields[0]}")
    assigned = _section_number(fields[1])
    if assigned is None:
        raise _bad(path, no, f"номер назначенного сечения: {fields[1]}")
    for text in fields[2:]:
        if text != "NR" and not _NUMBER.fullmatch(text):
            raise _bad(path, no, f"нечисловое значение «{text}»")
    file.elements[number] = RezElement(
        number, no, selected or 0, assigned, fields[2:5], fields[5:8]
    )


def load(path: str | Path) -> RezFile:
    """Load and validate a .rez file, retaining its original numeric strings."""
    path = Path(path)
    if not path.is_file():
        # As .NET FileNotFoundException(message, fileName): the message, the path aside
        raise RezMissing("Нет файла результатов ЛИРЫ", str(path))
    if path.stat().st_size > MAX_BYTES:
        raise _bad(path, 0, "файл больше 50 МБ")
    data = path.read_bytes()
    text, encoding = decode(data)
    file = RezFile(str(path.resolve()), hashlib.sha256(data).hexdigest().upper(), encoding)
    section = None
    seen = set()
    for no, line in enumerate(re.split(r"\r\n|\n", text), 1):
        if line.startswith("["):
            if not line.endswith("]"):
                raise _bad(path, no, "незакрытый заголовок секции")
            section = line[1:-1]
            if section in seen:
                raise _bad(path, no, f"повтор секции [{section}]")
            seen.add(section)
            continue
        if section is None or not line.strip() or line.lstrip().startswith(";"):
            continue
        if section == "General":
            eq = line.find("=")
            if eq <= 0:
                raise _bad(path, no, "строка [General] без '='")
            file.general[line[:eq].strip()] = line[eq + 1 :].strip()
        elif section == "Assigned Sections":
            _section(file, path, line, no)
        elif section == "Elements":
            _element(file, path, line, no)
    if "General" not in seen:
        raise _bad(path, 0, "нет секции [General]")
    mode = file.general.get("ResultMode", "нет")
    if mode != "PercentLS":
        raise _bad(
            path, 0, "ResultMode должен быть PercentLS (проценты использования), в файле: " + mode
        )
    if "Elements" not in seen or not file.elements:
        raise _bad(path, 0, "нет строк в секции [Elements]")
    if "Assigned Sections" not in seen:
        raise _bad(path, 0, "нет секции [Assigned Sections]")
    file.elements = dict(sorted(file.elements.items()))
    file.assigned = dict(sorted(file.assigned.items()))
    for element in file.elements.values():
        if element.asn not in file.assigned:
            raise _bad(
                path,
                element.line,
                f"элемент {element.number} ссылается на отсутствующее назначенное сечение "
                f"{element.asn}",
            )
    return file


def _maximum(file: RezFile, elements: list[int], measure: int) -> RezMax:
    result = RezMax()
    best = 0.0
    for number in elements:
        text = file.elements[number].asn_pct[measure]
        # Parsing accepts Unicode digits, but .NET Double.TryParse does not.
        value = float(text) if text != "NR" and text.isascii() else SERVICE_VALUE
        if value >= SERVICE_VALUE:
            result.no_result += 1
            continue
        result.numeric += 1
        if result.text is None or value > best:
            best, result.text, result.element = value, text, number
    if result.text is not None:
        result.display = result.text.replace(".", ",")
    return result


def summarize(
    file: RezFile, stiffness: Iterable[int] | None = None, elements: Iterable[int] | None = None
) -> RezGroup:
    """Union of assigned-section stiffness matches and explicitly requested elements."""
    group = RezGroup()
    want = dict.fromkeys(() if stiffness is None else stiffness)
    for number in want:
        if not any(s.stiffness == number for s in file.assigned.values()):
            group.warnings.append(f"жёсткость {number} не найдена среди назначенных сечений")
    if want and any(s.stiffness is None for s in file.assigned.values()):
        group.warnings.append(
            "у части назначенных сечений нет номера жёсткости в имени; "
            "их элементы можно указать только списком"
        )
    selected = {
        e.number
        for e in file.elements.values()
        if (file.assigned[e.asn].stiffness if file.assigned[e.asn].stiffness is not None else -1)
        in want
    }
    for number in () if elements is None else elements:
        if number in file.elements:
            selected.add(number)
        else:
            group.warnings.append(f"элемента {number} нет в [Elements]")
    group.elements = sorted(selected)
    if not group.elements:
        group.warnings.append("группа не содержит ни одного элемента")
    group.uls, group.sls, group.local = (
        _maximum(file, group.elements, measure) for measure in range(3)
    )
    return group


def changed(file: RezFile, stored_sha256: str | None) -> bool:
    """Whether a nonempty saved checksum differs from the newly loaded file."""
    return bool(stored_sha256) and file.sha256.lower() != stored_sha256.lower()
