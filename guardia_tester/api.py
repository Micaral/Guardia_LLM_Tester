from __future__ import annotations

import asyncio
import base64
import json
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import Decision


class RequestTemplateError(RuntimeError):
    pass


class CurlKarasenaClient:
    """Replay a browser-captured cURL request, replacing only its JSON `text` field."""

    _DATA_FLAGS = {"--data", "--data-raw", "--data-binary", "-d"}
    _HEADER_FLAGS = {"--header", "-H"}
    _COOKIE_FLAGS = {"--cookie", "-b"}
    _DROP_HEADERS = {"content-length", "host", "accept-encoding"}

    def __init__(self, request_file: str | Path, timeout_seconds: int = 30):
        self.request_file = Path(request_file)
        self.timeout_seconds = timeout_seconds
        self.reload_from_file()

    def reload_from_file(self) -> None:
        """Reload credentials after the user replaces the captured cURL file."""
        self.url, self.method, self.headers, self.body_template = self._parse_file()
        self.token_expires_at = self._authorization_expiry()
        self._validate_token_lifetime()

    def _parse_file(self) -> tuple[str, str, dict[str, str], dict[str, Any]]:
        try:
            content = self.request_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RequestTemplateError(
                f"No se puede leer {self.request_file}. Copia allí la petición como cURL."
            ) from exc
        try:
            tokens = shlex.split(content, posix=True)
        except ValueError as exc:
            raise RequestTemplateError(f"cURL inválido: {exc}") from exc
        if len(tokens) < 2 or tokens[0].lower() not in {"curl", "curl.exe"}:
            raise RequestTemplateError(
                "El archivo no contiene un cURL. En DevTools usa Copy > Copy as cURL (bash)."
            )

        url = tokens[1]
        headers: dict[str, str] = {}
        body_raw: str | None = None
        explicit_method: str | None = None
        index = 2
        while index < len(tokens):
            token = tokens[index]
            if token in self._HEADER_FLAGS and index + 1 < len(tokens):
                raw_header = tokens[index + 1]
                if ":" in raw_header:
                    name, value = raw_header.split(":", 1)
                    if name.strip().lower() not in self._DROP_HEADERS:
                        headers[name.strip()] = value.strip()
                index += 2
            elif token in self._COOKIE_FLAGS and index + 1 < len(tokens):
                headers["Cookie"] = tokens[index + 1]
                index += 2
            elif token in self._DATA_FLAGS and index + 1 < len(tokens):
                body_raw = tokens[index + 1]
                index += 2
            elif token in {"--request", "-X"} and index + 1 < len(tokens):
                explicit_method = tokens[index + 1].upper()
                index += 2
            else:
                index += 1

        if body_raw is None:
            raise RequestTemplateError("La petición no contiene --data-raw con el campo text")
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError as exc:
            raise RequestTemplateError("El cuerpo de la petición no es JSON válido") from exc
        if not isinstance(body, dict) or "text" not in body:
            raise RequestTemplateError("El JSON de la petición no contiene el campo 'text'")
        method = explicit_method or "POST"
        return url, method, headers, body

    async def check(self, prompt: str) -> tuple[Decision, str]:
        self._validate_token_lifetime()
        return await asyncio.to_thread(self._check_sync, prompt)

    def _check_sync(self, prompt: str) -> tuple[Decision, str]:
        body = dict(self.body_template)
        body["text"] = prompt
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, headers=self.headers, method=self.method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RequestTemplateError(
                    "La sesión ha caducado. Captura de nuevo la petición cURL desde Karasena."
                ) from exc
            raise RequestTemplateError(f"Karasena devolvió HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RequestTemplateError(f"No se pudo conectar con Karasena: {exc.reason}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestTemplateError("Karasena devolvió una respuesta que no es JSON") from exc
        return self._parse_response(data)

    def _authorization_expiry(self) -> float | None:
        authorization = next(
            (value for name, value in self.headers.items() if name.lower() == "authorization"),
            None,
        )
        if not authorization:
            return None
        token = authorization.rsplit(" ", 1)[-1]
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            expiry = claims.get("exp")
            return float(expiry) if isinstance(expiry, (int, float)) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _validate_token_lifetime(self) -> None:
        if self.token_expires_at is None:
            return
        remaining = int(self.token_expires_at - time.time())
        if remaining <= 15:
            raise RequestTemplateError(
                "El token del cURL ha caducado. Captura otra petición y ejecuta las pruebas "
                "inmediatamente; Karasena emite tokens de 5 minutos."
            )

    @staticmethod
    def _parse_response(data: Any) -> tuple[Decision, str]:
        if not isinstance(data, dict) or not isinstance(data.get("valido"), bool):
            raise RequestTemplateError("La respuesta no contiene el booleano 'valido'")
        decision: Decision = "allow" if data["valido"] else "block"
        reason_value = data.get("razon")
        reason = "" if reason_value is None else str(reason_value)
        return decision, reason
