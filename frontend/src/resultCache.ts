/** Persist large read-only results; every reuse must be validated by the server. */
const cacheName = 'keyword-result-details-v1';
const detailPath = /^\/api\/v1\/projects\/[0-9a-f-]{36}\/pipeline-runs\/[0-9a-f-]{36}\/(?:library(?:\/[0-9a-f-]{36}(?:\/template)?)?|traffic(?:\/[0-9a-f-]{36}\/(?:rows|reviews))?|cleaning(?:\/[0-9a-f-]{36}\/rows)?|core-selection|competitors|aba-reports(?:\/[0-9a-f-]{36})?)$/;

export async function fetchResult(url: string, init: RequestInit): Promise<Response> {
  const parsed = new URL(url, location.origin);
  if ((init.method ?? 'GET').toUpperCase() !== 'GET' || !detailPath.test(parsed.pathname))
    return fetch(url, init);
  const headers = new Headers(init.headers);
  const key = new URL(parsed);
  key.searchParams.set('__device_cache', headers.get('X-Keyword-Browser-Device') ?? '');
  let cache: Cache | undefined;
  let saved: Response | undefined;
  try {
    cache = await caches.open(cacheName);
    saved = await cache.match(key.href);
    const etag = saved?.headers.get('ETag');
    if (etag) headers.set('If-None-Match', etag);
  } catch { /* Disabled storage or quota limits must not prevent normal reads. */ }
  let response = await fetch(url, { ...init, headers, cache: 'no-store' });
  if (response.status === 304) {
    if (saved) return saved;
    headers.delete('If-None-Match');
    response = await fetch(url, { ...init, headers, cache: 'no-store' });
  }
  if (cache && response.ok && response.headers.has('ETag')) {
    // Keep a bounded cache. Writes are awaited so a successful reload can reuse it.
    try {
      await cache.put(key.href, response.clone());
      const keys = await cache.keys();
      for (const stale of keys.slice(0, Math.max(0, keys.length - 40))) await cache.delete(stale);
    } catch { /* The network result remains usable if persistence fails. */ }
  } else if (cache && !response.ok) {
    await cache.delete(key.href).catch(() => false);
  }
  return response;
}
