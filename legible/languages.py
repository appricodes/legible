"""Canonical language selection shared by all OCR engines.

Each engine has its own language taxonomy:
  * PaddleOCR uses mostly ISO-639-1-ish codes ("de", "en", ...), but a few
    languages are special-cased ("japan", "korean", "ch" - not "ja"/"ko"/"zh").
  * RapidOCR selects a *script family* recognition model via its LangRec enum
    ("latin" covers German/French/Spanish/...; "en" and a handful of others
    get their own dedicated model instead of falling into a family).
  * Tesseract uses per-language ISO 639-2 codes tied to separately-installed
    <code>.traineddata files ("deu", "eng", ...) - one real model per
    language, not a family.

This module is the one place that maps a single user-facing language choice
to each engine's own code, so Settings has one "Preferred language" dropdown
instead of three engine-specific ones. Values were verified against each
engine's real, installed source (PPStructureV3's LATIN_LANGS/ESLAV_LANGS/...
tables, RapidOCR's LangRec enum, and Tesseract's standard tessdata naming).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class Language:
    code: str  # canonical id, stored in AppSettings.ocr_lang
    display_name: str
    paddle_lang: str  # PPStructureV3(lang=...)
    rapid_lang_type: str  # RapidOCR LangRec member value (Rec.lang_type)
    tesseract_lang: str  # pytesseract lang=; needs a matching .traineddata


_LANGUAGES: List[Language] = [
    Language("de", "German", "de", "latin", "deu"),
    Language("en", "English", "en", "en", "eng"),
    Language("fr", "French", "fr", "latin", "fra"),
    Language("es", "Spanish", "es", "latin", "spa"),
    Language("it", "Italian", "it", "latin", "ita"),
    Language("nl", "Dutch", "nl", "latin", "nld"),
    Language("pt", "Portuguese", "pt", "latin", "por"),
    Language("pl", "Polish", "pl", "latin", "pol"),
    Language("cs", "Czech", "cs", "latin", "ces"),
    Language("sv", "Swedish", "sv", "latin", "swe"),
    Language("da", "Danish", "da", "latin", "dan"),
    Language("tr", "Turkish", "tr", "latin", "tur"),
    Language("vi", "Vietnamese", "vi", "latin", "vie"),
    Language("ru", "Russian", "ru", "eslav", "rus"),
    Language("uk", "Ukrainian", "uk", "eslav", "ukr"),
    Language("ar", "Arabic", "ar", "arabic", "ara"),
    Language("el", "Greek", "el", "el", "ell"),
    Language("th", "Thai", "th", "th", "tha"),
    Language("hi", "Hindi", "hi", "devanagari", "hin"),
    Language("ja", "Japanese", "japan", "japan", "jpn"),
    Language("ko", "Korean", "korean", "korean", "kor"),
    Language("ch", "Chinese (Simplified)", "ch", "ch", "chi_sim"),
]

_BY_CODE: Dict[str, Language] = {lang.code: lang for lang in _LANGUAGES}

DEFAULT_LANGUAGE = "de"


def all_languages() -> List[Language]:
    return list(_LANGUAGES)


def get(code: str) -> Language:
    """Look up a language by its canonical code, falling back to the default
    (German) for an unrecognised code rather than raising - settings.json
    could in principle hold a stale/invalid value from an older version."""
    return _BY_CODE.get(code, _BY_CODE[DEFAULT_LANGUAGE])
