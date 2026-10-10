"""Package protocols, independent of task language and installation commands.

Only trusted repository endpoints and metadata may create upstream URLs. All
artifact routes recheck identity before the shared cache, including replay.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import lzma
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

try:
    from .protocol import GatewayError, canonical_package, check_package
except ImportError:
    from protocol import GatewayError, canonical_package, check_package


def json_response(value):
    return json.dumps(value).encode(), "application/json"


def checked_artifact(gateway, url, hosts):
    parsed = urlsplit(url)
    if (parsed.hostname not in hosts or parsed.username or parsed.password or parsed.fragment
            or parsed.scheme not in ({"http", "https"} if gateway.service["allow_insecure_upstreams"] else {"https"})):
        raise GatewayError(502, "registry artifact URL is outside configured hosts")
    return url


class SimpleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def simple_files(gateway, package):
    """PEP 503 mirrors (including Nexus/Artifactory), with pinned SHA256 links."""
    url = gateway.service["python_simple_url"].rstrip("/") + "/" + quote(package) + "/"
    hosts = [urlsplit(url).hostname, *gateway.service["python_artifact_hosts"]]
    parser = SimpleParser()
    parser.feed(gateway.fetch(url, hosts).decode("utf-8"))
    files = []
    for link in parser.links:
        if "href" not in link:
            continue
        parsed = urlsplit(urljoin(url, link["href"]))
        filename = unquote(parsed.path.rsplit("/", 1)[-1])
        if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.(?:whl|zip|tar\.(?:gz|bz2|xz))", filename):
            continue
        hashes = parse_qs(parsed.fragment)
        digest = hashes.get("sha256", [""])[0]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise GatewayError(502, "Simple API artifact requires SHA256")
        artifact = checked_artifact(gateway, urlunsplit(parsed._replace(fragment="")), hosts)
        files.append({"filename": filename, "url": artifact, "digests": {"sha256": digest},
                      "requires_python": link.get("data-requires-python"), "yanked": "data-yanked" in link,
                      "yanked_reason": link.get("data-yanked")})
    return files


def cargo_prefix(crate):
    return str(len(crate)) if len(crate) < 3 else ("3/" + crate[0] if len(crate) == 3 else crate[:2] + "/" + crate[2:4])


class RegistryProtocols:
    def __init__(self, gateway):
        self.g = gateway
        self.s = gateway.service
        self.apt_files = {}

    def enabled(self, ecosystem):
        if ecosystem not in self.g.policy["ecosystems"]:
            raise PermissionError("ecosystem is not enabled")

    def route(self, role, path, origin):
        base = origin + "/" + role
        if path.startswith("/npm/"):
            return self.npm(path[5:], base)
        if path.startswith("/cargo/"):
            return self.cargo(path[7:], base)
        if path.startswith("/maven/"):
            return self.maven(path[7:])
        if path.startswith("/nuget/"):
            return self.nuget(path[7:], base)
        if path.startswith("/apt/"):
            return self.apt(path[5:])
        if match := re.fullmatch(r"/source/([a-z0-9_-]+)/([A-Za-z0-9_.+-]+)", path):
            source = self.g.policy.get("sources", {}).get(match[1])
            if not source or source["filename"] != match[2]:
                raise PermissionError("source is not declared in trusted task policy")
            return self.g.fetch(source["url"], source["redirect_hosts"], digest=source["sha256"]), "application/octet-stream"
        return None

    def npm_metadata(self, package):
        package = check_package(self.g.policy, "npm", package)
        url = self.s["npm_registry_url"].rstrip("/") + "/" + quote(package, safe="@")
        try:
            metadata = json.loads(self.g.fetch(
                url, self.s["npm_artifact_hosts"], accept="application/vnd.npm.install-v1+json",
                max_bytes=self.s.get("max_npm_metadata_bytes", 16 * 1024 * 1024)))
            if canonical_package("npm", metadata["name"]) != package or not isinstance(metadata["versions"], dict):
                raise ValueError()
            return metadata
        except (ValueError, KeyError, TypeError):
            raise GatewayError(502, "invalid npm registry metadata") from None

    def npm(self, suffix, base):
        # npm ci rewrites existing lockfiles' public-registry tarball origins
        # but preserves their conventional /<package>/-/<file>.tgz path.
        if match := re.fullmatch(r"((?:@[^/]+/)?[^/]+)/-/([^/]+\.tgz)", suffix):
            package, filename = match.groups()
            metadata = self.npm_metadata(package)
            for version, item in metadata["versions"].items():
                if unquote(urlsplit(item["dist"]["tarball"]).path).rsplit("/", 1)[-1] == filename:
                    return self.npm("_files/" + package + "/" + version + ".tgz", base)
            raise GatewayError(404, "npm tarball is not listed for this package")
        # A private artifact namespace avoids npm's scoped-name ambiguity.
        if match := re.fullmatch(r"_files/((?:@[^/]+/)?[^/]+)/([^/]+)\.tgz", suffix):
            package, version = match.groups()
            metadata = self.npm_metadata(package)
            item = metadata["versions"].get(version)
            if not item:
                raise GatewayError(404, "npm version not found")
            dist = item["dist"]
            hosts = [urlsplit(self.s["npm_registry_url"]).hostname, *self.s["npm_artifact_hosts"]]
            body = self.g.fetch(checked_artifact(self.g, dist["tarball"], hosts), hosts)
            integrity = dist.get("integrity", "")
            matched = False
            # Verify the strongest advertised supported hash, not just SHA1.
            for algorithm in ("sha512", "sha384", "sha256", "sha1"):
                hashes = [token.split("-", 1)[1] for token in integrity.split() if token.startswith(algorithm + "-")]
                if hashes:
                    matched = base64.b64encode(hashlib.new(algorithm, body).digest()).decode() in hashes
                    break
            else:
                matched = bool(dist.get("shasum")) and hashlib.sha1(body).hexdigest() == dist["shasum"]
            if not matched:
                raise GatewayError(502, "npm artifact hash mismatch or missing integrity")
            return body, "application/octet-stream"
        package = check_package(self.g.policy, "npm", suffix.rstrip("/"))
        metadata = self.npm_metadata(package)
        for version, item in metadata["versions"].items():
            if not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
                raise GatewayError(502, "invalid npm version")
            if canonical_package("npm", item.get("name", package)) != package:
                raise GatewayError(502, "npm registry returned a different package")
            item["dist"]["tarball"] = base + "/npm/_files/" + quote(package, safe="@/") + "/" + version + ".tgz"
        return json_response(metadata)

    def cargo_entries(self, crate):
        check_package(self.g.policy, "cargo", crate)
        if crate != crate.lower():
            raise GatewayError(400, "Cargo sparse paths must be lowercase")
        url = self.s["cargo_index_url"].rstrip("/") + "/" + cargo_prefix(crate) + "/" + crate
        body = self.g.fetch(url, self.s["cargo_artifact_hosts"])
        try:
            entries = [json.loads(line) for line in body.splitlines() if line]
            for item in entries:
                if canonical_package("cargo", item["name"]) != canonical_package("cargo", crate):
                    raise ValueError()
                if not re.fullmatch(r"[a-f0-9]{64}", item["cksum"]):
                    raise ValueError()
            return body, entries
        except (ValueError, KeyError, TypeError):
            raise GatewayError(502, "invalid Cargo sparse index") from None

    def cargo(self, suffix, base):
        self.enabled("cargo")
        if suffix == "index/config.json":
            return json_response({"dl": base + "/cargo/files/{crate}/{version}/download"})
        if match := re.fullmatch(r"index/(.+)/([a-z0-9_-]+)", suffix):
            if match[1] != cargo_prefix(match[2]):
                raise GatewayError(400, "invalid Cargo sparse prefix")
            return self.cargo_entries(match[2])[0], "text/plain"
        if match := re.fullmatch(r"files/([a-z0-9_-]+)/([A-Za-z0-9.+_-]+)/download", suffix):
            crate, version = match.groups()
            _, entries = self.cargo_entries(crate)
            for item in entries:
                if item["vers"] == version:
                    url = self.s["cargo_download_url"].format(crate=crate, version=version, prefix=cargo_prefix(crate), lowerprefix=cargo_prefix(crate))
                    return self.g.fetch(url, self.s["cargo_artifact_hosts"], digest=item["cksum"]), "application/octet-stream"
            raise GatewayError(404, "crate version not found")
        raise GatewayError(404, "unknown Cargo route")

    def maven(self, suffix):
        self.enabled("maven")
        parts = suffix.split("/")
        if len(parts) < 3 or not all(re.fullmatch(r"[A-Za-z0-9_.+-]+", part) for part in parts):
            raise GatewayError(400, "invalid Maven coordinates")
        # Artifact metadata lives above the version directory; timestamped
        # SNAPSHOT metadata and binaries live inside it.
        metadata = re.fullmatch(r"maven-metadata\.xml(?:\.(?:sha1|sha256|sha512|md5))?", parts[-1])
        if metadata and not parts[-2].endswith("-SNAPSHOT"):
            artifact, group = parts[-2], ".".join(parts[:-2])
        else:
            if len(parts) < 4:
                raise GatewayError(400, "invalid Maven artifact path")
            artifact, group = parts[-3], ".".join(parts[:-3])
            if not metadata and (not parts[-1].startswith(artifact + "-") or not re.search(
                    r"\.(?:pom|jar|module|aar|war|zip|klib|exe|tar\.gz)(?:\.(?:sha1|sha256|sha512|md5|asc))?$", parts[-1])):
                raise GatewayError(404, "unknown Maven artifact type")
        check_package(self.g.policy, "maven", group + ":" + artifact)
        for upstream in self.s["maven_repository_urls"]:
            try:
                return self.g.fetch(upstream.rstrip("/") + "/" + suffix, self.s["maven_artifact_hosts"]), "application/octet-stream"
            except GatewayError as exc:
                if exc.status != 404:
                    raise
        raise GatewayError(404, "Maven artifact not found")

    def nuget(self, suffix, base):
        self.enabled("nuget")
        if suffix == "index.json":
            return json_response({"version": "3.0.0", "resources": [{"@id": base + "/nuget/flat/", "@type": "PackageBaseAddress/3.0.0"}]})
        if match := re.fullmatch(r"flat/([a-z0-9_.-]+)/(index\.json|[a-z0-9.+_-]+/[a-z0-9.+_-]+\.(?:nupkg|nuspec))", suffix):
            package, operation = match.groups()
            check_package(self.g.policy, "nuget", package)
            if operation != "index.json":
                version, filename = operation.split("/")
                if filename not in {f"{package}.{version}.nupkg", f"{package}.nuspec"}:
                    raise GatewayError(400, "NuGet artifact identity mismatch")
            url = self.s["nuget_flat_url"].rstrip("/") + "/" + package + "/" + operation
            return self.g.fetch(url, self.s["nuget_artifact_hosts"]), "application/octet-stream"
        raise GatewayError(404, "unknown NuGet route")

    def apt_index(self, repo_name, repo, relative):
        body = self.g.fetch(repo["url"].rstrip("/") + "/" + relative, [])
        if re.search(r"/Packages(?:\.(?:xz|gz))?$", relative):
            # Bound decompression separately from the compressed fetch limit.
            import io
            stream = lzma.LZMAFile(io.BytesIO(body)) if relative.endswith(".xz") else (
                gzip.GzipFile(fileobj=io.BytesIO(body)) if relative.endswith(".gz") else io.BytesIO(body))
            with stream:
                raw = stream.read(self.s["max_artifact_bytes"] + 1)
            if len(raw) > self.s["max_artifact_bytes"]:
                raise GatewayError(502, "apt index exceeded decompression limit")
            for paragraph in raw.decode().split("\n\n"):
                fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
                if not all(key in fields for key in ("Package", "Filename", "SHA256")):
                    continue
                filename = fields["Filename"]
                if (not re.fullmatch(r"pool/[A-Za-z0-9/+_.~-]+\.deb", filename)
                        or any(part in {".", ".."} for part in filename.split("/"))
                        or not re.fullmatch(r"[a-f0-9]{64}", fields["SHA256"])):
                    raise GatewayError(502, "invalid apt package metadata")
                self.apt_files[(repo_name, filename)] = fields
        return body

    def apt(self, suffix):
        self.enabled("apt")
        repo_name, sep, relative = suffix.partition("/")
        repo = self.s["apt_repositories"].get(repo_name)
        if not sep or not repo:
            raise PermissionError("apt repository is not configured")
        if relative.startswith("dists/"):
            parts = relative.split("/")
            valid = len(parts) == 3 and parts[1] in repo["suites"] and parts[2] in {"InRelease", "Release", "Release.gpg"}
            valid |= (len(parts) == 5 and parts[1] in repo["suites"] and parts[2] in repo["components"]
                      and parts[3] in ["binary-" + arch for arch in repo["architectures"]]
                      and parts[4] in {"Packages", "Packages.gz", "Packages.xz", "Release"})
            if not valid:
                raise GatewayError(404, "apt index is not enabled")
            return self.apt_index(repo_name, repo, relative), "application/octet-stream"
        if re.fullmatch(r"pool/[A-Za-z0-9/+_.~-]+\.deb", relative):
            if (repo_name, relative) not in self.apt_files:
                for suite in repo["suites"]:
                    for component in repo["components"]:
                        for arch in repo["architectures"]:
                            for extension in (".xz", ".gz", ""):
                                try:
                                    self.apt_index(repo_name, repo, f"dists/{suite}/{component}/binary-{arch}/Packages{extension}")
                                    break
                                except GatewayError as exc:
                                    if exc.status != 404:
                                        raise
            fields = self.apt_files.get((repo_name, relative))
            if not fields:
                raise GatewayError(404, "apt artifact is not in configured package indexes")
            check_package(self.g.policy, "apt", fields["Package"])
            check_package(self.g.policy, "apt", fields.get("Source", fields["Package"]).split()[0])
            return self.g.fetch(repo["url"].rstrip("/") + "/" + relative, [], digest=fields["SHA256"]), "application/octet-stream"
        raise GatewayError(404, "unknown apt route")
