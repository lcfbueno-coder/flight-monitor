const CACHE = 'flight-monitor-v2';
const STATIC = ['./', './index.html', './manifest.json', './icon-192.png', './icon-512.png'];

// Instala: cacheia o shell (falha em um arquivo não derruba os outros)
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      Promise.allSettled(STATIC.map(u => c.add(u)))
    )
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

// Estratégia:
//  • Navegação (index.html) e dados (data/history.json): REDE PRIMEIRO,
//    cache só como fallback offline — o painel nunca fica preso numa
//    versão velha após um deploy.
//  • Demais estáticos (ícones, manifest): cache primeiro.
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  const isData = url.pathname.endsWith('data.json') || url.pathname.endsWith('history.json');

  if (req.mode === 'navigate' || isData) {
    // data.json chega com ?_=timestamp — normaliza a chave do cache
    // para não acumular uma entrada por requisição.
    const key = isData ? url.pathname : req;
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(key, copy));
        return res;
      }).catch(() => caches.match(key))
    );
    return;
  }

  e.respondWith(
    caches.match(req).then(cached =>
      cached || fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return res;
      })
    )
  );
});
