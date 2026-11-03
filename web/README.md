# Fomo (static / Pyodide)

A no-backend version of the reservation grid, styled after the old Domo interface. Python runs in the browser via Pyodide (WebAssembly), so the same
`kulku.py` slot and booking logic is reused. The page is plain HTML, CSS and
vanilla JavaScript.

`index.html` is **self-contained**: `kulku.py`, `app.py`, and the hero pattern
are embedded in it. You can open it directly from disk
(double-click) as long as the browser can reach the Pyodide CDN. No local
Python server is needed for normal use.

The reservation view follows Domo's own `booking-tabs` / `booking-table`
structure: day tabs, machines across the top, time rows down the left, a
sticky time column, green `Book` buttons and red `Cancel` buttons for your own
bookings.
Space names come from the API's English translation when available; otherwise the
API default name is shown as-is.

This is an unofficial personal mockup. The background pattern is used only to
match the old Domo look.

## Build

After editing `app.py`, `template.html` or the CLI's `kulku.py`:

```bash
python3 web/build.py
```

That regenerates `web/index.html`. Source files:

- `template.html` — UI and JavaScript
- `app.py` — browser-side Python glue
- `assets/` — hero pattern, inlined by the build
- `build.py` — embeds the Python files and assets into `index.html`

## Run

Options, in order of convenience:

1. **Double-click `web/index.html`.** Works with internet access because
   Pyodide comes from jsDelivr. `file://` pages cannot read neighbouring
   files, which is why the Python source is embedded.
2. **Serve the folder**, e.g. `python3 -m http.server 8765` from the repo root,
   then open `http://localhost:8765/web/`. Use this for local testing.
3. **Host it** on GitHub Pages / Cloudflare Pages / Netlify. For this repo,
   publish from the repository root.

On a phone, use the hosted URL or `http://<computer-ip>:8765/web/` on the same
WiFi. A hosted URL is usually the most convenient for phone use.

## Auth

- **Log in** with your Kulku phone number and password. The token is stored in
  the browser's `localStorage` (same model as the official `time.kulku.io`).
- **Import `.auth.json`** if you already have a session file. It is read
  locally by the browser and stored in `localStorage`.
- **Log out** removes the token.

The password is never stored. The token is only sent to `portal.kulku.io`.
If `localStorage` is unavailable (some browsers restrict it on `file://`),
the page still works for the current session but does not remember the login.

## Speed

- The grid fetches the space list once, then fetches that day's bookings and
  every visible machine **in parallel**. It no longer makes a dozen sequential
  API calls.
- Booking and cancelling from the grid are one API request each; the server is
  still the authority on conflicts and quotas.
- Pyodide itself is the slow part of a cold start (roughly a few seconds and
  ~10 MB). After the first load the browser caches it.

## Notes

- First load downloads Pyodide from jsDelivr (roughly 10 MB); it is cached by
  the browser afterwards.
- The Kulku API currently allows cross-origin requests (`Access-Control-Allow-
  Origin: *`), which is what makes a static page possible. If that changes,
  this page stops working and a small backend would be needed.
- Paid bookings still need the normal Kulku payment UI. This page is for free
  reservations such as laundry and sauna.
- For offline use, self-host the Pyodide runtime and change `CDN` in
  `template.html`, then rebuild. Browsers usually block `file://` + WASM file
  reads, so an offline copy should be served over HTTP or installed as a PWA.
- `pyxhr` is synchronous; the UI blocks briefly while a request runs. For a
  smoother version, move Pyodide into a Web Worker.
