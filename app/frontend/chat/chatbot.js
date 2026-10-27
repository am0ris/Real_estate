// Atelier property assistant — UI + conversation state.
// Builds DOM with textContent only (never innerHTML with data), so backend text cannot inject markup.
import { CONFIG } from './config.js';
import { t, quickActions } from './i18n.js';
import { getWhatsAppUrl, openWhatsApp } from './whatsapp.js';
import { sendChat, checkHealth, buildPayload, parseTurn, resetConversation, ApiError } from './api.js';
import { fromServerRequirements, SERVER_KEYS, describeFilters, refineChips, typeChips, detectLang } from './requirements.js';
import { normalizeProperty, formatPrice, formatNumber } from './normalize.js';
import { VoiceSession } from './voice.js';

const STORE_KEY = 'atelier-chat-v1';
const MAX_STORED = 40;
const MAX_COMPARE = 3;

const ICON = {
  chat: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.2 3.4c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5z"/><path d="M12 7.6l.9 1.9 1.9.9-1.9.9-.9 1.9-.9-1.9-1.9-.9 1.9-.9z" fill="currentColor" stroke="none"/></svg>',
  close: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  min: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 12h12"/></svg>',
  expand: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 14l6-6 6 6"/></svg>',
  send: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
  mic: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>',
  micOff: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 9.5V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.6 2.5M5.5 11a6.5 6.5 0 0 0 10.8 4.9M18.5 11c0 .8-.1 1.5-.4 2.2M12 17.5V21M4 4l16 16"/></svg>',
  hangup: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  phone: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.8 12.8 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.8 12.8 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>',
  hush: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4zM16 9.5l5 5M21 9.5l-5 5"/></svg>',
};

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'html') el.innerHTML = v; // static icon strings only
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const isMobile = () => window.matchMedia('(max-width: 640px)').matches;

function loadSaved() {
  try {
    const s = JSON.parse(sessionStorage.getItem(STORE_KEY) || 'null');
    if (s && Array.isArray(s.messages)) return s;
  } catch { /* storage unavailable or corrupt: start fresh */ }
  return {};
}

export function initChatbot({ resolvePropertyHref = () => null, appRoot = null } = {}) {
  if (document.getElementById('chatbot-root')) return null;

  const saved = loadSaved();
  const state = {
    mode: 'closed', // 'closed' | 'open' | 'min'
    lang: saved.lang === 'en' || saved.lang === 'ar' ? saved.lang : CONFIG.defaultLang,
    messages: saved.messages || [],
    filters: saved.filters || {},
    loading: false,
    health: 'checking',
    healthChecked: false,
    unread: false,
    compare: [],
    // Signed server session: the same conversation for typed and spoken turns.
    sessionId: typeof saved.sessionId === 'string' ? saved.sessionId : null,
    voice: 'idle',
    muted: false,
  };
  const catalog = new Map();
  for (const m of state.messages) if (m.kind === 'results') m.properties.forEach((p) => catalog.set(p.id, p));

  // ---------- DOM skeleton ----------
  const launcherIcon = h('span', { class: 'cb-launcher-icon' });
  const launcherText = h('span', { class: 'cb-launcher-text' });
  const launcher = h('button', { type: 'button', class: 'cb-launcher', 'aria-controls': 'cb-panel', 'aria-expanded': 'false' }, launcherIcon, launcherText);

  // Floating WhatsApp button — sits above the chatbot launcher; hidden when the panel is open/minimized (CSS).
  const waBtn = h('button', { type: 'button', class: 'wa-btn', 'aria-label': 'WhatsApp' }, h('span', { class: 'wa-btn-icon', html: ICON.phone }), h('span', { class: 'wa-btn-text' }, 'WhatsApp'));
  waBtn.addEventListener('click', (e) => { e.stopPropagation(); openWhatsApp(); });

  const statusDot = h('span', { class: 'cb-dot', 'aria-hidden': 'true' });
  const statusText = h('span', { class: 'cb-status' });
  const titleEl = h('h2', { class: 'cb-title', id: 'cb-title' });
  const langBtn = h('button', { type: 'button', class: 'cb-iconbtn cb-lang' });
  const minBtn = h('button', { type: 'button', class: 'cb-iconbtn' });
  const closeBtn = h('button', { type: 'button', class: 'cb-iconbtn', html: ICON.close });
  const headMain = h('div', { class: 'cb-head-main' }, h('span', { class: 'cb-avatar', 'aria-hidden': 'true', html: ICON.chat }), h('div', { class: 'cb-head-text' }, titleEl, h('p', { class: 'cb-status-row' }, statusDot, statusText)));
  const header = h('header', { class: 'cb-head' }, headMain, h('div', { class: 'cb-head-actions' }, langBtn, minBtn, closeBtn));

  const log = h('div', { class: 'cb-log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions', tabindex: '0' });
  const filtersBar = h('div', { class: 'cb-filters', role: 'group', hidden: true });
  const compareInfo = h('span', { class: 'cb-compare-info' });
  const compareGo = h('button', { type: 'button', class: 'cb-btn cb-btn--primary cb-btn--sm' });
  const compareClear = h('button', { type: 'button', class: 'cb-btn cb-btn--sm' });
  const compareBar = h('div', { class: 'cb-comparebar', hidden: true }, compareInfo, h('span', { class: 'cb-compare-actions' }, compareClear, compareGo));
  const ta = h('textarea', { class: 'cb-input', rows: '1', maxlength: '500', dir: 'auto', autocomplete: 'off', enterkeyhint: 'send' });
  const sendBtn = h('button', { type: 'submit', class: 'cb-send', html: ICON.send, disabled: true });
  const micBtn = h('button', { type: 'button', class: 'cb-mic', html: ICON.mic, 'aria-pressed': 'false', hidden: !CONFIG.voiceEnabled });
  const form = h('form', { class: 'cb-form' }, ta, micBtn, sendBtn);

  const voiceStatus = h('span', { class: 'cb-voice-status', role: 'status' });
  const voiceCaption = h('p', { class: 'cb-voice-caption', dir: 'auto', 'aria-hidden': 'true' });
  const muteBtn = h('button', { type: 'button', class: 'cb-iconbtn cb-voice-btn', html: ICON.mic, 'aria-pressed': 'false' });
  const hushBtn = h('button', { type: 'button', class: 'cb-iconbtn cb-voice-btn', html: ICON.hush, hidden: true });
  const endBtn = h('button', { type: 'button', class: 'cb-iconbtn cb-voice-btn cb-voice-end', html: ICON.hangup });
  const voiceBar = h('div', { class: 'cb-voice', role: 'group', hidden: true, 'data-state': 'idle' },
    h('span', { class: 'cb-voice-orb', 'aria-hidden': 'true' }),
    h('div', { class: 'cb-voice-text' }, voiceStatus, voiceCaption),
    h('div', { class: 'cb-voice-actions' }, hushBtn, muteBtn, endBtn));
  const panel = h('section', { class: 'cb-panel', id: 'cb-panel', role: 'dialog', 'aria-labelledby': 'cb-title', hidden: true }, header, log, filtersBar, compareBar, voiceBar, form);
  const root = h('div', { class: 'cb', id: 'chatbot-root', 'data-mode': 'closed' }, launcher, waBtn, panel);
  document.body.append(root);

  // ---------- chrome (language-dependent static text) ----------
  function renderStatus() {
    const k = state.health === 'up' ? 'statusUp' : state.health === 'down' ? 'statusDown' : 'statusChecking';
    statusText.textContent = t(state.lang, k);
    statusDot.dataset.state = state.health;
  }
  function renderLauncher() {
    const open = state.mode === 'open';
    launcherIcon.innerHTML = open && !isMobile() ? ICON.close : ICON.chat;
    launcherText.textContent = t(state.lang, 'launcherLabel');
    launcher.setAttribute('aria-label', open ? t(state.lang, 'close') : t(state.lang, 'launcher'));
    launcher.setAttribute('aria-expanded', String(open));
    launcher.toggleAttribute('data-unread', state.unread && state.mode !== 'open');
  }
  function renderChrome() {
    const l = state.lang;
    panel.lang = l;
    panel.dir = l === 'ar' ? 'rtl' : 'ltr';
    titleEl.textContent = t(l, 'name');
    langBtn.textContent = t(l, 'langBtn');
    langBtn.setAttribute('aria-label', t(l, 'langToggle'));
    langBtn.setAttribute('lang', l === 'ar' ? 'en' : 'ar');
    const min = state.mode === 'min';
    minBtn.innerHTML = min ? ICON.expand : ICON.min;
    minBtn.setAttribute('aria-label', t(l, min ? 'expand' : 'minimize'));
    closeBtn.setAttribute('aria-label', t(l, 'close'));
    ta.placeholder = t(l, 'placeholder');
    ta.setAttribute('aria-label', t(l, 'inputLabel'));
    sendBtn.setAttribute('aria-label', t(l, 'send'));
    compareGo.textContent = t(l, 'compareGo');
    compareClear.textContent = t(l, 'compareClear');
    filtersBar.setAttribute('aria-label', t(l, 'filtersLabel'));
    voiceBar.setAttribute('aria-label', t(l, 'voiceLabel'));
    hushBtn.setAttribute('aria-label', t(l, 'interrupt'));
    hushBtn.title = t(l, 'interrupt');
    endBtn.setAttribute('aria-label', t(l, 'voiceEnd'));
    endBtn.title = t(l, 'voiceEnd');
    renderStatus();
    renderLauncher();
    renderFilters();
    syncCompareUI();
    renderVoice();
  }

  // ---------- voice bar ----------
  const VOICE_STATUS = { connecting: 'voiceConnecting', listening: 'voiceListening', thinking: 'voiceThinking', speaking: 'voiceSpeaking' };
  function renderVoice() {
    const l = state.lang;
    const active = state.voice !== 'idle' && state.voice !== 'error';
    voiceBar.hidden = !active;
    voiceBar.dataset.state = state.muted ? 'muted' : state.voice;
    voiceStatus.textContent = t(l, state.muted && active ? 'voiceMuted' : VOICE_STATUS[state.voice] || 'voiceIdle');
    micBtn.classList.toggle('is-active', active);
    micBtn.setAttribute('aria-pressed', String(active));
    micBtn.setAttribute('aria-label', t(l, active ? 'voiceEnd' : 'voiceStart'));
    micBtn.title = t(l, active ? 'voiceEnd' : 'voiceStart');
    micBtn.innerHTML = active ? ICON.hangup : ICON.mic;
    muteBtn.innerHTML = state.muted ? ICON.micOff : ICON.mic;
    muteBtn.setAttribute('aria-pressed', String(state.muted));
    muteBtn.setAttribute('aria-label', t(l, state.muted ? 'unmute' : 'mute'));
    muteBtn.title = t(l, state.muted ? 'unmute' : 'mute');
    hushBtn.hidden = state.voice !== 'speaking';
    if (!active) voiceCaption.textContent = '';
  }

  // ---------- mode / focus / page lock ----------
  function lockPage(on) {
    document.documentElement.classList.toggle('cb-lock', on);
    if (appRoot) on ? appRoot.setAttribute('inert', '') : appRoot.removeAttribute('inert');
  }
  function setMode(mode, { focus = true } = {}) {
    const prev = state.mode;
    state.mode = mode;
    root.dataset.mode = mode;
    panel.hidden = mode === 'closed';
    if (mode === 'open') state.unread = false;
    if (mode === 'closed' && voice.active) voice.stop();
    lockPage(mode === 'open' && isMobile());
    renderChrome();
    if (mode === 'open') {
      ensureHealth();
      scrollLog(true);
      if (focus) ta.focus({ preventScroll: true });
    } else if (focus && prev !== mode) {
      (mode === 'closed' ? launcher : minBtn).focus({ preventScroll: true });
    }
  }
  function syncViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    root.style.setProperty('--cb-vh', `${vv.height}px`);
    root.style.setProperty('--cb-top', `${vv.offsetTop}px`);
  }
  window.visualViewport?.addEventListener('resize', syncViewport);
  window.visualViewport?.addEventListener('scroll', syncViewport);
  window.matchMedia('(max-width: 640px)').addEventListener?.('change', () => { lockPage(state.mode === 'open' && isMobile()); renderLauncher(); });
  syncViewport();

  async function ensureHealth() {
    if (state.healthChecked) return;
    state.healthChecked = true;
    renderStatus();
    state.health = (await checkHealth()) ? 'up' : 'down';
    renderStatus();
  }

  // ---------- persistence ----------
  function persist() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ lang: state.lang, filters: state.filters, sessionId: state.sessionId, messages: state.messages.slice(-MAX_STORED) }));
    } catch { /* ignore quota / private mode */ }
  }

  // ---------- scrolling ----------
  function scrollLog(toEnd, el) {
    requestAnimationFrame(() => {
      const top = toEnd || !el ? log.scrollHeight : Math.max(0, el.offsetTop - 8);
      log.scrollTo({ top, behavior: reducedMotion() ? 'auto' : 'smooth' });
    });
  }

  // ---------- answer text (paragraphs, bullets, **bold**, clickable property ids) ----------
  function formatAnswer(text, ids) {
    const frag = document.createDocumentFragment();
    const idAlt = [...new Set(ids.filter((i) => i && i.length >= 3))].sort((a, b) => b.length - a.length).map(escRe).join('|');
    const re = new RegExp(`\\*\\*(.+?)\\*\\*${idAlt ? `|(^|[^\\w-])(${idAlt})(?![\\w-])` : ''}`, 'g');
    const inline = (line) => {
      const nodes = [];
      let last = 0;
      for (const m of line.matchAll(re)) {
        if (m[1] !== undefined) {
          if (m.index > last) nodes.push(line.slice(last, m.index));
          nodes.push(h('strong', { text: m[1] }));
        } else {
          const start = m.index + m[2].length;
          if (start > last) nodes.push(line.slice(last, start));
          nodes.push(h('button', { type: 'button', class: 'cb-idref', 'data-goto': m[3], text: m[3] }));
        }
        last = m.index + m[0].length;
      }
      if (last < line.length) nodes.push(line.slice(last));
      return nodes;
    };
    let list = null;
    for (const raw of text.replace(/\r/g, '').split('\n')) {
      const line = raw.trim().replace(/^#{1,6}\s+/, '');
      if (!line) { list = null; continue; }
      const li = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (li) {
        if (!list) { list = h('ul', { class: 'cb-list' }); frag.append(list); }
        list.append(h('li', {}, inline(li[1])));
      } else {
        list = null;
        frag.append(h('p', {}, inline(line)));
      }
    }
    return frag;
  }

  // ---------- property card ----------
  function propertyCard(p, lang, showCompare) {
    const internal = resolvePropertyHref(p.id, p);
    const href = internal || p.url;
    const heading = p.title || [p.type, p.location].filter(Boolean).join(' — ') || `${t(lang, 'id')} ${p.id}`;
    const facts = [
      p.bedrooms && t(lang, p.bedrooms === 1 ? 'beds' : 'bedsN', { n: formatNumber(p.bedrooms, lang) }),
      p.bathrooms && t(lang, p.bathrooms === 1 ? 'baths' : 'bathsN', { n: formatNumber(p.bathrooms, lang) }),
      p.area != null && t(lang, 'sqm', { n: formatNumber(p.area, lang) }),
    ].filter(Boolean);
    const q = `property=${encodeURIComponent(p.id)}`;
    const more = p.description || p.source || p.id;
    const card = h('article', { class: `cb-prop${p.image ? ' has-img' : ''}`, 'data-pid': p.id, tabindex: '-1' },
      p.image && h('img', {
        class: 'cb-prop-img', src: p.image, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer',
        onerror: (e) => { e.target.closest('.cb-prop')?.classList.remove('has-img'); e.target.remove(); },
      }),
      h('div', { class: 'cb-prop-main' },
        (p.type || p.inAnswer || p.isDemo) && h('div', { class: 'cb-prop-tags' },
          p.isDemo && h('span', { class: 'cb-tag cb-tag--demo', text: t(lang, 'demoListing') }),
          p.type && p.type !== heading && h('span', { class: 'cb-tag', text: p.type }),
          p.inAnswer && h('span', { class: 'cb-tag cb-tag--ref', text: t(lang, 'inAnswer') })),
        h('h3', { class: 'cb-prop-title', dir: 'auto', text: heading }),
        p.location && p.title && h('p', { class: 'cb-prop-loc', dir: 'auto', text: p.location }),
        p.price != null && h('p', { class: 'cb-prop-price', text: formatPrice(p.price, p.currency, lang) }),
        facts.length > 0 && h('ul', { class: 'cb-facts' }, facts.map((f) => h('li', { text: f }))),
        more && h('details', { class: 'cb-more' },
          h('summary', { text: t(lang, 'details') }),
          p.description && h('p', { class: 'cb-more-text', dir: 'auto', text: p.description }),
          h('p', { class: 'cb-more-meta' }, `${t(lang, 'id')}: `, h('bdi', { text: p.id }), p.source && ` · ${t(lang, 'source')}: `, p.source && h('bdi', { text: p.source }))),
        h('div', { class: 'cb-prop-actions' },
          href && h('a', { class: 'cb-btn cb-btn--primary', href, ...(internal ? { 'data-nav': '' } : { target: '_blank', rel: 'noopener noreferrer' }), text: t(lang, 'view') }),
          h('a', { class: 'cb-btn', href: `#/contact?${q}&intent=contact`, 'data-nav': '', text: t(lang, 'contact') }),
          h('a', { class: 'cb-btn', href: `#/contact?${q}&intent=viewing`, 'data-nav': '', text: t(lang, 'viewing') }),
          showCompare && h('button', { type: 'button', class: 'cb-btn cb-btn--toggle', 'data-compare-id': p.id, 'aria-pressed': String(state.compare.includes(p.id)), text: t(lang, 'compare') }))));
    return card;
  }

  // ---------- comparison table (only rows with data; missing cells show an em dash) ----------
  function compareTable(items, lang) {
    const rows = [
      ['rowType', (p) => p.type],
      ['rowLocation', (p) => p.location],
      ['rowPrice', (p) => (p.price != null ? formatPrice(p.price, p.currency, lang) : undefined)],
      ['rowBedrooms', (p) => p.bedrooms && formatNumber(p.bedrooms, lang)],
      ['rowBathrooms', (p) => p.bathrooms && formatNumber(p.bathrooms, lang)],
      ['rowArea', (p) => (p.area != null ? t(lang, 'sqm', { n: formatNumber(p.area, lang) }) : undefined)],
    ].filter(([, get]) => items.some((p) => get(p)));
    return h('div', { class: 'cb-table-wrap' },
      h('table', { class: 'cb-table' },
        h('thead', {}, h('tr', {}, h('td', {}), items.map((p) => h('th', { scope: 'col' }, p.title || p.type || p.id, h('small', {}, h('bdi', { text: p.id })))))),
        h('tbody', {}, rows.map(([label, get]) => h('tr', {}, h('th', { scope: 'row', text: t(lang, label) }), items.map((p) => h('td', { text: get(p) || '—' })))))));
  }

  // ---------- messages ----------
  const bubble = (text, extra = '') => h('div', { class: `cb-bubble ${extra}`.trim(), dir: 'auto', text });
  function chipsEl(m, lang) {
    if (!m.chips?.length) return null;
    return h('div', { class: 'cb-chips' },
      m.hint && h('p', { class: 'cb-hint', text: m.hint }),
      h('div', { class: 'cb-chip-row' }, m.chips.map((c) => h('button', {
        type: 'button', class: 'cb-chip', text: c.label, dir: 'auto',
        onclick: () => (c.remove ? handleSend(c.text, { clear: SERVER_KEYS[c.remove] })
          : c.action === 'voice' ? startVoice()
            : c.action === 'guide' ? handleSend(c.label, { action: 'guide' }) : handleSend(c.text)),
      }))));
  }

  function buildMessage(m) {
    const lang = m.lang || state.lang;
    const wrap = h('div', { class: `cb-msg cb-msg--${m.role}`, 'data-id': m.id });
    if (m.role === 'user') {
      const b = bubble(m.text, m.voice ? 'cb-bubble--voice' : '');
      if (m.voice) b.setAttribute('aria-label', `${t(state.lang, 'youSaid')}: ${m.text}`);
      wrap.append(b);
      return wrap;
    }
    const stack = h('div', { class: 'cb-stack' });
    wrap.append(stack);
    if (m.kind === 'welcome') {
      stack.append(bubble(t(state.lang, 'welcome')));
      if (m.showChips) stack.append(chipsEl({ chips: quickActions(state.lang) }, state.lang));
    } else if (m.kind === 'text') {
      stack.append(bubble(m.text), chipsEl(m, lang));
    } else if (m.kind === 'error') {
      const key = { timeout: 'errTimeout', invalid: 'errInvalid', network: 'errNetwork', rate_limited: 'errRate' }[m.errorKind] || 'errServer';
      stack.append(h('div', { class: 'cb-bubble cb-bubble--error', role: 'alert', dir: 'auto' }, h('p', { text: t(lang, key) }),
        m.req && h('button', { type: 'button', class: 'cb-btn cb-btn--sm', text: t(lang, 'retry'), onclick: () => retry(m) })));
    } else if (m.kind === 'compare') {
      stack.append(h('div', { class: 'cb-bubble cb-bubble--wide' }, h('p', { class: 'cb-compare-title', text: t(lang, 'compareTitle') }), compareTable(m.items, lang)));
    } else if (m.kind === 'results') {
      const ids = m.properties.map((p) => p.id);
      if (m.answer) stack.append(h('div', { class: 'cb-bubble cb-answer', dir: 'auto' }, formatAnswer(m.answer, ids)));
      else stack.append(bubble(t(lang, m.properties.length ? 'foundFallback' : 'empty')));
      const cited = m.properties.filter((p) => p.inAnswer);
      const others = m.properties.filter((p) => !p.inAnswer);
      const show = m.properties.length > 1;
      const group = (title, list) => {
        if (!list.length) return;
        if (title) stack.append(h('p', { class: 'cb-group', text: title }));
        list.forEach((p) => stack.append(propertyCard(p, lang, show)));
      };
      const both = cited.length && others.length;
      group(both ? t(lang, 'groupCited') : '', cited);
      group(both ? t(lang, 'groupOthers') : '', others);
      stack.append(chipsEl(m, lang));
    }
    return wrap;
  }

  function renderLog() {
    log.replaceChildren(...state.messages.map(buildMessage));
    scrollLog(true);
  }

  function pushMessage(m) {
    m.id = `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    for (const prev of state.messages) { delete prev.chips; delete prev.hint; if (prev.kind === 'welcome') prev.showChips = false; }
    log.querySelectorAll('.cb-chips').forEach((n) => n.remove());
    state.messages.push(m);
    if (m.kind === 'results') m.properties.forEach((p) => catalog.set(p.id, p));
    const el = buildMessage(m);
    log.append(el);
    persist();
    return el;
  }

  // ---------- filters bar ----------
  function renderFilters() {
    const items = describeFilters(state.filters, state.lang);
    const started = state.messages.some((m) => m.role === 'user');
    filtersBar.hidden = !items.length && !started;
    filtersBar.replaceChildren(
      started && h('button', { type: 'button', class: 'cb-link', text: t(state.lang, 'newSearch'), onclick: newSearch }),
      ...items.map((f) => h('button', {
        type: 'button', class: 'cb-chip cb-chip--filter', 'aria-label': t(state.lang, 'removeFilter', { x: f.label }),
        onclick: () => !state.loading && handleSend(t(state.lang, 'removed', { x: f.label }), { clear: SERVER_KEYS[f.key] }),
      }, h('span', { text: f.label }), h('span', { class: 'cb-x', 'aria-hidden': 'true', text: '×' }))),
    );
  }

  // ---------- compare ----------
  function syncCompareUI() {
    log.querySelectorAll('[data-compare-id]').forEach((b) => b.setAttribute('aria-pressed', String(state.compare.includes(b.dataset.compareId))));
    compareBar.hidden = state.compare.length === 0;
    compareInfo.textContent = t(state.lang, 'selected', { n: formatNumber(state.compare.length, state.lang) });
    compareGo.disabled = state.compare.length < 2;
  }
  function toggleCompare(id) {
    const i = state.compare.indexOf(id);
    if (i >= 0) state.compare.splice(i, 1);
    else if (state.compare.length >= MAX_COMPARE) { compareInfo.textContent = t(state.lang, 'compareMax'); return; }
    else state.compare.push(id);
    syncCompareUI();
  }
  function runCompare() {
    const items = state.compare.map((id) => catalog.get(id)).filter(Boolean).map((p) => ({ ...p }));
    if (items.length < 2) return;
    state.compare = [];
    const el = pushMessage({ role: 'assistant', kind: 'compare', lang: state.lang, items });
    syncCompareUI();
    scrollLog(false, el);
  }

  // ---------- conversation flow ----------
  function pushWelcome() { state.messages.push({ id: 'welcome', role: 'assistant', kind: 'welcome', showChips: true }); }
  function newSearch() {
    if (state.loading) return;
    // Clears search filters server-side too; the customer's profile is kept.
    resetConversation(state.sessionId);
    state.filters = {};
    state.messages = [];
    state.compare = [];
    catalog.clear();
    pushWelcome();
    renderLog();
    renderFilters();
    syncCompareUI();
    persist();
  }

  const showLoader = (lang) => {
    const el = h('div', { class: 'cb-msg cb-msg--assistant cb-msg--loading' },
      h('div', { class: 'cb-bubble', role: 'status' }, h('span', { class: 'cb-dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), h('span', { text: t(lang, 'searching') })));
    log.append(el);
    scrollLog(true);
    return el;
  };

  function buildChips(lang, filters, empty) {
    if (empty) {
      const active = describeFilters(filters, lang);
      if (active.length) return { hint: t(lang, 'tryRelax'), chips: active.map((f) => ({ label: t(lang, 'without', { x: f.label }), text: t(lang, 'removed', { x: f.label }), remove: f.key })) };
    }
    const chips = refineChips(lang, filters);
    return chips.length ? { hint: t(lang, 'refineHint'), chips } : {};
  }

  const SEARCH_INTENTS = new Set(['property_search']);

  // Render one agent turn (from /chat or a voice `agent_result`) into the log.
  function pushAssistantTurn(data, lang) {
    if (data.sessionId) state.sessionId = data.sessionId;
    state.filters = fromServerRequirements(data.requirements);
    renderFilters();
    if (!data.properties.length && !SEARCH_INTENTS.has(data.intent)) {
      return pushMessage({ role: 'assistant', kind: 'text', lang, text: data.answer || t(lang, 'foundFallback') });
    }
    // cited properties first, then other grounding properties, then the rest in backend (reranked) order
    const rank = (p) => (data.cited.includes(p.id) ? 0 : data.grounding.includes(p.id) ? 1 : 2);
    const props = data.properties.map(normalizeProperty).filter((p) => p.id)
      .map((p, i) => ({ p, i })).sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i).map(({ p }) => ({ ...p, inAnswer: data.cited.includes(p.id) }));
    const chips = data.degraded ? {} : buildChips(lang, state.filters, props.length === 0);
    return pushMessage({ role: 'assistant', kind: 'results', lang, answer: data.answer, properties: props, cited: data.cited, grounding: data.grounding, ...chips });
  }

  async function runTurn(req) {
    state.loading = true;
    sendBtn.disabled = true;
    log.setAttribute('aria-busy', 'true');
    const loader = showLoader(req.lang);
    let el;
    try {
      const payload = buildPayload({ query: req.query, sessionId: state.sessionId, language: req.lang, clearRequirements: req.clear });
      const data = await sendChat(payload);
      loader.remove();
      state.health = 'up';
      renderStatus();
      el = pushAssistantTurn(data, req.lang);
    } catch (err) {
      loader.remove();
      const kind = err instanceof ApiError ? err.kind : 'server';
      if (kind === 'network' || kind === 'server') { state.health = 'down'; renderStatus(); }
      el = pushMessage({ role: 'assistant', kind: 'error', lang: req.lang, errorKind: kind, req });
    } finally {
      state.loading = false;
      log.removeAttribute('aria-busy');
      sendBtn.disabled = !ta.value.trim();
      if (state.mode !== 'open') { state.unread = true; renderLauncher(); }
    }
    scrollLog(false, el);
  }

  function retry(m) {
    if (state.loading) return;
    state.messages = state.messages.filter((x) => x !== m);
    log.querySelector(`[data-id="${m.id}"]`)?.remove();
    runTurn(m.req);
  }

  async function handleSend(rawText, opts = {}) {
    const text = rawText.trim();
    if (!text || state.loading) return;
    if (state.mode !== 'open') setMode('open', { focus: false });
    const lang = detectLang(text, state.lang);
    pushMessage({ role: 'user', kind: 'text', text });
    scrollLog(true);
    renderFilters();

    if (opts.action === 'guide') {
      pushMessage({ role: 'assistant', kind: 'text', lang, text: t(lang, 'guide'), chips: typeChips(lang) });
      scrollLog(true);
      return;
    }
    // The server keeps the conversation (requirements, selected property, booking progress),
    // so the message is sent as typed; removing a chip names the requirement keys to clear.
    await runTurn({ query: text, lang, clear: opts.clear || [] });
  }

  // ---------- voice ----------
  // Spoken turns run through the same agent workflow and session as typed ones. The log shows
  // what the customer said (transcript) and the agent's grounded reply + property cards.
  let voiceTurn = { user: '', assistant: '', userShown: false, hadResult: false };
  let levelFrame = 0;
  const flushVoiceUser = () => {
    const text = voiceTurn.user.trim();
    if (text && !voiceTurn.userShown) {
      pushMessage({ role: 'user', kind: 'text', text, voice: true });
      voiceTurn.userShown = true;
      scrollLog(true);
    }
  };
  const voice = new VoiceSession({
    getSessionId: () => state.sessionId,
    setSessionId: (id) => { if (id) { state.sessionId = id; persist(); } },
    getLang: () => state.lang,
    onState: (s) => { state.voice = s; if (s === 'idle' || s === 'error') state.muted = false; renderVoice(); },
    onTranscript: (role, text) => {
      if (role === 'user') {
        if (voiceTurn.userShown) voiceTurn = { user: '', assistant: '', userShown: false, hadResult: false };
        voiceTurn.user += text;
        voiceCaption.textContent = voiceTurn.user.trim();
      } else {
        voiceTurn.assistant += text;
        voiceCaption.textContent = voiceTurn.assistant.trim();
      }
    },
    onAgentResult: (event) => {
      flushVoiceUser();
      const data = parseTurn(event);
      const el = pushAssistantTurn(data, event.language === 'ar' || event.language === 'en' ? event.language : state.lang);
      voiceTurn.hadResult = true;
      scrollLog(false, el);
      if (state.mode !== 'open') { state.unread = true; renderLauncher(); }
    },
    onTurnComplete: () => {
      flushVoiceUser();
      // Small talk answered without the agent tool is only in the spoken transcript.
      const spoken = voiceTurn.assistant.trim();
      if (!voiceTurn.hadResult && spoken) {
        pushMessage({ role: 'assistant', kind: 'text', lang: detectLang(spoken, state.lang), text: spoken });
        scrollLog(true);
      }
      voiceTurn = { user: '', assistant: '', userShown: false, hadResult: false };
    },
    onError: (reason) => {
      const retryable = reason === 'lost' || reason === 'unavailable' || reason === 'busy' || reason === 'idle' || reason === 'max_duration';
      pushMessage({
        role: 'assistant', kind: 'text', lang: state.lang, text: t(state.lang, `voice_${reason}`),
        chips: retryable ? [{ label: t(state.lang, 'voiceRetry'), action: 'voice' }] : undefined,
      });
      scrollLog(true);
    },
    onLevel: (level) => {
      cancelAnimationFrame(levelFrame);
      levelFrame = requestAnimationFrame(() => voiceBar.style.setProperty('--cb-level', Math.min(1, level * 4).toFixed(2)));
    },
  });

  function startVoice() {
    if (state.mode !== 'open') setMode('open', { focus: false });
    voiceTurn = { user: '', assistant: '', userShown: false, hadResult: false };
    voice.start();
  }

  // ---------- events ----------
  launcher.addEventListener('click', () => setMode(state.mode === 'open' ? 'closed' : 'open'));
  closeBtn.addEventListener('click', () => setMode('closed'));
  micBtn.addEventListener('click', () => (voice.active ? voice.stop() : startVoice()));
  endBtn.addEventListener('click', () => voice.stop());
  hushBtn.addEventListener('click', () => voice.interrupt());
  muteBtn.addEventListener('click', () => {
    state.muted = !state.muted;
    voice.setMuted(state.muted);
    renderVoice();
  });
  // Never leave the microphone open behind a closed panel or a navigated-away page.
  window.addEventListener('pagehide', () => voice.stop());
  minBtn.addEventListener('click', () => setMode(state.mode === 'min' ? 'open' : 'min'));
  headMain.addEventListener('click', () => state.mode === 'min' && setMode('open'));
  langBtn.addEventListener('click', () => {
    state.lang = state.lang === 'ar' ? 'en' : 'ar';
    renderChrome();
    renderLog();
    persist();
  });
  panel.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); setMode('closed'); }
  });
  compareGo.addEventListener('click', runCompare);
  compareClear.addEventListener('click', () => { state.compare = []; syncCompareUI(); });

  log.addEventListener('click', (e) => {
    const cmp = e.target.closest('[data-compare-id]');
    if (cmp) return toggleCompare(cmp.dataset.compareId);
    const ref = e.target.closest('[data-goto]');
    if (ref) {
      const card = ref.closest('.cb-msg')?.querySelector(`[data-pid="${CSS.escape(ref.dataset.goto)}"]`);
      if (card) {
        card.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
        card.focus({ preventScroll: true });
        card.classList.add('is-flash');
        setTimeout(() => card.classList.remove('is-flash'), 1500);
      }
      return;
    }
    if (e.target.closest('a[data-nav]') && isMobile()) setMode('min', { focus: false }); // reveal the page on small screens
  });

  const autosize = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`; };
  const submit = () => {
    const v = ta.value;
    if (!v.trim() || state.loading) return;
    ta.value = '';
    autosize();
    sendBtn.disabled = true;
    handleSend(v);
  };
  ta.addEventListener('input', () => { autosize(); sendBtn.disabled = state.loading || !ta.value.trim(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });

  // Any element with data-chat-open opens the assistant (optionally sending data-chat-prompt). Survives page re-renders.
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest?.('[data-chat-open]');
    if (!trigger) return;
    e.preventDefault();
    setMode('open');
    const prompt = trigger.getAttribute('data-chat-prompt');
    if (prompt) handleSend(prompt);
  });

  // ---------- boot ----------
  if (!state.messages.length) pushWelcome();
  renderChrome();
  renderLog();

  return { open: () => setMode('open'), close: () => setMode('closed'), send: handleSend };
}
