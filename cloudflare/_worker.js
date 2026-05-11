const ALLOWED_PATHS = ['/webhook', '/api/', '/health'];
const BACKEND = 'http://195.201.31.49';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!ALLOWED_PATHS.some(p => url.pathname === p || url.pathname.startsWith(p))) {
      return new Response('Not Found', { status: 404 });
    }

    const target = `${BACKEND}${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    headers.set('X-Proxy-Secret', env.PROXY_SECRET || '');
    headers.set('X-Forwarded-Host', url.hostname);

    return fetch(target, {
      method: request.method,
      headers,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
    });
  },
};
