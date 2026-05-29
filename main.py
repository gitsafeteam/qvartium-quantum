"""
Qvartium — REAL quantum execution backend (IBM Quantum / Qiskit Runtime).

The dApp calls this service DIRECTLY (no Netlify proxy) so the browser can wait
out cold starts / IBM init without hitting a serverless timeout.

Auth (either works):
  - Authorization: Bearer <Privy access token>   (verified against your Privy app)
  - x-qv-key: <QV_BACKEND_KEY>                    (shared-secret fallback)

Endpoints:
  GET  /health                           -> {ok, instance_set, backends?}   (PUBLIC, for testing)
  POST /submit   {gates, qubits, shots}  -> {job_id, backend}
  GET  /status/{job_id}                  -> {status, counts?, backend}

Env:
  IBM_QUANTUM_TOKEN  (secret)  — your 44-char IBM API key
  IBM_INSTANCE                 — your instance CRN (or name). STRONGLY recommended:
                                  without it Qiskit scans ALL instances, hits invalid
                                  CRNs and hangs (-> 502). Copy from quantum.cloud.ibm.com
                                  -> Instances -> copy icon.
  PRIVY_APP_ID                 — your Privy app id (public), e.g. cmppnoqxk00u10cl2r201hqhq
  QV_BACKEND_KEY    (optional) — shared secret fallback
  PUBLIC_URL                   — https://app.qvartium.xyz (for CORS)
  IBM_CHANNEL       (optional) — defaults to ibm_quantum_platform

Targets qiskit >= 1.0 (works on 2.x) + qiskit-ibm-runtime >= 0.30 (SamplerV2).
"""
import os, time, logging
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import jwt
from jwt import PyJWKClient
from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qvartium")

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
        log.warning("[auth] PRIVY_APP_ID not set \u2014 cannot verify Privy token")
        return not expected
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token:
        log.warning("[auth] missing Bearer token")
        return False
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["ES256"], audience=PRIVY_APP_ID, leeway=30)
        return True
    except Exception as ex:
        log.warning("[auth] Privy verify failed: %s: %s", type(ex).__name__, ex)
        return False

def guard(authorization, x_qv_key):
    if not authorized(authorization, x_qv_key):
        raise HTTPException(status_code=401, detail="unauthorized")

# ---------- IBM service (cached) ----------
IBM_INSTANCE = os.environ.get("IBM_INSTANCE", "").strip()
_service = None
def service():
    global _service
    if _service is None:
        kw = dict(
            channel=os.environ.get("IBM_CHANNEL", "ibm_quantum_platform"),
            token=os.environ["IBM_QUANTUM_TOKEN"],
        )
        if IBM_INSTANCE:
            kw["instance"] = IBM_INSTANCE
            log.info("[ibm] init with instance=%s", IBM_INSTANCE)
        else:
            log.warning("[ibm] IBM_INSTANCE NOT set \u2014 scanning all instances (slow / may hang on invalid CRN). "
                        "Set IBM_INSTANCE to your CRN from quantum.cloud.ibm.com -> Instances.")
        _service = QiskitRuntimeService(**kw)
    return _service

_backend_cache = {"b": None, "ts": 0.0}
def pick_backend(min_qubits):
    now = time.time()
    b = _backend_cache["b"]
    if b is not None and (now - _backend_cache["ts"]) < 300 and b.num_qubits >= min_qubits:
        return b
    b = service().least_busy(operational=True, simulator=False, min_num_qubits=max(2, min_qubits))
    _backend_cache.update(b=b, ts=now)
    log.info("[ibm] picked backend=%s (%dq)", b.name, b.num_qubits)
    return b

JOBS = {}  # job_id -> {"backend": name}

class CircuitReq(BaseModel):
    gates: list
    qubits: int
    shots: int = 1024

def build_circuit(req: CircuitReq) -> QuantumCircuit:
    n = max(1, min(int(req.qubits), 20))
    qc = QuantumCircuit(n)
    one = {"h": "h", "x": "x", "y": "y", "z": "z", "s": "s", "t": "t"}
    for g in req.gates:
        op = str(g.get("op", "")).lower()
        try:
            if op in one:
                getattr(qc, one[op])(int(g["q"]) % n)
            elif op in ("cx", "cnot"):
                qc.cx(int(g["c"]) % n, int(g["t"]) % n)
            elif op == "cz":
                qc.cz(int(g["c"]) % n, int(g["t"]) % n)
            elif op in ("rx", "ry", "rz"):
                getattr(qc, op)(float(g.get("theta", 1.5707963)), int(g["q"]) % n)
            # "measure" handled by measure_all() below
        except Exception:
            pass
    qc.measure_all()
    return qc

def extract_counts(res):
    data = res[0].data
    for name in ("meas", "c", "c0"):
        v = getattr(data, name, None)
        if v is not None and hasattr(v, "get_counts"):
            return v.get_counts()
    for name in dir(data):
        if name.startswith("_"):
            continue
        v = getattr(data, name, None)
        if hasattr(v, "get_counts"):
            return v.get_counts()
    return {}

@app.get("/health")
def health():
    """Public, INSTANT — shows config only (never calls IBM, so it can't hang)."""
    return {
        "ok": True,
        "token_set": bool(os.environ.get("IBM_QUANTUM_TOKEN")),
        "instance_set": bool(IBM_INSTANCE),
        "instance_preview": (IBM_INSTANCE[:48] + "\u2026") if IBM_INSTANCE else None,
        "privy_set": bool(PRIVY_APP_ID),
        "hint": "open /backends to test the IBM connection (can take ~30s; needs IBM_INSTANCE set)",
    }

@app.get("/backends")
def list_backends():
    """Public — actually contacts IBM and lists real devices. Slow if IBM_INSTANCE is missing/invalid."""
    if not os.environ.get("IBM_QUANTUM_TOKEN"):
        return {"ok": False, "error": "IBM_QUANTUM_TOKEN missing"}
    try:
        names = [b.name for b in service().backends(operational=True, simulator=False)]
        return {"ok": True, "instance_set": bool(IBM_INSTANCE), "backends": names}
    except Exception as e:
        return {"ok": False, "instance_set": bool(IBM_INSTANCE), "error": f"{type(e).__name__}: {e}"}

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
        log.info("[submit] job=%s backend=%s", jid, backend.name)
        return {"job_id": jid, "backend": backend.name}
    except Exception as e:
        log.exception("[submit] failed")
        raise HTTPException(status_code=502, detail=f"submit_failed: {type(e).__name__}: {e}")

@app.get("/status/{job_id}")
def status(job_id: str, authorization: str = Header(default=""), x_qv_key: str = Header(default="")):
    guard(authorization, x_qv_key)
    try:
        job = service().job(job_id)
        st = str(job.status())
        if "DONE" in st or "COMPLETED" in st:
            return {"status": "COMPLETED", "counts": extract_counts(job.result()),
                    "backend": JOBS.get(job_id, {}).get("backend")}
        if "ERROR" in st or "CANCEL" in st or "FAIL" in st:
            return {"status": "FAILED", "detail": st}
        return {"status": "RUNNING"}
    except Exception as e:
        log.exception("[status] failed")
        raise HTTPException(status_code=502, detail=f"status_failed: {type(e).__name__}: {e}")
