"""What the server reports about itself at initialize: name, title and icon."""

import base64

import server


def test_server_advertises_an_embedded_png_icon_for_the_connectors_list():
    (icon,) = server.mcp.icons
    assert icon.mime_type == "image/png"
    assert icon.src.startswith("data:image/png;base64,")
    png = base64.b64decode(icon.src.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
