from censor_proxy.inject import extend_csp_headers, extend_policy, inject_html, script_tag

O = "https://censor.local"


def directives(policy):
    return {d.split()[0]: d.split()[1:] for d in policy.split(";") if d.strip()}


def test_host_added_to_script_media_connect():
    p, nonce = extend_policy("script-src 'self'; media-src 'self' blob:; connect-src *", O, "N")
    d = directives(p)
    assert O in d["script-src"] and O in d["media-src"] and O in d["connect-src"]
    assert not nonce and "'nonce-N'" not in p


def test_default_src_fallback_extended_once():
    p, _ = extend_policy("default-src 'self'; img-src *", O, "N")
    assert directives(p)["default-src"] == ["'self'", O]


def test_none_replaced():
    p, _ = extend_policy("media-src 'none'", O, "N")
    assert directives(p)["media-src"] == [O]


def test_strict_dynamic_gets_nonce():
    p, nonce = extend_policy("script-src 'strict-dynamic' 'nonce-abc'; object-src 'none'", O, "N")
    assert nonce and "'nonce-N'" in directives(p)["script-src"]
    assert directives(p)["object-src"] == ["'none'"]  # untouched


def test_unsafe_inline_policy_gets_no_nonce():
    # A nonce would switch off 'unsafe-inline' and break the page's inline scripts.
    p, nonce = extend_policy("script-src 'self' 'unsafe-inline'", O, "N")
    assert not nonce and "nonce" not in p


def test_no_nonce_on_default_src_for_media_only():
    p, nonce = extend_policy("script-src 'self'; default-src 'nonce-x'", O, "N")
    assert not nonce and "'nonce-N'" not in p


def test_multiple_headers_and_comma_policies():
    headers = {"Content-Security-Policy": ["script-src 'self', media-src 'self'", "default-src 'self'"]}
    extend_csp_headers(headers, O, "N")
    for value in headers["Content-Security-Policy"]:
        for policy in value.split(","):
            assert O in policy


def test_no_csp_no_change():
    headers = {}
    assert extend_csp_headers(headers, O, "N") is False and headers == {}


def test_inject_after_head():
    tag = script_tag(O, None)
    out = inject_html(b"<!doctype html><html lang=en><HEAD id=x><title>t</title></head>", tag)
    assert out == b"<!doctype html><html lang=en><HEAD id=x>" + tag + b"<title>t</title></head>"


def test_inject_without_head():
    tag = script_tag(O, "N")
    assert b'nonce="N"' in tag
    out = inject_html(b"<html><body>hi</body></html>", tag)
    assert out.startswith(b"<html>" + tag)


def test_page_carrying_our_tag_is_injected_anyway():
    # A body check would let any site opt out by including the tag or a
    # marker; the script itself runs once per document.
    tag = script_tag(O, None)
    page = b"<html><head>" + tag + b"</head></html>"
    assert inject_html(page, tag) == b"<html><head>" + tag + tag + b"</head></html>"


def test_fragments_left_alone():
    assert inject_html(b'<div class="comment">hi</div>', script_tag(O, None)) is None


def test_head_inside_script_string_not_used():
    # No head element; a script string holds "<head>" followed by attacker
    # markup. Our tag's "</script>" would end that script and make the rest
    # live HTML, so only the <html> tag before the script is used.
    tag = script_tag(O, None)
    page = (b'<!doctype html><html><script>var s = "<head><img src=x onerror=alert(1)>";'
            b'</script><body>hi</body></html>')
    out = inject_html(page, tag)
    assert out == b"<!doctype html><html>" + tag + page[len(b"<!doctype html><html>"):]
    # Nothing usable before the first script: left alone rather than guessed.
    assert inject_html(b'<script>var s = "<html><head>";</script><p>hi', tag) is None
    assert inject_html(b'<body><script>var s = "<!doctype html>";</script>', tag) is None


def test_reddit_style_nonce_policy():
    policy = ("default-src 'none'; script-src 'nonce-abc'; style-src 'unsafe-inline'; "
              "img-src https://www.redditstatic.com; form-action 'self'")
    p, nonce = extend_policy(policy, O, "N")
    d = directives(p)
    assert nonce and d["script-src"] == ["'nonce-abc'", O, "'nonce-N'"]
    # media-src and connect-src fall back to default-src, which no longer blocks us
    assert d["default-src"] == [O]
    assert d["style-src"] == ["'unsafe-inline'"] and d["img-src"] == ["https://www.redditstatic.com"]
