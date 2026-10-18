"""Loopback V1 HTTP fixtures + preparation-only gate; no AWS/DB requests."""
import argparse
import asyncio
from aiohttp import web
from request_cutover.config import load
from request_cutover.gate import serve


async def main(config):
    runners = []
    async def upstream(request):
        response = web.Response(status=201, body=await request.read())
        response.headers['X-Mock-URI'] = request.rel_url.raw_path_qs
        response.headers['X-Mock-Host'] = request.headers.get('Host', '')
        response.headers['X-Mock-CSRF'] = request.headers.get('X-CSRF-TOKEN', '')
        response.headers['X-Mock-Cookie'] = request.headers.get('Cookie', '')
        response.headers['X-Mock-Backend'] = request.app['kind']
        response.headers.add('Set-Cookie', 'a=1; HttpOnly; Path=/')
        response.headers.add('Set-Cookie', 'b=2; Secure; Path=/')
        return response
    try:
        for port, kind in ((8080, 'api'), (3000, 'web')):
            app = web.Application()
            app['kind'] = kind
            app.router.add_route('*', '/{path:.*}', upstream)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', port).start()
            runners.append(runner)
        await serve(config, '127.0.0.1', 19091)
    finally:
        for runner in runners:
            await runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    try:
        asyncio.run(main(load(parser.parse_args().config)))
    except KeyboardInterrupt:
        pass
