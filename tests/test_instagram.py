import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

from cligram.instagram import (
    COLLECTION_QUERY,
    MEDIA_QUERY,
    ORIGIN,
    InstagramError,
    LoginRequired,
    media_id,
    open_instagram,
)


def connection(nodes, cursor=None):
    return {
        "edges": [{"node": node} for node in nodes],
        "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor},
    }


class InstagramBrowserTest(unittest.TestCase):
    def test_browser_session_and_collection_pagination(self):
        requests = []

        def serve(route):
            request = route.request
            if request.url == ORIGIN + "/":
                route.fulfill(content_type="text/html", body='<script type="application/json">["DTSGInitialData", {"token":"test-token"}]</script>')
                return
            if request.url == ORIGIN + f"/api/v1/media/{media_id('ReelOne')}/info/":
                requests.append(("media", request.headers["x-csrftoken"]))
                route.fulfill(json={"items": [{"code": "ReelOne", "product_type": "clips"}]})
                return
            if request.url != ORIGIN + "/api/graphql":
                route.abort()
                return
            form = parse_qs(request.post_data)
            query = form["doc_id"][0]
            variables = json.loads(form["variables"][0])
            requests.append((query, variables.get("after"), form["fb_dtsg"][0], request.headers["x-fb-friendly-name"]))
            if query == COLLECTION_QUERY[0]:
                node = {"__typename": "MediaCollection", "collection_id": "42", "collection_name": "Travel"}
                data = {"viewer": {"collections_unified_with_auto_collections": connection([node])}}
            elif variables["collection_id"] == "42":
                page = connection([{"code": "ReelTwo"}]) if variables["after"] else connection([{"code": "ReelOne"}], "next")
                data = {"fetch__MediaCollection": {"media": page}}
            else:
                data = {"fetch__MediaCollection": None}
            route.fulfill(content_type="application/json", body="for (;;);" + json.dumps({"data": data}))

        with TemporaryDirectory() as folder:
            try:
                with open_instagram(Path(folder)) as instagram:
                    with self.assertRaises(LoginRequired):
                        instagram.account_id()
                    instagram.context.add_cookies([
                        {"name": "sessionid", "value": "test-session", "url": ORIGIN},
                        {"name": "ds_user_id", "value": "123", "url": ORIGIN},
                        {"name": "csrftoken", "value": "test-csrf", "url": ORIGIN},
                    ])
                    instagram.context.route("**/*", serve)
                    self.assertEqual(instagram.account_id(), "123")
                    self.assertEqual([(item.id, item.name) for item in instagram.collections()], [("42", "Travel")])
                    scan = instagram.snapshot("42")
                    self.assertTrue(scan.complete)
                    self.assertEqual(list(scan.posts), ["ReelOne", "ReelTwo"])
                    self.assertEqual(instagram.media("ReelOne")["product_type"], "clips")
                    with self.assertRaises(InstagramError):
                        instagram.snapshot("99")
            except InstagramError as error:
                if str(error).startswith("Install Chromium"):
                    self.skipTest(str(error))
                raise

        self.assertIn((MEDIA_QUERY[0], "next", "test-token", MEDIA_QUERY[1]), requests)
        self.assertIn(("media", "test-csrf"), requests)


if __name__ == "__main__":
    unittest.main()
