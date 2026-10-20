# The canvas

Your display layer over the user's screens: a whiteboard and pinboard you use however the
moment needs. Draw on it (arrows, boxes, labels, strokes), pin photos, videos, notes and cards,
make small working tools, ask quick questions. It is less intrusive than talking: nothing takes
focus, things can be short-lived (`ttl_seconds`) or stay until cleared (`ttl_seconds=0`), and
showing the same `id` again updates a thing in place. Read one section with `canvas_guide(topic)`.

## Kinds

| kind | for | key fields |
|---|---|---|
| `text`, `markdown` | notes, status, explanations | `title`, `body` |
| `reminder` | something to not forget (stays longer) | `title`, `body` |
| `image` | a photo or screenshot | `image` (https URL or a file on the main PC) |
| `drawing` | marks on the screen itself | `shapes` (below) |
| `ui` | anything interactive: a template or your own markup | `template` + `data`, or `ui` (+ `data`) |

Each monitor and each device can show something different: aim with `monitor` and `devices`.
Placement: `position` = `top-left` … `bottom-right` / `center`, or `{x, y, w}` in 0–1 of the monitor;
`monitor` = `active` (where the user is), `all`, `assistant` (your own display) or a number;
`devices` = `["pc"]`, `["phone"]`, a device name or `["everywhere"]`.

## Drawing

`kind="drawing"`, `shapes`: `{"type": "arrow", "x": .2, "y": .3, "x2": .5, "y2": .5}`, `rect` / `ellipse`
(`x, y, w, h`), `line` (`x, y, x2, y2`), `label` (`x, y, text`), `stroke` (`points: [[x, y], …]`), all in
0–1 of the monitor, optional `color` (#rrggbb[aa]) and `width`. Point at things on the user's real screen.

## Templates

`kind="ui", template=<name>, data={…}`: ready-made cards; you send only data.

| template | data | sends back (canvas_events) |
|---|---|---|
| `choice` | `question`, `options` (strings or `{label, description}`), `multi`, `header`, `allow_other` (default true) | `answer` {choices, other} |
| `checklist` | `title`, `items`, `done` | `checked` {item, done, all} |
| `timer` | `seconds`, `label`, `autostart` (default true), `unpin` (default true: removes itself `unpin_after` seconds after it ends, default 15) | `done` {label} |
| `stats` | `title`, `status` + `live` (badge), `items` [{label, value, note, color}], `columns`, `caption`: scores, prices, progress; re-show the id with new data to update live | nothing |
| `message` | `title`, `to`, `variants` [{label, body}] (or `body`) | `use` {text, variant}; Copy copies |
| `chart` | `title`, `caption`, `chartType` (line, bar, area, pie, donut, scatter), `xKey`, `series` [{dataKey, label, color}], `data` | nothing |
| `video` | `src` (video URL or YouTube link), `title`, `caption`, `autoplay` (muted), `loop`, `poster` | nothing |
| `gallery` | `images` (URLs or `{src, caption}`), `title`, `caption`, `columns`, `aspect` | nothing |

To ask the user something and wait for the answer, use `canvas_ask` (a `choice` card that returns the reply).

## Markup

For your own cards: `kind="ui", ui=<markup>`. It is the language ChatGPT writes for its live widgets:
Markdown plus Svelte-style blocks and JSX-style components.

```
{@body const [count, setCount] = DIL.useState(0)}      state (any JS statements; names are visible below)
{@body const now = DIL.useTick(1000)}                  current time, re-rendering every second (clocks)
{expr}                                                  value anywhere, also in attributes: width={`${p}%`}
{#if cond} ... {:else if other} ... {:else} ... {/if}
{#each list as item, i} ... {:else} (empty) ... {/each}  destructuring works: {#each rows as {name, n}}
**Markdown** works between and inside components: headings, lists, tables, `code`, links, \[ LaTeX \]
```

Handlers are arrow functions: `onClick={() => setCount(count + 1)}`. `onChange` gets the new value,
not an event: `<slider value={v} onChange={setV}/>`. `data` passed with the card is readable as
`data.x` and as plain `x`; show the same `id` with new `data` to update it live (the user's state is kept).

## Components

Layout: `box` (column) and `row`: `gap padding radius background border flex align justify wrap width
height minHeight maxWidth clip aspectRatio`. `card` (bordered box). `grid columns={3}` with `grid-item
span={2}`. `divider`. `spacer`. Spacing numbers are steps of 4px (`gap={3}` = 12px); strings pass through.

Text: `title size="sm|md|lg|xl|2xl"`, `text size="xs|sm|md|lg" color weight tabularNums textAlign
truncate maxLines`, `caption`, `label`, `badge color`, `blockquote`, `underline`, `kbd`,
`icon name="lucide-name" size="xs..2xl" color fill inline`.

Input: `button` (`variant="solid|outline|ghost|soft" color size="sm|md|lg" block disabled submit`),
`input` (`type placeholder value onChange onSubmit`; Enter calls onSubmit), `textarea rows autoResize`,
`select options={[{label, value}]} block`, `slider min max step`, `checkbox checked lineThrough`,
`switch`, `radio-group direction` + `radio value`, `segmented-control options block`, `date-picker`,
`time-picker`, `form onSubmit`, `pressable onClick tooltip` (any clickable surface).

Show: `progress value max color`, `table` + `table-row` + `table-cell width align`, `Chart content={{…}}`
(as the chart template), `svg` with any SVG children (attributes can be expressions; camelCase like
`strokeWidth` is fine), `image src alt aspectRatio radius`, `video src autoplay loop poster` (YouTube
links work), `CodeBlock language editable` (`mermaid` draws a diagram), `popover` > `popover-trigger` +
`popover-content`, `collapsible title`, `carousel showArrows visibleItems` + `carousel-item`.

Colors: `default secondary tertiary primary success danger warning discovery`, or any CSS color.
Backgrounds: `surface surface-secondary surface-tertiary`, or any CSS color. Radius: `sm md lg xl 2xl full`.

## Talking back

| Call | What happens |
|---|---|
| `UA.send(text)` | The user says `text` to you from the card. It runs as a chat turn that knows the card's id and state; your reply shows beside the card unless you update the card yourself. |
| `UA.action(name, data)` | A named event with data, for the agent that made the card (`canvas_events`). |
| `UA.close()` | Removes the card. The user can also close it with its × button. |
| `GenUI.copy(text)` / `GenUI.openUrl(url)` | Copy to clipboard; open a link in the user's browser. |

Each event carries the card's saved state. `canvas_events(id, after=<last seq>, wait_seconds=60)` waits
for the next one; `canvas_list` shows each card's state too.

## Example

```
{@body const [left, setLeft] = DIL.useState(data.minutes)}
{@body const [done, setDone] = DIL.useState([])}
<row justify="between">
  <title>{data.task}</title>
  <badge color={left < 5 ? "warning" : "success"}>{left} min</badge>
</row>
<slider min={5} max={60} step={5} value={left} onChange={setLeft}/>
{#each data.steps as step}
  <checkbox checked={done.includes(step)} lineThrough
            onChange={on => setDone(on ? [...done, step] : done.filter(s => s !== step))}>{step}</checkbox>
{/each}
<row gap={2}>
  <box flex="1"><button block onClick={() => UA.action("start", {minutes: left, done})}>Start</button></box>
  <button variant="outline" color="secondary" onClick={() => UA.send("Break this into smaller steps")}>Help</button>
</row>
```

`canvas_show(kind="ui", id="focus", title="Focus", ui=…, data={"task": "Essay intro", "minutes": 25,
"steps": ["Outline", "Draft", "Edit"]}, ttl_seconds=0)`

## Keep in mind

- It floats over the user's work: keep each thing small and focused, and clear it when it is done.
- Prefer a template over custom markup; prefer updating an id over adding another element.
- A render error shows as a small warning line on the card and arrives as an `error` event.
- Icons, maths and diagrams use bundled libraries on PC and phone. Web content still needs a connection.
- Interactive cards work on PC and phone; keep phone layouts narrow.


## Feeds

Use `canvas_feed_save(spec)` to connect a real data source to a live card. The main PC refreshes
it without an AI call on each poll. Definitions survive restarts. `canvas_feed_list()` shows status
and errors; `canvas_feed_refresh(id)` requests a refresh now; `canvas_feed_remove(id)` stops it.
Closing or clearing a feed card pauses its updates, so the next poll does not reopen it.

A feed specification has `id`, `source`, `card`, `interval_seconds`, and optional `mapping`,
`conditions`, `enabled`. `card` uses the same fields as `canvas_show`: a template plus data,
custom ui plus data, including devices, position and monitor. Feed cards use kind `ui`.
Use `ttl_seconds: 0` for a card that should stay until closed.

Sources: `weather` retrieves current weather, `json` reads a JSON API, `html` extracts a web page,
`file` reads local data, and `script` runs a local program producing JSON. Source-specific options
identify the location or URL, selectors, file path or command. Use URLs accessible from the main PC;
local paths refer to that PC. Scripts should be bounded data collectors, not interactive programs.

Source shapes (all return JSON objects):
- Weather: `{"type":"weather","latitude":39.77,"longitude":-86.16,"units":"fahrenheit","title":"Indianapolis"}`.
  Returns card-ready `title`, `items`, `caption`, plus `current` and `current_units`. Without mapping,
  bind to the stats template directly. Weather is model data from Open-Meteo; retain its attribution.
- API: `{"type":"json","url":"https://example.com/status","select":"result"}`. Optional `params`
  object and `headers_env:{"Authorization":"MY_API_AUTH"}` read credentials from the runtime environment.
  Do not put secrets in URLs or saved definitions. Redirects are not followed; use the final endpoint.
- Scraper: `{"type":"html","url":"https://example.com/","fields":{"headline":{"selector":"h1"},"link":{"selector":"a.story","attribute":"href"}}}`.
  Static HTML only; missing matches return null. Browser-rendered pages can use a script source.
- File: `{"type":"file","path":"C:\\data\\status.json","select":"result"}`.
- Script: `{"type":"script","argv":["C:\\Python314\\python.exe","C:\\data\\collector.py"],"timeout":15}`.
  Optional absolute `cwd`. No shell; stdout must be one JSON object. Collectors run automatically and
  should only collect data. HTTP/file/script output is bounded; script timeouts are at most 30 seconds.

Example: `canvas_feed_save({"id":"weather","source":{"type":"weather","latitude":39.77,
"longitude":-86.16},"card":{"kind":"ui","template":"stats","devices":["phone"],"ttl_seconds":0},
"interval_seconds":600})`. Coordinates here are an example, not the user's live location.

`mapping` maps a target path in the card's data to a dotted path in the source result; numeric
segments address list items. For example, `{"items.0.value": "current.temperature_2m"}` puts a
weather temperature in the first stats item. Keep labels, units and layout in the card data.
Refresh intervals must be at least 10 seconds, 600 seconds for weather, and 60 seconds for HTML.

Conditions are `[{"id": "hot", "path": "current.temperature_2m", "op": "gt", "value": 30}]`.
Paths refer to source data. Conditions emit on a transition into a matching state; set
`fire_initial: true` on a condition when an initially matching observation should also fire.
The event is `app.assistant.feed.condition`; its payload identifies `feed_id` and `condition_id`.
To have the assistant react, use Herald's existing `schedule_agent` event schedule with that event
and a payload filter for those identifiers. Updating a card alone does not wake an assistant.

Do not invent fetched values. Inspect the first refresh result and its source fields before
mapping them into a card, and surface stale data or collection errors honestly.


## Moving and clearing phone items

Set `draggable=true` for a floating note, reference, or interactive card the user may need to move out of the way. Its drag handle moves the item without consuming card controls. Leave it false for screen-aligned arrows and annotations. The user placement survives content updates; changing the item position or disabling dragging restores assistant placement. Removal clears the local placement.

Use finite `ttl_seconds` for demonstrations, and clear showcase elements when finished. `ttl_seconds=0` deliberately persists until cleared. A canvas tool response confirms publication, not verified rendering on the device.
