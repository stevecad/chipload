"""Application operations backed by supervised service processes."""

from .operations import Operations
from .service_pool import ServicePool


class ProcessOperations(Operations):
    def __init__(self, service, *, cache_bytes):
        super().__init__(service)
        self.pool = ServicePool(service.repository.path, cache_bytes=cache_bytes)

    def start(self):
        self.pool.start()

    def close(self):
        self.pool.close()

    def call(self, name, principal, request):
        return self.pool.call(name, principal, request)
