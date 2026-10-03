"""Формы одного имени (Дмитрий = Дима = Дим = Dmitry = Dmytro) — данные и чистая функция.

Одиночное имя в тексте («Дима написал…») указывает на человека только внутри круга
разговора, а круг называет его любой формой: в графе «Dima Zaporozhets Dmytro», в
чате «Дим». Группа задаёт, какие слова считать одним именем; слова сравниваются по
`dupe_keys.word_key` (транслит и падежи уже сняты).
"""
from __future__ import annotations

from vera_shared.graph.dupe_keys import word_key

_GROUPS = (
    "дмитрий дмитро дима дим митя дімa діма dmitry dmitri dmytro dima dim",
    "александр олександр саша саня шура alexander alex oleksandr sasha sanya",
    "алексей олексій алёша леша alexey aleksey alexei oleksii lesha",
    "андрей андрій андрюша andrey andrei andrii andrew",
    "анастасия настя анастасія nastya anastasia anastasiia",
    "анна аня анна anna anya",
    "артем артём тема артем artem artyom",
    "владимир володимир вова володя vladimir volodymyr vova volodya",
    "виктор віктор витя viktor victor vitya",
    "евгений євген женя eugene evgeny yevhen zhenya",
    "екатерина катя катерина ekaterina kate katya kateryna",
    "елена лена олена elena helen lena olena",
    "иван іван ваня ivan vanya",
    "игорь ігор igor ihor",
    "ирина ира ірина irina ira iryna",
    "максим макс maxim maksim max",
    "михаил михайло миша mikhail mykhailo misha michael",
    "николай микола коля nikolay nikolai mykola kolya",
    "ольга оля olga olya",
    "павел павло паша pavel pavlo pasha paul",
    "сергей сергій серёжа сережа серега sergey sergei serhii seryozha",
    "татьяна тетяна таня tatiana tatyana tetiana tanya",
    "юлия юлія юля yulia julia yuliia yulya",
    "наталья наталія наташа natalia natalya natasha nataliia",
    "мария марія маша maria mariia masha",
    "роман рома roman roma",
    "денис дэн denis den",
    "кирилл кирило kirill kyrylo",
    "вадим vadim",
    "олег oleg",
    "виктория вікторія вика victoria vika",
    "валерия лера valeria lera",
)

_FORM_TO_GROUP: dict[str, int] = {}
for _gid, _group in enumerate(_GROUPS):
    for _form in _group.split():
        _FORM_TO_GROUP.setdefault(word_key(_form), _gid)


def name_group(key: str) -> int | None:
    """Группа имени по ключу слова (`word_key`); None — слово не из таблицы."""
    return _FORM_TO_GROUP.get(key)
