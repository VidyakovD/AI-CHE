/**
 * KILL-SWITCH Service Worker для AI Студии Че.
 *
 * После недели проблем с агрессивным кэшированием — мы УБИРАЕМ SW
 * полностью. Этот sw.js при активации:
 *   1. Удаляет ВСЕ caches (старого SW)
 *   2. Unregister-ит сам себя
 *   3. Force-reload-ит всех активных clients
 *
 * Браузеры юзеров с СТАРЫМ SW получат этот новый файл через
 * network-first или периодический check. Новый SW активируется,
 * сделает cleanup, перезагрузит вкладку. Дальше SW нет — браузер
 * сам обращается к серверу напрямую, кэшировать нечего.
 *
 * Когда захотим вернуть PWA-кэш — пишем sw.js заново.
 */

const CACHE_VERSION = 'aiche-killswitch-2026-05-12';

self.addEventListener('install', (event) => {
  // skipWaiting() — новый SW активируется СРАЗУ, не ждёт закрытия вкладок
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    try {
      // 1. Удаляем все caches
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
      console.log('[killswitch-sw] cleared caches:', keys);

      // 2. Unregister самого себя
      await self.registration.unregister();
      console.log('[killswitch-sw] unregistered');

      // 3. Force-reload всех клиентов
      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((c) => {
        try { c.navigate(c.url); } catch (_) {}
      });
    } catch (e) {
      console.error('[killswitch-sw] activate error:', e);
    }
  })());
});

// fetch — пропускаем всё насквозь. Никакого кэша.
self.addEventListener('fetch', (event) => {
  // НЕ перехватываем — браузер сам fetch к серверу
});

// Push-уведомления оставляем (если кто-то подписан — пусть работает)
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
