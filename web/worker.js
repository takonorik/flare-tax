// 計算エンジン: Pyodide (ブラウザの中で動く Python) で flare_tax を動かす Web Worker。
// 画面を固まらせないよう、取得・分類・円価格の計算はすべてここで行う。
//
// メッセージ (すべて id 付き。応答には同じ id を返す)
//   → {type:'run', id, wallets, startDate, endDate, history: ArrayBuffer|null, refresh}
//   ← {type:'progress', id, data} (随時) / {type:'done', id, result, history: ArrayBuffer} / {type:'error', id, message}
//   → {type:'inspect', id, wallets, history: ArrayBuffer}
//   ← {type:'done', id, result} / {type:'error', id, message}
//   ← {type:'ready'} (起動完了) / {type:'error', message} (起動失敗。id なし)

const PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js';

// Python のファイル一覧 (ビルド手順を作らないので、ここに手で持つ。flare_tax/ に足したらここにも足す)
const PY_FILES = [
  '__init__', 'blockscout', 'classify', 'helpers', 'history', 'pipeline',
  'postprocess', 'pricing', 'protocols', 'service',
].map(n => ({ url: `../flare_tax/${n}.py`, path: `/app/flare_tax/${n}.py` }));
// httpx の代用 (ブラウザ用)。本物の httpx と取り違えないよう別のフォルダに置く
PY_FILES.push({ url: './httpx.py', path: '/app/shim/httpx.py' });

// Python 側の入口。結果は JSON の文字列で返し (PyProxy の変換に頼らない)、
// 履歴ファイルは _flare_hist に置いて JS が1回だけコピーして受け取る。
const GLUE = `
import json, traceback
from flare_tax import service
from flare_tax.history import HistoryError

_flare_hist = None

async def _flare_run(wallets, start_date, end_date, history, refresh, on_progress):
    global _flare_hist
    try:
        out = await service.run(
            list(wallets.to_py()), start_date or None, end_date or None,
            history.to_bytes() if history is not None else None, bool(refresh),
            on_progress=lambda d: on_progress(json.dumps(d)), strict=False)
        _flare_hist = out["history_bytes"]
        return '{"ok":true,"result":' + json.dumps(out["result"], ensure_ascii=False) + '}'
    except HistoryError as e:
        return json.dumps({"ok": False, "message": str(e)}, ensure_ascii=False)
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"ok": False, "message": "計算中にエラーが起きました: %s" % e}, ensure_ascii=False)

def _flare_inspect(wallets, history):
    try:
        out = service.inspect_history(history.to_bytes(), list(wallets.to_py()))
        return json.dumps({"ok": True, "result": out}, ensure_ascii=False)
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"ok": False, "message": "計算中にエラーが起きました: %s" % e}, ensure_ascii=False)

def _flare_take_hist():
    global _flare_hist
    data, _flare_hist = _flare_hist, None
    return data
`;

let pyodide = null;
let pyRun = null, pyInspect = null, pyTakeHist = null;

async function init() {
  importScripts(PYODIDE_URL);
  pyodide = await loadPyodide();
  // 更新がすぐ反映されるよう、毎回取り直す (no-cache)
  const files = await Promise.all(PY_FILES.map(async f => {
    const r = await fetch(new URL(f.url, self.location.href), { cache: 'no-cache' });
    if (!r.ok) throw new Error(`${f.url} を読み込めませんでした (${r.status})`);
    return { path: f.path, text: await r.text() };
  }));
  pyodide.FS.mkdirTree('/app/flare_tax');
  pyodide.FS.mkdirTree('/app/shim');
  for (const f of files) pyodide.FS.writeFile(f.path, f.text);
  pyodide.runPython("import sys; sys.path[:0] = ['/app/shim', '/app']");
  pyodide.runPython(GLUE);
  pyRun = pyodide.globals.get('_flare_run');
  pyInspect = pyodide.globals.get('_flare_inspect');
  pyTakeHist = pyodide.globals.get('_flare_take_hist');
}

const ready = init();
ready.then(
  () => { self.postMessage({ type: 'ready' }); },
  (e) => { self.postMessage({ type: 'error', message: String((e && e.message) || e) }); });

// ArrayBuffer を Python に渡す (コピーは Python 側の to_bytes() の1回だけ)
function asView(buf) {
  return buf ? new Uint8Array(buf) : null;
}

async function handleRun(m) {
  const onProgress = (s) => self.postMessage({ type: 'progress', id: m.id, data: JSON.parse(s) });
  const out = await pyRun(m.wallets, m.startDate || '', m.endDate || '', asView(m.history), !!m.refresh, onProgress);
  const parsed = JSON.parse(out);
  if (!parsed.ok) {
    self.postMessage({ type: 'error', id: m.id, message: parsed.message });
    return;
  }
  // 履歴ファイルは Python の bytes を1回だけコピーして、transfer で渡す
  const proxy = pyTakeHist();
  let history = null;
  if (proxy) {
    const buf = proxy.getBuffer();
    try { history = buf.data.slice().buffer; } finally { buf.release(); proxy.destroy(); }
  }
  self.postMessage({ type: 'done', id: m.id, result: parsed.result, history }, history ? [history] : []);
}

function handleInspect(m) {
  const out = JSON.parse(pyInspect(m.wallets, asView(m.history)));
  if (!out.ok) {
    self.postMessage({ type: 'error', id: m.id, message: out.message });
    return;
  }
  self.postMessage({ type: 'done', id: m.id, result: out.result });
}

self.onmessage = async (ev) => {
  const m = ev.data || {};
  try {
    await ready;
  } catch (e) {
    self.postMessage({ type: 'error', id: m.id, message: '計算エンジンを読み込めませんでした。通信環境を確認して、ページを開き直してください。' });
    return;
  }
  try {
    if (m.type === 'run') await handleRun(m);
    else if (m.type === 'inspect') handleInspect(m);
  } catch (e) {
    self.postMessage({ type: 'error', id: m.id, message: '計算中にエラーが起きました: ' + String((e && e.message) || e) });
  }
};
