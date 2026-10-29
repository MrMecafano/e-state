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
    "uz-latn": "Biz hech qanday oldindan to'lov so'ramaymiz. To'lov masalasini faqat rieltor bilan uchrashuvda hal qilasiz: {contact}",
    "uz-cyrl": "Биз ҳеч қандай олдиндан тўлов сўрамаймиз. Тўлов масаласини фақат риэлтор билан учрашувда ҳал қиласиз: {contact}",
    "ru": "Мы не берём никакой предоплаты. Оплату обсуждайте только с риэлтором при встрече: {contact}",
    "en": "We never ask for any prepayment. Discuss payment only with the realtor in person: {contact}",
}

NO_CONTACT = {
    "uz-latn": "Iltimos, rieltor bilan odatdagi aloqa kanalingiz orqali bog'laning.",
    "uz-cyrl": "Илтимос, риэлтор билан одатдаги алоқа каналингиз орқали боғланинг.",
    "ru": "Пожалуйста, обратитесь к риэлтору через ваш обычный канал связи.",
    "en": "Please contact your realtor through your usual communication channel.",
}

_VIEWING = re.compile(r"(\bko'r|\bkor(?:ish|sam|moq)|кўр|посмотр|просмотр|"
                      r"\bview(?:ing)?\b|\bvisit\b|\btour\b|\bsee (?:it|the|this)|"
                      r"\brent it|\bolaman\b|снять|арендую)")
_PAYMENT = re.compile(r"(oldindan|to'lov|to'la|o'tkaz|\bpul\w*|тўлов|тўла|ўтказ|\bпул\w*|"
                      r"олдиндан|предоплат|\b(?:за|о)?плат|\b(?:за|о)?плач|\bденьг|"
                      r"перевести|перевод|prepay|\bpay(?:ment)?\b|"
                      r"deposit|depozit|zaklad|avans|аванс|залог|депозит|\bmoney\b|\btransfer\b)")

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


def mentions_payment(text: str) -> bool:
    return bool(_PAYMENT.search(normalize_text(text)))


def format_listing(l: Listing, lang: str = "en") -> str:
    """Localized card; private fields are excluded, but check before sending it."""
    month, rooms, amenities, photos, post, videos = _CARD[lang]
    index = {"uz-latn": 0, "uz-cyrl": 1, "ru": 2}.get(lang)
    translated = [(_AMENITIES[a][index] if index is not None and a in _AMENITIES else a)
                  for a in l.amenities]
    amen = ", ".join(translated) if translated else "-"
    video_count = f" | {videos}: {len(l.videos)}" if l.videos else ""
    return (f"[{l.id}] {l.title}\n${l.rent_usd}/{month} | {l.district} | {l.rooms} {rooms}, {l.area_m2:g} m2\n"
            f"{amenities}: {amen}\n{photos}: {len(l.photos)}{video_count} | {post}: {l.post_url}")


def compose(kb: KnowledgeBase, message: str, c: Criteria, lang: str, realtor_contact: str) -> str:
    if mentions_payment(message):
        return NO_PAYMENT[lang].format(contact=realtor_contact)          # F-11
    if wants_viewing(message):
        return HANDOVER[lang].format(contact=realtor_contact)            # F-07
    missing = c.missing()
    if missing:
        return ASK_PREFIX[lang] + ", ".join(ASK[lang][m] for m in missing) + "?"  # F-03
    hits = recommend(kb, c)
    if not hits:
        return NONE_FOUND[lang]
    return "\n\n".join(format_listing(l, lang) for l in hits)           # F-02, F-04
