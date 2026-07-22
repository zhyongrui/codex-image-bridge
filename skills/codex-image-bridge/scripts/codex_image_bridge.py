#!/usr/bin/env python3
"""Compatibility bridge for Codex's standalone image generation extension."""

import argparse
import json
import logging
import os
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

BRIDGE_VERSION = "1.3.0-dev"


@dataclass(frozen=True)
class BridgeConfig:
    upstream_base_url: str
    responses_model: str
    mount_path: str = "/openai"
    timeout_seconds: int = 300
    max_request_bytes: int = 300 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "upstream_base_url", self.upstream_base_url.rstrip("/"))
        mount = "/" + self.mount_path.strip("/") if self.mount_path.strip("/") else ""
        object.__setattr__(self, "mount_path", mount)


def relative_request_path(path: str, mount_path: str) -> str:
    parsed = urllib.parse.urlsplit(path)
    route = parsed.path
    if mount_path and (route == mount_path or route.startswith(mount_path + "/")):
        route = route[len(mount_path) :] or "/"
    if parsed.query:
        route += "?" + parsed.query
    return route


def upstream_url(config: BridgeConfig, request_path: str) -> str:
    relative = relative_request_path(request_path, config.mount_path)
    if not relative.startswith("/"):
        relative = "/" + relative
    return config.upstream_base_url + relative


def image_endpoint(config: BridgeConfig, request_path: str) -> Optional[str]:
    relative = urllib.parse.urlsplit(relative_request_path(request_path, config.mount_path)).path
    if relative == "/images/generations":
        return "generate"
    if relative == "/images/edits":
        return "edit"
    return None


def _copy_tool_option(source: Dict[str, Any], target: Dict[str, Any], key: str) -> None:
    value = source.get(key)
    if value is not None:
        target[key] = value


def build_responses_payload(
    image_request: Dict[str, Any], action: str, responses_model: str
) -> Dict[str, Any]:
    prompt = image_request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("image request requires a non-empty prompt")

    tool: Dict[str, Any] = {"type": "image_generation", "action": action}
    for key in ("size", "quality", "background", "n"):
        _copy_tool_option(image_request, tool, key)

    if action == "generate":
        model_input: Any = prompt
    else:
        images = image_request.get("images")
        if not isinstance(images, list) or not images:
            raise ValueError("image edit request requires at least one image")
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            image_url = image.get("image_url") if isinstance(image, dict) else None
            if not isinstance(image_url, str) or not image_url:
                raise ValueError("every edit image requires image_url")
            content.append({"type": "input_image", "image_url": image_url})
        model_input = [{"role": "user", "content": content}]

    return {
        "model": responses_model,
        "input": model_input,
        "tools": [tool],
        "stream": False,
        "store": False,
    }


def extract_image_results(response_payload: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
    results: List[str] = []
    revised_prompt: Optional[str] = None
    output = response_payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "image_generation_call":
                continue
            result = item.get("result")
            if isinstance(result, str) and result:
                results.append(result)
            candidate = item.get("revised_prompt")
            if revised_prompt is None and isinstance(candidate, str):
                revised_prompt = candidate
    return results, revised_prompt


def convert_responses_to_images(
    response_payload: Dict[str, Any], image_request: Dict[str, Any]
) -> Dict[str, Any]:
    results, revised_prompt = extract_image_results(response_payload)
    if not results:
        error = response_payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            raise ValueError(error["message"])
        raise ValueError("upstream Responses API returned no generated image")

    created = response_payload.get("created_at", time.time())
    try:
        created_timestamp = int(created)
    except (TypeError, ValueError):
        created_timestamp = int(time.time())

    data = [{"b64_json": result} for result in results]
    converted: Dict[str, Any] = {"created": created_timestamp, "data": data}
    for key in ("background", "quality", "size"):
        value = image_request.get(key)
        if value is not None:
            converted[key] = value
    if revised_prompt:
        converted["revised_prompt"] = revised_prompt
    return converted


def filtered_request_headers(headers: Iterable[Tuple[str, str]], translated: bool) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name, value in headers:
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
            continue
        if translated and lowered in {"accept", "accept-encoding"}:
            continue
        result[name] = value
    if translated:
        result["Content-Type"] = "application/json"
        result["Accept"] = "application/json"
        result["Accept-Encoding"] = "identity"
    return result


def filtered_response_headers(headers: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered == "content-length":
            continue
        result.append((name, value))
    return result


def upstream_error_message(error: BaseException) -> str:
    reason = getattr(error, "reason", error)
    detail = str(reason)
    if isinstance(reason, ssl.SSLError) or "EOF occurred in violation of protocol" in detail:
        return (
            "upstream TLS connection ended unexpectedly; the request was not retried "
            "because it may already have reached the upstream service"
        )
    if isinstance(reason, TimeoutError):
        return "upstream request timed out; the request was not retried"
    return "upstream request failed: %s" % detail


def make_handler(config: BridgeConfig):
    class BridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "CodexImageBridge/" + BRIDGE_VERSION

        def log_message(self, fmt: str, *args: Any) -> None:
            logging.info("%s - %s", self.client_address[0], fmt % args)

        def _read_body(self) -> bytes:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                raise ValueError("invalid Content-Length")
            if length < 0 or length > config.max_request_bytes:
                raise ValueError("request body is too large")
            return self.rfile.read(length) if length else b""

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self._write_client(body)
            self.close_connection = True

        def _write_client(self, body: bytes, flush: bool = False) -> bool:
            try:
                self.wfile.write(body)
                if flush:
                    self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                logging.info("client disconnected while receiving %s %s", self.command, self.path)
                self.close_connection = True
                return False

        def _send_error_json(self, status: int, message: str, error_type: str) -> None:
            self._send_json(status, {"error": {"message": message, "type": error_type}})

        def _open_upstream(self, request: urllib.request.Request):
            try:
                return urllib.request.urlopen(request, timeout=config.timeout_seconds)
            except urllib.error.HTTPError as error:
                return error

        def _handle_image(self, action: str, raw_body: bytes) -> None:
            try:
                image_request = json.loads(raw_body.decode("utf-8"))
                if not isinstance(image_request, dict):
                    raise ValueError("image request body must be a JSON object")
                payload = build_responses_payload(image_request, action, config.responses_model)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._send_error_json(400, str(error), "invalid_request_error")
                return

            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                upstream_url(config, config.mount_path + "/responses"),
                data=body,
                headers=filtered_request_headers(self.headers.items(), translated=True),
                method="POST",
            )
            started = time.monotonic()
            try:
                with self._open_upstream(request) as response:
                    response_body = response.read()
                    status = response.status
                    if status < 200 or status >= 300:
                        self._relay_buffered_response(status, response.headers.items(), response_body)
                        return
                    response_payload = json.loads(response_body.decode("utf-8"))
                    converted = convert_responses_to_images(response_payload, image_request)
                    logging.info(
                        "translated image %s completed in %.2fs with %d image(s)",
                        action,
                        time.monotonic() - started,
                        len(converted["data"]),
                    )
                    self._send_json(200, converted)
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as error:
                logging.warning("image upstream failure: %s", error)
                self._send_error_json(502, upstream_error_message(error), "upstream_error")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._send_error_json(502, "image bridge could not convert upstream response: %s" % error, "upstream_error")

        def _relay_buffered_response(
            self, status: int, headers: Iterable[Tuple[str, str]], body: bytes
        ) -> None:
            self.send_response(status)
            for name, value in filtered_response_headers(headers):
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self._write_client(body)
            self.close_connection = True

        def _handle_proxy(self, raw_body: bytes) -> None:
            request = urllib.request.Request(
                upstream_url(config, self.path),
                data=raw_body if raw_body else None,
                headers=filtered_request_headers(self.headers.items(), translated=False),
                method=self.command,
            )
            response_started = False
            try:
                with self._open_upstream(request) as response:
                    self.send_response(response.status)
                    for name, value in filtered_response_headers(response.headers.items()):
                        self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    response_started = True
                    if self.command != "HEAD":
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            if not self._write_client(chunk, flush=True):
                                break
                    self.close_connection = True
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as error:
                logging.warning("proxy upstream failure for %s %s: %s", self.command, self.path, error)
                if response_started:
                    self.close_connection = True
                else:
                    self._send_error_json(502, upstream_error_message(error), "upstream_error")

        def _handle(self) -> None:
            if self.path == "/__codex_image_bridge__/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "version": BRIDGE_VERSION,
                        "python": platform.python_version(),
                        "ssl": ssl.OPENSSL_VERSION,
                        "upstream": config.upstream_base_url,
                        "responses_model": config.responses_model,
                    },
                )
                return
            try:
                raw_body = self._read_body()
            except ValueError as error:
                self._send_error_json(413, str(error), "invalid_request_error")
                return
            action = image_endpoint(config, self.path)
            if action and self.command == "POST":
                self._handle_image(action, raw_body)
            else:
                self._handle_proxy(raw_body)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle
        do_OPTIONS = _handle
        do_HEAD = _handle

    return BridgeHandler


def create_server(host: str, port: int, config: BridgeConfig) -> ThreadingHTTPServer:
    if ":" in host:
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        return IPv6ThreadingHTTPServer((host, port), make_handler(config))
    return ThreadingHTTPServer((host, port), make_handler(config))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CODEX_IMAGE_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODEX_IMAGE_BRIDGE_PORT", "8787")))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CODEX_IMAGE_BRIDGE_UPSTREAM"),
        required=os.environ.get("CODEX_IMAGE_BRIDGE_UPSTREAM") is None,
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_IMAGE_BRIDGE_MODEL", "gpt-5.6-sol"),
    )
    parser.add_argument("--mount", default=os.environ.get("CODEX_IMAGE_BRIDGE_MOUNT", "/openai"))
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("CODEX_IMAGE_BRIDGE_TIMEOUT", "300")),
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("CODEX_IMAGE_BRIDGE_LOG_FILE"),
        help="write logs to this file instead of stderr",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing to listen outside loopback")
    config = BridgeConfig(
        upstream_base_url=args.upstream,
        responses_model=args.model,
        mount_path=args.mount,
        timeout_seconds=args.timeout,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        filename=args.log_file,
        encoding="utf-8" if args.log_file else None,
    )
    server = create_server(args.host, args.port, config)
    display_host = "[%s]" % args.host if ":" in args.host else args.host
    logging.info(
        "Codex image bridge %s listening on http://%s:%d%s -> %s using %s (Python %s)",
        BRIDGE_VERSION,
        display_host,
        args.port,
        config.mount_path,
        config.upstream_base_url,
        config.responses_model,
        sys.version.split()[0],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
