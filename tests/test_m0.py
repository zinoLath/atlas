"""Testes do M0 do ATLAS (unittest, sem dependências externas)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"


class AtlasTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-", dir=TMP_ROOT))
        self.env = dict(os.environ, ATLAS_HOME=str(self.home))
        self.run_atlas("init")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def run_atlas(self, *args: str, now: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", now or NOW, *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def ok(self, *args: str, now: str | None = None) -> str:
        proc = self.run_atlas(*args, now=now)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def run_fail(self, *args: str, code: int = 2, now: str | None = None) -> subprocess.CompletedProcess:
        proc = self.run_atlas(*args, now=now)
        self.assertEqual(
            proc.returncode, code, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        return proc

    def jsonl(self, name: str) -> list[dict]:
        path = self.home / "state" / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write_jsonl(self, name: str, rows: list[dict]) -> None:
        path = self.home / "state" / name
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def add_demand(self, *extra: str) -> str:
        out = self.ok("add", "--area", "projetos", "--title", "Demanda teste", *extra)
        return out.split()[0]

    def advance(self, demand_id: str, *states: str) -> None:
        for state in states:
            self.ok("transition", demand_id, state)

    def demand(self, demand_id: str) -> dict:
        return {row["id"]: row for row in self.jsonl("demands.jsonl")}[demand_id]

    # ---- 1. fluxo feliz ----------------------------------------------
    def test_flow_records_events_in_order(self) -> None:
        demand_id = self.add_demand()
        self.assertEqual(self.demand(demand_id)["state"], "new")
        self.advance(demand_id, "routed", "triaged", "planned", "ready")
        self.assertEqual(self.demand(demand_id)["state"], "ready")

        events = self.jsonl("events.jsonl")
        self.assertEqual(
            [(event["from"], event["to"]) for event in events],
            [
                ("", "new"),
                ("new", "routed"),
                ("routed", "triaged"),
                ("triaged", "planned"),
                ("planned", "ready"),
            ],
        )
        self.assertEqual([event["seq"] for event in events], [1, 2, 3, 4, 5])
        self.assertTrue(all(event["demand_id"] == demand_id for event in events))

    # ---- 2. transição ilegal ----------------------------------------
    def test_illegal_transition_exits_2_and_writes_nothing(self) -> None:
        demand_id = self.add_demand()
        events_path = self.home / "state" / "events.jsonl"
        before = events_path.read_text(encoding="utf-8")
        transactions_path = self.home / "state" / "transactions.jsonl"
        before_tx = transactions_path.read_text(encoding="utf-8")
        proc = self.run_fail("transition", demand_id, "done")
        self.assertIn("transição ilegal", proc.stderr)
        self.assertEqual(self.demand(demand_id)["state"], "new")
        self.assertEqual(events_path.read_text(encoding="utf-8"), before)
        self.assertEqual(transactions_path.read_text(encoding="utf-8"), before_tx)

    # ---- 3. lease ----------------------------------------------------
    def test_running_requires_lease_and_expiry_returns_ready(self) -> None:
        demand_id = self.add_demand()
        self.advance(demand_id, "routed", "triaged", "planned", "ready")

        self.run_fail("transition", demand_id, "running")
        self.assertEqual(self.demand(demand_id)["state"], "ready")

        self.ok(
            "transition",
            demand_id,
            "running",
            "--owner",
            "zino",
            "--lease",
            "2026-11-01T15:30:00-03:00",
        )
        self.assertEqual(self.demand(demand_id)["state"], "running")

        self.ok("expire", now="2026-11-01T15:00:00-03:00")
        self.assertEqual(self.demand(demand_id)["state"], "running")

        out = self.ok("expire", now="2026-11-01T16:00:00-03:00")
        self.assertIn("1 lease", out)
        demand = self.demand(demand_id)
        self.assertEqual(demand["state"], "ready")
        self.assertIsNone(demand["owner"])
        self.assertIsNone(demand["lease_until"])

        last = self.jsonl("events.jsonl")[-1]
        self.assertEqual((last["from"], last["to"], last["actor"]), ("running", "ready", "system"))

    # ---- 4. policy ---------------------------------------------------
    def test_policy_lane_rules(self) -> None:
        sys.path.insert(0, str(REPO))
        from atlas_cli.policy import compute_lane

        base = {
            "difficulty": 1,
            "complexity": 1,
            "importance": 3,
            "urgency": 1,
            "responsibility": "agent",
            "agent_level": 1,
            "confidence": 0.9,
        }
        self.assertEqual(compute_lane(base), "auto_flash")
        self.assertEqual(compute_lane({**base, "responsibility": "me"}), "human")
        self.assertEqual(compute_lane({**base, "confidence": 0.5}), "human")
        self.assertEqual(
            compute_lane({**base, "importance": 5, "difficulty": 4, "agent_level": 0}),
            "approve_premium",
        )
        self.assertEqual(compute_lane({**base, "agent_level": 4}), "human")

        demand_id = self.add_demand("--importance", "5", "--difficulty", "4")
        out = self.ok("classify", demand_id, "--agent-level", "0", "--confidence", "0.9")
        self.assertIn("lane=approve_premium", out)
        out = self.ok("classify", demand_id, "--responsibility", "me")
        self.assertIn("lane=human", out)

    # ---- 5. digest ---------------------------------------------------
    def test_digest_rollover_send_and_briefing(self) -> None:
        demand_id = self.add_demand("--importance", "1", "--urgency", "1")

        out = self.ok("digest", now="2026-11-01T07:00:00-03:00")
        self.assertEqual(out.strip(), "")
        digests = self.jsonl("digests.jsonl")
        self.assertEqual(len(digests), 1)
        self.assertEqual(digests[-1]["sent"], 0)
        self.assertEqual(digests[-1]["reason"], "rollover")
        self.assertEqual(self.demand(demand_id)["rollover"]["digest_count"], 1)

        self.ok("digest", now="2026-11-01T07:30:00-03:00")
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["sent"], 0)
        self.assertEqual(self.demand(demand_id)["rollover"]["digest_count"], 2)

        out = self.ok("digest", now="2026-11-02T03:00:00-03:00")
        self.assertIn("1 itens", out)
        self.assertIn(demand_id, out)
        last = self.jsonl("digests.jsonl")[-1]
        self.assertEqual((last["sent"], last["reason"]), (1, "age"))

        # 11-02T03:00 + backoff 24h = 11-03T03:00: o briefing traz o lembrete.
        out = self.ok("digest", now="2026-11-03T08:00:00-03:00")
        self.assertIn("1 itens", out)
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["kind"], "briefing")
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["sent"], 1)

        before = len(self.jsonl("digests.jsonl"))
        out = self.ok("digest", "--dry-run", now="2026-11-03T09:00:00-03:00")
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(self.jsonl("digests.jsonl")), before)

    # ---- 6. KB -------------------------------------------------------
    def test_kb_tree_edges_check_and_search(self) -> None:
        root = self.ok(
            "kb", "add", "--parent", "", "--kind", "area",
            "--title", "Projetos", "--summary", "raiz",
        ).strip()
        article = self.ok(
            "kb", "add", "--parent", root, "--kind", "paper",
            "--title", "Graph search", "--summary", "busca em grafo",
        ).strip()
        child = self.ok(
            "kb", "add", "--parent", article, "--kind", "paper",
            "--title", "Second node", "--summary", "nó ligado",
        ).strip()
        stub = self.ok(
            "kb", "add", "--parent", article, "--kind", "note",
            "--title", "Stub node", "--summary", "",
        ).strip()
        self.ok("kb", "link", article, "cites", child, "--weight", "0.8")

        proc = self.run_fail("kb", "check", code=1)
        self.assertIn(stub, proc.stdout)

        rows = self.jsonl("kb.jsonl")
        for row in rows:
            if row["id"] == stub:
                row["summary"] = "preenchido"
        self.write_jsonl("kb.jsonl", rows)

        edges = self.jsonl("edges.jsonl")
        edges.append({"src": article, "rel": "self", "dst": article, "weight": 0.5, "note": ""})
        edges.append({"src": article, "rel": "ghost", "dst": "projetos/ghost", "weight": 0.5, "note": ""})
        edges.append({"src": article, "rel": "cites", "dst": child, "weight": 0.5, "note": ""})
        self.write_jsonl("edges.jsonl", edges)

        proc = self.run_fail("kb", "check", code=1)
        self.assertIn("self-edge", proc.stdout)
        self.assertIn("destino inexistente", proc.stdout)
        self.assertIn("aresta duplicada", proc.stdout)

        tree = self.ok("kb", "tree", root)
        self.assertIn("Graph search", tree)
        self.assertIn("  graph-search", tree)
        self.assertIn("    second-node", tree)

        path = self.ok("kb", "path", child).splitlines()
        self.assertEqual(path, [root, article, child])

        related = self.ok("kb", "related", article)
        self.assertIn("OUT", related)
        self.assertIn("cites", related)
        self.assertIn(child, related)

        # Limpa as arestas inválidas para a busca (o check anterior foi proposital).
        self.write_jsonl("edges.jsonl", edges[:1])

        results = self.ok("q", "graph").strip().splitlines()
        by_id = {line.split(" | ")[0]: line for line in results}
        self.assertIn(article, by_id)
        self.assertIn("fts", by_id[article])
        self.assertIn(child, by_id)
        self.assertIn("edge:cites", by_id[child])

    # ---- 6b. índice navegável ----------------------------------------
    def test_kb_index_writes_navigable_index(self) -> None:
        root = self.ok(
            "kb", "add", "--parent", "", "--kind", "area",
            "--title", "Projetos", "--summary", "raiz",
        ).strip()
        article = self.ok(
            "kb", "add", "--parent", root, "--kind", "paper",
            "--title", "Graph search", "--summary", "busca em grafo",
        ).strip()
        child = self.ok(
            "kb", "add", "--parent", article, "--kind", "paper",
            "--title", "Second node", "--summary", "x" * 130,
        ).strip()

        out = self.ok("kb", "index")
        self.assertIn("3 nós", out)
        index_path = self.home / "INDEX.md"
        text = index_path.read_text(encoding="utf-8")

        self.assertIn("# ÍNDICE — base de conhecimento", text)
        self.assertIn(f"Gerado em {NOW}. 3 nós, 0 arestas.", text)
        self.assertIn("## projetos (3 nós)", text)
        self.assertIn(f"- [Projetos](kb/{root}.md) — raiz", text)
        self.assertIn(f"  - [Graph search](kb/{article}.md) — busca em grafo", text)
        self.assertIn("x" * 120 + "…", text)
        self.assertNotIn("x" * 121, text)

        first = index_path.read_bytes()
        self.ok("kb", "index")
        self.assertEqual(first, index_path.read_bytes())
        self.assertTrue((self.home / "kb" / f"{child}.md").exists())

    def test_kb_check_flags_missing_file(self) -> None:
        root = self.ok(
            "kb", "add", "--parent", "", "--kind", "area",
            "--title", "Projetos", "--summary", "raiz",
        ).strip()
        self.ok("kb", "check")
        (self.home / "kb" / f"{root}.md").unlink()

        proc = self.run_fail("kb", "check", code=1)
        self.assertIn("arquivo ausente", proc.stdout)
        self.assertIn(root, proc.stdout)

    # ---- 7. rebuild --------------------------------------------------
    def test_rebuild_preserves_query_and_custom_bodies(self) -> None:
        root = self.ok(
            "kb", "add", "--parent", "", "--kind", "area",
            "--title", "Projetos", "--summary", "raiz",
        ).strip()
        body_file = self.home / "state" / "body.md"
        body_file.write_text("# Paper LiDAR\n\nCorpo original do paper.\n", encoding="utf-8")
        paper = self.ok(
            "kb", "add", "--parent", root, "--kind", "paper",
            "--title", "Paper LiDAR", "--summary", "paper",
            "--body-file", str(body_file),
        ).strip()

        demand_id = self.add_demand()
        self.advance(demand_id, "routed", "triaged", "planned", "ready")
        demand_md = self.home / "demands" / f"{demand_id}.md"
        demand_md.write_text(
            demand_md.read_text(encoding="utf-8") + "\nCORPO CUSTOM DA DEMANDA\n",
            encoding="utf-8",
        )

        before_list = self.ok("list")
        before_show = self.ok("show", demand_id)
        before_q = self.ok("q", "lidar")

        db = self.home / "state" / "hub.db"
        self.assertTrue(db.exists())
        db.unlink()
        self.ok("rebuild")

        self.assertEqual(self.ok("list"), before_list)
        self.assertEqual(self.ok("show", demand_id), before_show)
        self.assertEqual(self.ok("q", "lidar"), before_q)
        self.assertIn("CORPO CUSTOM DA DEMANDA", demand_md.read_text(encoding="utf-8"))
        self.assertIn(
            "Corpo original do paper",
            (self.home / "kb" / f"{paper}.md").read_text(encoding="utf-8"),
        )

    # ---- 8. bulletin -------------------------------------------------
    def test_bulletin_is_byte_identical(self) -> None:
        demand_id = self.add_demand("--due", "2026-11-02T10:00:00-03:00")
        self.advance(demand_id, "routed", "triaged")
        self.ok("digest", now="2026-11-01T14:00:00-03:00")

        first = self.ok("bulletin", now="2026-11-01T14:00:00-03:00")
        path = self.home / "BULLETIN.md"
        first_bytes = path.read_bytes()
        second = self.ok("bulletin", now="2026-11-01T14:00:00-03:00")
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, path.read_bytes())

        for section in ("AGORA", "EM EXECUÇÃO", "TRIAGEM", "PRÓXIMOS", "ROLLOVER", "ÚLTIMOS DIGESTS", "SAÚDE"):
            self.assertIn(f"## {section}", first)
        self.assertIn(demand_id, first)

    # ---- 9. seed -----------------------------------------------------
    def test_seed_demo_is_idempotent_and_search_finds_paper(self) -> None:
        seed = REPO / "examples" / "seed_demo.py"
        snapshots = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(seed), "--home", str(self.home), "--now", NOW],
                capture_output=True,
                text=True,
                env=self.env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snapshots.append((self.home / "state" / "events.jsonl").read_bytes())

        self.assertEqual(snapshots[0], snapshots[1], "seed gerou eventos diferentes na 2ª execução")
        created = {demand["id"]: demand["created_at"] for demand in self.jsonl("demands.jsonl")}
        events = self.jsonl("events.jsonl")
        for event in events:
            self.assertGreaterEqual(event["ts"], created[event["demand_id"]])
        stamps = [event["ts"] for event in events]
        self.assertEqual(stamps, sorted(stamps), "eventos fora de ordem temporal")

        self.assertEqual(len(self.jsonl("kb.jsonl")), 6)
        self.assertEqual(len(self.jsonl("edges.jsonl")), 6)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 7)
        self.assertEqual(len(self.jsonl("events.jsonl")), 28)

        first = self.ok("q", "lidar", now=NOW).splitlines()[0]
        self.assertIn("seq2seq-tcn", first)
        self.ok("rebuild", now=NOW)
        first = self.ok("q", "lidar", now=NOW).splitlines()[0]
        self.assertIn("seq2seq-tcn", first)

    def test_seed_respects_atlas_home_env(self) -> None:
        seed = REPO / "examples" / "seed_demo.py"
        home = Path(tempfile.mkdtemp(prefix="atlas-env-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, home, True)
        repo_state = REPO / "state"
        before = {
            name: (repo_state / name).read_bytes() if (repo_state / name).exists() else None
            for name in ("kb.jsonl", "demands.jsonl", "events.jsonl")
        }

        proc = subprocess.run(
            [sys.executable, str(seed), "--now", NOW],
            capture_output=True,
            text=True,
            env=dict(os.environ, ATLAS_HOME=str(home)),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        def rows(name: str) -> list[dict]:
            path = home / "state" / name
            self.assertTrue(path.exists(), f"{path} não foi criado pelo seed")
            text = path.read_text(encoding="utf-8")
            return [json.loads(line) for line in text.splitlines() if line.strip()]

        self.assertEqual(len(rows("kb.jsonl")), 6)
        self.assertEqual(len(rows("demands.jsonl")), 7)
        for name, data in before.items():
            path = repo_state / name
            after = path.read_bytes() if path.exists() else None
            self.assertEqual(after, data, f"seed escreveu em {path} ignorando ATLAS_HOME")

    # ---- 10. add: validação por tipo de link --------------------------
    def test_add_validates_kb_and_related_but_accepts_free_artifacts(self) -> None:
        demand_id = self.add_demand(
            "--artifact", "https://example.org/paper",
            "--artifact", "notas/relatorio.pdf",
        )
        self.assertEqual(
            self.demand(demand_id)["links"],
            {"kb": [], "related": [], "artifacts": ["https://example.org/paper", "notas/relatorio.pdf"]},
        )

        before = len(self.jsonl("demands.jsonl"))
        proc = self.run_fail("add", "--area", "projetos", "--title", "X", "--related", "dmd-inexistente")
        self.assertIn("demanda relacionada inexistente", proc.stderr)
        proc = self.run_fail("add", "--area", "projetos", "--title", "X", "--kb", "no-inexistente")
        self.assertIn("nó de KB inexistente", proc.stderr)
        self.assertEqual(len(self.jsonl("demands.jsonl")), before)

        self.ok("kb", "add", "--parent", "", "--kind", "area", "--title", "Projetos", "--summary", "raiz")
        dependent = self.add_demand("--related", demand_id, "--kb", "projetos")
        links = self.demand(dependent)["links"]
        self.assertEqual(links["related"], [demand_id])
        self.assertEqual(links["kb"], ["projetos"])

    # ---- 11. policy parcial ------------------------------------------
    def test_partial_policy_merges_recursive_defaults(self) -> None:
        policy_file = self.home / "policy" / "v1.json"
        policy_file.write_text('{"digest": {"threshold": 9}}\n', encoding="utf-8")

        sys.path.insert(0, str(REPO))
        from atlas_cli.policy import load_policy

        merged = load_policy(self.home)
        self.assertEqual(
            merged["digest"],
            {"threshold": 9, "rollover_max_hours": 12, "briefing_hour": 8},
        )
        self.assertEqual(
            merged["budget"], {"premium_daily_usd": 2.0, "max_auto_actions_per_hour": 6}
        )
        self.assertEqual(
            merged["lane_by_agent_level"],
            {"0": "observe", "1": "auto_flash", "2": "flash_review", "3": "approve_premium", "4": "human"},
        )
        self.assertEqual(len(merged["forbidden_auto"]), 10)

        # score 8: enviaria com threshold default (7), mas não com 9.
        self.add_demand("--importance", "3", "--urgency", "2")
        self.assertEqual(self.ok("digest", now="2026-11-01T07:00:00-03:00").strip(), "")
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["reason"], "rollover")

        # briefing_hour default (8) continua valendo.
        self.ok("digest", now="2026-11-02T08:00:00-03:00")
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["kind"], "briefing")

    # ---- 12. digest regenera BULLETIN --------------------------------
    def test_digest_regenerates_bulletin(self) -> None:
        self.add_demand("--importance", "1", "--urgency", "1")
        bulletin = self.home / "BULLETIN.md"

        self.ok("digest", now="2026-11-01T07:00:00-03:00")
        first = self.jsonl("digests.jsonl")[-1]
        self.assertEqual(first["sent"], 0)
        self.assertIn(first["id"], bulletin.read_text(encoding="utf-8"))

        self.ok("digest", now="2026-11-01T07:30:00-03:00")
        second = self.jsonl("digests.jsonl")[-1]
        text = bulletin.read_text(encoding="utf-8")
        self.assertIn(second["id"], text)
        self.assertIn(first["id"], text)

        self.ok("digest", "--force", now="2026-11-01T07:45:00-03:00")
        third = self.jsonl("digests.jsonl")[-1]
        self.assertEqual(third["sent"], 1)
        self.assertIn(third["id"], bulletin.read_text(encoding="utf-8"))

    # ---- 13. TRIAGEM no BULLETIN -------------------------------------
    def test_bulletin_lists_triage_states_once(self) -> None:
        demand_new = self.add_demand()
        demand_routed = self.add_demand()
        self.advance(demand_routed, "routed")
        demand_triaged = self.add_demand()
        self.advance(demand_triaged, "routed", "triaged")
        demand_planned = self.add_demand()
        self.advance(demand_planned, "routed", "triaged", "planned")
        demand_ready = self.add_demand()
        self.advance(demand_ready, "routed", "triaged", "planned", "ready")

        text = self.ok("bulletin", now=NOW)
        sections: dict[str, list[str]] = {}
        current = None
        for line in text.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
            elif current is not None and line.startswith("- "):
                sections[current].append(line[2:])

        triage = [item.split()[0] for item in sections["TRIAGEM"]]
        upcoming = [item.split()[0] for item in sections["PRÓXIMOS"]]
        expected = sorted([demand_new, demand_routed, demand_triaged, demand_planned])
        self.assertEqual(triage, expected)
        self.assertEqual(upcoming, [demand_ready])
        self.assertFalse(set(triage) & set(upcoming))
        self.assertLess(list(sections).index("TRIAGEM"), list(sections).index("PRÓXIMOS"))


if __name__ == "__main__":
    unittest.main()
