// Runtime configuration — loaded as a classic script BEFORE app.js.
// Edit this file per environment (or overwrite it at deploy time). It must never
// contain secrets: everything here is public and readable in the browser.
window.APP_CONFIG = {
  // '' (empty) = same origin. Correct when FastAPI serves this frontend (the default setup).
  // Set e.g. 'https://api.example.com' when the API lives on another origin
  // (and add this site's origin to CORS_ALLOWED_ORIGINS on the API).
  API_BASE_URL: '',

  // Default language of the chat interface: 'ar' or 'en'. Users can switch in the chat header.
  CHAT_DEFAULT_LANG: 'ar',

  // How long to wait for /chat before showing the timeout message (ms).
  REQUEST_TIMEOUT_MS: 60000,

  // Show the built-in SAMPLE listings while the live catalogue is empty. Development only:
  // a production build (scripts/build_frontend.py) sets this to false so customers never
  // see properties that do not exist.
  SHOW_PREVIEW_INVENTORY: true,

  // Show the microphone button. Voice also needs GEMINI_API_KEY on the server and an
  // https page (or localhost); otherwise the button explains why voice is unavailable.
  VOICE_ENABLED: true,

  // WhatsApp click-to-chat number (E.164 without '+'): visitors are linked to
  // https://wa.me/<number>. The existing WhatsApp backend integration handles replies.
  WHATSAPP_NUMBER: '15551981366',
};
