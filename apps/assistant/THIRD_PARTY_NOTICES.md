# Third-Party Notices

## Kokoro ONNX runtime

The app uses `kokoro-onnx`, licensed under MIT:
https://github.com/thewh1teagle/kokoro-onnx

## Kokoro speech model and voices

The bundled Kokoro model is distributed under Apache License 2.0. The bundled
ONNX exports and voice pack are published by the `kokoro-onnx` project:
https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1

The upstream model card and license are available at:
https://huggingface.co/hexgrad/Kokoro-82M

## Bundled Router and browser dependencies

Herald is distributed under its included MIT license. This is a custom friend build.
Kapture (MIT): https://github.com/williamkapke/kapture
Microsoft Playwright MCP (Apache-2.0): https://github.com/microsoft/playwright-mcp
PyWinpty (MIT): https://github.com/andfoy/pywinpty

Browser MCP packages and Chromium are installed from their upstream distributions during setup; their license files remain in the installed packages. Node.js is used from the device when available, otherwise its official Windows LTS archive is downloaded and checksum checked; its license remains in that archive.

Optional g4f API/GUI dependency: gpt4free 8.5.9, GPL-3.0, installed from PyPI during setup. Upstream source and license: https://github.com/xtekky/gpt4free. curl_cffi is installed as its browser-compatible HTTP transport.
