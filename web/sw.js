const CACHE_NAME = 'xiaoya-v1.8.1';
const ASSETS_TO_CACHE = [
  './',
  './index.html',
  './manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  // API 请求交由浏览器默认处理（网络优先）
  if (event.request.url.includes('/api/')) {
    return;
  }
  // HTML 页面：网络优先，离线才回退缓存（保证改动即时可见）
  if (event.request.mode === 'navigate' || event.request.url.endsWith('/index.html')) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  // 其他静态资源：缓存优先
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request);
    })
  );
});
