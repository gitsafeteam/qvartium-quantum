# Qvartium — Real quantum backend (IBM) + tier model

This turn delivers the **real-quantum** layer:
- `quantum-backend/` — a Python service (Qiskit Runtime) that runs circuits on **real IBM QPUs**.
- Netlify functions `run-quantum` + `quantum-status` — a secure proxy (keep the backend key server-side).
- The dApp's **Rent & Execute** already calls them and shows **real measurement counts** — switched on by a flag.

> Honest order: get real jobs running first, **then** turn on tokenomics. The token tiers
> (Phantom gating, discounts, SAPPHIRE) are built on top of this and go live only once real runs work.

---

## Step 1 — IBM Quantum account + token
1. Create an account at the IBM Quantum Platform and copy your **API key** (44 chars).
2. The **Open Plan is free** (~10 min of real-QPU runtime per month — enough to prove it's real / for beta).
   Beyond that it's pay-per-use (Pay-As-You-Go ≈ $96/min, billed per second) — price your rates accordingly.

## Step 2 — Deploy the Python quantum service
`quantum-backend/` can't run on Netlify (it's Python). Deploy on **Render / Railway / Fly**:
- Render: New → Web Service → upload the folder (or connect a repo) → it reads `requirements.txt` + `Procfile`.
- Set environment variables on that service:
  ```
  IBM_QUANTUM_TOKEN = <your IBM API key>        (SECRET)
  QV_BACKEND_KEY    = <make up a long random string>   (SECRET; shared with Netlify below)
  IBM_INSTANCE      = <CRN, only if your IBM account requires one>   (optional)
  PUBLIC_URL        = https://app.qvartium.xyz
  ```
- After deploy you get a URL like `https://qvartium-quantum.onrender.com`. Test:
  `GET <url>/health` (send header `x-qv-key: <QV_BACKEND_KEY>`) → should list real backends.

> Qiskit's API moves fast. `main.py` targets qiskit ≥ 1.0 + qiskit-ibm-runtime ≥ 0.30 (SamplerV2).
> If your versions differ, adjust the service init / result parsing per https://quantum.cloud.ibm.com/docs

## Step 3 — Point Netlify at the service
In the `app.qvartium.xyz` Netlify project → Environment variables, add:
```
QV_QUANTUM_URL = https://<your-render-url>     (the Python service)
QV_BACKEND_KEY = <same long random string as Step 2>
```
(`run-quantum` / `quantum-status` are already in this deploy's functions.)

## Step 4 — Flip the switch
In `index.html` find:
```js
window.QV_REAL_QUANTUM = ... : false;
```
set it to `true` and redeploy. Now **Rent & Execute** submits real circuits and the Job History
polls for **real** counts (noisy — that's authentic NISQ hardware).

## Step 5 — Map QPU names to real devices (honesty)
The fictional `QPU-GARNET-20PQ` / `QPU-EMERALD-54PQ` (with invented fidelities) must reflect **real**
IBM devices. Either show the real device names IBM returns (the backend already returns e.g. `ibm_brisbane`),
or keep brand names but display the real specs ("powered by IBM Heron r2", real qubit counts). Don't keep made-up fidelities.

---

## Tier model (build next, on top of real hardware)
Once real runs work, this is the token-utility layer (Phantom connect → read $QVA → tier):

| Hold $QVA | Discount on compute | Unlocks |
|-----------|--------------------|---------|
| 50,000    | −10% | — |
| 150,000   | −25% | — |
| 300,000   | −50% | — |
| 1,000,000 | −50% | **SAPPHIRE-128PQ** (premium device, ≈ $1.45/s base) |

- Phantom wallet connect → read the wallet's $QVA SPL balance → resolve tier → apply discount to the rate and unlock SAPPHIRE.
- Staking variant: lock $QVA for the same tiers (reduces sell pressure). Keep it **utility** (discount/priority/access), not yield, to avoid securities issues. **No buybacks.**
- All of this is gated behind `QV_REAL_QUANTUM` so it only ever goes live with real hardware.

When `/health` shows real backends and a real job completes, say the word and I'll build the full tier UI + Phantom gating + SAPPHIRE.
