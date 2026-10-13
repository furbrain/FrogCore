from workers import WorkerEntrypoint

from app import FrogCoreApp


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await FrogCoreApp(self.env).handle(request)
