"""
Grounded response generation for the AI Real Estate Assistant.

RAG flow:

    Metadata Filtering
            ↓
    Hybrid Search
            ↓
    Reranking
            ↓
    Generation
            ↓
    Grounded Customer Response

Design principles:

- The LLM must not invent property IDs, prices, availability, payment
  plans, locations, or other property facts.
- Generation never performs retrieval itself.
- Only retrieved SearchResult objects are passed as evidence.
- Every response keeps traceability to retrieved property IDs and source files.
- Arabic and English are supported.
- Model and API settings are loaded from app.helpers.config.
- PostgreSQL is not accessed directly by this module.
- The LLM must never calculate, derive, estimate, or transform property facts.
- Financial values must only be shown when explicitly present in the evidence.
- Missing fields must remain missing; they must never be inferred.
- A post-generation safety layer removes unsupported financial claims.
- Normalized search requirements are provided to generation when available,
  so generation understands the criteria that produced the retrieved results.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..helpers.config import get_settings
from .retrieval.hybrid_search import SearchResult


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

settings = get_settings()

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 800


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class GenerationResult:
    """
    Final generated response plus traceability information.

    Attributes:
        answer_text:
            Customer-facing answer only.

        cited_property_ids:
            Property IDs explicitly cited by the model and verified against
            the retrieved evidence.

        grounding_property_ids:
            Property IDs provided to the model as evidence.

        model:
            Model used for generation.

        raw_response:
            Raw provider response when available.
    """

    answer_text: str
    cited_property_ids: list[str] = field(default_factory=list)
    grounding_property_ids: list[str] = field(default_factory=list)
    model: str = ""
    raw_response: dict[str, Any] | None = None


# Reply when retrieval found nothing. Deliberately fixed text, never model output.
_ARABIC_LETTER = re.compile(r"[؀-ۿ]")
_NO_MATCH_REPLY = {
    "en": (
        "I couldn't find any listings that match those details right now. "
        "You could adjust the budget, area or property type, or I can connect you with a sales advisor."
    ),
    "ar": (
        "للأسف مفيش عقارات مطابقة للتفاصيل دي دلوقتي. "
        "جرّب تغيّر الميزانية أو المنطقة أو نوع العقار، أو أوصّلك بمستشار مبيعات."
    ),
}


# ---------------------------------------------------------------------------
# Groq client
# ---------------------------------------------------------------------------

_client: Any = None


def _get_client() -> Any:
    """
    Lazily construct the Groq client.

    Lazy initialization keeps the module easy to import during tests and
    application startup without requiring an API key immediately.
    """
    global _client

    if _client is None:
        from groq import Groq

        api_key = settings.GROQ_API_KEY

        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")

        _client = Groq(api_key=api_key)

    return _client


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------


def _get_model_name() -> str:
    """Return the configured generation model with a safe fallback."""
    model_name = settings.GENERATION_MODEL or settings.GROQ_MODEL

    if not model_name:
        raise RuntimeError(
            "No generation model is configured. "
            "Set GENERATION_MODEL or GROQ_MODEL."
        )

    return model_name


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------



SYSTEM_PROMPT_TEMPLATE = """You are a real-estate sales assistant for a property company.

You help customers in Arabic or English.

LANGUAGE RULE:

- Always reply in the same language used by the customer.
- If the customer uses Egyptian Arabic, reply naturally in Egyptian Arabic.

GROUNDING SOURCE:

The RETRIEVED PROPERTIES section is the only authoritative source for
property facts in this request.

NORMALIZED SEARCH REQUIREMENTS:

The NORMALIZED SEARCH REQUIREMENTS section contains the structured criteria
that were used by the application to retrieve and filter the properties.

Use these requirements to understand what the retrieval pipeline was asked
to search for.

IMPORTANT:

- Normalized search requirements describe search criteria, not additional
  property facts.
- Never use a requirement value as a property attribute unless that same
  value is explicitly present in the retrieved property evidence.
- If the customer's original wording differs from the normalized search
  requirements, do not silently pretend the two are identical.
- When useful, explain that the displayed properties match the normalized
  search criteria used by the system.
- Do not invent why a requirement was normalized or transformed.

STRICT GROUNDING RULES:

- Use ONLY information explicitly present in RETRIEVED PROPERTIES for
  property facts.
- Never invent property IDs.
- Never invent prices.
- Never invent availability.
- Never invent payment plans.
- Never invent locations.
- Never invent bedrooms, bathrooms, areas, developers, compounds, finishing,
  delivery dates, or amenities.
- Never infer a missing property attribute.
- Never calculate, derive, estimate, interpolate, or transform property facts.
- Never calculate down payments.
- Never calculate installments.
- Never calculate percentages, deposits, fees, discounts, or payment plans.
- Never convert currencies or units unless the exact converted value is
  explicitly present in the evidence.
- If a field is missing, empty, or N/A, do not present a specific value
  for that field.
- Numerical values must be copied exactly from the evidence.

FINANCIAL INFORMATION:

- A price may only be reported if explicitly present.
- A down payment may only be reported if explicitly present.
- A payment plan may only be reported if explicitly present.
- Installments may only be reported if explicitly present.
- Deposits may only be reported if explicitly present.
- Fees may only be reported if explicitly present.
- Discounts may only be reported if explicitly present.
- Never derive one financial value from another.
- Never calculate a financial value from a percentage.
- Never calculate a percentage from two financial values.

PROPERTY MATCHING:

- Retrieved properties are the available evidence for this response.
- Inspect the retrieved properties before deciding whether relevant results
  exist.
- If retrieved properties satisfy the customer's request based on explicit
  evidence, present those properties directly.
- If retrieved properties satisfy the NORMALIZED SEARCH REQUIREMENTS, they
  may be presented as matching the search criteria, but do not claim that
  they satisfy any additional criterion that is unsupported by evidence.
- If retrieved properties only partially satisfy the request, clearly explain
  the limitation and present only what the evidence supports.
- If no properties are retrieved, explain that no matching properties were
  found and ask for a useful clarification.
- Do not invent alternatives when no properties were retrieved.
- Do not silently replace the customer's requested location with another
  location.
- If the location in the customer's request differs from the location shown
  in retrieved properties, explicitly acknowledge the difference.
- Do not claim that a property satisfies a requirement unless the evidence
  explicitly supports that claim or the application explicitly provided the
  requirement as a normalized retrieval criterion.

IMPORTANT:

The application may retrieve properties using structured requirements that
are not word-for-word identical to the customer's original query.

When this happens:

1. Treat the normalized requirements as the criteria actually used for
   retrieval.
2. Treat RETRIEVED PROPERTIES as the only source of property facts.
3. Do not invent equivalence between different location names.
4. If the original query and retrieved property location visibly differ,
   explain that difference accurately.
5. Do not conclude that there are no results merely because the original
   wording differs from the normalized search criteria.

RESPONSE FORMAT:

- Do NOT use Markdown tables.
- Do NOT create a table containing multiple properties.
- Do NOT reproduce all retrieved property fields in the response text.
- The application UI displays retrieved properties separately as property
  cards. Your job is to provide the conversational explanation around them.
- When multiple properties are retrieved, briefly summarize the result and
  let the property cards provide the detailed property information.
- Mention a property name, compound, or property ID only when it helps the
  customer understand the response.
- Do not repeat the same property information in both the conversational
  answer and the property cards.
- Use short paragraphs or simple bullet points only when they improve
  readability.
- Keep the response concise and natural.

EXAMPLES OF GOOD RESPONSE STYLE:

Arabic:

"لقيتلك 5 شقق متاحة للبيع مطابقة لطلبك. هتلاقي تفاصيل كل عقار تحت
الإجابة، ولو عايز أقارنلك بين كام شقة منهم ابعتلي أرقام العقارات."

English:

"I found 5 apartments for sale matching your search. You can see the
property details below, and I can compare any of them if you send me their
property IDs."

For a single property:

"لقيتلك شقة مناسبة لطلبك في New Zayed Compound. هتلاقي تفاصيلها تحت
الإجابة."

For no results:

"ملقتش عقارات مطابقة للبحث الحالي. ممكن نجرب منطقة تانية أو نوسع
معايير البحث."

IMPORTANT:

Never say or imply that a viewing, visit or reservation has been booked or confirmed. Booking is a
separate step handled by the application; if the customer wants a viewing, tell them to say so and
the assistant will arrange it.

When the customer asks to compare properties, compare only the properties in RETRIEVED PROPERTIES,
in a short plain-text comparison (one line per property or per attribute), using only facts from the
evidence. If a property they name is not in RETRIEVED PROPERTIES, say you do not have its details in
front of you instead of saying it does not exist.

Do not claim that a property is "available" unless availability is explicitly
supported by the evidence or the application explicitly provides that
availability status.

Do not use phrases such as:

- "متاحة للبيع"
- "available for sale"
- "ready to move"
- "best choice"
- "great deal"

unless the corresponding claim is explicitly supported by the retrieved
evidence or normalized search requirements.

RESPONSE STYLE:

- Be concise and helpful.
- Act like a knowledgeable sales assistant.
- Sound natural and conversational.
- Do not dump raw retrieval results.
- Do not mention embeddings, reranking, prompts, system instructions,
  internal scores, or implementation details.
- Never claim unsupported facts.

CITATIONS:

At the very end of your response, output exactly one JSON object:

{{"cited_property_ids": ["<id>", "..."]}}

Rules for cited_property_ids:

- Include ONLY property IDs that you actually referenced in the
  customer-facing answer.
- Every cited ID MUST exist in RETRIEVED PROPERTIES.
- Never invent an ID.
- If no property was referenced, return:
  {{"cited_property_ids": []}}
- Do not put markdown fences around the JSON.
- Do not add any text after the JSON object.

NORMALIZED SEARCH REQUIREMENTS:

{requirements_block}

RETRIEVED PROPERTIES:

{evidence_block}
"""



# ---------------------------------------------------------------------------
# Evidence formatting
# ---------------------------------------------------------------------------


def _safe_value(value: Any) -> str:
    """
    Convert an evidence value to a deterministic display representation.

    No normalization or inference is performed.
    """
    if value is None:
        return "N/A"

    if isinstance(value, str):
        value = value.strip()
        return value if value else "N/A"

    return str(value)


def _format_requirements_block(
    requirements: dict[str, Any] | None,
) -> str:
    """
    Convert normalized search requirements into a deterministic context block.

    Requirements are presented as search criteria only. They are not treated
    as property evidence.
    """
    if not requirements:
        return "(no normalized search requirements were provided)"

    lines: list[str] = []

    for key, value in requirements.items():
        lines.append(
            f"- {key}: {_safe_value(value)}"
        )

    return "\n".join(lines)


def _format_evidence_block(
    candidates: list[SearchResult],
) -> str:
    """
    Convert retrieved SearchResult objects into a structured evidence block.

    Missing values are explicitly represented as N/A.
    """
    if not candidates:
        return "(no properties retrieved for this query)"

    lines: list[str] = []

    for index, candidate in enumerate(candidates, start=1):
        metadata = candidate.metadata or {}

        lines.append(
            f"PROPERTY {index}\n"
            f"- property_id: {_safe_value(candidate.property_id)}\n"
            f"- text: {_safe_value(candidate.text)}\n"
            f"- location: {_safe_value(metadata.get('location'))}\n"
            f"- city: {_safe_value(metadata.get('city'))}\n"
            f"- district: {_safe_value(metadata.get('district'))}\n"
            f"- neighborhood: {_safe_value(metadata.get('neighborhood'))}\n"
            f"- compound: {_safe_value(metadata.get('compound'))}\n"
            f"- developer: {_safe_value(metadata.get('developer'))}\n"
            f"- property_type: {_safe_value(metadata.get('property_type'))}\n"
            f"- transaction_type: {_safe_value(metadata.get('transaction_type'))}\n"
            f"- price: {_safe_value(metadata.get('price'))}\n"
            f"- currency: {_safe_value(metadata.get('currency'))}\n"
            f"- area: {_safe_value(metadata.get('area'))}\n"
            f"- area_unit: {_safe_value(metadata.get('area_unit'))}\n"
            f"- bedrooms: {_safe_value(metadata.get('bedrooms'))}\n"
            f"- bathrooms: {_safe_value(metadata.get('bathrooms'))}\n"
            f"- payment_plan: {_safe_value(metadata.get('payment_plan'))}\n"
            f"- availability: {_safe_value(metadata.get('availability'))}\n"
            f"- finishing: {_safe_value(metadata.get('finishing'))}\n"
            f"- delivery: {_safe_value(metadata.get('delivery'))}\n"
            f"- amenities: {_safe_value(metadata.get('amenities'))}\n"
            f"- source_file: {_safe_value(candidate.source_file)}"
        )

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def _validate_conversation_history(
    conversation_history: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """
    Validate and clean conversation history before sending it to the LLM.

    Only user and assistant messages are accepted.

    System messages are excluded because this module owns the system prompt.
    """
    if not conversation_history:
        return []

    valid_messages: list[dict[str, str]] = []

    for message in conversation_history:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        content = message.get("content")

        if role not in {"user", "assistant"}:
            continue

        if not isinstance(content, str) or not content.strip():
            continue

        valid_messages.append(
            {
                "role": role,
                "content": content.strip(),
            }
        )

    return valid_messages


# ---------------------------------------------------------------------------
# Channels without property cards
# ---------------------------------------------------------------------------
# The website shows every retrieved property as a card under the answer, so the standard prompt
# tells the model to summarise and leave the details to the cards. WhatsApp is plain text: there
# are no cards, so the application appends a numbered list built from the stored listing data and
# the model must write only the short conversational part.
_FORMAT_START = SYSTEM_PROMPT_TEMPLATE.index("RESPONSE FORMAT:")
_FORMAT_END = SYSTEM_PROMPT_TEMPLATE.index("IMPORTANT:", _FORMAT_START)
_TEXT_ONLY_FORMAT = """RESPONSE FORMAT:

- Do NOT use Markdown tables.
- This channel is plain text and shows NO property cards. The application adds a numbered list of
  the retrieved properties (type, place, area, bedrooms, price, ID) right after your answer.
- Therefore do NOT list the properties yourself, do NOT repeat their prices or areas, and NEVER
  mention "cards", "below", "تحت" or "البطاقات".
- Write 1 to 3 short sentences: how many properties matched, what they have in common, and one
  useful next step.
- When the customer asks to compare, give a short plain-text comparison of the properties in
  RETRIEVED PROPERTIES (one line per property), using only facts from the evidence.
- Keep the response concise and natural.

EXAMPLES OF GOOD RESPONSE STYLE:

Arabic:

"لقيتلك 5 شقق مطابقة لطلبك. دي القايمة، ولو عايز تفاصيل عقار قولّي رقمه أو كوده."

English:

"I found 5 apartments matching your search. Here is the list; tell me a number or ID for details."

For no results:

"ملقتش عقارات مطابقة للبحث الحالي. ممكن نجرب منطقة تانية أو نوسع معايير البحث."

"""
SYSTEM_PROMPT_TEXT_ONLY = (
    SYSTEM_PROMPT_TEMPLATE[:_FORMAT_START] + _TEXT_ONLY_FORMAT + SYSTEM_PROMPT_TEMPLATE[_FORMAT_END:]
)


def build_messages(
    customer_query: str,
    candidates: list[SearchResult],
    requirements: dict[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    channel: str | None = None,
) -> list[dict[str, str]]:
    """
    Build the Groq chat-completion messages array.

    Retrieved evidence is placed inside the system message so the model
    treats it as authoritative property evidence for this request.

    Normalized requirements are also included as search context when
    available, but they are explicitly distinguished from property evidence.
    """
    if not isinstance(customer_query, str) or not customer_query.strip():
        raise ValueError("customer_query must not be empty.")

    evidence_block = _format_evidence_block(candidates)
    requirements_block = _format_requirements_block(requirements)

    template = SYSTEM_PROMPT_TEXT_ONLY if channel == "whatsapp" else SYSTEM_PROMPT_TEMPLATE
    system_prompt = template.format(
        requirements_block=requirements_block,
        evidence_block=evidence_block,
    )

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    messages.extend(
        _validate_conversation_history(conversation_history)
    )

    messages.append(
        {
            "role": "user",
            "content": customer_query.strip(),
        }
    )

    return messages


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------


_CITATION_PATTERN = re.compile(
    r'\{\s*"cited_property_ids"\s*:\s*\[[^\]]*\]\s*\}',
    re.DOTALL,
)


def _extract_cited_ids(
    raw_text: str,
) -> tuple[str, list[str]]:
    """
    Split model output into:

        (customer-facing answer, cited_property_ids)

    The citation JSON must be present at the end of the model response.
    """
    if not raw_text:
        return "", []

    matches = list(_CITATION_PATTERN.finditer(raw_text))

    if not matches:
        return raw_text.strip(), []

    match = matches[-1]
    answer = raw_text[:match.start()].strip()

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning(
            "generation.py: failed to parse citation JSON."
        )
        return answer, []

    if not isinstance(parsed, dict):
        return answer, []

    if set(parsed.keys()) != {"cited_property_ids"}:
        logger.warning(
            "generation.py: citation JSON has an unexpected schema."
        )
        return answer, []

    cited_ids = parsed.get("cited_property_ids")

    if not isinstance(cited_ids, list):
        return answer, []

    normalized_ids: list[str] = []

    for property_id in cited_ids:
        if not isinstance(property_id, (str, int)):
            continue

        normalized_id = str(property_id).strip()

        if normalized_id:
            normalized_ids.append(normalized_id)

    return answer, list(dict.fromkeys(normalized_ids))


# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------


def _validate_citations(
    cited_ids: list[str],
    candidates: list[SearchResult],
) -> list[str]:
    """
    Keep only citation IDs that exist in the retrieved evidence.

    This is a hard safety boundary between model output and traceability.
    """
    grounding_ids = list(
        dict.fromkeys(
            str(candidate.property_id).strip()
            for candidate in candidates
            if (
                candidate.property_id is not None
                and str(candidate.property_id).strip()
            )
        )
    )

    grounding_set = set(grounding_ids)

    valid_ids: list[str] = []
    unsupported_ids: list[str] = []

    for property_id in cited_ids:
        normalized_id = str(property_id).strip()

        if normalized_id in grounding_set:
            if normalized_id not in valid_ids:
                valid_ids.append(normalized_id)
        else:
            unsupported_ids.append(normalized_id)

    if unsupported_ids:
        logger.warning(
            "Model cited property IDs absent from retrieved evidence: %s",
            unsupported_ids,
        )

    return valid_ids


# ---------------------------------------------------------------------------
# Financial evidence validation
# ---------------------------------------------------------------------------


_FINANCIAL_FIELDS = (
    "down_payment",
    "payment_plan",
    "installments",
    "deposit",
    "fees",
    "discount",
)


def _has_explicit_field(
    candidates: list[SearchResult],
    field_name: str,
) -> bool:
    """
    Return True only when at least one retrieved property explicitly contains
    a non-empty value for the requested field.

    No value is inferred from another field.

    NOTE:
        This helper is kept for backward compatibility with existing callers.
        Financial claim sanitization below does NOT rely on this function for
        property-specific validation.
    """
    for candidate in candidates:
        metadata = candidate.metadata or {}
        value = metadata.get(field_name)

        if value is None:
            continue

        if isinstance(value, str):
            if value.strip() and value.strip().upper() != "N/A":
                return True
        else:
            return True

    return False


def _has_explicit_property_field(
    candidate: SearchResult,
    field_name: str,
) -> bool:
    """
    Return True only when the specific property contains an explicit value
    for the requested field.

    This prevents a financial field belonging to one property from
    authorizing the same claim for another property.
    """
    metadata = candidate.metadata or {}
    value = metadata.get(field_name)

    if value is None:
        return False

    if isinstance(value, str):
        normalized = value.strip()

        if not normalized:
            return False

        if normalized.upper() == "N/A":
            return False

    return True


def _property_ids_in_text(
    text: str,
    candidates: list[SearchResult],
) -> list[str]:
    """
    Return retrieved property IDs explicitly mentioned in the text.

    IDs are matched against retrieved evidence only.
    """
    if not text:
        return []

    property_ids: list[str] = []

    for candidate in candidates:
        if candidate.property_id is None:
            continue

        property_id = str(candidate.property_id).strip()

        if not property_id:
            continue

        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(property_id)}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        ):
            property_ids.append(property_id)

    return property_ids


def _financial_field_has_evidence_for_text(
    text: str,
    field_name: str,
    candidates: list[SearchResult],
) -> bool:
    """
    Determine whether a financial field is explicitly supported for the
    property referenced by a piece of generated text.

    Rules:

    - If the text explicitly names one or more retrieved properties, the
      field must exist on at least one of those referenced properties.
    - If no property ID is present, the field is considered unsupported for
      post-generation safety purposes.
    """
    referenced_ids = _property_ids_in_text(
        text=text,
        candidates=candidates,
    )

    if not referenced_ids:
        return False

    candidate_by_id = {
        str(candidate.property_id).strip(): candidate
        for candidate in candidates
        if (
            candidate.property_id is not None
            and str(candidate.property_id).strip()
        )
    }

    for property_id in referenced_ids:
        candidate = candidate_by_id.get(property_id)

        if candidate is None:
            continue

        if _has_explicit_property_field(
            candidate,
            field_name,
        ):
            return True

    return False


def _remove_markdown_table_column(
    text: str,
    header_pattern: re.Pattern[str],
) -> str:
    """
    Remove a markdown table column when the corresponding evidence field
    does not exist.

    This is intentionally conservative and only operates on markdown tables.
    """
    lines = text.splitlines()

    table_indexes = [
        index
        for index, line in enumerate(lines)
        if "|" in line
    ]

    if len(table_indexes) < 2:
        return text

    for index in table_indexes:
        line = lines[index]

        if not header_pattern.search(line):
            continue

        cells = [
            cell.strip()
            for cell in line.strip().strip("|").split("|")
        ]

        column_index = None

        for cell_index, cell in enumerate(cells):
            if header_pattern.search(cell):
                column_index = cell_index
                break

        if column_index is None:
            continue

        start = index
        end = index

        while start > 0 and "|" in lines[start - 1]:
            start -= 1

        while end + 1 < len(lines) and "|" in lines[end + 1]:
            end += 1

        for row_index in range(start, end + 1):
            row = lines[row_index]

            if "|" not in row:
                continue

            row_cells = [
                cell.strip()
                for cell in row.strip().strip("|").split("|")
            ]

            if column_index >= len(row_cells):
                continue

            del row_cells[column_index]

            lines[row_index] = "| " + " | ".join(row_cells) + " |"

    return "\n".join(lines)


def _has_money_value(text: str) -> bool:
    """
    Detect whether a generated statement contains an explicit monetary value.

    Property IDs such as A102 or B205 must NOT be treated as monetary values.

    A value is considered monetary only when:
    - It has an explicit currency symbol/code, or
    - It has a percentage sign.

    Examples:
        "A102 has an installment payment plan."
            -> False

        "A102 has monthly installments of 50,000 EGP."
            -> True

        "Down payment: 480,000 EGP"
            -> True

        "Discount: 10%"
            -> True
    """
    if not text:
        return False

    return bool(
        re.search(
            r"""
            (?:
                # Currency before the number:
                # EGP 480,000
                # USD 10,000
                # $50,000
                (?:EGP|USD|EUR|AED|GBP|\$|€|£)
                \s*
                \d[\d,.\s]*

                |

                # Currency after the number:
                # 480,000 EGP
                # 10,000 USD
                \d[\d,.\s]*
                \s*
                (?:EGP|USD|EUR|AED|GBP|\$|€|£)

                |

                # Percentage:
                # 10%
                \d[\d,.\s]*
                \s*%
            )
            """,
            text,
            re.IGNORECASE | re.VERBOSE,
        )
    )


def _remove_unsupported_financial_claims(
    answer_text: str,
    candidates: list[SearchResult],
) -> str:
    """
    Remove financial claims that are not explicitly supported by evidence.

    This is a post-generation safety layer. It exists because an LLM can
    occasionally violate a prompt rule even when the rule is explicit.

    Safety rules:

    - payment_plan text such as "installment" is allowed when payment_plan
      exists in the evidence.
    - "installment" as a descriptive payment-plan word must not be treated
      as an unsupported installment amount.
    - A payment-plan value must never authorize a down payment, installment
      amount, deposit, fee, discount, or other financial amount.
    - Property-specific financial fields are validated against the specific
      property mentioned in the generated claim.
    - Unsupported financial table columns are removed when the field is not
      explicitly represented in the retrieved evidence.
    - Unsupported financial standalone statements are removed.
    """
    if not answer_text:
        return answer_text

    sanitized = answer_text

    field_patterns: dict[str, re.Pattern[str]] = {
        "down_payment": re.compile(
            r"(down[\s-]?payment|down payment|مقدم|دفعة\s+مقدمة)",
            re.IGNORECASE,
        ),
        "payment_plan": re.compile(
            r"(payment\s+plan|payment\s+plans|خطة\s+السداد|نظام\s+السداد)",
            re.IGNORECASE,
        ),
        "installments": re.compile(
            r"(installment|installments|قسط|أقساط)",
            re.IGNORECASE,
        ),
        "deposit": re.compile(
            r"(deposit|حجز|وديعة)",
            re.IGNORECASE,
        ),
        "fees": re.compile(
            r"(fees?|رسوم|مصاريف)",
            re.IGNORECASE,
        ),
        "discount": re.compile(
            r"(discount|discounts|خصم|خصومات)",
            re.IGNORECASE,
        ),
    }

    # -----------------------------------------------------------------------
    # 1. Remove unsupported financial table columns.
    # -----------------------------------------------------------------------

    for field_name, pattern in field_patterns.items():
        has_any_evidence = _has_explicit_field(
            candidates,
            field_name,
        )

        if has_any_evidence:
            continue

        sanitized = _remove_markdown_table_column(
            sanitized,
            pattern,
        )

    # -----------------------------------------------------------------------
    # 2. Remove unsupported financial standalone lines/statements.
    # -----------------------------------------------------------------------

    lines = sanitized.splitlines()
    filtered_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            filtered_lines.append(line)
            continue

        # Markdown table rows have already been processed above.
        if "|" in stripped:
            filtered_lines.append(line)
            continue

        removed = False

        for field_name, pattern in field_patterns.items():
            if not pattern.search(stripped):
                continue

            # ---------------------------------------------------------------
            # Detect monetary values BEFORE descriptive payment-plan
            # exceptions.
            #
            # IMPORTANT:
            # Property IDs such as A102 contain digits, but are NOT money.
            # _has_money_value() therefore only recognizes values accompanied
            # by an explicit currency or percentage sign.
            # ---------------------------------------------------------------
            has_money_value = _has_money_value(stripped)

            # ---------------------------------------------------------------
            # installments:
            #
            # "installment" can be a descriptive payment-plan word.
            #
            # It is allowed only when:
            #
            # 1. The statement contains NO monetary amount.
            # 2. The referenced property explicitly has payment_plan.
            #
            # If a numeric amount exists, this branch is skipped and the
            # amount must be supported by the explicit "installments" field.
            # ---------------------------------------------------------------
            if (
                field_name == "installments"
                and not has_money_value
            ):
                payment_plan_pattern = field_patterns["payment_plan"]

                if payment_plan_pattern.search(stripped):
                    if _financial_field_has_evidence_for_text(
                        text=stripped,
                        field_name="payment_plan",
                        candidates=candidates,
                    ):
                        continue

                referenced_ids = _property_ids_in_text(
                    text=stripped,
                    candidates=candidates,
                )

                if referenced_ids:
                    if _financial_field_has_evidence_for_text(
                        text=stripped,
                        field_name="payment_plan",
                        candidates=candidates,
                    ):
                        continue

            # ---------------------------------------------------------------
            # payment_plan:
            #
            # Descriptive payment-plan text is allowed when explicitly
            # present in the evidence.
            #
            # However, a numeric financial claim must NEVER be authorized
            # merely because payment_plan contains a descriptive value such
            # as "installment".
            #
            # Therefore this exception only applies when the statement has
            # no monetary value.
            # ---------------------------------------------------------------
            if (
                field_name == "payment_plan"
                and not has_money_value
            ):
                if _financial_field_has_evidence_for_text(
                    text=stripped,
                    field_name="payment_plan",
                    candidates=candidates,
                ):
                    continue

            # ---------------------------------------------------------------
            # Financial amount.
            #
            # A monetary claim must be supported by the specific financial
            # field on the specific referenced property.
            # ---------------------------------------------------------------
            if has_money_value:
                if _financial_field_has_evidence_for_text(
                    text=stripped,
                    field_name=field_name,
                    candidates=candidates,
                ):
                    continue

                logger.warning(
                    "Removed unsupported financial claim for field '%s'.",
                    field_name,
                )
                removed = True
                break

            # ---------------------------------------------------------------
            # For non-payment-plan fields, remove short standalone claims
            # even when they do not contain an explicit number.
            #
            # Examples:
            #
            #   Discount available
            #   Fees apply
            #
            # Only remove these when the field is unsupported globally.
            # ---------------------------------------------------------------
            if field_name != "payment_plan":
                if not _has_explicit_field(
                    candidates,
                    field_name,
                ):
                    if len(stripped.split()) <= 12:
                        logger.warning(
                            "Removed unsupported financial statement for "
                            "field '%s'.",
                            field_name,
                        )
                        removed = True
                        break

        if not removed:
            filtered_lines.append(line)

    sanitized = "\n".join(filtered_lines)

    # -----------------------------------------------------------------------
    # 3. Clean excessive blank lines introduced by removed claims.
    # -----------------------------------------------------------------------

    sanitized = re.sub(
        r"\n{3,}",
        "\n\n",
        sanitized,
    )

    return sanitized.strip()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_generation_parameters(
    temperature: float,
    max_tokens: int,
) -> None:
    """Validate generation parameters before calling the provider."""
    if not isinstance(temperature, (int, float)):
        raise TypeError("temperature must be a number.")

    if not 0 <= float(temperature) <= 2:
        raise ValueError("temperature must be between 0 and 2.")

    if not isinstance(max_tokens, int):
        raise TypeError("max_tokens must be an integer.")

    if max_tokens <= 0:
        raise ValueError(
            "max_tokens must be greater than 0."
        )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_response(
    customer_query: str,
    candidates: list[SearchResult],
    requirements: dict[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    channel: str | None = None,
) -> GenerationResult:
    """
    Generate a response grounded only in the supplied retrieved results.

    Args:
        customer_query:
            Original customer request.

        candidates:
            Final retrieved/reranked property evidence.

        requirements:
            Structured/normalized search requirements used by the retrieval
            pipeline. Optional for backward compatibility with existing
            callers and tests.

        conversation_history:
            Optional previous user/assistant messages.

        temperature:
            Provider generation temperature.

        max_tokens:
            Maximum generated tokens.

    This function does not perform retrieval or access PostgreSQL.
    """
    _validate_generation_parameters(
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # No evidence, no model call. Asking an LLM to answer a property question
    # from an empty evidence block invites it to make listings up, and a prompt
    # instruction is not a guarantee. The reply is fixed text instead.
    if not candidates:
        if not isinstance(customer_query, str) or not customer_query.strip():
            raise ValueError("customer_query must not be empty.")
        return GenerationResult(
            answer_text=_NO_MATCH_REPLY["ar" if _ARABIC_LETTER.search(customer_query) else "en"],
            cited_property_ids=[],
            grounding_property_ids=[],
            model="none (no evidence retrieved)",
            raw_response=None,
        )

    messages = build_messages(
        customer_query=customer_query,
        candidates=candidates,
        requirements=requirements,
        conversation_history=conversation_history,
        channel=channel,
    )

    model_name = _get_model_name()

    completion = _get_client().chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=float(temperature),
        max_tokens=max_tokens,
    )

    if not completion.choices:
        raise RuntimeError(
            "Generation provider returned no completion choices."
        )

    raw_text = (
        completion.choices[0].message.content
        or ""
    )

    answer_text, cited_ids = _extract_cited_ids(raw_text)

    # -----------------------------------------------------------------------
    # Hard post-generation grounding guardrail.
    #
    # The prompt tells the LLM not to invent financial values, but the
    # application must not rely on prompt compliance alone.
    # -----------------------------------------------------------------------

    sanitized_answer = _remove_unsupported_financial_claims(
        answer_text=answer_text,
        candidates=candidates,
    )

    if sanitized_answer != answer_text:
        logger.warning(
            "generation.py: removed unsupported financial claims "
            "from model output."
        )

    grounding_ids = list(
        dict.fromkeys(
            str(candidate.property_id).strip()
            for candidate in candidates
            if (
                candidate.property_id is not None
                and str(candidate.property_id).strip()
            )
        )
    )

    grounded_citations = _validate_citations(
        cited_ids=cited_ids,
        candidates=candidates,
    )

    raw_response: dict[str, Any] | None = None

    if hasattr(completion, "model_dump"):
        raw_response = completion.model_dump()

    return GenerationResult(
        answer_text=sanitized_answer,
        cited_property_ids=grounded_citations,
        grounding_property_ids=grounding_ids,
        model=model_name,
        raw_response=raw_response,
    )