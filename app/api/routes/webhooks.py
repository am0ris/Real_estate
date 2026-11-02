"""Webhook endpoints for external communication providers.

Two WhatsApp transports are supported and both run the shared LangGraph
agent workflow with one conversation thread per phone number:

* ``/whatsapp/webhook`` - Meta WhatsApp Cloud API (official, signed webhooks).
* ``/internal/whatsapp/*`` - the optional open-wa gateway in
  ``app/channels/whatsapp.ts`` (bearer-secret authenticated).

Inbound voice notes are transcribed (Groq Whisper) and answered as text;
real-time WhatsApp calls are not supported by either transport.

Delivery guarantees: providers deliver at least once and retry on slow or failed
responses, so every message id is claimed atomically in PostgreSQL before it is
processed (see ``app.database.replay``). A claim survives restarts and is shared
by all workers; a message whose processing failed, or whose worker died, can be
claimed again by the provider's retry instead of being lost.
"""

import base64
import binascii
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...channels.whatsapp import (
    extract_incoming_messages,
    download_media,
    is_valid_signature,
    send_text_message,
    transcribe_audio,
    verify_webhook_challenge,
)
from app.agents.messages import say
from app.agents.service import conversation_service
from app.api.ratelimit import allow_whatsapp_sender, limit_webhook
from app.database import replay
from app.helpers.config import get_settings
from app.helpers.phone import mask_phone
from app.observability.metrics import record_request


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])
gateway_router = APIRouter(prefix="/internal/whatsapp", tags=["whatsapp"], include_in_schema=False)

MAX_AUDIO_BYTES = 16 * 1024 * 1024

# Meta redelivers for days; remember ids this long so a late retry stays a duplicate.
DEDUP_TTL_SECONDS = 7 * 24 * 3600
# A claim still "processing" after this long belongs to a worker that died.
STALE_CLAIM_SECONDS = 300

_UNSUPPORTED_NOTICE = {
    "en": "I can read text and voice messages. Please type your request or send a voice note.",
    "ar": "أقدر أقرأ الرسائل النصية والصوتية. اكتبلي طلبك أو ابعت رسالة صوتية.",
}


async def _claim(provider: str, message_id: str) -> bool:
    """True if this delivery should be processed (first time, or a retake)."""
    return await run_in_threadpool(
        replay.claim, provider, message_id, ttl_seconds=DEDUP_TTL_SECONDS, stale_after_seconds=STALE_CLAIM_SECONDS
    )


async def _settle(provider: str, message_id: str, *, ok: bool) -> None:
    await run_in_threadpool(replay.settle, provider, message_id, ok=ok)


def whatsapp_thread(sender: str) -> str:
    return "whatsapp:" + "".join(ch for ch in sender if ch.isdigit() or ch == "+")


def run_whatsapp_turn(sender: str, text: str, modality: str) -> str:
    """Run one WhatsApp message through the agent graph and return the reply."""
    thread_id = whatsapp_thread(sender)
    started = time.monotonic()
    try:
        result = conversation_service.run_turn(
            thread_id,
            text,
            channel="whatsapp",
            customer_id=thread_id,
            modality=modality,
        )
    except Exception:
        record_request("whatsapp_turn", "error", time.monotonic() - started)
        raise
    if result.error:
        logger.warning(
            "WhatsApp turn degraded | sender=%s error=%s",
            mask_phone(sender),
            result.error,
        )
        record_request("whatsapp_turn", "degraded", time.monotonic() - started)
    else:
        record_request("whatsapp_turn", "ok", time.monotonic() - started)
    return result.response


async def process_whatsapp_message(message: dict[str, str]) -> None:
    """Run an accepted Cloud API message through the agents and send the answer."""
    try:
        if message["type"] == "unsupported":
            await send_text_message(message["from"], f"{_UNSUPPORTED_NOTICE['ar']}\n{_UNSUPPORTED_NOTICE['en']}")
            await _settle("whatsapp", message["id"], ok=True)
            return
        if message["type"] == "audio":
            audio = await download_media(message["media_id"])
            customer_text = await run_in_threadpool(transcribe_audio, audio)
            modality = "voice_note"
        else:
            customer_text = message["text"]
            modality = "text"

        reply = await run_in_threadpool(run_whatsapp_turn, message["from"], customer_text, modality)
        await send_text_message(message["from"], reply)
        await _settle("whatsapp", message["id"], ok=True)
    except Exception:
        logger.exception("WhatsApp workflow failed | message_id=%s sender=%s", message["id"], mask_phone(message["from"]))
        # Let Meta's redelivery try again, and tell the customer meanwhile
        # instead of leaving them without any answer.
        await _settle("whatsapp", message["id"], ok=False)
        try:
            notice = f"{say('ar', 'search_unavailable')}\n{say('en', 'search_unavailable')}"
            await send_text_message(message["from"], notice)
        except Exception:
            logger.exception("WhatsApp failure notice could not be sent | message_id=%s", message["id"])


@router.get("/webhook", response_class=Response)
async def verify_whatsapp_webhook(
    request: Request,
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Complete Meta's webhook-verification handshake."""
    limit_webhook(request)
    verified_challenge = verify_webhook_challenge(mode, verify_token, challenge)
    if verified_challenge is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verify token")
    return Response(content=verified_challenge, media_type="text/plain")


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Validate and acknowledge WhatsApp Cloud API events quickly."""
    limit_webhook(request)
    body = await request.body()
    if not is_valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload: dict[str, Any] = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from error

    expected_number = get_settings().WHATSAPP_PHONE_NUMBER_ID
    for message in extract_incoming_messages(payload):
        # A signed event for another number on the same Meta app is not ours.
        if expected_number and message.get("phone_number_id") not in (None, expected_number):
            logger.info("Ignoring WhatsApp event for another phone number id")
            continue
        if not allow_whatsapp_sender(message["from"]):
            logger.warning("Dropping WhatsApp flood from %s", mask_phone(message["from"]))
            continue
        if not await _claim("whatsapp", message["id"]):
            logger.info("Skipping duplicate WhatsApp delivery | message_id=%s", message["id"])
            continue
        logger.info("Received WhatsApp %s message (id=%s)", message["type"], message["id"])
        background_tasks.add_task(process_whatsapp_message, message)

    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# open-wa gateway (app/channels/whatsapp.ts)
# ---------------------------------------------------------------------------


class GatewayText(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    sender: str = Field(min_length=3, max_length=64)
    type: str = "text"
    text: str = Field(min_length=1, max_length=4_000)


class GatewayAudio(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    sender: str = Field(min_length=3, max_length=64)
    mime_type: str = Field(default="audio/ogg", max_length=100)
    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BYTES * 4 // 3 + 4)


def _require_gateway(authorization: str | None) -> None:
    secret = get_settings().WHATSAPP_GATEWAY_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="WhatsApp gateway is not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid gateway credentials")


def _gateway_sender(sender: str) -> str:
    # open-wa ids look like "201001234567@c.us".
    return sender.split("@", 1)[0]


@gateway_router.post("/message")
async def gateway_message(
    payload: GatewayText, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    limit_webhook(request)
    _require_gateway(authorization)
    sender = _gateway_sender(payload.sender)
    if not allow_whatsapp_sender(sender):
        raise HTTPException(status_code=429, detail="Too many messages", headers={"Retry-After": "30"})
    if not await _claim("whatsapp_gateway", payload.message_id):
        return {"reply": None, "duplicate": True}
    try:
        reply = await run_in_threadpool(run_whatsapp_turn, sender, payload.text, "text")
    except Exception:
        await _settle("whatsapp_gateway", payload.message_id, ok=False)
        raise
    await _settle("whatsapp_gateway", payload.message_id, ok=True)
    return {"reply": reply}


@gateway_router.post("/audio")
async def gateway_audio(
    payload: GatewayAudio, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    limit_webhook(request)
    _require_gateway(authorization)
    if not payload.mime_type.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Unsupported media type")
    sender = _gateway_sender(payload.sender)
    if not allow_whatsapp_sender(sender):
        raise HTTPException(status_code=429, detail="Too many messages", headers={"Retry-After": "30"})
    try:
        audio = base64.b64decode(payload.audio_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail="Invalid audio encoding") from error
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Voice note too large")
    if not await _claim("whatsapp_gateway", payload.message_id):
        return {"reply": None, "duplicate": True}
    try:
        text = await run_in_threadpool(transcribe_audio, audio)
    except Exception as error:
        logger.exception("Voice-note transcription failed | message_id=%s", payload.message_id)
        await _settle("whatsapp_gateway", payload.message_id, ok=False)
        raise HTTPException(status_code=503, detail="Transcription unavailable") from error
    try:
        reply = await run_in_threadpool(run_whatsapp_turn, sender, text, "voice_note")
    except Exception:
        await _settle("whatsapp_gateway", payload.message_id, ok=False)
        raise
    await _settle("whatsapp_gateway", payload.message_id, ok=True)
    return {"reply": reply, "transcript": text}
