"""Core recording logic for split-screen mobile demos from trajectories.

Left panel: phone viewport with tap/scroll indicators.
Right panel: agent conversation with tool call summaries.

Prerequisites:
  - Mock-web service running: uv run python -m tolokaforge.env.mock_web_service.app
  - JSON DB service running: uv run python -m tolokaforge.env.json_db_service.app
  - ffmpeg installed
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

# --- Defaults (override via CLI args) ---
DEFAULT_TRAJECTORY_PATH = Path("results/demo/trials/example_task/0/trajectory.yaml")
DEFAULT_FRAMES_DIR = Path("results/demo/demo_frames")
DEFAULT_OUTPUT_VIDEO = Path("results/demo/demo.mp4")
DEFAULT_MOCK_WEB_URL = "http://localhost:8080"

PHONE_W, PHONE_H = 412, 915
PANEL_W = 868
CANVAS_W = PHONE_W + PANEL_W  # 1280
CANVAS_H = PHONE_H  # 915
GRID = 1000

# Fonts
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
MONO_FONT_PATH = "/System/Library/Fonts/SFNSMono.ttf"
FONT_BOLD_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Colors
BG_COLOR = (26, 26, 46)  # #1a1a2e
USER_BG = (44, 62, 112)  # blue-ish
AGENT_BG = (55, 55, 65)  # dark gray
TOOL_BG = (35, 40, 50)  # darker
SYSTEM_BG = (80, 40, 40)  # reddish
TEXT_COLOR = (230, 230, 230)
LABEL_USER = (100, 160, 255)
LABEL_AGENT = (130, 220, 130)
LABEL_SYSTEM = (255, 130, 130)
TAP_COLOR = (255, 60, 60, 120)  # semi-transparent red
BEZEL_COLOR = (30, 30, 30)

FPS = 0.5  # 2 seconds per frame


def load_fonts():
    try:
        font = ImageFont.truetype(FONT_PATH, 14)
        font_bold = ImageFont.truetype(FONT_BOLD_PATH, 14)
        font_small = ImageFont.truetype(FONT_PATH, 12)
        mono = ImageFont.truetype(MONO_FONT_PATH, 11)
    except OSError:
        font = ImageFont.load_default()
        font_bold = font
        font_small = font
        mono = font
    return font, font_bold, font_small, mono


def grid_to_pixel(gx, gy):
    return int((gx / GRID) * PHONE_W), int((gy / GRID) * PHONE_H)


def draw_tap_indicator(img, gx, gy):
    """Draw a red circle at the tap location."""
    px, py = grid_to_pixel(gx, gy)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    r = 18
    draw.ellipse(
        [px - r, py - r, px + r, py + r], fill=TAP_COLOR, outline=(255, 60, 60, 200), width=2
    )
    # Inner dot
    draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(255, 60, 60, 200))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def draw_scroll_indicator(img, direction):
    """Draw a scroll arrow overlay."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = PHONE_W // 2, PHONE_H // 2
    arrow_len = 60
    color = (100, 200, 255, 150)

    if direction == "down":
        draw.line([(cx, cy - arrow_len), (cx, cy + arrow_len)], fill=color, width=4)
        draw.polygon(
            [(cx - 15, cy + arrow_len - 15), (cx + 15, cy + arrow_len - 15), (cx, cy + arrow_len)],
            fill=color,
        )
    elif direction == "up":
        draw.line([(cx, cy + arrow_len), (cx, cy - arrow_len)], fill=color, width=4)
        draw.polygon(
            [(cx - 15, cy - arrow_len + 15), (cx + 15, cy - arrow_len + 15), (cx, cy - arrow_len)],
            fill=color,
        )

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def render_conversation_panel(conversation, font, font_bold, font_small, mono):
    """Render the conversation panel as a PIL image."""
    panel = Image.new("RGB", (PANEL_W, CANVAS_H), BG_COLOR)
    draw = ImageDraw.Draw(panel)

    # Title bar
    draw.rectangle([0, 0, PANEL_W, 40], fill=(20, 20, 35))
    draw.text((20, 10), "Agent Conversation", fill=TEXT_COLOR, font=font_bold)

    # Render messages bottom-aligned (most recent visible)
    y_cursor = CANVAS_H - 15  # start from bottom
    margin = 15
    max_text_w = PANEL_W - 2 * margin - 20  # padding inside bubble

    # Calculate char width for wrapping
    char_w = font.getlength("M")
    wrap_chars = max(30, int(max_text_w / char_w))

    # Render in reverse order (bottom up)
    for entry in reversed(conversation):
        role = entry["role"]
        text = entry["text"]
        action_summary = entry.get("action_summary", "")

        # Wrap text
        lines = []
        if action_summary:
            for aline in action_summary.split("\n"):
                lines.extend(textwrap.wrap(aline, wrap_chars) or [""])
            if text:
                lines.append("")  # spacer only when regular text exists
        for tline in text.split("\n"):
            lines.extend(textwrap.wrap(tline, wrap_chars) or [""])

        line_h = 18
        bubble_h = len(lines) * line_h + 30  # padding
        label_h = 20

        total_h = bubble_h + label_h + 8

        y_top = y_cursor - total_h

        if y_top < 45:  # below title bar
            break

        # Role label
        if role == "user":
            label_color = LABEL_USER
            bg = USER_BG
            label = "User"
        elif role == "assistant":
            label_color = LABEL_AGENT
            bg = AGENT_BG
            label = "Agent"
        else:
            label_color = LABEL_SYSTEM
            bg = SYSTEM_BG
            label = "System"

        draw.text((margin, y_top), label, fill=label_color, font=font_bold)

        bubble_top = y_top + label_h
        bubble_rect = [margin, bubble_top, PANEL_W - margin, bubble_top + bubble_h]
        draw.rounded_rectangle(bubble_rect, radius=8, fill=bg)

        # Draw text inside bubble
        ty = bubble_top + 10
        action_line_count = len(action_summary.split("\n")) if action_summary else 0
        for line_idx, line in enumerate(lines):
            # Use mono font for action summary lines
            if action_summary and line_idx < action_line_count:
                draw.text((margin + 10, ty), line, fill=(180, 200, 255), font=mono)
            else:
                draw.text((margin + 10, ty), line, fill=TEXT_COLOR, font=font_small)
            ty += line_h

        y_cursor = y_top - 8  # gap between messages

    return panel


def normalize_actions_payload(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        cleaned = re.sub(r"//.*?$", "", payload, flags=re.MULTILINE).strip()
        if not cleaned:
            return []
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, dict) and "actions" in parsed:
            parsed = parsed["actions"]
        return parsed if isinstance(parsed, list) else []
    return []


def expand_actions_payload(payload):
    """Expand payload so each returned entry is a single action."""
    expanded = []
    normalized = normalize_actions_payload(payload)
    for entry in normalized:
        if isinstance(entry, dict):
            nested = entry.get("actions")
            if isinstance(nested, list):
                expanded.extend(expand_actions_payload(nested))
            else:
                expanded.append(entry)
            continue
        if isinstance(entry, str):
            cleaned = entry.strip()
            if not cleaned:
                continue
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                expanded.extend(expand_actions_payload(parsed))
                continue
            if isinstance(parsed, dict):
                expanded.extend(expand_actions_payload([parsed]))
                continue
            if "|" in cleaned:
                for part in cleaned.split("|"):
                    part = part.strip()
                    if part:
                        expanded.append({"type": "raw_action", "text": part})
            else:
                expanded.append({"type": "raw_action", "text": cleaned})
    return expanded


def format_action_summary(action):
    """Create a compact summary for a single action."""
    if isinstance(action, str):
        return action.strip()
    if not isinstance(action, dict):
        return "unknown action"

    atype = action.get("type", "?")
    if atype == "raw_action":
        return action.get("text", "raw action")
    if atype == "click_at":
        return f"tap({action.get('x', '?')}, {action.get('y', '?')})"
    if atype == "type_text_at":
        text = str(action.get("text", ""))
        if len(text) > 60:
            text = text[:57] + "..."
        return f'type "{text}" at ({action.get("x", "?")}, {action.get("y", "?")})'
    if atype == "scroll_document":
        return f"scroll {action.get('direction', '?')}"
    if atype == "scroll_at":
        return f"scroll {action.get('direction', '?')} at ({action.get('x', '?')}, {action.get('y', '?')})"
    if atype == "open_app":
        return f"open {action.get('app_name', '?')}"
    if atype == "select":
        return f"select({action.get('x', '?')}, {action.get('y', '?')})"
    if atype == "press_enter":
        return "press enter"
    if atype == "go_back":
        return "back"
    if atype == "drag_and_drop":
        return (
            f"drag ({action.get('x', '?')},{action.get('y', '?')}) -> "
            f"({action.get('destination_x', '?')},{action.get('destination_y', '?')})"
        )
    if atype == "wait_5_seconds":
        return "wait 5s"
    if atype == "key_combination":
        return f"keys {action.get('keys', [])}"
    return atype


def load_task_config(task_yaml_path: Path | None) -> tuple[dict[str, str], str | None]:
    if not task_yaml_path:
        return {}, None
    try:
        data = yaml.safe_load(task_yaml_path.read_text())
    except Exception:
        return {}, None
    mobile = data.get("tools", {}).get("agent", {}).get("mobile", {})
    if not isinstance(mobile, dict):
        return {}, None
    apps = mobile.get("apps", {})
    if not isinstance(apps, dict):
        apps = {}
    initial_app = mobile.get("initial_app")
    return {str(k): str(v) for k, v in apps.items()}, (str(initial_app) if initial_app else None)


def normalize_url(url: str) -> str:
    return url.replace("mock-web:8080", "localhost:8080").replace("mock-web", "localhost")


def infer_task_yaml(trajectory_path: Path, trajectory: dict) -> Path | None:
    # Preferred source: the per-trial task snapshot saved by the orchestrator.
    # This avoids ambiguity when task_id exists in multiple suites (e.g. browser/mobile).
    trial_task_yaml = trajectory_path.parent / "task.yaml"
    if trial_task_yaml.exists():
        return trial_task_yaml

    def _pick_candidate(candidates: list[Path]) -> Path | None:
        if not candidates:
            return None
        mobile = [c for c in candidates if "/mobile/" in c.as_posix()]
        if mobile:
            return mobile[0]
        return candidates[0]

    task_id = trajectory.get("task_id")
    if task_id:
        candidates = list(Path("tasks").glob(f"*/{task_id}/task.yaml"))
        picked = _pick_candidate(candidates)
        if picked:
            return picked
    # Fallback: try to infer from trajectory path (.../trials/<task_id>/...)
    parts = list(trajectory_path.parts)
    if "trials" in parts:
        idx = parts.index("trials")
        if idx + 1 < len(parts):
            task_id = parts[idx + 1]
            candidates = list(Path("tasks").glob(f"*/{task_id}/task.yaml"))
            picked = _pick_candidate(candidates)
            if picked:
                return picked
    return None


async def replay_and_capture(
    trajectory,
    app_map: dict[str, str] | None = None,
    mock_web_url: str = DEFAULT_MOCK_WEB_URL,
    frames_dir: Path = DEFAULT_FRAMES_DIR,
    initial_url: str | None = None,
):
    """Replay trajectory actions and capture frames."""
    messages = trajectory["messages"]
    fonts = load_fonts()
    font, font_bold, font_small, mono = fonts

    frames_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": PHONE_W, "height": PHONE_H})
        page = await context.new_page()

        # Navigate to initial page (task initial_app if available)
        start_url = initial_url or mock_web_url
        await page.goto(normalize_url(start_url), wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        conversation = []
        frame_idx = 0

        def save_frame(screenshot_img, conversation_state):
            nonlocal frame_idx
            canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BEZEL_COLOR)

            # Left: phone screenshot with 2px bezel
            canvas.paste(screenshot_img, (0, 0))

            # Separator line
            draw = ImageDraw.Draw(canvas)
            draw.line([(PHONE_W, 0), (PHONE_W, CANVAS_H)], fill=(60, 60, 60), width=2)

            # Right: conversation panel
            panel = render_conversation_panel(conversation_state, font, font_bold, font_small, mono)
            canvas.paste(panel, (PHONE_W + 2, 0))

            frame_path = frames_dir / f"frame_{frame_idx:04d}.png"
            canvas.save(frame_path)
            frame_idx += 1
            return frame_path

        # Initial frame
        screenshot = await page.screenshot()
        screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
        save_frame(screenshot_img, [])

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            if role == "user":
                # Add user message to conversation
                conversation.append({"role": "user", "text": content})
                # Take screenshot (page doesn't change on user message)
                screenshot = await page.screenshot()
                screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                save_frame(screenshot_img, list(conversation))

            elif role == "assistant":
                # Extract actions if present
                actions = []
                if tool_calls:
                    for tc in tool_calls:
                        args = tc.get("arguments", {})
                        payload = args.get("actions", [])
                        actions.extend(expand_actions_payload(payload))

                # Add assistant message to conversation
                if content:
                    conversation.append(
                        {
                            "role": "assistant",
                            "text": content,
                            "action_summary": "",
                        }
                    )

                if content and not actions:
                    # No actions — just capture frame with the message
                    screenshot = await page.screenshot()
                    screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                    save_frame(screenshot_img, list(conversation))
                    continue
                elif content and actions:
                    # Assistant text will be shown alongside the first action frame
                    pass

                # Execute each action and capture frame
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    action_summary = format_action_summary(action)
                    conversation.append(
                        {
                            "role": "assistant",
                            "text": "",
                            "action_summary": action_summary,
                        }
                    )
                    atype = action.get("type")

                    try:
                        if atype == "click_at":
                            x, y = action.get("x", GRID // 2), action.get("y", GRID // 2)
                            px = (x / GRID) * PHONE_W
                            py = (y / GRID) * PHONE_H
                            await page.mouse.click(px, py)
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            await asyncio.sleep(0.3)

                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            screenshot_img = draw_tap_indicator(screenshot_img, x, y)
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "type_text_at":
                            text = action.get("text", "")
                            clear = action.get("clear_before_typing", True)
                            enter = action.get("press_enter", True)
                            if "x" in action and "y" in action:
                                x, y = action["x"], action["y"]
                                px = (x / GRID) * PHONE_W
                                py = (y / GRID) * PHONE_H
                                await page.mouse.click(px, py)
                                await asyncio.sleep(0.2)
                                if clear:
                                    await page.keyboard.press("Control+A")
                                    await page.keyboard.press("Delete")
                                await page.keyboard.type(text)
                                if enter:
                                    await page.keyboard.press("Enter")
                                    await asyncio.sleep(0.3)

                                screenshot = await page.screenshot()
                                screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                                screenshot_img = draw_tap_indicator(screenshot_img, x, y)
                                save_frame(screenshot_img, list(conversation))
                            else:
                                # Fallback: focus first input and type
                                await page.evaluate(
                                    """() => {
                                    const el = document.querySelector('input,textarea,[contenteditable="true"], [contenteditable=""], [contenteditable="plaintext-only"]');
                                    if (el) el.focus();
                                  }"""
                                )
                                if clear:
                                    await page.keyboard.press("Control+A")
                                    await page.keyboard.press("Delete")
                                await page.keyboard.type(text)
                                if enter:
                                    await page.keyboard.press("Enter")
                                    await asyncio.sleep(0.3)
                                screenshot = await page.screenshot()
                                screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                                save_frame(screenshot_img, list(conversation))

                        elif atype == "scroll_document":
                            direction = action.get("direction", "down")
                            scroll_px = 500
                            if direction == "down":
                                await page.evaluate(f"window.scrollBy(0, {scroll_px})")
                            elif direction == "up":
                                await page.evaluate(f"window.scrollBy(0, -{scroll_px})")
                            elif direction == "left":
                                await page.evaluate(f"window.scrollBy(-{scroll_px}, 0)")
                            elif direction == "right":
                                await page.evaluate(f"window.scrollBy({scroll_px}, 0)")
                            await asyncio.sleep(0.3)

                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            screenshot_img = draw_scroll_indicator(screenshot_img, direction)
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "select":
                            x, y = action.get("x", GRID // 2), action.get("y", GRID // 2)
                            px = (x / GRID) * PHONE_W
                            py = (y / GRID) * PHONE_H
                            await page.mouse.click(px, py)
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            await asyncio.sleep(0.2)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            screenshot_img = draw_tap_indicator(screenshot_img, x, y)
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "scroll_at":
                            x, y = action.get("x", GRID // 2), action.get("y", GRID // 2)
                            px = (x / GRID) * PHONE_W
                            py = (y / GRID) * PHONE_H
                            direction = action.get("direction", "down")
                            magnitude = action.get("magnitude", 800)
                            scroll_pixels = (magnitude / GRID) * 500

                            await page.mouse.move(px, py)
                            dx, dy = 0, 0
                            if direction == "down":
                                dy = scroll_pixels
                            elif direction == "up":
                                dy = -scroll_pixels
                            elif direction == "left":
                                dx = -scroll_pixels
                            elif direction == "right":
                                dx = scroll_pixels
                            await page.mouse.wheel(dx, dy)
                            await asyncio.sleep(0.3)

                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            screenshot_img = draw_scroll_indicator(screenshot_img, direction)
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "go_back":
                            await page.go_back()
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "wait_5_seconds":
                            await asyncio.sleep(1)  # shorter for demo
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "press_enter":
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "key_combination":
                            keys = action.get("keys", [])
                            key_map = {
                                "CTRL": "Control",
                                "ALT": "Alt",
                                "SHIFT": "Shift",
                                "META": "Meta",
                                "ENTER": "Enter",
                                "TAB": "Tab",
                                "ESCAPE": "Escape",
                                "ESC": "Escape",
                            }
                            if isinstance(keys, list):
                                pw_keys = [key_map.get(k.upper(), k) for k in keys]
                                key_string = "+".join(pw_keys)
                            else:
                                key_string = keys
                            await page.keyboard.press(key_string)
                            await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "drag_and_drop":
                            sx = (action.get("x", GRID // 2) / GRID) * PHONE_W
                            sy = (action.get("y", GRID // 2) / GRID) * PHONE_H
                            ex = (action.get("destination_x", GRID // 2) / GRID) * PHONE_W
                            ey = (action.get("destination_y", GRID // 2) / GRID) * PHONE_H
                            await page.mouse.move(sx, sy)
                            await page.mouse.down()
                            await page.mouse.move(ex, ey)
                            await page.mouse.up()
                            await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                        elif atype == "navigate":
                            url = action.get("url", "")
                            # Resolve docker hostnames
                            url = normalize_url(url)
                            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))
                        elif atype == "open_app":
                            app_name = action.get("app_name")
                            target_url = None
                            if app_name and app_map:
                                target_url = app_map.get(app_name)
                                if target_url is None:
                                    for key, value in app_map.items():
                                        if key.lower() == str(app_name).lower():
                                            target_url = value
                                            break
                            if target_url:
                                url = normalize_url(target_url)
                                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(0.3)
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))
                        else:
                            screenshot = await page.screenshot()
                            screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                            save_frame(screenshot_img, list(conversation))

                    except Exception as e:
                        print(f"  Warning: action {atype} failed: {e}")
                        screenshot = await page.screenshot()
                        screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                        save_frame(screenshot_img, list(conversation))

            elif role == "system":
                conversation.append({"role": "system", "text": content})
                screenshot = await page.screenshot()
                screenshot_img = Image.open(__import__("io").BytesIO(screenshot))
                save_frame(screenshot_img, list(conversation))

            # Skip role=tool (just verification data, not visual)

        await browser.close()

    return frame_idx


def stitch_video(frame_count, frames_dir: Path, output_video: Path):
    """Use ffmpeg to stitch frames into MP4."""
    output_video.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(FPS),
        "-i",
        str(frames_dir / "frame_%04d.png"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        str(output_video),
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error: {result.stderr}")
        sys.exit(1)
    print(f"Video saved to: {output_video}")


def record_single(
    trajectory_path: Path = DEFAULT_TRAJECTORY_PATH,
    frames_dir: Path = DEFAULT_FRAMES_DIR,
    output: Path = DEFAULT_OUTPUT_VIDEO,
    mock_web_url: str = DEFAULT_MOCK_WEB_URL,
    task_yaml: Path | None = None,
    frames_only: bool = False,
    allow_default_start: bool = False,
) -> None:
    """Record a single trajectory into a split-screen demo video (or frames).

    This is the main entry point that replaces the original script's ``main()``
    function.
    """
    print("Loading trajectory...")
    with open(trajectory_path) as f:
        trajectory = yaml.safe_load(f)

    msg_count = len(trajectory.get("messages", []))
    print(f"Found {msg_count} messages in trajectory")

    print("Replaying actions and capturing frames...")
    resolved_task_yaml = task_yaml
    if not resolved_task_yaml:
        resolved_task_yaml = infer_task_yaml(trajectory_path, trajectory)
        if resolved_task_yaml:
            print(f"Inferred task yaml: {resolved_task_yaml}")
    app_map, initial_app = (
        load_task_config(resolved_task_yaml) if resolved_task_yaml else ({}, None)
    )
    initial_url = app_map.get(initial_app) if initial_app else None
    if initial_url:
        print(f"Initial app: {initial_app} -> {initial_url}")
    elif not allow_default_start:
        raise SystemExit(
            "Failed to infer initial_app URL. Provide --task-yaml or pass --allow-default-start."
        )

    frame_count = asyncio.run(
        replay_and_capture(
            trajectory,
            app_map=app_map,
            initial_url=initial_url,
            mock_web_url=mock_web_url,
            frames_dir=frames_dir,
        )
    )
    print(f"Captured {frame_count} frames")

    if frames_only:
        print(f"Frames written to: {frames_dir}")
        print("Done!")
        return

    print("Stitching video...")
    stitch_video(frame_count, frames_dir, output)
    print("Done!")
