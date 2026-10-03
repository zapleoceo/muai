"""Словарь слабых признаков роли (ru / uk / en / id) — данные, не правила.

Счётчики по этим регэкспам идут в пакет улик как «слабые эвристики»: модель взвешивает
их сама, ни одна роль по ним одним не выводится.
"""
from __future__ import annotations

import re

_I = re.IGNORECASE
INSTRUCTION = re.compile(
    r"\b(прош[уя]|просил[аи]?|нужно|надо|необходимо|сделай\w*|подготов(ь|ьте)\w*|отправь\w*|"
    r"давай\w*|ознаком\w+|согласу\w+|утверд\w+|проконтролир\w+|please|kindly|need you|"
    r"make sure|send me|prepare|approve|tolong|mohon|harap|segera)\b", _I)
REPORT = re.compile(
    r"\b(отправил[аи]?|сделал[аи]?|готово|подготовил[аи]?|отч[её]т\w*|доклад\w*|"
    r"выполнил[аи]?|принял[аи]?|done|sent|completed|report\w*|attached|updated|"
    r"sudah|selesai|laporan)\b", _I)
TITLE = re.compile(
    r"\b(директор\w*|руководител\w+|начальник\w*|менеджер\w*|director|head of|manager|"
    r"ceo|cto|founder|supervisor|kepala|direktur)\b", _I)
FORMAL_YOU = re.compile(r"\b(Вы|Вас|Вам|Вами|Ваш\w*)\b")
INFORMAL_YOU = re.compile(r"\b(ты|тебе|тебя|тобой|твой|твоя|твоё|твои)\b", _I)
PATRONYMIC = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ович|евич|ьич|овна|евна|ична)\b")
#: Сообщение с такими словами информативнее для выбора в пакет.
CUE = re.compile(
    INSTRUCTION.pattern + "|" + REPORT.pattern + "|" + TITLE.pattern +
    r"|\b(зарплат\w*|увол\w+|отпуск\w*|премия|штраф\w*|собеседован\w+|подчин\w+|"
    r"salary|fire[d]?|vacation|bonus|resign\w*|gaji|cuti)\b", _I)
