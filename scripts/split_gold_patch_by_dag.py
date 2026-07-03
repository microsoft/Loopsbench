#!/usr/bin/env python3
"""
Split a monolithic gold patch into gold_patches/<unit_id>.diff shards.

Uses:
  - unit_dag.json (node ids + edges) for the set of units and slug_order hints
  - slug_diff_map.json (optional): unit stems must match its values
  - requirements/*.yaml: \"## Files to modify\" blocks map repo-relative paths → unit id

Units with no hunks in the monolithic patch get a **0-byte** shard; tasks that
use `solution.sh` with `[ -s "$patch_file" ]` skip applying those. A marker file
`gold_patches/.lhb_split_noop.json` lists those stems so orchestration can treat
the shard set as complete.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

FILES_SECTION = "## Files to modify"


def _load_unit_ids_from_dag(task_dir: Path) -> list[str]:
    path = task_dir / "unit_dag.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return []
    out: list[str] = []
    for node in nodes:
        if isinstance(node, dict):
            uid = str(node.get("id", "")).strip()
            if uid:
                out.append(uid)
    return out


def _slug_map_unit_stems(task_dir: Path) -> list[str] | None:
    p = task_dir / "slug_diff_map.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    stems: list[str] = []
    for diff_rel in data.values():
        stem = Path(str(diff_rel)).stem.removeprefix("pr_")
        if stem and stem not in stems:
            stems.append(stem)
    return stems


def _paths_from_requirement_yaml(path: Path) -> tuple[str, list[str]]:
    data = yaml.safe_load(path.read_text()) or {}
    unit_id = str(data.get("id", path.stem)).strip()
    text = data.get("requirement") or ""
    if FILES_SECTION not in text:
        return unit_id, []
    rest = text.split(FILES_SECTION, 1)[1]
    if "##" in rest:
        rest = rest.split("##", 1)[0]
    rels: list[str] = []
    for line in rest.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.strip("`")
        line = line.removeprefix("/workspace/").removeprefix("workspace/")
        # Comma- and/or whitespace-separated path tokens on each line.
        for segment in line.split(","):
            seg = segment.strip().strip("`")
            if not seg:
                continue
            seg = seg.removeprefix("/workspace/").removeprefix("workspace/")
            seg_n = seg.replace("\\", "/")
            if seg_n.startswith("http"):
                continue
            # Lines like "1. MySQL - 中文目录/…" must stay one path (do not split on spaces).
            # Also "src/a/foo.h + src/b/bar.h (Part 1)" → two paths.
            if "/" in seg_n and not seg_n.startswith("/"):
                for piece in re.split(r"\s+\+\s+", seg_n):
                    piece = piece.strip()
                    piece = re.sub(r"\s*\([^)]*\)\s*$", "", piece).strip()
                    if piece:
                        rels.append(piece)
                continue
            for token in seg.split():
                first = token.strip().strip("`")
                if not first or first.startswith("http"):
                    continue
                # Repo-relative paths: src/... (common) or e.g. A1-SL/foo.py, lab2-.../kernel/... (course layouts)
                if first.startswith("src/") or ("/" in first and not first.startswith("/")):
                    rels.append(first)
                    continue
                # Repo-root files (no slash), e.g. Dockerfile-lab2-base
                if re.fullmatch(r"[A-Za-z0-9_.-]+", first):
                    rels.append(first)
    return unit_id, rels


def _expand_req_path_patterns(task_dir: Path, raw: str) -> list[str]:
    """Expand lab-range shorthands (``lab5–lab8/...``) and glob ``*``/``?`` against ``task_dir/base``."""
    norm = raw.replace("\\", "/").strip().strip("`")
    norm = norm.removeprefix("/workspace/").removeprefix("workspace/")
    if not norm or norm.startswith("http"):
        return []

    m = re.match(r"^lab(\d+)\s*[\u2013-]\s*lab(\d+)/(.*)$", norm)
    if m:
        lo, hi, tail = int(m.group(1)), int(m.group(2)), m.group(3)
        patterns = [f"lab{i}/{tail}" for i in range(lo, hi + 1)]
    else:
        patterns = [norm]

    base = task_dir / "base"
    if not base.is_dir():
        out: list[str] = []
        for ptn in patterns:
            out.append(ptn.rstrip("/") if ptn.endswith("/") else ptn)
        return out

    out = []
    for ptn in patterns:
        if any(ch in ptn for ch in "*?["):
            try:
                hits = sorted(base.glob(ptn))
            except Exception:
                hits = []
            for p in hits:
                if p.is_file():
                    out.append(str(p.relative_to(base)).replace("\\", "/"))
        elif ptn.endswith("/"):
            out.append(ptn.rstrip("/"))
        else:
            out.append(ptn)
    return out


def _build_path_to_unit(task_dir: Path) -> tuple[dict[str, str], set[str]]:
    """Map repo-relative paths to a single unit id when unambiguous.

    If multiple requirement YAMLs list the same path (e.g. several units touching
    one .cpp file), that path is omitted from the map and returned in the second
    set; ``split_task`` then splits the file's diff chunk per ``@@`` hunk and
    routes hunks using ``_route_ambiguous_hunk``.
    """
    req_dir = task_dir / "requirements"
    path_to_unit: dict[str, str] = {}
    if not req_dir.is_dir():
        return path_to_unit, set()

    claimants: dict[str, set[str]] = defaultdict(set)
    for ypath in sorted(req_dir.glob("*.yaml")):
        try:
            uid, paths = _paths_from_requirement_yaml(ypath)
        except Exception:
            continue
        for rel in paths:
            expanded = _expand_req_path_patterns(task_dir, rel)
            if not expanded:
                expanded = [rel.replace("\\", "/").strip()]
            for norm in expanded:
                norm = norm.replace("\\", "/")
                claimants[norm].add(uid)
                # Patches are often authored from inside NJU_DBPractice (paths `src/...`);
                # requirements may list `NJU_DBPractice/src/...`. Register both.
                if norm.startswith("NJU_DBPractice/"):
                    tail = norm[len("NJU_DBPractice/") :]
                    if tail:
                        claimants[tail].add(uid)

    ambiguous: set[str] = {p for p, uids in claimants.items() if len(uids) > 1}
    for norm, uids in claimants.items():
        if len(uids) == 1:
            path_to_unit[norm] = next(iter(uids))
        elif len(uids) > 1:
            pass  # resolved per-hunk
        # len 0 cannot happen
    return path_to_unit, ambiguous


# (regex, unit_id) — first match in the hunk wins; order matters (longer/specific names first).
_AMBIG_HUNK_RULES: dict[str, list[tuple[str, str]]] = {
    "src/geometry/halfedge-local.cpp": [
        (r"Halfedge_Mesh::bisect_edge\s*\(", "bisect_edge"),
        (r"Halfedge_Mesh::split_edge\s*\(", "split_edge"),
        # @@ context may start inside extrude_face / collapse; use body anchors.
        (r"Cannot extrude a boundary face", "extrude_face"),
        (r"Halfedge_Mesh::extrude_face\s*\(", "extrude_face"),
        (r"Halfedge_Mesh::extrude_positions\s*\(", "extrude_face"),
        (r"Halfedge_Mesh::flip_edge\s*\(", "flip_edge"),
        (r"Halfedge_Mesh::collapse_edge\s*\(", "collapse_edge"),
    ],
    "src/geometry/halfedge-global.cpp": [
        # First catmark hunk may start mid-signature; body markers are stable.
        (r"No vertex position supplied for vertex with id", "catmark_subdivide_helper"),
        (r"Halfedge_Mesh::catmark_subdivide_helper\s*\(", "catmark_subdivide_helper"),
        (r"void\s+Halfedge_Mesh::flip_orientation\s*\(", "global_mesh_utilities"),
        (r"void\s+Halfedge_Mesh::set_corner_normals\s*\(", "global_mesh_utilities"),
        (r"void\s+Halfedge_Mesh::set_corner_uvs_per_face\s*\(", "global_mesh_utilities"),
    ],
    # CS188 task_cs188: one file, four requirement units; route by inserted body anchors.
    "tracking/inference.py": [
        (r"joinFactorsByVariable", "inference_bn_ve"),
        (r"variableDomainsDict\[PAC\]", "inference_bn_ve"),
        (r"busters\.getObservationProbability", "inference_distribution"),
        (r"Cannot sample from an empty distribution", "inference_distribution"),
        (r"self\[key\]\s*=\s*self\[key\]\s*/\s*total", "inference_distribution"),
        (r"self\.beliefs\[pos\]\s*\*=", "inference_exact"),
        (r"newBeliefs = DiscreteDistribution", "inference_exact"),
        (r"positions = self\.legalPositions", "inference_particle"),
        (r"weights = DiscreteDistribution", "inference_particle"),
        (r"dist\[p\] \+= 1", "inference_particle"),
        (r"newParticles = \[\]", "inference_particle"),
    ],
    # task_cse234: pa1/auto_diff.py shared by five DAG units. Hunk trailers often show the next
    # ``class Foo``; use body anchors before generic ``class`` patterns to avoid mis-routing.
    "pa1/auto_diff.py": [
        (r"node_to_grad\[id\(output_node\)\]", "graph_execution"),
        (r"if isinstance\(node\.op, PlaceholderOp\)", "graph_execution"),
        (r"def dfs\(node\):", "graph_execution"),
        (r"import torch\.nn\.functional as F", "normalization_ops"),
        (r"F\.layer_norm", "normalization_ops"),
        (r"return torch\.softmax\(input_values\[0\]", "normalization_ops"),
        (r"s = softmax\(x, d\)", "normalization_ops"),
        (r"return torch\.matmul\(input_values\[0\], input_values\[1\]\)", "reduction_broadcast_ops"),
        (r"matmul\(transpose\(A", "reduction_broadcast_ops"),
        (r"dims_to_sum", "reduction_broadcast_ops"),
        (r"class BroadcastOp", "reduction_broadcast_ops"),
        (r"class LogOp", "reduction_broadcast_ops"),
        (r"print\('expand_op'", "reduction_broadcast_ops"),
        (r"class ExpandAsOp3d", "reduction_broadcast_ops"),
        (r"return input_tensor\.expand_as\(target_tensor\)", "reduction_broadcast_ops"),
        (r"return \[sum_op\(output_grad,dim=0\)", "reduction_broadcast_ops"),
        (r"return \[sum_op\(output_grad,dim=\(0, 1\)\)", "reduction_broadcast_ops"),
        (r"input_values\[0\]\.sum\(dim=node\.dim", "reduction_broadcast_ops"),
        (r"input_values\[0\]\.mean\(dim=node\.attrs", "reduction_broadcast_ops"),
        (r"torch\.relu\(input_values\[0\]\)", "reduction_broadcast_ops"),
        (r"torch\.sqrt\(input_values\[0\]\)", "reduction_broadcast_ops"),
        (r"input_values\[0\] \*\* node\.attrs\[\"exponent\"\]", "reduction_broadcast_ops"),
        (r"power\(node\.inputs\[0\], n - 1\)", "reduction_broadcast_ops"),
        (r"mul_by_const\(output_grad \* x1, -1\) / \(x2 \* x2\)", "basic_arithmetic_ops"),
        (r"return \[div_by_const\(output_grad, node\.constant\)\]", "basic_arithmetic_ops"),
        (r"return torch\.ones_like\(input_values\[0\]\)", "basic_arithmetic_ops"),
        (r"return torch\.zeros_like\(input_values\[0\]\)", "basic_arithmetic_ops"),
        (r"class TransposeOp", "reduction_broadcast_ops"),
        (r"class DivByConstOp", "basic_arithmetic_ops"),
        (r"class DivOp\(Op\)", "basic_arithmetic_ops"),
        (r"class OnesLikeOp", "basic_arithmetic_ops"),
        (r"class ZerosLikeOp", "basic_arithmetic_ops"),
        (r"class SubOp", "basic_arithmetic_ops"),
        (r"class GreaterThanOp", "basic_arithmetic_ops"),
        (r"class MulByConstOp", "basic_arithmetic_ops"),
        (r"class MulOp\(Op\)", "basic_arithmetic_ops"),
        (r"class AddByConstOp", "basic_arithmetic_ops"),
        (r"class AddOp\(Op\)", "basic_arithmetic_ops"),
        (r"def __add__\(self, other\)", "basic_arithmetic_ops"),
        (r"class LayerNormOp", "normalization_ops"),
        (r"class SoftmaxOp", "normalization_ops"),
        (r"class MatMulOp", "reduction_broadcast_ops"),
        (r"class SumOp", "reduction_broadcast_ops"),
        (r"class ExpandAsOp\(Op\)", "reduction_broadcast_ops"),
    ],
    # task_epflxsh: Lab1 formula vs AIG share BooleanAlgebra.scala; route by implemented defs / trailers.
    "labs/lab1/src/BooleanAlgebra.scala": [
        (r"def AIG_eval", "lab1_aig"),
        (r"def AIG_variables", "lab1_aig"),
        (r"def AIG_validity", "lab1_aig"),
        (r"def formulaToAIG", "lab1_aig"),
        (r"No newline at end of file", "lab1_aig"),
        (r"def eval\(f: Formula", "lab1_formula"),
        (r"def substitute\(f: Formula", "lab1_formula"),
        (r"def nnf\(f: Formula", "lab1_formula"),
        (r"def variables\(f: Formula", "lab1_formula"),
        (r"def validity\(f: Formula", "lab1_formula"),
    ],
    # task_epflxsh: transforms vs proof checker share Resolution.scala.
    "labs/lab3/src/Resolution.scala": [
        (r"def checkResolutionProof", "lab3_resolution_proof"),
        (r"def checkResolutionStep", "lab3_resolution_proof"),
        (r"def mergeResults", "lab3_resolution_proof"),
        (r"assumpts = proof\.assumptions", "lab3_resolution_proof"),
        (r"def conjunctionPrenexSkolemizationNegation", "lab3_resolution_transforms"),
        (r"def prenexSkolemizationNegation", "lab3_resolution_transforms"),
        (r"def skolemizationNegation", "lab3_resolution_transforms"),
        (r"def negationNormalForm", "lab3_resolution_transforms"),
        (r"private def substituteFormula", "lab3_resolution_transforms"),
        (r"def makeVariableNamesUnique", "lab3_resolution_transforms"),
    ],
    # task_zjuos: lab3_switch_to vs lab3_task_init both edit sched.c.
    "Lab3/Lab3_vol/arch/riscv/kernel/sched.c": [
        (r"void\s+switch_to\s*\(", "lab3_switch_to"),
        (r"void\s+task_init\s*\(", "lab3_task_init"),
    ],
    # task_mlp: layers_core, layers_batch_conv, selu all touch mlp/layers.py.
    "mlp/layers.py": [
        (r"self\.lamda \* self\.elu\.fprop", "selu"),
        (r"scaled_outputs = outputs / self\.lamda", "selu"),
        (r"self\.lamda \* self\.elu\.bprop", "selu"),
        (r"return 'SELULayer'", "selu"),
        # RadialBasisFunctionLayer fprop/bprop (not matched by generic layers_core anchors).
        (r"self\.centres\[None", "layers_core"),
        (r"num_basis = self\.centres\.shape", "layers_core"),
        (r"grads_wrt_gamma", "layers_batch_conv"),
        (r"grads_wrt_x_hat = grads_wrt_outputs \* self\.gamma", "layers_batch_conv"),
        (r"x_hat = \(inputs - mean\) / np\.sqrt\(var \+ self\.epsilon\)", "layers_batch_conv"),
        (r"C_out, _, kH, kW = self\.kernels\.shape", "layers_batch_conv"),
        (r"out_H = H - kH \+ 1", "layers_batch_conv"),
        (r"grads_wrt_kernels = np\.zeros_like\(self\.kernels\)", "layers_batch_conv"),
        (r"grads_wrt_weights = np\.dot\(grads_wrt_outputs\.T, inputs\)", "layers_core"),
        (r"params_penalty \+= self\.weights_penalty\(self\.weights\)", "layers_core"),
        (r"self\.weights\.dot\(inputs\.T\)\.T \+ self\.biases", "layers_core"),
        (r"return grads_wrt_outputs\.dot\(self\.weights\)", "layers_core"),
        (r"1\. / \(1\. \+ np\.exp\(-inputs\)\)", "layers_core"),
        (r"return grads_wrt_outputs \* outputs \* \(1\. - outputs\)", "layers_core"),
        (r"return np\.maximum\(inputs, 0\.\)", "layers_core"),
        (r"return \(outputs > 0\) \* grads_wrt_outputs", "layers_core"),
        (r"negative_gradients = self\.alpha \* \(outputs < 0\) \* grads_wrt_outputs", "layers_core"),
        (r"outputs_to_use = \(outputs < 0\) \* outputs", "layers_core"),
        (r"negative_inputs = np\.copy\(inputs\)", "layers_core"),
        (r"negative_inputs = inputs\n\+        negative_inputs\[negative_inputs>0\] = 0\.", "layers_core"),
        (r"return 'ELULayer'", "layers_core"),
        (r"return np\.tanh\(inputs\)", "layers_core"),
        (r"return \(1\. - outputs\*\*2\) \* grads_wrt_outputs", "layers_core"),
        (r"exp_inputs = np\.exp\(inputs - inputs\.max", "layers_core"),
        (r"grads_wrt_outputs \* outputs\)\.sum\(-1\)", "layers_core"),
        (r"self\.centres\[None", "layers_core"),
        (r"return -2 \* \(", "layers_core"),
        (r"self\._mask = \(self\.rng\.uniform", "layers_core"),
        (r"return grads_wrt_outputs \* self\._mask", "layers_core"),
    ],
    # task_nkucn: receiver_udp_io vs receiver_connection_transfer share receiver.cpp.
    "lab3_linux/receiver.cpp": [
        (r"int recvFileStopWait\s*\(", "receiver_connection_transfer"),
        (r"int acceptConnect\s*\(", "receiver_connection_transfer"),
        (r"&clientaddr", "receiver_udp_io"),
    ],
    # task_nkucn: sender_udp_io vs sender_connection_transfer share sender.cpp.
    "lab3_linux/sender.cpp": [
        (r"int sendFileStopWait\s*\(", "sender_connection_transfer"),
        (r"int doConnect\s*\(", "sender_connection_transfer"),
        (r"static int waitSend\s*\(", "sender_udp_io"),
        (r"struct sockaddr_in from", "sender_udp_io"),
    ],
    # task_nnucpu: MUX2to1 / Ext32 (cpu_helpers) vs top-level wiring comments (cpu_integration).
    "CPU_1/CPU_1.v": [
        (r"module\s+MUX2to1\b", "cpu_helpers"),
        (r"module\s+Ext32\b", "cpu_helpers"),
        (r"(?s).", "cpu_integration"),
    ],
    # task_pdcs252: lab1_freelist_helpers vs lab1_allocation_core share lab1-src-final/myMalloc.c.
    # Match allocation helpers by signature lines (insert_chunk bodies call delete_from_freelist).
    "lab1-src-final/myMalloc.c": [
        (r"static inline header \* allocate_object\s*\(", "lab1_allocation_core"),
        (r"static inline void insert_chunk_to_freelist\s*\(", "lab1_allocation_core"),
        (r"static inline header \* ptr_to_approp_freelist\s*\(", "lab1_allocation_core"),
        (r"static inline header \* split_block\s*\(", "lab1_allocation_core"),
        (r"static inline void deallocate_object\s*\(", "lab1_allocation_core"),
        (r"static inline void update_freelist\s*\(", "lab1_freelist_helpers"),
        (r"static inline void delete_from_freelist\s*\(", "lab1_freelist_helpers"),
        (r"static inline void insert_into_freelist\s*\(", "lab1_freelist_helpers"),
    ],
    # task_stanbx: dist vs cluster helpers vs main loop share kmeansThread.cpp.
    "prog6_kmeans/kmeansThread.cpp": [
        (r"void kMeansThread\s*\(", "prog6_main_loop"),
        (r"double dist\s*\(", "prog6_distance"),
        (r"void computeAssignments\s*\(", "prog6_cluster_compute"),
        (r"void computeCentroids\s*\(", "prog6_cluster_compute"),
        (r"void computeCost\s*\(", "prog6_cluster_compute"),
    ],
    # task_panda3d_collision_hard: prepare_colliders_* vs traverse/compare_* share collisionTraverser.cxx.
    "panda3d/panda/src/collide/collisionTraverser.cxx": [
        (r"prepare_colliders_(single|double|quad)\s*\(", "collision_traverser_prepare_colliders"),
        (r"r_traverse_(single|double|quad)\s*\(", "collision_traverser_execute"),
        (r"_cnode_volume_pcollector\.add_level", "collision_traverser_execute"),
        (r"_gnode_volume_pcollector\.add_level", "collision_traverser_execute"),
        (r"compare_collider_to_solid\s*\(", "collision_traverser_execute"),
        (r"compare_collider_to_geom\s*\(", "collision_traverser_execute"),
    ],
    # task_panda3d_collision_hard: geometric helpers vs test_intersection_from_* share collisionSphere.cxx.
    "panda3d/panda/src/collide/collisionSphere.cxx": [
        (r"intersects_line\(double &t1, double &t2,", "collision_sphere_line_math"),
        (r"intersects_parabola\(double &t, const LParabola", "collision_sphere_line_math"),
        (r"fill_viz_geom\s*\(", "collision_sphere_intersection_tests"),
        (r"test_intersection_from_\w+\s*\(", "collision_sphere_intersection_tests"),
    ],
    # task_panda3d_pgraph_medium: transform_state_math vs transform_state_diagnostics share
    # transformState.cxx. Harness applies math before diagnostics; ``do_compose`` / ``do_invert_compose``
    # hunks use line coordinates that assume the diagnostic hunks above them are already applied,
    # so those hunks must be emitted on the diagnostics shard (see monolithic hunk order).
    "panda3d/panda/src/pgraph/transformState.cxx": [
        (r"CPT\(TransformState\)\s+TransformState::\s*\n\s*do_invert_compose\s*\(", "transform_state_diagnostics"),
        (r"CPT\(TransformState\)\s+TransformState::\s*\n\s*do_compose\s*\(", "transform_state_diagnostics"),
        (r"int\s+TransformState::\s*\n\s*get_num_unused_states\s*\(", "transform_state_diagnostics"),
        (r"void\s+TransformState::\s*\n\s*list_cycles\s*\(", "transform_state_diagnostics"),
        (r"void\s+TransformState::\s*\n\s*list_states\s*\(", "transform_state_diagnostics"),
        (r"void\s+TransformState::\s*\n\s*output\s*\(", "transform_state_diagnostics"),
        (r"CPT\(TransformState\)\s+TransformState::\s*\n\s*invert_compose\s*\(", "transform_state_math"),
        (r"CPT\(TransformState\)\s+TransformState::\s*\n\s*compose\s*\(", "transform_state_math"),
    ],
}


def _split_single_file_chunk_at_hunks(chunk: str) -> list[str]:
    """One unified diff chunk (one file) -> list of valid one-hunk diff texts."""
    if not chunk.endswith("\n"):
        chunk = chunk + "\n"
    # Do not use ``splitlines()``: it drops trailing ``\\r`` on each line, breaking patches
    # whose context lines must match CRLF trees (e.g. task_nnucpu base/CPU_1/CPU_1.v).
    lines = chunk[:-1].split("\n")
    if not lines:
        return []
    preamble_end = 0
    for i, line in enumerate(lines):
        if line.startswith("@@ "):
            preamble_end = i
            break
    else:
        return [chunk]
    preamble = lines[:preamble_end]
    if not preamble:
        return [chunk]
    i = preamble_end
    pieces: list[list[str]] = []
    while i < len(lines):
        block = [lines[i]]
        i += 1
        while i < len(lines) and not lines[i].startswith("@@ "):
            block.append(lines[i])
            i += 1
        pieces.append(block)
    base = "\n".join(preamble) + "\n"
    return [base + "\n".join(piece) + "\n" for piece in pieces]


# task_etharch: one monolithic @@ replaces all stubs in pipe.c; split into per-unit
# patches in the same order as solution.sh / validate_per_pr.
_ETHARCH_PIPE_C_REL = "lab2_problem_ca_fall2022/src/pipe.c"
_ETHARCH_PIPE_UNIT_ORDER = [
    "pipe_recover",
    "pipe_stage_fetch",
    "pipe_stage_decode",
    "pipe_stage_execute",
    "pipe_stage_mem",
    "pipe_stage_wb",
    "pipe_cycle",
]

_NNCPU_ALU_V_REL = "ALU/ALU.v"


def _find_c_void_func_span(lines: list[str], func: str) -> tuple[int, int] | None:
    """Inclusive line range for one ``void <func>(...) { ... }`` in *lines*."""
    sig = re.compile(rf"^void\s+{re.escape(func)}\s*\(")
    start: int | None = None
    for i, line in enumerate(lines):
        if sig.match(line):
            start = i
            break
    if start is None:
        return None
    depth = 0
    seen_open = False
    for j in range(start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
                if seen_open and depth == 0:
                    return (start, j)
    return None


def _etharch_golden_pipe_lines(task_dir: Path, monolithic: Path) -> list[str]:
    lab = task_dir / "base" / "lab2_problem_ca_fall2022"
    if not lab.is_dir():
        raise ValueError(f"{task_dir}: missing base/{_ETHARCH_PIPE_C_REL.split('/')[0]}")
    td = Path(tempfile.mkdtemp(prefix="lhb_etharch_golden_"))
    try:
        ws = td / "workspace"
        shutil.copytree(lab, ws / "lab2_problem_ca_fall2022")
        r = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(ws)],
            input=monolithic.read_bytes(),
            capture_output=True,
        )
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace")[:800]
            raise ValueError(f"apply monolithic gold in temp workspace failed: {err}")
        p = ws / "lab2_problem_ca_fall2022" / "src" / "pipe.c"
        # No trailing newlines in list elements — required so difflib emits valid `` ``/``-``/``+`` prefixes.
        return p.read_bytes().decode("latin-1").splitlines()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _etharch_split_pipe_c_mono_to_shards(
    task_dir: Path, monolithic: Path, units: list[str]
) -> dict[str, str]:
    """Build one valid unified diff per DAG unit; sequential apply matches monolithic."""
    base_pipe = task_dir / "base" / "lab2_problem_ca_fall2022" / "src" / "pipe.c"
    if not base_pipe.is_file():
        raise ValueError(f"missing {base_pipe}")
    unit_set = set(units)
    if unit_set != set(_ETHARCH_PIPE_UNIT_ORDER):
        raise ValueError(
            f"task_etharch pipe.c splitter: unexpected unit set "
            f"{sorted(unit_set)} vs {sorted(_ETHARCH_PIPE_UNIT_ORDER)}"
        )

    current = base_pipe.read_bytes().decode("latin-1").splitlines()
    golden = _etharch_golden_pipe_lines(task_dir, monolithic)
    out: dict[str, str] = {}
    rel = _ETHARCH_PIPE_C_REL
    n_ctx = 3

    for uid in _ETHARCH_PIPE_UNIT_ORDER:
        if uid not in unit_set:
            continue
        sp = _find_c_void_func_span(current, uid)
        sg = _find_c_void_func_span(golden, uid)
        if sp is None or sg is None:
            raise ValueError(f"could not locate void {uid}(...) in base or golden pipe.c")
        lo, hi = sp
        lo_g, hi_g = sg
        clo = max(0, lo - n_ctx)
        chi = min(len(current) - 1, hi + n_ctx)
        old_view = current[clo : chi + 1]
        new_view = current[clo:lo] + golden[lo_g : hi_g + 1] + current[hi + 1 : chi + 1]
        # difflib requires each sequence element to end with ``\n`` (see unified_diff docstring).
        old_seq = [ln + "\n" for ln in old_view]
        new_seq = [ln + "\n" for ln in new_view]
        diff_lines = list(
            difflib.unified_diff(
                old_seq,
                new_seq,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="\n",
                n=n_ctx,
            )
        )
        body = "".join(diff_lines) if diff_lines else ""
        if body and not body.endswith("\n"):
            body += "\n"
        out[uid] = body
        current[:] = current[:clo] + new_view + current[chi + 1 :]

    if current != golden:
        raise ValueError(
            "etharch pipe.c shard sequence did not reproduce golden tree "
            "(internal error in splitter)"
        )
    return out


_PDCS252_MYHTTPD_REL = "lab5-src/myhttpd.cc"


def _pdcs252_forward_decl(line: str) -> bool:
    s = line.strip()
    return s.endswith(";") and "(" in s and ")" in s


def _pdcs252_find_brace_span(lines: list[str], header_re: re.Pattern) -> tuple[int, int] | None:
    start: int | None = None
    for i, line in enumerate(lines):
        if not header_re.match(line):
            continue
        if _pdcs252_forward_decl(line):
            continue
        start = i
        break
    if start is None:
        return None
    depth = 0
    seen = False
    for j in range(start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
                if seen and depth == 0:
                    return (start, j)
    return None


def _pdcs252_golden_myhttpd_lines(task_dir: Path, monolithic: Path) -> list[str]:
    td = Path(tempfile.mkdtemp(prefix="lhb_pdcs252_golden_"))
    try:
        ws = td / "workspace"
        shutil.copytree(task_dir / "base", ws)
        r = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(ws)],
            input=monolithic.read_bytes(),
            capture_output=True,
        )
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace")[:800]
            raise ValueError(f"task_pdcs252 myhttpd splitter: apply monolithic failed: {err}")
        p = ws / "lab5-src" / "myhttpd.cc"
        if not p.is_file():
            raise ValueError(f"task_pdcs252 myhttpd splitter: missing {p}")
        return p.read_bytes().decode("latin-1").splitlines()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _pdcs252_file_unidiff(rel: str, old_lines: list[str], new_lines: list[str], *, n_ctx: int = 3) -> str:
    old_seq = [ln + "\n" for ln in old_lines]
    new_seq = [ln + "\n" for ln in new_lines]
    diff_lines = list(
        difflib.unified_diff(
            old_seq,
            new_seq,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="\n",
            n=n_ctx,
        )
    )
    body = "".join(diff_lines) if diff_lines else ""
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def _pdcs252_split_myhttpd_mono_to_shards(task_dir: Path, monolithic: Path) -> dict[str, str]:
    """lab5_http_utilities vs lab5_http_server share myhttpd.cc; monolithic folds many funcs into few @@ hunks."""
    base_p = task_dir / "base" / "lab5-src" / "myhttpd.cc"
    if not base_p.is_file():
        raise ValueError(f"missing {base_p}")
    rel = _PDCS252_MYHTTPD_REL
    golden = _pdcs252_golden_myhttpd_lines(task_dir, monolithic)
    utils_headers = [
        re.compile(r"^bool\s+endsWith\s*\("),
        re.compile(r"^void\s+writeLink\s*\("),
        re.compile(r"^void\s+writeHeader\s*\("),
        re.compile(r"^void\s+writeFail\s*\("),
        re.compile(r"^int\s+sortNameA\s*\("),
        re.compile(r"^int\s+sortModifiedTimeA\s*\("),
        re.compile(r"^int\s+sortSizeA\s*\("),
        re.compile(r"^int\s+sortNameD\s*\("),
        re.compile(r"^int\s+sortModifiedTimeD\s*\("),
        re.compile(r"^int\s+sortSizeD\s*\("),
    ]
    server_headers = [
        re.compile(r"^int\s+main\s*\("),
        re.compile(r"^void\s+poolSlave\s*\("),
        re.compile(r"^void\s+processRequestThread\s*\("),
        re.compile(r"^void\s+processRequest\s*\("),
    ]
    n_ctx = 3
    utils_frags: list[str] = []
    cur = base_p.read_bytes().decode("latin-1").splitlines()

    for hdr in utils_headers:
        sb = _pdcs252_find_brace_span(cur, hdr)
        sg = _pdcs252_find_brace_span(golden, hdr)
        if sb is None or sg is None:
            raise ValueError(f"task_pdcs252 myhttpd splitter: missing span for {hdr.pattern!r}")
        lo, hi = sb
        lo_g, hi_g = sg
        if cur[lo : hi + 1] == golden[lo_g : hi_g + 1]:
            continue
        clo = max(0, lo - n_ctx)
        chi = min(len(cur) - 1, hi + n_ctx)
        old_view = cur[clo : chi + 1]
        new_view = cur[clo:lo] + golden[lo_g : hi_g + 1] + cur[hi + 1 : chi + 1]
        frag = _pdcs252_file_unidiff(rel, old_view, new_view, n_ctx=n_ctx)
        if frag.strip():
            utils_frags.append(frag)
        cur[lo : hi + 1] = golden[lo_g : hi_g + 1]

    server_frags: list[str] = []
    for hdr in server_headers:
        sb = _pdcs252_find_brace_span(cur, hdr)
        sg = _pdcs252_find_brace_span(golden, hdr)
        if sb is None or sg is None:
            raise ValueError(f"task_pdcs252 myhttpd splitter: missing server span for {hdr.pattern!r}")
        lo, hi = sb
        lo_g, hi_g = sg
        clo = max(0, lo - n_ctx)
        chi = min(len(cur) - 1, hi + n_ctx)
        old_view = cur[clo : chi + 1]
        new_view = cur[clo:lo] + golden[lo_g : hi_g + 1] + cur[hi + 1 : chi + 1]
        frag = _pdcs252_file_unidiff(rel, old_view, new_view, n_ctx=n_ctx)
        if frag.strip():
            server_frags.append(frag)
        cur[lo : hi + 1] = golden[lo_g : hi_g + 1]

    if cur != golden:
        raise ValueError("task_pdcs252 myhttpd splitter: shards did not reproduce golden file")

    return {
        "lab5_http_utilities": "".join(utils_frags),
        "lab5_http_server": "".join(server_frags),
    }


_STANBX_PROG2_MAIN_REL = "prog2_vecintrin/main.cpp"
_STANBX_PROG2_SERIAL_FUNCS = [
    "initValue",
    "verifyResult",
    "absSerial",
    "clampedExpSerial",
    "arraySumSerial",
]
_STANBX_PROG2_VECTOR_FUNCS = ["absVector", "clampedExpVector", "arraySumVector"]

_UCSD_MAIN_REL = "assignment8-GreenSnake/src/main.rs"
_UCSD_START_REL = "assignment8-GreenSnake/runtime/start.rs"


def _stanbx_find_func_span(lines: list[str], func: str) -> tuple[int, int] | None:
    sig = re.compile(rf"^(void|bool|float)\s+{re.escape(func)}\s*\(")
    start: int | None = None
    for i, line in enumerate(lines):
        if not sig.match(line):
            continue
        # Skip forward declarations (`void foo(...);`) — match definitions only.
        if line.rstrip().endswith(");"):
            continue
        start = i
        break
    if start is None:
        return None
    depth = 0
    seen_open = False
    for j in range(start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
                if seen_open and depth == 0:
                    return (start, j)
    return None


def _stanbx_golden_main_cpp_lines(task_dir: Path, monolithic: Path) -> list[str]:
    td = Path(tempfile.mkdtemp(prefix="lhb_stanbx_prog2_golden_"))
    try:
        ws = td / "ws"
        shutil.copytree(task_dir / "base", ws)
        r = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(ws)],
            input=monolithic.read_bytes(),
            capture_output=True,
        )
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace")[:1200]
            raise ValueError(f"task_stanbx prog2 splitter: apply monolithic failed: {err}")
        p = ws / "prog2_vecintrin" / "main.cpp"
        if not p.is_file():
            raise ValueError(f"task_stanbx prog2 splitter: missing {p}")
        return p.read_bytes().decode("latin-1").splitlines()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _stanbx_apply_funcs_from_golden(cur: list[str], golden: list[str], funcs: list[str]) -> list[str]:
    out = cur[:]
    for fn in funcs:
        sp = _stanbx_find_func_span(out, fn)
        sg = _stanbx_find_func_span(golden, fn)
        if sp is None or sg is None:
            raise ValueError(f"task_stanbx prog2 splitter: missing span for {fn!r}")
        lo, hi = sp
        lo_g, hi_g = sg
        out = out[:lo] + golden[lo_g : hi_g + 1] + out[hi + 1 :]
    return out


def _stanbx_split_prog2_main_cpp_to_shards(task_dir: Path, monolithic: Path) -> dict[str, str]:
    """prog2_serial vs prog2_vector share main.cpp; monolithic folds serial+vector into the same @@ hunks."""
    base_p = task_dir / "base" / "prog2_vecintrin" / "main.cpp"
    if not base_p.is_file():
        raise ValueError(f"missing {base_p}")
    rel = _STANBX_PROG2_MAIN_REL
    n_ctx = 3
    base_lines = base_p.read_bytes().decode("latin-1").splitlines()
    golden_lines = _stanbx_golden_main_cpp_lines(task_dir, monolithic)

    after_serial = _stanbx_apply_funcs_from_golden(base_lines, golden_lines, _STANBX_PROG2_SERIAL_FUNCS)
    frag_s = _pdcs252_file_unidiff(rel, base_lines, after_serial, n_ctx=n_ctx)

    final = _stanbx_apply_funcs_from_golden(after_serial, golden_lines, _STANBX_PROG2_VECTOR_FUNCS)
    frag_v = _pdcs252_file_unidiff(rel, after_serial, final, n_ctx=n_ctx)

    if final != golden_lines:
        raise ValueError("task_stanbx prog2 splitter: shards did not reproduce golden main.cpp")

    return {"prog2_serial": frag_s, "prog2_vector": frag_v}


def _ucsdcompiler_golden_lines(task_dir: Path, monolithic: Path, rel: str) -> list[str]:
    td = Path(tempfile.mkdtemp(prefix="lhb_ucsdcompiler_golden_"))
    try:
        ws = td / "ws"
        shutil.copytree(task_dir / "base", ws)
        r = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch", "-d", str(ws)],
            input=monolithic.read_bytes(),
            capture_output=True,
        )
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace")[:1200]
            raise ValueError(f"task_ucsdcompiler splitter: apply monolithic failed: {err}")
        p = ws / rel
        if not p.is_file():
            raise ValueError(f"task_ucsdcompiler splitter: missing {p}")
        return p.read_bytes().decode("utf-8").splitlines()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _ucsdcompiler_apply_headers_from_golden(
    cur: list[str], golden: list[str], headers: list[re.Pattern[str]]
) -> list[str]:
    out = cur[:]
    for hdr in headers:
        sp = _pdcs252_find_brace_span(out, hdr)
        sg = _pdcs252_find_brace_span(golden, hdr)
        if sp is None or sg is None:
            raise ValueError(f"task_ucsdcompiler splitter: missing span for {hdr.pattern!r}")
        lo, hi = sp
        lo_g, hi_g = sg
        out = out[:lo] + golden[lo_g : hi_g + 1] + out[hi + 1 :]
    return out


def _ucsdcompiler_split_main_rs_to_shards(task_dir: Path, monolithic: Path) -> dict[str, str]:
    """Several DAG units share main.rs; monolithic folds them into one or two @@ hunks."""
    rel = _UCSD_MAIN_REL
    base_p = task_dir / "base" / rel
    if not base_p.is_file():
        raise ValueError(f"missing {base_p}")
    base_lines = base_p.read_bytes().decode("utf-8").splitlines()
    golden = _ucsdcompiler_golden_lines(task_dir, monolithic, rel)
    n_ctx = 3
    cur = base_lines[:]
    out: dict[str, str] = {}
    steps: list[tuple[str, list[re.Pattern[str]]]] = [
        (
            "asm_emission",
            [re.compile(r"^fn instr_to_str\b"), re.compile(r"^fn val_to_str\b")],
        ),
        ("codegen", [re.compile(r"^fn compile_to_instrs\b")]),
        (
            "parser",
            [
                re.compile(r"^fn parse_defn\b"),
                re.compile(r"^fn parse_expr\b"),
                re.compile(r"^fn parse_bind\b"),
            ],
        ),
        (
            "compile_wrappers",
            [re.compile(r"^fn compile_defn\b"), re.compile(r"^fn compile\b")],
        ),
        ("main_entry", [re.compile(r"^fn main\b")]),
    ]
    for uid, hdrs in steps:
        nxt = _ucsdcompiler_apply_headers_from_golden(cur, golden, hdrs)
        frag = _pdcs252_file_unidiff(rel, cur, nxt, n_ctx=n_ctx)
        if frag.strip():
            out[uid] = frag
        cur = nxt
    if cur != golden:
        raise ValueError("task_ucsdcompiler main.rs splitter: shards did not reproduce golden")
    return out


def _ucsdcompiler_split_start_rs_to_shards(task_dir: Path, monolithic: Path) -> dict[str, str]:
    """runtime_io vs runtime_values share start.rs; one @@ hunk mixes get_real_content and parse_input."""
    rel = _UCSD_START_REL
    base_p = task_dir / "base" / rel
    if not base_p.is_file():
        raise ValueError(f"missing {base_p}")
    base_lines = base_p.read_bytes().decode("utf-8").splitlines()
    golden = _ucsdcompiler_golden_lines(task_dir, monolithic, rel)
    n_ctx = 3
    cur = base_lines[:]
    out: dict[str, str] = {}

    io_hdrs = [
        re.compile(r"^pub extern \"C\" fn snek_error\b"),
        re.compile(r"^fn parse_input\b"),
    ]
    val_hdrs = [
        re.compile(r"^unsafe fn snek_structural_equal_helper\b"),
        re.compile(r"^fn get_real_content\b"),
    ]

    nxt_io = _ucsdcompiler_apply_headers_from_golden(cur, golden, io_hdrs)
    frag_io = _pdcs252_file_unidiff(rel, cur, nxt_io, n_ctx=n_ctx)
    if frag_io.strip():
        out["runtime_io"] = frag_io
    cur = nxt_io

    nxt_val = _ucsdcompiler_apply_headers_from_golden(cur, golden, val_hdrs)
    frag_val = _pdcs252_file_unidiff(rel, cur, nxt_val, n_ctx=n_ctx)
    if frag_val.strip():
        out["runtime_values"] = frag_val
    cur = nxt_val

    if cur != golden:
        raise ValueError("task_ucsdcompiler start.rs splitter: shards did not reproduce golden")
    return out


def _old_path_key(fragment: str) -> str | None:
    """Stable key for grouping fragments that belong to one file (GNU ``---`` / ``+++`` paths)."""
    lines = fragment.splitlines()
    minus_raw: str | None = None
    for line in lines:
        if line.startswith("--- "):
            raw = line[4:].strip().split("\t", 1)[0].strip()
            if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
                raw = raw[1:-1]
            raw = _decode_gnu_diff_path_escapes(raw)
            minus_raw = raw
            break
    if minus_raw is None:
        return None
    if minus_raw == "/dev/null":
        # New-file patches all have ``--- /dev/null``; distinguish by ``+++ b/...`` target.
        for line in lines:
            if line.startswith("+++ "):
                raw = line[4:].strip().split("\t", 1)[0].strip()
                if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
                    raw = raw[1:-1]
                raw = _decode_gnu_diff_path_escapes(raw)
                if raw.startswith("a/") or raw.startswith("b/"):
                    raw = raw[2:]
                return raw.replace("\\", "/")
        return "/dev/null"
    if minus_raw.startswith("a/") or minus_raw.startswith("b/"):
        minus_raw = minus_raw[2:]
    return minus_raw.replace("\\", "/")


def _diff_text_to_lines(text: str) -> list[str]:
    """Split on ``\\n`` only (keep trailing ``\\r`` on each line); ``splitlines()`` drops CR."""
    if not text.endswith("\n"):
        text = text + "\n"
    return text[:-1].split("\n")


def _fuse_diff_fragments(block: list[str]) -> str:
    """Join consecutive fragments that patch the same ``---`` path into one patch (one preamble, many @@)."""
    lines0 = _diff_text_to_lines(block[0])
    hi = next((i for i, ln in enumerate(lines0) if ln.startswith("@@ ")), len(lines0))
    preamble_lines = lines0[:hi]
    merged_body: list[str] = list(lines0[hi:])
    for frag in block[1:]:
        fl = _diff_text_to_lines(frag)
        k = next((i for i, ln in enumerate(fl) if ln.startswith("@@ ")), len(fl))
        merged_body.extend(fl[k:])
    return "\n".join(preamble_lines + merged_body) + "\n"


def _merge_consecutive_same_old_path(parts: list[str]) -> list[str]:
    """Avoid repeated ``diff``/``---`` headers for one file in a shard (GNU patch may leave ``*.orig``)."""
    if not parts:
        return []
    out: list[str] = []
    i = 0
    while i < len(parts):
        key = _old_path_key(parts[i])
        j = i + 1
        if key is not None:
            while j < len(parts) and _old_path_key(parts[j]) == key:
                j += 1
        block = parts[i:j]
        if len(block) == 1:
            out.append(block[0])
        else:
            out.append(_fuse_diff_fragments(block))
        i = j
    return out


def _merge_shard_parts(parts: list[str]) -> str:
    return "".join(_merge_consecutive_same_old_path(parts))


def _route_ambiguous_hunk(rel_norm: str, hunk_chunk: str, units: list[str]) -> str:
    rules = _AMBIG_HUNK_RULES.get(rel_norm)
    if not rules:
        raise ValueError(
            f"ambiguous path {rel_norm!r} has no hunk-routing rules; "
            f"extend _AMBIG_HUNK_RULES in split_gold_patch_by_dag.py"
        )
    text = hunk_chunk
    for pattern, uid in rules:
        if uid not in units:
            continue
        if re.search(pattern, text):
            return uid
    raise ValueError(
        f"could not route hunk for ambiguous path {rel_norm!r} "
        f"(no signature matched). Hunk head:\n{text[:500]}"
    )


def _rel_to_unit(rel: str, path_to_unit: dict[str, str]) -> str | None:
    """Exact path match, else longest requirement-path prefix (for directory entries)."""
    rel = rel.replace("\\", "/")
    if rel in path_to_unit:
        return path_to_unit[rel]
    best_len = -1
    best_uid: str | None = None
    for key, uid in path_to_unit.items():
        kn = key.rstrip("/")
        if rel == kn or rel.startswith(kn + "/"):
            if len(kn) > best_len:
                best_len = len(kn)
                best_uid = uid
    return best_uid


def _find_monolithic(task_dir: Path) -> Path | None:
    for name in ("gold-patch.diff", "gold_patch.diff"):
        p = task_dir / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _split_diff_chunks(text: str) -> list[str]:
    if not text.strip():
        return []
    parts = re.split(r"(?=^diff )", text, flags=re.M)
    out = [p for p in parts if p.startswith("diff ")]
    if out:
        return out
    # Legacy unified diff without `diff --git` (e.g. comments then `--- a/path`).
    parts2 = re.split(r"(?=^--- a/)", text, flags=re.M)
    if len(parts2) <= 1:
        return []
    head, *rest = parts2
    chunks: list[str] = []
    for i, body in enumerate(rest):
        if not body.strip():
            continue
        if i == 0 and head.strip():
            chunks.append(head.rstrip("\n") + "\n" + body)
        else:
            chunks.append(body)
    return chunks


# e.g. "diff -ruN tasks/task_x/base/src/foo /tmp/ref/src/foo" or "diff --git a/src/foo b/src/foo"
_RE_DIFF_RUN = re.compile(r"^diff\s+(?:--git|-ruN)\s+(\S+)\s+(\S+)\s*$", re.M)


def _decode_gnu_diff_path_escapes(s: str) -> str:
    """Decode ``\\ooo`` octal bytes (GNU unified diff quoting) into a UTF-8 string."""
    buf = bytearray()
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n and s[i + 1] in "01234567":
            j = i + 1
            val = 0
            cnt = 0
            while j < n and s[j] in "01234567" and cnt < 3:
                val = val * 8 + (ord(s[j]) - 48)
                j += 1
                cnt += 1
            if cnt:
                buf.append(val & 0xFF)
                i = j
                continue
        o = ord(s[i])
        if o < 128:
            buf.append(o)
        else:
            buf.extend(s[i].encode("utf-8"))
        i += 1
    return buf.decode("utf-8", errors="replace")


def _parse_diff_ruN_quoted_paths(first_line: str) -> tuple[str, str] | None:
    """Parse ``diff -ruN \"a/...\" \"b/...\"`` (paths may contain spaces)."""
    if not first_line.startswith("diff "):
        return None
    m0 = re.match(r"^diff\s+-ruN\s+", first_line)
    if not m0:
        return None
    tail = first_line[m0.end() :].lstrip()

    def _scan_quoted(s: str, start: int) -> tuple[str, int] | None:
        if start >= len(s) or s[start] != '"':
            return None
        j = start + 1
        while j < len(s):
            if s[j] == '"':
                return s[start + 1 : j], j + 1
            j += 1
        return None

    a = _scan_quoted(tail, 0)
    if a is None:
        return None
    inner_a, pos = a
    rest = tail[pos:].lstrip()
    b = _scan_quoted(rest, 0)
    if b is None:
        return None
    inner_b, pos_b = b
    if rest[pos_b:].strip():
        return None
    return _decode_gnu_diff_path_escapes(inner_a), _decode_gnu_diff_path_escapes(inner_b)


def _chunk_primary_relpath(
    chunk: str, task_dir: Path, path_to_unit: dict[str, str], ambiguous_paths: set[str]
) -> str:
    task_marker = f"tasks/{task_dir.name}/base/"

    def _strip_ab(p: str) -> str:
        p = p.replace("\\", "/")
        if p.startswith("a/"):
            return p[2:]
        if p.startswith("b/"):
            return p[2:]
        return p

    first_line = chunk.splitlines()[0] if chunk else ""
    qp = _parse_diff_ruN_quoted_paths(first_line)
    if qp:
        left = qp[0].replace("\\", "/")
    else:
        left = ""
        m = _RE_DIFF_RUN.match(first_line)
        if m:
            left = m.group(1).replace("\\", "/")
    if left:
        # Course-repo monolithic diffs often use `base/Lab …/file.c` on the left.
        if left.startswith("base/"):
            left = left[len("base/") :]
        if task_marker in left:
            return left.split(task_marker, 1)[1]
        if "/base/" in left:
            return left.split("/base/", 1)[1]
        cand = _strip_ab(left).replace("\\", "/")
        if _rel_to_unit(cand, path_to_unit) is not None:
            return cand
        if cand in ambiguous_paths:
            return cand
    for line in chunk.splitlines():
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :].strip()
            parts = rest.split()
            if len(parts) >= 2:
                a = parts[0].lstrip("ab/").replace("\\", "/")
                if a.startswith("src/"):
                    return a
                if a.startswith("test/"):
                    return a
                if _rel_to_unit(a, path_to_unit) is not None:
                    return a
                if a in ambiguous_paths:
                    return a
        if line.startswith("--- "):
            raw = line[4:].strip().split("\t")[0].strip()
            if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
                raw = raw[1:-1]
            raw = _decode_gnu_diff_path_escapes(raw)
            raw = _strip_ab(raw)
            raw = raw.replace("\\", "/")
            if raw.startswith("base/"):
                raw = raw[len("base/") :]
            if raw == "/dev/null":
                continue
            if raw.startswith("src/"):
                return raw
            if raw.startswith("test/"):
                return raw
            if task_marker in raw:
                return raw.split(task_marker, 1)[1]
            if _rel_to_unit(raw, path_to_unit) is not None:
                return raw
            if raw in ambiguous_paths:
                return raw
    raise ValueError("could not resolve path from diff chunk (first line):\n" + chunk[:400])


def _expected_units(task_dir: Path, dag_units: list[str], slug_stems: list[str] | None) -> list[str]:
    if slug_stems is not None:
        dag_set = set(dag_units)
        slug_set = set(slug_stems)
        if dag_set != slug_set:
            missing = sorted(dag_set - slug_set)
            extra = sorted(slug_set - dag_set)
            raise ValueError(
                f"{task_dir}: unit_dag node ids and slug_diff_map stems differ "
                f"(missing in slug: {missing}, extra in slug: {extra})"
            )
        return slug_stems
    return dag_units


def split_task(task_dir: Path, *, dry_run: bool = False) -> dict:
    task_dir = task_dir.resolve()
    monolithic = _find_monolithic(task_dir)
    if monolithic is None:
        return {"ok": False, "error": "no non-empty gold-patch.diff or gold_patch.diff"}

    dag_units = _load_unit_ids_from_dag(task_dir)
    if not dag_units:
        return {"ok": False, "error": "unit_dag.json missing or has no nodes"}

    slug_stems = _slug_map_unit_stems(task_dir)
    try:
        units = _expected_units(task_dir, dag_units, slug_stems)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    path_to_unit, ambiguous_paths = _build_path_to_unit(task_dir)
    if not path_to_unit and not ambiguous_paths:
        return {
            "ok": False,
            "error": f"{task_dir}/requirements/*.yaml not found or no '## Files to modify' paths",
        }

    # Prefer UTF-8 when valid so requirement paths (often UTF-8, e.g. Chinese dirs) match
    # chunk paths; fall back to latin-1 byte-preserving decode for legacy/binary-ish patches.
    raw_mono = monolithic.read_bytes()
    try:
        raw_mono.decode("utf-8")
        text = raw_mono.decode("utf-8")
        mono_write_enc = "utf-8"
    except UnicodeDecodeError:
        text = raw_mono.decode("latin-1")
        mono_write_enc = "latin-1"
    chunks = _split_diff_chunks(text)
    by_unit: dict[str, list[str]] = defaultdict(list)
    unmapped: list[str] = []

    for ch in chunks:
        # Do not ``rstrip("\n")``: monolithic chunks often end with ``\n\n`` before the
        # next ``diff --git``; stripping collapses that and breaks multi-chunk shards.
        if not ch.endswith("\n"):
            ch = ch + "\n"
        try:
            rel = _chunk_primary_relpath(ch, task_dir, path_to_unit, ambiguous_paths)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        rel = rel.replace("\\", "/")
        if rel in ambiguous_paths:
            subs = _split_single_file_chunk_at_hunks(ch)
            if task_dir.name == "task_etharch" and rel == _ETHARCH_PIPE_C_REL and len(subs) == 1:
                try:
                    eth = _etharch_split_pipe_c_mono_to_shards(task_dir, monolithic, units)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                for uid in units:
                    frag = eth.get(uid, "")
                    if frag.strip():
                        by_unit[uid].append(frag)
                continue
            # task_nnucpu: monolithic folds all ALU edits into one @@ hunk while unit_dag splits
            # alu_submodules vs alu_toplevel on the same path. Sequential byte-equivalent sharding
            # is not representable as two valid patches; keep the monolithic chunk on alu_submodules
            # and emit an explicit noop shard for alu_toplevel (see gold_patches/.lhb_split_noop.json).
            if task_dir.name == "task_nnucpu" and rel == _NNCPU_ALU_V_REL and len(subs) == 1:
                by_unit["alu_submodules"].append(ch)
                continue
            if task_dir.name == "task_pdcs252" and rel == _PDCS252_MYHTTPD_REL:
                try:
                    pd252 = _pdcs252_split_myhttpd_mono_to_shards(task_dir, monolithic)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                for uid in ("lab5_http_utilities", "lab5_http_server"):
                    frag = pd252.get(uid, "")
                    if frag.strip():
                        by_unit[uid].append(frag)
                continue
            if task_dir.name == "task_stanbx" and rel == _STANBX_PROG2_MAIN_REL:
                try:
                    st = _stanbx_split_prog2_main_cpp_to_shards(task_dir, monolithic)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                for uid in ("prog2_serial", "prog2_vector"):
                    frag = st.get(uid, "")
                    if frag.strip():
                        by_unit[uid].append(frag)
                continue
            if task_dir.name == "task_ucsdcompiler" and rel == _UCSD_MAIN_REL:
                try:
                    uc_main = _ucsdcompiler_split_main_rs_to_shards(task_dir, monolithic)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                for uid, frag in uc_main.items():
                    if frag.strip():
                        by_unit[uid].append(frag)
                continue
            if task_dir.name == "task_ucsdcompiler" and rel == _UCSD_START_REL:
                try:
                    uc_start = _ucsdcompiler_split_start_rs_to_shards(task_dir, monolithic)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                for uid, frag in uc_start.items():
                    if frag.strip():
                        by_unit[uid].append(frag)
                continue
            for sub in subs:
                try:
                    uid = _route_ambiguous_hunk(rel, sub, units)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                if uid not in units:
                    unmapped.append(f"{rel} -> {uid} (unit not in dag/slug set)")
                    continue
                by_unit[uid].append(sub)
            continue

        uid = _rel_to_unit(rel, path_to_unit)
        if uid is None:
            unmapped.append(rel)
            continue
        if uid not in units:
            unmapped.append(f"{rel} -> {uid} (unit not in dag/slug set)")
            continue
        by_unit[uid].append(ch)

    if unmapped:
        return {"ok": False, "error": "unmapped or unknown unit paths: " + "; ".join(sorted(set(unmapped)))}

    gold_dir = task_dir / "gold_patches"
    if not dry_run:
        gold_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, int] = {}
    noop: list[str] = []
    for uid in units:
        parts = by_unit.get(uid) or []
        body = _merge_shard_parts(parts)
        if not body.strip():
            noop.append(uid)
            body = ""
        out_path = gold_dir / f"{uid}.diff"
        if not dry_run:
            out_path.write_bytes(body.encode(mono_write_enc))
        written[uid] = len(body.encode(mono_write_enc))

    meta = {"noop_empty_units": noop}
    if not dry_run:
        meta_path = gold_dir / ".lhb_split_noop.json"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "monolithic": str(monolithic),
        "units": units,
        "bytes_written": written,
        "noop_empty_units": noop,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task-dir", type=Path, required=True, help="tasks/task_foo")
    ap.add_argument("--dry-run", action="store_true", help="parse only; do not write files")
    args = ap.parse_args()
    res = split_task(args.task_dir, dry_run=args.dry_run)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
