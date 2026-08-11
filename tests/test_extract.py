import json

from strain_db import extract

HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Blue Dream",
 "description":"A sativa-dominant hybrid.",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.4","reviewCount":"12043"}}
</script>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"strain":{"id":42,"name":"Blue Dream","category":"Hybrid",
 "thcAvg":18.5,"effects":[{"name":"Relaxed"},{"name":"Happy"}]}}}}
</script>
</head><body>
<h1>Blue Dream</h1>
<div class="thc-percentage">THC 17-24%</div>
<ul class="effect-list"><li>Relaxed</li><li>Happy</li><li>Relaxed</li></ul>
<table><tr><th>Flowering Time</th><td>9-10 weeks</td></tr>
<tr><th>Yield Indoor</th><td>500 gr/m2</td></tr></table>
</body></html>
"""


class TestJsonLd:
    def test_finds_product(self):
        soup = extract.soup_of(HTML)
        obj = extract.json_ld_of_type(soup, "Product")
        assert obj["name"] == "Blue Dream"

    def test_graph_is_flattened(self):
        html = '<script type="application/ld+json">{"@graph":[{"@type":"Thing","name":"X"}]}</script>'
        soup = extract.soup_of(html)
        assert extract.json_ld_of_type(soup, "Thing")["name"] == "X"

    def test_malformed_is_skipped(self):
        soup = extract.soup_of('<script type="application/ld+json">{oops</script>')
        assert extract.json_ld(soup) == []


class TestAppState:
    def test_next_data(self):
        name, data = extract.app_state(HTML)
        assert name == "__NEXT_DATA__"
        assert data["props"]["pageProps"]["strain"]["id"] == 42

    def test_window_assignment(self):
        html = '<script>window.__NUXT__ = {"a":{"b":1}};</script>'
        name, data = extract.app_state(html)
        assert name == "__NUXT__" and data["a"]["b"] == 1

    def test_absent(self):
        assert extract.app_state("<html></html>") == (None, None)

    def test_braces_inside_strings_do_not_break_balancing(self):
        payload = {"text": "a } brace { inside", "n": 1}
        html = f"<script>window.__NUXT__ = {json.dumps(payload)};</script>"
        _, data = extract.app_state(html)
        assert data["n"] == 1


class TestJsonPath:
    def setup_method(self):
        _, self.data = extract.app_state(HTML)

    def test_dotted(self):
        assert extract.json_path(self.data, "props.pageProps.strain.name") == "Blue Dream"

    def test_missing_returns_default(self):
        assert extract.json_path(self.data, "props.nope.here", "fallback") == "fallback"

    def test_index(self):
        assert (
            extract.json_path(self.data, "props.pageProps.strain.effects[0].name") == "Relaxed"
        )

    def test_wildcard_maps_key(self):
        assert extract.json_path(self.data, "props.pageProps.strain.effects[*].name") == [
            "Relaxed",
            "Happy",
        ]

    def test_empty_path(self):
        assert extract.json_path(self.data, "", "d") == "d"

    def test_deep_find(self):
        assert extract.deep_find(self.data, "thcAvg") == [18.5]


class TestSelect:
    def setup_method(self):
        self.soup = extract.soup_of(HTML)

    def test_text(self):
        assert extract.select(self.soup, "h1") == "Blue Dream"

    def test_missing_returns_none(self):
        assert extract.select(self.soup, ".nope") is None

    def test_default(self):
        assert extract.select(self.soup, {"sel": ".nope", "default": "x"}) == "x"

    def test_many_dedupes_preserving_order(self):
        assert extract.select(self.soup, {"sel": ".effect-list li", "many": True}) == [
            "Relaxed",
            "Happy",
        ]

    def test_regex_group(self):
        assert (
            extract.select(self.soup, {"sel": ".thc-percentage", "regex": r"([\d.]+-[\d.]+%)"})
            == "17-24%"
        )

    def test_attr(self):
        soup = extract.soup_of('<meta name="description" content="hello">')
        assert extract.select(soup, {"sel": "meta[name=description]", "attr": "content"}) == "hello"

    def test_alternatives_fall_through(self):
        assert extract.select(self.soup, [{"sel": ".nope"}, {"sel": "h1"}]) == "Blue Dream"

    def test_bad_selector_does_not_raise(self):
        assert extract.select(self.soup, {"sel": "a[[["}) is None


class TestPairs:
    def test_table_rows(self):
        soup = extract.soup_of(HTML)
        pairs = extract.select_pairs(soup, {"row": "table tr", "label": "th", "value": "td"})
        assert pairs["flowering time"] == "9-10 weeks"
        assert pairs["yield indoor"] == "500 gr/m2"

    def test_empty_spec(self):
        assert extract.select_pairs(extract.soup_of(HTML), {}) == {}
