'use strict';
const STORAGE = 'bgnews.news.v2';
const names = { Z: 'زومجی', V: 'ویجیاتو', G: 'گیمفا' };
let state = { items: [], sources: {}, lastUpdate: null };
let storageWarning = false;
try {
  const saved = JSON.parse(localStorage.getItem(STORAGE));
  if (saved && Array.isArray(saved.items) && saved.sources && typeof saved.sources === 'object') state = saved;
} catch { storageWarning = true; }
let filter = 'all';
const $ = id => document.getElementById(id);
function httpURL(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
}
function imageURL(value) {
  if (typeof value === 'string' && /^\/api\/image\/[a-f0-9]{64}$/.test(value)) return value;
  return httpURL(value);
}
function message(text, error = false) {
  const node = document.createElement('div'); node.className = 'notice' + (error ? ' error' : ''); node.textContent = text; $('messages').append(node);
}
function persist() {
  try { localStorage.setItem(STORAGE, JSON.stringify(state)); }
  catch { message('حافظهٔ مرورگر در دسترس نیست؛ فهرست با بستن صفحه ذخیره نمی‌شود.', true); }
}
function render() {
  const selected = state.items.filter(item => filter === 'all' || item.source === filter);
  $('articles').replaceChildren();
  for (const item of selected) {
    if (!httpURL(item.url) || !names[item.source]) continue;
    const card = document.createElement('article'); card.className = 'card';
    const letter = document.createElement('span'); letter.className = 'source-letter'; letter.textContent = item.source; letter.title = names[item.source];
    const link = document.createElement('a'); link.className = 'story-link'; link.href = httpURL(item.url); link.target = '_blank'; link.rel = 'noopener noreferrer';
    const imageWrap = document.createElement('div'); imageWrap.className = 'image-wrap';
    const fallback = () => { imageWrap.replaceChildren(); const mark = document.createElement('span'); mark.textContent = item.source; imageWrap.append(mark); };
    if (imageURL(item.image)) {
      const image = document.createElement('img'); image.src = imageURL(item.image); image.alt = item.title; image.loading = 'lazy'; image.addEventListener('error', fallback, { once: true }); imageWrap.append(image);
    } else fallback();
    const title = document.createElement('h2'); title.textContent = item.title;
    const meta = document.createElement('div'); meta.className = 'card-meta';
    const source = document.createElement('span'); source.textContent = names[item.source];
    const time = document.createElement('time'); const date = new Date(item.published);
    if (!Number.isNaN(date.getTime())) { time.dateTime = date.toISOString(); time.textContent = new Intl.DateTimeFormat('fa-IR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date); }
    const storyCopy = document.createElement('div'); storyCopy.className = 'story-copy';
    meta.append(source, time); storyCopy.append(meta, title); link.append(imageWrap, storyCopy); card.append(letter, link); $('articles').append(card);
  }
  $('total').textContent = state.items.length.toLocaleString('fa-IR');
  $('empty').hidden = selected.length > 0;
  if (state.items.length && !selected.length) {
    $('empty').querySelector('h2').textContent = 'هنوز مطلبی از این منبع نداریم';
    $('empty').querySelector('p').textContent = 'منبع دیگری را انتخاب کن یا دوباره تازه‌ها را بگیر.';
  } else if (!state.items.length) {
    $('empty').querySelector('h2').textContent = 'یک رفرش تا تازه‌ترین‌ها';
    $('empty').querySelector('p').textContent = 'دکمهٔ بنفش را بزن؛ آخرین مطلب هر سه رسانه اینجا می‌آید. دفعه‌های بعد، مطالب جدید به این فهرست اضافه می‌شوند.';
  }
  if (state.lastUpdate) $('update-time').textContent = 'آخرین بررسی موفق: ' + new Date(state.lastUpdate).toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' });
}
// The first successful response establishes a separate baseline for each source.
// Retain all baseline IDs, but display only its newest item. Later refreshes add
// entries before the most recent known entry, avoiding old posts resurfacing.
function mergeSource(current, code, incoming) {
  if (!incoming.length) return 0;
  const previous = current.sources[code];
  const incomingById = new Map(incoming.map(item => [item.id, item]));
  // Refresh metadata for saved stories too. This replaces image URLs saved by
  // older versions with the current local image proxy URL.
  current.items = current.items.map(item => {
    const latest = item.source === code ? incomingById.get(item.id) : null;
    return latest ? { ...item, ...latest, source: code } : item;
  });
  let fresh;
  if (!previous) fresh = incoming.slice(0, 1);
  else {
    const known = new Set(previous.seen);
    const boundary = incoming.findIndex(item => known.has(item.id));
    const candidates = boundary < 0 ? incoming : incoming.slice(0, boundary);
    fresh = candidates.filter(item => !known.has(item.id));
  }
  const existing = new Set(current.items.map(item => item.source + ':' + item.id));
  fresh = fresh.filter(item => !existing.has(code + ':' + item.id));
  current.items.unshift(...fresh.map(item => ({ ...item, source: code })));
  current.sources[code] = { seen: [...new Set([...(previous?.seen || []), ...incoming.map(item => item.id)])] };
  return fresh.length;
}
async function refresh() {
  const button = $('refresh'); button.disabled = true; button.setAttribute('aria-busy', 'true'); $('refresh-label').textContent = 'در حال بررسی…'; $('messages').replaceChildren();
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch('/api/news', { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error('server');
    const result = await response.json(); let added = 0; let success = 0;
    for (const code of Object.keys(names)) {
      const source = result[code];
      if (!source || source.error || !Array.isArray(source.items) || !source.items.length) { message(source?.error || `پاسخی از ${names[code]} دریافت نشد.`, true); continue; }
      const items = source.items.filter(item => item && typeof item.id === 'string' && typeof item.title === 'string' && httpURL(item.url));
      if (!items.length) { message(`فهرست ${names[code]} قابل خواندن نیست.`, true); continue; }
      added += mergeSource(state, code, items); success++;
    }
    if (success) {
      state.items.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
      state.lastUpdate = new Date().toISOString(); persist(); render();
      message(added ? `${added.toLocaleString('fa-IR')} مطلب تازه به فهرست اضافه شد.` : 'بررسی شد؛ مطلب تازه‌ای منتشر نشده است.');
    }
  } catch (error) {
    message(error.name === 'AbortError' ? 'دریافت بیش از حد طول کشید؛ دوباره امتحان کن.' : 'ارتباط با سرور محلی برقرار نشد. برنامه را با python3 server.py اجرا کن و صفحه را از آدرس سرور باز کن.', true);
  } finally { clearTimeout(timeout); button.disabled = false; button.removeAttribute('aria-busy'); $('refresh-label').textContent = 'تازه‌ها رو بگیر'; }
}
document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
  filter = button.dataset.filter;
  document.querySelectorAll('.filter').forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); });
  render();
}));
$('refresh').addEventListener('click', refresh);
render();
if (storageWarning) message('ذخیرهٔ قبلی خوانده نشد؛ در این صفحه از فهرست تازه استفاده می‌شود.', true);
if (location.protocol === 'file:') message('برای دریافت خبرها، python3 server.py را اجرا کن و آدرس http://127.0.0.1:8765 را در مرورگر باز کن.', true);
