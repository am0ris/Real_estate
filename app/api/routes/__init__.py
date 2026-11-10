"""FastAPI route modules."""

from .admin import router as admin_router
from .chat import router as chat_router
from .crm_admin import router as crm_admin_router
from .documents import router as documents_router
from .leads import router as leads_router
from .properties import router as properties_router
from .voice import router as voice_router
from .webhooks import gateway_router as whatsapp_gateway_router
from .webhooks import router as webhooks_router

__all__ = [
    "admin_router",
    "chat_router",
    "crm_admin_router",
    "documents_router",
    "leads_router",
    "properties_router",
    "voice_router",
    "webhooks_router",
    "whatsapp_gateway_router",
]
