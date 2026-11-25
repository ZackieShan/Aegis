"""The languages the translator works between.

Codes are ISO 639-1 (``es``), with a few regional or script variants where
they change the result (``zh-Hant``, ``pt-BR``). The list is what the
translation model (TranslateGemma) names in its own prompt template; a
general chat model used as a stand-in understands the same names.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# code -> English name
LANGUAGES: Dict[str, str] = {
    "aa": "Afar", "ab": "Abkhazian", "af": "Afrikaans", "ak": "Akan", "am": "Amharic", "an": "Aragonese",
    "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani", "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian",
    "bm": "Bambara", "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian", "ca": "Catalan",
    "ce": "Chechen", "co": "Corsican", "cs": "Czech", "cv": "Chuvash", "cy": "Welsh", "da": "Danish", "de": "German",
    "dv": "Divehi", "dz": "Dzongkha", "ee": "Ewe", "el": "Greek", "en": "English", "eo": "Esperanto",
    "es": "Spanish", "et": "Estonian", "eu": "Basque", "fa": "Persian", "ff": "Fulah", "fi": "Finnish",
    "fo": "Faroese", "fr": "French", "fy": "Western Frisian", "ga": "Irish", "gd": "Scottish Gaelic",
    "gl": "Galician", "gn": "Guarani", "gu": "Gujarati", "gv": "Manx", "ha": "Hausa", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian", "hy": "Armenian", "ia": "Interlingua",
    "id": "Indonesian", "ie": "Interlingue", "ig": "Igbo", "ii": "Sichuan Yi", "ik": "Inupiaq", "io": "Ido",
    "is": "Icelandic", "it": "Italian", "iu": "Inuktitut", "ja": "Japanese", "jv": "Javanese", "ka": "Georgian",
    "ki": "Kikuyu", "kk": "Kazakh", "kl": "Kalaallisut", "km": "Khmer", "kn": "Kannada", "ko": "Korean",
    "ks": "Kashmiri", "ku": "Kurdish", "kw": "Cornish", "ky": "Kyrgyz", "la": "Latin", "lb": "Luxembourgish",
    "lg": "Ganda", "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian", "lu": "Luba-Katanga", "lv": "Latvian",
    "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi",
    "ms": "Malay", "mt": "Maltese", "my": "Burmese", "nb": "Norwegian Bokmål", "nd": "North Ndebele", "ne": "Nepali",
    "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian", "nr": "South Ndebele", "nv": "Navajo",
    "ny": "Chichewa", "oc": "Occitan", "om": "Oromo", "or": "Odia", "os": "Ossetian", "pa": "Punjabi",
    "pl": "Polish", "ps": "Pashto", "pt": "Portuguese", "qu": "Quechua", "rm": "Romansh", "rn": "Rundi",
    "ro": "Romanian", "ru": "Russian", "rw": "Kinyarwanda", "sa": "Sanskrit", "sc": "Sardinian", "sd": "Sindhi",
    "se": "Northern Sami", "sg": "Sango", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "sn": "Shona",
    "so": "Somali", "sq": "Albanian", "sr": "Serbian", "ss": "Swati", "st": "Southern Sotho", "su": "Sundanese",
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai", "ti": "Tigrinya",
    "tk": "Turkmen", "tl": "Tagalog", "tn": "Tswana", "to": "Tonga", "tr": "Turkish", "ts": "Tsonga", "tt": "Tatar",
    "ug": "Uyghur", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "ve": "Venda", "vi": "Vietnamese",
    "vo": "Volapük", "wa": "Walloon", "wo": "Wolof", "xh": "Xhosa", "yi": "Yiddish", "yo": "Yoruba", "za": "Zhuang",
    "zh": "Chinese", "zu": "Zulu",
}

# Variants worth offering: they give a different translation, not just a different label.
VARIANTS: Dict[str, str] = {
    "zh-Hans": "Chinese (Simplified)", "zh-Hant": "Chinese (Traditional)", "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)", "es-MX": "Spanish (Mexico)", "es-ES": "Spanish (Spain)",
    "en-GB": "English (UK)", "en-US": "English (US)", "fr-CA": "French (Canada)", "sr-Latn": "Serbian (Latin script)",
}

# What speakers call their own language — shown beside the English name in a picker.
NATIVE: Dict[str, str] = {
    "en": "English", "es": "Español", "fr": "Français", "de": "Deutsch", "it": "Italiano", "pt": "Português",
    "nl": "Nederlands", "ru": "Русский", "uk": "Українська", "pl": "Polski", "cs": "Čeština", "sk": "Slovenčina",
    "ro": "Română", "hu": "Magyar", "bg": "Български", "sr": "Српски", "hr": "Hrvatski", "sl": "Slovenščina",
    "el": "Ελληνικά", "tr": "Türkçe", "sv": "Svenska", "da": "Dansk", "no": "Norsk", "nb": "Norsk bokmål",
    "fi": "Suomi", "et": "Eesti", "lv": "Latviešu", "lt": "Lietuvių", "is": "Íslenska", "ga": "Gaeilge", "cy": "Cymraeg",
    "ca": "Català", "eu": "Euskara", "gl": "Galego", "la": "Latina", "sq": "Shqip", "mk": "Македонски",
    "ar": "العربية", "he": "עברית", "fa": "فارسی", "ur": "اردو", "ps": "پښتو", "ku": "Kurdî",
    "hi": "हिन्दी", "bn": "বাংলা", "pa": "ਪੰਜਾਬੀ", "gu": "ગુજરાતી", "mr": "मराठी", "ne": "नेपाली", "ta": "தமிழ்",
    "te": "తెలుగు", "kn": "ಕನ್ನಡ", "ml": "മലയാളം", "si": "සිංහල",
    "zh": "中文", "zh-Hans": "简体中文", "zh-Hant": "繁體中文", "ja": "日本語", "ko": "한국어", "vi": "Tiếng Việt",
    "th": "ไทย", "lo": "ລາວ", "km": "ខ្មែរ", "my": "မြန်မာ", "id": "Bahasa Indonesia", "ms": "Bahasa Melayu",
    "tl": "Tagalog", "sw": "Kiswahili", "am": "አማርኛ", "so": "Soomaali", "ha": "Hausa", "yo": "Yorùbá", "ig": "Igbo",
    "zu": "isiZulu", "xh": "isiXhosa", "af": "Afrikaans", "ht": "Kreyòl ayisyen", "hy": "Հայերեն", "ka": "ქართული",
    "az": "Azərbaycanca", "kk": "Қазақша", "uz": "Oʻzbekcha", "mn": "Монгол", "pt-BR": "Português (Brasil)",
    "es-MX": "Español (México)", "fr-CA": "Français (Canada)", "yi": "ייִדיש", "eo": "Esperanto", "mt": "Malti",
}

# The ones most people reach for, in the order a picker shows them first.
COMMON: List[str] = ["en", "es", "fr", "de", "it", "pt", "zh-Hans", "zh-Hant", "ja", "ko", "ru", "uk", "pl", "ar", "hi",
                     "vi", "tl", "th", "tr", "nl", "el", "he", "fa", "sw", "ht", "la"]

# Other things people call a language.
_ALIASES: Dict[str, str] = {
    "mandarin": "zh-Hans", "chinese": "zh-Hans", "simplified chinese": "zh-Hans", "chinese simplified": "zh-Hans",
    "traditional chinese": "zh-Hant", "chinese traditional": "zh-Hant", "cantonese": "zh-Hant", "taiwanese": "zh-Hant",
    "castilian": "es", "mexican spanish": "es-MX", "mexican": "es-MX", "brazilian portuguese": "pt-BR", "brazilian": "pt-BR",
    "farsi": "fa", "persian": "fa", "dari": "fa", "filipino": "tl", "pilipino": "tl", "tagalog": "tl", "burmese": "my",
    "myanmar": "my", "khmer": "km", "cambodian": "km", "haitian": "ht", "creole": "ht", "haitian creole": "ht",
    "kreyol": "ht", "bahasa": "id", "bahasa indonesia": "id", "malay": "ms", "flemish": "nl", "gaelic": "gd",
    "irish gaelic": "ga", "scots gaelic": "gd", "norwegian": "no", "bokmal": "nb", "nynorsk": "nn", "punjabi": "pa",
    "panjabi": "pa", "bangla": "bn", "sinhalese": "si", "odia": "or", "oriya": "or", "pashto": "ps", "pushto": "ps",
    "uighur": "ug", "uyghur": "ug", "kirghiz": "ky", "british english": "en-GB", "american english": "en-US",
    "canadian french": "fr-CA", "quebecois": "fr-CA", "serbian latin": "sr-Latn", "greek": "el", "ancient greek": "el",
    "hebrew": "he", "modern hebrew": "he", "swahili": "sw", "kiswahili": "sw", "slovene": "sl", "moldovan": "ro",
    "zh-cn": "zh-Hans", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh_cn": "zh-Hans", "zh_tw": "zh-Hant", "iw": "he",
    "in": "id", "jp": "ja", "kr": "ko", "cn": "zh-Hans", "ua": "uk", "gr": "el", "dk": "da", "se": "sv", "cz": "cs",
}


def _key(text: str) -> str:
    return re.sub(r"[^\w\s-]+", " ", str(text or "").strip().lower().replace("_", "-")).strip()


def all_codes() -> List[str]:
    return list(LANGUAGES) + list(VARIANTS)


def name(code: str) -> str:
    """``es`` -> ``Spanish``; "" for a code that is not a language here."""
    code = str(code or "")
    return VARIANTS.get(code) or LANGUAGES.get(code) or LANGUAGES.get(code.split("-")[0], "")


def plain_name(code: str) -> str:
    """The language without its variant: ``pt-BR`` -> ``Portuguese`` (what the model is told)."""
    return LANGUAGES.get(str(code or "").split("-")[0], "")


def _exact(key: str, codes: bool) -> str:
    """The language a whole (normalised) string names, or ""."""
    if not key:
        return ""
    if codes:
        for code in all_codes():
            if key == code.lower():
                return code
    if key in _ALIASES and (codes or len(key) > 3):
        return _ALIASES[key]
    for code, label in list(VARIANTS.items()) + list(LANGUAGES.items()):
        if key == _key(label):
            return code
    for code, label in NATIVE.items():
        if key == _key(label):
            return code
    base = key.split("-")[0]
    if codes and base in LANGUAGES and re.fullmatch(r"[a-z]{2}-[a-z]{2,4}", key):
        return base                                   # a regional code we do not list: its language
    return ""


def resolve(text: str, codes: bool = True) -> str:
    """The language someone means — a code (``es``, ``zh-TW``), its English
    name (``Spanish``), its own name (``español``) or another word for it
    (``Mandarin``, ``Farsi``). "" when nothing fits. With ``codes=False``
    only names count: inside a sentence "is", "it" and "no" are words, not
    Icelandic, Italian and Norwegian."""
    key = _key(text)
    if not key:
        return ""
    found = _exact(key, codes)
    if found:
        return found
    words = key.split()
    if len(words) > 1:                                # "the Spanish language", "in French", "It is Spanish."
        for size in range(min(3, len(words) - 1), 0, -1):      # the longest name first: "Haitian Creole" before "Creole"
            for first in range(0, len(words) - size + 1):
                piece = " ".join(words[first:first + size])
                if size == 1 and len(piece) <= 3:
                    continue                          # a little word is a word here, not a code
                found = _exact(piece, False)
                if found:
                    return found
    return ""


def listing() -> List[dict]:
    """Every language for a picker: the common ones first, then the rest by name."""
    def row(code: str) -> dict:
        return {"code": code, "name": name(code), "native": NATIVE.get(code, "")}

    seen = set(COMMON)
    rest = sorted((code for code in all_codes() if code not in seen and code != "zh"), key=lambda code: name(code))
    return [dict(row(code), common=True) for code in COMMON] + [dict(row(code), common=False) for code in rest]
