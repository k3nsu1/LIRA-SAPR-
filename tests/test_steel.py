"""Contract tests for the LiraRez.cs PercentLS parser port, using synthetic files."""

import hashlib
from dataclasses import asdict

import pytest

from lira_sapr_mcp.postprocessing import steel

SECTION = "1=Name=|12.3.4. Двутавр 20 |"
ELEMENT = "7=NR,1,91.1200,2,3,70.1234567890123456789,20.00,NR,0,NR"


def document(*, section=SECTION, elements=ELEMENT, mode="PercentLS"):
    return f"[General]\nResultMode={mode}\n[Assigned Sections]\n{section}\n[Elements]\n{elements}\n"


def write(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "steel.rez"
    path.write_bytes(text.encode(encoding))
    return path


@pytest.mark.parametrize(
    "encoding,label",
    [
        ("utf-8", "utf-8"),
        ("utf-8-sig", "utf-8"),
        ("cp1251", "windows-1251"),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_load_encodings_preserves_every_numeric_character(tmp_path, encoding, label, newline):
    path = write(tmp_path, document().replace("\n", newline), encoding)
    file = steel.load(path)
    assert file.path == str(path.resolve())
    assert file.encoding_name == label
    assert file.sha256 == hashlib.sha256(path.read_bytes()).hexdigest().upper()
    section = file.assigned[1]
    assert asdict(section) == {
        "number": 1,
        "name": "12.3.4. Двутавр 20 ",
        "title": "Двутавр 20",
        "stiffness": 12,
        "material": 3,
        "extra": 4,
    }
    element = file.elements[7]
    assert element.line == 6 and element.sel == 0 and element.asn == 1
    assert element.sel_pct == ["91.1200", "2", "3"]
    assert element.asn_pct == ["70.1234567890123456789", "20.00", "NR"]
    group = steel.summarize(file, [12])
    assert group.elements == [7] and group.warnings == []
    assert asdict(group.uls) == {
        "text": "70.1234567890123456789",
        "display": "70,1234567890123456789",
        "element": 7,
        "numeric": 1,
        "no_result": 0,
    }
    assert group.sls.text == "20.00" and group.sls.display == "20,00"
    assert group.local == steel.RezMax(no_result=1)


def test_encoding_fallback_and_strict_bom():
    assert steel.decode(b"\xff\x98") == ("я\x98", "windows-1251")
    with pytest.raises(UnicodeDecodeError):
        steel.decode(b"\xef\xbb\xbf\xff")


@pytest.mark.parametrize(
    "text,line,message",
    [
        ("", 0, "нет секции [General]"),
        ("[General", 1, "незакрытый заголовок секции"),
        ("[General] ", 1, "незакрытый заголовок секции"),
        ("[General]\n[General]", 2, "повтор секции [General]"),
        ("[Unknown]\n[Unknown]", 2, "повтор секции [Unknown]"),
        ("[General]\nbroken", 2, "строка [General] без '='"),
        ("[General]\n=x", 2, "строка [General] без '='"),
        ("[General]", 0, "ResultMode должен быть PercentLS (проценты использования), в файле: нет"),
        (
            document(mode="Stress"),
            0,
            "ResultMode должен быть PercentLS (проценты использования), в файле: Stress",
        ),
        ("[General]\nResultMode=PercentLS", 0, "нет строк в секции [Elements]"),
        (document(elements=""), 0, "нет строк в секции [Elements]"),
        (
            f"[General]\nResultMode=PercentLS\n[Elements]\n{ELEMENT}",
            0,
            "нет секции [Assigned Sections]",
        ),
        (document(section=""), 6, "элемент 7 ссылается на отсутствующее назначенное сечение 1"),
        (document(section="broken"), 4, "неверная строка [Assigned Sections]"),
        (document(section=SECTION + "\n" + SECTION), 5, "повтор назначенного сечения 1"),
        (document(elements="broken"), 6, "неверная строка [Elements]"),
        (document(elements=ELEMENT + "\n" + ELEMENT), 7, "повтор элемента 7"),
        (document(elements="7=1,1"), 6, "в строке [Elements] 2 полей вместо 10"),
        (document(elements=ELEMENT + ",0"), 6, "в строке [Elements] 11 полей вместо 10"),
    ],
)
def test_validation_errors_match_source(tmp_path, text, line, message):
    path = write(tmp_path, text)
    with pytest.raises(steel.RezError) as error:
        steel.load(path)
    location = f", строка {line}" if line else ""
    assert str(error.value) == f"steel.rez{location}: {message}"


@pytest.mark.parametrize(
    "index,value,message",
    [
        (0, "-1", "номер подобранного сечения: -1"),
        (0, "+1", "номер подобранного сечения: +1"),
        (0, "2147483648", "номер подобранного сечения: 2147483648"),
        (1, "NR", "номер назначенного сечения: NR"),
        (1, "1.0", "номер назначенного сечения: 1.0"),
        (1, "2147483648", "номер назначенного сечения: 2147483648"),
        (2, "1e20", "нечисловое значение «1e20»"),
        (3, "+1", "нечисловое значение «+1»"),
        (4, ".5", "нечисловое значение «.5»"),
        (5, "5.", "нечисловое значение «5.»"),
        (6, "NaN", "нечисловое значение «NaN»"),
        (7, "Infinity", "нечисловое значение «Infinity»"),
        (8, "", "нечисловое значение «»"),
        (9, "nr", "нечисловое значение «nr»"),
    ],
)
def test_all_fields_are_validated_including_unused_tail(tmp_path, index, value, message):
    fields = ELEMENT.split("=", 1)[1].split(",")
    fields[index] = value
    path = write(tmp_path, document(elements="7=" + ",".join(fields)))
    with pytest.raises(steel.RezError) as error:
        steel.load(path)
    assert str(error.value) == "steel.rez, строка 6: " + message


def test_comments_unknown_sections_and_general_duplicate_keys(tmp_path):
    text = "preamble ignored\n; comment\n" + document()
    text = text.replace(
        "ResultMode=PercentLS", "ResultMode=wrong\n ;comment\n ResultMode = PercentLS"
    )
    text += "[Selected Sections]\nignored data\n[Other]\nanything\n"
    file = steel.load(write(tmp_path, text))
    assert file.general == {"ResultMode": "PercentLS"}
    assert list(file.elements) == [7]


def test_group_union_warnings_sentinel_and_ties(tmp_path):
    sections = SECTION + "\n2=Name=|Без номера|\n3=Other=|x|\n4=Name=|99.1.0. Angle|"
    rows = [
        "9=4,1,999,999,999,50.000,100000000000000000000,NR,0,0",
        "2=4,1,999,999,999,50.0,NR,-12.5,0,0",
        "4=NR,2,0,0,0,49,0.000,-2.00,0,0",
        "1=NR,4,0,0,0,9999,9999,9999,0,0",
    ]
    file = steel.load(write(tmp_path, document(section=sections, elements="\n".join(rows))))
    assert list(file.elements) == [1, 2, 4, 9]
    group = steel.summarize(file, [12, 12, 123], [4, 4, 404])
    assert group.elements == [2, 4, 9]
    assert group.warnings == [
        "жёсткость 123 не найдена среди назначенных сечений",
        "у части назначенных сечений нет номера жёсткости в имени; "
        "их элементы можно указать только списком",
        "элемента 404 нет в [Elements]",
    ]
    assert group.uls == steel.RezMax("50.0", "50,0", 2, 3, 0)
    assert group.sls == steel.RezMax("0.000", "0,000", 4, 1, 2)
    assert group.local == steel.RezMax("-2.00", "-2,00", 4, 2, 1)
    assert steel.summarize(file, [99]).elements == [1]  # selected section 4 is irrelevant
    assert steel.summarize(file, elements=[9]).sls == steel.RezMax(no_result=1)


def test_no_selection_is_empty_not_all_elements(tmp_path):
    file = steel.load(write(tmp_path, document()))
    group = steel.summarize(file)
    assert group.elements == []
    assert group.warnings == ["группа не содержит ни одного элемента"]
    assert group.uls == group.sls == group.local == steel.RezMax()
    assert steel.summarize(file, elements=[404, 404]).warnings == [
        "элемента 404 нет в [Elements]",
        "элемента 404 нет в [Elements]",
        "группа не содержит ни одного элемента",
    ]


def test_negative_values_and_large_sentinels(tmp_path):
    row = "7=0,1,NR,NR,NR,-100000000000000000000,100000000000000000001,-0.000,0,0"
    group = steel.summarize(steel.load(write(tmp_path, document(elements=row))), [12])
    assert group.uls.text == "-100000000000000000000"  # source uses v < 1e20, not abs(v)
    assert group.sls == steel.RezMax(no_result=1)
    assert group.local.text == "-0.000"


def test_double_comparison_preserves_original_text_on_precision_tie(tmp_path):
    rows = "9=0,1,0,0,0,70.1234567890123456789,0,0,0,0\n2=0,1,0,0,0,70.1234567890123456788,0,0,0,0"
    result = steel.summarize(steel.load(write(tmp_path, document(elements=rows))), [12])
    assert result.uls.element == 2
    assert result.uls.text == "70.1234567890123456788"


def test_change_detection_is_case_insensitive_and_no_saved_hash_is_not_changed(tmp_path):
    path = write(tmp_path, document())
    before = steel.load(path)
    assert not steel.changed(before, None)
    assert not steel.changed(before, "")
    assert not steel.changed(before, before.sha256.lower())
    path.write_bytes(path.read_bytes() + b";changed\n")
    assert steel.changed(steel.load(path), before.sha256)


def test_absent_and_oversized_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="Нет файла результатов ЛИРЫ"):
        steel.load(tmp_path / "absent.rez")
    path = tmp_path / "huge.rez"
    with path.open("wb") as stream:
        stream.truncate(steel.MAX_BYTES + 1)
    with pytest.raises(steel.RezError, match=r"huge\.rez: файл больше 50 МБ"):
        steel.load(path)
