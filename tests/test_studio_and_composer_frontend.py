"""The Studio's layout, its Create screen and the composer's camera button —
the things other code and people rely on finding.

These read the page sources. They do not replace driving the pages in a
browser; they keep the pieces other code depends on from disappearing in a
later edit:
  * every Studio tab is still a ``.gallery-tab`` with its ``data-tab`` (chat
    and the photo detail switch tabs by clicking one);
  * making something happens on the Create screen and stays there — it does
    not send the person off to the Queue tab;
  * a photo can be given to Aegis from the message box itself, on a phone too.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def _read(*parts):
    return (STATIC.joinpath(*parts)).read_text(encoding="utf-8")


def test_the_studio_keeps_all_its_tabs_with_three_up_front():
    source = _read("js", "gallery.js")
    start = source.index('<div class="gallery-tabs">')
    strip = source[start:source.index('<div class="gallery-more-menu"', start)]
    menu = source[source.index('<div class="gallery-more-menu"', start):source.index('<div class="modal-body">', start)]
    tab = re.compile(r'class="gallery-tab(?: active)?"[^>]*data-tab="(\w+)"')
    assert tab.findall(strip) == ["create", "images", "queue"]
    assert tab.findall(menu) == ["albums", "editor", "movie", "music", "styles", "settings"]
    assert 'class="gallery-tab active" title="Make a picture' in strip                  # it opens on Create
    assert 'id="gallery-more-btn"' in strip and "gallery-tab-more" in strip
    # outside code can show a tab without drawing into it, and open a finished item
    assert "export function showStudioTab(name)" in source and "showStudioTab," in source
    assert "renderStudioHome(host, {" in source and "openItem: (id) => { openGalleryImage(id); }" in source
    # a picture's Animate / Stylize buttons start on Create with that picture
    assert "setStudioSource({ id: img.id, url: img.url" in source
    # the chat's "edit this picture" uses the helper instead of hiding two containers by hand
    assert "showStudioTab('editor')" in _read("js", "chatRenderer.js")


def test_making_something_stays_on_the_create_screen():
    source = _read("js", "studioHome.js")
    for endpoint in ("/api/image/generate", "/api/image/edit", "/api/video/generate", "/api/audio/generate", "/api/tts/synthesize",
                     "/api/video/status/", "/api/video/cancel/", "/api/queue", "/api/model-notes/lookup"):
        assert endpoint in source, endpoint
    assert 'data-tab="queue"' not in source and "gallery-tab" not in source               # no hand-off to another tab
    assert "gallery-refresh" in source                                                    # the Gallery hears about new work
    # each kind keeps its own words; a model is only sent for a job it can do
    for kind in ("image:", "video:", "audio:", "voice:"):
        assert kind in source.split("drafts: {", 1)[1].split("},\n  source:", 1)[0]
    assert "modelsFor(which).some((m) => m.model === draft.model) ? draft.model : ''" in source
    # errors are words, never "[object Object]"
    assert "function sayError(" in source and "JSON.stringify(detail)" in source
    assert _read("js", "jobQueue.js").count("gallery-refresh") == 1


def test_a_photo_can_be_given_from_the_message_box():
    page = _read("index.html")
    left = page[page.index('<div class="chat-input-left">'):page.index('<div class="chat-input-right">')] if '<div class="chat-input-right">' in page else page
    assert 'id="composer-photo-btn"' in left and 'aria-label="Add a photo"' in left
    button = _read("js", "photoButton.js")
    assert "input.accept = 'image/*'" in button and "setAttribute('capture', 'environment')" in button
    assert "identifyPhoto(file, hint)" in button and "fileHandlerModule.addFiles(" in button
    assert "initPhotoButton();" in _read("app.js")
    home = _read("lite", "home.js")
    # The family screen: one tap, the phone's own chooser (take or pick), and the photo is on its way to be
    # sorted — no sheet in between asking what to do with it (tests/test_family_answers.py has the rest).
    assert "class: 'cam'" in home and "accept: 'image/*'" in home and "setAttribute('capture'" not in home
    assert "/api/reference/identify" in home and "form.append('attachments'" not in home


def test_the_mode_switch_says_auto():
    page = _read("index.html")
    button = page[page.index('id="mode-agent-btn"') - 80:page.index('id="mode-chat-btn"')]
    assert ">Auto</button>" in button and 'id="mode-agent-btn"' in button                 # the name changed, the id did not
    assert "json.auto.label" in _read("js", "chat.js")
