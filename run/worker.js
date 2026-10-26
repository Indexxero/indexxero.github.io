// The run itself, off the main thread.
//
// WHY A WORKER. pilot.py on the scale shape takes minutes. On the main thread
// the tab is frozen for all of it: no progress, no scroll, and Chrome offers to
// kill the page. Here the UI stays live and every stage posts a message.
//
// WHY EVERYTHING IS LOADED FROM ./vendor/. Pyodide 0.28.3 and every wheel sit
// beside the page. A CDN would be a request to a third party, which would see
// the visitor's address on a page whose whole claim is that their numbers never
// leave the laptop. There is no CDN to be unreachable during an engagement
// either. The cost is 20.8 MB on first load, cached after that.
// A MODULE worker, so pyodide.mjs is a static import. A classic worker with
// importScripts cannot then dynamic-import an ES module beside it.
import { loadPyodide } from './vendor/pyodide/pyodide.mjs';

let pyodide = null;
let pilotBytes = null;
let receiptBytes = null;
let diffBytes = null;
const say = (stage, detail) => postMessage({ type: 'progress', stage, detail });

async function boot() {
  if (pyodide) return pyodide;
  say('Starting Python', 'about 20 MB, from this page, once');
  pyodide = await loadPyodide({ indexURL: './vendor/pyodide/' });
  say('Loading pandas', 'from this page, no network');
  await pyodide.loadPackage(['pandas', 'micropip'], { messageCallback: () => {} });
  // openpyxl is not in the Pyodide package set (measured: loadPackage answers
  // "No known package with name 'openpyxl'"). pilot.py needs it for pilot.xlsx.
  // Installed from the wheel beside this page, so still no network.
  //
  // BOTH WHEELS, AND `deps: false`, AND THE REASON IS A MEASURED DEFECT.
  // Installing openpyxl alone let micropip resolve its dependency itself, and a
  // Playwright run recorded three requests to pypi.org and
  // files.pythonhosted.org for et_xmlfile AFTER the files were chosen. The page
  // claimed in its own banner that it makes no network request at that point.
  // The claim was false and the run is what found it, not the reading.
  const mp = pyodide.pyimport('micropip');
  await mp.install(
    ['./vendor/pyodide/openpyxl-3.1.5-py2.py3-none-any.whl',
     './vendor/pyodide/et_xmlfile-2.0.0-py3-none-any.whl'],
    { deps: false },
  );
  // Fetched once, here, and kept: the page needs its bytes for the receipt's
  // pilot_sha256 and must not go back for them mid-run.
  pilotBytes = new Uint8Array(await (await fetch('./pilot.py')).arrayBuffer());
  // receipt.py beside pilot.py, as on a laptop. pilot.py imports it from its own
  // folder (never a subprocess, which raises OSError here) and writes
  // receipt.json itself. The page used to write its own receipt instead: a
  // second implementation of the same file, which had already drifted from the
  // verifier once (a list where receipt.py keys by role). One implementation now.
  receiptBytes = new Uint8Array(await (await fetch('./receipt.py')).arrayBuffer());
  // diff_runs.py, imported by the close pack only when --before is given. The
  // page passes no --before, so it is not imported today; it ships beside
  // pilot.py anyway so the folder is the kit's folder and not a subset of it.
  diffBytes = new Uint8Array(await (await fetch('./diff_runs.py')).arrayBuffer());
  return pyodide;
}

onmessage = async (e) => {
  // `warm` is sent at page load, BEFORE a file can have been chosen, so that
  // every byte this page will ever fetch is already fetched by then. Without it
  // the 20 MB arrived after the file choice, which is exactly the window the
  // banner promises is silent.
  if (e.data && e.data.type === 'warm') {
    try { await boot(); postMessage({ type: 'warm-done', pilotSha: await sha256Hex(pilotBytes) }); }
    catch (err) { postMessage({ type: 'error', message: String(err && err.message || err) }); }
    return;
  }
  const { schedule, invoices, mapping, asof, scheduleName, invoicesName, gl } = e.data;
  try {
    const py = await boot();
    const FS = py.FS;
    for (const d of ['/work', '/work/in', '/work/out']) { try { FS.mkdir(d); } catch {} }
    // Clear any previous run so a second run cannot inherit a stale file.
    for (const n of FS.readdir('/work/out')) if (!n.startsWith('.')) FS.unlink('/work/out/' + n);

    FS.writeFile('/work/pilot.py', pilotBytes);
    FS.writeFile('/work/receipt.py', receiptBytes);
    FS.writeFile('/work/diff_runs.py', diffBytes);
    FS.writeFile('/work/in/' + scheduleName, new Uint8Array(schedule));
    FS.writeFile('/work/in/' + invoicesName, new Uint8Array(invoices));
    const mappingBytes = new TextEncoder().encode(mapping);
    FS.writeFile('/work/in/mapping.json', mappingBytes);
    // The mapping this run used, in the OUTPUT folder and BEFORE the run, so the
    // kit's own receipt lists it. receipt.py verify compares the union of the
    // receipt's outputs and the folder's files, so a file added to the zip after
    // the receipt was made would read as a mismatch on every laptop.
    FS.writeFile('/work/out/mapping-used.json', mappingBytes);
    // The ledger balance, optional, passed as --deferred-gl (Cowork 06:10).
    // A copy goes in the output folder before the run, for the same reason as
    // the mapping: the run's `difference` column depends on it, and receipt.py
    // at 2e0165e does not hash it as an input, so without the copy the zip
    // would carry a figure whose source nobody could recompute.
    const hasGl = !!(gl && gl.byteLength);
    if (hasGl) {
      FS.writeFile('/work/in/deferred_gl.csv', new Uint8Array(gl));
      FS.writeFile('/work/out/deferred-gl-used.csv', new Uint8Array(gl));
    } else {
      try { FS.unlink('/work/in/deferred_gl.csv'); } catch {}
    }

    say('Running the comparison', 'pilot.py, unchanged');
    const t0 = Date.now();
    py.runPython(`
import sys, runpy, io, contextlib
sys.argv = ['pilot.py', '--schedule', '/work/in/${scheduleName}', '--invoices', '/work/in/${invoicesName}',
            '--map', '/work/in/mapping.json', '--asof', '${asof}', '--out', '/work/out']${hasGl ? " + ['--deferred-gl', '/work/in/deferred_gl.csv']" : ''}
_buf = io.StringIO()
_stopped = None
try:
    with contextlib.redirect_stdout(_buf):
        runpy.run_path('/work/pilot.py', run_name='__main__')
except SystemExit as _e:
    # pilot.py stops on purpose with sys.exit("<plain message>") when a run
    # cannot honestly proceed, e.g. a required column maps to nothing. On a
    # laptop that prints the sentence; here it would surface as a Pyodide
    # traceback with the sentence below the fold. So it is caught and only the
    # kit's own words go to the page. A clean exit (code 0 or None) is not a stop.
    if _e.code not in (0, None):
        _stopped = _e.code if isinstance(_e.code, str) else (_buf.getvalue().strip() or f"pilot.py stopped with exit {_e.code}")
_printed = _buf.getvalue()
`);
    const printed = py.globals.get('_printed');
    const stopped = py.globals.get('_stopped');
    const elapsedMs = Date.now() - t0;
    if (stopped) {
      // No zip: a stop means nothing was computed, and a zip of partial files
      // next to a stop message is the confusion this branch exists to prevent.
      postMessage({ type: 'error', message: String(stopped) });
      return;
    }

    const files = {};
    for (const n of FS.readdir('/work/out')) {
      if (n === '.' || n === '..') continue;
      files[n] = FS.readFile('/work/out/' + n);
    }
    postMessage({ type: 'done', files, printed: String(printed), elapsedMs }, Object.values(files).map(u => u.buffer));
  } catch (err) {
    // The message pilot.py printed is usually the useful half, so it is kept.
    postMessage({ type: 'error', message: String(err && err.message ? err.message : err) });
  }
};

async function sha256Hex(bytes) {
  const d = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}
