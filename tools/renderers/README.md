# Renderer build and vendoring

From the repository/source-archive root:

```sh
npm ci --prefix tools/renderers --ignore-scripts
python tools/renderers/vendor.py
npm run build --prefix tools/renderers
npm audit --prefix tools/renderers
```

The Python helper verifies pinned npm archives before copying the native chat
libraries, math assets, fonts and upstream licenses. Those `dist/` files must be
tracked despite the repository's general build-output ignore rule.

Mermaid is bundled from its published dependency-externalized core. Its upstream
prebuilt browser bundle embeds an older sanitizer, so changing package metadata
alone does not update the code in that bundle. Our locked build uses DOMPurify
3.4.17 and KaTeX 0.19.0, keeps every diagram loader, and records the actual runtime
modules and any dependencies already embedded in the upstream core.

The parser is rebuilt from the unmodified MIT source for the matching Mermaid
commit, retained in `upstream/parser` with its archive digest. `generate-parser.mjs`
generates its grammars with the locked Langium CLI, retaining schema validation.
This avoids stale dependencies embedded in the published parser artifact.
The upstream YAML module is replaced as a whole with the locked `js-yaml` exports;
the build refuses to proceed if the original module digest changes. No minified
security functions are patched. The build manifest records this replacement.
`fetch_parser.py` can restore the retained source from the pinned official archive.

The output contains the original Mermaid license, dependency notices and a build
manifest. `build.mjs` is the preferred build recipe; this is an Ultimate Assistant
dependency build, not the upstream browser artifact. The renderer remains a
third-party library. The Android source archive includes these build files.
