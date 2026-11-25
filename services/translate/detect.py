"""Which language is this? — without asking anyone.

The translation model has to be told the language it is translating from.
Two cheap observations settle most texts:

* the **script**: kana means Japanese, Hangul Korean, Thai is Thai; within
  Cyrillic and Arabic script a few letters give the language away (``ї`` is
  Ukrainian, ``پ`` Persian);
* for the Latin alphabet, the **little words**: a text full of "the, and, of"
  is English, "der, und, nicht" German. Letters such as ``ñ`` or ``ł`` add
  to the count.

``detect`` returns a language code and how sure it is (0–1). A short or mixed
text scores low; the caller then asks the model itself. The word lists are a
handful of the commonest function words of each language — enough to tell
them apart, not a dictionary.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# (first code point, last code point, language) for scripts that belong to one language here.
_SCRIPTS: Tuple[Tuple[int, int, str], ...] = (
    (0x3040, 0x30FF, "ja"), (0xAC00, 0xD7AF, "ko"), (0x1100, 0x11FF, "ko"), (0x0370, 0x03FF, "el"), (0x1F00, 0x1FFF, "el"),
    (0x0590, 0x05FF, "he"), (0x0E00, 0x0E7F, "th"), (0x0E80, 0x0EFF, "lo"), (0x0F00, 0x0FFF, "bo"), (0x1000, 0x109F, "my"),
    (0x10A0, 0x10FF, "ka"), (0x1200, 0x137F, "am"), (0x1780, 0x17FF, "km"), (0x0530, 0x058F, "hy"), (0x0980, 0x09FF, "bn"),
    (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"), (0x0B00, 0x0B7F, "or"), (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"),
    (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"), (0x0D80, 0x0DFF, "si"),
)
_HAN = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))
_CYRILLIC = (0x0400, 0x04FF)
_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_DEVANAGARI = (0x0900, 0x097F)

# Letters that settle the language within a shared script.
_CYRILLIC_MARKS = (("uk", "іїєґ"), ("be", "ў"), ("mk", "ѓќѕ"), ("sr", "јљњћђџ"), ("kk", "әғқңұһ"), ("mn", "өү"))
_ARABIC_MARKS = (("ur", "ٹڈڑےں"), ("ps", "ټډړښږڼ"), ("fa", "پچژگ"))
# Characters used only in Traditional (or only in Simplified) Chinese writing — a few very common ones.
_TRADITIONAL = set("們這個來時為說國學會對種後發經過還點當麼開關與問間現實樣電話機車書長門東風飛馬魚鳥龍")
_SIMPLIFIED = set("们这个来时为说国学会对种后发经过还点当么开关与问间现实样电话机车书长门东风飞马鱼鸟龙")

_WORDS: Dict[str, str] = {
    "en": "the and of to in is that it for was with as on be at by this have from or not are but had his they you were a an my we i me your our has do does did can will would what where when how who why there here if so no yes please need want",
    "es": "el la de que y en los se del las por un para con no una su al lo como más pero sus le ya o este sí porque muy es está hay mi tu yo dónde qué cómo cuándo quién necesito quiero tengo aquí",
    "fr": "le la les de des et à un une du en que qui pour dans ce il est pas sur au plus par je ne se avec son mais nous vous mon ma où quand comment pourquoi ici y ai avez faut",
    "de": "der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er hat dass ich wir sie wo wie was wann warum mein hier bitte haben brauche",
    "it": "il di che e la per un in è non una sono con si da come le più ma gli dei anche del della questo nel alla ho io mi dove quando perché qui ha abbiamo devo",
    "pt": "o a de que e do da em um para é com não uma os no se na por mais as dos como mas foi ao ele das tem à seu eu meu minha onde quando está aqui preciso quero temos",
    "nl": "de het een en van ik te dat die in is niet zijn op aan met als voor er maar om hij ook naar bij nog deze dit",
    "sv": "och i att det som en på är av för med till den har de inte om ett han men var jag sig från vi så kan när",
    "da": "og i at det er en til på de af for ikke den med han var jeg sig der har som et men om vi min havde fra",
    "no": "og i det er på en til som av for ikke har med de at han jeg var den om et men seg fra vi så kan eller blir",
    "fi": "ja on ei se että oli hän mutta ovat kun niin myös tai jos kuin vain sen joka olla minä mitä tämä siitä sitä",
    "pl": "i w nie na z że to się do jest o jak a po co tak ale za od przez tylko czy już był jego jej dla tym ich",
    "cs": "a se na je že v to s z do o i ve jako ale pro by za byl jsem si po tak jeho co nebo při už aby jsou",
    "sk": "a sa na je že v to s z do o aj vo ako ale pre by za bol som si po tak jeho čo alebo pri už aby sú",
    "ro": "și în de la a cu că nu pe un o este pentru din care mai se ce au fost sau dar după sunt lui prin fi",
    "hu": "a az és hogy nem is egy van volt ez de csak már még meg mint vagy azt el én aki ha lesz ki kell majd",
    "tr": "ve bir bu da de için ile çok daha ama gibi ne var olarak kadar değil en o ki her mi ben sen ya sonra",
    "id": "yang dan di ini itu dengan untuk tidak dari dalam akan pada juga saya ke karena ada atau oleh sudah bisa kami",
    "vi": "và của là có không trong một cho được người này với các đã để những khi tôi ở đến như cũng về nhưng rất",
    "tl": "ang ng sa na at mga ay si ito hindi para kung may ako ko mo niya siya namin natin kami pero din lang",
    "sw": "na ya wa kwa ni katika la za kuwa hii yake au pia hiyo kama lakini wake zake sana yao bila mimi wewe",
    "la": "et in est non ad quod cum ut qui sed ex si per aut atque enim de esse quae ab autem sunt eius hoc quam",
    "ca": "el la de i que a en els les un una per amb no és del al es com més però seu ha són aquest o hi",
    "hr": "i je u se na da za su s od ne što a to bi kao ali ili iz koji po sam biti će ih smo još samo",
    "sl": "in je v se na da za so s z ne ki pa to bi kot ali iz po sem biti bo jih smo še tudi",
    "lt": "ir yra į iš kad su o bet tai ar kaip dėl nuo po prie jis ji mes jūs buvo būti tik dar jau",
    "lv": "un ir uz no ka ar par bet tas vai kā pēc pie viņš viņa mēs jūs bija būt tikai vēl jau šis",
    "et": "ja on ei et see oli kui ka aga või mis nii siis veel ta ma me te olla ainult juba selle",
    "af": "die en van in is het dat nie vir op met wat sy hy ek ons om te was maar ook deur jy",
    "sq": "dhe në e të një për me që nga është nuk por si ka do më ai ajo ne ju janë ishte vetëm",
    "ht": "ak nan pou yon li mwen ou nou yo ki pa se sa a te gen ap men tout sou tankou",
    "ru": "и в не на что с это как по но из у за он то же так вы бы от мы она они был для его к",
    "uk": "і в не на що з це як по але із у за він то ж так ви від ми вона вони був для його до та",
    "bg": "и в не на че с това как по но от у за той се да така вие бих ние тя те беше за към или",
    "sr": "и је у се на да за су с од не што а то би као али или из који по сам бити ће их смо",
    "mk": "и е во се на да за се со од не што а тоа би како но или од кој по сум ќе ги сме",
    "be": "і ў не на што з гэта як па але з у за ён то ж так вы ад мы яна яны быў для яго да",
    "hi": "और का के की है में से को पर यह कि था हैं नहीं एक भी इस ने तो हो कर",
    "mr": "आणि आहे या ही हा मध्ये की ते तो ती नाही पण एक होते केले असे आहेत",
    "ne": "र छ को मा हो यो पनि लाई गर्न भएको थियो छन् तर एक गरेको भने",
    "ar": "في من على أن إلى هذا التي الذي ما لا عن مع هو كان هذه قد كل ذلك بين",
    "fa": "و در به از که این را با است برای آن یک خود تا کرد بر هم نیز شد ها",
    "ur": "اور کے کی ہے میں سے کو پر یہ کہ تھا ہیں نہیں ایک بھی اس نے تو ہو",
}
_WORD_SETS: Dict[str, frozenset] = {code: frozenset(words.split()) for code, words in _WORDS.items()}
_LATIN = ("en", "es", "fr", "de", "it", "pt", "nl", "sv", "da", "no", "fi", "pl", "cs", "sk", "ro", "hu", "tr", "id", "vi",
          "tl", "sw", "la", "ca", "hr", "sl", "lt", "lv", "et", "af", "sq", "ht")
# Letters that lean a Latin-alphabet text towards a language.
_LATIN_MARKS = (("es", "ñ¿¡"), ("pt", "ãõ"), ("de", "ß"), ("pl", "łżźćńęą"), ("hu", "őű"), ("tr", "ğış"), ("ro", "țșă"),
                ("cs", "řěů"), ("sk", "ľĺŕô"), ("vi", "ơưđạảấầẩẫậắằẳẵặẹẻẽếềểễệọỏốồổỗộớờởỡợụủứừửữự"), ("fr", "çœ"),
                ("da", "ø"), ("no", "ø"), ("sv", "å"), ("lt", "ėįųū"), ("lv", "āēīūķļņģ"), ("et", "õ"), ("sq", "ë"), ("ca", "·"))
# Words are what stands between spaces and punctuation. (Not ``\w+``: the vowel
# signs of Indian scripts are marks, not letters, and would cut every word up.)
_SPLIT = re.compile(r"[\s\d.,;:!?()\"'’‘“”«»।॥…—–\-/\[\]{}<>|*_=+~^%$#@&\\]+")


def _in(char: str, ranges) -> bool:
    point = ord(char)
    if isinstance(ranges[0], int):
        return ranges[0] <= point <= ranges[1]
    return any(first <= point <= last for first, last in ranges)


def _by_words(tokens: List[str], candidates, text: str, marks=()) -> Tuple[str, float]:
    """The candidate whose little words fill most of the text, and how far it leads."""
    if not tokens:
        return "", 0.0
    scores = {code: sum(1.0 for token in tokens if token in _WORD_SETS[code]) for code in candidates if code in _WORD_SETS}
    for code, letters in marks:
        if code in scores:
            scores[code] += 2.0 * min(3, sum(1 for char in text if char in letters))
    ranked = sorted(scores.items(), key=lambda pair: -pair[1])
    if not ranked or ranked[0][1] <= 0:
        return "", 0.0
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    share = min(1.0, top / max(3.0, len(tokens) * 0.25))          # the little words are a quarter of ordinary prose
    lead = (top - second) / top
    return best, round(min(1.0, share * (0.4 + 0.6 * lead)), 2)


def detect(text: str) -> Tuple[str, float]:
    """``(language code, how sure 0..1)``; ``("", 0.0)`` when there is nothing to go on."""
    text = str(text or "")
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return "", 0.0
    sample = letters[:4000]
    counts: Dict[str, int] = {}
    for char in sample:
        script = "latin"
        if _in(char, _HAN):
            script = "han"
        elif _in(char, _CYRILLIC):
            script = "cyrillic"
        elif _in(char, _ARABIC):
            script = "arabic"
        elif _in(char, _DEVANAGARI):
            script = "devanagari"
        else:
            point = ord(char)
            for first, last, code in _SCRIPTS:
                if first <= point <= last:
                    script = code
                    break
        counts[script] = counts.get(script, 0) + 1
    total = len(sample)
    # Kana anywhere means Japanese even when most of the characters are Chinese ones.
    if counts.get("ja", 0) >= max(1, total * 0.05):
        return "ja", 0.95
    script, most = max(counts.items(), key=lambda pair: pair[1])
    share = most / total
    lowered = text.lower()
    tokens = [token for token in _SPLIT.split(lowered) if token][:600]
    if script == "han":
        traditional = sum(1 for char in sample if char in _TRADITIONAL)
        simplified = sum(1 for char in sample if char in _SIMPLIFIED)
        return ("zh-Hant" if traditional > simplified else "zh-Hans"), round(0.6 + 0.35 * share, 2)
    if script == "cyrillic":
        for code, marks in _CYRILLIC_MARKS:
            if any(char in lowered for char in marks):
                return code, round(0.55 + 0.35 * share, 2)
        best, sure = _by_words(tokens, ("ru", "bg", "sr", "mk", "be", "uk"), lowered)
        return (best or "ru"), (max(sure, 0.5) if best else 0.45)
    if script == "arabic":
        for code, marks in _ARABIC_MARKS:
            if sum(1 for char in text if char in marks) >= 2:
                return code, round(0.55 + 0.35 * share, 2)
        best, sure = _by_words(tokens, ("ar", "fa", "ur"), lowered)
        return (best or "ar"), (max(sure, 0.55) if best else 0.5)
    if script == "devanagari":
        # Hindi unless the little words are plainly Marathi or Nepali.
        hindi = sum(1 for token in tokens if token in _WORD_SETS["hi"])
        best, sure = _by_words(tokens, ("mr", "ne"), lowered)
        other = sum(1 for token in tokens if best and token in _WORD_SETS[best])
        if best and other >= 2 and other > hindi * 1.5:
            return best, max(sure, 0.55)
        return "hi", (0.75 if hindi else 0.5)
    if script != "latin":
        return script, round(0.6 + 0.35 * share, 2)                # a script of its own
    best, sure = _by_words(tokens, _LATIN, lowered, _LATIN_MARKS)
    if len(tokens) < 3:
        sure = min(sure, 0.3)                                       # a word or two: could be anything
    return best, sure
