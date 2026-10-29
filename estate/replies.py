"""Compose client replies from the knowledge base only (F-02, F-04, F-07, F-11, F-12)."""
from __future__ import annotations

import re

from .kb import KnowledgeBase, Listing
from .language import normalize_text
from .matcher import Criteria, recommend

ASK = {
    "uz-latn": {"budget_usd": "oylik byudjetingiz (USD)", "district": "qaysi tuman", "rooms": "nechta xona"},
    "uz-cyrl": {"budget_usd": "ойлик бюджетингиз (USD)", "district": "қайси туман", "rooms": "нечта хона"},
    "ru": {"budget_usd": "ваш бюджет в месяц (USD)", "district": "какой район", "rooms": "сколько комнат"},
    "en": {"budget_usd": "your monthly budget (USD)", "district": "which district", "rooms": "how many rooms"},
}
ASK_PREFIX = {"uz-latn": "Iltimos, ayting: ", "uz-cyrl": "Илтимос, айтинг: ", "ru": "Подскажите, пожалуйста: ", "en": "Could you tell me "}
NONE_FOUND = {
    "uz-latn": "Hozircha mos bo'sh joy yo'q. Boshqa tuman yoki byudjetni ko'rib chiqasizmi?",
    "uz-cyrl": "Ҳозирча мос бўш жой йўқ. Бошқа туман ёки бюджетни кўриб чиқасизми?",
    "ru": "Сейчас подходящих свободных вариантов нет. Рассмотрим другой район или бюджет?",
    "en": "There is no available place that fits right now. Would you consider another district or budget?",
}
HANDOVER = {
    "uz-latn": "Ko'rish va kelishuv uchun rieltorning o'zi bilan gaplashing: {contact}",
    "uz-cyrl": "Кўриш ва келишув учун риэлторнинг ўзи билан гаплашинг: {contact}",
    "ru": "Чтобы посмотреть квартиру и договориться, свяжитесь с риэлтором: {contact}",
    "en": "To view the place and agree terms, please talk to the realtor directly: {contact}",
}
NO_PAYMENT = {
    "uz-latn": "Biz hech qanday oldindan to'lov so'ramaymiz. To'lov masalasini faqat rieltor bilan uchrashuvda hal qilasiz.",
    "uz-cyrl": "Биз ҳеч қандай олдиндан тўлов сўрамаймиз. Тўлов масаласини фақат риэлтор билан учрашувда ҳал қиласиз.",
    "ru": "Мы не берём никакой предоплаты. Оплату обсуждайте только с риэлтором при встрече.",
    "en": "We never ask for any prepayment. Discuss payment only with the realtor in person.",
}

# Answer to Telegram's automatic "/start": both languages, since we can't detect one yet.
WELCOME = ("Assalomu alaykum! Ijaraga uy qidiryapsizmi? Tuman, xonalar soni va oylik byudjetingizni yozing.\n\n"
           "Здравствуйте! Ищете жильё? Напишите район, количество комнат и бюджет в месяц.")

# What the realtor sees on /start or any message that is not a command.
REALTOR_MENU = {
    "uz-latn": ("Siz rieltor sifatida yozyapsiz. Buyruqlar:\n"
                "L-003 band — joy band bo'ldi\n"
                "L-003 bo'sh — joy yana bo'sh\n"
                "/list — e'lonlaringiz va ularning holati\n"
                "Kanalga yangi e'lon joylasangiz, bot uni o'zi qo'shadi (raqami bilan: 🆔 L-007)."),
    "uz-cyrl": ("Сиз риэлтор сифатида ёзяпсиз. Буйруқлар:\n"
                "L-003 банд — жой банд бўлди\n"
                "L-003 бўш — жой яна бўш\n"
                "/list — эълонларингиз ва уларнинг ҳолати\n"
                "Каналга янги эълон жойласангиз, бот уни ўзи қўшади (рақами билан: 🆔 L-007)."),
    "ru": ("Вы пишете как риэлтор. Команды:\n"
           "L-003 занято — объект занят\n"
           "L-003 свободно — объект снова свободен\n"
           "/list — ваши объявления и их статус\n"
           "Новое объявление в канале бот добавит сам (с номером: 🆔 L-007)."),
}
# Shown under the menu when the realtor assistant (AI) is on.
REALTOR_AI_HINT = {
    "uz-latn": "Oddiy gap bilan ham yozing, masalan: \"Minor metrodagi uy ijaraga berildi\" yoki \"Nechta mijoz yozdi?\"",
    "uz-cyrl": "Оддий гап билан ҳам ёзинг, масалан: \"Минор метродаги уй ижарага берилди\" ёки \"Нечта мижоз ёзди?\"",
    "ru": "Можно писать и обычными словами, например: \"Квартиру у метро Минор сдали\" или \"Сколько клиентов писали?\"",
}
# Confirming a status change the assistant proposed: heading, taken, free, how to answer, cancelled.
REALTOR_CONFIRM = {
    "uz-latn": ("Tasdiqlaysizmi?", "band", "bo'sh", "Ha / Yo'q", "Bekor qilindi, hech narsa o'zgarmadi."),
    "uz-cyrl": ("Тасдиқлайсизми?", "банд", "бўш", "Ҳа / Йўқ", "Бекор қилинди, ҳеч нарса ўзгармади."),
    "ru": ("Подтверждаете?", "занято", "свободно", "Да / Нет", "Отменено, ничего не изменилось."),
}
_YES = {"ha", "xa", "ҳа", "ха", "да", "yes", "ok", "ок", "mayli", "майли", "albatta", "албатта", "конечно", "+"}
_NO = {"yo'q", "yoq", "йўқ", "йук", "нет", "no", "bekor", "бекор", "отмена", "-"}

_LIST_WORDS = {  # heading, free, taken, empty
    "uz-latn": ("E'lonlaringiz:", "bo'sh", "band", "Hozircha e'lon yo'q."),
    "uz-cyrl": ("Эълонларингиз:", "бўш", "банд", "Ҳозирча эълон йўқ."),
    "ru": ("Ваши объявления:", "свободно", "занято", "Объявлений пока нет."),
}

NO_CONTACT = {
    "uz-latn": "Iltimos, rieltor bilan odatdagi aloqa kanalingiz orqali bog'laning.",
    "uz-cyrl": "Илтимос, риэлтор билан одатдаги алоқа каналингиз орқали боғланинг.",
    "ru": "Пожалуйста, обратитесь к риэлтору через ваш обычный канал связи.",
    "en": "Please contact your realtor through your usual communication channel.",
}

# Wanting to see the place. "Show me" (ko'rsat, кўрсат), "I'll think it over"
# (ko'rib chiq) and "see the options" (посмотреть варианты) are searches, and so is
# "I want to rent" (снять): those get the list, not the realtor.
_VIEWING = re.compile(r"(\bko'r(?!sat|ib\s+chiq)|\bkor(?:ish|sam|moq)|кўр(?!сат|иб\s+чиқ)|"
                      r"посмотр(?!\w*\s+(?:\w+\s+)?вариант)|просмотр|"
                      r"\bview(?:ing)?\b|\bvisit\b|\btour\b|\bsee (?:it|the|this))")
# Asking for the realtor or a way to reach them.
_REALTOR = re.compile(r"(rieltor|риэлтор|риелтор|realtor|bog'lan|боғлан|связат|связь|контакт|"
                      r"\baloqa|алоқа|\bcontact\b)")
_PAYMENT = re.compile(r"(oldindan|to'lov|to'la|o'tkaz|\bpul\w*|тўлов|тўла|ўтказ|\bпул\w*|"
                      r"олдиндан|предоплат|\b(?:за|о)?плат|\b(?:за|о)?плач|\bденьг|"
                      r"перевести|перевод|prepay|\bpay(?:ment)?\b|"
                      r"deposit|depozit|zaklad|avans|аванс|залог|депозит|\bmoney\b|\btransfer\b)")

# District names for Cyrillic cards: (uz-cyrl, ru). Latin cards keep the stored name.
_DISTRICT_NAMES = {
    "Chilonzor": ("Чилонзор", "Чиланзар"), "Yunusobod": ("Юнусобод", "Юнусабад"),
    "Mirzo Ulug'bek": ("Мирзо Улуғбек", "Мирзо-Улугбек"), "Shayxontohur": ("Шайхонтоҳур", "Шайхантахур"),
    "Yakkasaroy": ("Яккасарой", "Яккасарай"), "Mirobod": ("Миробод", "Мирабад"),
    "Sergeli": ("Сергели", "Сергели"), "Olmazor": ("Олмазор", "Алмазар"), "Uchtepa": ("Учтепа", "Учтепа"),
    "Yashnobod": ("Яшнобод", "Яшнабад"), "Bektemir": ("Бектемир", "Бектемир"),
    "Yangihayot": ("Янгиҳаёт", "Янгихаят"),
}

_CARD = {
    "en": ("month", "room(s)", "Amenities", "Photos", "Post", "Videos"),
    "uz-latn": ("oy", "xona", "Qulayliklar", "Rasmlar", "E'lon", "Videolar"),
    "uz-cyrl": ("ой", "хона", "Қулайликлар", "Расмлар", "Эълон", "Видеолар"),
    "ru": ("месяц", "комн.", "Удобства", "Фото", "Объявление", "Видео"),
}
# Only controlled vocabulary is translated. Free-form source text is preserved,
# not guessed at or silently trusted; the independent checker gates the whole card.
_AMENITIES = {
    "parking": ("avtoturargoh", "автотураргоҳ", "парковка"),
    "furnished": ("mebelli", "мебелли", "мебель"),
    "air conditioning": ("konditsioner", "кондиционер", "кондиционер"),
    "washing machine": ("kir yuvish mashinasi", "кир ювиш машинаси", "стиральная машина"),
    "elevator": ("lift", "лифт", "лифт"),
    "wifi": ("Wi-Fi", "Wi-Fi", "Wi-Fi"),
    "balcony": ("balkon", "балкон", "балкон"),
    "school nearby": ("yaqinida maktab", "яқинида мактаб", "школа рядом"),
    "park view": ("bog' manzarasi", "боғ манзараси", "вид на парк"),
    "renovated": ("ta'mirlangan", "таъмирланган", "с ремонтом"),
    "concierge": ("konsyerj", "консьерж", "консьерж"),
    "gym": ("sport zali", "спорт зали", "спортзал"),
}


def wants_viewing(text: str) -> bool:
    return bool(_VIEWING.search(normalize_text(text)))


def wants_handover(text: str) -> bool:
    """Wanting to see the place or to talk to the realtor: only then share the contact."""
    return wants_viewing(text) or bool(_REALTOR.search(normalize_text(text)))


def mentions_payment(text: str) -> bool:
    return bool(_PAYMENT.search(normalize_text(text)))


def say(table: dict, lang: str, **values) -> str:
    """One template in the client's language, or Uzbek then Russian for "both"."""
    langs = ("uz-latn", "ru") if lang == "both" else (lang,)
    return "\n\n".join(table[l].format(**values) for l in langs)


def _title(title: str, lang: str) -> str:
    """Titles written "Uzbek / Russian" show the client's half (Uzbek for both scripts)."""
    uz, separator, ru = title.partition(" / ")
    if not separator or lang == "both":
        return title
    return ru if lang == "ru" else uz


def format_listing(l: Listing, lang: str = "en") -> str:
    """Localized card; private fields are excluded, but check before sending it."""
    title = _title(l.title, lang)
    lang = "uz-latn" if lang == "both" else lang
    month, rooms, amenities, photos, post, videos = _CARD[lang]
    index = {"uz-latn": 0, "uz-cyrl": 1, "ru": 2}.get(lang)
    translated = [(_AMENITIES[a][index] if index is not None and a in _AMENITIES else a)
                  for a in l.amenities]
    amen = ", ".join(translated) if translated else "-"
    video_count = f" | {videos}: {len(l.videos)}" if l.videos else ""
    district = (_DISTRICT_NAMES[l.district][index - 1]
                if index in (1, 2) and l.district in _DISTRICT_NAMES else l.district)
    return (f"[{l.id}] {title}\n${l.rent_usd}/{month} | {district} | {l.rooms} {rooms}, {l.area_m2:g} m2\n"
            f"{amenities}: {amen}\n{photos}: {len(l.photos)}{video_count} | {post}: {l.post_url}")


def realtor_listings(listings: list[Listing], lang: str, limit: int = 50) -> str:
    """The realtor's own view: every listing with its status, newest IDs last."""
    langs = ("uz-latn", "ru") if lang == "both" else (lang,)
    blocks = []
    for l_ in langs:
        heading, free, taken, empty = _LIST_WORDS[l_]
        rows = [f"{'✅' if x.available else '⛔'} {x.id} · {free if x.available else taken} · "
                f"{_title(x.title, l_)} · ${x.rent_usd}"
                for x in sorted(listings, key=lambda x: x.id)[:limit]]
        blocks.append("\n".join([heading] + rows) if rows else empty)
    return "\n\n".join(blocks)


def _first_word(text: str) -> str:
    words = re.findall(r"[\w'+-]+", normalize_text(text))
    return words[0] if words else ""


def is_yes(text: str) -> bool:
    return _first_word(text) in _YES


def is_no(text: str) -> bool:
    return _first_word(text) in _NO


def realtor_confirmation(listings: dict, changes: list[tuple[str, str]], lang: str) -> str:
    """The question the realtor answers before any proposed status change is applied."""
    blocks = []
    for l_ in (("uz-latn", "ru") if lang == "both" else (lang,)):
        heading, taken, free, answer, _ = REALTOR_CONFIRM[l_]
        rows = [f"{listing_id} ({_title(listings[listing_id].title, l_)}) → {taken if status == 'taken' else free}"
                for listing_id, status in changes]
        blocks.append("\n".join([heading, *rows, answer]))
    return "\n\n".join(blocks)


def is_start(text: str) -> bool:
    return text.strip().split(" ", 1)[0].lower() == "/start"


def compose(kb: KnowledgeBase, message: str, c: Criteria, lang: str, realtor_contact: str) -> str:
    if is_start(message):
        return WELCOME
    if mentions_payment(message):
        return say(NO_PAYMENT, lang)                                     # F-11
    if wants_handover(message):
        return say(HANDOVER, lang, contact=realtor_contact)              # F-07
    missing = c.missing()
    if missing:
        return "\n\n".join(ASK_PREFIX[l] + ", ".join(ASK[l][m] for m in missing) + "?"
                           for l in (("uz-latn", "ru") if lang == "both" else (lang,)))  # F-03
    hits = recommend(kb, c)
    if not hits:
        return say(NONE_FOUND, lang)
    return "\n\n".join(format_listing(l, lang) for l in hits)           # F-02, F-04
