// WhatsApp click-to-chat, independent of the assistant widget.
// The button is a standalone link appended straight to <body> and pinned to the bottom-left
// corner (the AI assistant's launcher lives bottom-right). It does not read or react to the
// assistant's open / closed / minimized state in any way.
// The number comes from window.APP_CONFIG.WHATSAPP_NUMBER (runtime-config.js). No secrets here.

const appConfig = () => (typeof window !== 'undefined' && window.APP_CONFIG) || {};

export function getWhatsAppNumber() {
  // Digits only, with country code and no '+': 15551981366
  return String(appConfig().WHATSAPP_NUMBER || '').replace(/\D/g, '');
}

export function getWhatsAppUrl() {
  const number = getWhatsAppNumber();
  return number ? `https://wa.me/${number}` : '';
}

export function openWhatsApp() {
  const url = getWhatsAppUrl();
  if (url) window.open(url, '_blank', 'noopener,noreferrer');
}

function currentLang() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('atelier-chat-v1') || 'null')?.lang;
    if (saved === 'ar' || saved === 'en') return saved;
  } catch { /* storage unavailable: fall back to the configured default */ }
  return appConfig().CHAT_DEFAULT_LANG === 'en' ? 'en' : 'ar';
}

const LABEL = { ar: 'تواصل معنا على واتساب', en: 'Chat with us on WhatsApp' };

const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>';

// The styles travel with the script so the button looks right even if a cached stylesheet is stale.
const CSS = `
.wa-fab{position:fixed;left:20px;bottom:calc(20px + env(safe-area-inset-bottom,0px));z-index:50;display:inline-flex;align-items:center;justify-content:center;gap:10px;box-sizing:border-box;height:56px;padding:0 20px 0 16px;border-radius:28px;background:#0f7f72;color:#fff;text-decoration:none;font:600 .92rem/1 'DM Sans',system-ui,sans-serif;white-space:nowrap;box-shadow:0 10px 28px rgba(15,127,114,.35);transition:transform .2s,background .2s;-webkit-tap-highlight-color:transparent}
.wa-fab:hover{background:#0b6b60;transform:translateY(-2px)}
.wa-fab:active{transform:translateY(0)}
.wa-fab:focus-visible{outline:2px solid #0b6b60;outline-offset:3px}
.wa-fab svg{width:26px;height:26px;flex:none;fill:currentColor}
@media (max-width:640px){.wa-fab{left:16px;bottom:calc(16px + env(safe-area-inset-bottom,0px));width:56px;padding:0}.wa-fab-text{display:none}}
@media (prefers-reduced-motion:reduce){.wa-fab{transition:none}}
@media print{.wa-fab{display:none}}
`;

export function initWhatsAppButton() {
  if (typeof document === 'undefined') return null;
  const existing = document.getElementById('wa-fab');
  if (existing) return existing;
  const url = getWhatsAppUrl();
  if (!url) return null; // no number configured: show nothing rather than a dead button

  if (!document.getElementById('wa-fab-style')) {
    const style = document.createElement('style');
    style.id = 'wa-fab-style';
    style.textContent = CSS;
    document.head.append(style);
  }

  const link = document.createElement('a');
  link.id = 'wa-fab';
  link.className = 'wa-fab';
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.setAttribute('aria-label', LABEL[currentLang()]);
  link.innerHTML = `${ICON}<span class="wa-fab-text">WhatsApp</span>`;
  document.body.append(link);
  return link;
}
