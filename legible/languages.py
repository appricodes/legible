"""Canonical language selection shared by both OCR backends Legible offers
(Docling always runs the pipeline; EasyOCR/RapidOCR are the two possible
text-recognition backends inside it - see legible/ocr.py).

This is EasyOCR's *complete* supported-language list (its own
easyocr/config.py: latin_lang_list + arabic_lang_list + bengali_lang_list +
cyrillic_lang_list + devanagari_lang_list + other_lang_list - 86 languages
in total, matching EasyOCR's own "80+ languages" claim), since Settings'
"Preferred language" dropdown should let you pick any language EasyOCR can
actually recognize, not just a hand-picked subset.

Each backend has its own language taxonomy:
  * EasyOCR's own codes (easyocr/config.py) are used verbatim as
    `easyocr_lang` - mostly ISO-639-1, but with EasyOCR-specific codes for
    languages/scripts without one (e.g. "rs_latin"/"rs_cyrillic" for the two
    Serbian scripts, "ch_sim"/"ch_tra" for the two Chinese scripts).
  * RapidOCR (rapidocr.utils.typings.LangRec, verified against the real
    installed package) selects a *script family* recognition model, not a
    per-language one, via `rapid_lang_type` - e.g. every Latin-script
    language here maps to the same "latin" family model. Its family list is
    much shorter than EasyOCR's per-language one, so most of the "new"
    languages below (regional Cyrillic-script minority languages, most
    Devanagari-script languages beyond Hindi, Bengali-script languages,
    Kannada, ...) have no RapidOCR equivalent at all - "" means
    unsupported (see ocr.check_ocr_backend / languages.supported_languages).

This module is the one place that maps a single user-facing language choice
to each backend's own code, so Settings has one "Preferred language"
dropdown instead of two backend-specific ones, filtered per backend by
supported_languages() - EasyOCR's list is much longer than RapidOCR's, so
switching backend in Settings changes which languages are even offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class Language:
    code: str  # canonical id, stored in AppSettings.ocr_lang - EasyOCR's own code
    display_name: str
    easyocr_lang: str  # EasyOcrOptions(lang=[...]); "" = unsupported by EasyOCR
    rapid_lang_type: str  # RapidOcrOptions(lang=[...]); "" = unsupported by RapidOCR


# Grouped to match easyocr/config.py's own lang-list groupings (traceability
# against the source of truth), sorted by display name when returned below.
_LANGUAGES: List[Language] = [
    # -- latin_lang_list (Latin-script languages; RapidOCR's "latin" family
    #    model covers all of them except English, which gets its own
    #    dedicated "en" model) --------------------------------------------
    Language("af", "Afrikaans", "af", "latin"),
    Language("az", "Azerbaijani", "az", "latin"),
    Language("bs", "Bosnian", "bs", "latin"),
    Language("cs", "Czech", "cs", "latin"),
    Language("cy", "Welsh", "cy", "latin"),
    Language("da", "Danish", "da", "latin"),
    Language("de", "German", "de", "latin"),
    Language("en", "English", "en", "en"),
    Language("es", "Spanish", "es", "latin"),
    Language("et", "Estonian", "et", "latin"),
    Language("fr", "French", "fr", "latin"),
    Language("ga", "Irish", "ga", "latin"),
    Language("hr", "Croatian", "hr", "latin"),
    Language("hu", "Hungarian", "hu", "latin"),
    Language("id", "Indonesian", "id", "latin"),
    Language("is", "Icelandic", "is", "latin"),
    Language("it", "Italian", "it", "latin"),
    Language("ku", "Kurdish (Latin)", "ku", "latin"),
    Language("la", "Latin", "la", "latin"),
    Language("lt", "Lithuanian", "lt", "latin"),
    Language("lv", "Latvian", "lv", "latin"),
    Language("mi", "Maori", "mi", "latin"),
    Language("ms", "Malay", "ms", "latin"),
    Language("mt", "Maltese", "mt", "latin"),
    Language("nl", "Dutch", "nl", "latin"),
    Language("no", "Norwegian", "no", "latin"),
    Language("oc", "Occitan", "oc", "latin"),
    Language("pi", "Pali", "pi", "latin"),
    Language("pl", "Polish", "pl", "latin"),
    Language("pt", "Portuguese", "pt", "latin"),
    Language("ro", "Romanian", "ro", "latin"),
    Language("rs_latin", "Serbian (Latin)", "rs_latin", "latin"),
    Language("sk", "Slovak", "sk", "latin"),
    Language("sl", "Slovenian", "sl", "latin"),
    Language("sq", "Albanian", "sq", "latin"),
    Language("sv", "Swedish", "sv", "latin"),
    Language("sw", "Swahili", "sw", "latin"),
    Language("tl", "Tagalog (Filipino)", "tl", "latin"),
    Language("tr", "Turkish", "tr", "latin"),
    Language("uz", "Uzbek", "uz", "latin"),
    Language("vi", "Vietnamese", "vi", "latin"),

    # -- arabic_lang_list (RapidOCR "arabic" family) ----------------------
    Language("ar", "Arabic", "ar", "arabic"),
    Language("fa", "Persian (Farsi)", "fa", "arabic"),
    Language("ug", "Uyghur", "ug", "arabic"),
    Language("ur", "Urdu", "ur", "arabic"),

    # -- bengali_lang_list (no RapidOCR equivalent script family) --------
    Language("bn", "Bengali", "bn", ""),
    Language("as", "Assamese", "as", ""),
    Language("mni", "Manipuri (Meitei)", "mni", ""),

    # -- cyrillic_lang_list (RapidOCR splits Cyrillic into "eslav" - East
    #    Slavic: Russian/Ukrainian/Belarusian - and a broader "cyrillic"
    #    family for everything else Cyrillic-script) ----------------------
    Language("ru", "Russian", "ru", "eslav"),
    Language("uk", "Ukrainian", "uk", "eslav"),
    Language("be", "Belarusian", "be", "eslav"),
    Language("rs_cyrillic", "Serbian (Cyrillic)", "rs_cyrillic", "cyrillic"),
    Language("bg", "Bulgarian", "bg", "cyrillic"),
    Language("mn", "Mongolian", "mn", "cyrillic"),
    Language("tjk", "Tajik", "tjk", "cyrillic"),
    Language("abq", "Abaza", "abq", "cyrillic"),
    Language("ady", "Adyghe", "ady", "cyrillic"),
    Language("ava", "Avar", "ava", "cyrillic"),
    Language("dar", "Dargwa", "dar", "cyrillic"),
    Language("inh", "Ingush", "inh", "cyrillic"),
    Language("che", "Chechen", "che", "cyrillic"),
    Language("kbd", "Kabardian", "kbd", "cyrillic"),
    Language("lbe", "Lak", "lbe", "cyrillic"),
    Language("lez", "Lezghian", "lez", "cyrillic"),
    Language("tab", "Tabassaran", "tab", "cyrillic"),

    # -- devanagari_lang_list (RapidOCR "devanagari" family) --------------
    Language("hi", "Hindi", "hi", "devanagari"),
    Language("mr", "Marathi", "mr", "devanagari"),
    Language("ne", "Nepali", "ne", "devanagari"),
    Language("sa", "Sanskrit", "sa", "devanagari"),
    Language("bh", "Bihari", "bh", "devanagari"),
    Language("mai", "Maithili", "mai", "devanagari"),
    Language("ang", "Angika", "ang", "devanagari"),
    Language("bho", "Bhojpuri", "bho", "devanagari"),
    Language("mah", "Magahi", "mah", "devanagari"),
    Language("sck", "Nagpuri", "sck", "devanagari"),
    Language("new", "Newari", "new", "devanagari"),
    Language("gom", "Goan Konkani", "gom", "devanagari"),
    Language("bgc", "Haryanvi", "bgc", "devanagari"),

    # -- other_lang_list ---------------------------------------------------
    Language("th", "Thai", "th", "th"),
    Language("ch", "Chinese (Simplified)", "ch_sim", "ch"),
    Language("ch_tra", "Chinese (Traditional)", "ch_tra", "chinese_cht"),
    Language("ja", "Japanese", "ja", "japan"),
    Language("ko", "Korean", "ko", "korean"),
    Language("ta", "Tamil", "ta", "ta"),
    Language("te", "Telugu", "te", "te"),
    Language("kn", "Kannada", "kn", ""),

    # -- Not an EasyOCR language at all (no model exists for it), but kept
    #    for RapidOCR/existing-settings compatibility -----------------------
    Language("el", "Greek", "", "el"),
]

_BY_CODE: Dict[str, Language] = {lang.code: lang for lang in _LANGUAGES}

DEFAULT_LANGUAGE = "de"


def all_languages() -> List[Language]:
    return sorted(_LANGUAGES, key=lambda lang: lang.display_name)


def supported_languages(engine_name: str) -> List[Language]:
    """Languages the given OCR backend ("easyocr" or "rapidocr") actually
    supports - i.e. those with a non-empty easyocr_lang/rapid_lang_type,
    sorted by display name. EasyOCR supports far more languages than
    RapidOCR (whose "family" models cover only a dozen or so scripts, not
    86 individual languages) - Settings uses this to only ever offer a
    language the selected backend can actually use. Falls back to all
    languages for an unrecognised engine_name."""
    if engine_name == "easyocr":
        langs = [lang for lang in _LANGUAGES if lang.easyocr_lang]
    elif engine_name == "rapidocr":
        langs = [lang for lang in _LANGUAGES if lang.rapid_lang_type]
    else:
        langs = list(_LANGUAGES)
    return sorted(langs, key=lambda lang: lang.display_name)


def get(code: str) -> Language:
    """Look up a language by its canonical code, falling back to the default
    (German) for an unrecognised code rather than raising - settings.json
    could in principle hold a stale/invalid value from an older version."""
    return _BY_CODE.get(code, _BY_CODE[DEFAULT_LANGUAGE])
