import json
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright


ORIGIN = "https://www.instagram.com"
APP_ID = "936619743392459"
COLLECTION_QUERY = ("27959320833754327", "PolarisProfileSavedTabContentQuery_connection")
MEDIA_QUERY = ("28281867904811986", "PolarisSavedCollectionPageWWWQuery")
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{1,24}")
PAGE_TOKEN = re.compile(
    r'(?:"DTSGInitialData"|"dtsg"\s*:\s*\{)[^}]{0,400}?"token"\s*:\s*("[^"\\]+")'
)
FETCH = """async ({path, headers, body}) => {
    const response = await fetch(path, {
        method: body === null ? 'GET' : 'POST',
        body: body === null ? null : new URLSearchParams(body),
        headers,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: AbortSignal.timeout(20000),
    });
    return {status: response.status, url: response.url, text: await response.text()};
}"""


class InstagramError(Exception):
    pass


class LoginRequired(InstagramError):
    pass


class RateLimited(InstagramError):
    pass


class MediaUnavailable(InstagramError):
    pass


@dataclass(frozen=True)
class Collection:
    id: str
    name: str


@dataclass(frozen=True)
class Snapshot:
    posts: dict[str, dict]
    complete: bool


def page_nodes(connection):
    if not isinstance(connection, dict):
        raise InstagramError("Instagram did not return the collection connection.")
    edges = connection.get("edges")
    page = connection.get("page_info")
    if not isinstance(edges, list) or not isinstance(page, dict):
        raise InstagramError("Instagram returned an incomplete collection page.")
    more = page.get("has_next_page")
    if not isinstance(more, bool):
        raise InstagramError("Instagram omitted collection pagination information.")
    cursor = page.get("end_cursor") if more else None
    if more and (not isinstance(cursor, str) or not cursor or not edges):
        raise InstagramError("Instagram returned an invalid collection cursor.")
    nodes = [edge.get("node") if isinstance(edge, dict) else None for edge in edges]
    return nodes, cursor


def media_kind(node):
    if node.get("product_type") == "clips":
        return "reel"
    media_type = node.get("media_type")
    if media_type in (1, 8, "1", "8"):
        return "other"
    if media_type in (2, "2") and node.get("product_type") in ("feed", "igtv", "story"):
        return "other"
    return None


def media_id(code):
    value = 0
    for character in code:
        value = 64 * value + SHORTCODE_ALPHABET.index(character)
    return value


def video_url(node):
    variants = node.get("video_versions")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict) and isinstance(variant.get("url"), str) and variant["url"]:
                return variant["url"]
    url = node.get("video_url")
    return url if isinstance(url, str) and url else None


@contextmanager
def open_instagram(profile, *, headed=False):
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium
            executable = None
            if not Path(chromium.executable_path).is_file():
                executable = next(
                    (path for name in ("chromium", "google-chrome") if (path := shutil.which(name))),
                    None,
                )
                if executable is None:
                    raise InstagramError("Install Chromium with `python -m playwright install chromium`.")
            context = chromium.launch_persistent_context(
                profile, headless=not headed, executable_path=executable, timeout=30_000
            )
            try:
                yield Instagram(context)
            finally:
                context.close()
    except BrowserError:
        raise InstagramError("Chromium could not finish the Instagram request.") from None


class Instagram:
    def __init__(self, context):
        self.context = context
        self.page = context.pages[0] if context.pages else context.new_page()
        self.token = None

    def account_id(self):
        cookies = {cookie["name"]: cookie["value"] for cookie in self.context.cookies(ORIGIN)}
        account = cookies.get("ds_user_id", "")
        if not cookies.get("sessionid") or not account.isdecimal():
            raise LoginRequired("Sign in to Instagram using `cligram setup`.")
        return account

    def login(self):
        self.page.goto(f"{ORIGIN}/accounts/login/", wait_until="domcontentloaded", timeout=25_000)
        print("Use the Chromium window to sign in and complete any verification Instagram requests.")
        input("Press Enter here when Instagram is open on your account. ")
        return self.account_id()

    def _bootstrap(self):
        self.account_id()
        response = self.page.goto(f"{ORIGIN}/", wait_until="domcontentloaded", timeout=25_000)
        if response is None:
            raise InstagramError("Instagram did not return a web page.")
        self._check_response(response.status, self.page.url)
        match = PAGE_TOKEN.search(response.text())
        if not match:
            raise InstagramError("Instagram's web page did not include the session token for Saved.")
        self.token = json.loads(match[1])

    @staticmethod
    def _check_response(status, url):
        parts = urlsplit(url)
        if status == 401 or parts.path.startswith(("/accounts/login", "/challenge", "/checkpoint")):
            raise LoginRequired("Instagram needs you to sign in again. Run `cligram setup`.")
        if status == 429:
            raise RateLimited("Instagram is rate limiting requests.")
        if f"{parts.scheme}://{parts.netloc}" != ORIGIN:
            raise InstagramError("Instagram returned an unexpected redirect.")
        if not 200 <= status < 300:
            raise InstagramError(f"Instagram returned HTTP {status}.")

    def _fetch(self, path, *, body=None, friendly_name=None):
        if self.token is None:
            self._bootstrap()
        headers = {"X-IG-App-ID": APP_ID, "X-Requested-With": "XMLHttpRequest"}
        if friendly_name:
            headers["X-FB-Friendly-Name"] = friendly_name
        else:
            headers["X-ASBD-ID"] = "129477"
            headers["X-IG-WWW-Claim"] = "0"
            cookies = self.context.cookies(ORIGIN)
            for cookie in cookies:
                if cookie["name"] == "csrftoken":
                    headers["X-CSRFToken"] = cookie["value"]
                    break
        response = self.page.evaluate(FETCH, {"path": path, "headers": headers, "body": body})
        status, url = response["status"], response["url"]
        if status == 404 and path.startswith("/api/v1/media/"):
            raise MediaUnavailable("Instagram could not find the post.")
        try:
            payload = json.loads(response["text"].removeprefix("for (;;);"))
        except ValueError:
            self._check_response(status, url)
            raise InstagramError("Instagram did not return JSON for the web request.") from None
        if not isinstance(payload, dict):
            self._check_response(status, url)
            raise InstagramError("Instagram returned an unexpected response.")
        message = str(payload.get("message", "")).lower()
        if message in {"login_required", "checkpoint_required", "challenge_required"}:
            raise LoginRequired("Instagram needs you to sign in again. Run `cligram setup`.")
        if message in {"feedback_required", "rate_limit_error"} or "please wait" in message:
            raise RateLimited("Instagram has asked Cligram to slow down.")
        self._check_response(status, url)
        if payload.get("status") == "fail" or payload.get("errors") or payload.get("error"):
            raise InstagramError("Instagram rejected the web request.")
        return payload

    def _graph(self, query, variables):
        if self.token is None:
            self._bootstrap()
        doc_id, friendly_name = query
        payload = self._fetch(
            "/api/graphql",
            body={
                "doc_id": doc_id,
                "fb_dtsg": self.token,
                "server_timestamps": "true",
                "variables": json.dumps(variables),
            },
            friendly_name=friendly_name,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise InstagramError("Instagram omitted data from its Saved response.")
        return data

    def _pages(self, query, variables, connection_path):
        seen = set()
        while True:
            data = self._graph(query, variables)
            for key in connection_path:
                data = data.get(key) if isinstance(data, dict) else None
            nodes, cursor = page_nodes(data)
            if cursor in seen:
                raise InstagramError("Instagram repeated a collection cursor.")
            yield nodes
            if cursor is None:
                return
            seen.add(cursor)
            variables["after"] = cursor

    def collections(self):
        collections = {}
        pages = self._pages(
            COLLECTION_QUERY,
            {"first": 30, "collection_types": ["ALL_MEDIA_AUTO_COLLECTION", "MEDIA", "AUDIO_AUTO_COLLECTION"]},
            ("viewer", "collections_unified_with_auto_collections"),
        )
        for nodes in pages:
            for node in nodes:
                if not isinstance(node, dict) or node.get("__typename") != "MediaCollection":
                    continue
                collection_id = str(node.get("collection_id", ""))
                if collection_id.isdecimal():
                    name = node.get("collection_name") or "Untitled collection"
                    collections[collection_id] = Collection(collection_id, str(name))
        return list(collections.values())

    def snapshot(self, collection_id):
        posts = {}
        complete = True
        pages = self._pages(
            MEDIA_QUERY,
            {
                "after": None,
                "first": 24,
                "collection_id": collection_id,
                "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
            },
            ("fetch__MediaCollection", "media"),
        )
        for nodes in pages:
            for node in nodes:
                code = node.get("code") if isinstance(node, dict) else None
                if not isinstance(code, str) or not SHORTCODE.fullmatch(code) or code in posts:
                    complete = False
                    continue
                posts[code] = node
        return Snapshot(posts, complete)

    def media(self, code):
        payload = self._fetch(f"/api/v1/media/{media_id(code)}/info/")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise MediaUnavailable("Instagram did not return a playable post.")
        if items[0].get("code") != code:
            raise MediaUnavailable("Instagram returned media for a different post.")
        return items[0]
