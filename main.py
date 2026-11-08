#!/usr/bin/env python3
"""
HTML -> APK Telegram bot  (python-telegram-bot v21 + GitHub Actions + Capacitor)

Flow per request
  1. User sends an .html file or pastes HTML.
  2. Bot creates a throw-away branch  build/<job_id>  in the template repo,
     commits the HTML to  www/index.html  on that branch.
  3. Bot dispatches the  build-apk.yml  workflow on that branch.
  4. Bot polls the run; when it succeeds it downloads the  apk-<job_id>
     artifact, unzips it and sends the APK back to the user.
  5. Bot deletes the branch (and the run on success).

One branch per build means any number of users can build at the same time
without overwriting each other's code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass

import httpx
from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration (Railway -> Variables)
# --------------------------------------------------------------------------- #


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value or ""


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
GH_PAT = env("GH_PAT", required=True)
GITHUB_REPO = env("GITHUB_REPO", required=True)  # "owner/repo"
BASE_BRANCH = env("GITHUB_BASE_BRANCH", "main")
WORKFLOW_FILE = env("GITHUB_WORKFLOW_FILE", "build-apk.yml")

# Comma/space separated Telegram user IDs. Empty = anyone can use the bot.
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", env("ALLOWED_USER_IDS", ""))}

MAX_CONCURRENT_BUILDS = int(env("MAX_CONCURRENT_BUILDS", "2"))
BUILD_TIMEOUT = int(env("BUILD_TIMEOUT_SECONDS", "1500"))  # 25 min
POLL_INTERVAL = int(env("POLL_INTERVAL_SECONDS", "10"))

MAX_HTML_BYTES = 5 * 1024 * 1024  # 5 MB
TELEGRAM_MAX_UPLOAD = 49 * 1024 * 1024  # bots can upload up to 50 MB

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs full request URLs at INFO level, which would leak the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("html2apk")

HELP_TEXT = (
    "👋 Send me an HTML file (.html) or paste HTML code and I'll turn it into "
    "an Android APK.\n\n"
    "Tips:\n"
    "• Add a caption to your file to set the app name (otherwise I use <title>).\n"
    "• Telegram cuts messages at 4096 characters – for long code send a .html file.\n"
    "• A build usually takes 3–8 minutes.\n\n"
    "/id – show your Telegram user ID"
)

# --------------------------------------------------------------------------- #
# HTML / naming helpers
# --------------------------------------------------------------------------- #

FENCE_RE = re.compile(r"^\s*```[\w-]*[ \t]*\n(.*?)\n?```\s*$", re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
VIEWPORT_META = (
    '<meta name="viewport" content="width=device-width, initial-scale=1, '
    'viewport-fit=cover">'
)


def prepare_html(raw: str) -> str:
    """Strip code fences, wrap fragments into a document, ensure a viewport tag."""
    text = raw.strip()
    fence = FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()

    low = text.lower()
    if "<html" not in low and "<!doctype" not in low:
        text = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f"<title>My App</title>\n</head>\n<body>\n{text}\n</body>\n</html>\n"
        )
        low = text.lower()

    if "viewport" not in low:
        if "<head" in low:
            text = re.sub(
                r"(<head[^>]*>)",
                lambda m: m.group(1) + "\n" + VIEWPORT_META,
                text,
                count=1,
                flags=re.I,
            )
        elif "<html" in low:
            text = re.sub(
                r"(<html[^>]*>)",
                lambda m: m.group(1) + "\n<head>" + VIEWPORT_META + "</head>",
                text,
                count=1,
                flags=re.I,
            )
        else:
            text = VIEWPORT_META + "\n" + text
    return text


def sanitize_name(name: str) -> str:
    """Keep letters/digits/space/dash only (safe for strings.xml and shells)."""
    name = re.sub(r"[^\w \-]", "", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:30].strip()


def derive_name(hint: str | None, html: str, filename: str | None = None) -> str:
    candidates = [hint or ""]
    title = TITLE_RE.search(html)
    if title:
        candidates.append(re.sub(r"<[^>]+>", "", title.group(1)))
    if filename:
        stem = os.path.splitext(filename)[0]
        if stem.lower() != "index":
            candidates.append(stem)
    for candidate in candidates:
        cleaned = sanitize_name(candidate)
        if cleaned:
            return cleaned
    return "My App"


def package_id_for(user_id: int, app_name: str) -> str:
    """Stable Android package id per (user, app name).

    The last segment is prefixed with 'app' so it can never be a Java keyword
    or start with a digit (both would break the Capacitor/Gradle build).
    """
    slug = re.sub(r"[^a-z0-9]", "", app_name.lower())[:20]
    if not slug:  # non-latin names
        slug = hashlib.sha1(app_name.encode()).hexdigest()[:8]
    return f"com.htmlapk.u{user_id}.app{slug}"


def file_slug(app_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", app_name).strip("-") or "app"


# --------------------------------------------------------------------------- #
# GitHub API client
# --------------------------------------------------------------------------- #


class GitHubError(RuntimeError):
    pass


class BuildTimeout(RuntimeError):
    pass


class GitHub:
    def __init__(self) -> None:
        self.repo = GITHUB_REPO
        self.http = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {GH_PAT}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "html2apk-telegram-bot",
            },
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    async def _req(
        self, method: str, path: str, *, expected: tuple[int, ...] = (200,), **kwargs
    ) -> httpx.Response:
        last_error = "unknown error"
        for attempt in range(4):
            try:
                resp = await self.http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc!r}"
            else:
                if resp.status_code in expected:
                    return resp
                if resp.status_code < 500 and resp.status_code != 429:
                    raise GitHubError(
                        f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                last_error = f"HTTP {resp.status_code}"
            await asyncio.sleep(2**attempt)
        raise GitHubError(f"{method} {path} failed after retries ({last_error})")

    async def preflight(self) -> None:
        """Fail fast at startup if the token / repo / workflow are misconfigured."""
        await self._req("GET", f"/repos/{self.repo}")
        await self._req("GET", f"/repos/{self.repo}/branches/{BASE_BRANCH}")
        await self._req("GET", f"/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}")

    async def create_branch(self, name: str) -> None:
        resp = await self._req("GET", f"/repos/{self.repo}/git/ref/heads/{BASE_BRANCH}")
        sha = resp.json()["object"]["sha"]
        await self._req(
            "POST",
            f"/repos/{self.repo}/git/refs",
            expected=(201,),
            json={"ref": f"refs/heads/{name}", "sha": sha},
        )

    async def put_file(self, branch: str, path: str, data: bytes, message: str) -> None:
        existing = await self._req(
            "GET",
            f"/repos/{self.repo}/contents/{path}",
            expected=(200, 404),
            params={"ref": branch},
        )
        body = {
            "message": message,
            "content": base64.b64encode(data).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        await self._req(
            "PUT",
            f"/repos/{self.repo}/contents/{path}",
            expected=(200, 201),
            json=body,
        )

    async def dispatch(self, branch: str, inputs: dict[str, str]) -> None:
        await self._req(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            expected=(204,),
            json={"ref": branch, "inputs": inputs},
        )

    async def find_run(self, branch: str, timeout: int = 120) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = await self._req(
                "GET",
                f"/repos/{self.repo}/actions/runs",
                params={"branch": branch, "event": "workflow_dispatch", "per_page": 5},
            )
            runs = resp.json().get("workflow_runs", [])
            if runs:
                return runs[0]
            await asyncio.sleep(3)
        raise GitHubError(
            "GitHub did not start a workflow run. Is the workflow file committed on "
            f"'{BASE_BRANCH}' and does it have 'on: workflow_dispatch'?"
        )

    async def get_run(self, run_id: int) -> dict:
        return (await self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}")).json()

    async def cancel_run(self, run_id: int) -> None:
        try:
            await self._req(
                "POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel", expected=(202, 409)
            )
        except GitHubError as exc:
            log.warning("Could not cancel run %s: %s", run_id, exc)

    async def failed_step(self, run_id: int) -> str | None:
        resp = await self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs")
        for job in resp.json().get("jobs", []):
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    return step.get("name")
        return None

    async def find_artifact(self, run_id: int, name: str) -> dict | None:
        resp = await self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/artifacts")
        for artifact in resp.json().get("artifacts", []):
            if artifact["name"] == name and not artifact.get("expired"):
                return artifact
        return None

    async def download_artifact(self, artifact_id: int) -> bytes:
        # GitHub answers with a 302 to a pre-signed blob URL. Follow it manually
        # WITHOUT the Authorization header (blob storage rejects it).
        resp = await self._req(
            "GET",
            f"/repos/{self.repo}/actions/artifacts/{artifact_id}/zip",
            expected=(302,),
            follow_redirects=False,
        )
        url = resp.headers["location"]
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as dl:
            blob = await dl.get(url)
            blob.raise_for_status()
            return blob.content

    async def cleanup(self, branch: str, run_id: int | None = None) -> None:
        """Best effort: delete the temp branch (your HTML) and, on success, the run."""
        targets = []
        if run_id:
            targets.append(f"/repos/{self.repo}/actions/runs/{run_id}")
        targets.append(f"/repos/{self.repo}/git/refs/heads/{branch}")
        for path in targets:
            try:
                await self._req("DELETE", path, expected=(204, 404))
            except Exception as exc:  # noqa: BLE001
                log.warning("Cleanup failed for %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Build orchestration
# --------------------------------------------------------------------------- #

STATUS_TEXT = {
    "in_progress": "🔨 Building your APK (usually 3–6 minutes)…",
}
BACKGROUND_TASKS: set[asyncio.Task] = set()


@dataclass
class BuildRequest:
    chat_id: int
    user_id: int
    html: str
    app_name: str


async def safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("edit_text failed: %s", exc)
    except TelegramError as exc:
        log.warning("edit_text failed: %s", exc)


def extract_apk(zip_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.filename.lower().endswith(".apk"):
                return archive.read(info)
    raise GitHubError("The build artifact did not contain an .apk file")


async def wait_for_run(gh: GitHub, run_id: int, say) -> dict:
    deadline = time.monotonic() + BUILD_TIMEOUT
    last_status = None
    while True:
        run = await gh.get_run(run_id)
        if run["status"] == "completed":
            return run
        if run["status"] != last_status:
            last_status = run["status"]
            await say(
                STATUS_TEXT.get(last_status, "⏳ Waiting for a GitHub runner…")
            )
        if time.monotonic() > deadline:
            raise BuildTimeout()
        await asyncio.sleep(POLL_INTERVAL)


async def run_build(app: Application, req: BuildRequest, status_msg: Message) -> None:
    gh: GitHub = app.bot_data["gh"]
    sem: asyncio.Semaphore = app.bot_data["sem"]
    job_id = uuid.uuid4().hex[:10]
    branch = f"build/{job_id}"
    package_id = package_id_for(req.user_id, req.app_name)
    started = time.monotonic()
    run_id: int | None = None
    success = False

    async def say(text: str) -> None:
        await safe_edit(status_msg, text)

    try:
        await say(f"⏳ “{req.app_name}” is queued…")
        async with sem:
            await say("📤 Uploading your code to GitHub…")
            await gh.create_branch(branch)
            await gh.put_file(
                branch, "www/index.html", req.html.encode("utf-8"), f"Build {job_id}"
            )
            await gh.dispatch(
                branch,
                {"job_id": job_id, "app_name": req.app_name, "package_id": package_id},
            )
            run = await gh.find_run(branch)
            run_id = run["id"]
            log.info("job %s -> run %s (%s)", job_id, run_id, run.get("html_url"))

            run = await wait_for_run(gh, run_id, say)
            if run["conclusion"] != "success":
                step = await gh.failed_step(run_id)
                log.error("job %s failed (%s): %s", job_id, run["conclusion"], run.get("html_url"))
                detail = f" at step “{step}”" if step else ""
                await say(
                    f"❌ The build failed{detail}.\n"
                    "Check that your HTML is valid, or look at the run logs in the "
                    "repository's Actions tab."
                )
                return

            await say("📦 Downloading the APK…")
            artifact = None
            for _ in range(6):  # artifact listing can lag a few seconds
                artifact = await gh.find_artifact(run_id, f"apk-{job_id}")
                if artifact:
                    break
                await asyncio.sleep(3)
            if not artifact:
                raise GitHubError("Build succeeded but no artifact was found")
            zip_bytes = await gh.download_artifact(artifact["id"])

        apk = extract_apk(zip_bytes)
        if len(apk) > TELEGRAM_MAX_UPLOAD:
            await say("❌ The APK is larger than Telegram's 50 MB bot upload limit.")
            return

        elapsed = int(time.monotonic() - started)
        await app.bot.send_document(
            chat_id=req.chat_id,
            document=io.BytesIO(apk),
            filename=f"{file_slug(req.app_name)}.apk",
            caption=f"✅ {req.app_name}\nBuilt in {elapsed // 60}m {elapsed % 60}s",
            read_timeout=300,
            write_timeout=300,
            connect_timeout=30,
        )
        success = True
        try:
            await status_msg.delete()
        except TelegramError:
            pass

    except BuildTimeout:
        log.error("job %s timed out", job_id)
        if run_id:
            await gh.cancel_run(run_id)
        await say("⌛ The build took too long and was cancelled. Please try again.")
    except GitHubError as exc:
        log.error("job %s GitHub error: %s", job_id, exc)
        await say("❌ GitHub rejected a request. The admin can find details in the bot logs.")
    except Exception:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
        await say("❌ Unexpected error while building. Please try again later.")
    finally:
        app.bot_data["active"].discard(req.user_id)
        await gh.cleanup(branch, run_id if success else None)


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #


async def authorized(update: Update) -> bool:
    user = update.effective_user
    if user and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS):
        return True
    if update.effective_message:
        uid = user.id if user else "?"
        await update.effective_message.reply_text(
            f"⛔ You're not authorized to use this bot. (Your ID: {uid})"
        )
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorized(update):
        await update.effective_message.reply_text(HELP_TEXT)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


async def enqueue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_html: str,
    name_hint: str | None = None,
    filename: str | None = None,
) -> None:
    msg = update.effective_message
    user_id = update.effective_user.id
    active: set[int] = context.bot_data["active"]

    if user_id in active:
        await msg.reply_text("⏳ You already have a build running. Please wait for it to finish.")
        return

    html = prepare_html(raw_html)
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        await msg.reply_text("❌ That HTML is larger than 5 MB.")
        return

    name = derive_name(name_hint, html, filename)
    active.add(user_id)
    status_msg = await msg.reply_text(f"📱 Preparing “{name}”…")

    request = BuildRequest(chat_id=msg.chat_id, user_id=user_id, html=html, app_name=name)
    task = asyncio.create_task(run_build(context.application, request, status_msg))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    msg = update.effective_message
    doc = msg.document
    name = (doc.file_name or "").lower()
    if not (name.endswith((".html", ".htm")) or doc.mime_type == "text/html"):
        await msg.reply_text("Please send an .html file, or paste your HTML code as text.")
        return
    if doc.file_size and doc.file_size > MAX_HTML_BYTES:
        await msg.reply_text("❌ That file is larger than 5 MB.")
        return

    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    try:
        html = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = data.decode("latin-1")
    await enqueue(update, context, html, msg.caption, doc.file_name)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    msg = update.effective_message
    text = msg.text or ""
    if "<" not in text or ">" not in text:
        await msg.reply_text("That doesn't look like HTML. Paste your code or send an .html file.")
        return
    await enqueue(update, context, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #


async def post_init(app: Application) -> None:
    gh = GitHub()
    try:
        await gh.preflight()
    except GitHubError as exc:
        raise RuntimeError(
            f"GitHub preflight failed - check GH_PAT permissions, GITHUB_REPO, "
            f"GITHUB_BASE_BRANCH and that '{WORKFLOW_FILE}' exists on that branch. ({exc})"
        ) from exc
    app.bot_data["gh"] = gh
    app.bot_data["sem"] = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
    app.bot_data["active"] = set()

    me = await app.bot.get_me()
    log.info("Bot @%s started, repo=%s, workflow=%s", me.username, GITHUB_REPO, WORKFLOW_FILE)
    if not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty - ANYONE can use this bot and consume your build minutes")


async def post_shutdown(app: Application) -> None:
    gh = app.bot_data.get("gh")
    if gh:
        await gh.http.aclose()


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(private & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
