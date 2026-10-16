"""Compose client replies from the knowledge base only (F-02, F-04, F-07, F-11, F-12)."""
from __future__ import annotations

import re

from .kb import KnowledgeBase, Listing
from .matcher import Criteria, recommend

ASK = {
    "uz-latn": {"budget_usd": "oylik byudjetingiz (USD)", "district": "qaysi tuman", "rooms": "nechta xona"},
    "uz-cyrl": {"budget_usd": "ойлик бюджетингиз (USD)", "district": "қайси туман", "rooms": "нечта хона"},
    "ru": {"budget_usd": "ваш бюджет в месяц (USD)", "district": "какой район", "rooms": "сколько комнат"},
    "en": {"budget_usd": "your monthly budget (USD)", "district": "which district", "rooms": "how many rooms"},
}
ASK_PREFIX = {"uz-latn": "Iltimos, ayting: ", "uz-cyrl": "Илтимос, айтинг: ", "ru": "Подскажите, пожалуйста: ", "en": "Could you tell me "}
NONE_FOUND = {
    "uz-latn": "Hozircha mos bo'sh joy yo'q. Rieltor siz bilan bog'lanadi.",
    "uz-cyrl": "Ҳозирча мос бўш жой йўқ. Риэлтор сиз билан боғланади.",
    "ru": "Сейчас подходящих свободных вариантов нет. Риэлтор свяжется с вами.",
    "en": "There is no free place that fits right now. The realtor will contact you.",
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

_VIEWING = re.compile(r"(ko'r|ko‘r|кўр|посмотр|просмотр|viewing|visit|see it|rent it|olaman|снять|арендую)", re.I)
_PAYMENT = re.compile(r"(oldindan|to'lov|тўлов|предоплат|оплат|prepay|deposit|zaklad|залог)", re.I)


def wants_viewing(text: str) -> bool:
    return bool(_VIEWING.search(text))


def mentions_payment(text: str) -> bool:
    return bool(_PAYMENT.search(text))


def format_listing(l: Listing) -> str:
    """Client-facing card (F-04). Owner name/phone are never included (F-12)."""
    amen = ", ".join(l.amenities) if l.amenities else "-"
    return (f"[{l.id}] {l.title}\n${l.rent_usd}/month | {l.district} | {l.rooms} room(s), {l.area_m2:g} m2\n"
            f"Amenities: {amen}\nPhotos: {len(l.photos)} | Post: {l.post_url}")


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
    return "\n\n".join(format_listing(l) for l in hits)                 # F-02, F-04
