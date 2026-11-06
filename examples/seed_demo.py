#!/usr/bin/env python3
"""Seed de demonstração do ATLAS. Idempotente: reexecutar não duplica nada.

Uso:
    python3 examples/seed_demo.py [--now ISO8601] [--home DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from atlas_cli.cli import main as atlas_main  # noqa: E402
from atlas_cli.states import allowed_targets  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import parse_dt  # noqa: E402

DEFAULT_NOW = "2026-11-01T14:00:00-03:00"


class Seed:
    def __init__(self, now: str) -> None:
        self.now = now
        self.base = parse_dt(now)

    def at(self, minutes: int) -> str:
        return (self.base + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def run(self, *argv: str, now: str | None = None) -> None:
        code = atlas_main(["--now", now or self.now, *argv])
        if code != 0:
            raise SystemExit(f"falha no seed: atlas {' '.join(argv)} -> exit {code}")

    # -- KB -------------------------------------------------------------
    def kb_titles(self) -> dict[str, str]:
        return {node["title"]: node["id"] for node in Store().kb_nodes()}

    def node(
        self,
        parent: str,
        kind: str,
        title: str,
        summary: str,
        *,
        importance: int = 3,
        body: str | None = None,
    ) -> str:
        titles = self.kb_titles()
        if title in titles:
            return titles[title]
        argv = [
            "kb", "add",
            "--parent", parent,
            "--kind", kind,
            "--title", title,
            "--summary", summary,
            "--importance", str(importance),
        ]
        body_path = None
        if body:
            body_path = Store().home / "state" / ".seed-body.md"
            body_path.write_text(body, encoding="utf-8")
            argv += ["--body-file", str(body_path)]
        self.run(*argv)
        if body_path is not None:
            body_path.unlink(missing_ok=True)
        return self.kb_titles()[title]

    def edge(self, src: str, rel: str, dst: str, weight: float, note: str) -> None:
        for edge in Store().edges():
            if (edge["src"], edge["rel"], edge["dst"]) == (src, rel, dst):
                return
        self.run("kb", "link", src, rel, dst, "--weight", str(weight), "--note", note)

    # -- demandas -------------------------------------------------------
    def demands_by_title(self) -> dict[str, dict]:
        return {demand["title"]: demand for demand in Store().demands()}

    def demand(self, title: str, minutes: int, *argv: str) -> None:
        if title in self.demands_by_title():
            return
        self.run("add", "--title", title, *argv, now=self.at(minutes))

    def advance(self, title: str, steps: list[tuple[str, list[str]]]) -> None:
        """Aplica apenas passos à frente do estado atual (idempotente).

        O estado atual, quando aparece no roteiro, avança o cursor: transições
        laterais (ex.: `running -> ready` por lease) não são reaplicadas.
        """
        targets = [target for target, _ in steps]
        demand = self.demands_by_title()[title]
        start = 0
        if demand["state"] in targets:
            start = targets.index(demand["state"]) + 1
        elif demand["state"] in ("done", "dropped"):
            return
        for index, (target, extra) in enumerate(steps[start:], start):
            demand = self.demands_by_title()[title]
            if target not in allowed_targets(demand["state"]):
                continue
            stamp = (
                parse_dt(demand["created_at"]) + timedelta(minutes=index + 1)
            ).isoformat(timespec="seconds")
            self.run("transition", demand["id"], target, *extra, now=stamp)


def main() -> int:
    parser = argparse.ArgumentParser(description="seed de demonstração do ATLAS")
    parser.add_argument("--now", default=DEFAULT_NOW)
    parser.add_argument("--home", default=os.environ.get("ATLAS_HOME", str(REPO)))
    args = parser.parse_args()

    os.environ["ATLAS_HOME"] = str(Path(args.home).expanduser().resolve())
    seed = Seed(args.now)

    seed.run("init")

    root = seed.node("", "area", "Projetos", "Área raiz de projetos.")
    fapemig = seed.node(root, "project", "FAPEMIG", "Projeto de pesquisa financiado pela FAPEMIG.")
    article = seed.node(
        fapemig,
        "paper",
        "2025 Seq2Seq TCN para LiDAR",
        "Artigo sobre sequências LiDAR com TCN.",
        importance=5,
        body=(
            "# 2025 Seq2Seq TCN para LiDAR\n\n"
            "Método de detecção de obstáculos em varreduras LiDAR usando "
            "seq2seq com TCN. Avaliamos generalização entre cenas urbanas e "
            "rodoviárias. O pipeline consome nuvens LiDAR brutas e produz "
            "trajetórias e máscaras.\n\n"
            "## Relevância\n\nBase para a ablação do projeto FAPEMIG.\n"
        ),
    )
    article2 = seed.node(
        fapemig,
        "paper",
        "2024 Grafo urbano em LiDAR",
        "Artigo sobre grafos de cenas urbanas.",
        importance=4,
        body="# 2024 Grafo urbano em LiDAR\n\nRepresentação em grafo para cenas urbanas.\n",
    )
    dataset = seed.node(
        fapemig,
        "dataset",
        "Dataset LiDAR urbano",
        "Nuvens LiDAR de 12 quarteirões.",
        importance=4,
        body="# Dataset LiDAR urbano\n\n12 quarteirões anotados. Uso restrito ao projeto.\n",
    )
    person = seed.node(
        fapemig,
        "person",
        "Ana Souza",
        "Orientanda responsável pelo pipeline.",
        importance=3,
        body="# Ana Souza\n\nOrientanda; mantém o pipeline de dados.\n",
    )

    seed.edge(fapemig, "funds", article, 0.9, "financiamento FAPEMIG")
    seed.edge(fapemig, "funds", article2, 0.8, "financiamento FAPEMIG")
    seed.edge(article, "cites", dataset, 0.8, "usa o dataset")
    seed.edge(article, "cites", article2, 0.7, "baseline de grafo")
    seed.edge(article2, "uses", dataset, 0.7, "avaliação no dataset")
    seed.edge(person, "authors", article, 0.9, "autoria")

    seed.demand(
        "Revisar proposta FAPEMIG",
        0,
        "--area", "projetos",
        "--importance", "5",
        "--urgency", "4",
        "--responsibility", "me",
        "--difficulty", "4",
        "--kb", article,
        "--source-reader", "email",
        "--source-ref", "msg-418",
    )
    seed.advance(
        "Revisar proposta FAPEMIG",
        [
            ("routed", []),
            ("triaged", []),
            ("waiting_user", ["--reason", "aguardando parecer"]),
        ],
    )

    seed.demand(
        "Rodar ablação do TCN",
        10,
        "--area", "projetos",
        "--importance", "4",
        "--urgency", "3",
        "--difficulty", "3",
        "--kb", article,
        "--due", "2026-11-05T18:00:00-03:00",
    )
    seed.advance(
        "Rodar ablação do TCN",
        [
            ("routed", []),
            ("triaged", []),
            ("planned", []),
            ("ready", []),
            ("running", ["--owner", "zino", "--lease", "2026-11-01T16:00:00-03:00"]),
        ],
    )

    seed.demand(
        "Escrever seção de métodos",
        20,
        "--area", "faculdade",
        "--importance", "3",
        "--urgency", "2",
        "--due", "2026-11-04T23:00:00-03:00",
    )
    seed.advance(
        "Escrever seção de métodos",
        [("routed", []), ("triaged", []), ("planned", []), ("ready", [])],
    )

    seed.demand(
        "Organizar planilha de gastos",
        30,
        "--area", "financas",
        "--importance", "2",
        "--urgency", "1",
    )
    seed.advance(
        "Organizar planilha de gastos",
        [("routed", []), ("triaged", []), ("planned", [])],
    )

    seed.demand(
        "Ler capítulo 4 de otimização",
        40,
        "--area", "faculdade",
        "--importance", "2",
        "--urgency", "1",
    )
    seed.advance(
        "Ler capítulo 4 de otimização",
        [
            ("routed", []),
            ("triaged", []),
            ("planned", []),
            ("ready", []),
            ("running", ["--owner", "zino", "--lease", "2026-11-01T15:00:00-03:00"]),
            ("done", ["--result", "capítulo lido e resumido na KB"]),
        ],
    )

    # Dois itens de inbox (estado new) para o próximo digest.
    seed.demand(
        "Mensagem do orientador sobre dataset",
        50,
        "--area", "mensagens",
        "--importance", "2",
        "--urgency", "1",
        "--kb", dataset,
        "--source-reader", "inbox",
        "--source-ref", "inbox-77",
    )
    seed.demand(
        "Paper sobre LiDAR urbano",
        60,
        "--area", "internet",
        "--importance", "2",
        "--urgency", "1",
        "--kb", article,
        "--source-reader", "inbox",
        "--source-ref", "inbox-81",
    )

    store = Store(now=args.now)
    print(
        f"seed ok: {len(store.kb_nodes())} nós, {len(store.edges())} arestas, "
        f"{len(store.demands())} demandas"
    )
    print(f"próximo passo: bin/atlas --now {args.now} bulletin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
