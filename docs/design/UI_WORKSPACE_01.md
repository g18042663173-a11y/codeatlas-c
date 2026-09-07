# Question workspace · visual direction 1

The selected light-blue question workspace is implemented in the existing FastAPI
single page. It does not add a frontend framework or change the knowledge pipeline.

- `server/static/index.html`: semantic shell, retained workflow screens and existing API controllers.
- `server/static/workspace.css`: shared colors, spacing, typography and responsive navigation.
- `server/static/workspace.js`: composer, last-run presentation, controlled DOM actions,
  source pagination and snapshot checks.
- `tests/ui_workspace.test.cjs`: Node built-in tests; run `node --test tests/ui_workspace.test.cjs`.
- `tests/test_ui_workspace.py`: static serving and distribution checks.

Home suggestions only fill the composer. Running a question is explicit, shared with
the Agent in-flight lock, and never implicitly saves a card or selects an old session.
Failed requests do not retry automatically. Browser storage never holds raw runs.
Raw tool payloads and secondary evidence can be expanded when needed.

Source readers request at most 300 lines per page and compare the returned
`X-CodeAtlas-Knowledge-Set` with the run/page snapshot. A changed snapshot displays
a warning instead of substituting new code for old evidence. This is a UI check in
addition to the existing server read restrictions, not a replacement authorization layer.

## Asset provenance

Icons are unmodified [Feather v4.29.0](https://github.com/feathericons/feather/tree/v4.29.0/icons),
fixed source commit `f81cd40fdcdd5e94f3f97eb670a5058e3aac528d`.
The upstream MIT license is included in `server/static/icons/LICENSE` and packaged
with the application. SVGs are local files, not handwritten approximations or CDN dependencies.
Fonts use the platform's system/CJK font stack. There are no new raster assets.

See the root `design-qa.md` for local visual evidence, checks and known limits.
