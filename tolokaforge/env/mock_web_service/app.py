"""Mock web server for benchmarking"""

import json
import logging
import mimetypes
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Mock Web Service")
logger = logging.getLogger(__name__)

# In-memory state for stateful interactions
state = {}

templates = Jinja2Templates(directory="templates")

# JSON DB URL
JSON_DB_URL = os.getenv("JSON_DB_URL", "http://json-db:8000")


# Default tasks root (category directories directly under this path).
def _default_tasks_dir() -> Path:
    """Resolve a sensible default tasks dir across local and container layouts."""
    # Honor explicit override first.
    env_tasks_dir = os.getenv("TASKS_DIR")
    if env_tasks_dir:
        return Path(env_tasks_dir)

    # Local layout: walk parent tree and pick the first existing /tasks.
    this_file = Path(__file__).resolve()
    for parent in this_file.parents:
        candidate = parent / "tasks"
        if candidate.is_dir():
            return candidate

    # Container fallback used by docker/mock_web.Dockerfile.
    return Path("/app/tasks")


DEFAULT_TASKS_DIR = _default_tasks_dir()


def _normalize_tasks_root(path: Path) -> Path:
    """Normalize incoming root path to category-root format.

    Accepts either:
    - direct category root: /path/.../tasks
    - pack root containing tasks/: /path/.../<pack>
    """
    if (path / "tasks").is_dir():
        return path / "tasks"
    return path


def _parse_tasks_roots(
    tasks_dirs_env: str | None = None,
    tasks_dir_env: str | None = None,
) -> list[Path]:
    """Parse task roots from env vars with backward-compatible fallback."""
    raw_dirs = tasks_dirs_env if tasks_dirs_env is not None else os.getenv("TASKS_DIRS", "")
    raw_dir = tasks_dir_env if tasks_dir_env is not None else os.getenv("TASKS_DIR", "")

    roots: list[Path] = []
    if raw_dirs.strip():
        for part in raw_dirs.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            roots.append(_normalize_tasks_root(Path(candidate)))
        if not roots:
            logger.warning("TASKS_DIRS was set but contained no valid entries; falling back")
            if raw_dir.strip():
                roots.append(_normalize_tasks_root(Path(raw_dir.strip())))
            else:
                roots.append(DEFAULT_TASKS_DIR)
    elif raw_dir.strip():
        roots.append(_normalize_tasks_root(Path(raw_dir.strip())))
    else:
        roots.append(DEFAULT_TASKS_DIR)

    # Backward-compatible Docker fallback
    if not any(root.exists() for root in roots):
        docker_default = Path("/app/tasks")
        if docker_default.exists():
            roots = [docker_default]

    # Keep deterministic order and de-duplicate
    seen: set[Path] = set()
    normalized: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    if not normalized:
        logger.warning("No valid task roots resolved; using default tasks directory")
        return [DEFAULT_TASKS_DIR.resolve()]
    return normalized


TASK_ROOTS = _parse_tasks_roots()


def _log_duplicate_task_roots() -> None:
    """Warn when the same /category/task_id exists in multiple task roots."""
    seen: dict[str, Path] = {}
    for root in TASK_ROOTS:
        if not root.exists():
            continue
        for category_dir in root.iterdir():
            if not category_dir.is_dir():
                continue
            for task_dir in category_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                key = f"{category_dir.name}/{task_dir.name}"
                if key in seen and seen[key] != task_dir:
                    logger.warning(
                        "Duplicate task path across task roots; first root wins for routing",
                        extra={
                            "task": key,
                            "kept_root": str(seen[key]),
                            "ignored_root": str(task_dir),
                        },
                    )
                else:
                    seen[key] = task_dir


_log_duplicate_task_roots()


def _task_roots_debug() -> str:
    if not TASK_ROOTS:
        return "<none>"
    return ", ".join(str(root) for root in TASK_ROOTS)


def _iter_task_dirs() -> list[Path]:
    """Iterate over all task directories across all categories (browser, mobile, etc.)"""
    task_dirs = []
    for tasks_root in TASK_ROOTS:
        if not tasks_root.exists():
            continue
        for category_dir in tasks_root.iterdir():
            if not category_dir.is_dir():
                continue
            for task_dir in category_dir.iterdir():
                if task_dir.is_dir():
                    task_dirs.append(task_dir)
    return task_dirs


def _extract_task_prefix(request_path: str) -> tuple[Path | None, str, Path | None, str | None]:
    """Extract /task/{category}/{task_id}/ prefix if present."""
    rel_path = request_path.lstrip("/")
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "task":
        category, task_id = parts[1], parts[2]
        for tasks_root in TASK_ROOTS:
            task_dir = tasks_root / category / task_id
            if task_dir.exists() and task_dir.is_dir():
                remainder = "/".join(parts[3:])
                return task_dir, remainder, tasks_root, category
    return None, rel_path, None, None


def find_html_file(filename: str) -> Path | None:
    """Find HTML file in tasks directory by searching all task subdirectories"""
    for task_dir in _iter_task_dirs():
        # Check main task directory
        file_path = task_dir / filename
        if file_path.exists() and file_path.is_file():
            return file_path

        # Check mock_web subdirectory
        mock_web_path = task_dir / "mock_web" / filename
        if mock_web_path.exists() and mock_web_path.is_file():
            return mock_web_path

    return None


def find_static_file(request_path: str) -> Path | None:
    """Find a static file in task www/ subdirectories"""
    task_dir, rel_path, task_root, category = _extract_task_prefix(request_path)
    task_dirs = [task_dir] if task_dir else _iter_task_dirs()
    rel_path = rel_path.lstrip("/")
    if rel_path == ".":
        rel_path = ""

    # Allow shared assets under a top-level _assets task directory
    if task_dir and task_dir.name == "_assets" and rel_path:
        asset_path = task_dir / rel_path
        if asset_path.exists() and asset_path.is_file():
            return asset_path

    # For prefixed task paths, check task-local file first.
    if task_dir and rel_path:
        local_file = task_dir / rel_path
        if local_file.exists() and local_file.is_file():
            return local_file
        local_index = local_file / "index.html"
        if local_index.exists() and local_index.is_file():
            return local_index

    # Search www/ subdirectories in each task
    for task_dir in task_dirs:
        # First, allow direct resolution from task_dir/www for multi-page sites.
        shared_www = task_dir / "www"
        if shared_www.exists():
            file_path = shared_www / rel_path
            if file_path.exists() and file_path.is_file():
                if file_path.name != ".DS_Store":
                    return file_path
            index_path = file_path / "index.html"
            if index_path.exists() and index_path.is_file():
                return index_path

        # Backward-compatible lookup across www/* roots.
        for www_dir in sorted(task_dir.glob("www/*/")):
            if rel_path == "":
                index_path = www_dir / "index.html"
                if index_path.exists() and index_path.is_file():
                    return index_path
            file_path = www_dir / rel_path
            if file_path.exists() and file_path.is_file():
                if file_path.name == ".DS_Store":
                    continue
                return file_path
            # Try index.html for directory paths
            index_path = file_path / "index.html"
            if index_path.exists() and index_path.is_file():
                return index_path

    # Pack-shared asset fallback:
    # 1) same root where task was resolved
    # 2) remaining roots in configured order
    if rel_path and category and task_root:
        ordered_roots = [task_root] + [root for root in TASK_ROOTS if root != task_root]
        for root in ordered_roots:
            shared_asset = root / category / "_assets" / rel_path
            if shared_asset.exists() and shared_asset.is_file():
                return shared_asset

    return None


@lru_cache(maxsize=16)
def _load_dataset(dataset: str) -> dict[str, Any]:
    dataset_dir = None
    for tasks_root in TASK_ROOTS:
        candidate = tasks_root / "mobile" / "_data" / dataset
        if candidate.exists():
            dataset_dir = candidate
            break
    if dataset_dir is None:
        raise FileNotFoundError(f"Dataset not found: {dataset} (roots: {_task_roots_debug()})")

    def _load(name: str) -> Any:
        path = dataset_dir / name
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    return {
        "dataset": dataset,
        "places": _load("places.json"),
        "menus": _load("menus.json"),
        "hours": _load("hours.json"),
        "reviews": _load("reviews.json"),
        "reservations": _load("reservations.json"),
        "grocery_items": _load("grocery_items.json"),
        "coffee_menu": _load("coffee_menu.json"),
        "events": _load("events.json"),
        "notes": _load("notes.json"),
    }


# ── Routes ────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "healthy"}


@app.get("/api/state")
async def get_state():
    """Get current state (for grading)"""
    return state


@app.post("/api/reset")
async def reset_state():
    """Reset state"""
    state.clear()
    return {"status": "ok"}


@app.get("/api/app-data")
async def app_data(app: str | None = None, dataset: str = "v1"):
    """Serve shared dataset for mobile apps."""
    try:
        data = _load_dataset(dataset)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {dataset}")
    return {"app": app, **data}


@app.get("/booking", response_class=HTMLResponse)
async def booking_form():
    """Booking form"""
    return """
    <html>
        <head><title>Hotel Booking</title></head>
        <body>
            <h1>Hotel Booking</h1>
            <form method="POST" action="/booking/confirm">
                <label for="name">Name:</label>
                <input type="text" id="name" name="name" required><br><br>

                <label for="hotel">Hotel:</label>
                <select id="hotel" name="hotel">
                    <option value="grand_plaza">Grand Plaza</option>
                    <option value="seaside_resort">Seaside Resort</option>
                    <option value="mountain_lodge">Mountain Lodge</option>
                </select><br><br>

                <label for="checkin">Check-in:</label>
                <input type="date" id="checkin" name="checkin" required><br><br>

                <label for="checkout">Check-out:</label>
                <input type="date" id="checkout" name="checkout" required><br><br>

                <button type="submit" id="confirm">Confirm Booking</button>
            </form>
        </body>
    </html>
    """


@app.post("/booking/confirm", response_class=HTMLResponse)
async def booking_confirm(request: Request):
    """Confirm booking"""
    form_data = await request.form()

    booking = {
        "name": form_data.get("name"),
        "hotel": form_data.get("hotel"),
        "checkin": form_data.get("checkin"),
        "checkout": form_data.get("checkout"),
        "status": "confirmed",
    }

    state["last_booking"] = booking

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{JSON_DB_URL}/query",
                json={"jsonpath": "$.bookings"},
                timeout=5.0,
            )
            if response.status_code == 200:
                current_bookings = response.json().get("result", [])
                current_bookings.append(booking)
                await client.post(
                    f"{JSON_DB_URL}/update",
                    json={
                        "ops": [{"op": "replace", "path": "$.bookings", "value": current_bookings}]
                    },
                    timeout=5.0,
                )
    except Exception as e:
        print(f"Warning: Failed to store booking in JSON DB: {e}")

    hotel_names = {
        "grand_plaza": "Grand Plaza",
        "seaside_resort": "Seaside Resort",
        "mountain_lodge": "Mountain Lodge",
    }

    return f"""
    <html>
        <head><title>Booking Confirmed</title></head>
        <body>
            <h1>Booking Confirmed</h1>
            <div id="status">
                <p>Thank you, {booking["name"]}!</p>
                <p>Your reservation at {hotel_names.get(booking["hotel"], booking["hotel"])} is confirmed.</p>
                <p>Check-in: {booking["checkin"]}</p>
                <p>Check-out: {booking["checkout"]}</p>
                <p>Confirmation number: BK{booking["hotel"][:3].upper()}12345</p>
            </div>
        </body>
    </html>
    """


# ── Static file serving ──────────────────────────────────────────────
# These catch-all routes MUST be registered last.


@app.get("/{filename}.html")
async def serve_task_html(filename: str):
    """Serve HTML files from task directories"""
    html_filename = f"{filename}.html"
    file_path = find_html_file(html_filename)

    if file_path is None:
        return HTMLResponse(
            content=(
                "<html><body><h1>404 Not Found</h1>"
                f"<p>File {html_filename} not found in any task directory</p>"
                f"<p>roots={_task_roots_debug()}</p>"
                "</body></html>"
            ),
            status_code=404,
        )

    return FileResponse(path=str(file_path), media_type="text/html")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve index.html from task www/ directories if available, otherwise default"""
    index_file = find_static_file("index.html")
    if index_file:
        return FileResponse(path=str(index_file), media_type="text/html")

    return """
    <html>
        <head><title>Mock Service</title></head>
        <body>
            <h1>Welcome to Mock Service</h1>
            <p>This is a mock web service for benchmarking.</p>
            <a href="/booking">Go to Booking</a>
        </body>
    </html>
    """


@app.get("/{path:path}")
async def serve_static(path: str):
    """Catch-all: serve static files from task www/ directories"""
    file_path = find_static_file(path)
    if file_path is None:
        return HTMLResponse(
            content=(
                "<html><body><h1>404 Not Found</h1>"
                f"<p>{path}</p>"
                f"<p>roots={_task_roots_debug()}</p>"
                "</body></html>"
            ),
            status_code=404,
        )

    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(path=str(file_path), media_type=media_type)
