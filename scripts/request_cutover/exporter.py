"""Read-only Prometheus bridge. No HTTP route can call begin/abort/commit."""
import argparse
import asyncio
from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, web


async def metrics(request):
    try:
        async with ClientSession(connector=UnixConnector(path=request.app['socket']),
                                 timeout=ClientTimeout(total=1)) as client:
            async with client.get('http://localhost/metrics') as response:
                if response.status != 200:
                    raise ValueError('gate metrics unavailable')
                text = await response.text()
        return web.Response(text='yeodam_request_cutover_exporter_up 1\n' + text)
    except (ClientError, OSError, TimeoutError, ValueError):
        return web.Response(text='yeodam_request_cutover_exporter_up 0\n')


def application(socket):
    app = web.Application()
    app['socket'] = socket
    app.router.add_get('/metrics', metrics)
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--socket', required=True)
    p.add_argument('--bind', default='127.0.0.1')
    p.add_argument('--port', type=int, default=19092)
    args = p.parse_args()
    web.run_app(application(args.socket), host=args.bind, port=args.port, access_log=None)


if __name__ == '__main__':
    main()
