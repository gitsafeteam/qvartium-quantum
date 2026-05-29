"""
Qvartium — REAL quantum execution backend (IBM Quantum / Qiskit Runtime).

The dApp calls this service DIRECTLY (no Netlify proxy) so the browser can wait
out cold starts / IBM init without hitting a serverless timeout.

Auth (either works):
  - Authorization: Bearer <Privy access token>   (verified against your Privy app)
  - x-qv-key: <QV_BACKEND_KEY>                    (shared-secret, e.g. for a proxy)

Endpoints:
  POST /submit   {gates, qubits, shots}  -> {job_id, backend}
  GET  /status/{job_id}                  -> {status, counts?, backend}
  GET  /health                           -> {ok, backends}

Env:
  IBM_QUANTUM_TOKEN  (secret) — your IBM API key
  PRIVY_APP_ID       — your Privy app id (public) e.g. cmppnoqxk00u10cl2r201hqhq
  QV_BACKEND_KEY     (optional) — shared secret fallback
  PUBLIC_URL         — https://app.qvartium.xyz (for CORS)
  IBM_INSTANCE       (optional) — CRN if your account needs it

Targets qiskit >= 1.0 (works on 2.x) + qiskit-ibm-runtime >= 0.30 (SamplerV2).
"""
import os, time
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import jwt
from jwt import PyJWKClient
from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

app = FastAPI(title="Qvartium Quantum Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("PUBLIC_URL", "*"), "*"],
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- auth ----------
PRIVY_APP_ID = os.environ.get("PRIVY_APP_ID", "").strip()
_jwks = PyJWKClient(f"https://auth.privy.io/api/v1/apps/{PRIVY_APP_ID}/jwks.json") if PRIVY_APP_ID else None

def authorized(authorization: str, x_qv_key: str) -> bool:
    expected = os.environ.get("QV_BACKEND_KEY")
    if expected and x_qv_key and x_qv_key == expected:
        return True
    if not PRIVY_APP_ID or _jwks is None:
        print("[auth] PRIVY_APP_ID not set \u2014 cannot verify Privy token")
        return not expected  # allow only if no auth is configured at all (dev)
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token:
        print("[auth] missing Bearer token")
        return False
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["ES256"], audience=PRIVY_APP_ID, leeway=30)
        return True
    except Exception as ex:
        print(f"[auth] Privy verify failed: {type(ex).__name__}: {ex}")
        return False

def guard(authorization, x_qv_key):
    if not authorized(authorization, x_qv_key):
        raise HTTPException(status_code=401, detail="unauthorized")

# ---------- IBM service (cached) ----------
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

_backend_cache = {"b": None, "ts": 0.0}
def pick_backend(min_qubits):
    now = time.time()
    b = _backend_cache["b"]
    if b is not None and (now - _backend_cache["ts"]) < 300 and b.num_qubits >= min_qubits:
        return b
    b = service().least_busy(operational=True, simulator=False, min_num_qubits=max(2, min_qubits))
    _backend_cache.update(b=b, ts=now)
    return b

JOBS = {}  # job_id -> {"backend": name}

class CircuitReq(BaseModel):
    gates: list
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
    if measured:
        qc.measure(range(n), range(n))
    else:
        qc.measure_all()
    return qc

@app.get("/health")
def health(authorization: str = Header(default=""), x_qv_key: str = Header(default="")):
    guard(authorization, x_qv_key)
    try:
        names = [b.name for b in service().backends(operational=True, simulator=False)]
        return {"ok": True, "backends": names}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/submit")
def submit(req: CircuitReq, authorization: str = Header(default=""), x_qv_key: str = Header(default="")):
    guard(authorization, x_qv_key)
    qc = build_circuit(req)
    try:
        backend = pick_backend(req.qubits)
        tqc = transpile(qc, backend=backend, optimization_level=1)
        sampler = Sampler(mode=backend)
        job = sampler.run([tqc], shots=int(req.shots))
        jid = job.job_id()
        JOBS[jid] = {"backend": backend.name}
        return {"job_id": jid, "backend": backend.name}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"submit_failed: {e}")

@app.get("/status/{job_id}")
def status(job_id: str, authorization: str = Header(default=""), x_qv_key: str = Header(default="")):
    guard(authorization, x_qv_key)
    try:
        job = service().job(job_id)
        st = str(job.status())
        if "DONE" in st:
            res = job.result()
            data = res[0].data
            reg = "meas" if hasattr(data, "meas") else next(iter(vars(data)))
            counts = getattr(data, reg).get_counts()
            return {"status": "COMPLETED", "counts": counts, "backend": JOBS.get(job_id, {}).get("backend")}
        if "ERROR" in st or "CANCEL" in st:
            return {"status": "FAILED", "detail": st}
        return {"status": "RUNNING"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"status_failed: {e}")
