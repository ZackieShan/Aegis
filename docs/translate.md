# Translate

Type, speak or photograph something and read — or hear — it in another
language. It runs on your own machine: nothing is sent anywhere, and it works
with the internet down.

* **The page** — `/translate` (also in the sidebar, in the simple home
  screen's menu, and `/translate` as a command in a chat).
* **In a chat** — *"translate this into French"*, *"how do you say thank you
  in Japanese?"*, *"what does this say?"* The assistant uses its `translate`
  tool.
* **A photo** — a sign, a label, a page: the camera button on the page reads
  the words in the picture and gives them in your language.

## The page

Two language boxes (the first can be left on *Detect language*), a box to
type or paste into, and the answer in large type you can show to someone.

| Button | What it does |
|---|---|
| Microphone | Speak instead of typing. Uses the phone's own speech recognition, or the server's when one is set up. It translates as soon as you stop. |
| Camera | Take or choose a photo; the words in it are translated. The picture is not kept, only what it said. |
| Speaker | Reads the translation aloud — in the phone's own voice for that language when it has one, otherwise the server's voice. |
| Reply | Swaps the two languages and clears the box, ready for the other person's answer. |
| Swap | Exchanges the languages and puts the translation in the box, to check it by translating it back. |

A long text is translated a paragraph at a time and appears as it is done;
the paragraphs stay where they were. One translation takes up to 20,000
characters.

*Lately* lists your recent translations (yours only; up to 200). Tap one to
see it again, or forget one or all of them.

## Languages

160 languages, the most used at the top of the list. Chinese is offered as
its two scripts, and a few regional variants are separate because they change
the result (Portuguese of Brazil and of Portugal, Spanish of Mexico and of
Spain, French of Canada). A language can be given by name, by its own name or
by code: `Spanish`, `español`, `es`, `zh-TW`.

When the source is left on *Detect language*, a sentence is recognised by
its script and its commonest words; for a word or two the model is asked.
The answer says which language it took the text to be.

## The model

The translator is [TranslateGemma](https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/)
(Google, January 2026; Gemma terms of use), a translation-tuned model that
also reads text in pictures. It is addressed like any other model on the
local model server, under the name `translate`.

**Without it**, translation still works: the ordinary chat model is asked
with the same words. That is slower to start and less even across languages,
and it cannot read a photo.

To install it (about 8 GB for the 12B model with its picture reader):

```bash
mkdir -p models/translategemma-12b-it && cd models/translategemma-12b-it
base=https://huggingface.co/mradermacher/translategemma-12b-it-GGUF/resolve/main
aria2c -c -o translategemma-12b-it.Q4_K_M.gguf       "$base/translategemma-12b-it.Q4_K_M.gguf"
aria2c -c -o translategemma-12b-it.mmproj-Q8_0.gguf  "$base/translategemma-12b-it.mmproj-Q8_0.gguf"
```

and add it to `llama-swap.yaml`:

```yaml
models:
  "translate":
    cmd: |
      llama-server
      -m "/path/to/models/translategemma-12b-it/translategemma-12b-it.Q4_K_M.gguf"
      --mmproj "/path/to/models/translategemma-12b-it/translategemma-12b-it.mmproj-Q8_0.gguf"
      --host 127.0.0.1 --port ${PORT}
      -ngl 99 -c 8192 -fa on
      --jinja --chat-template-file "/path/to/models/translategemma-12b-it/plain-turns.jinja"
      --no-webui
    ttl: 900

groups:
  # Small models that ride along beside whatever big model is loaded: loading
  # one never evicts a chat model, and a chat model loading never evicts it.
  riders:
    swap: false
    exclusive: false
    persistent: true
    members: [translate]
```

**The template file matters.** The model's built-in chat template expects
its own message shape (the two language codes as fields). Given an ordinary
chat message it wraps the whole request as "translate English into English",
and translations come back in the wrong language. `plain-turns.jinja` passes
a message through as written:

```jinja
{{- bos_token -}}
{%- for message in messages -%}
{%- if message['role'] == 'assistant' -%}{%- set role = 'model' -%}{%- else -%}{%- set role = 'user' -%}{%- endif -%}
{{- '<start_of_turn>' + role + '\n' -}}
{%- if message['content'] is string -%}
{{- message['content'] | trim -}}
{%- else -%}
{%- for part in message['content'] -%}
{%- if part['type'] == 'image' or part['type'] == 'image_url' -%}{{- '<start_of_image>' -}}
{%- elif part['type'] == 'text' -%}{{- part['text'] | trim -}}
{%- endif -%}
{%- endfor -%}
{%- endif -%}
{{- '<end_of_turn>\n' -}}
{%- endfor -%}
{%- if add_generation_prompt -%}{{- '<start_of_turn>model\n' -}}{%- endif -%}
```

(`--chat-template gemma` does not do this: with `--jinja` the word is taken
as the template itself.) Aegis writes the model's own instruction wording —
both languages named, "produce only the translation" — in the message.

On a machine with unified memory the 12B model answers a sentence in one to
two seconds on the GPU (about 14 tokens a second) and four to six on the CPU.
The 4B model (2.5 GB) is about three times faster and noticeably less exact.

| Setting | Meaning |
|---|---|
| `translate_model` (settings) | The model's name on the model server. Default `translate`; an empty string always uses the chat model. |
| `AEGIS_TRANSLATE_URL` (environment) | The chat-completions address of the translator, when it is not the local model server. |

## For the assistant and for programs

The `translate` tool takes `{"text": …, "to": …, "from"?: …}` and returns the
translation with the two languages named. It keeps nothing.

```
GET    /api/translate/languages       the languages, common ones first
POST   /api/translate                 {"text", "to", "from"?, "keep"?}
POST   /api/translate/stream          the same; one line of JSON per paragraph, then the whole result
POST   /api/translate/photo           multipart: file, to, from?
GET    /api/translate/history         your recent translations
DELETE /api/translate/history[/{id}]  forget them, or one
```

All of it needs a signed-in person; the history is per person.

## Limits worth knowing

* The model works on about two thousand tokens at a time, which is why long
  text goes by paragraph. A single paragraph longer than about 1,400
  characters is cut at sentence ends.
* A translation is not checked by anyone. For a dose, a legal form or
  anything where a wrong word matters, translate it back (the swap button)
  and see whether it still says what you meant.
* Handwriting and stylised lettering in photos are read less reliably than
  print.
* Speech in and out depend on what the phone or the server offers for that
  language; the translation itself does not.
