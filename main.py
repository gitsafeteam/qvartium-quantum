"""
Qvartium — REAL quantum execution backend (IBM Quantum / Qiskit Runtime).

This is a small async service: the dApp submits a circuit (as a gate list),
this service runs it on a REAL IBM QPU and returns real measurement counts.

It is intentionally separate from the Netlify (Node) functions because
qiskit-ibm-runtime is Python. Deploy it on a Python host (Render / Railway / Fly).

Endpoints:
  POST /submit   {gates, qubits, shots}  -> {job_id, backend}
  GET  /status/{job_id}                  -> {status, counts?, backend}
  GET  /health                           -> {ok, backends}

Auth: set a shared secret QV_BACKEND_KEY; the dApp/Netlify sends it as
header `x-qv-key`. The IBM token lives only here (IBM_QUANTUM_TOKEN), never in the frontend.

NOTE: Qiskit's API evolves. This targets qiskit >= 1.0 and
qiskit-ibm-runtime >= 0.30 (SamplerV2). If your versions differ, adjust the
service init / result parsing per IBM's current docs:
https://quantum.cloud.ibm.com/docs
"""
import os, uuid
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

app = FastAPI(title="Qvartium Quantum Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("PUBLIC_URL", "*")],
    allow_methods=["*"], allow_headers=["*"],
)

# --- IBM Quantum service (token from env; never hardcode) ---
# The newest IBM Quantum Platform uses channel="ibm_quantum_platform".
# If your account needs an instance, pass instance="<CRN>".
_service = None
def service():
    global _service
    if _service is None:
        _service = QiskitRuntimeService(
            channel=os.environ.get("IBM_CHANNEL", "ibm_quantum_platform"),
            token=os.environ["IBM_QUANTUM_TOKEN"],
            instance=os.environ.get("IBM_INSTANCE") or None,
        )
    return _service

JOBS = {}  # job_id -> {"backend": name}

class CircuitReq(BaseModel):
    gates: list   # e.g. [{"op":"h","q":0},{"op":"cx","c":0,"t":1},{"op":"measure"}]
    qubits: int
    shots: int = 1024

def build_circuit(req: CircuitReq) -> QuantumCircuit:
    n = max(1, int(req.qubits))
    qc = QuantumCircuit(n, n)
    one = {"h": "h", "x": "x", "y": "y", "z": "z", "s": "s", "t": "t"}
    measured = False
    for g in req.gates:
        op = str(g.get("op", "")).lower()
        if op in one:
            getattr(qc, one[op])(int(g["q"]))
        elif op in ("cx", "cnot"):
            qc.cx(int(g["c"]), int(g["t"]))
        elif op == "cz":
            qc.cz(int(g["c"]), int(g["t"]))
        elif op in ("rx", "ry", "rz"):
            getattr(qc, op)(float(g.get("theta", 1.5707963)), int(g["q"]))
        elif op == "measure":
            measured = True
    qc.measure_all() if not measured else qc.measure(range(n), range(n))
    return qc

def _check(key):
    expected = os.environ.get("QV_BACKEND_KEY")
    if expected and key != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

@app.get("/health")
def health(x_qv_key: str = Header(default="")):
    _check(x_qv_key)
    try:
        names = [b.name for b in service().backends(operational=True, simulator=False)]
        return {"ok": True, "backends": names}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/submit")
def submit(req: CircuitReq, x_qv_key: str = Header(default="")):
    _check(x_qv_key)
    qc = build_circuit(req)
    try:
        backend = service().least_busy(operational=True, simulator=False,
                                       min_num_qubits=max(2, req.qubits))
        tqc = transpile(qc, backend=backend, optimization_level=1)
        sampler = Sampler(mode=backend)
        job = sampler.run([tqc], shots=int(req.shots))
        jid = job.job_id()
        JOBS[jid] = {"backend": backend.name}
        return {"job_id": jid, "backend": backend.name}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"submit_failed: {e}")

@app.get("/status/{job_id}")
def status(job_id: str, x_qv_key: str = Header(default="")):
    _check(x_qv_key)
    try:
        job = service().job(job_id)
        st = str(job.status())
        if "DONE" in st or st == "JobStatus.DONE":
            res = job.result()
            # SamplerV2: counts live under the measured register (measure_all -> 'meas')
            data = res[0].data
            reg = "meas" if hasattr(data, "meas") else next(iter(vars(data)))
            counts = getattr(data, reg).get_counts()
            return {"status": "COMPLETED", "counts": counts,
                    "backend": JOBS.get(job_id, {}).get("backend")}
        if "ERROR" in st or "CANCEL" in st:
            return {"status": "FAILED", "detail": st}
        return {"status": "RUNNING"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"status_failed: {e}")
