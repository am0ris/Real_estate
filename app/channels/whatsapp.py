"""Meta WhatsApp Cloud API webhook channel.

The webhook deliberately acknowledges messages quickly. A background worker
should download and transcode inbound voice notes before passing PCM audio to
``app.voice.gemini_live``; doing that work in the webhook request risks Meta
retrying the event.
"""

import hashlib
import hmac
import logging
from typing import Any

import httpx

from ..helpers.config import get_settings

logger = logging.getLogger(__name__)

# WhatsApp rejects a text body over 4096 characters; stay well under it.
MAX_MESSAGE_CHARS = 3800


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Cut a long reply into several messages at paragraph, line or word boundaries.

    A single message is never cut mid-sentence; text that fits is returned as one part.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:  # no sensible boundary: hard cut rather than loop forever
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [part for part in parts if part]


def verify_webhook_challenge(
    mode: str | None, verify_token: str | None, challenge: str | None
) -> str | None:
    """Return Meta's verification challenge when the request is valid."""
    settings = get_settings()
    if (
        mode == "subscribe"
        and challenge is not None
        and settings.WHATSAPP_VERIFY_TOKEN
        and hmac.compare_digest(verify_token or "", settings.WHATSAPP_VERIFY_TOKEN)
    ):
        return challenge
    return None


def is_valid_signature(body: bytes, signature: str | None) -> bool:
    """Check Meta's SHA-256 webhook signature without exposing its secret."""
    secret = get_settings().WHATSAPP_APP_SECRET
    if not secret:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return bool(signature and hmac.compare_digest(signature, expected))


# Message types the assistant cannot read; the customer gets a short notice.
_UNREADABLE_TYPES = {"image", "video", "document", "sticker", "location", "contacts", "interactive", "button"}


def extract_incoming_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract inbound WhatsApp messages from a webhook payload.

    Text and voice notes are returned for processing; media and other types the
    assistant cannot read come back as ``type="unsupported"`` so the customer
    can be told rather than ignored. Reactions, system events and delivery
    ``statuses`` are skipped.
    """
    messages: list[dict[str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            for message in value.get("messages", []):
                message_type = message.get("type")
                sender = message.get("from")
                message_id = message.get("id")
                if not sender or not message_id:
                    continue
                if message_type in _UNREADABLE_TYPES:
                    extracted = {"id": message_id, "from": sender, "type": "unsupported"}
                elif message_type in {"audio", "text"}:
                    extracted = {"id": message_id, "from": sender, "type": message_type}
                    if message_type == "text":
                        text = message.get("text", {}).get("body")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        extracted["text"] = text.strip()
                    else:
                        # The media id lives under the "audio" object, distinct from
                        # the message's own id; it is needed to download the note.
                        media_id = message.get("audio", {}).get("id")
                        if not media_id:
                            continue
                        extracted["media_id"] = media_id
                else:
                    continue
                if phone_number_id:
                    extracted["phone_number_id"] = str(phone_number_id)
                messages.append(extracted)
    return messages


async def send_text_message(recipient: str, text: str) -> None:
    """Send a WhatsApp Cloud API text response."""
    settings = get_settings()
    if not settings.WHATSAPP_ACCESS_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WhatsApp sending credentials are not configured")
    url = (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        for part in split_message(text):
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": part},
            }
            response = await client.post(url, headers=headers, json=payload)
            if getattr(response, "is_error", False):
                # raise_for_status() hides Meta's reason (e.g. 131030: number not in the allowed list).
                logger.error("WhatsApp send failed: status=%s body=%s", response.status_code, response.text[:300])
            response.raise_for_status()


async def download_media(media_id: str) -> bytes:
    """Resolve and download WhatsApp media using the configured access token."""
    settings = get_settings()
    if not settings.WHATSAPP_ACCESS_TOKEN:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    metadata_url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{media_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        metadata_response = await client.get(metadata_url, headers=headers)
        metadata_response.raise_for_status()
        media_url = metadata_response.json().get("url")
        if not media_url:
            raise RuntimeError("WhatsApp media response did not include a URL")
        media_response = await client.get(media_url, headers=headers)
        media_response.raise_for_status()
        return media_response.content


def transcribe_audio(audio: bytes) -> str:
    """Transcribe a downloaded WhatsApp voice note through Groq Whisper."""
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")
    from groq import Groq

    transcription = Groq(api_key=settings.GROQ_API_KEY).audio.transcriptions.create(
        file=("voice-note.ogg", audio),
        model=settings.WHATSAPP_TRANSCRIPTION_MODEL,
        response_format="text",
    )
    text = transcription if isinstance(transcription, str) else getattr(transcription, "text", "")
    if not text or not text.strip():
        raise RuntimeError("Voice-note transcription was empty")
    return text.strip()
