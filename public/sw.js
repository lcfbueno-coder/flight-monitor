const CACHE = 'flight-monitor-v1';
const SHELL = ['./index.html', './manifest.json'];

// Instala: cacheia o shell estático
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL))
  );
  self.skipWaiting();
});

// Ativa: limpa caches antigos
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: data.json sempre da rede; resto do cache com fallback
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Sempre buscar data.json e history.json da rede (dados dinâmicos)
  if (url.pathname.endsWith('data.json') || url.pathname.endsWith('history.json')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Para o shell: cache-first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
