from workers import WorkerEntrypoint, asgi

from app import app
from uploads import handle_upload, is_upload


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if is_upload(request):
            return await handle_upload(request, self.env)
        return await asgi.fetch(app, request, self.env, self.ctx)
