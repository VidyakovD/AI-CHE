/**
 * Service Worker для AI Студии Че — PWA-обёртка (save-to-home + offline).
 *
 * Стратегия:
 *   - Статика (CSS/JS/fonts/иконки) → stale-while-revalidate (мгновенный
 *     ответ из кэша + фоновое обновление). Версионируется CACHE_VERSION.
 *   - HTML/navigation → network-first с fallback на закэшированную копию
 *     (если юзер офлайн — открывается последняя версия страницы).
 *   - API/auth/SSE/webhook/upload → НЕ перехватываем. Всегда прямо в сеть.
 *   - Non-GET (POST/PUT/DELETE/PATCH) → НЕ перехватываем.
 *   - Cross-origin → НЕ перехватываем.
 *
 * Это даёт юзеру "приложение" на главном экране (Android Add to Home /
 * iOS Add to Home Screen), которое запускается fullscreen, работает офлайн
 * для уже посещённых страниц, не ломает обновления (HTML всегда свежий).
 *
 * Push-уведомления оставлены (если кто-то подпишется через VAPID — будут).
 *
 * При смене статики — поднимать CACHE_VERSION (старый кэш удалится в activate).
 *
 * Заменил kill-switch sw.js 2026-05-16. Юзеры со старым SW (если остались)
 * получили unregister, теперь скачают этот.
 */

const CACHE_VERSION = 'aiche-pwa-2026-05-16-v1';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const HTML_CACHE   = `${CACHE_VERSION}-html`;

// Pre-cache минимум критичных файлов чтобы offline-start работал
const PRECACHE_URLS = [
  '/',
  '/fonts.css',
  '/styles.css',
  '/shared.js',
  '/icons.js',
  '/logo-192.png',
  '/logo-512.png',
  '/manifest.json',
];

// Пути которые НЕ перехватываем — всегда сеть
const NETWORK_ONLY_PREFIXES = [
  '/api/',
  '/auth/',
  '/agents/',         // /agents/* runtime endpoints
  '/agent/',          // legacy ReAct agent endpoints
  '/solutions/',
  '/knowledge/',
  '/chat/',
  '/chatbots/',
  '/proposals/',
  '/sites/',
  '/presentations/',
  '/creators/',
  '/payment/',
  '/admin/',
  '/webhook/',
  '/user/',
  '/uploads/',
  '/notifications/',
  '/oauth/',
];

function isNetworkOnly(url) {
  const p = url.pathname;
  for (const prefix of NETWORK_ONLY_PREFIXES) {
    if (p.startsWith(prefix)) return true;
  }
  // SSE стримы — текстовый event-stream, не кэшируем
  if (p.endsWith('/stream')) return true;
  return false;
}

function isHtmlRequest(req, url) {
  if (req.mode === 'navigate') return true;
  const accept = req.headers.get('accept') || '';
  if (accept.includes('text/html')) return true;
  if (url.pathname.endsWith('.html')) return true;
  return false;
}

function isCacheableStatic(url) {
  const p = url.pathname.toLowerCase();
  return p.endsWith('.css') || p.endsWith('.js') || p.endsWith('.woff2')
      || p.endsWith('.woff') || p.endsWith('.ttf') || p.endsWith('.svg')
      || p.endsWith('.png') || p.endsWith('.jpg') || p.endsWith('.jpeg')
      || p.endsWith('.webp') || p.endsWith('.ico') || p.endsWith('.json');
}

// ── install ───────────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    // Pre-cache best-effort: если какой-то файл недоступен, не падаем
    await Promise.all(PRECACHE_URLS.map(async (url) => {
      try {
        const resp = await fetch(url, { cache: 'no-store' });
        if (resp && resp.ok) await cache.put(url, resp);
      } catch (_) { /* ignore */ }
    }));
    self.skipWaiting();  // новая версия активируется сразу, не ждёт закрытия вкладок
  })());
});

// ── activate ──────────────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Удаляем старые версии кэша
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => {
      if (k !== STATIC_CACHE && k !== HTML_CACHE) {
        return caches.delete(k);
      }
    }));
    await self.clients.claim();
  })());
});

// ── fetch (главная логика) ────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Только GET
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (_) { return; }

  // Только same-origin
  if (url.origin !== self.location.origin) return;

  // API и dynamic-эндпоинты — пропускаем (network only)
  if (isNetworkOnly(url)) return;

  if (isHtmlRequest(req, url)) {
    // Network-first для HTML — юзер всегда видит свежую страницу
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        // Кэшируем только успешные ответы
        if (fresh && fresh.ok) {
          const cache = await caches.open(HTML_CACHE);
          cache.put(req, fresh.clone()).catch(() => {});
        }
        return fresh;
      } catch (_) {
        // Офлайн — пробуем достать из кэша
        const cached = await caches.match(req);
        if (cached) return cached;
        // Совсем нет — отдаём главную, если она в кэше
        const root = await caches.match('/');
        if (root) return root;
        // Последний фоллбэк — простой offline-стаб
        return new Response(
          '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">' +
          '<title>Офлайн — AI Студия Че</title>' +
          '<style>body{background:#1C1C1C;color:#f0e6d8;font-family:system-ui,sans-serif;' +
          'display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:20px}</style>' +
          '</head><body><div><h1 style="color:#ff8c42">📡 Нет связи</h1>' +
          '<p>Откроется автоматически когда появится интернет.</p>' +
          '<p style="opacity:.6;font-size:14px">AI Студия Че · aiche.ru</p></div></body></html>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 }
        );
      }
    })());
    return;
  }

  if (isCacheableStatic(url)) {
    // Stale-while-revalidate для статики: мгновенный ответ из кэша,
    // в фоне обновляем версию
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const cached = await cache.match(req);
      const fetchPromise = fetch(req).then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
        return resp;
      }).catch(() => null);
      return cached || (await fetchPromise) || new Response('', { status: 504 });
    })());
    return;
  }

  // Всё остальное — браузер сам
});

// ── push notifications (наследуем от kill-switch версии) ──────────────────
self.addEventListener('push', (event) => {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); }
  catch (e) { data = { title: 'AI Студия Че', body: event.data.text() }; }
  event.waitUntil(self.registration.showNotification(
    data.title || 'AI Студия Че',
    {
      body: data.body || '',
      icon: '/logo-192.png',
      badge: '/logo-192.png',
      data: data.url ? { url: data.url } : undefined,
      tag: data.tag || undefined,
      requireInteraction: !!data.requireInteraction,
    }
  ));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // Если приложение уже открыто — фокусируемся
    for (const c of allClients) {
      if (c.url.includes(self.location.origin) && 'focus' in c) {
        try { await c.navigate(url); } catch (_) {}
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});
