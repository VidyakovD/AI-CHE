/**
 * Service Worker для AI Студии Че.
 *
 * Минимальная стратегия:
 *   - Иконки/manifest/static-assets — cache-first (быстрый старт offline)
 *   - HTML-страницы — network-first с fallback на кэш (при потере сети)
 *   - API-запросы (/proposals, /chatbots, /chat...) — НЕ кэшируем
 *     (всегда свежие данные)
 *
 * Версия v1: при изменении — поднять CACHE_VERSION чтобы старый кэш
 * автоматически почистился при первой регистрации нового SW.
 */

const CACHE_VERSION = 'aiche-v2-2026-04-28';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const HTML_CACHE = `${CACHE_VERSION}-html`;

// Что прекэшируем при install — критичные shell-ассеты
const PRECACHE_URLS = [
  '/icon.svg',
  '/manifest.json',
  '/icons.js',
];

self.addEventListener('install', (event) => {
  // skipWaiting() — новый SW активируется сразу, не ждёт закрытия вкладок
  self.skipWaiting();
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_URLS))
      .catch(() => { /* offline-сеть при install — не критично */ })
  );
});

self.addEventListener('activate', (event) => {
  // Удаляем кэши старых версий
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Skip cross-origin (CDN'ы — Tailwind/icons и т.д.) — браузер сам кэширует
  if (url.origin !== self.location.origin) return;

  // НЕ кэшируем API-запросы (всё что не статика и не HTML)
  // Признак API: путь не заканчивается на .html, .css, .js, .svg, .png, .jpg, .ico, и НЕ корень
  const path = url.pathname;
  const isApi = (
    path.startsWith('/auth/') ||
    path.startsWith('/proposals/') ||
    path.startsWith('/chatbots/') ||
    path.startsWith('/sites/') ||
    path.startsWith('/chat/') ||
    path.startsWith('/admin/') ||
    path.startsWith('/agent/') ||
    path.startsWith('/payment/') ||
    path.startsWith('/webhook/') ||
    path.startsWith('/widget/') ||
    path.startsWith('/user/') ||
    path.startsWith('/assets/') ||
    path.startsWith('/uploads/') ||
    path === '/message' ||
    path === '/upload'
  );
  if (isApi) return;  // дефолтный браузерный fetch без перехвата

  // HTML-страницы: network-first (всегда свежий после деплоя)
  const isHtml = path === '/' || path.endsWith('.html');
  if (isHtml) {
    event.respondWith(
      fetch(req).then((resp) => {
        // Кэшируем успешный ответ для offline
        if (resp && resp.status === 200) {
          const respClone = resp.clone();
          caches.open(HTML_CACHE).then((c) => c.put(req, respClone));
        }
        return resp;
      }).catch(() => caches.match(req).then((cached) => cached || new Response(
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Нет сети</title>' +
        '<style>body{background:#1C1C1C;color:#f0e6d8;font-family:sans-serif;padding:40px;text-align:center}' +
        'h1{color:#ff8c42}</style></head><body><h1>Нет интернета</h1>' +
        '<p>Подключитесь к сети и обновите страницу.</p></body></html>',
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      )))
    );
    return;
  }

  // JS/CSS — network-first: иначе после деплоя нового icons.js юзер
  // продолжит видеть старую версию из кэша. При offline — fallback на кэш.
  const isCode = path.endsWith('.js') || path.endsWith('.css');
  if (isCode) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp && resp.status === 200) {
          const respClone = resp.clone();
          caches.open(STATIC_CACHE).then((c) => c.put(req, respClone));
        }
        return resp;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Иконки/SVG/PNG/manifest — cache-first (бинарные ассеты редко меняются)
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        if (resp && resp.status === 200) {
          const respClone = resp.clone();
          caches.open(STATIC_CACHE).then((c) => c.put(req, respClone));
        }
        return resp;
      });
    })
  );
});

// Обработка push-уведомлений (для будущего: события «новый КП», «ответ клиента»)
self.addEventListener('push', (event) => {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); } catch (e) { data = { title: 'AI Студия Че', body: event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'AI Студия Че', {
    body: data.body || '',
    icon: '/icon.svg',
    badge: '/icon.svg',
    data: data.url ? { url: data.url } : undefined,
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(self.clients.openWindow(url));
});
