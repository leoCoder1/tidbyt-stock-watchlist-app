# Test-only wrappers capture the selected render tree while constructing real widgets.
def test_node(kind, constructor, args):
    props = dict(args)
    children = []
    if "child" in props:
        children.append(props["child"][1])
        props["child"] = props["child"][0]
    if "children" in props:
        children = [c[1] for c in props["children"]]
        props["children"] = [c[0] for c in props["children"]]
    return constructor(**props), {
        "kind": kind,
        "props": {k: v for k, v in args.items() if k not in ("child", "children")},
        "children": children,
    }

def test_root(**args):
    node, trace = test_node("Root", render.Root, args)
    print("TEST_TREE:" + json.encode(trace))
    return node

def test_row(**args):
    return test_node("Row", render.Row, args)

def test_box(**args):
    return test_node("Box", render.Box, args)

def test_text(content, **args):
    return render.Text(content, **args), {"kind": "Text", "props": dict(args, content = content), "children": []}

def test_column(**args):
    return test_node("Column", render.Column, args)

def test_padding(**args):
    return test_node("Padding", render.Padding, args)

def test_animation(**args):
    return test_node("Animation", render.Animation, args)

def test_sequence(**args):
    return test_node("Sequence", render.Sequence, args)

def test_marquee(**args):
    return test_node("Marquee", render.Marquee, args)

def test_cache_set(key, value, ttl_seconds):
    cache.set(key, value, ttl_seconds = ttl_seconds)
    print("TEST_WRITE:" + key.split(":")[0])
    if key.startswith("last:"):
        print("TEST_CACHE:" + value)

def main(config):
    indices = config.str("test_indices", "")
    if indices:
        cache.set("last-indices:" + hash.sha256(config.str("rapidapi_key", "")), indices, ttl_seconds = 604800)

    # Two main() calls share Pixlet's real cache, as consecutive device renders do.
    lists = json.decode(config.str("test_lists", "[]"))
    if lists:
        for symbols in lists:
            root = app_main(config, test_symbols = symbols)
    else:
        for i in range(int(config.str("test_renders", "1"))):
            root = app_main(config)
    return root
