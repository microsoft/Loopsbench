"""Read-only package protocol gateway; stdlib only, no package code execution.

Run with: python server.py CONFIG.json DATA_DIR [PORT]. The data directory is
owned by the gateway and never mounted in client/tester containers.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from .protocol import GatewayError, canonical_package, check_package
    from .registries import RegistryProtocols, simple_files
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from protocol import GatewayError, canonical_package, check_package
    from registries import RegistryProtocols, simple_files


class CheckedRedirect(HTTPRedirectHandler):
    def __init__(self, hosts: set[str], insecure: bool):
        self.hosts = hosts
        self.insecure = insecure

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.hostname not in self.hosts or parsed.username or parsed.password
                or parsed.scheme not in ({"http", "https"} if self.insecure else {"https"})):
            raise GatewayError(502, "upstream redirected outside configured artifact hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Gateway:
    def __init__(self, config: dict, data: Path):
        self.policy = config["policy"]
        self.service = config["service"]
        self.data = data
        data.mkdir(parents=True, exist_ok=True)
        (data / "blobs").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.inflight: dict[str, Future] = {}
        self.download_slots = threading.BoundedSemaphore(self.service.get("max_concurrent_downloads", 4))
        self.request_slots = threading.BoundedSemaphore(self.service.get("max_concurrent_requests", 4))
        self.npm_slots = threading.BoundedSemaphore(self.service.get("max_concurrent_npm_requests", 2))
        self.index_path = data / "resolution.json"
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        self.errors = {"client": 0, "tester": 0}
        self.instance_id = str(uuid.uuid4())
        self.digest = hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()
        self.total = sum(p.stat().st_size for p in (data / "blobs").iterdir() if p.is_file())
        self.registries = RegistryProtocols(self)
        self._save_status()
        (data / "policy.json").write_text(json.dumps(self.policy, indent=2))

    def _atomic_json(self, path: Path, payload):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2))
        temporary.replace(path)

    def _save_status(self):
        self._atomic_json(self.data / "status.json", {
            "protocol": 1, "policy_digest": self.digest, "infrastructure_errors": self.errors,
            "instance_id": self.instance_id,
            "replay_only": bool(self.service.get("replay_from")),
        })

    def audit(self, role: str, path: str, status: int):
        with self.lock:
            if status >= 500:
                self.errors[role] += 1
                self._save_status()
            with (self.data / "requests.jsonl").open("a") as stream:
                stream.write(json.dumps({"time": time.time(), "role": role, "path": path[:2048], "status": status}) + "\n")

    def fetch(self, url: str, hosts: list[str], *, digest: str | None = None,
              accept: str | None = None, max_bytes: int | None = None) -> bytes:
        """Freeze every successful response (including version lists) per run.

        Callers MUST authorize the package before reaching this shared cache.
        URLs originate only in fixed protocol routes or checked registry data.
        """
        key = url if accept is None else url + "#accept=" + quote(accept, safe="")
        limit = min(max_bytes or self.service["max_artifact_bytes"], self.service["max_artifact_bytes"])
        with self.lock:
            record = self.index.get(key)
            if record is None and self.service.get("replay_from"):
                raise GatewayError(503, "frozen dependency replay has no record for this request")
            pending = self.inflight.get(key)
            owner = record is None and pending is None
            if owner:
                pending = self.inflight[key] = Future()
        if record is not None:
            if record.get("status") == 404:
                raise GatewayError(404, "package/version not found")
            blob = self.data / "blobs" / record["sha256"]
            if blob.stat().st_size > limit:
                raise GatewayError(502, "cached response exceeded artifact limit")
            body = blob.read_bytes()
            if hashlib.sha256(body).hexdigest() != record["sha256"]:
                raise GatewayError(502, "cached artifact hash mismatch")
        elif not owner:
            # Coalesce the same URL so mutable metadata is frozen consistently.
            body = pending.result()
        else:
            try:
                body = self._download(url, hosts, digest=digest, key=key, accept=accept, limit=limit)
                pending.set_result(body)
            except Exception as exc:
                pending.set_exception(exc)
                raise
            finally:
                with self.lock:
                    del self.inflight[key]
        if len(body) > limit:
            raise GatewayError(502, "response exceeded artifact limit")
        if digest and hashlib.sha256(body).hexdigest() != digest:
            raise GatewayError(502, "cached artifact hash mismatch")
        return body

    def _download(self, url: str, hosts: list[str], *, digest: str | None,
                  key: str, accept: str | None, limit: int) -> bytes:
        # Neither slow upstream I/O nor waiting for a download slot may hold
        # the index/status lock. Health checks and cache hits stay responsive.
        if not self.download_slots.acquire(timeout=self.service["request_timeout_sec"]):
            raise GatewayError(503, "dependency download queue timed out")
        try:
            parsed = urlsplit(url)
            allowed = {*hosts, parsed.hostname}
            opener = build_opener(ProxyHandler({}), CheckedRedirect(allowed, self.service["allow_insecure_upstreams"]))
            try:
                headers = {"User-Agent": "LoopsBench-Dependency-Gateway/1", "Accept-Encoding": "identity"}
                if accept is not None:
                    headers["Accept"] = accept
                request = Request(url, headers=headers)
                with opener.open(request, timeout=self.service["request_timeout_sec"]) as response:
                    body = response.read(limit + 1)
                if len(body) > limit:
                    raise GatewayError(502, "upstream response exceeded artifact limit")
            except HTTPError as exc:
                if exc.code in {404, 410}:
                    with self.lock:
                        self.index[key] = {"status": 404}
                        self._atomic_json(self.index_path, self.index)
                    raise GatewayError(404, "package/version not found") from None
                raise GatewayError(502, "package upstream HTTP failure") from None
            except (URLError, OSError, HTTPException):
                raise GatewayError(502, "package upstream unavailable") from None
            actual = hashlib.sha256(body).hexdigest()
            if digest and actual != digest:
                raise GatewayError(502, "registry artifact hash mismatch")
            with self.lock:
                blob = self.data / "blobs" / actual
                if not blob.exists():
                    if self.total + len(body) > self.service["max_cache_bytes"]:
                        raise GatewayError(507, "dependency cache capacity exceeded")
                    temporary = blob.with_suffix(".tmp")
                    temporary.write_bytes(body)
                    temporary.replace(blob)
                    self.total += len(body)
                self.index[key] = {"sha256": actual, "size": len(body), "status": 200}
                self._atomic_json(self.index_path, self.index)
            return body
        finally:
            self.download_slots.release()

    def python_files(self, package: str) -> list[dict]:
        package = check_package(self.policy, "python", package)
        if self.service.get("python_simple_url"):
            return simple_files(self, package)
        url = self.service["python_metadata_url"].rstrip("/") + "/" + package + "/json"
        try:
            metadata = json.loads(self.fetch(url, self.service["python_artifact_hosts"]))
            returned_name = canonical_package("python", metadata["info"]["name"])
        except (ValueError, KeyError, TypeError):
            raise GatewayError(502, "invalid package registry metadata") from None
        if returned_name != package:
            raise GatewayError(502, "registry returned a different package")
        files = []
        for release in metadata["releases"].values():
            for item in release:
                parsed = urlsplit(item["url"])
                if (parsed.hostname not in self.service["python_artifact_hosts"] or parsed.username or parsed.password
                        or parsed.scheme not in ({"http", "https"} if self.service["allow_insecure_upstreams"] else {"https"})):
                    raise GatewayError(502, "registry artifact URL is outside configured hosts")
                if not re.fullmatch(r"[a-f0-9]{64}", item["digests"]["sha256"]):
                    raise GatewayError(502, "registry artifact has no SHA256")
                if not re.fullmatch(r"[A-Za-z0-9_.+!-]+", item["filename"]):
                    raise GatewayError(502, "invalid registry filename")
                files.append(item)
        return files

    def route(self, path: str, origin: str = "http://loopsbench-dependencies:8080") -> tuple[bytes, str]:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not path.startswith("/"):
            raise GatewayError(400, "only package protocol paths are accepted")
        path = unquote(parsed.path, errors="strict")
        if (len(path) > 2048 or any(ord(c) < 32 for c in path) or "\\" in path or "%" in path
                or "//" in path or any(p in {".", ".."} for p in path.split("/"))):
            raise GatewayError(400, "invalid protocol path")
        if path == "/healthz":
            return json.dumps({"protocol": 1, "policy_digest": self.digest}).encode(), "application/json"
        if path == "/status":
            with self.lock:
                return (self.data / "status.json").read_bytes(), "application/json"
        # Route labels help auditing, but are not authenticated identities.
        match = re.fullmatch(r"/(client|tester)(/.*)", path)
        if not match:
            raise GatewayError(404, "unknown package protocol route")
        role, path = match.groups()
        response = self.registries.route(role, path, origin)
        if response is not None:
            return response
        if match := re.fullmatch(r"/python/simple/([A-Za-z0-9_.-]+)/?", path):
            package = check_package(self.policy, "python", match[1])
            links = []
            for item in self.python_files(package):
                digest = item["digests"]["sha256"]
                name = item["filename"]
                href = f"/{role}/python/files/{package}/{digest}/{quote(name)}#sha256={digest}"
                attrs = ""
                if item.get("requires_python"):
                    attrs += ' data-requires-python="' + html.escape(item["requires_python"], quote=True) + '"'
                if item.get("yanked"):
                    attrs += ' data-yanked="' + html.escape(item.get("yanked_reason") or "yanked", quote=True) + '"'
                links.append(f'<a href="{href}"{attrs}>{html.escape(name)}</a>')
            return ("<!doctype html><html><body>" + "\n".join(links) + "</body></html>").encode(), "text/html; charset=utf-8"
        if match := re.fullmatch(r"/python/files/([^/]+)/([a-f0-9]{64})/([A-Za-z0-9_.+!-]+)", path):
            package, digest, filename = match.groups()
            check_package(self.policy, "python", package)
            for item in self.python_files(package):
                if item["filename"] == filename and item["digests"]["sha256"] == digest:
                    return self.fetch(item["url"], self.service["python_artifact_hosts"], digest=digest), "application/octet-stream"
            raise GatewayError(404, "artifact is not listed for this package")
        if match := re.fullmatch(r"/go/(.+)/(@latest|@v/(?:list|[A-Za-z0-9.!+_-]+\.(?:info|mod|zip)))", path):
            module, operation = match.groups()
            check_package(self.policy, "go", module)
            url = self.service["go_proxy_url"].rstrip("/") + "/" + quote(module, safe="/!") + "/" + quote(operation, safe="/@!+")
            return self.fetch(url, self.service["go_artifact_hosts"]), "application/octet-stream"
        if path.startswith("/sumdb/"):
            if "go" not in self.policy["ecosystems"]:
                raise PermissionError("ecosystem is not enabled")
            suffix = path[len("/sumdb/"):]
            if match := re.fullmatch(r"lookup/(.+)@([A-Za-z0-9.!+_-]+)", suffix):
                check_package(self.policy, "go", match[1])
            elif not re.fullmatch(r"tile/[0-9]+/(?:[0-9]+|data)/(?:x[0-9]{3}/)*[0-9]{3}(?:\.p/[0-9]+)?", suffix):
                raise GatewayError(404, "unknown checksum protocol route")
            url = self.service["go_sumdb_url"].rstrip("/") + "/" + quote(suffix, safe="/@!+")
            return self.fetch(url, []), "application/octet-stream"
        raise GatewayError(404, "unknown package protocol route")


def make_server(gateway: Gateway, address=("0.0.0.0", 8080), *, control_only=False):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(gateway.service["request_timeout_sec"])

        def do_GET(self):
            # Bound the entire allocation lifetime, including cached JSON
            # decoding, URL rewriting and delivery, not only upstream I/O.
            acquired = False
            npm_acquired = False
            try:
                if control_only and self.path not in {"/healthz", "/status"}:
                    self.send_error(404)
                    return
                if self.path not in {"/healthz", "/status"}:
                    acquired = gateway.request_slots.acquire(timeout=gateway.service["request_timeout_sec"])
                    if acquired and re.match(r"/(client|tester)/npm/", unquote(self.path)):
                        npm_acquired = gateway.npm_slots.acquire(timeout=gateway.service["request_timeout_sec"])
                        if not npm_acquired:
                            gateway.request_slots.release()
                            acquired = False
                    if not acquired:
                        role = "tester" if self.path.startswith("/tester/") else "client"
                        gateway.audit(role, self.path, 503)
                        self.send_error(503, "dependency request queue timed out")
                        return
                self.deliver()
            finally:
                if npm_acquired:
                    gateway.npm_slots.release()
                if acquired:
                    gateway.request_slots.release()

        def deliver(self):
            role = "tester" if self.path.startswith("/tester/") else "client"
            status = 200
            try:
                host = self.headers.get("Host", "")
                if not re.fullmatch(r"[A-Za-z0-9.\[\]:_-]+", host):
                    raise GatewayError(400, "invalid gateway host")
                origin = gateway.service.get("endpoint") or "http://" + host
                body, content_type = gateway.route(self.path, origin.rstrip("/"))
            except PermissionError:
                status, body, content_type = 403, b"package rejected by task dependency policy", "text/plain"
            except (ValueError, UnicodeError):
                status, body, content_type = 400, b"invalid package request", "text/plain"
            except GatewayError as exc:
                status, body, content_type = exc.status, str(exc).encode(), "text/plain"
            except Exception:
                status, body, content_type = 502, b"dependency gateway failed", "text/plain"
            gateway.audit(role, self.path, status)
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except OSError:
                # An upstream success is not a delivered dependency. Count a
                # broken client/proxy connection separately from HTTP status.
                gateway.audit(role, self.path, 502)

        def log_message(self, *_args):
            pass

        do_HEAD = do_GET

    class Server(ThreadingHTTPServer):
        request_queue_size = 128

        def __init__(self):
            self.handlers = threading.BoundedSemaphore(
                gateway.service.get("max_concurrent_requests", 4) + gateway.service.get("max_pending_requests", 32))
            super().__init__(address, Handler)

        def process_request(self, request, client_address):
            # Leave excess connections in the TCP backlog rather than failing
            # legitimate package-manager bursts or spawning unbounded threads.
            # The loopback control listener remains independently responsive.
            self.handlers.acquire()
            try:
                super().process_request(request, client_address)
            except Exception:
                self.handlers.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.handlers.release()

    return Server()


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    gateway = Gateway(config, Path(sys.argv[2]))
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8080
    control = make_server(gateway, ("127.0.0.1", port + 1), control_only=True)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    make_server(gateway, ("0.0.0.0", port)).serve_forever()
