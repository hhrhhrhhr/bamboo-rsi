"""Local, certificate-verified read-only Riot Live Client Data API."""
import json
from pathlib import Path
import ssl
import urllib.request
import psutil

from bamboo_delta.protocol import NoRedirect, ProbeError
from .protocol import snapshot

URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"


class LiveClient:
    def __init__(self, certificate):
        self.expected_pid = None
        path = Path(certificate)
        if not certificate or not path.is_file():
            raise ProbeError("riot_ca_certificate_missing")
        # Trust this application's supplied CA only; never disable TLS checks.
        context = ssl.create_default_context(cafile=str(path))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                 urllib.request.HTTPSHandler(context=context))

    def bind(self, pid):
        self.expected_pid = pid
        self.check_owner()

    def check_owner(self):
        if self.expected_pid is None:
            return
        try:
            owners = {c.pid for c in psutil.net_connections(kind='tcp')
                      if c.status == psutil.CONN_LISTEN and c.laddr.port == 2999
                      and c.laddr.ip in ('127.0.0.1', '0.0.0.0', '::', '::1')}
        except psutil.Error:
            raise ProbeError('live_client_owner_unavailable') from None
        if owners != {self.expected_pid}:
            raise ProbeError('live_client_process_mismatch_or_ambiguous')

    def fetch(self):
        try:
            self.check_owner()
            with self.opener.open(URL, timeout=2) as response:
                raw = response.read(2_000_001)
            self.check_owner()
            if len(raw) > 2_000_000:
                raise ProbeError("live_data_too_large")
            value = json.loads(raw)
            return snapshot(value), value
        except ProbeError:
            raise
        except Exception:
            raise ProbeError("live_client_unavailable_or_certificate_invalid") from None
