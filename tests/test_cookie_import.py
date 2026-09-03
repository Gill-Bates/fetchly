#!/usr/bin/env python3
#
# tests/test_cookie_import.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import http.cookiejar
import json
import tempfile
import unittest
from pathlib import Path

from app.utils.cookie_import import (
    CookieImportError,
    convert_to_netscape,
    has_session_cookie,
)
from app.utils.cookie_status import analyze_cookie_file

# What the Network tab's "Copy value" on the cookie request header produces
# for a signed-in youtube.com request. LOGIN_INFO and the SAPISID family are
# what make it a session; the rest is decoration.
YOUTUBE_HEADER = (
    "VISITOR_INFO1_LIVE=abc; __Secure-3PSID=g.a000xyz; __Secure-3PAPISID=aG5YCo; "
    "LOGIN_INFO=AFmmF2s:QUQ3MjN; PREF=tz=Europe.Berlin"
)


def entries(netscape: str) -> list[list[str]]:
    """The tab-separated cookie rows of a Netscape file, comments dropped."""
    return [
        line.split("\t")
        for line in netscape.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def load_into_jar(netscape: str, platform: str):
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "cookies.txt"
        path.write_text(netscape, encoding="utf-8")
        return analyze_cookie_file(path, platform)


class HeaderPasteTests(unittest.TestCase):
    def test_cookie_header_becomes_a_netscape_jar_for_the_platform(self) -> None:
        result = convert_to_netscape(YOUTUBE_HEADER, "youtube")

        self.assertEqual(result.source_format, "header")
        self.assertEqual(
            result.names,
            ("VISITOR_INFO1_LIVE", "__Secure-3PSID", "__Secure-3PAPISID", "LOGIN_INFO", "PREF"),
        )
        for domain, include_subdomains, path, secure, expires, _name, _value in entries(
            result.netscape
        ):
            self.assertEqual(domain, ".youtube.com")
            self.assertEqual(include_subdomains, "TRUE")
            self.assertEqual(path, "/")
            # __Secure- prefixed names are invalid without the secure flag, and
            # all four platforms are HTTPS-only anyway.
            self.assertEqual(secure, "TRUE")
            # 0 is yt-dlp's marker for a session cookie: a copied header has no
            # expiry to carry over, and inventing one would be a fabricated date.
            self.assertEqual(expires, "0")

    def test_values_containing_equals_signs_survive(self) -> None:
        result = convert_to_netscape(YOUTUBE_HEADER, "youtube")
        pref = next(row for row in entries(result.netscape) if row[5] == "PREF")
        self.assertEqual(pref[6], "tz=Europe.Berlin")

    def test_leading_header_name_is_stripped(self) -> None:
        with_prefix = convert_to_netscape(f"Cookie: {YOUTUBE_HEADER}", "youtube")
        self.assertEqual(with_prefix.names[0], "VISITOR_INFO1_LIVE")

    def test_platform_decides_the_domain(self) -> None:
        result = convert_to_netscape("sessionid=abc; csrftoken=t", "instagram")
        self.assertTrue(all(row[0] == ".instagram.com" for row in entries(result.netscape)))

    def test_the_result_is_a_valid_jar_for_that_platform(self) -> None:
        analysis = load_into_jar(convert_to_netscape(YOUTUBE_HEADER, "youtube").netscape, "youtube")
        self.assertEqual(analysis.status, "valid")
        self.assertTrue(analysis.is_usable)
        # Session cookies carry no date, so none is claimed in the UI either.
        self.assertIsNone(analysis.expires_at)

    def test_unreadable_input_is_rejected_with_a_readable_message(self) -> None:
        for text in ("", "   ", "no cookies here", "<html><body>oops</body></html>"):
            with self.subTest(text=text), self.assertRaises(CookieImportError):
                convert_to_netscape(text, "youtube")

    def test_unknown_platform_is_rejected(self) -> None:
        with self.assertRaises(CookieImportError):
            convert_to_netscape("sessionid=abc", "vimeo")


class CopiedRequestTests(unittest.TestCase):
    def test_copy_as_curl_carries_the_cookie_header(self) -> None:
        curl = (
            "curl 'https://www.youtube.com/watch?v=x' \\\n"
            "  -H 'accept: */*' \\\n"
            "  -H 'cookie: SAPISID=abc123; __Secure-1PSID=g.a000; LOGIN_INFO=AFm' \\\n"
            "  -H 'user-agent: Mozilla/5.0' \\\n"
            "  --compressed"
        )
        result = convert_to_netscape(curl, "youtube")

        self.assertEqual(result.source_format, "request")
        self.assertEqual(result.names, ("SAPISID", "__Secure-1PSID", "LOGIN_INFO"))
        # Only the cookie header, never the other headers around it.
        self.assertNotIn("user-agent", result.netscape)
        self.assertNotIn("accept", result.netscape)

    def test_copy_as_fetch_carries_the_cookie_header(self) -> None:
        node = (
            'fetch("https://www.instagram.com/", {\n'
            '  "headers": {\n'
            '    "accept": "*/*",\n'
            '    "cookie": "csrftoken=tok; sessionid=12345%3Aabc"\n'
            "  }\n"
            "});"
        )
        result = convert_to_netscape(node, "instagram")
        self.assertEqual(result.names, ("csrftoken", "sessionid"))

    def test_windows_cmd_quoting_is_undone(self) -> None:
        # Edge's "Copy as cURL (cmd)" escapes every quote with a caret and
        # continues lines with one. Left in place, the extractor finds no
        # header, the raw command reaches the plain-header parser, and what
        # gets stored is missing its first cookie and mangles the rest.
        cmd = (
            'curl --url ^"https://www.youtube.com/^" ^\n'
            '  -H ^"accept: text/html^" ^\n'
            '  -H ^"cookie: SAPISID=abc123; __Secure-3PSID=g.a000; PREF=tz^%^3DEurope.Berlin^" ^\n'
            '  -H ^"user-agent: Mozilla/5.0^"'
        )
        result = convert_to_netscape(cmd, "youtube")

        self.assertEqual(result.source_format, "request")
        self.assertEqual(result.names, ("SAPISID", "__Secure-3PSID", "PREF"))
        rows = {row[5]: row[6] for row in entries(result.netscape)}
        self.assertEqual(rows["PREF"], "tz%3DEurope.Berlin")

    def test_powershell_seeds_its_cookies_into_a_session_object(self) -> None:
        # "Copy as PowerShell" builds no cookie header at all.
        powershell = (
            "$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession\n"
            '$session.Cookies.Add((New-Object System.Net.Cookie("SAPISID", "abc123", "/", ".youtube.com")))\n'
            '$session.Cookies.Add((New-Object System.Net.Cookie("__Secure-3PSID", "g.a0", "/", ".youtube.com")))\n'
            'Invoke-WebRequest -UseBasicParsing -Uri "https://www.youtube.com/" -WebSession $session'
        )
        result = convert_to_netscape(powershell, "youtube")

        self.assertEqual(result.source_format, "request")
        self.assertEqual(result.names, ("SAPISID", "__Secure-3PSID"))
        self.assertTrue(all(row[0] == ".youtube.com" for row in entries(result.netscape)))

    def test_a_request_without_cookies_is_refused_not_guessed(self) -> None:
        # The page request of a site with a service worker: real target, but
        # only provisional headers. Parsing the command line as a header would
        # store a jar built from URL fragments.
        provisional = (
            'curl --url ^"https://www.youtube.com/^" ^\n'
            '  -H ^"DNT: 1^" ^\n'
            '  -H ^"Service-Worker-Navigation-Preload: true^"'
        )
        with self.assertRaises(CookieImportError) as caught:
            convert_to_netscape(provisional, "youtube")
        self.assertIn("Fetch/XHR", str(caught.exception))

    def test_short_cookie_flag_is_read(self) -> None:
        result = convert_to_netscape("curl 'https://www.tiktok.com/' -b 'sid_tt=abc; tt_csrf=x'", "tiktok")
        self.assertEqual(result.names, ("sid_tt", "tt_csrf"))


class JsonExportTests(unittest.TestCase):
    def payload(self) -> str:
        return json.dumps(
            [
                {
                    "domain": ".youtube.com",
                    "name": "__Secure-3PSID",
                    "value": "g.a000",
                    "path": "/",
                    "secure": True,
                    "expirationDate": 1798761600.5,
                },
                {
                    "domain": ".youtube.com",
                    "name": "PREF",
                    "value": "f6=4",
                    "path": "/",
                    "secure": False,
                    "session": True,
                },
            ]
        )

    def test_extension_export_keeps_its_own_expiry_and_flags(self) -> None:
        result = convert_to_netscape(self.payload(), "youtube")
        self.assertEqual(result.source_format, "json")

        rows = {row[5]: row for row in entries(result.netscape)}
        self.assertEqual(rows["__Secure-3PSID"][4], "1798761600")
        self.assertEqual(rows["__Secure-3PSID"][3], "TRUE")
        # A session cookie in the export stays one in the jar.
        self.assertEqual(rows["PREF"][4], "0")
        self.assertEqual(rows["PREF"][3], "FALSE")

    def test_the_reported_expiry_is_the_one_from_the_export(self) -> None:
        analysis = load_into_jar(convert_to_netscape(self.payload(), "youtube").netscape, "youtube")
        self.assertEqual(analysis.status, "valid")
        self.assertEqual(analysis.expires_at, 1798761600)

    def test_wrapped_and_malformed_payloads(self) -> None:
        wrapped = json.dumps({"cookies": [{"name": "sessionid", "value": "abc"}]})
        self.assertEqual(convert_to_netscape(wrapped, "instagram").names, ("sessionid",))

        with self.assertRaises(CookieImportError):
            convert_to_netscape("[{broken", "instagram")
        with self.assertRaises(CookieImportError):
            convert_to_netscape("[]", "instagram")


class ForeignDomainTests(unittest.TestCase):
    """A paste may only deposit cookies belonging to its own platform."""

    def test_a_json_export_is_filtered_to_the_platform(self) -> None:
        payload = json.dumps(
            [
                {"domain": ".example.com", "name": "sessionid", "value": "foreign", "path": "/"},
                {"domain": ".instagram.com", "name": "csrftoken", "value": "own", "path": "/"},
            ]
        )
        result = convert_to_netscape(payload, "instagram")

        # The foreign session token is neither stored nor allowed to stand in
        # for Instagram's own login cookie just because the names match.
        self.assertEqual(result.names, ("csrftoken",))
        self.assertNotIn("foreign", result.netscape)
        self.assertFalse(has_session_cookie(result.names, "instagram"))

    def test_a_netscape_file_is_filtered_to_the_platform(self) -> None:
        source = (
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tTRUE\t0\tsessionid\tforeign\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\town\n"
        )
        result = convert_to_netscape(source, "instagram")
        self.assertEqual(result.names, ("sessionid",))
        self.assertNotIn("foreign", result.netscape)
        self.assertNotIn("example.com", result.netscape)

    def test_an_export_with_nothing_of_ours_is_refused(self) -> None:
        payload = json.dumps([{"domain": ".example.com", "name": "sessionid", "value": "x"}])
        with self.assertRaises(CookieImportError) as caught:
            convert_to_netscape(payload, "instagram")
        self.assertIn("instagram.com", str(caught.exception))


class NetscapePassthroughTests(unittest.TestCase):
    def test_httponly_prefixed_entries_are_kept(self) -> None:
        # Browser extensions write yt-dlp's #HttpOnly_ prefix. http.cookiejar
        # reads those lines as comments, so leaving them in place would hide
        # exactly the login cookies that matter from every validity check.
        source = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1798761600\tsessionid\t12345%3Aabc\n"
            ".instagram.com\tTRUE\t/\tFALSE\t1798761600\tcsrftoken\ttok\n"
        )
        result = convert_to_netscape(source, "instagram")

        self.assertEqual(result.source_format, "netscape")
        self.assertEqual(result.names, ("sessionid", "csrftoken"))
        self.assertNotIn("#HttpOnly_", result.netscape)

        jar = http.cookiejar.MozillaCookieJar()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.txt"
            path.write_text(result.netscape, encoding="utf-8")
            jar.load(str(path), ignore_discard=True, ignore_expires=True)
        self.assertIn("sessionid", {cookie.name for cookie in jar})

    def test_a_file_without_entries_is_rejected(self) -> None:
        with self.assertRaises(CookieImportError):
            convert_to_netscape("# Netscape HTTP Cookie File\n\n# nothing here\n", "youtube")


class SessionCookieDetectionTests(unittest.TestCase):
    def test_document_cookie_output_has_no_login_cookie(self) -> None:
        # document.cookie cannot see HttpOnly cookies, which is exactly what a
        # login is made of - this is the paste box's main failure mode.
        result = convert_to_netscape("csrftoken=tok; ds_user_id=999; mid=Zx", "instagram")
        self.assertFalse(has_session_cookie(result.names, "instagram"))

    def test_a_signed_in_header_satisfies_every_group(self) -> None:
        cases = {
            "youtube": YOUTUBE_HEADER,
            "instagram": "sessionid=abc; csrftoken=t",
            "tiktok": "sid_tt=abc; sessionid_ss=abc; tt_csrf=x",
            "facebook": "c_user=1; xs=abc",
        }
        for platform, header in cases.items():
            with self.subTest(platform=platform):
                result = convert_to_netscape(header, platform)
                self.assertTrue(has_session_cookie(result.names, platform))

    def test_youtube_needs_login_info_the_way_yt_dlp_does(self) -> None:
        # yt-dlp's YoutubeBaseInfoExtractor._has_auth_cookies requires
        # LOGIN_INFO *and* one of the SAPISID family. A jar with only the SID
        # cookies passes a naive check and then downloads signed out - which
        # is precisely what a request copied from google.com produces.
        without_login_info = convert_to_netscape(
            "__Secure-3PSID=g.a000; __Secure-3PAPISID=aG5YCo; NID=534", "youtube"
        )
        self.assertFalse(has_session_cookie(without_login_info.names, "youtube"))

        without_sapisid = convert_to_netscape("LOGIN_INFO=AFm; __Secure-3PSID=g.a0", "youtube")
        self.assertFalse(has_session_cookie(without_sapisid.names, "youtube"))

        complete = convert_to_netscape("LOGIN_INFO=AFm; __Secure-3PAPISID=aG5YCo", "youtube")
        self.assertTrue(has_session_cookie(complete.names, "youtube"))

    def test_a_request_to_another_domain_is_refused(self) -> None:
        # Google's scripts on a YouTube page carry google.com's cookies: the
        # names look close enough to pass, LOGIN_INFO is absent, and filing
        # them under .youtube.com would produce a jar Settings calls valid and
        # the downloader treats as signed out.
        google = (
            "curl --url 'https://www.google.com/js/th/Gwp.js' "
            "-b '__Secure-3PAPISID=aG5YCo; __Secure-3PSID=g.a000; NID=534'"
        )
        with self.assertRaises(CookieImportError) as caught:
            convert_to_netscape(google, "youtube")
        self.assertIn("www.google.com", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
