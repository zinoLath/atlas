# AGENTS.md — atlas

Regras específicas deste repositório.

## Escopo

- M0 do ATLAS: CLI Python 3.12+ **só stdlib**. Não adicione dependências
  (`pytest`, `PyYAML`, `requests`) nem rede.
- Fonte de verdade: `state/*.jsonl`. `state/hub.db` e os `.md` são derivados;
  `atlas rebuild` deve sempre reconstruí-los.
- Todo comando aceita `--now ISO8601`; preserve o determinismo (ordenação,
  IDs, `BULLETIN.md` byte-idêntico).

## Convenções

- Código, mensagens de CLI e docs em PT-BR; identificadores de API em inglês.
- Erros de uso/estado: `AtlasError` com exit 2. `kb check` com problemas: exit 1.
- Escrita canônica: `util.write_jsonl` (tmp+rename). Nunca append direto no
  arquivo canônico.
- Frontmatter é gerado; o corpo markdown é preservado em re-render.
- Não commitar nem dar push sem confirmação do usuário.

## Verificação

```
python3 -m unittest discover -s tests -v
python3 examples/seed_demo.py --now 2026-11-01T14:00:00-03:00
```

O seed é idempotente; rode duas vezes ao mexer em estados ou KB.
