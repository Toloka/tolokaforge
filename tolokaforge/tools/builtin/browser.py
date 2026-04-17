"""Browser tool using Playwright - Gemini Computer Use API compatible"""

import asyncio
import base64
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - optional dependency
    async_playwright = None  # type: ignore[assignment]

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class BrowserTool(Tool):
    """Headless browser automation with coordinate-based Gemini Computer Use actions"""

    # All supported action types
    ALL_ACTIONS = [
        "open_web_browser",
        "navigate",
        "wait_5_seconds",
        "screenshot",
        "go_back",
        "go_forward",
        "search",
        "click_at",
        "select",
        "hover_at",
        "type_text_at",
        "key_combination",
        "scroll_document",
        "scroll_at",
        "drag_and_drop",
    ]
    KEY_ALIASES = {
        "CTRL": "Control",
        "CONTROL": "Control",
        "ALT": "Alt",
        "OPTION": "Alt",
        "SHIFT": "Shift",
        "META": "Meta",
        "CMD": "Meta",
        "COMMAND": "Meta",
        "ENTER": "Enter",
        "RETURN": "Enter",
        "TAB": "Tab",
        "ESCAPE": "Escape",
        "ESC": "Escape",
        "UP": "ArrowUp",
        "DOWN": "ArrowDown",
        "LEFT": "ArrowLeft",
        "RIGHT": "ArrowRight",
        "BACKSPACE": "Backspace",
        "DEL": "Delete",
        "DELETE": "Delete",
        "SPACE": "Space",
        "SPACEBAR": "Space",
        "HOME": "Home",
        "END": "End",
        "PAGEUP": "PageUp",
        "PAGEDOWN": "PageDown",
    }
    TRANSIENT_DRIVER_ERRORS = (
        "Connection closed while reading from the driver",
        "Target page, context or browser has been closed",
        "Browser has been closed",
        "Channel closed",
    )

    def __init__(
        self,
        screenshots_dir: str = "/tmp/screenshots",
        viewport_width: int = 1440,
        viewport_height: int = 900,
        initial_url: str | None = None,
        allowed_actions: list[str] | None = None,
        headless: bool = True,
        video_dir: str | None = None,
        visual_mode: bool = False,
        db_base_url: str | None = None,
    ):
        if async_playwright is None:
            raise ImportError(
                "BrowserTool requires Playwright. Install with: pip install 'tolokaforge[browser]'"
            )
        policy = ToolPolicy(
            timeout_s=60.0,
            category=ToolCategory.COMPUTE,
            visibility=["agent"],
        )
        super().__init__(
            name="browser",
            description="Control a headless browser using coordinate-based actions (Gemini Computer Use API)",
            policy=policy,
        )
        self.screenshots_dir = screenshots_dir
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.initial_url = initial_url
        self.allowed_actions = allowed_actions or self.ALL_ACTIONS
        self.headless = headless
        self.video_dir = video_dir
        self.video_path = None
        self.visual_mode = visual_mode
        self.db_base_url = db_base_url
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._loop = None  # Persistent event loop across tool calls
        # Coordinate grid is 1000x1000
        self.GRID_SIZE = 1000

    @classmethod
    def _is_transient_driver_error(cls, error: str | None) -> bool:
        if not error:
            return False
        return any(marker in error for marker in cls.TRANSIENT_DRIVER_ERRORS)

    def _context_options(self) -> dict[str, Any]:
        context_options: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height}
        }
        if self.video_dir:
            import os

            os.makedirs(self.video_dir, exist_ok=True)
            context_options["record_video_dir"] = self.video_dir
            context_options["record_video_size"] = {
                "width": self.viewport_width,
                "height": self.viewport_height,
            }
        return context_options

    async def _reset_browser_state(self):
        """Reset browser resources after transient driver failures."""
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "description": "Sequence of browser actions to perform (Gemini Computer Use API)",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": self.allowed_actions,
                                    },
                                    "url": {
                                        "type": "string",
                                        "description": "URL for navigate action",
                                    },
                                    "query": {
                                        "type": "string",
                                        "description": "Search query for search action",
                                    },
                                    "x": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "X coordinate (0-999 for viewport, >999 for scrollable content)",
                                    },
                                    "y": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "Y coordinate (0-999 for viewport, >999 for scrollable content - will auto-scroll)",
                                    },
                                    "destination_x": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "Destination X coordinate for drag_and_drop",
                                    },
                                    "destination_y": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "Destination Y coordinate for drag_and_drop",
                                    },
                                    "text": {
                                        "type": "string",
                                        "description": "Text to type for type_text_at action or option label for select action",
                                    },
                                    "press_enter": {
                                        "type": "boolean",
                                        "description": "Whether to press Enter after typing (default: True)",
                                    },
                                    "clear_before_typing": {
                                        "type": "boolean",
                                        "description": "Whether to clear field before typing (default: True)",
                                    },
                                    "keys": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Array of key tokens (e.g., ['CTRL', 'C'] or ['ALT', 'TAB'])",
                                    },
                                    "direction": {
                                        "type": "string",
                                        "enum": ["up", "down", "left", "right"],
                                        "description": "Scroll direction",
                                    },
                                    "magnitude": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 999,
                                        "description": "Scroll magnitude (default: 800)",
                                    },
                                },
                                "required": ["type"],
                            },
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False,
                },
            },
        }

    def _grid_to_pixel(self, grid_x: int, grid_y: int) -> tuple[float, float]:
        """Convert grid coordinates to actual pixel coordinates.

        For coordinates 0-999, maps to viewport (standard 1000x1000 grid).
        For coordinates >999, treats them as absolute page coordinates and
        the caller should scroll to bring the element into view.
        """
        pixel_x = (grid_x / self.GRID_SIZE) * self.viewport_width
        pixel_y = (grid_y / self.GRID_SIZE) * self.viewport_height
        return pixel_x, pixel_y

    def _normalize_key_combination(self, keys: list[str] | str) -> str:
        """Normalize key aliases to Playwright-compatible key names."""
        if isinstance(keys, list):
            normalized = [self.KEY_ALIASES.get(str(k).upper(), str(k)) for k in keys]
            return "+".join(normalized)
        if isinstance(keys, str):
            tokens = [tok.strip() for tok in keys.split("+")]
            normalized_tokens = [self.KEY_ALIASES.get(tok.upper(), tok) for tok in tokens if tok]
            return "+".join(normalized_tokens) if normalized_tokens else keys
        return str(keys)

    async def _scroll_to_and_click(self, grid_x: int, grid_y: int):
        """Scroll to bring element into view if needed, then click.

        Handles coordinates beyond the viewport by scrolling first.
        Also handles fixed-position elements like modals.
        """
        pixel_x, pixel_y = self._grid_to_pixel(grid_x, grid_y)

        # If coordinates are within viewport (0-999), try clicking directly
        if grid_y <= 999:
            # First try to find and click an element at this position
            # This handles fixed-position modals better
            element = await self.page.evaluate(
                f"""
                () => {{
                    const elem = document.elementFromPoint({pixel_x}, {pixel_y});
                    const stack = document.elementsFromPoint({pixel_x}, {pixel_y});
                    if (elem || (stack && stack.length)) {{
                        const fireClick = (node) => {{
                            if (!node) return false;
                            if (typeof node.click === 'function') {{
                                node.click();
                                return true;
                            }}
                            try {{
                                node.dispatchEvent(new MouseEvent('click', {{ bubbles: true }}));
                                return true;
                            }} catch (err) {{
                                return false;
                            }}
                        }};

                        const isClickable = (node) => {{
                            if (!node || !node.tagName) return false;
                            if (node.onclick || node.hasAttribute('onclick')) return true;
                            return node.tagName === 'BUTTON' || node.tagName === 'A' ||
                                node.tagName === 'INPUT' || node.getAttribute('role') === 'button';
                        }};

                        // Prefer clickable elements from the stacking context (handles overlays)
                        if (stack && stack.length) {{
                            const clickable = stack.find(isClickable);
                            if (clickable && fireClick(clickable)) return 'clicked';
                        }}

                        // Check if element or parent has onclick
                        let current = elem;
                        while (current && current !== document.body) {{
                            if (isClickable(current)) {{
                                if (fireClick(current)) return 'clicked';
                            }}
                            current = current.parentElement;
                        }}
                        // Fallback: click the top element directly
                        if (fireClick(elem || stack[0])) return 'clicked';
                        return 'not_clickable';
                    }}
                    return 'not_found';
                }}
            """
            )

            # If JavaScript click didn't work, fall back to mouse click
            if element != "clicked":
                await self.page.mouse.click(pixel_x, pixel_y)
            return

        # For coordinates > 999, we need to scroll
        # Calculate how many "screens" down we need to scroll
        screens_down = grid_y // self.GRID_SIZE
        remainder_y = grid_y % self.GRID_SIZE

        # Scroll down by the required amount
        scroll_amount = screens_down * self.viewport_height
        await self.page.evaluate(f"window.scrollTo(0, {scroll_amount})")
        await asyncio.sleep(0.3)  # Wait for scroll to complete

        # Now click at the remainder position within the viewport
        pixel_x = (grid_x / self.GRID_SIZE) * self.viewport_width
        pixel_y = (remainder_y / self.GRID_SIZE) * self.viewport_height

        # Try JavaScript click first for better modal handling
        element = await self.page.evaluate(
            f"""
            () => {{
                const elem = document.elementFromPoint({pixel_x}, {pixel_y});
                if (elem) {{
                    let current = elem;
                    while (current && current !== document.body) {{
                        if (current.onclick || current.hasAttribute('onclick')) {{
                            current.click();
                            return 'clicked';
                        }}
                        if (current.tagName === 'BUTTON' || current.tagName === 'A' ||
                            current.tagName === 'INPUT' || current.getAttribute('role') === 'button') {{
                            current.click();
                            return 'clicked';
                        }}
                        current = current.parentElement;
                    }}
                    elem.click();
                    return 'clicked';
                }}
                return 'not_found';
            }}
        """
        )

        if element != "clicked":
            await self.page.mouse.click(pixel_x, pixel_y)

    async def _ensure_browser(self):
        """Ensure browser is initialized"""
        if self.browser:
            try:
                if not self.browser.is_connected():
                    await self._reset_browser_state()
            except Exception:
                await self._reset_browser_state()

        if self.page:
            try:
                if self.page.is_closed():
                    self.page = None
            except Exception:
                self.page = None

        if self.context:
            try:
                _ = self.context.pages
            except Exception:
                self.context = None

        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=self.headless)

        if not self.context:
            self.context = await self.browser.new_context(**self._context_options())
            if self.db_base_url:
                await self.context.add_init_script(f"window.__JSON_DB_BASE = '{self.db_base_url}';")

        if not self.page:
            self.page = await self.context.new_page()
            if self.initial_url:
                url = self._resolve_url(self.initial_url)
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self._inject_db_namespace()

    async def _inject_db_namespace(self):
        """Inject JSON DB namespace override into the page for per-trial isolation."""
        if self.db_base_url and self.page:
            await self.page.evaluate(f"window.__JSON_DB_BASE = '{self.db_base_url}'")

    async def _capture_screenshot_blocks(self, action_summary: str) -> list[dict[str, Any]]:
        """Capture a screenshot and return it as multimodal content blocks."""
        screenshot_bytes = await self.page.screenshot(type="png")
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        # Anthropic-native image block format. Other providers may require translation.
        return [
            {"type": "text", "text": action_summary},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
        ]

    async def _get_page_observation(self) -> str:
        """Get page observation: DOM text in normal mode, empty in visual mode (screenshots used instead)."""
        if self.visual_mode:
            return await self._get_interactive_observation()
        return await self._get_page_content()

    async def _get_interactive_observation(self) -> str:
        """Return a compact observation focused on interactive elements."""
        content = await self._get_page_content()
        marker = "Interactive Elements"
        if marker in content:
            head = content.splitlines()[0] if content else ""
            return "\n".join([head, content[content.index(marker) :]])
        return content

    async def _get_page_content(self) -> str:
        """Extract page content as text (accessibility tree representation)"""
        try:
            # Get page title
            title = await self.page.title()

            # Get visible text content
            text_content = await self.page.evaluate(
                """() => {
                // Extract visible text from body
                const body = document.body;
                if (!body) return '';

                // Helper function to check if element or any ancestor is hidden
                const isVisible = (element) => {
                    let current = element;
                    while (current && current !== document.body) {
                        const style = window.getComputedStyle(current);
                        const rect = current.getBoundingClientRect();

                        // Check CSS visibility properties
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }

                        // Skip elements outside the viewport to avoid exposing offscreen text
                        if (
                            rect.bottom <= 0 ||
                            rect.top >= window.innerHeight ||
                            rect.right <= 0 ||
                            rect.left >= window.innerWidth
                        ) {
                            return false;
                        }

                        // Check if element is collapsed (has no height)
                        // Elements with max-height: 0, overflow: hidden will have height = 0
                        if (rect.height === 0) {
                            return false;
                        }

                        current = current.parentElement;
                    }
                    return true;
                };

                // Get all text nodes
                const walker = document.createTreeWalker(
                    body,
                    NodeFilter.SHOW_TEXT,
                    {
                        acceptNode: (node) => {
                            // Skip hidden elements - check all ancestors
                            const parent = node.parentElement;
                            if (!parent) return NodeFilter.FILTER_REJECT;
                            if (!isVisible(parent)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            // Only include non-empty text
                            if (node.textContent.trim().length === 0) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );

                const texts = [];
                let node;
                while (node = walker.nextNode()) {
                    texts.push(node.textContent.trim());
                }

                return texts.join('\\n');
            }"""
            )

            # Get interactive elements (links, buttons, inputs, elements with onclick)
            interactive = await self.page.evaluate(
                """() => {
                const elements = [];
                const seen = new Set();  // Track unique elements by coordinates

                const bestLabel = (item) => {
                    const normalized = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const candidates = [
                        normalized(item.innerText),
                        normalized(item.textContent),
                        normalized(item.getAttribute('aria-label')),
                        normalized(item.getAttribute('title')),
                        normalized(item.getAttribute('placeholder')),
                    ];
                    for (const candidate of candidates) {
                        if (candidate.length > 0) return candidate.substring(0, 50);
                    }
                    return '';
                };

                // Get standard interactive elements
                const selectors = ['a', 'button', 'input', 'select', 'textarea'];
                for (const selector of selectors) {
                    const items = document.querySelectorAll(selector);
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const inView =
                            rect.bottom > 0 &&
                            rect.top < window.innerHeight &&
                            rect.right > 0 &&
                            rect.left < window.innerWidth;
                        if (rect.width > 0 && rect.height > 0 && inView) {
                            const left = Math.max(rect.left, 0);
                            const right = Math.min(rect.right, window.innerWidth);
                            const top = Math.max(rect.top, 0);
                            const bottom = Math.min(rect.bottom, window.innerHeight);
                            if (right <= left || bottom <= top) {
                                continue;
                            }
                            const x = Math.round(((left + right) / 2) / window.innerWidth * 1000);
                            const y = Math.round(((top + bottom) / 2) / window.innerHeight * 1000);
                            const text = bestLabel(item);
                            const key = `${x},${y},${text}`;
                            if (!seen.has(key)) {
                                seen.add(key);
                                elements.push({
                                    tag: item.tagName.toLowerCase(),
                                    text: text,
                                    type: item.type || '',
                                    href: item.href || '',
                                    x: x,
                                    y: y
                                });
                            }
                        }
                    }
                }

                // Get elements with onclick handlers (like FAQ questions)
                const clickableItems = document.querySelectorAll('[onclick]');
                for (const item of clickableItems) {
                    const rect = item.getBoundingClientRect();
                    const inView =
                        rect.bottom > 0 &&
                        rect.top < window.innerHeight &&
                        rect.right > 0 &&
                        rect.left < window.innerWidth;
                    if (rect.width > 0 && rect.height > 0 && inView) {
                        const left = Math.max(rect.left, 0);
                        const right = Math.min(rect.right, window.innerWidth);
                        const top = Math.max(rect.top, 0);
                        const bottom = Math.min(rect.bottom, window.innerHeight);
                        if (right <= left || bottom <= top) {
                            continue;
                        }
                        const x = Math.round(((left + right) / 2) / window.innerWidth * 1000);
                        const y = Math.round(((top + bottom) / 2) / window.innerHeight * 1000);
                        const text = bestLabel(item);
                        const key = `${x},${y},${text}`;
                        if (!seen.has(key)) {
                            seen.add(key);
                            elements.push({
                                tag: item.tagName.toLowerCase() + '[onclick]',
                                text: text,
                                type: '',
                                href: '',
                                x: x,
                                y: y
                            });
                        }
                    }
                }

                return elements;
            }"""
            )

            # Format output
            result = f"Page: {title}\n\n"
            result += f"Content:\n{text_content[:2000]}\n\n"

            if interactive:
                result += "Interactive Elements (with approximate grid coordinates):\n"
                for elem in interactive[:120]:  # Limit to first 120
                    elem_info = f"  [{elem['tag']}]"
                    if elem["text"]:
                        elem_info += f" {elem['text']}"
                    if elem["href"]:
                        elem_info += f" (href: {elem['href']})"
                    elem_info += f" @ ({elem['x']}, {elem['y']})"
                    result += elem_info + "\n"

            return result

        except Exception as e:
            return f"Could not extract page content: {str(e)}"

    @staticmethod
    def _resolve_url(url: str) -> str:
        """Normalize Docker-internal hostnames to localhost equivalents.

        Legacy compatibility shim. Inside Docker containers the service
        hostnames (mock-web, json-db, etc.) resolve natively, so the
        replacements are effectively no-ops. Kept for safety in case the
        browser tool is ever exercised outside a container network.
        """
        # Map Docker service hostnames to localhost equivalents
        docker_hosts = {
            "mock-web:8080": "localhost:8080",
            "mock-web": "localhost",
            "json-db:8000": "localhost:8000",
            "json-db": "localhost",
            "rag-service:8001": "localhost:8001",
            "rag-service": "localhost",
        }
        for docker_host, local_host in docker_hosts.items():
            if docker_host in url:
                return url.replace(docker_host, local_host)
        return url

    def _normalize_navigation_url(self, url: str) -> str:
        """Normalize navigation URL and remap common wrong localhost ports.

        Agents often guess local dev ports (3000/4000/8081/etc.) for browser tasks.
        When a task has an explicit initial_url, prefer that origin.
        """
        resolved = self._resolve_url(url)
        if not self.initial_url:
            return resolved

        initial_resolved = self._resolve_url(self.initial_url)
        try:
            parsed_target = urlparse(resolved)
            parsed_initial = urlparse(initial_resolved)
            if (
                parsed_target.hostname in {"localhost", "127.0.0.1"}
                and parsed_initial.hostname in {"localhost", "127.0.0.1"}
                and parsed_target.netloc != parsed_initial.netloc
            ):
                parsed_target = parsed_target._replace(netloc=parsed_initial.netloc)
                return urlunparse(parsed_target)
        except Exception:
            return resolved

        return resolved

    async def _execute_actions(self, actions: list[dict[str, Any]]) -> tuple[bool, str, str]:
        """Execute sequence of browser actions"""
        try:
            results = []

            for action in actions:
                action_type = action["type"]

                if action_type == "open_web_browser":
                    await self._ensure_browser()
                    results.append("Browser opened")

                elif action_type == "navigate":
                    await self._ensure_browser()
                    url = self._normalize_navigation_url(action["url"])
                    await self.page.goto(url, timeout=30000)
                    await self._inject_db_namespace()
                    content = await self._get_page_observation()
                    results.append(
                        f"Navigated to {url}\n\n{content}" if content else f"Navigated to {url}"
                    )

                elif action_type == "screenshot":
                    await self._ensure_browser()
                    screenshot = await self.page.screenshot(type="png")
                    image_b64 = base64.b64encode(screenshot).decode("utf-8")
                    results.append(
                        "Captured screenshot (base64 PNG): " + image_b64[:120] + "...[truncated]"
                    )

                elif action_type == "wait_5_seconds":
                    await asyncio.sleep(5)
                    results.append("Waited 5 seconds")

                elif action_type == "go_back":
                    await self._ensure_browser()
                    await self.page.go_back()
                    await self._inject_db_namespace()
                    content = await self._get_page_observation()
                    results.append(f"Navigated back\n\n{content}" if content else "Navigated back")

                elif action_type == "go_forward":
                    await self._ensure_browser()
                    await self.page.go_forward()
                    await self._inject_db_namespace()
                    content = await self._get_page_observation()
                    results.append(
                        f"Navigated forward\n\n{content}" if content else "Navigated forward"
                    )

                elif action_type == "search":
                    await self._ensure_browser()
                    query = action["query"]
                    # Navigate to Google search
                    search_url = f"https://www.google.com/search?q={query}"
                    await self.page.goto(search_url, timeout=30000)
                    content = await self._get_page_observation()
                    results.append(
                        f"Searched for: {query}\n\n{content}"
                        if content
                        else f"Searched for: {query}"
                    )

                elif action_type == "click_at":
                    await self._ensure_browser()
                    x = action.get("x", 500)
                    y = action.get("y", 200)
                    # Use scroll_to_and_click for coordinates beyond viewport
                    await self._scroll_to_and_click(x, y)
                    # Wait a bit for any page changes
                    await asyncio.sleep(0.5)
                    content = await self._get_page_observation()
                    results.append(
                        f"Clicked at ({x}, {y})\n\n{content}"
                        if content
                        else f"Clicked at ({x}, {y})"
                    )

                elif action_type == "select":
                    await self._ensure_browser()
                    x = action.get("x", 500)
                    y = action.get("y", 200)
                    option_text = action.get("text") or action.get("option") or action.get("value")
                    pixel_x, pixel_y = self._grid_to_pixel(x, y)
                    did_select = await self.page.evaluate(
                        """(payload) => {
                        const px = payload.x;
                        const py = payload.y;
                        const text = payload.text;

                        const selects = Array.from(document.querySelectorAll('select'));
                        const visibleSelects = selects.filter(el => {
                          const rect = el.getBoundingClientRect();
                          if (!rect || rect.width === 0 || rect.height === 0) return false;
                          const style = window.getComputedStyle(el);
                          return style && style.visibility !== 'hidden' && style.display !== 'none';
                        });

                        const distTo = (rect) => {
                          const cx = rect.left + rect.width / 2;
                          const cy = rect.top + rect.height / 2;
                          const inside = px >= rect.left && px <= rect.right && py >= rect.top && py <= rect.bottom;
                          return inside ? 0 : Math.hypot(cx - px, cy - py);
                        };

                        let target = null;
                        let bestDist = Infinity;
                        for (const el of visibleSelects) {
                          const rect = el.getBoundingClientRect();
                          const d = distTo(rect);
                          if (d < bestDist) {
                            bestDist = d;
                            target = el;
                          }
                        }

                        if (!target) {
                          const active = document.activeElement;
                          if (active && active.tagName === 'SELECT') target = active;
                        }
                        if (!target) return false;

                        if (!text) {
                          target.focus();
                          target.click();
                          return true;
                        }

                        const needle = String(text).trim().toLowerCase();
                        const options = Array.from(target.options || []);
                        const exact = options.find(o => (o.textContent || '').trim().toLowerCase() === needle);
                        const byValue = options.find(o => (o.value || '').trim().toLowerCase() === needle);
                        const partial = options.find(o => (o.textContent || '').toLowerCase().includes(needle));
                        const chosen = exact || byValue || partial;
                        if (!chosen) return false;

                        target.value = chosen.value;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                      }""",
                        {"x": pixel_x, "y": pixel_y, "text": option_text},
                    )
                    content = await self._get_page_observation()
                    if option_text:
                        msg = (
                            f"Selected option '{option_text}' at ({x}, {y})"
                            if did_select
                            else f"Failed to select '{option_text}' at ({x}, {y})"
                        )
                    else:
                        msg = (
                            f"Opened select at ({x}, {y})"
                            if did_select
                            else f"Failed to open select at ({x}, {y})"
                        )
                    results.append(f"{msg}\n\n{content}" if content else msg)

                elif action_type == "hover_at":
                    await self._ensure_browser()
                    x = action["x"]
                    y = action["y"]
                    pixel_x, pixel_y = self._grid_to_pixel(x, y)
                    await self.page.mouse.move(pixel_x, pixel_y)
                    results.append(f"Hovered at ({x}, {y})")

                elif action_type == "type_text_at":
                    await self._ensure_browser()
                    has_coords = "x" in action and "y" in action
                    x = action.get("x", 500)
                    y = action.get("y", 200)
                    text = action["text"]
                    clear_before_typing = action.get("clear_before_typing", True)
                    press_enter = action.get("press_enter", True)

                    typed_via_dom = False
                    if has_coords:
                        # Click at position first to focus
                        pixel_x, pixel_y = self._grid_to_pixel(x, y)
                        await self.page.mouse.click(pixel_x, pixel_y)
                        await asyncio.sleep(0.2)

                        typed_via_dom = await self.page.evaluate(
                            """(payload) => {
                            const px = payload.x;
                            const py = payload.y;
                            const text = payload.text;
                            const clear = payload.clear;

                            const candidates = Array.from(
                              document.querySelectorAll('input,textarea,[contenteditable="true"],[contenteditable=""],[contenteditable="plaintext-only"]')
                            );

                            const isEditable = (el) => {
                              if (!el) return false;
                              if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return true;
                              return !!el.isContentEditable;
                            };

                            let best = null;
                            let bestDist = Infinity;
                            for (const el of candidates) {
                              if (!isEditable(el)) continue;
                              const rect = el.getBoundingClientRect();
                              if (!rect || rect.width === 0 || rect.height === 0) continue;
                              const inside = px >= rect.left && px <= rect.right && py >= rect.top && py <= rect.bottom;
                              const cx = rect.left + rect.width / 2;
                              const cy = rect.top + rect.height / 2;
                              const dist = inside ? 0 : Math.hypot(cx - px, cy - py);
                              if (dist < bestDist) {
                                bestDist = dist;
                                best = el;
                              }
                            }

                            if (!best) {
                              const active = document.activeElement;
                              if (isEditable(active)) {
                                best = active;
                              }
                            }

                            if (!best) return false;
                            best.focus();

                            if (best.tagName === 'INPUT' || best.tagName === 'TEXTAREA') {
                              const nextValue = clear ? text : `${best.value || ''}${text}`;
                              best.value = nextValue;
                            } else if (best.isContentEditable) {
                              const nextValue = clear ? text : `${best.textContent || ''}${text}`;
                              best.textContent = nextValue;
                            }

                            best.dispatchEvent(new Event('input', { bubbles: true }));
                            best.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                          }""",
                            {
                                "x": pixel_x,
                                "y": pixel_y,
                                "text": text,
                                "clear": clear_before_typing,
                            },
                        )
                    else:
                        typed_via_dom = await self.page.evaluate(
                            """(payload) => {
                            const text = payload.text;
                            const clear = payload.clear;

                            const candidates = Array.from(
                              document.querySelectorAll('input,textarea,[contenteditable="true"],[contenteditable=""],[contenteditable="plaintext-only"]')
                            );

                            const isEditable = (el) => {
                              if (!el) return false;
                              if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return true;
                              return !!el.isContentEditable;
                            };

                            let target = null;
                            const active = document.activeElement;
                            if (isEditable(active)) {
                              const hasValue = (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')
                                ? !!active.value
                                : !!active.textContent;
                              if (clear && hasValue) {
                                const empty = candidates.find(el => {
                                  if (!isEditable(el)) return false;
                                  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                                    return !el.value;
                                  }
                                  return !el.textContent;
                                });
                                if (empty) target = empty;
                              }
                              if (!target) target = active;
                            }

                            if (!target) {
                              target = candidates.find(el => {
                                if (!isEditable(el)) return false;
                                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                                  return !el.value;
                                }
                                return !el.textContent;
                              }) || candidates.find(isEditable) || null;
                            }

                            if (!target) return false;
                            target.focus();

                            if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') {
                              const nextValue = clear ? text : `${target.value || ''}${text}`;
                              target.value = nextValue;
                            } else if (target.isContentEditable) {
                              const nextValue = clear ? text : `${target.textContent || ''}${text}`;
                              target.textContent = nextValue;
                            }

                            target.dispatchEvent(new Event('input', { bubbles: true }));
                            target.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                          }""",
                            {"text": text, "clear": clear_before_typing},
                        )

                    if not typed_via_dom:
                        # Clear if requested (Ctrl+A, Delete)
                        if clear_before_typing:
                            await self.page.keyboard.press("Control+A")
                            await self.page.keyboard.press("Delete")

                        # Type text
                        await self.page.keyboard.type(text)

                    # Press Enter if requested
                    if press_enter:
                        await self.page.keyboard.press("Enter")
                        await asyncio.sleep(0.5)  # Wait for potential page load

                    # Record last typed text/date for app-side fallbacks
                    await self.page.evaluate(
                        """(payload) => {
                        const text = payload.text || '';
                        window.__lastTypedText = text;
                        const match = text.match(/(\\d{4}-\\d{2}-\\d{2})/);
                        if (match) window.__lastTypedDate = match[1];
                      }""",
                        {"text": text},
                    )

                    content = await self._get_page_observation()
                    results.append(
                        f"Typed '{text}' at ({x}, {y})\n\n{content}"
                        if content
                        else f"Typed '{text}' at ({x}, {y})"
                    )

                elif action_type == "key_combination":
                    await self._ensure_browser()
                    keys = action["keys"]
                    key_string = self._normalize_key_combination(keys)

                    await self.page.keyboard.press(key_string)
                    results.append(f"Pressed key combination: {key_string}")

                elif action_type == "scroll_document":
                    await self._ensure_browser()
                    direction = action["direction"]

                    # Scroll amount in pixels
                    scroll_amount = 500

                    if direction == "down":
                        await self.page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                    elif direction == "up":
                        await self.page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
                    elif direction == "left":
                        await self.page.evaluate(f"window.scrollBy(-{scroll_amount}, 0)")
                    elif direction == "right":
                        await self.page.evaluate(f"window.scrollBy({scroll_amount}, 0)")

                    await asyncio.sleep(0.3)
                    content = await self._get_page_observation()
                    results.append(
                        f"Scrolled {direction}\n\n{content}" if content else f"Scrolled {direction}"
                    )

                elif action_type == "scroll_at":
                    await self._ensure_browser()
                    x = action["x"]
                    y = action["y"]
                    direction = action["direction"]
                    magnitude = action.get("magnitude", 800)

                    # Move to position first
                    pixel_x, pixel_y = self._grid_to_pixel(x, y)
                    await self.page.mouse.move(pixel_x, pixel_y)

                    # Scroll at position using mouse wheel
                    delta_x = 0
                    delta_y = 0
                    scroll_pixels = (magnitude / self.GRID_SIZE) * 500  # Scale magnitude

                    if direction == "down":
                        delta_y = scroll_pixels
                    elif direction == "up":
                        delta_y = -scroll_pixels
                    elif direction == "left":
                        delta_x = -scroll_pixels
                    elif direction == "right":
                        delta_x = scroll_pixels

                    await self.page.mouse.wheel(delta_x, delta_y)
                    await asyncio.sleep(0.3)
                    results.append(f"Scrolled {direction} at ({x}, {y})")

                elif action_type == "drag_and_drop":
                    await self._ensure_browser()
                    x = action["x"]
                    y = action["y"]
                    dest_x = action["destination_x"]
                    dest_y = action["destination_y"]

                    # Convert to pixels
                    start_pixel_x, start_pixel_y = self._grid_to_pixel(x, y)
                    end_pixel_x, end_pixel_y = self._grid_to_pixel(dest_x, dest_y)

                    # Perform drag and drop
                    await self.page.mouse.move(start_pixel_x, start_pixel_y)
                    await self.page.mouse.down()
                    await self.page.mouse.move(end_pixel_x, end_pixel_y)
                    await self.page.mouse.up()

                    await asyncio.sleep(0.3)
                    results.append(f"Dragged from ({x}, {y}) to ({dest_x}, {dest_y})")

                else:
                    return False, "", f"Unknown action type: {action_type}"

            return True, "\n\n---\n\n".join(results), ""

        except Exception as e:
            return False, "", str(e)

    def execute(self, actions: list[dict[str, Any]]) -> ToolResult:
        """Execute browser actions"""
        # Check for risky actions and surface safety_decision metadata
        risky_actions = ["navigate", "type_text_at", "key_combination", "click_at", "select"]
        has_risky_action = any(a.get("type") in risky_actions for a in actions)

        # Reuse event loop across tool calls to keep browser alive
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        try:
            success, output, error = self._loop.run_until_complete(
                asyncio.wait_for(self._execute_actions(actions), timeout=self.policy.timeout_s)
            )

            if not success and self._is_transient_driver_error(error):
                self._loop.run_until_complete(self._reset_browser_state())
                success, output, error = self._loop.run_until_complete(
                    asyncio.wait_for(self._execute_actions(actions), timeout=self.policy.timeout_s)
                )

            # In visual_mode, capture a screenshot and return as content_blocks
            content_blocks = None
            if self.visual_mode and success and self.page:
                content_blocks = self._loop.run_until_complete(
                    self._capture_screenshot_blocks(output)
                )

            # Surface safety_decision metadata for risky actions
            metadata = {}
            if has_risky_action:
                metadata["safety_decision"] = {
                    "requires_confirmation": True,
                    "risky_actions": [
                        a.get("type") for a in actions if a.get("type") in risky_actions
                    ],
                    "reason": "Actions may modify page state or navigate away",
                }

            return ToolResult(
                success=success,
                output=output,
                error=error if error else None,
                metadata=metadata,
                content_blocks=content_blocks,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                output="",
                error=f"Browser actions timed out after {self.policy.timeout_s}s",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Browser execution failed: {str(e)}",
            )
        # Note: Don't close loop in finally - we want to keep browser alive for next tool call

    async def cleanup(self):
        """Cleanup browser resources.

        Note: This coroutine must be run on the SAME event loop where
        Playwright was started (self._loop). Playwright's async operations
        are bound to their creation loop. Calling via asyncio.run() (which
        creates a new loop) will hang indefinitely.

        The caller is responsible for closing self._loop after this coroutine
        completes.
        """
        if self.page:
            # Capture video path before closing (if video recording was enabled)
            if self.video_dir:
                try:
                    video = self.page.video
                    if video:
                        self.video_path = await video.path()
                except Exception:
                    pass  # Video may not be available
            await self.page.close()
            self.page = None
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    def close_loop(self):
        """Close the event loop after cleanup. Call this AFTER cleanup() completes."""
        if self._loop and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

    def get_video_path(self) -> str | None:
        """Get the path to the recorded video (available after cleanup)"""
        return self.video_path
