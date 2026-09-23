# LBM Solver Selection Study — OpenLB vs HARVEY vs FluidX3D (Track 4.4)

**Status:** Evaluation only (desk research). No solver was installed, built, or executed for this study; the host has no GPU.
**As-of date for all web-sourced facts:** 2026-09-23 (each source in §9 lists the URL; facts are as of that access date unless another date is stated).
**Local measurements** (lattice sizes, §6.2) were taken from the repo's own data on 2026-09-23 with `.venv/bin/python` + nibabel/numpy and are labeled "measured".
**Method:** vendor/author documentation, source repositories, and peer-reviewed publications only; performance figures are literature- or vendor-reported and are explicitly labeled as such. Cross-solver performance numbers are **not** directly comparable (different cases, years, and measurement methods) — §6 defines the apples-to-apples trials that must replace them before any purchase/scale-up decision.

---

## 1. Phase-2 requirements used as evaluation axes

The Phase-2 use case is: **patient-specific vascular blood flow + passive-scalar contrast transport (advection–diffusion) on cloud GPUs, with live re-solve of the scalar field when injection parameters change.** Decomposed into hard requirements:

| # | Requirement | Consequence for solver choice |
|---|---|---|
| R1 | Commercial use in a BMT commercial hardware context | License must permit commercial use, or a paid license must be obtainable |
| R2 | Pulsatile blood flow in image-derived (segmented) vascular geometry | STL/mask ingestion → lattice; physiological inlet/outlet BCs (velocity waveform in; pressure/resistance out) |
| R3 | Passive-scalar advection–diffusion for contrast bolus transport | A dedicated scalar lattice (ADE) coupled one-way to the flow field |
| R4 | **Live re-solve of the scalar field when injection parameters change** (velocity field frozen) | Scalar must be re-solvable independently of the flow solve; checkpoint/restart of the velocity state; parameter changes without recompilation |
| R5 | Parallel sweeps on cloud GPUs (AWS/GCP/Azure-class accelerators) | NVIDIA CUDA at minimum; AMD HIP / Intel SYCL a plus; multi-GPU (and ideally multi-node MPI) scaling |
| R6 | Integration with the existing Python pipeline (`pipeline/build_cardiac_glb.py` outputs: GLB/STL geometry, NIfTI masks upstream) | File-based ingestion of STL or masks; VTK/VTI field output; subprocess-driven batch orchestration from Python |

LBM model axes compared per solver: collision operators (BGK/SRT, MRT), boundary conditions for in/outflow, passive-scalar and multiphase transport, plus language/build, GPU backends, cloud deployability, meshing/geometry ingestion, community activity/maintenance, published benchmark evidence, and integration cost.

---

## 2. Comparison matrix (detail in §3–§5)

| Axis | OpenLB | HARVEY | FluidX3D |
|---|---|---|---|
| License (as of 2026-09-23) | GPLv2 [1] — commercial use allowed; copyleft obligations on **distribution** | Proprietary Duke research license — free academic tier; commercial terms unpublished ("cannot release HARVEY … due to proprietary reasons") [17][18] | Source-available **non-commercial** license; commercial use explicitly prohibited without permission; military use prohibited [10][11] |
| Language / build | C++ library; GNU Make platform configs (`config/*.mk`, incl. `gpu_openmpi.mk`, `gpu_openmpi_mixed.mk`) [2][3][19] | C++ (MPI+CUDA); artifact build env: OpenMPI 4.1.4 / Spectrum MPI 10.4.0.3 / CUDA 11.4, RHEL/Amazon Linux [17] | C++17 host + runtime-compiled OpenCL C; `make.sh` (+ `make`), or Visual Studio `.sln` [11][12] |
| GPU backends | CUDA (mature), HIP/ROCm (preliminary, v1.9.0), SYCL/Intel (preliminary, 2026) [3][4] | Production MPI+CUDA; portability studies: OpenMP/OpenACC (2019), CUDA/SYCL/HIP/Kokkos (2023) [16][20] | OpenCL only — runs on NVIDIA/AMD/Intel/Apple/ARM GPUs and CPUs; no CUDA/HIP-native path [11][12] |
| Cloud-GPU deployability | Any Linux VM with CUDA/MPI toolchain; MPI multi-node proven to 512 A100s and 1,000 Aurora nodes [4] | **Proven at scale on AWS** (400× `hpc6a.48xlarge`; `p3dn.24xlarge` 8×V100 compared) — but access-gated [17] | Easiest bring-up (single binary, OpenCL ICD per vendor [12]); single-node multi-GPU only (PCIe domain decomposition, no MPI) [11] |
| LBM models | BGK, MRT, Smagorinsky (BGK/MRT), KBC, cumulant, entropic, k-ε RANS, non-Newtonian BGK/power-law; ADE + NS↔ADE coupling; Shan–Chen/free-energy/phase-field/free-surface multiphase [5][3] | D3Q19, single-relaxation-time BGK (SRT), half-way bounce-back walls [15]; no MRT documented | D2Q9/D3Q15/D3Q19/D3Q27; SRT/BGK + TRT; Smagorinsky SUBGRID; **MRT deliberately absent** [11] |
| In/outflow BCs | Zou–He (pressure & velocity), extended finite-difference/regularized, local & interpolated pressure/velocity, **convective outflow** (`interpolatedConvection`), characteristic BC, Bouzidi walls, ADE Dirichlet (scalar injection) [6][3] | Zou–He and finite-difference/regularized compared in image-derived vasculature; resistance-style outlet params in run inputs [15][17] | Equilibrium boundaries (`TYPE_E`, fixed ρ and/or u, non-reflective), moving walls; **no** Zou–He/regularized/convective/Windkessel-style outflow [11][12] |
| Passive scalar / transport | `advectionDiffusionDynamics` + `navierStokesAdvectionDiffusionCoupling` (one-way NS→ADE) + ADE Dirichlet BCs; ADR examples [5][6][8] | None documented in publications (cell transport via immersed-boundary particles exists; no passive-scalar ADE found) [17][21] | One scalar only, via `TEMPERATURE` extension (D3Q7 advection–diffusion, `TYPE_T` Dirichlet) — repurposable as passive scalar (set buoyancy 0); no second scalar [12] |
| Geometry ingestion | **STL in** (`stlReader`, `blockLatticeSTLreader`, built-in STL voxelization) + XML/CLI params + checkpointing + VTK/VTU/VTI I/O [2][7] | Image-derived pipeline (CTA/MRA segmentation → geometry); run inputs carry `geometry/` dir; exact mesh I/O not public [15][17] | **Binary STL in** (watertight, outward normals), fast GPU voxelization (`voxelize_stl`/`voxelize_mesh_on_device`); VTK out [12][11] |
| Community / maintenance (2026-09-23) | Active: 138 examples refactored in v1.9.0 (2025-12-19), repo activity 2026-09-18, ~31 authors, forum 958 topics/4,301 posts, 7 spring schools, Zenodo DOI releases [3][2][8] | Lab-internal; publications through 2024–2026; "digital twin engine" announcement 2026-03-11; no public repo [17][18][21] | Very active but **solo-maintained** (bus factor 1): v3.8 2026-09-05, repo pushed 2026-09-07, 5,289 stars/472 forks/54 open issues; PRs collaborators-only [11][13] |
| Published benchmarks | HoreKa: ~1.33 TLUPs on 512×A100 (1.5, 2022); Aurora: 21,120 GLUPs on 1,000 nodes (SYCL, 2026); Magnus: 142,479 MLUPs CPU [4] | Summit multi-GPU (4×10⁹ fluid pts, 13–18× GPU-vs-CPU) [16]; AWS cloud wall-clock vs Summit/Stampede2 [17]; A100 MLUPs second-hand via [20] | Author-maintained MLUPs table incl. all datacenter GPUs (e.g. A100-80 SXM ≈ 10.2 GLUPs FP32 / 18.4 GLUPs FP16S) with roofline efficiency; GigaIO 32×MI210 case study [11][14][22] |
| Python pipeline integration | Subprocess + files: STL/mask → lattice, XML/CLI params (v1.9 CLI overrides **without recompiling**), VTK out, `serializerIO` checkpoints [3][7] | Input-deck workflow + point-cloud outputs (55 GB per hemodynamic unit) — high glue cost, access-gated [17] | Per-setup C++ edits in `src/setup.cpp` + recompile; VTK out; **no checkpoints**; third-party PyPI wrapper exists (unofficial, provenance unverified) [11][12][23] |

---

## 3. OpenLB

### 3.1 License & commercial-use implications
- **GPL v2.0** ("The source code of the project is freely available and is distributed under the GNU General Public License 2.0") [1]; overview page confirms "Gnu General Public License V.2 (GPL2)" [2] (as of 2026-09-23).
- Commercial use is permitted; the obligations that matter for BMT are distribution-triggered (§3 of GPLv2): shipping OpenLB or a linked derivative inside a product delivered to customers requires conveying corresponding source under GPLv2-compatible terms. Running it server-side (cloud service, internal tool) is not "distribution" and does not trigger that obligation. Process/file-boundary separation of our Python pipeline from the solver reduces but does not eliminate legal review needs.
- **No published dual/commercial license found** as of 2026-09-23: the consortium page advertises support/training memberships (installation help, priority support, training, influence on roadmap), not an alternative license [9]. If the solver must ship inside BMT hardware/software, either architect it as a hosted/service component or open a commercial-license conversation with the copyright holders (KIT LBRG + authors) via the consortium.

### 3.2 Language & build system
- C++ template library ("The code is in C++ … modular and can easily be extended") [2]. Build via GNU Make platform configuration files — released tree ships `default.mk`, `config/gpu_openmpi.mk`, `config/gpu_openmpi_mixed.mk`, `config/gpu_leonardo_mixed.mk` etc. [2][19] (as of 2026-09-23).
- v1.9.0 (2025-12-19, DOI 10.5281/zenodo.17899765) tested on NixOS 24.11+ (Nix flake provided), Fedora 39, RHEL 9.4, Rocky 8.9, Windows 10/11 via WSL, macOS Tahoe; tested on HoreKa, Karolina, Leonardo, ALPS [3].

### 3.3 GPU backends
- **CUDA** is the mature GPU path (MPI+CUDA multi-GPU) [2][4].
- **HIP/ROCm**: "preliminary support for AMD GPUs … tested on AMD Instinct MI300A and AMD RX 7800 XT" in v1.9.0 [3].
- **SYCL (Intel)**: "preliminary support for Intel GPU … a new SYCL-based backend for our platform-transparent model implementations" (performance page, 2026) — "OpenLB now supports hardware acceleration across all of the big three platforms: NVIDIA, AMD, and Intel" [4]. The SYCL backend is not described in the 1.9.0 release notes [3]; the exact public snapshot carrying it is **unverified** (flag F5).
- No OpenCL backend documented [2][3].

### 3.4 Cloud-GPU deployability
- Standard Linux + CUDA/MPI (or HIP/SYCL) toolchain; no official AMI/container found (expected DIY image cost low) (as of 2026-09-23).
- Multi-node/multi-GPU evidence on hardware classes that mirror cloud SKUs: 512 NVIDIA A100 GPUs on HoreKa (2022) with published strong-scaling efficiencies (e.g. 0.64–0.81 over the largest runs) and up to 54 billion cells on 512 GPUs (Karolina, 2025); H100 partition scaling study (HoreKa Teal, 2025, preprint arXiv:2506.21804); 1,000 nodes of Aurora with Intel GPUs (2026) [4]. This maps directly onto AWS `p4d`/`p5` + EFA, GCP `a2`/`a3` + GPUDirect, Azure ND-series + InfiniBand.

### 3.5 LBM model support
- Collision operators (from released source tree `src/dynamics/`, as of 2026-09-23) [5]: `collision.h` (BGK/SRT), `mrt.h`/`mrtDynamics.h`/`collisionMRT.h` (**MRT**), `smagorinskyBGKdynamics.h`/`smagorinskyMRTdynamics.h` (LES), `kbcDynamics.h`/`collisionKBC.h`, `cumulantDynamics.h`/`collisionCUM.h`, `collisionHRR.h`, `entropicDynamics.h`, `kEpsilonRANSDynamics.h`, `stochasticSGSdynamics.h`; blood-relevant rheology headroom via `nonNewtonianBGKdynamics.h`, `powerLawBGKdynamics.h`, `guoZhaoDynamics.h` (non-Newtonian is *not* required by R3 but is the standard small-vessel caveat [15]).
- **Passive scalar / contrast transport** [5][6][8]: `advectionDiffusionDynamics.h`, `navierStokesAdvectionDiffusionCoupling{PostProcessor2D,3D}.h` (one-way Navier–Stokes → advection–diffusion coupling — exactly the frozen-velocity structure R4 needs), `advectionDiffusionReactionCouplingPostProcessor{2D,3D}.h`; scalar inlet injection via `advectionDiffusionDirichlet{,2D,3D}.h`; worked cases under `examples/advectionDiffusionReaction/` (`advectionDiffusion3d`, `longitudinalMixing3d`, `convectedPlate3d`, `laminarReactiveTmixer`, …) and `examples/thermal/` [8].
- **Multiphase** [3][5]: Shan–Chen forced couplings (single/multi-component), free-energy (`freeEnergyDynamics`, `equationsOfState.h`), phase-field (`phaseFieldCoupling`, `phaseFieldInletOutlet`), free-surface post-processors; v1.9.0 adds "physically parameterized and well-balanced multi phase" and cell-centered grid refinement.
- **Boundary conditions** (`src/boundary/`, as of 2026-09-23) [6]: walls — `bounceBack`, Bouzidi interpolated (`bouzidiFields.h`, `setBouzidiBoundary.h`), `slip`/`partialSlip`; in/outflow — `zouHePressure`/`zouHeVelocity`/`zouHeDynamics` (Zou–He), `extendedFiniteDifferenceBoundary{2D,3D}` (finite-difference/regularized family), `localPressure`/`localVelocity`, `interpolatedPressure`/`interpolatedVelocity`, **`interpolatedConvection` (convective outflow)**, `characteristicBoundary.h` (new in 1.9.0 with damping for acoustics [3]), `setZeroGradientBoundary3D`, `vortexMethod`; scalar/thermal — `advectionDiffusionDirichlet`, `regularizedTemperature`, `regularizedHeatFlux`, `robin`. This covers both BC families validated for image-derived vasculature (Zou–He and FD/regularized [15]) plus the convective/resistance outflow styles used in hemodynamics.

### 3.6 Meshing & geometry ingestion
- "Automated preprocessing with built-in voxelization from stl-files and setting of boundary conditions" [2]; source: `src/io/stlReader.h`, `src/io/blockLatticeSTLreader.h` (STL → lattice flags) [7].
- Supporting I/O for our pipeline loop [7][2]: `xmlReader.h` (XML input parameters), `cliReader.h` (CLI parameter overrides), `serializerIO.h` (checkpoint serialization), VTK/VTU/VTI readers/writers (field output consumable by ParaView/Python), "full serial and parallel checkpointing" [2].

### 3.7 Community & maintenance
- Release cadence: 1.6 (2023-06) → 1.7 (2024-06) → 1.8 (2025-08) → 1.9.0 (2025-12-19); repo `openlb/release` last activity 2026-09-18, 19 stars / 49 forks (GitLab API, 2026-09-23) [3][8].
- Project-reported traction (Nov 2024) [2]: 6,390+ Zenodo downloads (1.6/1.7), forum 958+ topics / 4,301+ posts, 7 spring schools (~350 attendees), 1.9M+ site visitors. ~31 named authors in the 1.9.0 citation [3] — low bus factor.

### 3.8 Published benchmark evidence
- HoreKa (2022, OpenLB 1.5): single-precision D3Q19 + BGK + Periodic Shift streaming, lid-driven cavity; up to 512 A100 GPUs; **~1.33 TLUPs** maximum total; a turbulent nozzle (LES + interpolated boundaries) reached 92% of the lid-driven-cavity reference on 224 A100s; strong-scaling efficiencies published per problem size [4]. (≈2.6 GLUPs/GPU derived from the reported totals — derived figure, not vendor-quoted.)
- Aurora (2026, SYCL): 4×10¹² single-precision D3Q19 cells on 1,000 nodes (~10% of Aurora); **21,120 GLUPs** peak [4].
- Magnus CPU (2018): 142,479 MLUPs on 32,784 cores [4].
- All figures are project-reported scaling studies, not independent audits; different vintages/BC treatments than the FluidX3D table — see §6 for the fair comparison protocol.

### 3.9 Integration cost with our Python pipeline (low–moderate)
- `pipeline/build_cardiac_glb.py` consumes NIfTI masks (ImageCAS `label.nii.gz`, TotalSegmentator 104-label masks) and emits viewer-GLB geometry at a 120k-triangle budget (window 100k–150k), Taubin-smoothed and decimated, with a world transform RAS-mm → Y-up-meters and quantization. **None of that display geometry is suitable for physics as-is**: the decimation budget under-resolves vessel cross-sections and quantization perturbs coordinates.
- Cheapest correct path: export **pre-decimation** meshes to STL via trimesh (already in `.venv`) — or, better, voxelize the **binary NIfTI masks directly** into OpenLB lattice geometry (all three solvers ultimately work on voxel flags; masks skip the mesh round-trip entirely). OpenLB ingests STL directly (`blockLatticeSTLreader`) when meshes are preferred [7].
- Orchestration: Python generates case parameter files (XML/CLI; v1.9 CLI overrides mean **no recompilation for parameter sweeps** [3]), launches the C++ case binary via `subprocess` (single node or `mpirun` across cloud nodes), and reads VTK/VTI + checkpoint files back. The pipeline's report JSON fields (`watertight`, `volume_final_mm3`, `volume_drift_pct`) serve as geometry-QA gates before physics runs.
- The R4 live-re-solve loop fits cleanly: solve flow once → `serializerIO` checkpoint of the velocity state → for each injection variant, restart the ADE lattice with new `advectionDiffusionDirichlet` inlet parameters (CLI-overridable) and march only the scalar.

---

## 4. HARVEY (Randles Lab, Duke University)

### 4.1 License & commercial-use implications
- **Not open source; distributed under a proprietary Duke license.** The SC'23 artifact appendix states: "we … evaluated LHMF and a modified version, LHMFC, using the 3D hemodynamics solver HARVEY, but **cannot release HARVEY or our artifacts due to proprietary reasons**" [17]. A companion SC'23 paper's artifact appendix is more specific (verbatim): "HARVEY is generally available under a proprietary research license from Duke University. This license has a provision for a free license for academic use. For access, contact the Duke Office of Licensing and Ventures." [18]
- **Commercial terms are not published:** a research license exists (free academic tier), but any commercial/production use would be a negotiated Duke license whose pricing, field-of-use (including any military/defense restrictions), and support obligations are unverifiable from public sources (flag F1). A 2026-03-11 Duke announcement positions HARVEY as a "high-fidelity cardiovascular digital twin engine" [21], suggesting an active commercialization track.
- For BMT: a commercial license is plausible to negotiate but unverifiable today; nothing can be assumed about field-of-use restrictions (defense/military clauses unknown), support terms, or escrow.

### 4.2 Language & build system
- C++ with MPI+CUDA; SC'23 artifact description gives the build environment: "Program: C++, Python 3.7.16; Compilation: OpenMPI 4.1.4 · Spectrum MPI 10.4.0.3 · CUDA 11.4" on Amazon Linux 2 / RHEL 8.2 / CentOS 7 / Stream 8 [17]. Python used for post-processing scripts. Build system, dependency management, and case layout are internal (only a `testrun` input-deck bundle is described: `building_blocks/` inlet waveform, `geometry/`, `input`, `resistance/` outflow parameters, `slice/`, `timesteps/`, `runscript/`) [17].

### 4.3 GPU backends
- Production: MPI + CUDA ("memory bandwidth-limited stencil code … performant on CPU-only and CPU-GPU architectures"; "scale linearly on up to 1.5 million CPUs and 3 thousand GPUs" [17]).
- Portability research around the code: MPI+OpenMP/MPI+OpenACC/CUDA and other architectures (Lee, Gounley, Randles & Vetter, J. Parallel Distrib. Comput. 2019 [29]); Martin et al. (SC-W 2023) benchmark CUDA/SYCL/HIP/Kokkos implementations of a hemodynamics proxy derived from this workload [20]. Whether any non-CUDA path exists in the production code today is **unverified** (flag F2). (An ALCF AR24 page reportedly covers HARVEY on Intel GPUs/Aurora; it was unreachable from this host due to TLS failure and is treated as unverified, flag F8.)

### 4.4 Cloud-GPU deployability
- **Best cloud pedigree of the three — but access-gated.** SC'23 deployed on AWS at scale [17]: production on 400 × `hpc6a.48xlarge` (2× AMD EPYC 7R13, EFA 100 Gbps) = 38,400 vCPUs for high-throughput sweep of 680 hemodynamic units (avg 2.45 h per unit on 6 nodes); comparison runs on `c7g.16xlarge` (Graviton3) and `p3dn.24xlarge` (8× V100-SXM2); baselines on OLCF Summit and TACC Stampede2; conclusion "cloud resources produced non-inferior wallclock time to traditional clusters". Inputs ≈ 50 MB/unit, outputs ≈ 55 GB/unit (point clouds) [17].
- Multi-GPU: immersed-boundary hemodynamics on Summit (6× V100/node) up to 4×10⁹ fluid points and 17M red blood cells; GPU 13–18× CPU; ~70% weak scaling at the largest node count; communication/transfers approached half of runtime in some cases [16].
- Cell-scale variant (APR, hybrid CPU-GPU): 256 Summit nodes (1.5k GPUs / 10.8k CPUs, IBM XL + Spectrum MPI + CUDA 11); a cerebral-metastasis case ran "using just hundreds of node-hours on a single-node AWS cloud instance" [18].

### 4.5 LBM model support
- **D3Q19, uniform Cartesian grid, single-relaxation-time BGK (SRT), half-way bounce-back at vessel walls** [15], independently confirmed in [18]. No MRT documented in the accessible literature (flag F2).
- **Boundary conditions** (the domain's reference study for exactly our problem class, run on HARVEY [15]): Zou–He (ZH) and finite-difference/regularized (FD) inlet/outlet conditions with Poiseuille/Womersley velocity inlets and pressure/density outlets were compared in four image-derived arteries (coarctation Aorta, dissected aorta, femoral, left coronary). FD was more stable and faster at equal physiological time; ZH slightly more accurate vs 3D-printed PIV but needed larger τ, resolution, and step counts. A parallel inlet-velocity-profile algorithm for irregular inlets is described. Blood modeled Newtonian (μ = 4 cP, ρ = 1060 kg/m³) with the explicit note that small vessels need Carreau–Yasuda/power-law non-Newtonian models. The run-input layout includes `resistance/` outflow (resistance-style outlet coupling) [17].
- **Passive scalar / contrast transport: none documented.** Targeted searches found no Randles-lab publication describing an ADE/contrast-agent module coupled to HARVEY; the lab's transport work uses discrete-cell immersed-boundary machinery (deformable red cells, cancer cells) [18]. Building R3/R4 would be co-development with the lab — scope, timeline, and IP ownership unverifiable (flag F3).

### 4.6 Meshing & geometry ingestion
- Image-derived workflow: CTA/MRA segmentation (e.g. Materialise Mimics in [15]) → computational lattice; SC'23 inputs carry a patient-specific coronary `geometry/` file; visualization projects wall shear stress onto "stereolithography vertices" [17]. The precise mesh/voxel input formats and preprocessing tools are not public (flag F2); whether binary STL is a supported input is **unverified**.

### 4.7 Community & maintenance
- No public repository or issue tracker; maintenance is lab-internal. Publication record is continuous: ICCS 2015 → JPDC 2019 → J. Comput. Sci 2020 → SC'23 → npj Digital Medicine 2024 (longitudinal hemodynamic mapping framework) [28][16][17][18][20][21]. The 2026-03-11 "digital twin engine" announcement [21] indicates active external positioning. Effective "community" for a BMT integration = a support contract with Duke/Randles lab (terms unverifiable, flag F1).

### 4.8 Published benchmark evidence
- Randles et al., ICCS 2015: full-body arterial simulations at up to 1.57M Blue Gene/Q cores, resolutions to 10 μm [28].
- Ames et al., J. Comput. Sci. 44:101153 (2020): multi-GPU Summit results as in §4.4 [16].
- Martin et al., SC-W 2023: framework comparison (CUDA/SYCL/HIP/Kokkos) for hemodynamics proxies [20]; MLUPs figures sometimes quoted for HARVEY-class runs on A100s (≈3,000 MLUPs/GPU for a 4-GPU proxy; ≈625 MLUPs/GPU at 64×A100 on an artery geometry; ≈585 MLUPs/GPU at 1,024×A100) are **second-hand quotations** via Suffa et al. 2026 [20][24] — treat as indicative only (flag F6).
- SC'23 cloud wall-clock/strong-scaling results (near-linear to 6 nodes of `hpc6a.48xlarge`) [17].

### 4.9 Integration cost with our Python pipeline (high, and gated)
- Even after licensing, integration is opaque: input-deck bundle conventions are internal; outputs are large point clouds (55 GB/unit [17]) requiring custom Python post-processing; no documented VTK/STL/checkpoint interfaces (flag F2). Geometry prep would likely follow their image-derived route (masks → their preprocessing), which is favorable to our NIfTI-based pipeline but requires their tooling. The R4 live scalar re-solve would require lab-side development (no scalar module exists). Cost estimate cannot be bounded without vendor access (flag F3).

---

## 5. FluidX3D (ProjectPhysX / Dr. Moritz Lehmann)

### 5.1 License & commercial-use implications
- Custom license, "source-available no-cost non-commercial", (c) 2022–2026 Dr. Moritz Lehmann [10]. Key clauses as of 2026-09-23:
  - Free for "public research, education or personal use"; alterations/redistribution allowed non-commercially with attribution.
  - **"Commercial use is not allowed."** Includes selling, and providing third parties "for a fee or other consideration (including without limitation fees for hosting or consulting/support services related to the software)[] a product or service whose value derives from the functionality of this software" — "unless explicit permission is granted to you by the copyright owner" [10].
  - **"Military use is not allowed"** (military research, defense industry purposes, military institutions) [10].
  - No AI training on the source; publishing binaries/results of altered source versions requires publishing the altered source [10].
- README FAQ is explicit: "I work at a company in CFD/consulting/R&D … Can I use FluidX3D commercially? **No.**" A second (commercial) license "may" be added later; interested parties are asked to contact the author (dr.moritz.lehmann@gmail.com) [11].
- **BMT implications:** BMT is a commercial hardware context — the standard license does not cover it, and the hosting/consulting clause reaches a BMT-hosted solve service too. The military/defense clause (3) is an absolute bar if any part of the intended BMT deployment touches military/defense purposes — this needs explicit legal review of BMT's field of use (flag F4). Without written permission/a purchased license from Dr. Lehmann, FluidX3D cannot be used for this project. Note also the altered-source publication clause (5) for any kernel work (e.g. adding a decoupled scalar re-solve).

### 5.2 Language & build system
- C++17 host code with OpenCL C kernels compiled at runtime (bundled OpenCL-Wrapper + Khronos OpenCL headers) [11][12]; build = `./make.sh` (uses `make` for parallel compile when present; ~5 s compile) on Linux/macOS/Android, or `FluidX3D.sln` in Visual Studio on Windows [12].
- Case configuration is done by editing `src/setup.cpp` and recompiling; extensions toggled in `src/defines.hpp` [12]. No config-file/CLI physics interface.

### 5.3 GPU backends
- **OpenCL only** ("runs on all GPUs and CPUs via OpenCL") — NVIDIA/AMD/Intel/Apple/ARM GPUs and CPUs (CPU via Intel CPU Runtime for OpenCL or PoCL); the author documents per-vendor driver/OpenCL-ICD installation (including headless Linux recipes for AMD/ROCm, Intel compute-runtime, NVIDIA) [11][12]. No CUDA- or HIP-native backend; the author argues OpenCL matches CUDA efficiency on NVIDIA GPUs (roofline analysis in the PRE 2022 paper) [11][25].
- Runtime kernel compilation means one binary serves all vendors — attractive for heterogeneous cloud sweeps (incl. AMD MI300X on Azure, where a HIP build of other solvers may lag).

### 5.4 Cloud-GPU deployability
- Best bring-up story: single binary + vendor OpenCL ICD; documented driver recipes for exactly the headless-Linux case [12]; headless rendering in OpenCL (no display hardware needed — A100/MI200-class covered) and ASCII terminal visualization for SSH-only hosts [11].
- **Single-node multi-GPU only** (cross-vendor domain decomposition over PCIe; "All GPUs must however be installed in the same node … no MPI installation required") [11][12]. Grid size is bounded by node VRAM/RAM pool (FP16 storage ≈ 55 B/cell ⇒ ≈19M cells/GB; 80 GB ⇒ ≈1150³ max cubic) [11]; no multi-node scaling exists, so >1-node T3-tier problems must be split by parameter sweeps (embarrassingly parallel) rather than domain-decomposed.
- The vendor benchmark table covers datacenter SKUs one-for-one with AWS/GCP/Azure offerings (T4, L4, A10, A100, V100, H100, H200, B200, MI210/250/300X, GPU Max 1100) [11], and a GigaIO composable-infrastructure case study exists for 32× MI210 [22].

### 5.5 LBM model support
- Velocity sets D2Q9/D3Q15/D3Q19 (default)/D3Q27; collision operators **SRT/BGK (default) and TRT**; the FAQ states MRT is deliberately **not** implemented and considered not worthwhile [11]. Smagorinsky–Lilly SUBGRID extension (no performance cost) [12][11].
- **Boundary conditions** [12][11]: periodic; stationary and moving mid-grid bounce-back solids (`TYPE_S`); **equilibrium boundaries** (`TYPE_E`, non-reflective inflow/outflow enforcing fixed ρ and/or u); temperature boundaries (`TYPE_T`); volume force (Guo) for pressure-gradient-driven flow. No Zou–He, regularized, convective, or Windkessel-style outflow — for arterial outflow the equilibrium boundary is the only built-in option, which is a fidelity gap versus [15]'s guidance (FD/ZH families) and standard hemodynamic practice.
- **Passive scalar / contrast transport:** the `TEMPERATURE` extension implements exactly one advected–diffused scalar on a D3Q7 subgrid (diffusion coefficient α, thermal expansion β, `TYPE_T` Dirichlet boundaries) [12][11]. With β = 0 it behaves as a passive scalar advected by the flow — an adequate contrast-bolus vehicle in principle. Limitations for R4: only one scalar; no documented scalar-only marching with a frozen velocity; **no checkpoint/restart** ("FluidX3D does not support saving/loading checkpoints") [11], so the velocity state cannot be reloaded and re-solved against new injection parameters without re-running the coupled simulation or modifying the kernels (which trips license clause 5 [10]).
- **Multiphase:** free-surface (volume-of-fluid + PLIC) `SURFACE` extension — but it ignores the gas phase; genuine two-phase (Shan–Chen/phase-field) is explicitly not implemented (explored and rejected) [11][12]. No chemistry/reaction modeling ("Can FluidX3D model chemical reactions? No.") [11]. No AMR [11]. Mach < 0.3 regime only [11] (fine for blood).

### 5.6 Meshing & geometry ingestion
- **Binary STL in only** [12]: `lbm.voxelize_stl(path, center, rotation, size)` or `read_stl(...)` + `lbm.voxelize_mesh_on_device(mesh)` with fast GPU voxelization (ms-scale since v2.1) [11]. Requirements: watertight mesh, consistently outward-facing normals; auto or manual scaling/placement. Composite STL assemblies and moving/rotating parts (periodic re-voxelization) supported [12].
- Output: binary `.vtk` (SI-converted) for ρ/u/flags/T/φ; PNG/QOI/BMP renders; mesh→VTK export [12]. Volumetric exports are warned to be tens of GB per frame [12]; in-situ rendering is the intended consumption path.

### 5.7 Community & maintenance
- GitHub API as of 2026-09-23 [13]: created 2022-08-04, last push 2026-09-07, v3.8 released 2026-09-05, 5,289 stars, 472 forks, 54 open issues, discussions enabled, **pull requests restricted to collaborators**. Very rapid release cadence (v1.0 2022-08 → v3.8 2026-09) with detailed changelogs [11].
- **Solo-developed and maintained** ("FluidX3D is solo-developed and maintained by Dr. Moritz Lehmann") [11] — bus factor 1; upstream development control is total (our modifications would be a private fork under clause-5 constraints).

### 5.8 Published benchmark evidence
- Author-maintained benchmark tables (README) with a documented methodology: D3Q19 SRT, no extensions, ~256³ box, FP32 arithmetic with FP32/FP16S/FP16C storage; per-cell traffic 153 B/step (FP32/FP32) or 77 B/step (FP16) → memory-bandwidth-bound with roofline efficiency percentages [11]. Representative rows (MLUPs/s; FP32/FP32 and FP16S; vendor-community-reported):
  - Tesla T4: 1,356 / 2,869 · L4: 1,490 / 2,854 · A10: 2,931 / 5,741
  - V100-SXM2 32GB: 4,471 / 8,947 · A100-SXM4 40GB: 8,543 (84% roofline) / 16,013 · A100-SXM4 80GB: 10,228 / 18,448
  - H100-SXM5 80GB: 17,602 / 29,561 · H200-SXM5: 23,056 / 36,610 · MI300X: 22,867 / 41,327 · GPU Max 1100: 3,769 / 6,303
  - Multi-GPU (single node): 8× A100-SXM4 40GB: 37,619 / 72,965 (4.4×/4.6× scaling); 8× H200: 92,008 / 157,743; 8× MI300X: 152,835 / 204,924 (FP16C); 32× MI210 (GigaIO): 23,881 / 50,952 (≈6× at 32 GPUs).
  - (The README's single-GPU and multi-GPU tables differ slightly for the same card, e.g. A100-40 FP16S 16,013 vs 15,917 — figures above are taken from the single-GPU table.)
- Independent/academic evidence: FP16 accuracy & performance study (Lehmann et al., Phys. Rev. E 106, 015308, 2022) [25]; Esoteric-Pull streaming + free-surface LBM (Lehmann, Computation 10, 92, 2022) [26]; PhD thesis 2023 [27]; GigaIO case study (32× MI210 composable) [22]. **Application pedigree is free-surface/environmental (microplastics), not hemodynamics** — no published vascular validation.

### 5.9 Integration cost with our Python pipeline (moderate–high)
- Geometry: export pre-decimation meshes to **binary, watertight, outward-normal STL** (trimesh export from the pipeline's mesh stage; the pipeline's `watertight` report field becomes a hard gate — non-watertight cardiac structures will fail FluidX3D's requirement [12]). The quantized GLB is unusable (quantization + display-oriented decimation).
- Orchestration: every scenario is a `src/setup.cpp` edit + recompile [12]; parameters are C++ literals, so batch sweeps mean templating C++ and rebuilding per case (or one parameterized setup with generated code). Third-party PyPI package `fluidx3d` exists [23] but is unofficial and its license/compatibility position is unverified.
- R4 (live scalar re-solve) requires either the author's cooperation (checkpointing + decoupled scalar stepping) or kernel modification under clause 5 [10] — the largest integration risk of the three candidates.

---

## 6. Cloud-GPU benchmarking plan (to run before final procurement)

Purpose: replace the non-comparable literature/vendor numbers above with one apples-to-apples dataset across solvers × cloud GPUs × Phase-2 workloads, including the R4 live-re-solve latency that drives the product experience. **Nothing in this section has been run; sizes below are grounded in measurements of the repo's own data (2026-09-23).**

### 6.1 Instance / accelerator matrix

| Tier | Instance examples (verify SKU at provisioning) | Accelerator | Purpose |
|---|---|---|---|
| T-budget | AWS `g4dn.xlarge`, GCP `n1-standard-8`+T4 | 1× T4 (16 GB) | Cost floor; smoke tests |
| T-mid | AWS `g5.xlarge` (A10G), GCP `g2-standard-*` (L4), Azure NC L4as v5 (verify) | 1× A10G/L4 (24 GB) | Interactive-latency candidate |
| T-perf | AWS `p4de.24xlarge` / `p4d`, GCP `a2-highgpu`/`a2-ultragpu`, Azure ND A100 v5 | 1–8× A100 (40/80 GB, NVLink) | Primary sweep class |
| T-top | AWS `p5.48xlarge`, GCP `a3-highgpu-8g`/`a3-megagpu`, Azure ND H100 v5 | 8× H100 (80 GB) | Latency targets, scaling |
| T-amd | Azure ND MI300X v5 | 8× MI300X (192 GB) | HIP (OpenLB) / OpenCL (FluidX3D) portability |
| T-multi-node | 2–4 nodes of T-perf/T-top (EFA/InfiniBand) | multi-node MPI | OpenLB/HARVEY scaling only (FluidX3D cannot span nodes [11]) |

### 6.2 Workloads and sizes

Lattice sizes derived from measurements of `data/raw/` on 2026-09-23 (nibabel header + mask bounding boxes, `measured`):

- ImageCAS: 601 = 512×512×243 @ (0.318, 0.318, 0.5) mm, coronary bbox 317×263×231; 700 = 512×512×206 @ (0.33, 0.33, 0.5) mm, bbox 278×267×189; 798 = 512×512×268 @ (0.449, 0.449, 0.5) mm, bbox 237×258×159.
- TotalSegmentator: s0004 = 255×177×440 @ 1.5 mm iso (cardiac-6 union bbox ≈ 92×97×223); s0015 = 293×293×344 @ 1.5 mm iso (union ≈ 121×101×198).

| ID | Workload | Grid (lattice cells) | What it measures |
|---|---|---|---|
| W0a | 3D Poiseuille + Womersley pulsatile pipe; Gaussian-bolus advection–diffusion in a plug-flow pipe (analytic reference) | 128³ (2.1M) | Correctness gates: L2 error vs analytic, scalar mass conservation, bolus dispersion error |
| W0b | Lid-driven cavity (BGK and MRT), GPU-vs-CPU determinism check | 256³ (16.8M) | Kernel throughput reference (comparable to [11]'s methodology); reproducibility |
| W1 | Microbench sweep: D3Q19 BGK and MRT, empty box, no extensions, FP32 | 256³ (16.8M), 512³ (134M) | MLUPs + measured bytes/cell + roofline efficiency per solver/GPU (the apples-to-apples replacement for §3.8/§5.8) |
| W2 | **Phase-2 working cases:** ImageCAS 601/700/798 coronary trees, cropped to mask bbox + 16-cell margin | ≈ 2.7e7 / 2.0e7 / 1.5e7 cells (measured bboxes + margin; fluid fraction ≈ 1–5%, to be measured) | Time-to-solution per cardiac cycle (2–5 cycles at 60–100 bpm), WSS/pressure outputs, I/O share |
| W3 | Refined W2 (2× linear resample of geometry + inlet BCs) | ≈ 2.2e8 cells | Multi-GPU/multi-node scaling; VRAM headroom (est. footprint: FluidX3D ≈ 12–14 GB FP16 at 55 B/cell [11]; conventional FP32 D3Q19 double-buffered ≈ 40–50 GB — OpenLB bytes/cell unverified, must be measured) |
| W4 | Multi-structure cardiac: TotalSegmentator s0004/s0015 cardiac-6 union, native 1.5 mm and 2× resample (0.75 mm) | ≈ 2–2.5M and ≈ 2e7 | Multi-structure BC handling (inlets at great vessels), throughput per $ |
| W5 | **R4 live-re-solve protocol** on the W2 velocity checkpoint (below) | scalar lattice only (ADE D3Q7/D3Q19 subset of W2) | The decisive product metric |

### 6.3 Metrics (recorded per run)

1. **MLUPs/s** — fluid and scalar sweeps separately; kernel-window and end-to-end (incl. voxelization, checkpoint I/O, VTK export).
2. **Effective bandwidth** — bytes/cell·MLUPs vs published peak bandwidth (roofline efficiency %), with measured bytes/cell for solvers that do not publish it.
3. **Time-to-solution** — wall-clock per simulated cardiac cycle at each HR; and per 10 s of contrast-transport time.
4. **R4 re-solve latency** — wall-clock from "new injection parameters accepted" to "full scalar field + target observables (e.g. attenuation curve at ROI) written", on a frozen velocity field.
5. **Scaling** — strong scaling 1/2/4/8 GPUs within a node (OpenLB, FluidX3D, HARVEY) and 2/4 nodes MPI (OpenLB, HARVEY only); efficiency vs 1 GPU.
6. **Memory** — peak VRAM, measured bytes/cell, max achievable lattice per instance type.
7. **Cost** — on-demand $ per W2 cardiac cycle and per W5 re-solve (instance rate × wall-clock), recorded at trial time (cloud rates drift).
8. **Accuracy gates** (must pass before performance is credited): Womersley L2 error < 1% at working resolution; scalar mass drift < 0.1% per transport window; bolus first/second moments within 2% of the 1D analytic advection–diffusion solution (W0a).
9. **Robustness** — determinism (repeat runs bit-identical?), FP16-storage accuracy delta vs FP32 for W2 observables (FluidX3D FP16S/FP16C modes [11][25]), failure/restart behavior.

### 6.4 R4 live-re-solve protocol (the differentiator test)

1. Run W2 flow to a periodic state (or 2–3 cycles); write the velocity-state checkpoint (OpenLB `serializerIO`; FluidX3D/HARVEY: whatever they support — expected gaps per §3–§5).
2. Freeze velocity. Define 3 injection variants (e.g. bolus duration 2×, concentration 0.5×, shifted start time) covering the product's parameter surface.
3. Re-solve **only** the scalar transport for a 10 s-equivalent window per variant; record metric 4 and scalar-sweep MLUPs.
4. Gate: re-solve latency ≤ 2 min on T-perf at W2 scale (product SLA placeholder — to be confirmed with stakeholders) **and** ≤ 30% of the wall-clock of a full coupled re-run. Solvers that must re-run the coupled system fail this gate by construction.

### 6.5 Methodology

- Dedicated/on-demand instances (no spot for timed runs); record instance type, GPU SKU, driver, CUDA/ROCm/OpenCL-ICD versions, MPI version, CPU model, clocks (and whether clock locking is possible), solver commit/version.
- 5 warm-up iterations then ≥20 timed iterations for W1; ≥3 full repeats for W2–W5; report median and p95.
- Same grids, BCs, τ/Re, and stopping criteria across solvers; FP32/FP32 as the baseline column (FluidX3D FP16S/FP16C reported as separate columns with the accuracy deltas of metric 9).
- Self-reported vendor numbers (§5.8) are logged alongside measured values for reference only.

### 6.6 Decision gates

- **G1 License (pass/fail):** usable commercial path for BMT's deployment mode (hosted service vs shipped product) with legal sign-off — including the FluidX3D military-use clause and the GPLv2 distribution analysis for OpenLB.
- **G2 Physics:** W0 accuracy gates pass; W2 runs with FD/regularized or Zou–He inlet + convective/resistance outlet; passive scalar with inlet Dirichlet injection.
- **G3 R4:** §6.4 gate passes.
- **G4 Integration:** end-to-end from `data/raw` masks (and an STL export path for `build_cardiac_glb.py` meshes) to VTK outputs, driven by Python subprocess batch, demonstrated on W2 within 1 engineer-day per solver after setup.

### 6.7 Effort envelope

~48 core runs (3 solvers × 4 instance classes × 4 workloads) × 3 repeats plus W0/W5 — order of 150–200 instance-hours dominated by W2/W3, i.e. a mid-4-figure to low-5-figure cloud budget at on-demand rates (rates to be quoted at trial time). Setup effort: OpenLB 3–5 engineer-days (build + case templating), FluidX3D 1–2 days (build + setup.cpp templating) plus license wait, HARVEY n/a until access is granted.

---

## 7. Selection recommendation

### 7.1 Recommendation: **OpenLB (1.9.x)** as the Phase-2 LBM platform

### 7.2 Rationale

1. **License is usable for BMT as-is (with compliance management).** GPLv2 permits commercial use [1]; obligations attach on distribution, so a cloud/hosted deployment of the solve service avoids the copyleft trigger entirely, and even a shipped-product embedding has a defined compliance path (or a clean negotiation target via the consortium [9]). By contrast FluidX3D's license flatly prohibits commercial use — including fee-bearing hosting/consulting services — and additionally prohibits military use [10][11]; HARVEY is proprietary and usable only under a Duke license whose commercial terms are unpublished [17][18].
2. **Physics coverage matches the use case without research risk.** MRT *and* BGK/Smagorinsky collision [5]; the exact inlet/outlet BC families validated for image-derived vasculature (Zou–He, FD/regularized) plus convective outflow [6][15]; and — decisively for R3/R4 — a first-class one-way **Navier–Stokes → advection–diffusion coupling** with dedicated scalar Dirichlet (injection) boundaries and worked ADE examples [5][6][8]. Non-Newtonian rheology and multiphase are available headroom if contrast pooling/extravasation enters scope later [5][3].
3. **The R4 live re-solve is architecturally native.** Serial/parallel checkpointing [2] plus v1.9's CLI parameter interface "without re-compilation" [3] give exactly the frozen-velocity, re-parameterized scalar re-solve loop the product needs. FluidX3D cannot do it (no checkpoints, no scalar-only marching [11]); HARVEY has no scalar module to re-solve (§4.5).
4. **Cloud GPU coverage is complete and scaling is proven.** CUDA production path + preliminary HIP and SYCL covers NVIDIA/AMD/Intel cloud classes [3][4]; multi-GPU/multi-node scaling demonstrated up to 512 A100s and 1,000 Aurora nodes [4], matching T-perf/T-top/T-multi-node (§6.1).
5. **Integration cost is bounded and file-based.** STL (or direct mask) ingestion, XML/CLI parameters, VTK outputs, checkpoints [2][7] — all driven from our Python pipeline via subprocess; no modification of solver source required for the base scope (keeping GPLv2 obligations minimal and reviewable).
6. **Maintenance risk is lowest.** Multi-author project with quarterly-to-biannual releases, 138 maintained examples, Zenodo DOIs, active forum/spring schools [2][3][8].

**Accepted costs/risks of this recommendation (with mitigations):**
- GPLv2 compliance must be engineered (keep the solver as a service-side component or behind a file-exchange process boundary; legal review; parallel commercial-license inquiry via the consortium [9]).
- Raw per-GPU throughput is likely below FluidX3D's esoteric-pull + FP16 scheme (compare ~2.6 GLUPs/A100 derived from [4] vs 8.5–18.4 GLUPs/A100 in [11] — different cases/years, so treat as hypothesis): mitigate with the `mixed`-precision build configs [19], more GPUs, and W1 measurement; if W1 shows a ≥2× deficit at equal accuracy, re-open §7.3.
- HIP/SYCL backends are labeled "preliminary" [3][4]: pin CUDA for production, treat AMD/Intel as stretch.
- C++ template API learning curve; budget the §6.7 setup effort.

### 7.3 Runner-up: **FluidX3D** — technically strong, legally blocked

- **Strengths:** best memory efficiency in class (55 B/cell FP16, in-place streaming) [11][26]; best published per-GPU MLUPs across every cloud SKU [11]; trivial bring-up on any vendor's cloud GPU via OpenCL [12]; single-node cross-vendor multi-GPU pooling [11].
- **Blockers:** (a) commercial use prohibited without the copyright holder's explicit written permission [10][11] — BMT is a commercial context, and the hosting/consulting clause reaches a hosted solve service; (b) military-use prohibition must be cleared against BMT's field of use (flag F4); (c) R4 cannot be delivered without kernel changes (no checkpoints, no scalar-only re-solve) which then trip the altered-source publication clause [10][11]; (d) weak outflow BC set for arterial flow [12].
- **Conditions to promote to primary:** written commercial license from Dr. Lehmann covering BMT's deployment + legal clearance of clause 3; an agreed scalar re-solve mechanism (author-provided or licensed modification); and W1/W2 showing ≥1.5× cost-per-solve advantage at equal accuracy.

### 7.4 Third: **HARVEY** — best domain pedigree, worst access

- Right problem, right scales: image-derived patient-specific hemodynamics is its home turf, with published BC guidance for exactly our geometry class [15], multi-GPU pedigree [16], and real AWS production at 400-instance scale [17].
- But it fails R1 today (proprietary; a research license exists but commercial terms are unpublished [17][18]) and R3/R4 would be lab-side co-development (no documented scalar module, §4.5); integration surface is opaque (§4.9). The March-2026 "digital twin engine" announcement [21] suggests a licensing/product track may open — **watch item:** revisit if Duke publishes commercial terms, or if stakeholders want a co-development relationship with the Randles ecosystem more than they want ownership of the stack.

---

## 8. Unverifiable / flagged items

- **F1 — HARVEY license terms:** a proprietary Duke research license with a free academic-use provision exists [18], but commercial pricing, field-of-use (including any military/defense restrictions), support, and escrow terms are not published — only Duke's Office of Licensing and Ventures can clarify. The March-2026 announcement [21] suggests a commercialization track but states no terms.
- **F2 — HARVEY internals:** build system, current production backends (non-CUDA?), MRT presence, checkpointing, mesh input formats (STL support?), and performance-portability state are not publicly inspectable; the MLUPs figures attributed to HARVEY-class runs in [24] are second-hand quotations of [20] (flag F6).
- **F3 — HARVEY contrast/transport module:** no published passive-scalar implementation found; absence of evidence is not evidence of absence inside the proprietary code. Co-development scope/IP unverifiable.
- **F4 — FluidX3D vs BMT field of use:** whether BMT's intended deployment is "commercial use" under clause 2 (assumed yes) and whether it touches "military use" under clause 3 is a legal determination not makeable from public sources; if clause 3 applies, no license negotiation can cure it (the clause has no exception).
- **F5 — OpenLB SYCL/Intel backend:** described as "preliminary" on the 2026 performance page [4] but absent from the 1.9.0 release notes [3]; which public snapshot carries it is unverified. Same for the exact semantics of the `mixed`-precision build configs [19] and OpenLB's per-cell memory footprint (no published bytes/cell figure).
- **F6 — Cross-source performance comparability:** all MLUPs figures in §3.8/§5.8 are self-reported by their projects under different cases/vintages/methods; only §6 trials are admissible for a procurement decision.
- **F7 — FluidX3D third-party PyPI wrapper** [23]: provenance, maintenance, and license compatibility unverified; do not depend on it without review.
- **F8 — HARVEY on Intel GPUs / ALCF AR24 page:** unreachable from this host (TLS certificate failure); an Intel developer article titled "Vascular blood flow simulations accelerated by GPUs" exists in search indexes but was not read. Treated as unverified.
- **F9 — Cloud SKUs/prices** in §6.1/§6.7 are class-level references as of 2026-09-23; exact instance availability and on-demand rates must be quoted at provisioning time.
- **F10 — All performance claims are literature/vendor-reported only.** This host has no GPU; nothing was executed.

---

## 9. Sources (all accessed 2026-09-23 unless noted)

1. OpenLB — Legal Notice (GPL 2.0): https://www.openlb.net/legal-notice/
2. OpenLB — Project overview (features, STL voxelization, XML, checkpointing, VTK, MPI/OpenMP/SIMD/CUDA, project numbers Nov 2024): https://www.openlb.net/overview/
3. OpenLB Release 1.9.0 notes (2025-12-19; 138 examples, CLI w/o recompilation, HIP/ROCm preliminary, models, tested systems, Zenodo DOI 10.5281/zenodo.17899765): https://gitlab.com/openlb/release/-/releases/1.9.0
4. OpenLB — Performance (Aurora/SYCL 2026, Karolina 2025, HoreKa Teal 2025, HoreKa 1.5 2022, Magnus 2018): https://www.openlb.net/performance/
5. OpenLB source, `src/dynamics/` (collision/model headers): https://gitlab.com/openlb/release/-/tree/master/src/dynamics
6. OpenLB source, `src/boundary/` (BC headers): https://gitlab.com/openlb/release/-/tree/master/src/boundary
7. OpenLB source, `src/io/` (STL reader, lattice STL reader, XML/CLI readers, serializerIO, VTK writers): https://gitlab.com/openlb/release/-/tree/master/src/io
8. OpenLB examples + GitLab project metadata (`openlb/release`, stats via https://gitlab.com/api/v4/projects/openlb%2Frelease): https://gitlab.com/openlb/release/-/tree/master/examples , https://gitlab.com/openlb/release/-/tree/master/examples/advectionDiffusionReaction
9. OpenLB — Consortium & Support (no published commercial license found): https://www.openlb.net/consortium/
10. FluidX3D LICENSE.md (verbatim license text): https://github.com/ProjectPhysX/FluidX3D/blob/master/LICENSE.md
11. FluidX3D README (update history to v3.8 2026-09-05, features, memory/VRAM tables, single- & multi-GPU benchmark tables, FAQs incl. MRT/checkpoints/AMR/licensing, references, contact): https://github.com/ProjectPhysX/FluidX3D
12. FluidX3D DOCUMENTATION.md (build, driver/OpenCL install, boundary conditions, STL loading/voxelization, SURFACE/TEMPERATURE/SUBGRID/PARTICLES extensions, VTK export): https://github.com/ProjectPhysX/FluidX3D/blob/master/DOCUMENTATION.md
13. FluidX3D GitHub API repo metadata (stars/forks/issues/push dates): https://api.github.com/repos/ProjectPhysX/FluidX3D
14. FluidX3D benchmark methodology + community benchmark thread: https://github.com/ProjectPhysX/FluidX3D/issues/8 (via [11])
15. Feiger, Vardhan, Gounley, Mortensen, Nair, Chaudhury, Frakes, Randles — "Suitability of lattice Boltzmann inlet and outlet boundary conditions for simulating flow in image-derived vasculature", Int. J. Numer. Method. Biomed. Eng. 35(6):e3198 (2019): https://pmc.ncbi.nlm.nih.gov/articles/PMC7605305/
16. Ames, Puleri, Balogh, Gounley, Draeger, Randles — "Multi-GPU immersed boundary method hemodynamics simulations", J. Comput. Sci. 44:101153 (2020): https://pmc.ncbi.nlm.nih.gov/articles/PMC7402620/
17. Tanade, Rakestraw, Ladd, Draeger, Randles — "Cloud Computing to Enable Wearable-Driven Longitudinal Hemodynamic Maps", SC'23, DOI 10.1145/3581784.3607101 (cloud instances, HARVEY architecture/benchmarks, artifact appendix with the proprietary-release statement): https://pmc.ncbi.nlm.nih.gov/articles/PMC11210499/
18. Roychowdhury, Balogh, Mahmud, Puleri, Martin, Gounley, Draeger, Randles — "Enhancing Adaptive Physics Refinement Simulations Through the Addition of Realistic Red Blood Cell Counts", SC'23, DOI 10.1145/3581784.3607105 (HARVEY D3Q19/BGK/half-way bounce-back; APR on 256 Summit nodes and single-node AWS; artifact appendix, verbatim: "HARVEY is generally available under a proprietary research license from Duke University. This license has a provision for a free license for academic use. For access, contact the Duke Office of Licensing and Ventures."): https://pmc.ncbi.nlm.nih.gov/articles/PMC10731911/
19. OpenLB build configs (`config/gpu_openmpi.mk`, `config/gpu_openmpi_mixed.mk`, `config/gpu_leonardo_mixed.mk`, `default.mk`): https://gitlab.com/openlb/release/-/blob/master/config/gpu_openmpi_mixed.mk
20. Martin et al. — "Performance Evaluation of Heterogeneous GPU Programming Frameworks for Hemodynamic Simulations", SC-W 2023 (CUDA/SYCL/HIP/Kokkos); MLUPs second-hand via Suffa et al. 2026, DOI 10.1177/10943420261434639: https://www.alcf.anl.gov/publications/performance-evaluation-heterogeneous-gpu-programming-frameworks-hemodynamic , https://journals.sagepub.com/doi/10.1177/10943420261434639
21. Duke Center for Computational and Digital Health Innovation — "Randles Lab … Announces HARVEY, a High-Fidelity Cardiovascular Digital Twin Engine …" (2026-03-11): https://comphealth.duke.edu/news/randles-lab-at-duke-university-announces-harvey-a-high-fidelity-cardiovascular-digital-twin-engine-built-for-extreme-scale-simulation/ ; Tanade et al., npj Digital Medicine (2024): https://www.nature.com/articles/s41746-024-01216-3
22. GigaIO — FluidX3D case study (32× MI210 composable, 2024): https://gigaio.com/wp-content/uploads/2024/04/GigaIO_FluidX3D_Case-Study_CFD_final_v1.03_04222024.pdf
23. Third-party PyPI package `fluidx3d` (provenance unverified, flag F7): https://pypi.org/project/fluidx3d/
24. Suffa, Holzer, Köstler, Rüde — "Architecture specific generation of large scale lattice Boltzmann methods for sparse complex geometries" (2026), secondary MLUPs quotations: https://journals.sagepub.com/doi/10.1177/10943420261434639
25. Lehmann, Krause, Amati, Sega, Harting, Gekle — "Accuracy and performance of the lattice Boltzmann method with 64-bit, 32-bit, and customized 16-bit number formats", Phys. Rev. E 106, 015308 (2022): https://www.researchgate.net/publication/362275548_Accuracy_and_performance_of_the_lattice_Boltzmann_method_with_64-bit_32-bit_and_customized_16-bit_number_formats
26. Lehmann — "Esoteric Pull and Esoteric Push: Two Simple In-Place Streaming Schemes for the Lattice Boltzmann Method on GPUs", Computation 10, 92 (2022), DOI 10.3390/computation10060092: https://doi.org/10.3390/computation10060092
27. Lehmann — PhD thesis, "Computational study of microplastic transport at the water-air interface with a memory-optimized lattice Boltzmann method" (2023), DOI 10.15495/EPub_UBT_00006977 (via [11])
28. Randles, Draeger, Bailey, et al. — "Massively parallel simulations of hemodynamics in the primary large arteries of the human vasculature", Procedia Comput. Sci. (ICCS 2015): https://pmc.ncbi.nlm.nih.gov/articles/PMC5693253/
29. Lee, Gounley, Randles, Vetter — "Performance portability study for massively parallel computational fluid dynamics application on scalable heterogeneous architectures", J. Parallel Distrib. Comput. (2019): https://www.sciencedirect.com/science/article/abs/pii/S0743731519301571

evaluation only — no solver executed on this host (no GPU)