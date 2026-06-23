from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .config import BrowserConfig
from .models import Decision


class BrowserSetupError(RuntimeError):
    pass


class KarasenaChecker:
    """Drive the Karasena checker with a persistent, user-authenticated Chrome profile."""

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None

    async def __aenter__(self) -> "KarasenaChecker":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserSetupError(
                "Falta Playwright. Instálalo con: python -m pip install -r requirements.txt"
            ) from exc

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir.resolve()),
                channel="chrome",
                headless=not self.config.headed,
                slow_mo=self.config.slow_mo_ms,
                viewport=None if self.config.headed else {"width": 1440, "height": 1000},
                args=["--start-maximized"] if self.config.headed else [],
            )
        except Exception as exc:
            await self._playwright.stop()
            raise BrowserSetupError(
                "No se pudo abrir Google Chrome. Comprueba que está instalado y que el perfil "
                "de pruebas no está abierto en otra ejecución."
            ) from exc

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.config.timeout_ms)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()

    async def open(self) -> None:
        await self.page.goto(self.config.url, wait_until="domcontentloaded")

    async def ensure_checker_ready(self) -> None:
        if await self._find_check_button(required=False):
            return
        await self._try_open_checker()
        if not await self._find_check_button(required=False):
            raise BrowserSetupError(
                "No encuentro el comprobador. Si la sesión ha caducado, ejecuta primero "
                "'python run_tests.py --login'."
            )

    async def check(self, prompt: str) -> tuple[Decision, str]:
        await self.ensure_checker_ready()
        text_input = await self._find_text_input()
        button = await self._find_check_button(required=True)

        await text_input.fill(prompt)
        # Most versions clear the previous result when the input changes.
        await self.page.wait_for_timeout(100)
        previous = await self._visible_result_text()
        await button.click()

        deadline = time.monotonic() + self.config.timeout_ms / 1000
        saw_busy = False
        while time.monotonic() < deadline:
            try:
                if not await button.is_enabled():
                    saw_busy = True
            except Exception:
                button = await self._find_check_button(required=True)
            result = await self._visible_result_text()
            if result and (saw_busy or result != previous):
                return self._parse_result(result)
            await self.page.wait_for_timeout(200)

        # A first test can occasionally return the same label before the button exposes busy state.
        result = await self._visible_result_text()
        if result:
            return self._parse_result(result)
        raise TimeoutError("Karasena no mostró un resultado dentro del tiempo configurado")

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True)

    async def _try_open_checker(self) -> None:
        candidates = [
            re.compile(r"comprobador", re.IGNORECASE),
            re.compile(r"comprobar texto", re.IGNORECASE),
            re.compile(r"nueva verificaci[oó]n", re.IGNORECASE),
            re.compile(r"verificar texto", re.IGNORECASE),
        ]
        for name in candidates:
            for role in ("button", "link"):
                locator = self.page.get_by_role(role, name=name)
                if await locator.count() and await locator.first.is_visible():
                    await locator.first.click()
                    await self.page.wait_for_timeout(250)
                    return

    async def _find_check_button(self, required: bool) -> Any | None:
        root = await self._checker_root()
        locator = root.get_by_role(
            "button", name=re.compile(r"comprobar\s+texto", re.IGNORECASE)
        )
        for index in range(await locator.count()):
            item = locator.nth(index)
            if await item.is_visible():
                return item
        if required:
            raise BrowserSetupError("No encuentro el botón 'Comprobar texto'")
        return None

    async def _find_text_input(self) -> Any:
        root = await self._checker_root()
        by_label = root.get_by_label(re.compile(r"texto\s+a\s+comprobar", re.IGNORECASE))
        for index in range(await by_label.count()):
            item = by_label.nth(index)
            if await item.is_visible():
                return item

        label = root.get_by_text(
            re.compile(r"^texto\s+a\s+comprobar$", re.IGNORECASE), exact=True
        )
        if await label.count():
            following = label.last.locator("xpath=following::textarea[1]")
            if await following.count() and await following.is_visible():
                return following

        visible: list[Any] = []
        textareas = root.locator("textarea")
        for index in range(await textareas.count()):
            item = textareas.nth(index)
            if await item.is_visible() and await item.is_editable():
                visible.append(item)
        if visible:
            # In the current UI the prompt textarea follows the optional justification textarea.
            return visible[-1]
        raise BrowserSetupError("No encuentro el campo 'Texto a comprobar'")

    async def _visible_result_text(self) -> str:
        root = await self._checker_root()
        body_text = await root.inner_text()
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            normalized = self._normalize(line)
            if normalized in {"no valido", "bloqueado", "valido", "permitido"}:
                following = lines[index + 1 : index + 3]
                return "\n".join([line, *following])
        return ""

    async def _checker_root(self) -> Any:
        """Prefer the checker dialog so statuses elsewhere on the page cannot be mistaken for a result."""
        dialogs = self.page.get_by_role("dialog")
        for index in range(await dialogs.count()):
            dialog = dialogs.nth(index)
            if not await dialog.is_visible():
                continue
            text = self._normalize(await dialog.inner_text())
            if "comprobador" in text or "comprobar texto" in text:
                return dialog

        title = self.page.get_by_text(re.compile(r"^comprobador$", re.IGNORECASE), exact=True)
        for index in range(await title.count()):
            item = title.nth(index)
            if not await item.is_visible():
                continue
            container = item.locator(
                "xpath=ancestor::*[.//textarea and .//button[contains(normalize-space(.), "
                "'Comprobar texto')]][1]"
            )
            if await container.count():
                return container
        return self.page.locator("body")

    @classmethod
    def _parse_result(cls, text: str) -> tuple[Decision, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        label = cls._normalize(lines[0]) if lines else ""
        if label in {"no valido", "bloqueado"}:
            decision: Decision = "block"
        elif label in {"valido", "permitido"}:
            decision = "allow"
        else:
            raise ValueError(f"Resultado desconocido: {text!r}")
        reason = " ".join(lines[1:]).strip()
        return decision, reason

    @staticmethod
    def _normalize(value: str) -> str:
        import unicodedata

        decomposed = unicodedata.normalize("NFKD", value)
        return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())
