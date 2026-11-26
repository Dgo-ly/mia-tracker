"""Deterministic supplier-document statuses.

This module contains rules only: no source files, credentials, supplier names,
network calls, or I/O at import time. The caller supplies verified source flags
and whether a comment belongs to an earlier reporting cut.

The legacy keyword order is preserved, with stale comments placed after
negative/problem keywords and before positive/work keywords, as observed in
the historical outputs. ``explicit_missing_documents`` is an opt-in extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Union

Money = Union[int, float, str, Decimal]

PROBLEM = (
    "неверн", "нет доков", "доков нет", "нет в эдо", "нигде нет",
    "не выходит на связь", "не могу найти", "суды", "не закроем",
    "нет такой суммы", "такой суммы платежа нет", "не подписан",
    "протокол разногласий", "расторгнут", "переделать", "не знаю",
    "нет оплат", "по ошибке", "недействующ", "прекратил",
    "рано подписывать", "нет операций", "в сбисе нет", "нет понимания",
    "игнор", "пропал", "кто это", "документов нет", "нет документов",
    "в эдо нет",
)
PARTIAL = (
    "до июля", "до июня", "кроме", "часть подписана", "часть доков",
    "не подписан только", "не нашла только", "не разнесен",
)
ADVANCE = ("переплат", "аванс", "возврат", "вернуть", "списать", "выровняется")
CLOSED = (
    "завершено", "все в эдо", "все есть в эдо", "подписали", "подписано",
    "подписаны", "подписан", "вышли в ноль", "все загружено",
    "загрузила всё", "загрузила все", "полностью закрыли",
    "закрыты по эдо", "все закрывашки",
)
SENT = (
    "отправлено", "отправлены", "отправил", "скинули", "скинул", "скинет",
    "выслано", "загрузила", "в чат", "акт-сверки отправлен",
    "акт сверки отправлен", "битрикс", "б24", "ждем подпис", "эдо подпись",
    "закрывашка отправлена", ".pdf", ".jpg", "в эдо", "сбис", "эдо",
    "диадок", "счет-фактуры", "контрагенты",
)
REQUESTED = (
    "запрош", "запросил", "запросить", "жду", "ждем", "ищу", "искать",
    "найти", "собирает", "делают", "закроем", "написала", "написал",
    "связал", "уточня", "проверить", "узнать", "предоставит", "ожидае",
    "вышлет", "попросила", "пробую",
)
# The trailing space in "бух " is significant. Removing it changes matching.
DEPARTMENT = ("маркетинг", "зтл", "хозка", "кц", "бухгалтер", "бух ", "ук", "окк", "айти", "ахч")

ATTENTION_STATUSES = frozenset({
    "Проблема", "Нет комментария", "Коммент. устарел — сумма выросла",
})


@dataclass(frozen=True)
class SourceFlags:
    """Facts extracted from the corresponding source, never inferred from age.

    ``all_upd_signed`` requires at least one UPD and every UPD signed; an empty
    list must not set it. Source-specific flags belong to their named clinic.
    ``remainder`` also applies when the advance row has no green fill.
    """

    pre_acquisition: bool = False
    uploaded_to_accounting: bool = False
    all_upd_signed: bool = False
    unsigned_upd: bool = False
    green_advance_row: bool = False
    remainder: bool = False


@dataclass(frozen=True)
class Decision:
    status: str
    rule: str
    matched: str = ""
    enhancement: bool = False


def normalize_text(comment: str | None) -> str:
    if comment is None:
        return ""
    if not isinstance(comment, str):
        raise TypeError("comment must be text or None")
    return comment.casefold().replace("ё", "е").replace("\u00a0", " ").replace("\u200b", "").strip()


def _money(value: Money | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("amount must be a number, not a checkbox")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid numeric amount") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount


def _match(text: str, keywords: tuple[str, ...]) -> str:
    return next((word for word in keywords if word.replace("ё", "е") in text), "")


# Deliberately restricted: every nonempty line must identify a numbered
# document and finish with an explicit unresolved statement. Extra narrative,
# "только", a later resolution, and generic "не нашла" do not match.
_MISSING_DOCUMENT_LINE = re.compile(
    r"^(?:\d+[.)]\s*)?документ\s*№\s*[\w/.-]+[^\r\n]{0,220}?"
    r"[-–—]\s*не\s+наш(?:ла|ел|ли)\s*[.!]?\s*$"
)


def is_explicit_missing_document_list(comment: str | None) -> bool:
    text = normalize_text(comment)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_MISSING_DOCUMENT_LINE.fullmatch(line) for line in lines)


def classify_status(
    comment: str | None,
    *,
    paid: Money | None = None,
    debt: Money | None = None,
    previous_paid: Money | None = None,
    from_previous_cut: bool = False,
    clinic_kind: str = "generic",
    flags: SourceFlags | None = None,
    explicit_missing_documents: bool = False,
) -> Decision:
    """Return a status with its reason; never read or mutate source data.

    ``paid``/``debt`` use the selected source's consistent monetary basis.
    Pass ``None`` to skip amount rules when classifying a comment alone.
    Do not pass an accounting balance as ``paid`` and a directors balance as
    ``previous_paid``: stale detection requires comparable reporting cuts.

    Supported source-specific ``clinic_kind`` values: ami, krd, msk, loft,
    vas, lig; generic performs keyword-only rules plus monetary priority.
    1C reconciliation statuses are owned by the caller and are not inferred
    from the absence of a comment or source record here.
    """
    if clinic_kind not in {"generic", "ami", "krd", "msk", "loft", "vas", "lig"}:
        raise ValueError("unknown clinic_kind")
    source = flags or SourceFlags()
    if source.all_upd_signed and source.unsigned_upd:
        raise ValueError("UPDs cannot be both all signed and unsigned")
    text = normalize_text(comment)
    p, d, previous = _money(paid), _money(debt), _money(previous_paid)

    if p is not None and p == 0 and d is not None and d > 0:
        return Decision("Долг перед поставщиком", "debt_without_payment")
    if p is not None and Decimal(0) < p < Decimal(1):
        return Decision("Копейки (<1 ₽)", "positive_amount_below_one")
    if clinic_kind == "krd" and source.pre_acquisition:
        return Decision("Оплата до покупки ООО", "source_pre_acquisition")
    if clinic_kind == "loft" and source.uploaded_to_accounting:
        return Decision("Документы загружены в бухгалтерию", "source_uploaded")
    if clinic_kind == "krd" and source.all_upd_signed:
        return Decision("Подписано в ЭДО", "source_all_upd_signed")
    if clinic_kind == "krd" and (source.unsigned_upd or "необходимо подписать" in text):
        return Decision("Документы есть в ЭДО — подписать", "source_upd_signature_required")
    if clinic_kind == "ami" and source.green_advance_row:
        if source.remainder:
            return Decision("Переплата — расходуется", "source_green_remainder")
        return Decision("Сверка / закрывающие есть / аванс", "source_green_advance")

    key = _match(text, PROBLEM)
    if key:
        return Decision("Проблема", "problem_keyword", key)
    if explicit_missing_documents and is_explicit_missing_document_list(text):
        return Decision("Проблема", "explicit_missing_document_list", enhancement=True)
    if text and from_previous_cut and p is not None and previous is not None and p > previous:
        return Decision("Коммент. устарел — сумма выросла", "stale_comment_increased_amount")
    key = _match(text, PARTIAL)
    if key:
        return Decision("Частично закрыто", "partial_keyword", key)
    if clinic_kind == "ami" and source.remainder:
        return Decision("Переплата / аванс", "source_remainder")
    for status, rule, keywords in (
        ("Переплата / аванс", "advance_keyword", ADVANCE),
        ("Закрыто", "closed_keyword", CLOSED),
        ("Отправлено / на проверке", "sent_keyword", SENT),
        ("Запрошено", "requested_keyword", REQUESTED),
        ("Передано в другой отдел", "department_keyword", DEPARTMENT),
    ):
        key = _match(text, keywords)
        if key:
            return Decision(status, rule, key)
    return Decision("В работе" if text else "Нет комментария", "comment_fallback" if text else "empty_comment")
