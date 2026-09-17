"""Shared UIS/CAS HTML response parsing."""
import json
import re
from html.parser import HTMLParser


class LoginCallback(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.action, self.method, self.fields = None, None, {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action, self.method = attrs.get("action"), attrs.get("method", "GET").upper()
        elif tag == "input" and attrs.get("type") != "submit" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


def login_variables(html):
    return {key: json.loads(value) for key, value in
            re.findall(r'(?:var|let) (\w+) = (".*?"|null);', html)}
