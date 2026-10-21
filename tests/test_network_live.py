"""Real isolated Python and SDK containers use the queue without sockets or credentials."""

import os
import unittest
from pathlib import Path

from test_network_transports import NetworkFixture

from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.sandbox import WorkerEnvironment


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker")
class NetworkLiveTests(NetworkFixture, unittest.TestCase):
    def test_hosted_python_uses_scoped_network_jobs_and_cannot_claim_connector_authority(self):
        self.connector()
        repository = self.buyer.service.repository
        environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())
        runner = DockerProgramRunner(environment, repository)
        agent = self.buyer.settings.bindings[0]
        processor = ProgramProcessor(repository, runner,
            lambda subject: next((who for who in self.buyer.settings.bindings if who.subject == subject), None))
        service = self.buyer.operations.programs
        code = '''
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import time
import uuid
from chipload.network_model import DirectRead
from chipload.peer_model import PeerCatalog, PeerRequest
shop = client.shop('mill')
envelope = PeerRequest(instance_id=inputs['instance'], request_id=uuid.uuid4().hex,
    deadline=datetime.now(timezone.utc) + timedelta(seconds=25), query=PeerCatalog(shop_id='supplier-far'))
job = shop.network(DirectRead(origin=inputs['origin'], request=envelope),
    deadline=envelope.deadline, request_key='hosted-read')
while True:
    receipt = shop.network_result(job['id'])
    if receipt['state'] not in ('queued', 'running'):
        break
    time.sleep(.1)
network_blocked = False
try:
    with socket.create_connection(('1.1.1.1', 443), timeout=.2):
        pass
except OSError:
    network_blocked = True
result = {'state': receipt['state'], 'shop': receipt['result']['result']['value']['shop']['id'],
    'network_blocked': network_blocked, 'database_absent': not Path('/data').exists(),
    'credentials_absent': not Path('/run/chipload/cloud-credentials').exists()}
'''
        job = service.submit(agent, RunPython(shop_id="mill", request_key="hosted-network", code=code,
            inputs={"origin": self.supplier.origin, "instance": self.supplier.settings.instance_id}))
        try:
            self.assertTrue(processor.run_next())
            result = service.read(agent, "mill", job.id)
            self.assertEqual(result.state, "succeeded", str(result))
            self.assertEqual(result.value, {"state": "succeeded", "shop": "supplier-far", "network_blocked": True,
                "database_absent": True, "credentials_absent": True})
        finally:
            runner.remove(job.id)
        # An agent with only query permissions cannot make a dispatch call. The
        # SDK also refuses these internal operations before argument validation.
        for operation in ("network_claim", "network_complete"):
            job = service.submit(agent, RunPython(shop_id="mill", request_key=operation,
                code=f"result = client.call({operation!r}, {{'shop_id': 'mill'}})"))
            try:
                self.assertTrue(processor.run_next())
                result = service.read(agent, "mill", job.id)
                self.assertEqual(result.state, "interrupted")
                self.assertEqual(result.calls, ())
            finally:
                runner.remove(job.id)
