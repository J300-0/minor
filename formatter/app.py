"""
app.py — Web UI (drag & drop)
Thin Flask wrapper around core.pipeline.run().
All business logic lives in core/ and stages/.
"""

import os
import uuid
from flask import Flask, request, send_file, jsonify, render_template_string
from core import config
from core.pipeline import run

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

os.makedirs(config.INPUT_DIR, exist_ok=True)

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IEEE Formatter</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#0c0c0c; --surface:#161616; --border:#272727;
  --accent:#4ade80; --accent-glow:#4ade8022;
  --text:#e2e2e2; --muted:#555; --error:#f87171;
  --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif;
}

body{background:var(--bg);color:var(--text);font-family:var(--sans);
  min-height:100vh;display:grid;place-items:center;padding:2rem}

.wrap{width:100%;max-width:600px}

/* header */
.header{margin-bottom:2.5rem}
.header h1{font-family:var(--mono);font-size:1.25rem;font-weight:600;
  color:var(--accent);letter-spacing:-.02em}
.header p{color:var(--muted);font-size:.78rem;margin-top:.35rem;font-family:var(--mono)}

/* pipeline steps row */
.pipeline{display:flex;align-items:center;gap:0;margin-bottom:2rem;flex-wrap:wrap}
.step{font-family:var(--mono);font-size:.6rem;color:var(--muted);
  padding:.2rem .4rem;border-radius:3px;transition:color .25s,text-shadow .25s;white-space:nowrap}
.step.active{color:var(--accent);text-shadow:0 0 8px var(--accent)}
.step.done{color:#22c55e}
.arrow{color:var(--border);font-size:.6rem;padding:0 1px;font-family:var(--mono)}

/* drop zone */
.drop{border:1.5px dashed var(--border);border-radius:8px;padding:3rem 2rem;
  text-align:center;cursor:pointer;position:relative;
  background:var(--surface);transition:border-color .2s,background .2s}
.drop.over{border-color:var(--accent);background:var(--accent-glow)}
.drop input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-icon{font-size:2rem;display:block;margin-bottom:.75rem}
.drop h2{font-size:.95rem;font-weight:600;margin-bottom:.3rem}
.drop p{color:var(--muted);font-size:.75rem;font-family:var(--mono)}

/* file badge */
.badge{display:none;margin-top:1.2rem;padding:.75rem 1rem;
  background:var(--surface);border:1px solid var(--border);border-radius:6px;
  font-family:var(--mono);font-size:.78rem;align-items:center;gap:.75rem}
.badge.show{display:flex}
.badge-name{flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge-size{color:var(--muted);flex-shrink:0}

/* button */
.btn{display:none;width:100%;margin-top:1rem;padding:.8rem;
  background:var(--accent);color:#000;border:none;border-radius:6px;
  font-family:var(--mono);font-size:.88rem;font-weight:600;
  cursor:pointer;transition:opacity .2s}
.btn:hover{opacity:.85}
.btn.show{display:block}
.btn:disabled{opacity:.35;cursor:not-allowed}

/* log */
.log{display:none;margin-top:1.2rem;padding:.9rem 1rem;
  background:var(--surface);border:1px solid var(--border);border-radius:6px;
  font-family:var(--mono);font-size:.72rem;color:var(--muted);
  max-height:160px;overflow-y:auto}
.log.show{display:block}
.ln{padding:1px 0}
.ln.ok{color:var(--accent)}
.ln.err{color:var(--error)}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>// ieee_formatter</h1>
    <p>PDF / DOCX → structured JSON → LaTeX → IEEE PDF</p>
  </div>

  <div class="pipeline">
    <span class="step" id="s1">layout_parser</span><span class="arrow"> → </span>
    <span class="step" id="s2">document_parser</span><span class="arrow"> → </span>
    <span class="step" id="s3">normalizer</span><span class="arrow"> → </span>
    <span class="step" id="s4">template_renderer</span><span class="arrow"> → </span>
    <span class="step" id="s5">latex_compiler</span>
  </div>

  <div class="drop" id="drop">
    <input type="file" id="fileInput" accept=".pdf,.docx,.doc">
    <span class="drop-icon">📄</span>
    <h2>Drop your file here</h2>
    <p>PDF or DOCX &nbsp;·&nbsp; up to 50 MB</p>
  </div>

  <div class="badge" id="badge">
    <span>📎</span>
    <span class="badge-name" id="bName">—</span>
    <span class="badge-size" id="bSize">—</span>
  </div>

  <button class="btn" id="btn" onclick="convert()">Convert → IEEE PDF</button>
  <div class="log"  id="log"></div>

</div>
<script>
const drop = document.getElementById('drop');
const fi   = document.getElementById('fileInput');
const badge = document.getElementById('badge');
const bName = document.getElementById('bName');
const bSize = document.getElementById('bSize');
const btn   = document.getElementById('btn');
const log   = document.getElementById('log');
let file = null;

const fmt = b => b < 1048576 ? (b/1024).toFixed(1)+' KB' : (b/1048576).toFixed(1)+' MB';

function setFile(f){
  if(!f) return;
  file = f;
  bName.textContent = f.name;
  bSize.textContent = fmt(f.size);
  badge.classList.add('show');
  btn.classList.add('show');
  log.classList.remove('show');
  log.innerHTML = '';
  resetSteps();
}

fi.addEventListener('change', e => setFile(e.target.files[0]));
drop.addEventListener('dragover',  e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', ()  => drop.classList.remove('over'));
drop.addEventListener('drop',      e  => { e.preventDefault(); drop.classList.remove('over'); setFile(e.dataTransfer.files[0]); });

function addLog(msg, cls=''){
  const d = document.createElement('div');
  d.className = 'ln '+cls;
  d.textContent = msg;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function setStep(n, cls){ document.getElementById('s'+n).className = 'step '+cls; }
function resetSteps(){ for(let i=1;i<=5;i++) setStep(i,''); }

async function convert(){
  if(!file) return;
  btn.disabled = true;
  log.classList.add('show');
  log.innerHTML = '';
  resetSteps();

  addLog('Uploading '+file.name+' ...');
  const fd = new FormData();
  fd.append('file', file);

  try {
    const res = await fetch('/convert', { method:'POST', body:fd });

    if(!res.ok){
      const e = await res.json();
      addLog('Error: '+(e.error||'unknown'), 'err');
      btn.disabled = false;
      return;
    }

    const stages = ['layout_parser','document_parser','normalizer','template_renderer','latex_compiler'];
    for(let i=0;i<stages.length;i++){
      setStep(i+1,'active');
      addLog('['+stages[i]+'] ...');
      await new Promise(r=>setTimeout(r,280));
      setStep(i+1,'done');
    }
    addLog('Done — downloading PDF', 'ok');

    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = file.name.replace(/\\.[^.]+$/,'')+'_ieee.pdf';
    a.click();
  } catch(err){
    addLog('Network error: '+err.message, 'err');
  }
  btn.disabled = false;
}
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400

    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()

    if ext not in config.SUPPORTED_EXTENSIONS:
        return jsonify({"error": f"Unsupported type '{ext}'. Use .pdf or .docx"}), 400

    uid = str(uuid.uuid4())[:8]
    input_path = os.path.join(config.INPUT_DIR, f"{uid}{ext}")
    f.save(input_path)

    try:
        pdf_path = run(input_path)
        return send_file(pdf_path, mimetype="application/pdf",
                         as_attachment=True, download_name=os.path.basename(pdf_path))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


if __name__ == "__main__":
    print("  IEEE Formatter → http://localhost:5000")
    app.run(debug=True, port=5000)