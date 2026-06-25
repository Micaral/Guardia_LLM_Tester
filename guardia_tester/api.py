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


class KarasenaAPIClient:
    """Native Karasena API client authenticated with a static Bearer JWT token."""

    DEFAULT_BASE_URL = "https://guardia.karasena.com"

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        platform_code: str | None = None,
        timeout_seconds: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.platform_code = platform_code
        self.timeout_seconds = timeout_seconds
        self._token = token
        self.token_expires_at = self._decode_expiry(token)
        self._validate_token_lifetime()

    def reload_token(self, new_token: str) -> None:
        """Swap in a fresh token mid-run without losing accumulated results."""
        self._token = new_token
        self.token_expires_at = self._decode_expiry(new_token)
        self._validate_token_lifetime()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _validate_token_lifetime(self) -> None:
        if self.token_expires_at is None:
            return
        if time.time() >= self.token_expires_at - 15:
            raise RequestTemplateError(
                "El token ha caducado. Actualiza KARASENA_TOKEN en .env y vuelve a ejecutar."
            )

    async def check(self, prompt: str) -> tuple[Decision, str]:
        self._validate_token_lifetime()
        return await asyncio.to_thread(self._check_sync, prompt)

    def _check_sync(self, prompt: str) -> tuple[Decision, str]:
        body: dict[str, Any] = {"text": prompt}
        if self.platform_code:
            body["plataforma"] = self.platform_code
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(
            f"{self.base_url}/api/comprobador", data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RequestTemplateError(
                    "La sesión ha caducado. Actualiza KARASENA_TOKEN en .env."
                ) from exc
            raise RequestTemplateError(f"Karasena devolvió HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RequestTemplateError(f"No se pudo conectar con Karasena: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestTemplateError("Karasena devolvió una respuesta que no es JSON") from exc
        return _parse_comprobador_response(data)

    def _get_json(self, path: str) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._auth_headers(), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _put_json(self, path: str, body: dict) -> Any:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=payload, headers=headers, method="PUT"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RequestTemplateError("Sesión caducada durante la actualización del prompt.") from exc
            raise RequestTemplateError(f"Karasena devolvió HTTP {exc.code} al actualizar el prompt.") from exc
        except urllib.error.URLError as exc:
            raise RequestTemplateError(f"No se pudo conectar con Karasena: {exc.reason}") from exc

    def get_my_company(self) -> dict | None:
        result = self._get_json("/api/companies/mycompany")
        return result if isinstance(result, dict) else None

    def list_platforms(self) -> list[dict]:
        result = self._get_json("/api/plataformas")
        return result if isinstance(result, list) else []

    def get_user_context(self) -> dict | None:
        result = self._get_json("/api/usuarios/contexto")
        return result if isinstance(result, dict) else None

    def get_prompts(self) -> list[dict]:
        result = self._get_json("/api/prompts/mycompany")
        return result if isinstance(result, list) else []

    def get_prompt_by_id(self, prompt_id: int) -> dict | None:
        result = self._get_json(f"/api/prompts/mycompany/{prompt_id}")
        return result if isinstance(result, dict) else None

    def set_prompt_active(self, prompt_id: int, active: bool) -> dict:
        current = self.get_prompt_by_id(prompt_id)
        if current is None:
            raise RequestTemplateError(f"Prompt {prompt_id} no encontrado en esta empresa.")
        body: dict[str, Any] = {
            "content": current.get("content", ""),
            "tipoBloqueoId": current.get("tipoBloqueoId"),
            "nombre": current.get("nombre", ""),
            "comprobable": active,
        }
        result = self._put_json(f"/api/prompts/mycompany/{prompt_id}", body)
        if result is None:
            raise RequestTemplateError(f"No se pudo actualizar el prompt {prompt_id}.")
        return result

    def get_blocking_types(self) -> dict[int, str]:
        result = self._get_json("/api/tipos-bloqueo")
        if not isinstance(result, list):
            return {}
        return {item["id"]: item.get("nombre", str(item["id"])) for item in result if "id" in item}

    def get_prohibited_topics(self) -> list[dict]:
        result = self._get_json("/api/temas-prohibidos/mycompany")
        return result if isinstance(result, list) else []

    @staticmethod
    def _decode_expiry(token: str) -> float | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            expiry = claims.get("exp")
            return float(expiry) if isinstance(expiry, (int, float)) else None
        except (ValueError, json.JSONDecodeError):
            return None


def _parse_comprobador_response(data: Any) -> tuple[Decision, str]:
    if not isinstance(data, dict) or not isinstance(data.get("valido"), bool):
        raise RequestTemplateError("La respuesta no contiene el booleano 'valido'")
    decision: Decision = "allow" if data["valido"] else "block"
    reason_value = data.get("razon")
    return decision, ("" if reason_value is None else str(reason_value))


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
        return _parse_comprobador_response(data)

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
        return _parse_comprobador_response(data)
