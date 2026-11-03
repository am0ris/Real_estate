"""Customer-facing agent phrases in Egyptian Arabic and English.

These are fixed workflow prompts (questions, confirmations, errors). Property
facts never come from here — they come from the grounded RAG workflow.
"""

from __future__ import annotations

PHRASES: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "Hello! I'm the Aqar assistant. Tell me the property type, area, budget or number of bedrooms you have in mind and I'll search our listings.",
        "ar": "أهلاً بيك! أنا مساعد عقار. قولّي نوع العقار والمنطقة والميزانية أو عدد الغرف وأنا أدوّرلك في العقارات المتاحة.",
    },
    "fallback": {
        "en": "I can help you find a property, answer questions about a listing, discuss payment options or book a viewing. What are you looking for?",
        "ar": "أقدر أساعدك تلاقي عقار، أو أجاوب على أسئلة عن وحدة معيّنة، أو نتكلم في طرق الدفع، أو أحجزلك معاينة. بتدوّر على إيه؟",
    },
    "search_unavailable": {
        "en": "Sorry, I can't reach the property listings right now. Please try again in a moment — your search details are saved.",
        "ar": "معلش، مش قادر أوصل للعقارات المتاحة دلوقتي. حاول تاني بعد شوية — تفاصيل بحثك محفوظة.",
    },
    "rent_unavailable": {
        "en": "Right now my listings are for sale only — I don't have rentals yet. Want me to look for properties to buy instead? Tell me the area, type or budget.",
        "ar": "حالياً العقارات المتاحة عندي للبيع بس، ومعنديش إيجار لسه. تحب أدوّرلك على عقارات للشراء؟ قولّي المنطقة أو النوع أو الميزانية.",
    },
    "ask_type": {"en": "What type of property are you looking for (apartment, villa, chalet...)?", "ar": "بتدوّر على نوع عقار إيه (شقة، فيلا، شاليه...)؟"},
    "ask_location": {"en": "Which area do you prefer?", "ar": "تفضّل أنهي منطقة؟"},
    "ask_budget": {"en": "What budget range do you have in mind?", "ar": "الميزانية اللي في بالك حوالي كام؟"},
    "ask_purpose": {"en": "Is it for living in or for investment?", "ar": "العقار للسكن ولا للاستثمار؟"},
    "ask_timeline": {"en": "When are you planning to buy or move?", "ar": "ناوي تشتري أو تنقل إمتى تقريباً؟"},
    "ask_payment_method": {"en": "Would you pay cash or prefer installments?", "ar": "هتدفع كاش ولا تفضّل التقسيط؟"},
    "qualification_complete": {
        "en": "Thanks, I have your main requirements: {summary}. Shall I search matching properties or book a call with an advisor?",
        "ar": "شكراً، معايا طلباتك الأساسية: {summary}. أدوّرلك على عقارات مناسبة ولا أحجزلك مكالمة مع مستشار؟",
    },
    "noted": {"en": "Noted: {summary}.", "ar": "تمام، سجلت: {summary}."},
    "schedule_need_property": {
        "en": "Which property would you like to visit? You can say its number from the list (for example \"the first one\") or its ID.",
        "ar": "حابب تعاين أنهي عقار؟ قولّي رقمه في القايمة (مثلاً \"الأول\") أو الكود بتاعه.",
    },
    "schedule_need_search": {
        "en": "Let's find the property first. Tell me what you're looking for and I'll show you options to visit.",
        "ar": "خلّينا نلاقي العقار الأول. قولّي بتدوّر على إيه وأنا أوريك اختيارات تقدر تعاينها.",
    },
    "schedule_need_date": {
        "en": "Which day suits you for visiting {property}? (e.g. tomorrow, Saturday or 2026-10-20)",
        "ar": "أنهي يوم يناسبك تعاين {property}؟ (مثلاً بكرة، السبت، أو 20/10)",
    },
    "schedule_bad_date": {"en": "Please pick a day from today onwards.", "ar": "من فضلك اختار يوم من النهارده أو بعد كده."},
    "schedule_no_slots": {
        "en": "There are no open viewing slots on {day}. Would another day work?",
        "ar": "مفيش مواعيد معاينة فاضية يوم {day}. يناسبك يوم تاني؟",
    },
    "schedule_offer_slots": {
        "en": "Available times on {day}: {slots}. Which time works for you?",
        "ar": "المواعيد المتاحة يوم {day}: {slots}. أنهي معاد يناسبك؟",
    },
    "schedule_slot_unavailable": {
        "en": "That time isn't available. Available times on {day}: {slots}.",
        "ar": "المعاد ده مش متاح. المواعيد المتاحة يوم {day}: {slots}.",
    },
    "schedule_need_contact": {
        "en": "To reserve the visit, please share a phone number our advisor can reach you on.",
        "ar": "علشان أحجزلك المعاينة، ابعتلي رقم موبايل المستشار يقدر يكلمك عليه.",
    },
    "schedule_phone_invalid": {
        "en": "That doesn't look like a valid mobile number. Please send it like 01012345678 or +201012345678.",
        "ar": "الرقم ده شكله مش صح. ابعته كده: 01012345678 أو +201012345678.",
    },
    "schedule_verify_phone": {
        "en": "Before I reserve it, let me check your number: {phone}. Is that correct? (yes / no)",
        "ar": "قبل ما أحجز، أتأكد من رقمك: {phone}. صح كده؟ (أيوه / لأ)",
    },
    "schedule_phone_rejected": {
        "en": "No problem. What is the correct number?",
        "ar": "ولا يهمك. إيه الرقم الصح؟",
    },
    "schedule_confirm": {
        "en": "Please confirm: viewing of {property} on {when}. Shall I reserve it? (yes / no)",
        "ar": "تأكيد: معاينة {property} يوم {when}. أحجزها؟ (أيوه / لأ)",
    },
    "cancel_none": {
        "en": "You don't have an upcoming viewing to cancel.",
        "ar": "مفيش عندك معاينة جاية علشان نلغيها.",
    },
    "cancel_pick": {
        "en": "Which viewing do you want to cancel?\n{items}\nSay its number.",
        "ar": "عايز تلغي أنهي معاينة؟\n{items}\nقولّي رقمها.",
    },
    "cancel_confirm": {
        "en": "Cancel your viewing of {property} on {when} (ref {ref})? (yes / no)",
        "ar": "ألغي معاينة {property} يوم {when} (رقم المرجع {ref})؟ (أيوه / لأ)",
    },
    "cancel_done": {
        "en": "Done - your viewing of {property} on {when} is cancelled. Tell me a new day if you'd like to rebook.",
        "ar": "تمام - اتلغت معاينة {property} يوم {when}. لو عايز تحجز معاد تاني قولّي اليوم.",
    },
    "cancel_kept": {
        "en": "OK, I've kept your viewing as it is.",
        "ar": "تمام، سيبت المعاينة زي ما هي.",
    },
    "cancel_failed": {
        "en": "Sorry, I couldn't cancel that right now. An advisor can do it for you - shall I request a call?",
        "ar": "معلش، مقدرتش ألغيها دلوقتي. مستشار يقدر يلغيها معاك - أطلبلك مكالمة؟",
    },
    "booking_status_none": {
        "en": "You don't have any viewing requests yet. Tell me which property you'd like to visit.",
        "ar": "لسه معندكش طلبات معاينة. قولّي عايز تعاين أنهي عقار.",
    },
    "booking_status_pending": {
        "en": "Your viewing of {property} on {when} (ref {ref}) is waiting for an advisor to confirm it.",
        "ar": "طلب معاينة {property} يوم {when} (رقم المرجع {ref}) مستني تأكيد من المستشار.",
    },
    "booking_status_confirmed": {
        "en": "Your viewing of {property} on {when} (ref {ref}) is confirmed.",
        "ar": "معاينة {property} يوم {when} (رقم المرجع {ref}) اتأكدت.",
    },
    "booking_confirmed_notice": {
        "en": "Good news! Your viewing of property {property} on {when} is confirmed (ref {ref}). An advisor will contact you if anything changes.",
        "ar": "خبر حلو! معاينة العقار {property} يوم {when} اتأكدت (رقم المرجع {ref}). المستشار هيتواصل معاك لو حصل أي تغيير.",
    },
    "booking_cancelled_notice": {
        "en": "We're sorry - we had to cancel your viewing of property {property} on {when} (ref {ref}). Reply here and I'll help you pick another time.",
        "ar": "معلش - اضطرينا نلغي معاينة العقار {property} يوم {when} (رقم المرجع {ref}). ردّ هنا وأنا أساعدك تختار معاد تاني.",
    },
    "schedule_booked": {
        "en": "Your viewing request for {property} on {when} is reserved (reference {ref}). An advisor will call you to confirm it.",
        "ar": "اتسجّل طلب معاينة {property} يوم {when} (رقم المرجع {ref}). مستشار هيكلمك يأكد المعاد.",
    },
    "schedule_conflict": {
        "en": "Sorry, that slot was just taken. Available times on {day}: {slots}.",
        "ar": "معلش، المعاد ده اتحجز حالاً. المواعيد المتاحة يوم {day}: {slots}.",
    },
    "schedule_cancelled": {"en": "No problem, I haven't booked anything. Anything else I can help with?", "ar": "ولا يهمك، محجزتش حاجة. أقدر أساعدك في حاجة تانية؟"},
    "schedule_unavailable": {
        "en": "Sorry, I can't access the viewing calendar right now. An advisor can arrange the visit with you — would you like me to request a call?",
        "ar": "معلش، مش قادر أوصل لجدول المعاينات دلوقتي. ممكن مستشار يرتّب المعاينة معاك — أطلبلك مكالمة؟",
    },
    "handoff_done": {
        "en": "I've passed your request to a sales advisor{contact}. They'll get in touch shortly with everything you've shared so far.",
        "ar": "حوّلت طلبك لمستشار مبيعات{contact}. هيتواصل معاك قريب ومعاه كل التفاصيل اللي قلتها.",
    },
    "handoff_need_contact": {
        "en": "Happy to connect you with a sales advisor. What phone number should they call?",
        "ar": "تمام، هوصّلك بمستشار مبيعات. يكلمك على أنهي رقم؟",
    },
    "sales_handoff_offer": {
        "en": "If you'd like, I can connect you with a sales advisor to discuss reservation and exact payment terms.",
        "ar": "لو تحب، أقدر أوصّلك بمستشار مبيعات يتكلم معاك في الحجز وتفاصيل الدفع بالظبط.",
    },
    "contact_saved": {"en": "Thanks, I've saved your contact details.", "ar": "شكراً، سجلت بيانات التواصل بتاعتك."},
}


def say(language: str, key: str, **values: object) -> str:
    template = PHRASES[key].get(language) or PHRASES[key]["en"]
    return template.format(**values) if values else template
