// WhatsApp click-to-chat utility — centralizes the WhatsApp URL and open behaviour.
// Reads the number from window.APP_CONFIG (set in runtime-config.js). No secrets here.

export function getWhatsAppNumber() {
  return (typeof window !== 'undefined' && window.APP_CONFIG && window.APP_CONFIG.WHATSAPP_NUMBER) || '';
}

export function getWhatsAppUrl() {
  const number = getWhatsAppNumber();
  return number ? `https://wa.me/${number}` : '';
}

export function openWhatsApp() {
  const url = getWhatsAppUrl();
  if (url) window.open(url, '_blank', 'noopener,noreferrer');
}