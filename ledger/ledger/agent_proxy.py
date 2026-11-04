"""把 /api/agent/* 原样转发给本机的台账助手进程，流式回传。

助手是独立进程：它会重启台账服务，也会被自己的守护脚本重启，所以不能住在本进程里。
用 HTTP/1.0 请求，响应体以连接关闭为界，省掉分块编码的解析。
"""
import asyncio
import os

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

PORT = int(os.environ.get('COPILOT_PORT', '8020'))
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length', 'host'}


def install(app):
    @app.api_route('/api/agent/{path:path}', methods=['GET', 'POST'])
    async def forward(path: str, request: Request):
        body = await request.body()
        target = request.url.path + (('?' + request.url.query) if request.url.query else '')
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', PORT), 5)
        except (OSError, asyncio.TimeoutError):
            return JSONResponse({'detail': '助手正在重启，请稍后再试'}, status_code=503)
        head = [f'{request.method} {target} HTTP/1.0', f'Host: 127.0.0.1:{PORT}', f'Content-Length: {len(body)}']
        if request.headers.get('content-type'):
            head.append('Content-Type: ' + request.headers['content-type'])
        writer.write(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body)
        await writer.drain()
        status_line = await reader.readline()
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            writer.close()
            return JSONResponse({'detail': '助手没有正常响应'}, status_code=502)
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b'\r\n', b'\n', b''):
                break
            name, _, value = line.decode('latin-1').partition(':')
            if name.strip().lower() not in HOP:
                headers[name.strip()] = value.strip()

        async def stream():
            try:
                while chunk := await reader.read(65536):
                    yield chunk
            finally:
                writer.close()

        media = headers.pop('content-type', None) or headers.pop('Content-Type', None)
        return StreamingResponse(stream(), status_code=status, headers=headers, media_type=media)
